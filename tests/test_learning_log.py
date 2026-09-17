"""Every learning-log entry must point at a real commit inside the event window."""

import re
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

IST = timezone(timedelta(hours=5, minutes=30))
LOG = Path(__file__).resolve().parent.parent / "LEARNING-LOG.md"

# The window: from the repo's first commit (made at kickoff) to the published deadline.
DEADLINE = datetime(2026, 9, 20, 20, 0, tzinfo=IST)

HEADING = re.compile(r"^## (\d{4}-\d{2}-\d{2} \d{2}:\d{2}) IST — ", re.M)
FIX = re.compile(r"^Fix:\s+([0-9a-f]{7,40})\b", re.M)
FIELDS = ("Tried:", "Broke:", "Wrong assumption:", "Fix:", "Evidence:")


def git(*args):
    return subprocess.check_output(["git", *args], cwd=LOG.parent, text=True).strip()


def first_commit_time():
    return datetime.fromisoformat(git("log", "--reverse", "--format=%cI", "--max-count=1"))


def entries():
    text = LOG.read_text()
    starts = [m.start() for m in HEADING.finditer(text)]
    return [text[s:e] for s, e in zip(starts, starts[1:] + [len(text)])]


def test_log_exists_with_template():
    assert LOG.exists()
    assert "Wrong assumption:" in LOG.read_text()


def test_every_entry_has_every_field():
    for entry in entries():
        for field in FIELDS:
            assert field in entry, f"missing {field!r} in entry:\n{entry[:120]}"


def test_every_timestamp_is_inside_the_window():
    start = first_commit_time()
    for entry in entries():
        stamp = HEADING.match(entry).group(1)
        when = datetime.strptime(stamp, "%Y-%m-%d %H:%M").replace(tzinfo=IST)
        assert start <= when <= DEADLINE, f"{stamp} is outside {start:%c} .. {DEADLINE:%c}"


def test_every_fix_hash_resolves_to_a_commit():
    for entry in entries():
        m = FIX.search(entry)
        assert m, f"entry has no fix hash:\n{entry[:120]}"
        assert git("cat-file", "-t", m.group(1)) == "commit"
