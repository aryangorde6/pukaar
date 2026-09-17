"""The one public origin, behind a Lambda Function URL.

    GET  /          the help button (the subject's page)
    POST /trigger   press it: start an escalation, answer with its id

Later routes (claim, cancel, status) attach here. GET never writes.
"""

import base64
import json
import os
import uuid
from string import Template

import boto3

ddb = boto3.client("dynamodb")
sfn = boto3.client("stepfunctions")

CONTACTS = os.environ["CONTACTS_TABLE"]
STATE_MACHINE_ARN = os.environ["STATE_MACHINE_ARN"]
SUBJECT_ID = os.environ["SUBJECT_ID"]
WAIT_S = int(os.environ.get("WAIT_S", "60"))
MAX_TIER = int(os.environ.get("MAX_TIER", "3"))


# --- routing ------------------------------------------------------------------

def handler(event, context):
    method = event["requestContext"]["http"]["method"]
    path = event.get("rawPath", "/")

    if method == "GET" and path == "/":
        return html(200, trigger_page())
    if method == "POST" and path == "/trigger":
        return trigger()
    return html(404, NOT_FOUND)


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


# --- data ---------------------------------------------------------------------

def first_circle_names():
    """The people paged first. Until ranking exists this is the static tier hint."""
    rows = ddb.query(
        TableName=CONTACTS,
        KeyConditionExpression="subject_id = :s",
        FilterExpression="tier_hint = :one",
        ExpressionAttributeValues={":s": {"S": SUBJECT_ID}, ":one": {"N": "1"}},
    )["Items"]
    return sorted(r["name"]["S"] for r in rows)


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
  --text-2xl: 40px; --text-action: 44px;
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
  var show = function (id) {
    ["idle", "sent", "failed"].forEach(function (s) { document.getElementById(s).hidden = (s !== id); });
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
        document.getElementById("sent-names").textContent = joinNames(data.told || names);
        document.getElementById("sent-time").textContent =
          new Date().toLocaleTimeString("en-IN", { hour: "numeric", minute: "2-digit" });
        show("sent");
      })
      .catch(function () { btn.disabled = false; show("failed"); });
  };
  document.getElementById("press").addEventListener("click", press);
  document.getElementById("retry").addEventListener("click", function () { show("idle"); press(); });
})();
</script>
</body>
</html>
""")

NOT_FOUND = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Not found</title>
<style>""" + STYLE + """</style></head><body>
<h1>That page does not exist</h1>
<p class="foot">Not working? Call <a href="tel:112">112</a></p>
</body></html>"""
