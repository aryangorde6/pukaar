"""After the wait: has anyone said they are going? Reads the incident row and says so.

Returns the input plus `claim` in {"claimed", "cancelled", "none"} and `claimed_by`.
A missing row is an error, not "none" — no evidence is not the same as no claim.
"""

import json
import os

import boto3

ddb = boto3.client("dynamodb")
INCIDENTS = os.environ["INCIDENTS_TABLE"]

CLAIM_BY_STATUS = {
    "OPEN": "none",
    "CLAIMED": "claimed",
    "CANCELLED": "cancelled",
    "FALLBACK": "none",
}


def handler(event, context):
    incident_id = event["incident_id"]
    item = ddb.get_item(
        TableName=INCIDENTS,
        Key={"incident_id": {"S": incident_id}},
        ConsistentRead=True,
    ).get("Item")
    if item is None:
        raise RuntimeError(f"incident {incident_id} has no row")

    status = item["status"]["S"]
    claim = CLAIM_BY_STATUS[status]
    claimed_by = item.get("claimed_by", {}).get("S")

    print(json.dumps({"component": "check_claim", "incident_id": incident_id, "status": status, "claim": claim}))
    return {**event, "claim": claim, "claimed_by": claimed_by}
