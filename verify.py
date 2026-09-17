#!/usr/bin/env python3
"""Six checks against the live stack. Each asserts on rows and execution history,
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
    ddb.put_item(TableName=TABLES["notifications"], Item={
        "incident_id": {"S": incident_id}, "contact_tier": {"S": f"{contact_id}#V"},
        "contact_id": {"S": contact_id}, "tier": {"N": "0"}, "channel": {"S": "verify"},
        "token_hash": {"S": hashlib.sha256(token.encode()).hexdigest()},
        "delivered": {"BOOL": False}, "created_at": {"N": str(int(time.time()))}})
    return token


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
st, body = http("POST", f"claim/{tok}")
b_status = wait_done(b_arn)
b_inc, b_states = incident(b_id), states(b_arn)
told = json.loads(b_inc.get("broadcast", {}).get("S", "{}")).get("told", [])
check("3 claim during the wait short-circuits to BroadcastClaim",
      st == 200 and "You're going" in body and b_status == "SUCCEEDED" and "BroadcastClaim" in b_states
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
winners = [n for n, b in outcome.items() if "You're going" in b]
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

wait_done(c_arn)
print()
passed = sum(1 for _, ok in results if ok)
print(f"{passed}/{len(results)} passed · executions: {a_arn.rsplit(':', 1)[1]}, {b_id}, {c_id}, {d_id}")
sys.exit(0 if passed == len(results) else 1)
