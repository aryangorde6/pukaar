"""Catch target for every spine state. Marks the incident FAILED so the failure is on the
row, and emits a metric so it can be alarmed on. The execution then ends in Fail."""

import json
import os
import time

import boto3
from botocore.exceptions import ClientError

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
        # The status becomes FAILED only while nobody has answered. A claim or a cancel that landed
        # before the spine broke is what happened; the failure is recorded beside it, not over it.
        try:
            ddb.update_item(
                TableName=INCIDENTS,
                Key={"incident_id": {"S": incident_id}},
                UpdateExpression="SET #s = :failed, failure = :why, failed_at = :now",
                ConditionExpression="attribute_not_exists(#s) OR #s IN (:open, :fallback)",
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues={
                    ":failed": {"S": "FAILED"},
                    ":open": {"S": "OPEN"},
                    ":fallback": {"S": "FALLBACK"},
                    ":why": {"S": reason},
                    ":now": {"N": str(int(time.time()))},
                },
            )
        except ClientError as e:
            if e.response["Error"]["Code"] != "ConditionalCheckFailedException":
                raise
            ddb.update_item(
                TableName=INCIDENTS,
                Key={"incident_id": {"S": incident_id}},
                UpdateExpression="SET failure = :why, failed_at = :now",
                ExpressionAttributeValues={":why": {"S": reason}, ":now": {"N": str(int(time.time()))}},
            )

    cw.put_metric_data(
        Namespace="Pukaar",
        MetricData=[{"MetricName": "EscalationFailed", "Value": 1, "Unit": "Count"}],
    )
    print(json.dumps({"component": "record_failure", "incident_id": incident_id, "error": error}))
    return {"recorded": True, "incident_id": incident_id}
