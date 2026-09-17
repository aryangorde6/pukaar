"""The score must change who is paged first, or it is not a feature.

Seed shape: Meena is 8 m away and has never answered a 2 pm page; Vaishali is 40 m
away and always has; Ravi is the son, 4 km away, and always answers; Anil, Sunil and
Prakash have no history. Proximity alone would page Meena first.
"""

import os
import sys
from pathlib import Path

os.environ.setdefault("AWS_DEFAULT_REGION", "ap-south-1")
os.environ.setdefault("CONTACTS_TABLE", "x")
os.environ.setdefault("RESPONSE_STATS_TABLE", "x")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lambdas"))

from ranking import bucket_for, score  # noqa: E402

BUCKET = "14#weekday"


def contact(cid, m, home):
    return {"contact_id": cid, "proximity_m": m, "home_during_day": home}


def rows(bucket, sent, answered, latency_ms):
    return [{"bucket": bucket, "pages_sent": sent, "responses": answered, "total_latency_ms": latency_ms}]


CIRCLE = {
    "vaishali": (contact("vaishali", 40, True), rows(BUCKET, 5, 5, 210_000)),
    "anil": (contact("anil", 60, True), []),
    "ravi": (contact("ravi", 4200, False), rows(BUCKET, 4, 4, 180_000)),
    "meena": (contact("meena", 8, False), rows(BUCKET, 6, 0, 0)),
    "sunil": (contact("sunil", 120, True), []),
    "prakash": (contact("prakash", 2600, False), []),
}


def top3(bucket):
    scored = {cid: score(c, r, bucket)[0] for cid, (c, r) in CIRCLE.items()}
    return sorted(scored, key=lambda cid: (-scored[cid], CIRCLE[cid][0]["proximity_m"]))[:3]


def test_membership_differs_from_proximity():
    by_distance = sorted(CIRCLE, key=lambda cid: CIRCLE[cid][0]["proximity_m"])[:3]
    assert "meena" in by_distance
    first = top3(BUCKET)
    assert "meena" not in first
    assert set(first) == {"vaishali", "ravi", "anil"}


def test_known_beats_unknown_beats_known_bad():
    good, _ = score(*CIRCLE["vaishali"], BUCKET)
    unknown, _ = score(*CIRCLE["anil"], BUCKET)
    bad, _ = score(*CIRCLE["meena"], BUCKET)
    assert good > unknown > bad


def test_other_hour_uses_every_hour():
    _, basis = score(*CIRCLE["vaishali"], "21#weekend")
    assert basis == "5/5 answered"
    _, basis = score(*CIRCLE["anil"], "21#weekend")
    assert basis == "no history; usually home"
    assert top3("21#weekend") == top3(BUCKET)


def test_paged_never_answered_gets_no_speed_credit():
    s, basis = score(*CIRCLE["meena"], BUCKET)
    assert basis == "0/6 answered, 0/6 this hour"
    assert s == round(0.6 * 0.5 / 14 + 0.2 / (1 + 8 / 100), 3)


def test_one_unanswered_page_does_not_erase_history():
    # The live find on 17 Sep: a test run at 17:56 paged Vaishali once; with this-hour
    # evidence used alone, 0/1 that hour outranked 5/5 ever, and two strangers to the
    # history were paged before her.
    vaishali, history = CIRCLE["vaishali"]
    ignored_once = history + rows("17#weekday", 1, 0, 0)
    dropped, basis = score(vaishali, ignored_once, "17#weekday")
    assert basis == "5/6 answered, 0/1 this hour"
    unknown, _ = score(*CIRCLE["sunil"], "17#weekday")
    assert dropped > unknown


def test_bucket_is_ist_hour_and_day_type():
    assert bucket_for(1789634100) == "14#weekday"  # Thu 17 Sep 2026 14:05 IST
    assert bucket_for(1789832100) == "21#weekend"  # Sat 19 Sep 2026 21:05 IST
