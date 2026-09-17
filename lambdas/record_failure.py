"""Catch target for every spine state. Marks the incident FAILED so the failure is on the
row, and emits a metric so it can be alarmed on. The execution then ends in Fail."""

import json
import os
import time

import boto3

ddb = boto3.client("dynamodb")
cw = boto3.client("cloudwatch")
INCIDENTS = os.environ["INCIDENTS_TABLE"]


def handler(event, context):
    incident_id = event.get("incident_id", "unknown")
    # Either a caught exception from a spine state, or NotifyDecision found that
    # nobody in the tier was reached and routed here without one.
    error = event.get("error") or {
        "Error": "TierReachedNobody",
        "Cause": f"tier {event.get('tier_index')} paged {len(event.get('contacts', []))} people; none reached",
    }
    reason = json.dumps(error)[:900]

    if incident_id != "unknown":
        ddb.update_item(
            TableName=INCIDENTS,
            Key={"incident_id": {"S": incident_id}},
            UpdateExpression="SET #s = :failed, failure = :why, failed_at = :now",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":failed": {"S": "FAILED"},
                ":why": {"S": reason},
                ":now": {"N": str(int(time.time()))},
            },
        )

    cw.put_metric_data(
        Namespace="Pukaar",
        MetricData=[{"MetricName": "EscalationFailed", "Value": 1, "Unit": "Count"}],
    )
    print(json.dumps({"component": "record_failure", "incident_id": incident_id, "error": error}))
    return {"recorded": True, "incident_id": incident_id}
