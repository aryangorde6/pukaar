"""Choose who gets paged next.

The tier is a *membership*, not an order: the three people not yet reached who are
most likely to answer. Order inside the parallel fan-out is invisible, so nothing
here ranks for order. At the last tier everyone is paged, reached before or not.

Until availability ranking exists the likelihood is the static tier hint, then
distance. Output adds tier_index, contacts, contacted_count, subject, pressed_at.
"""

import json
import os
import time
from datetime import datetime, timedelta, timezone

import boto3

ddb = boto3.client("dynamodb")
CONTACTS = os.environ["CONTACTS_TABLE"]
SUBJECTS = os.environ["SUBJECTS_TABLE"]
INCIDENTS = os.environ["INCIDENTS_TABLE"]
NOTIFICATIONS = os.environ["NOTIFICATIONS_TABLE"]
TIER_SIZE = 3
IST = timezone(timedelta(hours=5, minutes=30))


def handler(event, context):
    incident_id = event["incident_id"]
    subject_id = event["subject_id"]
    tier = int(event["tier_index"]) + 1
    max_tier = int(event["max_tier"])

    contacts = [
        {
            "contact_id": r["contact_id"]["S"],
            "name": r["name"]["S"],
            "email": r["email"]["S"],
            "tier_hint": int(r["tier_hint"]["N"]),
            "proximity_m": int(r["proximity_m"]["N"]),
        }
        for r in ddb.query(
            TableName=CONTACTS,
            KeyConditionExpression="subject_id = :s",
            ExpressionAttributeValues={":s": {"S": subject_id}},
        )["Items"]
    ]
    if not contacts:
        raise RuntimeError(f"subject {subject_id} has nobody to page")

    reached = {
        r["contact_id"]["S"]
        for r in ddb.query(
            TableName=NOTIFICATIONS,
            KeyConditionExpression="incident_id = :i",
            FilterExpression="delivered = :t",
            ExpressionAttributeValues={":i": {"S": incident_id}, ":t": {"BOOL": True}},
        )["Items"]
    }

    if tier >= max_tier:
        chosen = contacts
    else:
        candidates = [c for c in contacts if c["contact_id"] not in reached]
        chosen = sorted(candidates, key=lambda c: (c["tier_hint"], c["proximity_m"]))[:TIER_SIZE]

    incident = ddb.get_item(TableName=INCIDENTS, Key={"incident_id": {"S": incident_id}})["Item"]
    subject = ddb.get_item(TableName=SUBJECTS, Key={"subject_id": {"S": subject_id}})["Item"]
    started_at = int(incident["started_at"]["N"])

    ddb.update_item(
        TableName=INCIDENTS,
        Key={"incident_id": {"S": incident_id}},
        UpdateExpression="SET current_tier = :t",
        ExpressionAttributeValues={":t": {"N": str(tier)}},
    )

    print(json.dumps({"component": "select_tier", "incident_id": incident_id, "tier": tier,
                      "chosen": [c["contact_id"] for c in chosen], "already_reached": sorted(reached)}))
    return {
        **event,
        "tier_index": tier,
        "contacts": [{k: c[k] for k in ("contact_id", "name", "email")} for c in chosen],
        "contacted_count": len(reached | {c["contact_id"] for c in chosen}),
        "subject": {"name": subject["name"]["S"], "address": subject["address"]["S"]},
        "started_at": started_at,
        "pressed_at": datetime.fromtimestamp(started_at, IST).strftime("%-I:%M %p").lower(),
    }
