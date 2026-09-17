"""The README's cost figures are cost.py's output, verbatim, or the test is red."""

import io
import sys
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import cost  # noqa: E402


def test_readme_cost_lines_match_cost_py():
    out = io.StringIO()
    with redirect_stdout(out):
        cost.main()
    readme = (ROOT / "README.md").read_text()
    lines = [line for line in out.getvalue().splitlines() if line.strip()]
    assert len(lines) >= 4
    for line in lines:
        assert line in readme, f"README drifted from cost.py:\n{line}"


def test_headline_numbers_are_the_table_totals():
    readme = (ROOT / "README.md").read_text()
    for name, shape in cost.SHAPES.items():
        total = sum(cost.incident(shape).values())
        headline = f"${total:.4f}"
        assert headline in readme, f"headline {headline} for {name!r} not in README"
        assert f"₹{total * cost.INR_PER_USD:.2f}" in readme
