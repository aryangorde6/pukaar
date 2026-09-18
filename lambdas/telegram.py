"""One Telegram message to one person, from the bot she set up. Best-effort: the
email is the page of record; this is the same page on the channel that vibrates.

Nothing is sent unless the stack has a bot token and the contact has started the bot
(a Telegram bot cannot write to someone first). Returns the message id, or None with
the reason logged; it never raises, so a blocked bot cannot fail an iteration.
"""

import json
import os
import urllib.error
import urllib.request

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")


def send(chat_id, text, button=None):
    """button = (label, url) becomes one inline button under the message."""
    if not TOKEN or not chat_id:
        return None
    body = {"chat_id": chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
    if button:
        body["reply_markup"] = {"inline_keyboard": [[{"text": button[0], "url": button[1]}]]}
    req = urllib.request.Request(f"https://api.telegram.org/bot{TOKEN}/sendMessage",
                                 data=json.dumps(body).encode(), headers={"content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return str(json.load(r)["result"]["message_id"])
    except urllib.error.HTTPError as e:
        reason = e.read().decode(errors="replace")[:200]
    except (urllib.error.URLError, TimeoutError, OSError, KeyError, ValueError) as e:
        reason = repr(e)[:200]
    print(json.dumps({"component": "telegram", "event": "send_failed", "chat_id": str(chat_id), "detail": reason}))
    return None
