"""The page's schedule against the engine's, row for row.

`app.js` has its own `amortize`. Under [ADR 0008] duplicated *rules* get compiled into
data.json, but a loop is behaviour and there is nothing to compile — so this is a second
implementation of the same arithmetic, exactly like `solveMaxPrice`, and the only thing
keeping it honest is this file.

The failure being guarded is silent. Both sides return a full, confident, internally
consistent schedule and only one is right. Nothing on the page would look broken.

So the tolerance is zero. Not "within a dollar" — every one of the 360 rows must match to
the cent, because the two implementations use the same integer-cent arithmetic on purpose
and any drift at all means one of them stopped doing that. Python rounds with Decimal
ROUND_HALF_UP; JavaScript's Math.round is half-up for positive numbers, and money here is
always positive, so they agree by construction rather than by luck. A single-cent
difference means that construction broke.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from analyzer.core.amortization import schedule
from analyzer.core.profile import load_profile

REPO = Path(__file__).resolve().parents[1]

# Extracts `amortize` and `pmt` from app.js and runs them under node. Reads the real file
# rather than a copy, so an edit to app.js is what this test sees.
HARNESS = r"""
const src = require('fs').readFileSync(APPJS,'utf8');
eval(src.match(/function amortize[\s\S]*?\nfunction pmt[\s\S]*?\n}\n/)[0]);
const out = amortize(LOAN, RATE, TERM, EXTRA);
if(out === null){ console.log('null'); } else {
  console.log(JSON.stringify({
    months: out.months_to_payoff,
    payment: out.scheduled_payment,
    total_interest: out.total_interest,
    final_payment: out.final_payment,
    crossover: out.crossover_month,
    months_saved: out.months_saved,
    interest_saved: out.interest_saved,
    rows: out.payments.map(p => [p.interest, p.principal, p.extra, p.balance]),
    years: out.years.map(y => [y.payments, y.interest, y.principal, y.ending_balance])
  }));
}
"""

CASES = [
    (213_200.0, 0.0667, 360, 0.0),
    (213_200.0, 0.0667, 360, 200.0),
    (179_000.0, 0.0667, 360, 200.0),
    (120_000.0, 0.0, 360, 0.0),  # 0% -- straight division on both sides
    (250_000.0, 0.055, 180, 0.0),  # 15-year
    (250_000.0, 0.055, 180, 750.0),  # extra large enough to cut the term hard
    (1_000.0, 0.30, 12, 0.0),  # tiny and expensive: worst case for cent rounding
    (750_000.0, 0.0725, 360, 0.0),
    (213_200.0, 0.06, 360, 0.0),  # a rate that divides cleanly, so .5 cents can occur
]


def js_amortize(loan: float, rate: float, term: int, extra: float):
    node = shutil.which("node")
    if not node:
        # A parity test that skips is a green tick that checked nothing. Locally that is the
        # right trade -- not every machine has node and the suite should still run. In CI it
        # is a false pass, so the workflow sets HBA_REQUIRE_NODE=1 and this turns into a
        # failure instead.
        if os.environ.get("HBA_REQUIRE_NODE"):
            pytest.fail("HBA_REQUIRE_NODE is set but node is not installed; page parity would have been skipped")
        pytest.skip("node not installed; page parity unchecked in this environment")
    script = (
        HARNESS.replace("APPJS", json.dumps(str(REPO / "app.js")))
        .replace("LOAN", repr(loan))
        .replace("RATE", repr(rate))
        .replace("TERM", str(term))
        .replace("EXTRA", repr(extra))
    )
    out = subprocess.run(
        [node, "-e", script], capture_output=True, text=True, timeout=60, cwd=REPO
    )
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


@pytest.mark.parametrize(("loan", "rate", "term", "extra"), CASES)
def test_every_row_matches_to_the_cent(loan, rate, term, extra):
    """360 rows, four columns each, zero tolerance."""
    js = js_amortize(loan, rate, term, extra)
    py = schedule(loan, rate, term, extra_monthly=extra)

    assert js["months"] == py.months_to_payoff
    assert len(js["rows"]) == len(py.payments)

    for i, (row, p) in enumerate(zip(js["rows"], py.payments), start=1):
        js_i, js_pr, js_ex, js_bal = row
        assert round(js_i, 2) == round(p.interest, 2), f"interest differs at payment {i}"
        assert round(js_pr, 2) == round(p.principal, 2), f"principal differs at payment {i}"
        assert round(js_ex, 2) == round(p.extra, 2), f"extra differs at payment {i}"
        assert round(js_bal, 2) == round(p.balance, 2), f"balance differs at payment {i}"


@pytest.mark.parametrize(("loan", "rate", "term", "extra"), CASES)
def test_the_headline_numbers_match(loan, rate, term, extra):
    """The four figures a person actually reads off the page."""
    js = js_amortize(loan, rate, term, extra)
    py = schedule(loan, rate, term, extra_monthly=extra)

    assert round(js["payment"], 2) == round(py.scheduled_payment, 2)
    assert round(js["total_interest"], 2) == round(py.total_interest, 2)
    assert round(js["final_payment"], 2) == round(py.final_payment, 2)
    assert js["crossover"] == py.crossover_month
    assert js["months_saved"] == py.months_saved
    assert round(js["interest_saved"], 2) == round(py.interest_saved, 2)


@pytest.mark.parametrize(("loan", "rate", "term", "extra"), CASES)
def test_the_annual_rollup_matches(loan, rate, term, extra):
    """The page draws a year table, so the rollup is its own surface for drift --
    a correct row set summed wrongly still shows the wrong table."""
    js = js_amortize(loan, rate, term, extra)
    py = schedule(loan, rate, term, extra_monthly=extra)

    assert len(js["years"]) == len(py.years)
    for i, (y, p) in enumerate(zip(js["years"], py.years), start=1):
        count, interest, principal, ending = y
        assert count == p.payments, f"payment count differs in year {i}"
        assert round(interest, 2) == round(p.interest, 2), f"interest differs in year {i}"
        assert round(principal, 2) == round(p.principal, 2), f"principal differs in year {i}"
        assert round(ending, 2) == round(p.ending_balance, 2), f"balance differs in year {i}"


def test_the_page_closes_the_loan_on_its_term_month():
    """The 361st-payment bug, guarded on the browser side too. It is the same rounding
    trap and a hand-port is exactly where it would come back."""
    for loan, rate, term, extra in CASES:
        if extra:
            continue
        js = js_amortize(loan, rate, term, extra)
        assert js["months"] == term
        assert js["rows"][-1][3] == 0


def test_the_page_gives_up_rather_than_returning_a_half_finished_schedule():
    """The 1200-month guard, checked for what it actually does.

    Originally written as a negative-amortization test, which it was not: the page derives
    the payment from the loan, so a payment too small to cover interest cannot be passed
    in. That branch in app.js is unreachable today and is kept only so an added payment
    override fails loudly instead of drifting.

    What this input really exercises is the loop bound -- a 1000-month term is longer than
    the guard, so the loop stops with a balance still owing. The requirement is that it
    returns null and the section renders an explanation, rather than handing the chart 1200
    rows that never reach zero.
    """
    js = js_amortize(1_000_000.0, 0.0, 12_000, 0.0)
    assert js is None


def test_parity_holds_for_the_real_profile_loan():
    p = load_profile()
    loan = p.target_price - p.down_payment
    js = js_amortize(loan, p.mortgage_rate, p.loan_term_months, 0.0)
    py = schedule(loan, p.mortgage_rate, p.loan_term_months)
    assert round(js["total_interest"], 2) == round(py.total_interest, 2)
    assert js["crossover"] == py.crossover_month
