"""The one public origin, behind a Lambda Function URL.

    GET  /                the help button (the subject's page)
    POST /trigger         press it: start an escalation, answer with its id
    GET  /claim/{token}   the responder's page: who, where, and whether anyone has gone
    POST /claim/{token}   "I'm going now" - one conditional write decides the race
    POST /cancel          she is OK: mark it cancelled; the machine tells everyone
    GET  /status/{id}     what her screen shows: open, who is coming, cancelled
    GET  /manifest.webmanifest, /icon-192.png, /icon-512.png
                          so the button installs on her home screen as "Pukaar"

GET never writes. Mail clients and link scanners fetch every link in an email;
if a GET could claim, a corporate proxy would be on its way to Sunita instead
of Ravi.
"""

import base64
import hashlib
import json
import os
import time
import urllib.parse
import uuid
from datetime import datetime, timedelta, timezone
from string import Template

import boto3
from botocore.exceptions import ClientError

from ranking import TIER_SIZE, rank

ddb = boto3.client("dynamodb")
sfn = boto3.client("stepfunctions")
kms = boto3.client("kms")
lam = boto3.client("lambda")

CONTACTS = os.environ["CONTACTS_TABLE"]
SUBJECTS = os.environ["SUBJECTS_TABLE"]
INCIDENTS = os.environ["INCIDENTS_TABLE"]
NOTIFICATIONS = os.environ["NOTIFICATIONS_TABLE"]
RESPONSE_STATS = os.environ["RESPONSE_STATS_TABLE"]
STATE_MACHINE_ARN = os.environ["STATE_MACHINE_ARN"]
BROADCAST_FN = os.environ["BROADCAST_FN"]
SUBJECT_ID = os.environ["SUBJECT_ID"]
WAIT_S = int(os.environ.get("WAIT_S", "60"))
MAX_TIER = int(os.environ.get("MAX_TIER", "3"))
IST = timezone(timedelta(hours=5, minutes=30))


# --- routing ------------------------------------------------------------------

def handler(event, context):
    method = event["requestContext"]["http"]["method"]
    path = event.get("rawPath", "/")

    if method == "GET" and path == "/":
        return html(200, trigger_page())
    if method == "POST" and path == "/trigger":
        return trigger()
    if path.startswith("/claim/") and method in ("GET", "POST"):
        token = path[len("/claim/"):]
        return claim_page(token) if method == "GET" else claim(token)
    if method == "POST" and path == "/cancel":
        return cancel(event)
    if method == "GET" and path.startswith("/status/"):
        return status(path[len("/status/"):])
    if method == "GET" and path == "/manifest.webmanifest":
        return static(MANIFEST, "application/manifest+json")
    if method == "GET" and path in ("/icon-192.png", "/icon-512.png"):
        return static(ICONS[path], "image/png")
    return html(404, NOT_FOUND)


def cancel(event):
    body = event.get("body") or "{}"
    if event.get("isBase64Encoded"):
        body = base64.b64decode(body).decode()
    incident_id = json.loads(body).get("incident_id", "")
    if not incident_id:
        return jsonr(400, {"error": "incident_id required"})
    now = int(time.time())
    try:
        old = ddb.update_item(
            TableName=INCIDENTS, Key={"incident_id": {"S": incident_id}},
            UpdateExpression="SET #s = :c, cancelled_at = :t",
            # She may be fine after all, even once someone is on the way: they are told too.
            ConditionExpression="#s IN (:open, :fallback, :claimed)",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":c": {"S": "CANCELLED"}, ":open": {"S": "OPEN"}, ":fallback": {"S": "FALLBACK"},
                                       ":claimed": {"S": "CLAIMED"}, ":t": {"N": str(now)}},
            ReturnValues="ALL_OLD",
        )
    except ClientError as e:
        if e.response["Error"]["Code"] != "ConditionalCheckFailedException":
            raise
    else:
        if old["Attributes"]["status"]["S"] == "CLAIMED":
            # The machine finished when the claim was broadcast; tell everyone directly.
            lam.invoke(FunctionName=BROADCAST_FN, InvocationType="RequestResponse",
                       Payload=json.dumps({"kind": "false_alarm", "incident_id": incident_id}).encode())
        else:
            wake(old["Attributes"], "cancelled")
    print(json.dumps({"component": "web", "event": "cancel", "incident_id": incident_id}))
    return status(incident_id)


def wake(old_row, why):
    """Hand the parked task token back so the machine reads the row now, not at the
    tier boundary. Best-effort: a stale token means the machine already moved on."""
    token = old_row.get("task_token", {}).get("S")
    if not token:
        return
    try:
        sfn.send_task_success(taskToken=token, output=json.dumps({"woken_by": why}))
        print(json.dumps({"component": "web", "event": "woke", "why": why}))
    except ClientError as e:
        print(json.dumps({"component": "web", "event": "wake_skipped", "why": why,
                          "error": e.response["Error"]["Code"]}))


def status(incident_id):
    """What is true on the incident row, and nothing that is not on it."""
    item = ddb.get_item(TableName=INCIDENTS, Key={"incident_id": {"S": incident_id}},
                        ConsistentRead=True).get("Item")
    if item is None:
        return jsonr(404, {"error": "no such incident"})
    out = {"incident_id": incident_id, "status": item["status"]["S"]}
    if "claimed_by_name" in item:
        out["claimed_by_name"] = item["claimed_by_name"]["S"]
        out["claimed_at"] = fmt_time(item["claimed_at"]["N"])
    if "record_opened_at" in item:
        out["record_opened_at"] = fmt_time(item["record_opened_at"]["N"])
    if "cancelled_at" in item:
        out["cancelled_at"] = fmt_time(item["cancelled_at"]["N"])
    return jsonr(200, out)


# --- routes -------------------------------------------------------------------

def trigger_page():
    names = first_circle_names()
    if names:
        told = f"<strong>{escape(join_names(names))}</strong> will be told straight away."
        disabled = ""
    else:
        told = "No one has been added yet, so this button cannot reach anyone."
        disabled = "disabled"
    return PAGE.substitute(told=told, disabled=disabled, names_json=json.dumps(names),
                           strings_json=json.dumps(STRINGS, ensure_ascii=False))


def trigger():
    incident_id = uuid.uuid4().hex[:12]
    payload = {
        "incident_id": incident_id,
        "subject_id": SUBJECT_ID,
        "wait_s": WAIT_S,
        "max_tier": MAX_TIER,
    }
    started = sfn.start_execution(
        stateMachineArn=STATE_MACHINE_ARN,
        name=incident_id,
        input=json.dumps(payload),
    )
    print(json.dumps({"component": "web", "event": "trigger", "incident_id": incident_id,
                      "execution_arn": started["executionArn"]}))
    return jsonr(200, {"incident_id": incident_id, "told": first_circle_names()})


# --- claim --------------------------------------------------------------------

def resolve(token):
    """Token -> (notification row, incident row) or None. Never says which half was wrong."""
    if not token or len(token) > 64:
        return None
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    rows = ddb.query(
        TableName=NOTIFICATIONS,
        IndexName="token_hash-index",
        KeyConditionExpression="token_hash = :h",
        ExpressionAttributeValues={":h": {"S": token_hash}},
    )["Items"]
    if len(rows) != 1:
        return None
    note = rows[0]
    incident = ddb.get_item(TableName=INCIDENTS, Key={"incident_id": note["incident_id"]},
                            ConsistentRead=True).get("Item")
    if incident is None:
        return None
    return note, incident


def claim_page(token):
    found = resolve(token)
    if found is None:
        return html(404, BAD_LINK)
    note, incident = found
    return html(200, render_claim_state(note, incident))


def claim(token):
    found = resolve(token)
    if found is None:
        return html(404, BAD_LINK)
    note, incident = found
    contact = ddb.get_item(TableName=CONTACTS, Key={"subject_id": incident["subject_id"],
                                                    "contact_id": note["contact_id"]})["Item"]
    now = int(time.time())
    try:
        old = ddb.update_item(
            TableName=INCIDENTS,
            Key={"incident_id": incident["incident_id"]},
            UpdateExpression="SET #s = :claimed, claimed_by = :c, claimed_by_name = :n, claimed_at = :t",
            # A late answer on an alert that widened to everyone is still a good answer.
            ConditionExpression="#s IN (:open, :fallback)",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":claimed": {"S": "CLAIMED"}, ":open": {"S": "OPEN"}, ":fallback": {"S": "FALLBACK"},
                ":c": note["contact_id"], ":n": contact["name"], ":t": {"N": str(now)},
            },
            ReturnValues="ALL_OLD",
        )
        won = True
        wake(old["Attributes"], "claimed")
    except ClientError as e:
        if e.response["Error"]["Code"] != "ConditionalCheckFailedException":
            raise
        won = False
    count_answer(note, now)
    print(json.dumps({"component": "web", "event": "claim", "incident_id": incident["incident_id"]["S"],
                      "contact_id": note["contact_id"]["S"], "won": won}))
    # Either way, show what is true now: the row after the write, not what we hoped.
    incident = ddb.get_item(TableName=INCIDENTS, Key={"incident_id": incident["incident_id"]},
                            ConsistentRead=True)["Item"]
    return html(200, render_claim_state(note, incident, on_claim=won))


def count_answer(note, now):
    """They answered a page: one response and its latency, against the hour the page went
    out. Once per page, win or lose - a lost race still says they were reachable."""
    try:
        ddb.update_item(TableName=NOTIFICATIONS,
                        Key={"incident_id": note["incident_id"], "contact_tier": note["contact_tier"]},
                        UpdateExpression="SET responded_at = :t",
                        ConditionExpression="attribute_not_exists(responded_at)",
                        ExpressionAttributeValues={":t": {"N": str(now)}})
    except ClientError as e:
        if e.response["Error"]["Code"] != "ConditionalCheckFailedException":
            raise
        return
    if "sent_at" not in note or "bucket" not in note:
        return  # no page time on record, so no latency to count
    ddb.update_item(TableName=RESPONSE_STATS,
                    Key={"contact_id": note["contact_id"], "bucket": note["bucket"]},
                    UpdateExpression="ADD responses :one, total_latency_ms :ms",
                    ExpressionAttributeValues={":one": {"N": "1"},
                                               ":ms": {"N": str(max(0, now - int(note["sent_at"]["N"])) * 1000)}})


def render_claim_state(note, incident, on_claim=False):
    subject = ddb.get_item(TableName=SUBJECTS, Key={"subject_id": incident["subject_id"]})["Item"]
    status = incident["status"]["S"]
    name = escape(subject["name"]["S"])
    address = subject["address"]["S"]
    parts = address.split(", ")
    ctx = {
        "name": name,
        "address_line1": escape(", ".join(parts[:2])),
        "address_line2": escape(", ".join(parts[2:])),
        "maps_url": "https://maps.google.com/?q=" + urllib.parse.quote(address),
        "pressed_at": fmt_time(incident["started_at"]["N"]),
    }
    if status in ("OPEN", "FALLBACK"):
        reached = ddb.query(TableName=NOTIFICATIONS, KeyConditionExpression="incident_id = :i",
                            FilterExpression="delivered = :t",
                            ExpressionAttributeValues={":i": incident["incident_id"], ":t": {"BOOL": True}})["Count"]
        others = max(reached - 1, 0)
        ctx["contacted_count"] = reached
        ctx["others"] = f"{others} other{'s were' if others != 1 else ' was'} contacted too" if others else "you are the only one contacted"
        return CLAIM_ACTIONABLE.substitute(ctx)
    if status == "CLAIMED":
        ctx["claimed_at"] = fmt_time(incident["claimed_at"]["N"])
        if incident["claimed_by"]["S"] == note["contact_id"]["S"]:
            ctx["record"] = open_record(subject, incident["incident_id"]["S"], note["contact_id"]["S"], on_claim)
            return CLAIM_YOURS.substitute(ctx)
        ctx["claimer"] = escape(incident["claimed_by_name"]["S"])
        return CLAIM_TAKEN.substitute(ctx)
    if status == "CANCELLED":
        ctx["cancelled_at"] = fmt_time(incident["cancelled_at"]["N"])
        return CLAIM_CANCELLED.substitute(ctx)
    ctx["ended_at"] = fmt_time(incident.get("failed_at", incident.get("ended_at", incident["started_at"]))["N"])
    return CLAIM_OVER.substitute(ctx)


def open_record(subject, incident_id, contact_id, on_claim):
    """Her medical notes, for the one person who is going. The ciphertext is opened
    with her id as encryption context, and every opening is logged with who and when.
    The opening that comes with the claim itself is also written on her incident, so
    her screen can say who has them. Later refreshes are logged but write nothing."""
    if "record" not in subject:
        return "<p class=\"muted\">No medical notes on file.</p>"
    subject_id = subject["subject_id"]["S"]
    try:
        plain = kms.decrypt(CiphertextBlob=subject["record"]["B"],
                            EncryptionContext={"subject_id": subject_id})["Plaintext"].decode()
    except ClientError as e:
        print(json.dumps({"component": "web", "event": "record_release_failed", "incident_id": incident_id,
                          "contact_id": contact_id, "subject_id": subject_id, "error": e.response["Error"]["Code"]}))
        return "<p class=\"muted\">Her medical notes could not be opened. If it matters, call 112.</p>"
    now = datetime.now(timezone.utc)
    print(json.dumps({"component": "web", "event": "record_released", "incident_id": incident_id,
                      "contact_id": contact_id, "subject_id": subject_id, "at": now.isoformat(timespec="seconds")}))
    if on_claim:
        ddb.update_item(TableName=INCIDENTS, Key={"incident_id": {"S": incident_id}},
                        UpdateExpression="SET record_opened_by = :c, record_opened_at = :t",
                        ExpressionAttributeValues={":c": {"S": contact_id}, ":t": {"N": str(int(now.timestamp()))}})
    return "<p>" + "<br>".join(escape(line) for line in plain.splitlines()) + "</p>"


def fmt_time(epoch):
    return datetime.fromtimestamp(int(epoch), IST).strftime("%-I:%M %p").lower()


# --- data ---------------------------------------------------------------------

def first_circle_names():
    """The people a press right now would page first: the same ranking SelectTier uses,
    with nobody reached yet."""
    return [c["name"] for c in rank(SUBJECT_ID, time.time())[:TIER_SIZE]]


def join_names(names):
    if len(names) == 1:
        return names[0]
    return ", ".join(names[:-1]) + " and " + names[-1]


# --- responses ----------------------------------------------------------------

def html(status, body):
    return {
        "statusCode": status,
        "headers": {"content-type": "text/html; charset=utf-8", "cache-control": "no-store"},
        "body": body,
    }


def jsonr(status, obj):
    return {
        "statusCode": status,
        "headers": {"content-type": "application/json", "cache-control": "no-store"},
        "body": json.dumps(obj),
    }


def static(body, content_type):
    """The manifest and the icons: fixed bytes, cacheable for a day."""
    return {
        "statusCode": 200,
        "headers": {"content-type": content_type, "cache-control": "public, max-age=86400"},
        "body": base64.b64encode(body).decode() if isinstance(body, bytes) else body,
        "isBase64Encoded": isinstance(body, bytes),
    }


def escape(s):
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# --- home screen --------------------------------------------------------------

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
def _read(name):
    with open(os.path.join(STATIC_DIR, name), "rb") as f:
        return f.read()


ICONS = {f"/icon-{n}.png": _read(f"icon-{n}.png") for n in (192, 512)}
MANIFEST = json.dumps({
    "name": "Pukaar",
    "short_name": "Pukaar",
    "description": "One press. The people most likely to answer are paged at once.",
    "start_url": "/",
    "display": "standalone",
    "background_color": "#FAF9F7",
    "theme_color": "#A4161A",
    "icons": [
        {"src": "/icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any maskable"},
        {"src": "/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any maskable"},
    ],
})


# --- her page, in her language ------------------------------------------------
# Only her page is translated: the people paged are younger and the pages they get are
# English. English is served; ?lang=xx at setup picks her language and the phone remembers
# it; the one pill on the page switches between English and that language, never a menu.
# A language is these strings and, ideally, one person who speaks it and has read them.

STRINGS = {
    "en": {
        "title": "I need help", "idle_h1": "I NEED HELP", "press": "I need help",
        "press_busy": "Calling for help…", "press_once": "Press once.",
        "told": "{names} will be told straight away.",
        "told_none": "No one has been added yet, so this button cannot reach anyone.",
        "sent_h1": "Help is being called", "sent_names": "{names} have been told.",
        "waiting": "Waiting for one of them to answer…", "cancel": "Cancel — I’m OK",
        "cancelling": "Cancelling…", "coming_h1": "{name} is coming", "on_way": "On the way now.",
        "has_notes": "{name} has your medical notes.", "cancelled_h1": "Cancelled",
        "cancelled_p": "Everyone has been told it was a false alarm.",
        "failed_h1": "Couldn’t send", "retry": "Try again", "call_112": "Or call 112 now.",
        "foot": "Not working? Call", "and": " and ", "name": "English",
    },
    "mr": {
        "title": "मला मदत हवी आहे", "idle_h1": "मला मदत हवी आहे", "press": "मला मदत हवी आहे",
        "press_busy": "मदत बोलावत आहे…", "press_once": "एकदाच दाबा.",
        "told": "{names} यांना लगेच कळवले जाईल.",
        "told_none": "अजून कोणालाही जोडलेले नाही, त्यामुळे हे बटण कोणापर्यंत पोहोचू शकत नाही.",
        "sent_h1": "मदत बोलावली आहे", "sent_names": "{names} यांना कळवले आहे.",
        "waiting": "त्यांपैकी कोणीतरी उत्तर देण्याची वाट पाहत आहोत…", "cancel": "रद्द करा — मी ठीक आहे",
        "cancelling": "रद्द करत आहे…", "coming_h1": "{name} येत आहे", "on_way": "वाटेत आहेत.",
        "has_notes": "{name} यांच्याकडे तुमच्या वैद्यकीय नोंदी आहेत.", "cancelled_h1": "रद्द केले",
        "cancelled_p": "सर्वांना कळवले आहे की सगळे ठीक आहे.",
        "failed_h1": "पाठवता आले नाही", "retry": "पुन्हा प्रयत्न करा", "call_112": "किंवा आत्ताच 112 ला फोन करा.",
        "foot": "चालत नाही? फोन करा", "and": " आणि ", "name": "मराठी",
    },
    "hi": {
        "title": "मुझे मदद चाहिए", "idle_h1": "मुझे मदद चाहिए", "press": "मुझे मदद चाहिए",
        "press_busy": "मदद बुलाई जा रही है…", "press_once": "एक बार दबाएँ.",
        "told": "{names} को तुरंत बता दिया जाएगा.",
        "told_none": "अभी किसी को जोड़ा नहीं गया है, इसलिए यह बटन किसी तक नहीं पहुँच सकता.",
        "sent_h1": "मदद बुलाई गई है", "sent_names": "{names} को बता दिया गया है.",
        "waiting": "उनमें से किसी के जवाब का इंतज़ार है…", "cancel": "रद्द करें — मैं ठीक हूँ",
        "cancelling": "रद्द किया जा रहा है…", "coming_h1": "{name} आ रहे हैं", "on_way": "रास्ते में हैं.",
        "has_notes": "{name} के पास आपकी मेडिकल जानकारी है.", "cancelled_h1": "रद्द किया गया",
        "cancelled_p": "सबको बता दिया गया है कि सब ठीक है.",
        "failed_h1": "भेजा नहीं जा सका", "retry": "फिर से कोशिश करें", "call_112": "या अभी 112 पर फ़ोन करें.",
        "foot": "काम नहीं कर रहा? फ़ोन करें", "and": " और ", "name": "हिन्दी",
    },
    # The eight below are drafts checked by machine translation only, not yet read by a
    # speaker; the README says so, and English is one tap away on every screen.
    "gu": {
        "title": "મને મદદ જોઈએ છે", "idle_h1": "મને મદદ જોઈએ છે", "press": "મને મદદ જોઈએ છે", "press_busy": "મદદ બોલાવી રહ્યા છીએ…", "press_once": "એક વાર દબાવો.", "told": "{names} ને તરત જણાવવામાં આવશે.", "told_none": "હજી કોઈને ઉમેરવામાં આવ્યું નથી, તેથી આ બટન કોઈ સુધી પહોંચી શકતું નથી.", "sent_h1": "મદદ બોલાવી છે", "sent_names": "{names} ને જણાવી દીધું છે.", "waiting": "તેમાંથી કોઈના જવાબની રાહ જોઈએ છીએ…", "cancel": "રદ કરો — હું ઠીક છું", "cancelling": "રદ કરી રહ્યા છીએ…", "coming_h1": "{name} આવી રહ્યા છે", "on_way": "રસ્તામાં છે.", "has_notes": "{name} પાસે તમારી તબીબી માહિતી છે.", "cancelled_h1": "રદ કર્યું", "cancelled_p": "બધાને જણાવી દીધું છે કે બધું ઠીક છે.", "failed_h1": "મોકલી શકાયું નહીં", "retry": "ફરી પ્રયત્ન કરો", "call_112": "અથવા હમણાં જ 112 પર ફોન કરો.", "foot": "કામ નથી કરતું? ફોન કરો", "and": " અને ", "name": "ગુજરાતી",
    },
    "ta": {
        "title": "எனக்கு உதவி வேண்டும்", "idle_h1": "எனக்கு உதவி வேண்டும்", "press": "எனக்கு உதவி வேண்டும்", "press_busy": "உதவி அழைக்கப்படுகிறது…", "press_once": "ஒரு முறை அழுத்துங்கள்.", "told": "{names} ஆகியோருக்கு உடனே தெரிவிக்கப்படும்.", "told_none": "இன்னும் யாரும் சேர்க்கப்படவில்லை, எனவே இந்த பொத்தான் யாரையும் அடைய முடியாது.", "sent_h1": "உதவி அழைக்கப்பட்டது", "sent_names": "{names} ஆகியோருக்கு தெரிவிக்கப்பட்டது.", "waiting": "அவர்களில் ஒருவர் பதிலளிக்கக் காத்திருக்கிறோம்…", "cancel": "ரத்து செய் — நான் நலம்", "cancelling": "ரத்து செய்யப்படுகிறது…", "coming_h1": "{name} வருகிறார்", "on_way": "வழியில் இருக்கிறார்.", "has_notes": "{name} இடம் உங்கள் மருத்துவக் குறிப்புகள் உள்ளன.", "cancelled_h1": "ரத்து செய்யப்பட்டது", "cancelled_p": "எல்லாம் நலம் என்று அனைவருக்கும் தெரிவிக்கப்பட்டது.", "failed_h1": "அனுப்ப முடியவில்லை", "retry": "மீண்டும் முயற்சிக்கவும்", "call_112": "அல்லது இப்போதே 112-ஐ அழைக்கவும்.", "foot": "வேலை செய்யவில்லையா? அழைக்கவும்", "and": " மற்றும் ", "name": "தமிழ்",
    },
    "te": {
        "title": "నాకు సహాయం కావాలి", "idle_h1": "నాకు సహాయం కావాలి", "press": "నాకు సహాయం కావాలి", "press_busy": "సహాయం పిలుస్తున్నాం…", "press_once": "ఒకసారి నొక్కండి.", "told": "{names} కి వెంటనే తెలియజేయబడుతుంది.", "told_none": "ఇంకా ఎవరినీ చేర్చలేదు, కాబట్టి ఈ బటన్ ఎవరికీ చేరదు.", "sent_h1": "సహాయం పిలిచాం", "sent_names": "{names} కి తెలియజేశాం.", "waiting": "వారిలో ఎవరైనా జవాబు ఇచ్చే వరకు వేచి ఉన్నాం…", "cancel": "రద్దు చేయి — నేను బాగానే ఉన్నాను", "cancelling": "రద్దు చేస్తున్నాం…", "coming_h1": "{name} వస్తున్నారు", "on_way": "దారిలో ఉన్నారు.", "has_notes": "{name} దగ్గర మీ వైద్య వివరాలు ఉన్నాయి.", "cancelled_h1": "రద్దు చేయబడింది", "cancelled_p": "అంతా బాగానే ఉందని అందరికీ తెలియజేశాం.", "failed_h1": "పంపలేకపోయాం", "retry": "మళ్ళీ ప్రయత్నించండి", "call_112": "లేదా ఇప్పుడే 112 కి ఫోన్ చేయండి.", "foot": "పని చేయడం లేదా? ఫోన్ చేయండి", "and": " మరియు ", "name": "తెలుగు",
    },
    "kn": {
        "title": "ನನಗೆ ಸಹಾಯ ಬೇಕು", "idle_h1": "ನನಗೆ ಸಹಾಯ ಬೇಕು", "press": "ನನಗೆ ಸಹಾಯ ಬೇಕು", "press_busy": "ಸಹಾಯ ಕರೆಯಲಾಗುತ್ತಿದೆ…", "press_once": "ಒಮ್ಮೆ ಒತ್ತಿ.", "told": "{names} ಅವರಿಗೆ ತಕ್ಷಣ ತಿಳಿಸಲಾಗುವುದು.", "told_none": "ಇನ್ನೂ ಯಾರನ್ನೂ ಸೇರಿಸಿಲ್ಲ, ಆದ್ದರಿಂದ ಈ ಬಟನ್ ಯಾರನ್ನೂ ತಲುಪಲಾರದು.", "sent_h1": "ಸಹಾಯ ಕರೆಯಲಾಗಿದೆ", "sent_names": "{names} ಅವರಿಗೆ ತಿಳಿಸಲಾಗಿದೆ.", "waiting": "ಅವರಲ್ಲಿ ಯಾರಾದರೂ ಉತ್ತರಿಸುವವರೆಗೆ ಕಾಯುತ್ತಿದ್ದೇವೆ…", "cancel": "ರದ್ದು ಮಾಡಿ — ನಾನು ಚೆನ್ನಾಗಿದ್ದೇನೆ", "cancelling": "ರದ್ದು ಮಾಡಲಾಗುತ್ತಿದೆ…", "coming_h1": "{name} ಬರುತ್ತಿದ್ದಾರೆ", "on_way": "ದಾರಿಯಲ್ಲಿದ್ದಾರೆ.", "has_notes": "{name} ಅವರ ಬಳಿ ನಿಮ್ಮ ವೈದ್ಯಕೀಯ ಮಾಹಿತಿ ಇದೆ.", "cancelled_h1": "ರದ್ದು ಮಾಡಲಾಗಿದೆ", "cancelled_p": "ಎಲ್ಲವೂ ಸರಿಯಿದೆ ಎಂದು ಎಲ್ಲರಿಗೂ ತಿಳಿಸಲಾಗಿದೆ.", "failed_h1": "ಕಳುಹಿಸಲು ಆಗಲಿಲ್ಲ", "retry": "ಮತ್ತೆ ಪ್ರಯತ್ನಿಸಿ", "call_112": "ಅಥವಾ ಈಗಲೇ 112 ಗೆ ಕರೆ ಮಾಡಿ.", "foot": "ಕೆಲಸ ಮಾಡುತ್ತಿಲ್ಲವೇ? ಕರೆ ಮಾಡಿ", "and": " ಮತ್ತು ", "name": "ಕನ್ನಡ",
    },
    "bn": {
        "title": "আমার সাহায্য দরকার", "idle_h1": "আমার সাহায্য দরকার", "press": "আমার সাহায্য দরকার", "press_busy": "সাহায্য ডাকা হচ্ছে…", "press_once": "একবার চাপুন।", "told": "{names}-কে এখনই জানানো হবে।", "told_none": "এখনও কাউকে যোগ করা হয়নি, তাই এই বোতাম কারও কাছে পৌঁছাতে পারবে না।", "sent_h1": "সাহায্য ডাকা হয়েছে", "sent_names": "{names}-কে জানানো হয়েছে।", "waiting": "তাঁদের কারও উত্তরের অপেক্ষায় আছি…", "cancel": "বাতিল করুন — আমি ঠিক আছি", "cancelling": "বাতিল করা হচ্ছে…", "coming_h1": "{name} আসছেন", "on_way": "পথে আছেন।", "has_notes": "{name}-এর কাছে আপনার চিকিৎসার তথ্য আছে।", "cancelled_h1": "বাতিল হয়েছে", "cancelled_p": "সবাইকে জানানো হয়েছে যে সব ঠিক আছে।", "failed_h1": "পাঠানো যায়নি", "retry": "আবার চেষ্টা করুন", "call_112": "অথবা এখনই 112 নম্বরে ফোন করুন।", "foot": "কাজ করছে না? ফোন করুন", "and": " এবং ", "name": "বাংলা",
    },
    "ml": {
        "title": "എനിക്ക് സഹായം വേണം", "idle_h1": "എനിക്ക് സഹായം വേണം", "press": "എനിക്ക് സഹായം വേണം", "press_busy": "സഹായം വിളിക്കുന്നു…", "press_once": "ഒരു തവണ അമർത്തുക.", "told": "{names} എന്നിവരെ ഉടൻ അറിയിക്കും.", "told_none": "ഇതുവരെ ആരെയും ചേർത്തിട്ടില്ല, അതിനാൽ ഈ ബട്ടൺ ആരിലും എത്തില്ല.", "sent_h1": "സഹായം വിളിച്ചു", "sent_names": "{names} എന്നിവരെ അറിയിച്ചു.", "waiting": "അവരിൽ ആരെങ്കിലും മറുപടി നൽകാൻ കാത്തിരിക്കുന്നു…", "cancel": "റദ്ദാക്കുക — എനിക്ക് കുഴപ്പമില്ല", "cancelling": "റദ്ദാക്കുന്നു…", "coming_h1": "{name} വരുന്നു", "on_way": "വഴിയിലാണ്.", "has_notes": "{name}-ന്റെ കൈയിൽ നിങ്ങളുടെ മെഡിക്കൽ വിവരങ്ങളുണ്ട്.", "cancelled_h1": "റദ്ദാക്കി", "cancelled_p": "എല്ലാം ശരിയാണെന്ന് എല്ലാവരെയും അറിയിച്ചു.", "failed_h1": "അയയ്ക്കാനായില്ല", "retry": "വീണ്ടും ശ്രമിക്കുക", "call_112": "അല്ലെങ്കിൽ ഇപ്പോൾ തന്നെ 112 വിളിക്കുക.", "foot": "പ്രവർത്തിക്കുന്നില്ലേ? വിളിക്കുക", "and": " കൂടാതെ ", "name": "മലയാളം",
    },
    "pa": {
        "title": "ਮੈਨੂੰ ਮਦਦ ਚਾਹੀਦੀ ਹੈ", "idle_h1": "ਮੈਨੂੰ ਮਦਦ ਚਾਹੀਦੀ ਹੈ", "press": "ਮੈਨੂੰ ਮਦਦ ਚਾਹੀਦੀ ਹੈ", "press_busy": "ਮਦਦ ਬੁਲਾਈ ਜਾ ਰਹੀ ਹੈ…", "press_once": "ਇੱਕ ਵਾਰ ਦਬਾਓ।", "told": "{names} ਨੂੰ ਤੁਰੰਤ ਦੱਸ ਦਿੱਤਾ ਜਾਵੇਗਾ।", "told_none": "ਹਾਲੇ ਕਿਸੇ ਨੂੰ ਨਹੀਂ ਜੋੜਿਆ ਗਿਆ, ਇਸ ਲਈ ਇਹ ਬਟਨ ਕਿਸੇ ਤੱਕ ਨਹੀਂ ਪਹੁੰਚ ਸਕਦਾ।", "sent_h1": "ਮਦਦ ਬੁਲਾਈ ਗਈ ਹੈ", "sent_names": "{names} ਨੂੰ ਦੱਸ ਦਿੱਤਾ ਗਿਆ ਹੈ।", "waiting": "ਉਨ੍ਹਾਂ ਵਿੱਚੋਂ ਕਿਸੇ ਦੇ ਜਵਾਬ ਦੀ ਉਡੀਕ ਹੈ…", "cancel": "ਰੱਦ ਕਰੋ — ਮੈਂ ਠੀਕ ਹਾਂ", "cancelling": "ਰੱਦ ਕੀਤਾ ਜਾ ਰਿਹਾ ਹੈ…", "coming_h1": "{name} ਆ ਰਹੇ ਹਨ", "on_way": "ਰਾਹ ਵਿੱਚ ਹਨ।", "has_notes": "{name} ਕੋਲ ਤੁਹਾਡੀ ਮੈਡੀਕਲ ਜਾਣਕਾਰੀ ਹੈ।", "cancelled_h1": "ਰੱਦ ਕੀਤਾ ਗਿਆ", "cancelled_p": "ਸਭ ਨੂੰ ਦੱਸ ਦਿੱਤਾ ਗਿਆ ਹੈ ਕਿ ਸਭ ਠੀਕ ਹੈ।", "failed_h1": "ਭੇਜਿਆ ਨਹੀਂ ਜਾ ਸਕਿਆ", "retry": "ਫਿਰ ਕੋਸ਼ਿਸ਼ ਕਰੋ", "call_112": "ਜਾਂ ਹੁਣੇ 112 'ਤੇ ਫ਼ੋਨ ਕਰੋ।", "foot": "ਕੰਮ ਨਹੀਂ ਕਰ ਰਿਹਾ? ਫ਼ੋਨ ਕਰੋ", "and": " ਅਤੇ ", "name": "ਪੰਜਾਬੀ",
    },
    "or": {
        "title": "ମୋତେ ସାହାଯ୍ୟ ଦରକାର", "idle_h1": "ମୋତେ ସାହାଯ୍ୟ ଦରକାର", "press": "ମୋତେ ସାହାଯ୍ୟ ଦରକାର", "press_busy": "ସାହାଯ୍ୟ ଡକାଯାଉଛି…", "press_once": "ଥରେ ଦବାନ୍ତୁ।", "told": "{names} ଙ୍କୁ ତୁରନ୍ତ ଜଣାଇ ଦିଆଯିବ।", "told_none": "ଏପର୍ଯ୍ୟନ୍ତ କାହାକୁ ଯୋଡ଼ାଯାଇନାହିଁ, ତେଣୁ ଏହି ବଟନ୍ କାହା ପାଖରେ ପହଞ୍ଚିପାରିବ ନାହିଁ।", "sent_h1": "ସାହାଯ୍ୟ ଡକାଯାଇଛି", "sent_names": "{names} ଙ୍କୁ ଜଣାଇ ଦିଆଯାଇଛି।", "waiting": "ସେମାନଙ୍କ ମଧ୍ୟରୁ କାହାର ଉତ୍ତରକୁ ଅପେକ୍ଷା କରୁଛୁ…", "cancel": "ବାତିଲ କରନ୍ତୁ — ମୁଁ ଠିକ୍ ଅଛି", "cancelling": "ବାତିଲ କରାଯାଉଛି…", "coming_h1": "{name} ଆସୁଛନ୍ତି", "on_way": "ବାଟରେ ଅଛନ୍ତି।", "has_notes": "{name} ଙ୍କ ପାଖରେ ଆପଣଙ୍କ ଡାକ୍ତରୀ ତଥ୍ୟ ଅଛି।", "cancelled_h1": "ବାତିଲ ହୋଇଛି", "cancelled_p": "ସମସ୍ତଙ୍କୁ ଜଣାଇ ଦିଆଯାଇଛି ଯେ ସବୁ ଠିକ୍ ଅଛି।", "failed_h1": "ପଠାଯାଇପାରିଲା ନାହିଁ", "retry": "ପୁଣି ଚେଷ୍ଟା କରନ୍ତୁ", "call_112": "କିମ୍ବା ଏବେ 112 କୁ ଫୋନ୍ କରନ୍ତୁ।", "foot": "କାମ କରୁନାହିଁ? ଫୋନ୍ କରନ୍ତୁ", "and": " ଏବଂ ", "name": "ଓଡ଼ିଆ",
    },
}


# --- pages --------------------------------------------------------------------

STYLE = """
:root {
  --bg: #FAF9F7; --surface: #FFFFFF; --ink: #14110F; --ink-muted: #4A4442; --border: #8A827A;
  --emergency: #A4161A; --on-emergency: #FFFFFF; --emergency-active: #7F1113;
  --safe: #14532D; --safe-bg: #E4F2E8; --caution: #7A4106; --caution-bg: #FDF1E0; --over: #55504D;
  --text-xs: 16px; --text-sm: 18px; --text-base: 20px; --text-lg: 24px; --text-xl: 32px;
  --text-2xl: 40px; --text-3xl: 56px; --text-action: 44px;
  --space-3: 12px; --space-4: 16px; --space-6: 24px; --space-8: 32px; --space-12: 48px;
  --radius: 12px; --radius-btn: 20px; --target-min: 64px;
}
* { box-sizing: border-box; }
html { background: var(--bg); color: var(--ink); color-scheme: only light;
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Noto Sans",
    "Noto Sans Devanagari", "Helvetica Neue", Arial, sans-serif;
  font-size: var(--text-base); line-height: 1.6; }
body { margin: 0; padding: var(--space-6); max-width: 36rem; margin-inline: auto; }
@media (max-width: 360px) { body { padding: var(--space-4); } }
h1 { font-size: var(--text-2xl); line-height: 1.2; font-weight: 700; margin: 0 0 var(--space-6); }
h2 { font-size: var(--text-xl); line-height: 1.3; font-weight: 700; margin: 0 0 var(--space-4); }
h1, h2 { text-wrap: balance; }
#sent h1 { font-size: var(--text-xl); }
p { margin: 0 0 var(--space-4); }
.muted { color: var(--ink-muted); }
.time { font-size: var(--text-xs); color: var(--ink-muted); }
.btn { display: block; width: 100%; min-height: var(--target-min); border: 0; cursor: pointer;
  font: inherit; font-weight: 700; border-radius: var(--radius); padding: var(--space-4); }
.btn:focus-visible { outline: 4px solid var(--ink); outline-offset: 3px; }
.btn-emergency { min-height: 240px; font-size: var(--text-action); line-height: 1.15;
  letter-spacing: 0.02em; text-transform: uppercase; border-radius: var(--radius-btn);
  background: var(--emergency); color: var(--on-emergency); }
.btn-emergency:active { background: var(--emergency-active); }
.btn-emergency[disabled] { background: var(--border); cursor: not-allowed; }
.btn-secondary[disabled] { color: var(--ink-muted); cursor: not-allowed; }
@media (hover: hover) {
  .btn-emergency:hover:not([disabled]) { background: var(--emergency-active); }
  .btn-secondary:hover:not([disabled]) { border-color: var(--ink); }
}
.btn-secondary { background: var(--surface); color: var(--ink); border: 3px solid var(--border);
  font-size: var(--text-lg); margin-top: var(--space-8); }
.card { border-radius: var(--radius); padding: var(--space-6); margin-bottom: var(--space-6); }
.card-safe { background: var(--safe-bg); color: var(--safe); }
.card-caution { background: var(--caution-bg); color: var(--caution); }
.card-emergency { background: var(--surface); border: 3px solid var(--emergency); color: var(--emergency); }
.banner { font-size: var(--text-3xl); line-height: 1.1; font-weight: 700; letter-spacing: 0.02em;
  color: var(--emergency); margin: 0 0 var(--space-4); }
.lead { font-size: var(--text-lg); margin-top: calc(-1 * var(--space-4)); }
.address { font-size: var(--text-lg); line-height: 1.5; }
.btn-inline { display: inline-block; width: auto; min-width: var(--target-min); margin-top: 0; text-decoration: none;
  text-align: center; }
.btn-claim { min-height: 96px; font-size: var(--text-xl); text-transform: none; letter-spacing: 0; }
.card h1 { font-size: var(--text-xl); margin-bottom: var(--space-3); }
.card p { margin: 0; }
.card-over { background: var(--surface); border: 3px solid var(--border); color: var(--over); }
hr { border: 0; border-top: 2px solid var(--border); margin: var(--space-6) 0; }
.foot { margin-top: var(--space-12); font-size: var(--text-sm); color: var(--ink-muted); }
.foot a { color: inherit; }
a[href^="tel:"] { color: inherit; display: inline-block; min-height: 48px; line-height: 48px; padding: 0 var(--space-3);
  margin: -14px calc(-1 * var(--space-3)); font-weight: 700; text-underline-offset: 4px; }
a:focus-visible { outline: 4px solid var(--ink); outline-offset: 3px; border-radius: 6px; }
.langbar { text-align: right; margin: 0 0 var(--space-4); }
.btn-lang { min-height: 48px; padding: 0 var(--space-4); border-radius: 999px; cursor: pointer;
  background: var(--surface); color: var(--ink); border: 2px solid var(--border); font: inherit;
  font-size: var(--text-sm); font-weight: 700; }
.btn-lang:focus-visible { outline: 4px solid var(--ink); outline-offset: 3px; }
[hidden] { display: none !important; }
.sr-only { position: absolute; width: 1px; height: 1px; overflow: hidden; clip: rect(0 0 0 0); white-space: nowrap; }
"""

PAGE = Template("""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="only light"><meta name="robots" content="noindex">
<meta name="theme-color" content="#A4161A">
<link rel="manifest" href="/manifest.webmanifest">
<link rel="icon" href="/icon-192.png" type="image/png">
<link rel="apple-touch-icon" href="/icon-192.png">
<title>I need help</title>
<style>""" + STYLE + """</style>
</head>
<body>
<div class="sr-only" aria-live="assertive" id="announce"></div>
<p class="langbar"><button class="btn-lang" id="lang" lang="mr">मराठी</button></p>

<main id="idle">
  <h1 data-i18n="idle_h1">I NEED HELP</h1>
  <button class="btn btn-emergency" id="press" data-i18n="press" $disabled>I need help</button>
  <p style="margin-top: var(--space-6)"><span data-i18n="press_once">Press once.</span><br><span id="told">$told</span></p>
</main>

<main id="sent" hidden>
  <h1 data-i18n="sent_h1">Help is being called</h1>
  <p><span id="sent-names"></span><br><span class="time" id="sent-time"></span></p>
  <p class="muted" data-i18n="waiting">Waiting for one of them to answer…</p>
  <button class="btn btn-secondary" id="cancel" data-i18n="cancel">Cancel — I’m OK</button>
</main>

<main id="coming" hidden>
  <div class="card card-safe"><h1>&#10003; <span id="coming-name"></span></h1>
  <p><span data-i18n="on_way">On the way now.</span><br><span class="time" id="coming-time"></span></p>
  <p id="coming-record" hidden></p></div>
  <button class="btn btn-secondary" id="cancel2" data-i18n="cancel">Cancel — I’m OK</button>
</main>

<main id="cancelled" hidden>
  <div class="card card-caution"><h1 data-i18n="cancelled_h1">Cancelled</h1>
  <p data-i18n="cancelled_p">Everyone has been told it was a false alarm.</p></div>
  <button class="btn btn-emergency" id="again" data-i18n="press">I need help</button>
</main>

<main id="failed" hidden>
  <div class="card card-emergency"><h1>&#9888; <span data-i18n="failed_h1">Couldn’t send</span></h1></div>
  <button class="btn btn-emergency" id="retry" data-i18n="retry">Try again</button>
  <p style="margin-top: var(--space-6)"><strong data-i18n="call_112">Or call 112 now.</strong></p>
</main>

<p class="foot"><span data-i18n="foot">Not working? Call</span> <a href="tel:112">112</a></p>

<script>
(function () {
  var names = $names_json;
  var strings = $strings_json;
  var incident = null, poll = null, lang = "en", claimer = "", hasNotes = false;
  var esc = function (s) { return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;"); };
  var t = function (key) { return strings[lang][key]; };
  var joinNames = function (n) {
    return n.length < 2 ? n.join("") : n.slice(0, -1).join(", ") + t("and") + n[n.length - 1];
  };
  var fill = function (key, name, value) {
    return t(key).replace("{" + name + "}", "<strong>" + esc(value) + "</strong>");
  };
  var store = function (k, v) { try { localStorage.setItem(k, v); } catch (e) {} };
  var load = function (k) { try { return localStorage.getItem(k); } catch (e) { return null; } };
  var her = strings[load("her")] ? load("her") : "mr";
  var setLang = function (l) {
    lang = strings[l] ? l : "en";
    if (lang !== "en") { her = lang; store("her", her); }
    store("lang", lang);
    document.documentElement.lang = lang;
    document.title = t("title");
    Array.prototype.forEach.call(document.querySelectorAll("[data-i18n]"), function (e) { e.textContent = t(e.getAttribute("data-i18n")); });
    var other = lang === "en" ? her : "en", pill = document.getElementById("lang");
    pill.lang = other; pill.textContent = strings[other].name;
    document.getElementById("told").innerHTML = names.length ? fill("told", "names", joinNames(names)) : t("told_none");
    document.getElementById("sent-names").innerHTML = fill("sent_names", "names", joinNames(names));
    document.getElementById("coming-name").textContent = t("coming_h1").replace("{name}", claimer);
    document.getElementById("coming-record").innerHTML = fill("has_notes", "name", claimer);
    document.getElementById("coming-record").hidden = !hasNotes;
  };
  setLang(new URLSearchParams(location.search).get("lang") || load("lang") || "en");
  document.getElementById("lang").addEventListener("click", function () { setLang(lang === "en" ? her : "en"); });
  var say = function (text) {
    var a = document.getElementById("announce"); a.textContent = ""; setTimeout(function () { a.textContent = text; }, 50);
  };
  var show = function (id) {
    ["idle", "sent", "failed", "coming", "cancelled"].forEach(function (s) { document.getElementById(s).hidden = (s !== id); });
    if (id !== "idle") say(Array.prototype.map.call(document.querySelectorAll("#" + id + " h1, #" + id + " p:not([hidden])"),
      function (e) { return e.innerText; }).join(" ").replace(/\\s+/g, " ").trim());
  };
  var stopPoll = function () { if (poll) { clearInterval(poll); poll = null; } };
  var check = function () {
    if (!incident) return;
    fetch("/status/" + incident).then(function (r) { return r.json(); }).then(function (s) {
      if (s.status === "CLAIMED") {
        claimer = s.claimed_by_name; hasNotes = !!s.record_opened_at;
        document.getElementById("coming-name").textContent = t("coming_h1").replace("{name}", claimer);
        document.getElementById("coming-time").textContent = s.claimed_at;
        document.getElementById("coming-record").innerHTML = fill("has_notes", "name", claimer);
        document.getElementById("coming-record").hidden = !hasNotes;
        show("coming"); stopPoll();
      } else if (s.status === "CANCELLED") { show("cancelled"); stopPoll(); }
    }).catch(function () {});
  };
  var cancel = function (ev) {
    if (!incident) return;
    var btn = ev.currentTarget, label = btn.textContent;
    btn.disabled = true; btn.textContent = t("cancelling");
    var restore = function () { btn.disabled = false; btn.textContent = label; };
    fetch("/cancel", { method: "POST", headers: { "content-type": "application/json" },
      body: JSON.stringify({ incident_id: incident }) })
      .then(function (r) { return r.json(); })
      .then(function (s) { if (s.status === "CANCELLED") { show("cancelled"); stopPoll(); } else { check(); } restore(); })
      .catch(restore);
  };
  var press = function () {
    var btn = document.getElementById("press");
    btn.disabled = true; btn.textContent = t("press_busy");
    fetch("/trigger", { method: "POST" })
      .then(function (r) { if (!r.ok) throw new Error(r.status); return r.json(); })
      .then(function (data) {
        incident = data.incident_id;
        names = data.told || names;
        document.getElementById("sent-names").innerHTML = fill("sent_names", "names", joinNames(names));
        document.getElementById("sent-time").textContent =
          new Date().toLocaleTimeString("en-IN", { hour: "numeric", minute: "2-digit" });
        show("sent");
        stopPoll(); poll = setInterval(check, 3000);
      })
      .catch(function () { btn.disabled = false; btn.textContent = t("press"); show("failed"); });
  };
  document.getElementById("press").addEventListener("click", press);
  document.getElementById("retry").addEventListener("click", function () { show("idle"); press(); });
  document.getElementById("cancel").addEventListener("click", cancel);
  document.getElementById("cancel2").addEventListener("click", cancel);
  document.getElementById("again").addEventListener("click", function () {
    incident = null; document.getElementById("press").disabled = false; show("idle"); press();
  });
})();
</script>
</body>
</html>
""")

def _page(title, body):
    return ("""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><meta name="robots" content="noindex">
<meta name="color-scheme" content="only light">
<title>""" + title + """</title><style>""" + STYLE + """</style></head><body>
""" + body + """
<p class="foot">Pukaar · Ambulance: <a href="tel:112">112</a></p>
</body></html>""")


CLAIM_ACTIONABLE = Template(_page("Emergency — $name needs help", """
<p class="banner">EMERGENCY</p>
<h1>$name</h1>
<p class="lead">pressed her help button at <strong>$pressed_at</strong></p>
<p class="address"><strong>$address_line1</strong><br>$address_line2</p>
<p><a class="btn btn-secondary btn-inline" href="$maps_url" target="_blank" rel="noopener">Open in maps</a></p>
<p>You are one of <strong>$contacted_count people</strong> contacted.<br><strong>No one has gone yet.</strong></p>
<form method="post"><button class="btn btn-emergency btn-claim" type="submit">I’m going now</button></form>
<p class="muted">Can’t go? That’s alright — $others.</p>
"""))

CLAIM_YOURS = Template(_page("You’re going", """
<div class="card card-safe"><h1>&#10003; You’re going</h1>
<p>$name has been told you’re coming.<br>Everyone else contacted has been told as well.</p></div>
<p class="address"><strong>$address_line1</strong><br>$address_line2</p>
<p><a class="btn btn-secondary btn-inline" href="$maps_url" target="_blank" rel="noopener">Open in maps</a></p>
<hr>
<h2>$name’s medical notes</h2>
<p class="muted">Released because you’re going. $name is told you opened this.</p>
$record
"""))

CLAIM_TAKEN = Template(_page("$claimer is already on the way", """
<div class="card card-safe"><h1>&#10003; $claimer is already on the way</h1>
<p>They said they were going at <strong>$claimed_at</strong>.<br>Nothing more is needed.</p></div>
<p class="muted">Thank you for opening this.</p>
"""))

CLAIM_CANCELLED = Template(_page("$name cancelled this alert", """
<div class="card card-caution"><h1>&#8856; $name cancelled this alert</h1>
<p>She marked it a false alarm at <strong>$cancelled_at</strong>.<br>Nothing is needed.</p></div>
"""))

CLAIM_OVER = Template(_page("This alert is over", """
<div class="card card-over"><h1>This alert is over</h1>
<p>It ended at <strong>$ended_at</strong>. Nothing is needed.</p></div>
"""))

BAD_LINK = _page("This link isn’t valid", """
<h1>This link isn’t valid</h1>
<p>It may have been mistyped, or it belongs to an alert that has ended.</p>
<p><strong>If you think someone needs help, call 112.</strong></p>
""")

NOT_FOUND = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><meta name="color-scheme" content="only light"><title>Not found</title>
<style>""" + STYLE + """</style></head><body>
<h1>That page does not exist</h1>
<p class="foot">Not working? Call <a href="tel:112">112</a></p>
</body></html>"""
