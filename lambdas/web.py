"""The one public origin, behind a Lambda Function URL.

    GET  /                the help button (the subject's page)
    POST /trigger         press it: start an escalation, answer with its id
    GET  /claim/{token}   the responder's page: who, where, and whether anyone has gone
    POST /claim/{token}   "I'm going now" - one conditional write decides the race
    POST /cancel          she is OK: mark it cancelled; the machine tells everyone
    GET  /status/{id}     what her screen shows: open, who is coming, cancelled

GET never writes. Mail clients and link scanners fetch every link in an email;
if a GET could claim, a corporate proxy would be on its way to Sunita instead
of Ravi.
"""

import base64
import hashlib
import json
import os
import time
import urllib.parse
import uuid
from datetime import datetime, timedelta, timezone
from string import Template

import boto3
from botocore.exceptions import ClientError

from ranking import TIER_SIZE, rank

ddb = boto3.client("dynamodb")
sfn = boto3.client("stepfunctions")
kms = boto3.client("kms")
lam = boto3.client("lambda")

CONTACTS = os.environ["CONTACTS_TABLE"]
SUBJECTS = os.environ["SUBJECTS_TABLE"]
INCIDENTS = os.environ["INCIDENTS_TABLE"]
NOTIFICATIONS = os.environ["NOTIFICATIONS_TABLE"]
RESPONSE_STATS = os.environ["RESPONSE_STATS_TABLE"]
STATE_MACHINE_ARN = os.environ["STATE_MACHINE_ARN"]
BROADCAST_FN = os.environ["BROADCAST_FN"]
SUBJECT_ID = os.environ["SUBJECT_ID"]
WAIT_S = int(os.environ.get("WAIT_S", "60"))
MAX_TIER = int(os.environ.get("MAX_TIER", "3"))
IST = timezone(timedelta(hours=5, minutes=30))


# --- routing ------------------------------------------------------------------

def handler(event, context):
    method = event["requestContext"]["http"]["method"]
    path = event.get("rawPath", "/")

    if method == "GET" and path == "/":
        return html(200, trigger_page())
    if method == "POST" and path == "/trigger":
        return trigger()
    if path.startswith("/claim/") and method in ("GET", "POST"):
        token = path[len("/claim/"):]
        return claim_page(token) if method == "GET" else claim(token)
    if method == "POST" and path == "/cancel":
        return cancel(event)
    if method == "GET" and path.startswith("/status/"):
        return status(path[len("/status/"):])
    return html(404, NOT_FOUND)


def cancel(event):
    body = event.get("body") or "{}"
    if event.get("isBase64Encoded"):
        body = base64.b64decode(body).decode()
    incident_id = json.loads(body).get("incident_id", "")
    if not incident_id:
        return jsonr(400, {"error": "incident_id required"})
    now = int(time.time())
    try:
        old = ddb.update_item(
            TableName=INCIDENTS, Key={"incident_id": {"S": incident_id}},
            UpdateExpression="SET #s = :c, cancelled_at = :t",
            # She may be fine after all, even once someone is on the way: they are told too.
            ConditionExpression="#s IN (:open, :fallback, :claimed)",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":c": {"S": "CANCELLED"}, ":open": {"S": "OPEN"}, ":fallback": {"S": "FALLBACK"},
                                       ":claimed": {"S": "CLAIMED"}, ":t": {"N": str(now)}},
            ReturnValues="ALL_OLD",
        )
    except ClientError as e:
        if e.response["Error"]["Code"] != "ConditionalCheckFailedException":
            raise
    else:
        if old["Attributes"]["status"]["S"] == "CLAIMED":
            # The machine finished when the claim was broadcast; tell everyone directly.
            lam.invoke(FunctionName=BROADCAST_FN, InvocationType="RequestResponse",
                       Payload=json.dumps({"kind": "false_alarm", "incident_id": incident_id}).encode())
        else:
            wake(old["Attributes"], "cancelled")
    print(json.dumps({"component": "web", "event": "cancel", "incident_id": incident_id}))
    return status(incident_id)


def wake(old_row, why):
    """Hand the parked task token back so the machine reads the row now, not at the
    tier boundary. Best-effort: a stale token means the machine already moved on."""
    token = old_row.get("task_token", {}).get("S")
    if not token:
        return
    try:
        sfn.send_task_success(taskToken=token, output=json.dumps({"woken_by": why}))
        print(json.dumps({"component": "web", "event": "woke", "why": why}))
    except ClientError as e:
        print(json.dumps({"component": "web", "event": "wake_skipped", "why": why,
                          "error": e.response["Error"]["Code"]}))


def status(incident_id):
    """What is true on the incident row, and nothing that is not on it."""
    item = ddb.get_item(TableName=INCIDENTS, Key={"incident_id": {"S": incident_id}},
                        ConsistentRead=True).get("Item")
    if item is None:
        return jsonr(404, {"error": "no such incident"})
    out = {"incident_id": incident_id, "status": item["status"]["S"]}
    if "claimed_by_name" in item:
        out["claimed_by_name"] = item["claimed_by_name"]["S"]
        out["claimed_at"] = fmt_time(item["claimed_at"]["N"])
    if "record_opened_at" in item:
        out["record_opened_at"] = fmt_time(item["record_opened_at"]["N"])
    if "cancelled_at" in item:
        out["cancelled_at"] = fmt_time(item["cancelled_at"]["N"])
    return jsonr(200, out)


# --- routes -------------------------------------------------------------------

def trigger_page():
    names = first_circle_names()
    if names:
        told = f"<strong>{escape(join_names(names))}</strong> will be told straight away."
        disabled = ""
    else:
        told = "No one has been added yet, so this button cannot reach anyone."
        disabled = "disabled"
    return PAGE.substitute(told=told, disabled=disabled, names_json=json.dumps(names))


def trigger():
    incident_id = uuid.uuid4().hex[:12]
    payload = {
        "incident_id": incident_id,
        "subject_id": SUBJECT_ID,
        "wait_s": WAIT_S,
        "max_tier": MAX_TIER,
    }
    started = sfn.start_execution(
        stateMachineArn=STATE_MACHINE_ARN,
        name=incident_id,
        input=json.dumps(payload),
    )
    print(json.dumps({"component": "web", "event": "trigger", "incident_id": incident_id,
                      "execution_arn": started["executionArn"]}))
    return jsonr(200, {"incident_id": incident_id, "told": first_circle_names()})


# --- claim --------------------------------------------------------------------

def resolve(token):
    """Token -> (notification row, incident row) or None. Never says which half was wrong."""
    if not token or len(token) > 64:
        return None
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    rows = ddb.query(
        TableName=NOTIFICATIONS,
        IndexName="token_hash-index",
        KeyConditionExpression="token_hash = :h",
        ExpressionAttributeValues={":h": {"S": token_hash}},
    )["Items"]
    if len(rows) != 1:
        return None
    note = rows[0]
    incident = ddb.get_item(TableName=INCIDENTS, Key={"incident_id": note["incident_id"]},
                            ConsistentRead=True).get("Item")
    if incident is None:
        return None
    return note, incident


def claim_page(token):
    found = resolve(token)
    if found is None:
        return html(404, BAD_LINK)
    note, incident = found
    return html(200, render_claim_state(note, incident))


def claim(token):
    found = resolve(token)
    if found is None:
        return html(404, BAD_LINK)
    note, incident = found
    contact = ddb.get_item(TableName=CONTACTS, Key={"subject_id": incident["subject_id"],
                                                    "contact_id": note["contact_id"]})["Item"]
    now = int(time.time())
    try:
        old = ddb.update_item(
            TableName=INCIDENTS,
            Key={"incident_id": incident["incident_id"]},
            UpdateExpression="SET #s = :claimed, claimed_by = :c, claimed_by_name = :n, claimed_at = :t",
            # A late answer on an alert that widened to everyone is still a good answer.
            ConditionExpression="#s IN (:open, :fallback)",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":claimed": {"S": "CLAIMED"}, ":open": {"S": "OPEN"}, ":fallback": {"S": "FALLBACK"},
                ":c": note["contact_id"], ":n": contact["name"], ":t": {"N": str(now)},
            },
            ReturnValues="ALL_OLD",
        )
        won = True
        wake(old["Attributes"], "claimed")
    except ClientError as e:
        if e.response["Error"]["Code"] != "ConditionalCheckFailedException":
            raise
        won = False
    count_answer(note, now)
    print(json.dumps({"component": "web", "event": "claim", "incident_id": incident["incident_id"]["S"],
                      "contact_id": note["contact_id"]["S"], "won": won}))
    # Either way, show what is true now: the row after the write, not what we hoped.
    incident = ddb.get_item(TableName=INCIDENTS, Key={"incident_id": incident["incident_id"]},
                            ConsistentRead=True)["Item"]
    return html(200, render_claim_state(note, incident, on_claim=won))


def count_answer(note, now):
    """They answered a page: one response and its latency, against the hour the page went
    out. Once per page, win or lose - a lost race still says they were reachable."""
    try:
        ddb.update_item(TableName=NOTIFICATIONS,
                        Key={"incident_id": note["incident_id"], "contact_tier": note["contact_tier"]},
                        UpdateExpression="SET responded_at = :t",
                        ConditionExpression="attribute_not_exists(responded_at)",
                        ExpressionAttributeValues={":t": {"N": str(now)}})
    except ClientError as e:
        if e.response["Error"]["Code"] != "ConditionalCheckFailedException":
            raise
        return
    if "sent_at" not in note or "bucket" not in note:
        return  # no page time on record, so no latency to count
    ddb.update_item(TableName=RESPONSE_STATS,
                    Key={"contact_id": note["contact_id"], "bucket": note["bucket"]},
                    UpdateExpression="ADD responses :one, total_latency_ms :ms",
                    ExpressionAttributeValues={":one": {"N": "1"},
                                               ":ms": {"N": str(max(0, now - int(note["sent_at"]["N"])) * 1000)}})


def render_claim_state(note, incident, on_claim=False):
    subject = ddb.get_item(TableName=SUBJECTS, Key={"subject_id": incident["subject_id"]})["Item"]
    status = incident["status"]["S"]
    name = escape(subject["name"]["S"])
    address = subject["address"]["S"]
    parts = address.split(", ")
    ctx = {
        "name": name,
        "address_line1": escape(", ".join(parts[:2])),
        "address_line2": escape(", ".join(parts[2:])),
        "maps_url": "https://maps.google.com/?q=" + urllib.parse.quote(address),
        "pressed_at": fmt_time(incident["started_at"]["N"]),
    }
    if status in ("OPEN", "FALLBACK"):
        reached = ddb.query(TableName=NOTIFICATIONS, KeyConditionExpression="incident_id = :i",
                            FilterExpression="delivered = :t",
                            ExpressionAttributeValues={":i": incident["incident_id"], ":t": {"BOOL": True}})["Count"]
        others = max(reached - 1, 0)
        ctx["contacted_count"] = reached
        ctx["others"] = f"{others} other{'s were' if others != 1 else ' was'} contacted too" if others else "you are the only one contacted"
        return CLAIM_ACTIONABLE.substitute(ctx)
    if status == "CLAIMED":
        ctx["claimed_at"] = fmt_time(incident["claimed_at"]["N"])
        if incident["claimed_by"]["S"] == note["contact_id"]["S"]:
            ctx["record"] = open_record(subject, incident["incident_id"]["S"], note["contact_id"]["S"], on_claim)
            return CLAIM_YOURS.substitute(ctx)
        ctx["claimer"] = escape(incident["claimed_by_name"]["S"])
        return CLAIM_TAKEN.substitute(ctx)
    if status == "CANCELLED":
        ctx["cancelled_at"] = fmt_time(incident["cancelled_at"]["N"])
        return CLAIM_CANCELLED.substitute(ctx)
    ctx["ended_at"] = fmt_time(incident.get("failed_at", incident.get("ended_at", incident["started_at"]))["N"])
    return CLAIM_OVER.substitute(ctx)


def open_record(subject, incident_id, contact_id, on_claim):
    """Her medical notes, for the one person who is going. The ciphertext is opened
    with her id as encryption context, and every opening is logged with who and when.
    The opening that comes with the claim itself is also written on her incident, so
    her screen can say who has them. Later refreshes are logged but write nothing."""
    if "record" not in subject:
        return "<p class=\"muted\">No medical notes on file.</p>"
    subject_id = subject["subject_id"]["S"]
    try:
        plain = kms.decrypt(CiphertextBlob=subject["record"]["B"],
                            EncryptionContext={"subject_id": subject_id})["Plaintext"].decode()
    except ClientError as e:
        print(json.dumps({"component": "web", "event": "record_release_failed", "incident_id": incident_id,
                          "contact_id": contact_id, "subject_id": subject_id, "error": e.response["Error"]["Code"]}))
        return "<p class=\"muted\">Her medical notes could not be opened. If it matters, call 112.</p>"
    now = datetime.now(timezone.utc)
    print(json.dumps({"component": "web", "event": "record_released", "incident_id": incident_id,
                      "contact_id": contact_id, "subject_id": subject_id, "at": now.isoformat(timespec="seconds")}))
    if on_claim:
        ddb.update_item(TableName=INCIDENTS, Key={"incident_id": {"S": incident_id}},
                        UpdateExpression="SET record_opened_by = :c, record_opened_at = :t",
                        ExpressionAttributeValues={":c": {"S": contact_id}, ":t": {"N": str(int(now.timestamp()))}})
    return "<p>" + "<br>".join(escape(line) for line in plain.splitlines()) + "</p>"


def fmt_time(epoch):
    return datetime.fromtimestamp(int(epoch), IST).strftime("%-I:%M %p").lower()


# --- data ---------------------------------------------------------------------

def first_circle_names():
    """The people a press right now would page first: the same ranking SelectTier uses,
    with nobody reached yet."""
    return [c["name"] for c in rank(SUBJECT_ID, time.time())[:TIER_SIZE]]


def join_names(names):
    if len(names) == 1:
        return names[0]
    return ", ".join(names[:-1]) + " and " + names[-1]


# --- responses ----------------------------------------------------------------

def html(status, body):
    return {
        "statusCode": status,
        "headers": {"content-type": "text/html; charset=utf-8", "cache-control": "no-store"},
        "body": body,
    }


def jsonr(status, obj):
    return {
        "statusCode": status,
        "headers": {"content-type": "application/json", "cache-control": "no-store"},
        "body": json.dumps(obj),
    }


def escape(s):
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# --- pages --------------------------------------------------------------------

STYLE = """
:root {
  --bg: #FAF9F7; --surface: #FFFFFF; --ink: #14110F; --ink-muted: #4A4442; --border: #8A827A;
  --emergency: #A4161A; --on-emergency: #FFFFFF; --emergency-active: #7F1113;
  --safe: #14532D; --safe-bg: #E4F2E8; --caution: #7A4106; --caution-bg: #FDF1E0; --over: #55504D;
  --text-xs: 16px; --text-sm: 18px; --text-base: 20px; --text-lg: 24px; --text-xl: 32px;
  --text-2xl: 40px; --text-3xl: 56px; --text-action: 44px;
  --space-3: 12px; --space-4: 16px; --space-6: 24px; --space-8: 32px; --space-12: 48px;
  --radius: 12px; --radius-btn: 20px; --target-min: 64px;
}
* { box-sizing: border-box; }
html { background: var(--bg); color: var(--ink);
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Noto Sans",
    "Noto Sans Devanagari", "Helvetica Neue", Arial, sans-serif;
  font-size: var(--text-base); line-height: 1.6; }
body { margin: 0; padding: var(--space-6); max-width: 36rem; margin-inline: auto; }
@media (max-width: 360px) { body { padding: var(--space-4); } }
h1 { font-size: var(--text-2xl); line-height: 1.2; font-weight: 700; margin: 0 0 var(--space-6); }
h2 { font-size: var(--text-xl); line-height: 1.3; font-weight: 700; margin: 0 0 var(--space-4); }
p { margin: 0 0 var(--space-4); }
.muted { color: var(--ink-muted); }
.time { font-size: var(--text-xs); color: var(--ink-muted); }
.btn { display: block; width: 100%; min-height: var(--target-min); border: 0; cursor: pointer;
  font: inherit; font-weight: 700; border-radius: var(--radius); padding: var(--space-4); }
.btn:focus-visible { outline: 4px solid var(--ink); outline-offset: 3px; }
.btn-emergency { min-height: 240px; font-size: var(--text-action); line-height: 1.15;
  letter-spacing: 0.02em; text-transform: uppercase; border-radius: var(--radius-btn);
  background: var(--emergency); color: var(--on-emergency); }
.btn-emergency:active { background: var(--emergency-active); }
.btn-emergency[disabled] { background: var(--border); cursor: not-allowed; }
.btn-secondary { background: var(--surface); color: var(--ink); border: 3px solid var(--border);
  font-size: var(--text-lg); margin-top: var(--space-8); }
.card { border-radius: var(--radius); padding: var(--space-6); margin-bottom: var(--space-6); }
.card-safe { background: var(--safe-bg); color: var(--safe); }
.card-caution { background: var(--caution-bg); color: var(--caution); }
.card-emergency { background: var(--surface); border: 3px solid var(--emergency); color: var(--emergency); }
.banner { font-size: var(--text-3xl); line-height: 1.1; font-weight: 700; letter-spacing: 0.02em;
  color: var(--emergency); margin: 0 0 var(--space-4); }
.lead { font-size: var(--text-lg); margin-top: calc(-1 * var(--space-4)); }
.address { font-size: var(--text-lg); line-height: 1.5; }
.btn-inline { display: inline-block; width: auto; min-width: var(--target-min); margin-top: 0; text-decoration: none;
  text-align: center; }
.btn-claim { min-height: 96px; font-size: var(--text-xl); text-transform: none; letter-spacing: 0; }
.card h1 { font-size: var(--text-xl); margin-bottom: var(--space-3); }
.card p { margin: 0; }
.card-over { background: var(--surface); border: 3px solid var(--border); color: var(--over); }
hr { border: 0; border-top: 2px solid var(--border); margin: var(--space-6) 0; }
.foot { margin-top: var(--space-12); font-size: var(--text-sm); color: var(--ink-muted); }
.foot a { color: inherit; }
[hidden] { display: none !important; }
"""

PAGE = Template("""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>I need help</title>
<style>""" + STYLE + """</style>
</head>
<body>

<main id="idle">
  <h1>I NEED HELP</h1>
  <button class="btn btn-emergency" id="press" $disabled>I need help</button>
  <p style="margin-top: var(--space-6)">Press once.<br>$told</p>
</main>

<main id="sent" hidden>
  <h2>Help is being called</h2>
  <p><strong id="sent-names"></strong> have been told.<br><span class="time" id="sent-time"></span></p>
  <button class="btn btn-secondary" id="cancel">Cancel — I'm OK</button>
</main>

<main id="coming" hidden>
  <div class="card card-safe"><h2>&#10003; <span id="coming-name"></span> is coming</h2>
  <p>On the way now.<br><span class="time" id="coming-time"></span></p>
  <p id="coming-record" hidden><span id="coming-record-name"></span> has your medical notes.</p></div>
  <button class="btn btn-secondary" id="cancel2">Cancel — I'm OK</button>
</main>

<main id="cancelled" hidden>
  <div class="card card-caution"><h2>Cancelled</h2>
  <p>Everyone has been told it was a false alarm.</p></div>
  <button class="btn btn-emergency" id="again">I need help</button>
</main>

<main id="failed" hidden>
  <div class="card card-emergency"><h2>&#9888; Couldn't send</h2></div>
  <button class="btn btn-emergency" id="retry">Try again</button>
  <p style="margin-top: var(--space-6)"><strong>Or call 112 now.</strong></p>
</main>

<p class="foot">Not working? Call <a href="tel:112">112</a></p>

<script>
(function () {
  var names = $names_json;
  var incident = null, poll = null;
  var show = function (id) {
    ["idle", "sent", "failed", "coming", "cancelled"].forEach(function (s) { document.getElementById(s).hidden = (s !== id); });
  };
  var stopPoll = function () { if (poll) { clearInterval(poll); poll = null; } };
  var check = function () {
    if (!incident) return;
    fetch("/status/" + incident).then(function (r) { return r.json(); }).then(function (s) {
      if (s.status === "CLAIMED") {
        document.getElementById("coming-name").textContent = s.claimed_by_name;
        document.getElementById("coming-time").textContent = s.claimed_at;
        document.getElementById("coming-record-name").textContent = s.claimed_by_name;
        document.getElementById("coming-record").hidden = !s.record_opened_at;
        show("coming"); stopPoll();
      } else if (s.status === "CANCELLED") { show("cancelled"); stopPoll(); }
    }).catch(function () {});
  };
  var cancel = function () {
    if (!incident) return;
    fetch("/cancel", { method: "POST", headers: { "content-type": "application/json" },
      body: JSON.stringify({ incident_id: incident }) })
      .then(function (r) { return r.json(); })
      .then(function (s) { if (s.status === "CANCELLED") { show("cancelled"); stopPoll(); } else { check(); } })
      .catch(function () {});
  };
  var joinNames = function (n) {
    return n.length < 2 ? n.join("") : n.slice(0, -1).join(", ") + " and " + n[n.length - 1];
  };
  var press = function () {
    var btn = document.getElementById("press");
    btn.disabled = true;
    fetch("/trigger", { method: "POST" })
      .then(function (r) { if (!r.ok) throw new Error(r.status); return r.json(); })
      .then(function (data) {
        incident = data.incident_id;
        document.getElementById("sent-names").textContent = joinNames(data.told || names);
        document.getElementById("sent-time").textContent =
          new Date().toLocaleTimeString("en-IN", { hour: "numeric", minute: "2-digit" });
        show("sent");
        stopPoll(); poll = setInterval(check, 3000);
      })
      .catch(function () { btn.disabled = false; show("failed"); });
  };
  document.getElementById("press").addEventListener("click", press);
  document.getElementById("retry").addEventListener("click", function () { show("idle"); press(); });
  document.getElementById("cancel").addEventListener("click", cancel);
  document.getElementById("cancel2").addEventListener("click", cancel);
  document.getElementById("again").addEventListener("click", function () {
    incident = null; document.getElementById("press").disabled = false; show("idle"); press();
  });
})();
</script>
</body>
</html>
""")

def _page(title, body):
    return ("""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><meta name="robots" content="noindex">
<title>""" + title + """</title><style>""" + STYLE + """</style></head><body>
""" + body + """
<p class="foot">Ambulance: <a href="tel:112">112</a></p>
</body></html>""")


CLAIM_ACTIONABLE = Template(_page("Emergency — $name needs help", """
<p class="banner">EMERGENCY</p>
<h1>$name</h1>
<p class="lead">pressed her help button at <strong>$pressed_at</strong></p>
<p class="address"><strong>$address_line1</strong><br>$address_line2</p>
<p><a class="btn btn-secondary btn-inline" href="$maps_url" target="_blank" rel="noopener">Open in maps</a></p>
<p>You are one of <strong>$contacted_count people</strong> contacted.<br><strong>No one has gone yet.</strong></p>
<form method="post"><button class="btn btn-emergency btn-claim" type="submit">I'm going now</button></form>
<p class="muted">Can't go? That's alright — $others.</p>
"""))

CLAIM_YOURS = Template(_page("You're going", """
<div class="card card-safe"><h1>&#10003; You're going</h1>
<p>$name has been told you're coming.<br>Everyone else contacted has been told as well.</p></div>
<p class="address"><strong>$address_line1</strong><br>$address_line2</p>
<p><a class="btn btn-secondary btn-inline" href="$maps_url" target="_blank" rel="noopener">Open in maps</a></p>
<hr>
<h2>$name's medical notes</h2>
<p class="muted">Released because you're going. $name is told you opened this.</p>
$record
<hr>
<p><strong>Ambulance: <a href="tel:112">112</a></strong></p>
"""))

CLAIM_TAKEN = Template(_page("$claimer is already on the way", """
<div class="card card-safe"><h1>&#10003; $claimer is already on the way</h1>
<p>They said they were going at <strong>$claimed_at</strong>.<br>Nothing more is needed.</p></div>
<p class="muted">Thank you for opening this.</p>
"""))

CLAIM_CANCELLED = Template(_page("$name cancelled this alert", """
<div class="card card-caution"><h1>&#8856; $name cancelled this alert</h1>
<p>She marked it a false alarm at <strong>$cancelled_at</strong>.<br>Nothing is needed.</p></div>
"""))

CLAIM_OVER = Template(_page("This alert is over", """
<div class="card card-over"><h1>This alert is over</h1>
<p>It ended at <strong>$ended_at</strong>. Nothing is needed.</p></div>
"""))

BAD_LINK = _page("This link isn't valid", """
<h1>This link isn't valid</h1>
<p>It may have been mistyped, or it belongs to an alert that has ended.</p>
<p><strong>If you think someone needs help, call 112.</strong></p>
""")

NOT_FOUND = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Not found</title>
<style>""" + STYLE + """</style></head><body>
<h1>That page does not exist</h1>
<p class="foot">Not working? Call <a href="tel:112">112</a></p>
</body></html>"""
