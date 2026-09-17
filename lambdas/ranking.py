"""Who is most likely to answer right now.

Every page writes a counter and every answer writes another, per person and per hour
bucket (`14#weekday`). The score reads them back:

    score = 0.6 * answers + 0.2 * fast + 0.2 * near

answers  share of pages answered, smoothed by a prior worth two pages - one of them
         answered if they are usually home during the day, half if not - so nobody
         starts at zero or at one. Pages in this hour bucket count twice: the hour
         matters, but one unanswered page cannot erase a history of answering
fast     1 / (1 + mean answer time in minutes); 0.5 with no history, 0 if paged and
         never answered
near     1 / (1 + metres / 100)

So a known answerer beats an unknown, an unknown beats a known non-answerer, and
distance only decides between people the history cannot separate. The score decides
who is in the next circle, not the order inside it - the fan-out pages a circle in the
same instant.
"""

import os
from datetime import datetime, timedelta, timezone

import boto3

ddb = boto3.client("dynamodb")
CONTACTS = os.environ["CONTACTS_TABLE"]
RESPONSE_STATS = os.environ["RESPONSE_STATS_TABLE"]
IST = timezone(timedelta(hours=5, minutes=30))
W_ANSWERS, W_FAST, W_NEAR = 0.6, 0.2, 0.2
TIER_SIZE = 3


def bucket_for(ts):
    t = datetime.fromtimestamp(int(ts), IST)
    return f"{t.hour:02d}#{'weekend' if t.weekday() >= 5 else 'weekday'}"


def score(contact, rows, bucket):
    """(score, basis) for one contact from their response_stats rows, plain ints."""
    pages = sum(r["pages_sent"] for r in rows)
    answered = sum(r["responses"] for r in rows)
    latency_ms = sum(r["total_latency_ms"] for r in rows)
    pages_h = sum(r["pages_sent"] for r in rows if r["bucket"] == bucket)
    answered_h = sum(r["responses"] for r in rows if r["bucket"] == bucket)
    home = contact["home_during_day"]
    answers = (answered + answered_h + (1 if home else 0.5)) / (pages + pages_h + 2)
    if answered:
        fast = 1 / (1 + latency_ms / answered / 60000)
    else:
        fast = 0.0 if pages else 0.5
    near = 1 / (1 + contact["proximity_m"] / 100)
    if pages:
        basis = f"{answered}/{pages} answered" + (f", {answered_h}/{pages_h} this hour" if pages_h else "")
    else:
        basis = "no history; usually " + ("home" if home else "out")
    return round(W_ANSWERS * answers + W_FAST * fast + W_NEAR * near, 3), basis


def rank(subject_id, now):
    """Every contact of the subject, best first, each with score and basis."""
    contacts = [
        {
            "contact_id": r["contact_id"]["S"],
            "name": r["name"]["S"],
            "email": r["email"]["S"],
            "proximity_m": int(r["proximity_m"]["N"]),
            "home_during_day": r.get("home_during_day", {}).get("BOOL", False),
        }
        for r in ddb.query(
            TableName=CONTACTS,
            KeyConditionExpression="subject_id = :s",
            ExpressionAttributeValues={":s": {"S": subject_id}},
        )["Items"]
    ]
    bucket = bucket_for(now)
    for c in contacts:
        rows = [
            {
                "bucket": r["bucket"]["S"],
                "pages_sent": int(r.get("pages_sent", {}).get("N", 0)),
                "responses": int(r.get("responses", {}).get("N", 0)),
                "total_latency_ms": int(r.get("total_latency_ms", {}).get("N", 0)),
            }
            for r in ddb.query(
                TableName=RESPONSE_STATS,
                KeyConditionExpression="contact_id = :c",
                ExpressionAttributeValues={":c": {"S": c["contact_id"]}},
            )["Items"]
        ]
        c["score"], c["basis"] = score(c, rows, bucket)
    return sorted(contacts, key=lambda c: (-c["score"], c["proximity_m"], c["contact_id"]))
