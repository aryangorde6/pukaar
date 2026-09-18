"""Every message the system sends. One render(kind, ctx) -> (subject, text, html) for
email; render_telegram(kind, ctx) -> (text, button) for Telegram.

Kinds: first_alert, widened, someone_going, stepped_back, false_alarm, no_one_reached, checkin.
Words a neighbour would not use never appear here: no "incident", "tier",
"escalation", "claim". People are named. Every message offers 112.
"""

from string import Template

# ctx keys used below:
#   subject_name, address_line1, address_line2, pressed_at, minutes_ago,
#   contacted_count, claim_url, claimer_name, claimed_at, cancelled_at, released_name

TEXT = {
    "first_alert": Template("""$subject_name needs help

She pressed her help button at $pressed_at, just now.

$address_line1
$address_line2

I can go now: $claim_url

You are one of $contacted_count people contacted. No one has gone yet.

If you can't go, that's alright - others were contacted too.
If you think this is serious, call 112.
"""),
    "widened": Template("""Still no one has gone

$subject_name pressed her help button at $pressed_at.
That was $minutes_ago minutes ago. No one has been able to go.

$address_line1
$address_line2

I can go now: $claim_url

You are one of $contacted_count people now contacted.
If you can't go, please call 112 for her.
"""),
    "someone_going": Template("""$claimer_name is going

$claimer_name said they're going at $claimed_at.
$subject_name has been told they're coming.

Nothing more is needed from you.

Thank you for being on her list.
"""),
    "stepped_back": Template("""$released_name can't go after all

$released_name said they were going, and now can't. No one is going to $subject_name.
She pressed her help button at $pressed_at - $minutes_ago minutes ago.

$address_line1
$address_line2

I can go now: $claim_url

The next people on her list are being contacted too.
If you think this is serious, call 112.
"""),
    "false_alarm": Template("""False alarm - $subject_name is OK

She cancelled the alert at $cancelled_at.

Nothing is needed.

Sorry for the interruption, and thank you for being on her list.
"""),
    "checkin": Template("""Not an emergency - $subject_name is fine.

Her help button keeps a list of who is likely to answer at each hour, so that when
she does press it, the right three people are called first. This is a check-in:
if you could go to her right now, tap the link. If not, do nothing - that is a
useful answer too.

I'd be reachable now: $claim_url

Nothing else is needed. Thank you for being on her list.
"""),
    "no_one_reached": Template("""URGENT - no one has gone

$subject_name pressed her help button at $pressed_at.
It has been $minutes_ago minutes. No one has been able to go.

$address_line1
$address_line2

Please call 112 for her now, or go if you can.

I can go now: $claim_url
"""),
}

SUBJECT = {
    "first_alert": Template("$subject_name needs help now — $pressed_at"),
    "widened": Template("Still no one — $subject_name needs help"),
    "someone_going": Template("$claimer_name is going to $subject_name — nothing needed"),
    "stepped_back": Template("$released_name can't go after all — $subject_name still needs help"),
    "false_alarm": Template("False alarm — $subject_name is OK"),
    "no_one_reached": Template("URGENT: no one has reached $subject_name"),
    "checkin": Template("Check-in from $subject_name's list — not an emergency"),
}

# Styled but plain: no centred container, system fonts, the palette from the
# trigger page. Inline styles because mail clients strip <style>.
_H = "font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,'Noto Sans',Arial,sans-serif;"
_P = f"{_H}font-size:20px;line-height:1.6;color:#14110F;margin:0 0 16px;"
_TITLE = f"{_H}font-size:28px;line-height:1.3;font-weight:700;color:#14110F;margin:0 0 16px;"
_ADDR = f"{_H}font-size:22px;line-height:1.5;color:#14110F;margin:0 0 24px;"
_BTN = (f"{_H}display:inline-block;background:#A4161A;color:#FFFFFF;font-size:24px;font-weight:700;"
        "text-decoration:none;padding:24px 32px;border-radius:12px;letter-spacing:0.02em;")
_MUTED = f"{_H}font-size:18px;line-height:1.5;color:#4A4442;margin:24px 0 0;"


def _button(ctx):
    return f'<p style="margin:0 0 24px;"><a href="{ctx["claim_url"]}" style="{_BTN}">I CAN GO NOW</a></p>'


def _address(ctx):
    return f'<p style="{_ADDR}"><strong>{ctx["address_line1"]}</strong><br>{ctx["address_line2"]}</p>'


HTML = {
    "first_alert": lambda c: f"""
<h1 style="{_TITLE}">{c['subject_name']} needs help</h1>
<p style="{_P}">She pressed her help button at <strong>{c['pressed_at']}</strong>, just now.</p>
{_address(c)}
{_button(c)}
<p style="{_P}">You are one of <strong>{c['contacted_count']} people</strong> contacted. <strong>No one has gone yet.</strong></p>
<p style="{_MUTED}">If you can't go, that's alright — others were contacted too.<br>If you think this is serious, call <strong>112</strong>.</p>
""",
    "widened": lambda c: f"""
<h1 style="{_TITLE}">Still no one has gone</h1>
<p style="{_P}">{c['subject_name']} pressed her help button at <strong>{c['pressed_at']}</strong>.<br>That was <strong>{c['minutes_ago']} minutes ago</strong>. No one has been able to go.</p>
{_address(c)}
{_button(c)}
<p style="{_P}">You are one of <strong>{c['contacted_count']} people</strong> now contacted.<br><strong>If you can't go, please call 112 for her.</strong></p>
""",
    "someone_going": lambda c: f"""
<h1 style="{_TITLE}">&#10003; {c['claimer_name']} is going</h1>
<p style="{_P}"><strong>{c['claimer_name']}</strong> said they're going at <strong>{c['claimed_at']}</strong>.<br>{c['subject_name']} has been told they're coming.</p>
<p style="{_P}">Nothing more is needed from you.</p>
<p style="{_MUTED}">Thank you for being on her list.</p>
""",
    "stepped_back": lambda c: f"""
<h1 style="{_TITLE}">{c['released_name']} can't go after all</h1>
<p style="{_P}"><strong>{c['released_name']}</strong> said they were going, and now can't. <strong>No one is going to {c['subject_name']}.</strong><br>She pressed her help button at <strong>{c['pressed_at']}</strong> — {c['minutes_ago']} minutes ago.</p>
{_address(c)}
{_button(c)}
<p style="{_MUTED}">The next people on her list are being contacted too.<br>If you think this is serious, call <strong>112</strong>.</p>
""",
    "false_alarm": lambda c: f"""
<h1 style="{_TITLE}">False alarm — {c['subject_name']} is OK</h1>
<p style="{_P}">She cancelled the alert at <strong>{c['cancelled_at']}</strong>.</p>
<p style="{_P}">Nothing is needed.</p>
<p style="{_MUTED}">Sorry for the interruption, and thank you for being on her list.</p>
""",
    "checkin": lambda c: f"""
<h1 style="{_TITLE}">Not an emergency — {c['subject_name']} is fine</h1>
<p style="{_P}">Her help button keeps a list of who is likely to answer at each hour, so that when she does press it, the right three people are called first.</p>
<p style="{_P}">This is a check-in. <strong>If you could go to her right now</strong>, tap the button. If not, do nothing — that is a useful answer too.</p>
<p style="margin:0 0 24px;"><a href="{c['claim_url']}" style="{_BTN}">I’D BE REACHABLE NOW</a></p>
<p style="{_MUTED}">Nothing else is needed. Thank you for being on her list.</p>
""",
    "no_one_reached": lambda c: f"""
<h1 style="{_TITLE}">URGENT — no one has gone</h1>
<p style="{_P}">{c['subject_name']} pressed her help button at <strong>{c['pressed_at']}</strong>.<br>It has been <strong>{c['minutes_ago']} minutes</strong>. No one has been able to go.</p>
{_address(c)}
<p style="{_P}"><strong>Please call 112 for her now</strong>, or go if you can.</p>
{_button(c)}
""",
}


# The same messages for Telegram: shorter, the link is a button, HTML parse mode
# (so values are escaped below). "I can go now" stays a button here too - GET never writes.
TELEGRAM = {
    "first_alert": Template("""🆘 <b>$subject_name needs help</b>
She pressed her help button at $pressed_at, just now.

<b>$address_line1</b>
$address_line2

You are one of $contacted_count people contacted. No one has gone yet.
If you can't go, that's alright — others were contacted too. If you think this is serious, call 112."""),
    "widened": Template("""<b>Still no one has gone</b>
$subject_name pressed her help button at $pressed_at. That was $minutes_ago minutes ago.

<b>$address_line1</b>
$address_line2

You are one of $contacted_count people now contacted. If you can't go, please call 112 for her."""),
    "someone_going": Template("""✓ <b>$claimer_name is going</b>
$claimer_name said they're going at $claimed_at. $subject_name has been told they're coming.
Nothing more is needed from you."""),
    "stepped_back": Template("""<b>$released_name can't go after all</b>
$released_name said they were going, and now can't. No one is going to $subject_name. She pressed her help button at $pressed_at, $minutes_ago minutes ago.

<b>$address_line1</b>
$address_line2

The next people on her list are being contacted too. If you think this is serious, call 112."""),
    "false_alarm": Template("""<b>False alarm — $subject_name is OK</b>
She cancelled the alert at $cancelled_at. Nothing is needed. Sorry for the interruption."""),
    "checkin": Template("""<b>Not an emergency — $subject_name is fine.</b>
Her help button keeps a list of who is likely to answer at each hour. This is a check-in: if you could go to her right now, tap the button. If not, do nothing — that is a useful answer too."""),
    "no_one_reached": Template("""🆘 <b>URGENT — no one has gone</b>
$subject_name pressed her help button at $pressed_at. It has been $minutes_ago minutes.

<b>$address_line1</b>
$address_line2

Please call 112 for her now, or go if you can."""),
}
TELEGRAM_BUTTON = {"first_alert": "I CAN GO NOW", "widened": "I CAN GO NOW", "no_one_reached": "I CAN GO NOW",
                   "stepped_back": "I CAN GO NOW", "checkin": "I’D BE REACHABLE NOW"}


def render_telegram(kind, ctx):
    """Returns (text, button) for one kind; button is (label, claim_url) or None."""
    text = TELEGRAM[kind].substitute({k: _esc(v) for k, v in ctx.items()})
    return text, ((TELEGRAM_BUTTON[kind], ctx["claim_url"]) if kind in TELEGRAM_BUTTON else None)


def render(kind, ctx):
    """Returns (subject, text, html) for one kind of email."""
    body = HTML[kind]({k: _esc(v) for k, v in ctx.items()})
    html = f'<!doctype html><html lang="en"><body style="margin:0;padding:24px;background:#FAF9F7;">{body}</body></html>'
    return SUBJECT[kind].substitute(ctx), TEXT[kind].substitute(ctx), html


def _esc(v):
    s = str(v)
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
