"""One iteration of the fan-out: page one person by email, and record that you did.

Safe under retry. The notifications row is written first with a conditional put;
a second invocation for the same (incident, contact, tier) finds the row and sends
nothing if it was delivered. A transient send failure leaves delivered = false, so
the retry sends. A permanent one (a bad address) is recorded on the row and the
iteration returns notified = false without failing the others.
"""

import hashlib
import json
import os
import secrets
import time

import boto3
from botocore.exceptions import ClientError

from templates import render

ddb = boto3.client("dynamodb")
ses = boto3.client("sesv2")
NOTIFICATIONS = os.environ["NOTIFICATIONS_TABLE"]
BASE_URL = os.environ["BASE_URL"].rstrip("/") + "/"
SENDER = os.environ["SENDER"]

THROTTLE_CODES = {"Throttling", "ThrottlingException", "TooManyRequestsException", "LimitExceededException"}


class SesThrottled(Exception):
    """Raised so the state machine's Retry can back off and try again."""


def handler(event, context):
    incident_id = event["incident_id"]
    tier = int(event["tier"])
    contact = event["contact"]
    key = {"incident_id": {"S": incident_id}, "contact_tier": {"S": f"{contact['contact_id']}#{tier}"}}
    now = int(time.time())

    token = secrets.token_urlsafe(24)
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    try:
        ddb.put_item(
            TableName=NOTIFICATIONS,
            Item={
                **key,
                "contact_id": {"S": contact["contact_id"]},
                "tier": {"N": str(tier)},
                "token_hash": {"S": token_hash},
                "channel": {"S": "email"},
                "delivered": {"BOOL": False},
                "created_at": {"N": str(now)},
            },
            ConditionExpression="attribute_not_exists(incident_id)",
        )
    except ClientError as e:
        if e.response["Error"]["Code"] != "ConditionalCheckFailedException":
            raise
        row = ddb.get_item(TableName=NOTIFICATIONS, Key=key, ConsistentRead=True)["Item"]
        if row.get("delivered", {}).get("BOOL"):
            log("deduped", incident_id, contact, tier,
                "ConditionalCheckFailedException on put; row already delivered; not sending again")
            return {"notified": True, "deduped": True, "contact_id": contact["contact_id"]}
        # Row exists but nothing reached them: a retry after a transient failure. New token, send.
        ddb.update_item(TableName=NOTIFICATIONS, Key=key,
                        UpdateExpression="SET token_hash = :h", ExpressionAttributeValues={":h": {"S": token_hash}})
        log("retrying", incident_id, contact, tier, "row existed undelivered")

    kind = "first_alert" if tier == 1 else "widened"
    parts = event["subject"]["address"].split(", ")
    address_line1, address_line2 = ", ".join(parts[:2]), ", ".join(parts[2:])
    subject_line, text, html = render(kind, {
        "subject_name": event["subject"]["name"],
        "address_line1": address_line1,
        "address_line2": address_line2,
        "pressed_at": event["pressed_at"],
        "minutes_ago": max(1, round((now - int(event["started_at"])) / 60)),
        "contacted_count": event["contacted_count"],
        "claim_url": f"{BASE_URL}claim/{token}",
    })

    try:
        sent = ses.send_email(
            FromEmailAddress=SENDER,
            Destination={"ToAddresses": [contact["email"]]},
            Content={"Simple": {
                "Subject": {"Data": subject_line, "Charset": "UTF-8"},
                "Body": {"Text": {"Data": text, "Charset": "UTF-8"}, "Html": {"Data": html, "Charset": "UTF-8"}},
            }},
        )
    except ClientError as e:
        code = e.response["Error"]["Code"]
        if code in THROTTLE_CODES:
            raise SesThrottled(code) from e
        reason = f"{code}: {e.response['Error'].get('Message', '')}"[:300]
        ddb.update_item(TableName=NOTIFICATIONS, Key=key,
                        UpdateExpression="SET send_error = :r, failed_at = :t",
                        ExpressionAttributeValues={":r": {"S": reason}, ":t": {"N": str(now)}})
        log("send_failed", incident_id, contact, tier, reason)
        return {"notified": False, "reason": code, "contact_id": contact["contact_id"]}

    ddb.update_item(TableName=NOTIFICATIONS, Key=key,
                    UpdateExpression="SET delivered = :t, sent_at = :s, message_id = :m",
                    ExpressionAttributeValues={":t": {"BOOL": True}, ":s": {"N": str(now)},
                                               ":m": {"S": sent["MessageId"]}})
    log("sent", incident_id, contact, tier, sent["MessageId"])
    return {"notified": True, "contact_id": contact["contact_id"]}


def log(event_name, incident_id, contact, tier, detail):
    print(json.dumps({"component": "notify", "event": event_name, "incident_id": incident_id,
                      "contact_id": contact["contact_id"], "tier": tier, "detail": detail}))
