"""Every email the system sends. One render(kind, ctx) -> (subject, text, html).

Kinds: first_alert, widened, someone_going, false_alarm, no_one_reached.
Words a neighbour would not use never appear here: no "incident", "tier",
"escalation", "claim". People are named. Every message offers 112.
"""

from string import Template

# ctx keys used below:
#   subject_name, address_line1, address_line2, pressed_at, minutes_ago,
#   contacted_count, claim_url, claimer_name, claimed_at, cancelled_at

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
    "false_alarm": Template("""False alarm - $subject_name is OK

She cancelled the alert at $cancelled_at.

Nothing is needed.

Sorry for the interruption, and thank you for being on her list.
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
    "false_alarm": Template("False alarm — $subject_name is OK"),
    "no_one_reached": Template("URGENT: no one has reached $subject_name"),
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
    "false_alarm": lambda c: f"""
<h1 style="{_TITLE}">False alarm — {c['subject_name']} is OK</h1>
<p style="{_P}">She cancelled the alert at <strong>{c['cancelled_at']}</strong>.</p>
<p style="{_P}">Nothing is needed.</p>
<p style="{_MUTED}">Sorry for the interruption, and thank you for being on her list.</p>
""",
    "no_one_reached": lambda c: f"""
<h1 style="{_TITLE}">URGENT — no one has gone</h1>
<p style="{_P}">{c['subject_name']} pressed her help button at <strong>{c['pressed_at']}</strong>.<br>It has been <strong>{c['minutes_ago']} minutes</strong>. No one has been able to go.</p>
{_address(c)}
<p style="{_P}"><strong>Please call 112 for her now</strong>, or go if you can.</p>
{_button(c)}
""",
}


def render(kind, ctx):
    """Returns (subject, text, html) for one kind of email."""
    body = HTML[kind]({k: _esc(v) for k, v in ctx.items()})
    html = f'<!doctype html><html lang="en"><body style="margin:0;padding:24px;background:#FAF9F7;">{body}</body></html>'
    return SUBJECT[kind].substitute(ctx), TEXT[kind].substitute(ctx), html


def _esc(v):
    s = str(v)
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
