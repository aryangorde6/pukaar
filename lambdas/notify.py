"""One iteration of the fan-out: page one person by email - and on Telegram, if they
have her bot - and record that you did.

Safe under retry. The notifications row is written first with a conditional put;
a second invocation for the same (incident, contact, tier) finds the row and sends
nothing if it was delivered. A transient send failure leaves delivered = false, so
the retry sends. A permanent one (a bad address) is recorded on the row and the
iteration returns notified = false without failing the others. The person counts as
reached if either channel took the message; the same one-time link is in both.
"""

import hashlib
import json
import os
import secrets
import time

import boto3
from botocore.exceptions import ClientError

import telegram
from ranking import bucket_for
from templates import render, render_telegram

ddb = boto3.client("dynamodb")
ses = boto3.client("sesv2")
NOTIFICATIONS = os.environ["NOTIFICATIONS_TABLE"]
RESPONSE_STATS = os.environ["RESPONSE_STATS_TABLE"]
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
    bucket = bucket_for(now)

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
                "bucket": {"S": bucket},
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
                        UpdateExpression="SET token_hash = :h, bucket = :b",
                        ExpressionAttributeValues={":h": {"S": token_hash}, ":b": {"S": bucket}})
        log("retrying", incident_id, contact, tier, "row existed undelivered")

    kind = "first_alert" if tier == 1 else "widened"
    parts = event["subject"]["address"].split(", ")
    address_line1, address_line2 = ", ".join(parts[:2]), ", ".join(parts[2:])
    ctx = {
        "subject_name": event["subject"]["name"],
        "address_line1": address_line1,
        "address_line2": address_line2,
        "pressed_at": event["pressed_at"],
        "minutes_ago": max(1, round((now - int(event["started_at"])) / 60)),
        "contacted_count": event["contacted_count"],
        "claim_url": f"{BASE_URL}claim/{token}",
        "leave_url": f"{BASE_URL}leave/{token}",
    }
    subject_line, text, html = render(kind, ctx)

    message_id, code = None, None
    try:
        sent = ses.send_email(
            FromEmailAddress=SENDER,
            Destination={"ToAddresses": [contact["email"]]},
            Content={"Simple": {
                "Subject": {"Data": subject_line, "Charset": "UTF-8"},
                "Body": {"Text": {"Data": text, "Charset": "UTF-8"}, "Html": {"Data": html, "Charset": "UTF-8"}},
            }},
        )
        message_id = sent["MessageId"]
    except ClientError as e:
        code = e.response["Error"]["Code"]
        if code in THROTTLE_CODES:
            raise SesThrottled(code) from e
        reason = f"{code}: {e.response['Error'].get('Message', '')}"[:300]
        ddb.update_item(TableName=NOTIFICATIONS, Key=key,
                        UpdateExpression="SET send_error = :r, failed_at = :t",
                        ExpressionAttributeValues={":r": {"S": reason}, ":t": {"N": str(now)}})
        log("send_failed", incident_id, contact, tier, reason)

    tg_id = telegram.send(contact.get("telegram"), *render_telegram(kind, ctx)) if contact.get("telegram") else None

    if not message_id and not tg_id:
        return {"notified": False, "reason": code, "contact_id": contact["contact_id"]}

    channel = "+".join(c for c, ok in (("email", message_id), ("telegram", tg_id)) if ok)
    ids = {k: v for k, v in (("message_id", message_id), ("telegram_message_id", tg_id)) if v}
    ddb.update_item(TableName=NOTIFICATIONS, Key=key,
                    UpdateExpression="SET delivered = :t, sent_at = :s, channel = :c, " + ", ".join(f"{k} = :{k}" for k in ids),
                    ExpressionAttributeValues={":t": {"BOOL": True}, ":s": {"N": str(now)}, ":c": {"S": channel},
                                               **{f":{k}": {"S": v} for k, v in ids.items()}})
    log("sent", incident_id, contact, tier, f"{channel}: " + " ".join(ids.values()))
    count_page(contact, bucket)
    return {"notified": True, "contact_id": contact["contact_id"]}


def count_page(contact, bucket):
    """One more page to this person at this hour, for the ranking. The page is already
    sent and recorded; a failure here must not be read as a page that did not happen."""
    try:
        ddb.update_item(TableName=RESPONSE_STATS,
                        Key={"contact_id": {"S": contact["contact_id"]}, "bucket": {"S": bucket}},
                        UpdateExpression="ADD pages_sent :one",
                        ExpressionAttributeValues={":one": {"N": "1"}})
    except ClientError as e:
        print(json.dumps({"component": "notify", "event": "count_skipped", "contact_id": contact["contact_id"],
                          "bucket": bucket, "error": e.response["Error"]["Code"]}))


def log(event_name, incident_id, contact, tier, detail):
    print(json.dumps({"component": "notify", "event": event_name, "incident_id": incident_id,
                      "contact_id": contact["contact_id"], "tier": tier, "detail": detail}))
