#!/usr/bin/env python3
"""Sixteen checks against the live stack. Each asserts on rows and execution history,
never on a status code alone - SUCCEEDED with nothing in the tables is a failure.

Every check requires something positive to exist. A check that would pass against
an empty table is not a check.

Run through ./verify.sh, which reads the Terraform outputs.
"""

import hashlib
import json
import os
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

sfn = boto3.client("stepfunctions", region_name=REGION)
ddb = boto3.client("dynamodb", region_name=REGION)
lam = boto3.client("lambda", region_name=REGION)
kms = boto3.client("kms", region_name=REGION)
logs = boto3.client("logs", region_name=REGION)
PREFIX = out["tables"]["value"][0].split("-", 1)[0]
RUN = time.strftime("%H%M%S")
results = []


def check(name, ok, detail):
    results.append((name, ok))
    print(f"{'PASS' if ok else 'FAIL'}  {name}: {detail}")


def start(tag, wait_s, max_tier=2):
    incident_id = f"v-{RUN}-{tag}"
    arn = sfn.start_execution(stateMachineArn=SM, name=incident_id,
                              input=json.dumps({"incident_id": incident_id, "subject_id": "sunita",
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
st, body = http("POST", "cancel", json.dumps({"incident_id": d_id}).encode())
d_status = wait_done(d_arn)
d_inc, d_states = incident(d_id), states(d_arn)
d_bc = json.loads(d_inc.get("broadcast", {}).get("S", "{}"))
check("6 cancel -> Cancelled, everyone reached told it was a false alarm",
      st == 200 and json.loads(body)["status"] == "CANCELLED" and d_status == "SUCCEEDED" and "Cancelled" in d_states
      and d_inc["status"]["S"] == "CANCELLED" and d_bc.get("kind") == "false_alarm"
      and sorted(d_bc.get("told", [])) == reached(d_id) and len(d_bc.get("told", [])) >= 3,
      f"states {d_states[-2:]}, status {d_inc['status']['S']}, broadcast {d_bc.get('kind')} told {d_bc.get('told')}")

# 4. no claim -> NextTier -> everyone -> FinalFallback, all six told with fresh links
a_status = wait_done(a_arn)
a_inc, a_states = incident(a_id), states(a_arn)
a_bc = json.loads(a_inc.get("broadcast", {}).get("S", "{}"))
f_rows = [r for r in rows(a_id) if r["contact_tier"]["S"].endswith("#F")]
check("4 no claim -> NextTier -> FinalFallback, everyone told",
      a_status == "SUCCEEDED" and "NextTier" in a_states and "FinalFallback" in a_states
      and a_inc["status"]["S"] == "FALLBACK" and a_bc.get("kind") == "no_one_reached"
      and len(a_bc.get("told", [])) == 6 and len(f_rows) == 6,
      f"states include NextTier={'NextTier' in a_states} FinalFallback={'FinalFallback' in a_states}; "
      f"status {a_inc['status']['S']}; told {len(a_bc.get('told', []))}; fallback rows {len(f_rows)}")

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
st, body = http("POST", "cancel", json.dumps({"incident_id": b_id}).encode())
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
page_running = http("GET", "")[1]
st3, p3 = http("POST", "cancel", json.dumps({"incident_id": pressed}).encode())
page_idle = http("GET", "")[1]
check("15 a second press during an alert joins it, one incident, her page carries it",
      st1 == 200 and st2 == 200 and pressed and second.get("incident_id") == pressed and second.get("already") is True
      and f'var running = "{pressed}"' in page_running and st3 == 200 and json.loads(p3).get("status") == "CANCELLED"
      and "var running = null" in page_idle,
      f"press -> {pressed}, press again -> {second.get('incident_id')} already={second.get('already')}; "
      f"page while open: {'carries it' if pressed in page_running else 'does not'}; cancelled -> page idle: {'var running = null' in page_idle}")

# 16. where she is: a position her page sends lands on the live incident and on the page a
#     responder opens; on a finished alert it is refused and nothing is written
st1, p1 = http("POST", "location", json.dumps({"incident_id": a_id, "lat": 19.01765, "lon": 72.84268, "accuracy_m": 21}).encode())
a_page = http("GET", f"claim/{plant_token(a_id, 'prakash')}")[1]
st2, _ = http("POST", "location", json.dumps({"incident_id": b_id, "lat": 19.0, "lon": 72.8, "accuracy_m": 5}).encode())
a_loc, b_loc = incident(a_id).get("location", {}).get("M"), incident(b_id).get("location")
check("16 her phone's position reaches the live alert and the responder's page, never a finished one",
      st1 == 200 and a_loc and a_loc["lat"]["N"] == "19.01765" and "Her phone, at" in a_page
      and "maps.google.com/?q=19.01765,72.84268" in a_page and st2 == 409 and b_loc is None,
      f"live alert -> {st1}, row lat {a_loc and a_loc['lat']['N']}, on the page: {'Her phone, at' in a_page}; "
      f"finished alert -> {st2}, written: {b_loc is not None}")

print()
passed = sum(1 for _, ok in results if ok)
print(f"{passed}/{len(results)} passed · executions: {a_arn.rsplit(':', 1)[1]}, {b_id}, {c_id}, {d_id}")
sys.exit(0 if passed == len(results) else 1)
