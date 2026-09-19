#!/usr/bin/env python3
"""Twenty-one checks against the live stack. Each asserts on rows and execution history,
never on a status code alone - SUCCEEDED with nothing in the tables is a failure.

Every check requires something positive to exist. A check that would pass against
an empty table is not a check.

Run through ./verify.sh, which reads the Terraform outputs.
"""

import hashlib
import json
import os
import re
import secrets
import subprocess
import sys
import threading
import time
import urllib.request

import boto3

REGION = os.environ.get("AWS_REGION", "ap-south-1")
out = json.loads(subprocess.check_output(["terraform", "output", "-json"], text=True))
SM = out["state_machine_arn"]["value"]
URL = out["web_url"]["value"]
TABLES = {name.split("-", 1)[1]: name for name in out["tables"]["value"]}
HER_KEY = out["her_key"]["value"]  # what her page carries; a cancel or a position needs it
SENDER = out["sender"]["value"]  # "Pukaar <alert@domain>"; the identity whose default configuration set every page carries

sfn = boto3.client("stepfunctions", region_name=REGION)
ddb = boto3.client("dynamodb", region_name=REGION)
lam = boto3.client("lambda", region_name=REGION)
kms = boto3.client("kms", region_name=REGION)
logs = boto3.client("logs", region_name=REGION)
cw = boto3.client("cloudwatch", region_name=REGION)
ses = boto3.client("sesv2", region_name=REGION)
PREFIX = out["tables"]["value"][0].split("-", 1)[0]
RUN = time.strftime("%H%M%S")
T0 = time.time()
results = []


def check(name, ok, detail):
    results.append((name, ok))
    print(f"{'PASS' if ok else 'FAIL'}  {name}: {detail}")


def start(tag, wait_s, max_tier=2, subject_id="sunita"):
    incident_id = f"v-{RUN}-{tag}"
    arn = sfn.start_execution(stateMachineArn=SM, name=incident_id,
                              input=json.dumps({"incident_id": incident_id, "subject_id": subject_id,
                                                "wait_s": wait_s, "max_tier": max_tier}))["executionArn"]
    return incident_id, arn


def wait_done(arn, timeout=180):
    for _ in range(timeout):
        d = sfn.describe_execution(executionArn=arn)
        if d["status"] != "RUNNING":
            return d["status"]
        time.sleep(1)
    raise TimeoutError(arn)


def states(arn):
    names = []
    token = None
    while True:
        kw = {"nextToken": token} if token else {}
        h = sfn.get_execution_history(executionArn=arn, maxResults=500, **kw)
        names += [e["stateEnteredEventDetails"]["name"] for e in h["events"] if e["type"].endswith("StateEntered")]
        token = h.get("nextToken")
        if not token:
            return names


def incident(incident_id):
    return ddb.get_item(TableName=TABLES["incidents"], Key={"incident_id": {"S": incident_id}}, ConsistentRead=True).get("Item")


def rows(incident_id):
    return ddb.query(TableName=TABLES["notifications"], KeyConditionExpression="incident_id = :i",
                     ExpressionAttributeValues={":i": {"S": incident_id}}, ConsistentRead=True)["Items"]


def plant_token(incident_id, contact_id):
    """A row exactly like the one a delivered email leaves behind, with a token we know."""
    token = secrets.token_urlsafe(24)
    now = str(int(time.time()))
    ddb.put_item(TableName=TABLES["notifications"], Item={
        "incident_id": {"S": incident_id}, "contact_tier": {"S": f"{contact_id}#V"},
        "contact_id": {"S": contact_id}, "tier": {"N": "0"}, "channel": {"S": "verify"},
        "token_hash": {"S": hashlib.sha256(token.encode()).hexdigest()},
        "delivered": {"BOOL": False}, "created_at": {"N": now}, "sent_at": {"N": now},
        "bucket": {"S": "verify"}})
    return token


def stats():
    """Per contact, counters summed over every hour bucket."""
    out = {}
    for r in ddb.scan(TableName=TABLES["response-stats"], ConsistentRead=True)["Items"]:
        p, a, l = (int(r.get(k, {}).get("N", 0)) for k in ("pages_sent", "responses", "total_latency_ms"))
        cid = r["contact_id"]["S"]
        out[cid] = tuple(x + y for x, y in zip(out.get(cid, (0, 0, 0)), (p, a, l)))
    return out


def select_tier_outputs(arn):
    outs = []
    token = None
    while True:
        kw = {"nextToken": token} if token else {}
        h = sfn.get_execution_history(executionArn=arn, maxResults=500, **kw)
        outs += [json.loads(e["stateExitedEventDetails"]["output"]) for e in h["events"]
                 if e["type"].endswith("StateExited") and e["stateExitedEventDetails"]["name"] == "SelectTier"]
        token = h.get("nextToken")
        if not token:
            return outs


def http(method, path, body=None):
    req = urllib.request.Request(URL + path, method=method, data=body,
                                 headers={"content-type": "application/json"} if body else {})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def reached(incident_id):
    return sorted({r["contact_id"]["S"] for r in rows(incident_id)
                   if r.get("delivered", {}).get("BOOL") and not r["contact_tier"]["S"].endswith("#V")})


def wait_for_rows(incident_id, n, timeout=30):
    for _ in range(timeout):
        r = [x for x in rows(incident_id) if x.get("delivered", {}).get("BOOL")]
        if len(r) >= n:
            return r
        time.sleep(1)
    return [x for x in rows(incident_id) if x.get("delivered", {}).get("BOOL")]


# ---------------------------------------------------------------------------
print(f"stack {PREFIX} · {URL}")

stats_before = stats()

# A: fan-out, then nobody claims -> widen -> final fallback. B, C, D run alongside.
a_id, a_arn = start("fanout", wait_s=5)
b_id, b_arn = start("claim", wait_s=25)
c_id, c_arn = start("race", wait_s=25)
d_id, d_arn = start("cancel", wait_s=25)
f_id, f_arn = start("fail", wait_s=5, subject_id="nobody")  # a subject with nobody to page: the spine must fail loudly

# 1. three notification rows, delivered, for tier 1
tier1 = [r for r in wait_for_rows(a_id, 3) if r["tier"]["N"] == "1"]
check("1 fan-out writes three delivered rows", len(tier1) == 3 and all("message_id" in r for r in tier1),
      f"{len(tier1)} tier-1 rows delivered, message ids {[r.get('message_id', {}).get('S', '')[:8] for r in tier1]}")

# 2. dedupe: invoking notify again for a delivered row sends nothing
def ravi_tier1():
    return {r["contact_tier"]["S"]: r.get("message_id", {}).get("S") for r in rows(a_id) if r["tier"]["N"] == "1"}
before = ravi_tier1()
resp = lam.invoke(FunctionName=f"{PREFIX}-notify", Payload=json.dumps({
    "incident_id": a_id, "tier": 1, "contact": {"contact_id": "ravi", "name": "Ravi", "email": "x@invalid"},
    "subject": {"name": "Sunita", "address": "a, b, c"}, "started_at": int(time.time()),
    "pressed_at": "now", "contacted_count": 3}).encode())
dedup = json.loads(resp["Payload"].read())
after = ravi_tier1()
check("2 dedupe under re-invocation", dedup.get("deduped") is True and before == after and before.get("ravi#1"),
      f"response {dedup}; ravi#1 message id unchanged: {before.get('ravi#1', '')[:12]}")

# 3. claim mid-wait -> BroadcastClaim, everyone reached is told
wait_for_rows(b_id, 3)
tok = plant_token(b_id, "ravi")
b_actionable = http("GET", f"claim/{tok}")[1]
st, body = http("POST", f"claim/{tok}")
b_status = wait_done(b_arn)
b_inc, b_states = incident(b_id), states(b_arn)
told = json.loads(b_inc.get("broadcast", {}).get("S", "{}")).get("told", [])
check("3 claim during the wait short-circuits to BroadcastClaim",
      st == 200 and "You’re going" in body and b_status == "SUCCEEDED" and "BroadcastClaim" in b_states
      and "NextTier" not in b_states and b_inc["status"]["S"] == "CLAIMED" and b_inc["claimed_by"]["S"] == "ravi"
      and sorted(told) == reached(b_id) and len(told) >= 3,
      f"states {b_states[-3:]}, status {b_inc['status']['S']} by {b_inc.get('claimed_by', {}).get('S')}, told {sorted(told)}")

# 5. the race: two claims at once, exactly one winner, loser told by name
wait_for_rows(c_id, 3)
t_anil, t_vaish = plant_token(c_id, "anil"), plant_token(c_id, "vaishali")
outcome = {}
def go(name, t):
    outcome[name] = http("POST", f"claim/{t}")[1]
th = [threading.Thread(target=go, args=("anil", t_anil)), threading.Thread(target=go, args=("vaishali", t_vaish))]
[t.start() for t in th]; [t.join() for t in th]
winners = [n for n, b in outcome.items() if "You’re going" in b]
losers = [n for n, b in outcome.items() if "is already on the way" in b]
c_inc = incident(c_id)
loser_body = outcome[losers[0]] if losers else ""
winner_name = {"anil": "Anil", "vaishali": "Vaishali"}.get(winners[0] if winners else "", "")
check("5 concurrent claims: exactly one winner, loser told who by name",
      len(winners) == 1 and len(losers) == 1 and c_inc["claimed_by"]["S"] == winners[0]
      and f"{winner_name} is already on the way" in loser_body,
      f"winner {winners}, loser {losers}, row claimed_by {c_inc.get('claimed_by', {}).get('S')}")

# 6. cancel -> Cancelled state, false alarm to everyone reached
wait_for_rows(d_id, 3)
st0, _ = http("POST", "cancel", json.dumps({"incident_id": d_id, "key": "not-her-page"}).encode())  # a responder who knows the id
d_still = incident(d_id)["status"]["S"]
st, body = http("POST", "cancel", json.dumps({"incident_id": d_id, "key": HER_KEY}).encode())
d_status = wait_done(d_arn)
d_inc, d_states = incident(d_id), states(d_arn)
d_bc = json.loads(d_inc.get("broadcast", {}).get("S", "{}"))
check("6 cancel -> Cancelled, everyone reached told it was a false alarm; only her page can",
      st0 == 403 and d_still == "OPEN"
      and st == 200 and json.loads(body)["status"] == "CANCELLED" and d_status == "SUCCEEDED" and "Cancelled" in d_states
      and d_inc["status"]["S"] == "CANCELLED" and d_bc.get("kind") == "false_alarm"
      and sorted(d_bc.get("told", [])) == reached(d_id) and len(d_bc.get("told", [])) >= 3,
      f"without her key -> {st0}, still {d_still}; with it: states {d_states[-2:]}, status {d_inc['status']['S']}, "
      f"broadcast {d_bc.get('kind')} told {d_bc.get('told')}")

# 4. no claim -> NextTier -> everyone -> FinalFallback, all six told with fresh links
a_status = wait_done(a_arn)
a_inc, a_states = incident(a_id), states(a_arn)
a_bc = json.loads(a_inc.get("broadcast", {}).get("S", "{}"))
f_rows = [r for r in rows(a_id) if r["contact_tier"]["S"].endswith("#F")]
a_st = json.loads(http("GET", f"status/{a_id}")[1])  # what her screen polls: everyone told, nobody answered
check("4 no claim -> NextTier -> FinalFallback, everyone told, her screen knows",
      a_status == "SUCCEEDED" and "NextTier" in a_states and "FinalFallback" in a_states
      and a_inc["status"]["S"] == "FALLBACK" and a_bc.get("kind") == "no_one_reached"
      and len(a_bc.get("told", [])) == 6 and len(f_rows) == 6
      and a_st.get("status") == "FALLBACK" and len(a_st.get("told", [])) == 6,
      f"states include NextTier={'NextTier' in a_states} FinalFallback={'FinalFallback' in a_states}; "
      f"status {a_inc['status']['S']}; told {len(a_bc.get('told', []))}; fallback rows {len(f_rows)}; "
      f"/status {a_st.get('status')}, told {len(a_st.get('told', []))}")

# 7. ranking decides membership: the nearest contact with the worst history is not in
#    tier 1, though distance alone would put them there; they are paged at tier 2
contacts = ddb.query(TableName=TABLES["contacts"], KeyConditionExpression="subject_id = :s",
                     ExpressionAttributeValues={":s": {"S": "sunita"}})["Items"]
by_distance = [c["contact_id"]["S"] for c in sorted(contacts, key=lambda c: int(c["proximity_m"]["N"]))]
nearest = by_distance[0]
picks = select_tier_outputs(a_arn)
tier1 = [c["contact_id"] for c in picks[0]["contacts"]] if picks else []
basis = {r["contact_id"]: r["basis"] for r in picks[0].get("ranking", [])} if picks else {}
paged_at = {r["contact_id"]["S"]: r["tier"]["N"] for r in rows(a_id) if r.get("delivered", {}).get("BOOL")
            and not r["contact_tier"]["S"].endswith(("#V", "#F"))}
best = max(stats_before, key=lambda c: stats_before[c][1] / stats_before[c][0])  # answered / paged, seeded
check("7 ranking changes who is paged first",
      len(tier1) == 3 and nearest in by_distance[:3] and nearest not in tier1
      and basis.get(nearest, "").startswith("0/") and paged_at.get(nearest) == "2"
      and best in tier1 and all(basis.get(c) for c in by_distance),
      f"tier 1 {tier1} vs nearest three {by_distance[:3]}; {nearest} ({basis.get(nearest)}) paged at tier "
      f"{paged_at.get(nearest)}; best history {best} ({basis.get(best)}) in tier 1")

# 8. counters: every page adds pages_sent, every answer adds responses and its latency
wait_done(c_arn)
stats_after = stats()
delta = {cid: tuple(a - b for a, b in zip(stats_after.get(cid, (0, 0, 0)), stats_before.get(cid, (0, 0, 0))))
         for cid in {*stats_after, *stats_before}}
paged = {r["contact_id"]["S"] for i in (a_id, b_id, c_id, d_id) for r in rows(i)
         if r.get("delivered", {}).get("BOOL") and not r["contact_tier"]["S"].endswith(("#V", "#F"))}
answered = [r for i in (b_id, c_id) for r in rows(i) if r["contact_tier"]["S"].endswith("#V") and "responded_at" in r]
latency_expected = sum((int(r["responded_at"]["N"]) - int(r["sent_at"]["N"])) * 1000 for r in answered)
check("8 pages and answers are counted",
      len(paged) >= 3 and all(delta[c][0] >= 1 for c in paged)
      and len(answered) == 3 and all(delta[r["contact_id"]["S"]][1] == 1 for r in answered)
      and sum(delta[c][2] for c in {r["contact_id"]["S"] for r in answered}) == latency_expected,
      f"pages counted for {len(paged)} contacts; answers {[r['contact_id']['S'] for r in answered]} +1 each; "
      f"latency {latency_expected} ms")

# 9. the sealed record: ciphertext at rest, opened only with her id as context, shown
#    only on the winner's page - never the loser's, never before the claim
sealed = ddb.get_item(TableName=TABLES["subjects"], Key={"subject_id": {"S": "sunita"}})["Item"]["record"]["B"]
plain = kms.decrypt(CiphertextBlob=sealed, EncryptionContext={"subject_id": "sunita"})["Plaintext"].decode()
try:
    kms.decrypt(CiphertextBlob=sealed, EncryptionContext={"subject_id": "someone-else"})
    swap = "decrypted"
except kms.exceptions.InvalidCiphertextException:
    swap = "InvalidCiphertextException"
first_line = plain.splitlines()[0]
loser_tok = {"anil": t_anil, "vaishali": t_vaish}[losers[0]] if losers else ""
loser_get = http("GET", f"claim/{loser_tok}")[1] if loser_tok else ""
check("9 sealed record opens for the one who is going, and only them",
      bool(first_line) and first_line.encode() not in sealed and swap == "InvalidCiphertextException"
      and first_line in (outcome[winners[0]] if winners else "") and first_line not in loser_body
      and first_line not in loser_get and first_line not in b_actionable,
      f"at rest {len(sealed)} bytes of ciphertext; wrong context -> {swap}; on winner's page: "
      f"{first_line in outcome[winners[0]] if winners else False}; on loser's page/link: "
      f"{first_line in loser_body or first_line in loser_get}; before the claim: {first_line in b_actionable}")

# 10. every opening is logged with who and when
released = []
for _ in range(30):
    released = [json.loads(e["message"].split("\t")[-1]) for e in logs.filter_log_events(
        logGroupName=f"/aws/lambda/{PREFIX}-web", startTime=(int(time.time()) - 600) * 1000,
        filterPattern=f'{{ $.event = "record_released" && $.incident_id = "{c_id}" }}')["events"]]
    if released:
        break
    time.sleep(1)
check("10 the release is logged with who and when",
      len(released) >= 1 and all(r["contact_id"] == winners[0] and r["subject_id"] == "sunita" and r["at"] for r in released),
      f"{len(released)} line(s): {[(r['contact_id'], r['at']) for r in released]}")

# 11. she may be fine after all: a cancel after someone claimed still tells everyone,
#     though the machine finished with the claim
st, body = http("POST", "cancel", json.dumps({"incident_id": b_id, "key": HER_KEY}).encode())
b_inc = incident(b_id)
b_bc = json.loads(b_inc.get("broadcast", {}).get("S", "{}"))
b_page = http("GET", f"claim/{tok}")[1]
check("11 cancel after a claim: everyone reached told, the record closed",
      st == 200 and json.loads(body)["status"] == "CANCELLED" and b_inc["status"]["S"] == "CANCELLED"
      and b_bc.get("kind") == "false_alarm" and sorted(b_bc.get("told", [])) == reached(b_id)
      and len(b_bc.get("told", [])) >= 3 and "cancelled this alert" in b_page and first_line not in b_page,
      f"status {b_inc['status']['S']}, broadcast {b_bc.get('kind')} told {b_bc.get('told')}; claimer's link now: "
      f"{'cancelled' if 'cancelled this alert' in b_page else 'not cancelled'}, record shown: {first_line in b_page}")

# 12. the second channel: every page row's channel matches its contact - "email+telegram" with a
#     Telegram message id for a contact whose chat id is configured, "email" alone for the rest
chat_ids = out.get("telegram_chat_ids", {}).get("value", {}) or {}
page_rows = [r for r in rows(a_id) if r["tier"]["N"] not in ("0", "99")]
def channel_ok(r):
    want = "email+telegram" if r["contact_id"]["S"] in chat_ids else "email"
    return r.get("channel", {}).get("S") == want and (want == "email") != ("telegram_message_id" in r)
check("12 the second channel: every page row's channel matches its contact",
      len(page_rows) >= 6 and all(channel_ok(r) for r in page_rows),
      f"{len(page_rows)} page rows; on Telegram: {sorted(r['contact_id']['S'] for r in page_rows if 'telegram_message_id' in r)}; "
      f"configured: {sorted(chat_ids)}")

# 13. the weekly check-in is a page: it writes a delivered row and counts a page for that hour;
#     answered within ten minutes it counts one response, a second tap counts nothing more, a late
#     tap counts nothing, and a check-in link can never claim an incident
def plant_checkin(contact_id, sent_at):
    token = secrets.token_urlsafe(24)
    ddb.put_item(TableName=TABLES["notifications"], Item={
        "incident_id": {"S": f"checkin-v-{RUN}"}, "contact_tier": {"S": f"{contact_id}#C"},
        "contact_id": {"S": contact_id}, "tier": {"N": "0"}, "kind": {"S": "checkin"},
        "token_hash": {"S": hashlib.sha256(token.encode()).hexdigest()}, "bucket": {"S": "verify"},
        "delivered": {"BOOL": True}, "created_at": {"N": str(sent_at)}, "sent_at": {"N": str(sent_at)}})
    return token
before = stats()
ping = json.loads(lam.invoke(FunctionName=f"{PREFIX}-checkin",
                             Payload=json.dumps({"contacts": ["vaishali"]}).encode())["Payload"].read())
ping_rows = [r for r in rows(ping.get("checkin_id", "-")) if r["contact_id"]["S"] == "vaishali"]
now_s = int(time.time())
tok_ok, tok_late = plant_checkin("anil", now_s), plant_checkin("sunil", now_s - 700)
st1, b1 = http("POST", f"checkin/{tok_ok}")
st2, b2 = http("POST", f"checkin/{tok_ok}")
st3, b3 = http("POST", f"checkin/{tok_late}")
st4, _ = http("GET", f"claim/{tok_ok}")
after = stats()
delta = {c: tuple(a - b for a, b in zip(after.get(c, (0, 0, 0)), before.get(c, (0, 0, 0)))) for c in ("vaishali", "anil", "sunil")}
check("13 the weekly check-in is a page, answered in time counts once, late counts nothing",
      ping.get("sent") == ["vaishali"] and len(ping_rows) == 1 and ping_rows[0].get("delivered", {}).get("BOOL")
      and ping_rows[0].get("kind", {}).get("S") == "checkin" and delta["vaishali"][0] == 1 and delta["vaishali"][1] == 0
      and st1 == 200 and "Thank you" in b1 and st2 == 200 and "Already counted" in b2 and delta["anil"][1] == 1
      and st3 == 200 and "was at" in b3 and delta["sunil"][1] == 0 and st4 == 404,
      f"{ping.get('checkin_id')}: vaishali row delivered, pages +{delta['vaishali'][0]}; anil answered -> {st1} "
      f"{'thanks' if 'Thank you' in b1 else b1[:40]}, again -> {'already counted' if 'Already counted' in b2 else b2[:40]}, "
      f"responses +{delta['anil'][1]}; sunil late -> {'closed' if 'was at' in b3 else b3[:40]}, responses +{delta['sunil'][1]}; "
      f"check-in link on /claim -> {st4}")

# 14. the timeline page tells the claim incident's story from its rows, in order: the press,
#     the first circle at one timestamp, who went, who opened her notes, the cancel, the false alarm
st, page = http("GET", f"incident/{b_id}")
marks = ["pressed her help button", "were told at once", "is going", "opened her medical notes", "cancelled — she is OK",
         "false alarm"]
positions = [page.find(m) for m in marks]
check("14 the timeline page lists what happened, in order, from the rows",
      st == 200 and all(p >= 0 for p in positions) and positions == sorted(positions)
      and page.count("<li>") >= 6 and f"{PREFIX}" not in page,
      f"{st}; {page.count('<li>')} lines; order " + ("kept" if positions == sorted(positions) else f"broken {positions}")
      + f"; missing {[m for m, p in zip(marks, positions) if p < 0]}")

# 15. a second press during an alert joins it: two presses on the button, one incident, and her
#     page carries that incident until she cancels
st1, p1 = http("POST", "trigger")
st2, p2 = http("POST", "trigger")
first, second = json.loads(p1), json.loads(p2)
pressed = first.get("incident_id", "")
wait_for_rows(pressed, 3)
page_running = http("GET", "")[1]
# reopened mid-alert, her page names the people this alert reached - exactly them. The ranking has
# already learned from these pages, so "who a press now would page" is a different list.
shown = sorted(json.loads(re.search(r"var names = (\[.*?\]);", page_running).group(1)))
paged = sorted(c.capitalize() for c in reached(pressed))
st3, p3 = http("POST", "cancel", json.dumps({"incident_id": pressed, "key": HER_KEY}).encode())
page_idle = http("GET", "")[1]
check("15 a second press during an alert joins it, one incident, her page carries it and names exactly who it reached",
      st1 == 200 and st2 == 200 and pressed and second.get("incident_id") == pressed and second.get("already") is True
      and f'var running = "{pressed}"' in page_running and shown == paged and len(paged) == 3
      and st3 == 200 and json.loads(p3).get("status") == "CANCELLED" and "var running = null" in page_idle,
      f"press -> {pressed}, press again -> {second.get('incident_id')} already={second.get('already')}; "
      f"page while open: {'carries it' if pressed in page_running else 'does not'}, names {shown} vs reached {paged}; "
      f"cancelled -> page idle: {'var running = null' in page_idle}")

# 16. where she is: a position her page sends lands on the live incident and on the page a
#     responder opens; on a finished alert it is refused and nothing is written
st0, _ = http("POST", "location", json.dumps({"incident_id": a_id, "key": "not-her-page", "lat": 18.0, "lon": 73.0, "accuracy_m": 5}).encode())
a_before = incident(a_id).get("location")
st1, p1 = http("POST", "location", json.dumps({"incident_id": a_id, "key": HER_KEY, "lat": 19.01765, "lon": 72.84268, "accuracy_m": 21}).encode())
a_page = http("GET", f"claim/{plant_token(a_id, 'prakash')}")[1]
st2, _ = http("POST", "location", json.dumps({"incident_id": b_id, "key": HER_KEY, "lat": 19.0, "lon": 72.8, "accuracy_m": 5}).encode())
a_loc, b_loc = incident(a_id).get("location", {}).get("M"), incident(b_id).get("location")
check("16 her phone's position reaches the live alert and the responder's page, never a finished one, only from her page",
      st0 == 403 and a_before is None
      and st1 == 200 and a_loc and a_loc["lat"]["N"] == "19.01765" and "Her phone, at" in a_page
      and "maps.google.com/?q=19.01765,72.84268" in a_page and st2 == 409 and b_loc is None,
      f"without her key -> {st0}, written: {a_before is not None}; live alert -> {st1}, row lat {a_loc and a_loc['lat']['N']}, "
      f"on the page: {'Her phone, at' in a_page}; finished alert -> {st2}, written: {b_loc is not None}")

# 17. "I can't go after all": the one who claimed steps back. The alert reopens with the step-back
#     on its row, everyone else contacted is told with a fresh link, her page hears who stepped back
#     and who is told now, and the machine resumes at the next circle - not from the start - with the
#     timers the alert began with; a stranger's link cannot do it
w_tok = {"anil": t_anil, "vaishali": t_vaish}[winners[0]] if winners else ""
st0, _ = http("POST", f"release/{plant_token(c_id, 'sunil')}")
c_before = incident(c_id)
circle = sorted(r["contact_id"]["S"] for r in rows(c_id) if r["tier"]["N"] == "1" and r.get("delivered", {}).get("BOOL"))
st1, p1 = http("POST", f"release/{w_tok}")
c_inc = incident(c_id)
rel = [r["M"] for r in c_inc.get("releases", {}).get("L", [])]
r_rows = sorted(r["contact_id"]["S"] for r in rows(c_id) if r["contact_tier"]["S"].endswith("#R") and r.get("delivered", {}).get("BOOL"))
c_status = json.loads(http("GET", f"status/{c_id}")[1])
r_arn = SM.replace(":stateMachine:", ":execution:") + f":{c_id}-r1"
r_status = wait_done(r_arn)
r_states, r_picks = states(r_arn), select_tier_outputs(r_arn)
c_after = incident(c_id)
check("17 the one who claimed steps back: reopened, everyone told, resumed at the next circle",
      st0 == 200 and c_before["status"]["S"] == "CLAIMED" and st1 == 200 and "stepped back" in p1
      and c_inc["status"]["S"] == "OPEN" and "claimed_by" not in c_inc and len(rel) == 1 and rel[0]["contact_id"]["S"] == winners[0]
      and r_rows == [c for c in circle if c != winners[0]] and len(r_rows) >= 2
      and c_status.get("stepped_back") == winner_name and winner_name not in c_status.get("told", []) and len(c_status.get("told", [])) >= 2
      and r_status == "SUCCEEDED" and r_states[:3] == ["Start", "ResumeIncident", "SelectTier"] and "CreateIncident" not in r_states
      and r_picks and r_picks[0]["tier_index"] == 2 and r_picks[0]["wait_s"] == 25 and "FinalFallback" in r_states
      and c_after["status"]["S"] == "FALLBACK",
      f"stranger's link -> {st0}, still claimed: {c_before['status']['S'] == 'CLAIMED'}; {winners[0]} steps back -> {st1}, "
      f"row {c_inc['status']['S']}, releases {[r['contact_id']['S'] for r in rel]}; told with fresh links: {r_rows}; "
      f"her page: stepped_back={c_status.get('stepped_back')}, told {c_status.get('told')}; resumed run {r_status}: "
      f"{r_states[:3]} … tier {r_picks[0]['tier_index'] if r_picks else '-'} wait_s {r_picks[0]['wait_s'] if r_picks else '-'}, "
      f"ends {r_states[-2:]}; row now {c_after['status']['S']}")

# 18. the way off her list, at the foot of every message that carries a link: GET asks and writes
#     nothing; POST marks the contact gone, and from then on her screen stops naming them, the check-in
#     skips them, and a fresh alert - first circle and the fallback to everyone - never reaches them.
#     verify.sh's reseed puts them back.
def contact(cid):
    return ddb.get_item(TableName=TABLES["contacts"], Key={"subject_id": {"S": "sunita"}, "contact_id": {"S": cid}},
                        ConsistentRead=True)["Item"]
tok = plant_token(a_id, "anil")
st0, p0 = http("GET", f"leave/{tok}")
anil_before = contact("anil")
st1, p1 = http("POST", f"leave/{tok}")
anil_after = contact("anil")
idle = http("GET", "")[1]
ping = json.loads(lam.invoke(FunctionName=f"{PREFIX}-checkin",
                             Payload=json.dumps({"contacts": ["anil"]}).encode())["Payload"].read())
e_id, e_arn = start("left", wait_s=5)
e_status = wait_done(e_arn)
e_bc = json.loads(incident(e_id).get("broadcast", {}).get("S", "{}"))
e_paged = sorted({r["contact_id"]["S"] for r in rows(e_id)})
check("18 leaving her list: never paged, never asked, no longer named",
      st0 == 200 and "Leave" in p0 and "left_at" not in anil_before and st1 == 200 and "left" in p1
      and "left_at" in anil_after and '"Anil"' not in idle and '"Vaishali"' in idle and ping.get("sent") == []
      and e_status == "SUCCEEDED" and e_bc.get("kind") == "no_one_reached" and len(e_bc.get("told", [])) == 5
      and "anil" not in e_paged and len(e_paged) == 5,
      f"GET -> {st0}, wrote nothing: {'left_at' not in anil_before}; POST -> {st1}, left_at: {'left_at' in anil_after}; "
      f"her screen names Anil: {'Anil' in idle[idle.find('var names'):idle.find('var names') + 80]}; check-in sent {ping.get('sent')}; "
      f"fresh alert {e_status}: paged {e_paged}, fallback told {len(e_bc.get('told', []))}")

# 19. a broken escalation fails loudly: the row says FAILED and why, the metric counts it,
# and the operator's mail went out - EventBridge saw the execution end, SNS published.
def metric_sum(namespace, name, dimensions):
    pts = cw.get_metric_statistics(Namespace=namespace, MetricName=name, Dimensions=dimensions, StartTime=T0 - 120,
                                   EndTime=time.time() + 60, Period=60, Statistics=["Sum"])["Datapoints"]
    return sum(p["Sum"] for p in pts)


def wait_metrics(timeout=150):
    for _ in range(timeout // 10):
        counted = metric_sum("Pukaar", "EscalationFailed", [])
        told = metric_sum("AWS/Events", "Invocations", [{"Name": "RuleName", "Value": f"{PREFIX}-escalation-failed"}])
        mailed = metric_sum("AWS/SNS", "NumberOfMessagesPublished", [{"Name": "TopicName", "Value": f"{PREFIX}-failures"}])
        if counted and told and mailed:
            break
        time.sleep(10)
    return counted, told, mailed


f_status = wait_done(f_arn)
f_states, f_inc = states(f_arn), incident(f_id)
f_why = f_inc.get("failure", {}).get("S", "")
counted, told, mailed = wait_metrics()
check("19 a broken escalation fails loudly and the operator is told",
      f_status == "FAILED" and f_states[-2:] == ["RecordFailure", "Failed"] and f_inc["status"]["S"] == "FAILED"
      and "nobody to page" in f_why and "failed_at" in f_inc and counted >= 1 and told >= 1 and mailed >= 1,
      f"execution {f_status}, states {f_states}; row {f_inc['status']['S']}, why: {f_why[f_why.find('errorMessage'):][:60]}; "
      f"EscalationFailed metric {counted:.0f}, rule invoked {told:.0f}, SNS published {mailed:.0f}")

# 20. the rails around the record, and the two failures the machine cannot see. Every table
# restores to any second of the last 35 days and refuses deletion; no execution outlives an
# hour; her button's own Lambda erroring, and a page that bounces, reach the operator's topic.
# The last two are done, not read: one page to SES's bounce simulator, sent without naming a
# configuration set (the identity's default carries it, as it does for every real page), and
# the alarm forced into ALARM once. Both must show up as messages published on the topic.
topic = f"{PREFIX}-failures"
backups = {t: ddb.describe_continuous_backups(TableName=t)["ContinuousBackupsDescription"]
           ["PointInTimeRecoveryDescription"]["PointInTimeRecoveryStatus"] for t in TABLES.values()}
guarded = {t: ddb.describe_table(TableName=t)["Table"].get("DeletionProtectionEnabled", False) for t in TABLES.values()}
cap = json.loads(sfn.describe_state_machine(stateMachineArn=SM)["definition"]).get("TimeoutSeconds")
alarm = cw.describe_alarms(AlarmNames=[f"{PREFIX}-web-errors"])["MetricAlarms"]
alarm_topic = bool(alarm) and any(a.endswith(f":{topic}") for a in alarm[0]["AlarmActions"])
domain = SENDER.rsplit("@", 1)[1].rstrip(">")
default_set = ses.get_email_identity(EmailIdentity=domain).get("ConfigurationSetName")
bounce_dest = [d for d in ses.get_configuration_set_event_destinations(ConfigurationSetName=PREFIX)["EventDestinations"]
               if d["Enabled"] and "BOUNCE" in d["MatchingEventTypes"] and d.get("SnsDestination", {}).get("TopicArn", "").endswith(f":{topic}")]
ses.send_email(FromEmailAddress=SENDER, Destination={"ToAddresses": ["bounce@simulator.amazonses.com"]},
               Content={"Simple": {"Subject": {"Data": f"Pukaar check 20 {RUN}"},
                                   "Body": {"Text": {"Data": "A page to an address that bounces. The operator should hear."}}}})
cw.set_alarm_state(AlarmName=f"{PREFIX}-web-errors", StateValue="ALARM", StateReason=f"verify.py {RUN}: the alarm's mail must reach the operator")
for _ in range(15):
    bounced = metric_sum("AWS/SES", "Bounce", [{"Name": "ses:configuration-set", "Value": PREFIX}])
    published = metric_sum("AWS/SNS", "NumberOfMessagesPublished", [{"Name": "TopicName", "Value": topic}])
    if bounced >= 1 and published >= mailed + 2:
        break
    time.sleep(10)
check("20 the rails hold: backups, no deletion, an hour's cap, a bounce and a failing button reach the operator",
      all(v == "ENABLED" for v in backups.values()) and all(guarded.values()) and cap == 3600 and alarm_topic
      and default_set == PREFIX and len(bounce_dest) == 1 and bounced >= 1 and published >= mailed + 2,
      f"PITR {sum(v == 'ENABLED' for v in backups.values())}/5, deletion protection {sum(guarded.values())}/5, "
      f"machine TimeoutSeconds {cap}; alarm -> topic {alarm_topic}; {domain} default set {default_set!r}, bounce -> topic {len(bounce_dest)}; "
      f"SES Bounce {bounced:.0f}, topic published {published:.0f} (was {mailed:.0f})")

# 21. what a second reader found (19 Sep 21:50, a review by Cursor): every claim and cancel above
# happened mid-wait. After the last circle the machine has passed its last wait, so a late claim
# or a cancel from there has to be broadcast by the web function itself; two presses in the same
# instant have to be one alert (the subject row is taken with a condition before any machine
# starts); and a bad body on /cancel is a 400, never an error that pages the operator.
late = plant_token(e_id, "ravi")                     # e_id ended in FALLBACK in check 18, five reached
st_l, _ = http("POST", f"claim/{late}")
l_inc = incident(e_id); l_bc = json.loads(l_inc.get("broadcast", {}).get("S", "{}"))
st_c, p_c = http("POST", "cancel", json.dumps({"incident_id": a_id, "key": HER_KEY}).encode())  # a_id: FALLBACK since check 4
c_inc = incident(a_id); c_bc = json.loads(c_inc.get("broadcast", {}).get("S", "{}"))
still_listed = sorted(c for c in reached(a_id) if "left_at" not in contact(c))  # anil left in check 18
pair = [None, None]
def press(i):
    pair[i] = json.loads(http("POST", "trigger")[1])
threads = [threading.Thread(target=press, args=(i,)) for i in range(2)]
[t.start() for t in threads]; [t.join() for t in threads]
r_ids = {p["incident_id"] for p in pair}
r_id = pair[0]["incident_id"]
wait_for_rows(r_id, 3)
http("POST", "cancel", json.dumps({"incident_id": r_id, "key": HER_KEY}).encode())
r_status = wait_done(SM.replace(":stateMachine:", ":execution:") + ":" + r_id)
st_bad, _ = http("POST", "cancel", b"not-json")
st_key, _ = http("POST", "cancel", json.dumps({"incident_id": a_id, "key": "\u00e9"}).encode())
check("21 after the last circle a claim or a cancel still tells everyone; two presses at once are one alert; a bad body is a 400",
      st_l == 200 and l_inc["status"]["S"] == "CLAIMED" and l_inc["claimed_by"]["S"] == "ravi"
      and l_bc.get("kind") == "someone_going" and sorted(l_bc.get("told", [])) == reached(e_id)
      and st_c == 200 and c_inc["status"]["S"] == "CANCELLED" and c_bc.get("kind") == "false_alarm"
      and sorted(c_bc.get("told", [])) == still_listed
      and len(r_ids) == 1 and sum(1 for p in pair if not p.get("already")) == 1 and r_status == "SUCCEEDED"
      and st_bad == 400 and st_key == 403,
      f"late claim on FALLBACK {e_id}: {l_inc['status']['S']} by {l_inc.get('claimed_by', {}).get('S')}, broadcast {l_bc.get('kind')} told {sorted(l_bc.get('told', []))} vs reached {reached(e_id)}; "
      f"cancel from FALLBACK {a_id}: {c_inc['status']['S']}, broadcast {c_bc.get('kind')} told {len(c_bc.get('told', []))} vs still listed {len(still_listed)}; "
      f"two presses at once -> {len(r_ids)} alert ({sum(1 for p in pair if p.get('already'))} joined), {r_status} after cancel; "
      f"bad body -> {st_bad}, non-ascii key -> {st_key}")

print()
passed = sum(1 for _, ok in results if ok)
print(f"{passed}/{len(results)} passed · executions: {a_arn.rsplit(':', 1)[1]}, {b_id}, {c_id}, {d_id}, {f_id}")
sys.exit(0 if passed == len(results) else 1)
