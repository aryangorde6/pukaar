"""The three ways an escalation ends, told to the people who were paged.

    kind = someone_going    everyone reached, claimer included: who is coming, nothing needed
    kind = false_alarm      everyone reached: she cancelled, sorry, nothing needed
    kind = no_one_reached   everyone on her list, with a fresh link each: please call 112, or go

Sends are best-effort per person: one bad address is logged on its row and does
not stop the others. Anyone who has her Telegram bot is told there as well; a person
counts as told if either channel took it. The state fails only if nobody at all
could be told.
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
from templates import render, render_telegram

ddb = boto3.client("dynamodb")
ses = boto3.client("sesv2")
CONTACTS = os.environ["CONTACTS_TABLE"]
SUBJECTS = os.environ["SUBJECTS_TABLE"]
INCIDENTS = os.environ["INCIDENTS_TABLE"]
NOTIFICATIONS = os.environ["NOTIFICATIONS_TABLE"]
BASE_URL = os.environ["BASE_URL"].rstrip("/") + "/"
SENDER = os.environ["SENDER"]
IST = timezone(timedelta(hours=5, minutes=30))


def handler(event, context):
    kind = event["kind"]
    incident_id = event["incident_id"]
    now = int(time.time())
    incident = ddb.get_item(TableName=INCIDENTS, Key={"incident_id": {"S": incident_id}}, ConsistentRead=True)["Item"]
    subject = ddb.get_item(TableName=SUBJECTS, Key={"subject_id": incident["subject_id"]})["Item"]
    contacts = {
        r["contact_id"]["S"]: {"contact_id": r["contact_id"]["S"], "name": r["name"]["S"], "email": r["email"]["S"],
                               "telegram": r.get("telegram_chat_id", {}).get("S", "")}
        for r in ddb.query(TableName=CONTACTS, KeyConditionExpression="subject_id = :s",
                           ExpressionAttributeValues={":s": incident["subject_id"]})["Items"]
    }
    reached_ids = {
        r["contact_id"]["S"]
        for r in ddb.query(TableName=NOTIFICATIONS, KeyConditionExpression="incident_id = :i",
                           FilterExpression="delivered = :t",
                           ExpressionAttributeValues={":i": {"S": incident_id}, ":t": {"BOOL": True}})["Items"]
    }

    parts = subject["address"]["S"].split(", ")
    ctx = {
        "subject_name": subject["name"]["S"],
        "address_line1": ", ".join(parts[:2]),
        "address_line2": ", ".join(parts[2:]),
        "pressed_at": fmt(incident["started_at"]["N"]),
        "minutes_ago": max(1, round((now - int(incident["started_at"]["N"])) / 60)),
        "claimer_name": incident.get("claimed_by_name", {}).get("S", ""),
        "claimed_at": fmt(incident["claimed_at"]["N"]) if "claimed_at" in incident else "",
        "cancelled_at": fmt(incident["cancelled_at"]["N"]) if "cancelled_at" in incident else "",
        "contacted_count": len(reached_ids),
    }

    if kind == "no_one_reached":
        recipients = list(contacts.values())
        ddb.update_item(TableName=INCIDENTS, Key={"incident_id": {"S": incident_id}},
                        UpdateExpression="SET #s = :f, fallback_at = :t",
                        ConditionExpression="#s = :open",
                        ExpressionAttributeNames={"#s": "status"},
                        ExpressionAttributeValues={":f": {"S": "FALLBACK"}, ":open": {"S": "OPEN"}, ":t": {"N": str(now)}})
    else:
        recipients = [contacts[c] for c in reached_ids if c in contacts]

    told, failed = [], []
    for c in recipients:
        this_ctx = dict(ctx)
        if kind == "no_one_reached":
            this_ctx["claim_url"] = BASE_URL + "claim/" + new_token(incident_id, c["contact_id"], now)
        subject_line, text, html = render(kind, this_ctx)
        try:
            ses.send_email(FromEmailAddress=SENDER, Destination={"ToAddresses": [c["email"]]},
                           Content={"Simple": {"Subject": {"Data": subject_line, "Charset": "UTF-8"},
                                               "Body": {"Text": {"Data": text, "Charset": "UTF-8"},
                                                        "Html": {"Data": html, "Charset": "UTF-8"}}}})
            emailed = True
        except ClientError as e:
            emailed, reason = False, e.response["Error"]["Code"]
        tg_id = telegram.send(c["telegram"], *render_telegram(kind, this_ctx)) if c["telegram"] else None
        if tg_id and kind == "no_one_reached":
            ddb.update_item(TableName=NOTIFICATIONS,
                            Key={"incident_id": {"S": incident_id}, "contact_tier": {"S": f"{c['contact_id']}#F"}},
                            UpdateExpression="SET channel = :c, telegram_message_id = :g",
                            ExpressionAttributeValues={":c": {"S": "email+telegram" if emailed else "telegram"}, ":g": {"S": tg_id}})
        if emailed or tg_id:
            told.append(c["contact_id"])
        else:
            failed.append({"contact_id": c["contact_id"], "reason": reason})

    ddb.update_item(TableName=INCIDENTS, Key={"incident_id": {"S": incident_id}},
                    UpdateExpression="SET broadcast = :b",
                    ExpressionAttributeValues={":b": {"S": json.dumps({"kind": kind, "told": told, "failed": failed, "at": now})}})
    print(json.dumps({"component": "broadcast", "kind": kind, "incident_id": incident_id, "told": told, "failed": failed}))
    if recipients and not told:
        raise RuntimeError(f"{kind}: could not tell anyone ({failed})")
    return {**event, "broadcast": {"kind": kind, "told": told, "failed": failed}}


def new_token(incident_id, contact_id, now):
    token = secrets.token_urlsafe(24)
    ddb.put_item(TableName=NOTIFICATIONS, Item={
        "incident_id": {"S": incident_id}, "contact_tier": {"S": f"{contact_id}#F"},
        "contact_id": {"S": contact_id}, "tier": {"N": "99"},
        "token_hash": {"S": hashlib.sha256(token.encode()).hexdigest()},
        "channel": {"S": "email"}, "delivered": {"BOOL": True}, "created_at": {"N": str(now)}, "sent_at": {"N": str(now)},
    })
    return token


def fmt(epoch):
    return datetime.fromtimestamp(int(epoch), IST).strftime("%-I:%M %p").lower()
