"""Every word a person sees is a string in one of two places: her page's STRINGS (eleven
languages) and templates.py (every message sent). A language missing one key would fall
back to English mid-screen; a message kind missing a template would crash the send."""

import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for var in ("CONTACTS_TABLE", "SUBJECTS_TABLE", "INCIDENTS_TABLE", "NOTIFICATIONS_TABLE", "RESPONSE_STATS_TABLE",
            "STATE_MACHINE_ARN", "BROADCAST_FN", "SUBJECT_ID"):
    os.environ.setdefault(var, "x")
os.environ.setdefault("AWS_DEFAULT_REGION", "ap-south-1")
sys.path.insert(0, str(ROOT / "lambdas"))

import templates  # noqa: E402
import web  # noqa: E402

CTX = {"subject_name": "Sunita", "address_line1": "B-304, Shanti Sadan", "address_line2": "Dadar West, Mumbai",
       "pressed_at": "2:41 pm", "minutes_ago": 3, "contacted_count": 3, "claim_url": "https://example/claim/t",
       "leave_url": "https://example/leave/t", "claimer_name": "Ravi", "claimed_at": "2:42 pm", "cancelled_at": "2:50 pm",
       "released_name": "Ravi"}


def test_every_language_has_every_key():
    keys = set(web.STRINGS["en"])
    for code, strings in web.STRINGS.items():
        assert set(strings) == keys, f"{code}: {set(strings) ^ keys}"
        for key, text in strings.items():
            assert text.strip(), f"{code}.{key} is empty"
            # a placeholder the page fills must survive translation
            assert set(re.findall(r"\{\w+\}", text)) == set(re.findall(r"\{\w+\}", web.STRINGS["en"][key])), f"{code}.{key}"


def test_readme_counts_the_languages_and_strings():
    readme = (ROOT / "README.md").read_text()
    n = len(web.STRINGS) - 1
    words = {10: "Ten", 11: "Eleven", 12: "Twelve"}
    assert f"{words[n]} languages besides English" in readme
    per_language = len(web.STRINGS["en"]) - 1  # "name" labels the pill, it is not a string on her screen
    numbers = {23: "twenty-three", 24: "twenty-four", 25: "twenty-five", 26: "twenty-six"}
    assert f"A language is {numbers[per_language]} strings" in readme


def test_every_message_kind_renders_on_both_channels():
    kinds = set(templates.TEXT)
    assert kinds == set(templates.SUBJECT) == set(templates.HTML) == set(templates.TELEGRAM)
    for kind in kinds:
        subject, text, html = templates.render(kind, CTX)
        tg_text, button = templates.render_telegram(kind, CTX)
        assert subject and text and html and tg_text
        if button:  # a message with a link also carries the way off her list
            assert button[1] == CTX["claim_url"]
            assert CTX["leave_url"] in text and CTX["leave_url"] in html, f"{kind} has a link but no way to leave"
        else:
            assert CTX["leave_url"] not in text
        if button and kind != "checkin":  # every alert that asks someone to go offers 112; the check-in is not an emergency
            assert "112" in text and "112" in tg_text, f"{kind} asks someone to go without offering 112"
    assert templates.render("first_alert", {**CTX, "subject_name": "<b>"})[1].count("<b>") == 1  # text: as typed
    assert "&lt;b&gt;" in templates.render("first_alert", {**CTX, "subject_name": "<b>"})[2]     # html: escaped
