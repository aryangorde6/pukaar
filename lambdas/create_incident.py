"""First state of the escalation: open the incident row.

Input:  {"subject_id": "...", "wait_s": 60, "max_tier": 3, "execution_arn": "..."}
Output: the same, plus incident_id and tier_index 0, for the states that follow.
"""

import json
import os
import time
import uuid

import boto3

ddb = boto3.client("dynamodb")
INCIDENTS = os.environ["INCIDENTS_TABLE"]


def handler(event, context):
    incident_id = event.get("incident_id") or uuid.uuid4().hex[:12]
    now = int(time.time())

    ddb.put_item(
        TableName=INCIDENTS,
        Item={
            "incident_id": {"S": incident_id},
            "subject_id": {"S": event["subject_id"]},
            "status": {"S": "OPEN"},
            "started_at": {"N": str(now)},
            "current_tier": {"N": "0"},
            "execution_arn": {"S": event.get("execution_arn", "")},
        },
        # A retried invocation must not overwrite an incident that is already moving.
        ConditionExpression="attribute_not_exists(incident_id)",
    )

    print(json.dumps({"component": "create_incident", "incident_id": incident_id, "event": "opened"}))
    return {
        "incident_id": incident_id,
        "subject_id": event["subject_id"],
        "tier_index": 0,
        "max_tier": int(event.get("max_tier", 3)),
        "wait_s": int(event.get("wait_s", os.environ.get("WAIT_S", "60"))),
    }
