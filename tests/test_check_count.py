"""verify.py's checks are counted in four places a reader sees before running anything.
The count drifted once (ci.yml said twelve when there were eighteen); this keeps every
mention equal to the number of check() calls."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WORDS = {17: "seventeen", 18: "eighteen", 19: "nineteen", 20: "twenty"}


def test_every_mention_of_the_check_count_matches_verify_py():
    src = (ROOT / "verify.py").read_text()
    numbers = [int(n) for n in re.findall(r'^check\("(\d+) ', src, re.M)]
    n = len(numbers)
    assert sorted(numbers) == list(range(1, n + 1)), "checks are numbered 1..n, each once"
    word = WORDS[n]
    assert src.startswith(f'#!/usr/bin/env python3\n"""{word.capitalize()} checks'), "verify.py docstring"
    assert f"the {word} live checks" in (ROOT / "verify.sh").read_text(), "verify.sh header"
    assert f"{word} live checks" in (ROOT / ".github/workflows/ci.yml").read_text(), "ci.yml comment"
    readme = (ROOT / "README.md").read_text()
    assert f"runs {word} checks against the live stack" in readme, "README proof line"
    assert f"Last run {n}/{n}." in readme, "README last run"
    assert f"# {word} live checks" in readme, "README run-it block"
