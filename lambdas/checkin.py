"""The weekly check-in: one low-stakes page to everyone on her list, so the ranking
learns who is reachable at which hour without waiting for an emergency.

    {"subject_id": "sunita"}            # from the schedule; defaults to SUBJECT_ID

Each person gets one email (and Telegram, if they have her bot) that says, first,
that this is not an emergency, with one button: "I'd be reachable now". Tapping it
within ten minutes counts as an answered page for that hour (web.py, /checkin);
not tapping counts as a page that went unanswered - which is the honest reading
of "could you go right now?". Rows go to the notifications table under a
check-in id, so a check-in link can never claim an incident: there is none.
"""

import hashlib
import json
import os
import secrets
import time
from datetime import datetime, timedelta, timezone

import boto3
from botocore.exceptions import ClientError

import telegram
from ranking import bucket_for
from templates import render, render_telegram

ddb = boto3.client("dynamodb")
ses = boto3.client("sesv2")
CONTACTS = os.environ["CONTACTS_TABLE"]
SUBJECTS = os.environ["SUBJECTS_TABLE"]
NOTIFICATIONS = os.environ["NOTIFICATIONS_TABLE"]
RESPONSE_STATS = os.environ["RESPONSE_STATS_TABLE"]
BASE_URL = os.environ["BASE_URL"].rstrip("/") + "/"
SENDER = os.environ["SENDER"]
IST = timezone(timedelta(hours=5, minutes=30))


def handler(event, context):
    subject_id = (event or {}).get("subject_id") or os.environ["SUBJECT_ID"]
    now = int(time.time())
    checkin_id = "checkin-" + datetime.fromtimestamp(now, IST).strftime("%Y%m%d-%H%M%S")
    subject = ddb.get_item(TableName=SUBJECTS, Key={"subject_id": {"S": subject_id}})["Item"]
    contacts = ddb.query(TableName=CONTACTS, KeyConditionExpression="subject_id = :s",
                         ExpressionAttributeValues={":s": {"S": subject_id}})["Items"]
    only = set((event or {}).get("contacts") or [])  # verify.py pings one person, not six
    bucket = bucket_for(now)
    sent, failed = [], []
    for c in contacts:
        cid = c["contact_id"]["S"]
        if only and cid not in only:
            continue
        token = secrets.token_urlsafe(24)
        ddb.put_item(TableName=NOTIFICATIONS, Item={
            "incident_id": {"S": checkin_id}, "contact_tier": {"S": f"{cid}#C"},
            "contact_id": {"S": cid}, "tier": {"N": "0"}, "kind": {"S": "checkin"},
            "token_hash": {"S": hashlib.sha256(token.encode()).hexdigest()},
            "bucket": {"S": bucket}, "delivered": {"BOOL": False}, "created_at": {"N": str(now)},
        })
        ctx = {"subject_name": subject["name"]["S"], "claim_url": f"{BASE_URL}checkin/{token}"}
        subject_line, text, html = render("checkin", ctx)
        message_id = None
        try:
            message_id = ses.send_email(
                FromEmailAddress=SENDER, Destination={"ToAddresses": [c["email"]["S"]]},
                Content={"Simple": {"Subject": {"Data": subject_line, "Charset": "UTF-8"},
                                    "Body": {"Text": {"Data": text, "Charset": "UTF-8"},
                                             "Html": {"Data": html, "Charset": "UTF-8"}}}})["MessageId"]
        except ClientError as e:
            failed.append({"contact_id": cid, "reason": e.response["Error"]["Code"]})
        chat_id = c.get("telegram_chat_id", {}).get("S", "")
        tg_id = telegram.send(chat_id, *render_telegram("checkin", ctx)) if chat_id else None
        if not message_id and not tg_id:
            continue
        channel = "+".join(name for name, ok in (("email", message_id), ("telegram", tg_id)) if ok)
        ids = {k: v for k, v in (("message_id", message_id), ("telegram_message_id", tg_id)) if v}
        ddb.update_item(TableName=NOTIFICATIONS, Key={"incident_id": {"S": checkin_id}, "contact_tier": {"S": f"{cid}#C"}},
                        UpdateExpression="SET delivered = :t, sent_at = :s, channel = :c, " + ", ".join(f"{k} = :{k}" for k in ids),
                        ExpressionAttributeValues={":t": {"BOOL": True}, ":s": {"N": str(now)}, ":c": {"S": channel},
                                                   **{f":{k}": {"S": v} for k, v in ids.items()}})
        ddb.update_item(TableName=RESPONSE_STATS, Key={"contact_id": {"S": cid}, "bucket": {"S": bucket}},
                        UpdateExpression="ADD pages_sent :one", ExpressionAttributeValues={":one": {"N": "1"}})
        sent.append(cid)
    print(json.dumps({"component": "checkin", "checkin_id": checkin_id, "bucket": bucket, "sent": sent, "failed": failed}))
    return {"checkin_id": checkin_id, "bucket": bucket, "sent": sent, "failed": failed}
