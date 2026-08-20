"""The schedule. Mostly tests that it closes, because that is where schedules go wrong.

An amortization table is easy to write and easy to write almost correctly. The failures are
never "it returned nothing" -- they are a 361st payment for $3.31, a final balance of
-0.004, or a total interest figure a dollar off a lender's notice. Each of those looks fine
in a chart and destroys confidence in every other number the moment somebody checks.

So the invariants here are arithmetic identities, not expected values copied from a
calculator: the balance must land on exactly zero, the payments must sum to principal plus
interest, and the row count must equal the term unless extra payments shortened it.
"""

from __future__ import annotations

import re

import pytest

from analyzer.core import cost
from analyzer.core.amortization import MAX_MONTHS, schedule
from analyzer.core.profile import load_profile

# A loan close to the real thing: profile target price minus the profile down payment.
LOAN = 213_200.0
RATE = 0.0667
TERM = 360

CASES = [
    (LOAN, RATE, TERM),
    (120_000.0, 0.0, 360),  # 0% -- straight division, still has to close
    (250_000.0, 0.055, 180),  # 15-year
    (1_000.0, 0.30, 12),  # tiny and expensive, where cent rounding hurts most
    (750_000.0, 0.0725, 360),
    (50_000.0, 0.03, 24),
]


@pytest.mark.parametrize(("loan", "rate", "term"), CASES)
def test_balance_lands_on_exactly_zero(loan, rate, term):
    """Not "close to" zero. Exactly. This is the reason money is integer cents."""
    s = schedule(loan, rate, term)
    assert s.payments[-1].balance == 0.0


@pytest.mark.parametrize(("loan", "rate", "term"), CASES)
def test_payoff_takes_exactly_the_term(loan, rate, term):
    """A 30-year note has 360 payments.

    The level payment is rounded to the cent, so 360 of them do not land on zero on their
    own -- the last one absorbs the remainder. Before that fix this test failed at 361.
    """
    s = schedule(loan, rate, term)
    assert s.months_to_payoff == term
    assert len(s.payments) == term


@pytest.mark.parametrize(("loan", "rate", "term"), CASES)
def test_principal_repaid_equals_the_loan(loan, rate, term):
    s = schedule(loan, rate, term)
    assert round(sum(p.principal + p.extra for p in s.payments), 2) == round(loan, 2)
    assert round(s.total_principal, 2) == round(loan, 2)


@pytest.mark.parametrize(("loan", "rate", "term"), CASES)
def test_totals_reconcile_with_the_rows(loan, rate, term):
    """Totals are summed, not recomputed by a second formula that could drift."""
    s = schedule(loan, rate, term)
    assert round(sum(p.interest for p in s.payments), 2) == round(s.total_interest, 2)
    assert round(sum(p.payment for p in s.payments), 2) == round(s.total_paid, 2)
    assert round(s.total_paid, 2) == round(s.total_principal + s.total_interest, 2)


@pytest.mark.parametrize(("loan", "rate", "term"), CASES)
def test_balance_only_ever_falls(loan, rate, term):
    """Catches negative amortization and any clamp that overshoots into a refund."""
    balances = [s_.balance for s_ in schedule(loan, rate, term).payments]
    assert all(b >= 0 for b in balances)
    assert all(later <= earlier for earlier, later in zip(balances, balances[1:]))


@pytest.mark.parametrize(("loan", "rate", "term"), CASES)
def test_each_row_is_internally_consistent(loan, rate, term):
    s = schedule(loan, rate, term)
    running = loan
    for p in s.payments:
        assert round(p.payment, 2) == round(p.interest + p.principal + p.extra, 2)
        assert round(p.balance, 2) == round(running - p.principal - p.extra, 2)
        running = p.balance


def test_scheduled_payment_matches_the_cost_engine():
    """One formula, one place.

    If this ever fails, the schedule and the PITI figure on the same screen have started
    disagreeing about the same loan, which is worse than either being wrong alone.
    """
    s = schedule(LOAN, RATE, TERM)
    # Within a cent, because the schedule rounds half up and the engine returns a float.
    assert abs(s.scheduled_payment - cost.monthly_payment(LOAN, RATE, TERM)) < 0.01


def test_interest_matches_the_closed_form_total():
    """Independent check: payment x term minus principal, with the final payment substituted."""
    s = schedule(LOAN, RATE, TERM)
    expected = s.scheduled_payment * (TERM - 1) + s.final_payment - LOAN
    assert round(s.total_interest, 2) == round(expected, 2)


def test_final_payment_absorbs_the_rounding_remainder():
    """It differs from the level payment, and that difference is the point."""
    s = schedule(LOAN, RATE, TERM)
    assert s.final_payment != s.scheduled_payment
    # A few dollars at most. If this is large, the level payment itself is wrong.
    assert abs(s.final_payment - s.scheduled_payment) < 25


def test_first_payment_is_mostly_interest():
    """The number worth showing a buyer: at 6.67% the first month barely touches principal."""
    s = schedule(LOAN, RATE, TERM)
    assert s.payments[0].interest > s.payments[0].principal * 5


def test_crossover_is_late_and_real():
    """Where principal first beats interest. On a 30-year loan this is well past halfway."""
    s = schedule(LOAN, RATE, TERM)
    assert s.crossover_month is not None
    assert s.crossover_month > TERM // 2
    before = s.payments[s.crossover_month - 2]
    at = s.payments[s.crossover_month - 1]
    assert before.principal < before.interest
    assert at.principal >= at.interest


def test_zero_rate_is_straight_division():
    s = schedule(120_000.0, 0.0, 360)
    assert s.total_interest == 0.0
    assert s.crossover_month == 1


# --- extra payments ---------------------------------------------------------------------


def test_extra_monthly_shortens_the_loan_and_saves_interest():
    base = schedule(LOAN, RATE, TERM)
    s = schedule(LOAN, RATE, TERM, extra_monthly=200)
    assert s.months_to_payoff < base.months_to_payoff
    assert s.months_saved == base.months_to_payoff - s.months_to_payoff
    assert round(s.interest_saved, 2) == round(base.total_interest - s.total_interest, 2)
    assert s.interest_saved > 0


def test_extra_payments_still_repay_exactly_the_loan():
    """The clamp on the last row must not overshoot. An overshoot is a refund that
    never happened, and it silently inflates interest_saved."""
    for extra in (50, 200, 1_000, 5_000, 50_000):
        s = schedule(LOAN, RATE, TERM, extra_monthly=extra)
        assert s.payments[-1].balance == 0.0
        assert round(sum(p.principal + p.extra for p in s.payments), 2) == LOAN
        assert s.payments[-1].payment <= s.scheduled_payment + extra + 0.01


def test_an_extra_larger_than_the_loan_ends_it_in_one_month():
    s = schedule(10_000.0, RATE, TERM, extra_monthly=500_000)
    assert s.months_to_payoff == 1
    assert s.payments[0].extra + s.payments[0].principal == 10_000.0


def test_no_extra_means_no_savings_reported():
    """Zero, not a fabricated comparison against a baseline that is the same schedule."""
    s = schedule(LOAN, RATE, TERM)
    assert s.months_saved == 0
    assert s.interest_saved == 0.0


# --- rollup and payload -----------------------------------------------------------------


def test_annual_rollup_covers_every_payment():
    s = schedule(LOAN, RATE, TERM)
    assert len(s.years) == 30
    assert sum(y.payments for y in s.years) == TERM
    assert round(sum(y.interest for y in s.years), 2) == round(s.total_interest, 2)
    assert s.years[-1].ending_balance == 0.0


def test_a_partial_final_year_is_reported_as_partial():
    """Extra payments rarely land on a year boundary, and the last year must say so
    rather than quietly claiming twelve payments."""
    s = schedule(LOAN, RATE, TERM, extra_monthly=200)
    assert sum(y.payments for y in s.years) == s.months_to_payoff
    assert all(y.payments == 12 for y in s.years[:-1])
    expected_last = s.months_to_payoff % 12 or 12
    assert s.years[-1].payments == expected_last
    assert 1 <= s.years[-1].payments <= 12


def test_payload_can_omit_the_360_rows():
    s = schedule(LOAN, RATE, TERM)
    assert "payments" in s.to_dict()
    assert "payments" not in s.to_dict(include_payments=False)
    assert len(s.to_dict(include_payments=False)["years"]) == 30


def test_payload_states_what_it_excludes():
    """The schedule is P&I. Every figure in it is smaller than the PITI figure elsewhere
    in the tool, so the payload has to say so where a consumer will see it."""
    excludes = schedule(LOAN, RATE, TERM).to_dict()["excludes"]
    assert "property tax" in excludes
    assert "homeowners insurance" in excludes
    assert "HOA dues" in excludes
    # Mortgage insurance is the one a reader is most likely to assume is in here,
    # and the tool does not model it at all yet. Saying so is the whole point.
    assert "mortgage insurance" in excludes
    # Trimming this list without updating KNOWN_LIMITATIONS is how a caveat quietly dies.
    assert len(excludes) == 4


# --- bad input --------------------------------------------------------------------------


def test_a_percentage_rate_is_rejected_not_amortized():
    """`6.67` instead of `0.0667` would otherwise produce a confident schedule at 667%."""
    with pytest.raises(ValueError, match="looks like a percentage"):
        schedule(LOAN, 6.67, TERM)


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"loan_amount": 0.0}, "loan_amount"),
        ({"loan_amount": -1.0}, "loan_amount"),
        ({"term_months": 0}, "term_months"),
        ({"term_months": -12}, "term_months"),
        ({"annual_rate": -0.01}, "annual_rate"),
        ({"extra_monthly": -50}, "extra_monthly"),
    ],
)
def test_bad_input_raises(kwargs, match):
    args = {"loan_amount": LOAN, "annual_rate": RATE, "term_months": TERM}
    args.update(kwargs)
    with pytest.raises(ValueError, match=match):
        schedule(**args)


def test_the_loop_cannot_run_forever():
    """A term past the guard has to fail fast with a readable message, not hang.

    There is no realistic mortgage here -- the point is that the loop has a stop, because
    an unbounded `while balance > 0` in a web request is an outage, not a bug.
    """
    with pytest.raises(ValueError, match="did not amortize"):
        schedule(1_000_000.0, 0.0, MAX_MONTHS + 12)


def test_profile_defaults_produce_a_closing_schedule():
    """The real loan, from the real profile. Guards against a profile change that makes
    the tool's own numbers un-amortizable."""
    p = load_profile()
    loan = p.target_price - p.down_payment
    s = schedule(loan, p.mortgage_rate, p.loan_term_months)
    assert s.months_to_payoff == p.loan_term_months
    assert s.payments[-1].balance == 0.0
    assert s.total_interest > 0


# =============================================================================
# The doors. The core is tested above; these assert only the translation layer.
# =============================================================================


class TestRenderer:
    """The terminal view. Worth testing because it deliberately shows only some rows,
    and a filter that drops the wrong ones hides the shape of the curve."""

    def test_only_milestone_years_are_printed(self, capsys):
        from analyzer.cli import render_amortization

        render_amortization(schedule(LOAN, RATE, TERM).to_dict(include_payments=False))
        out = capsys.readouterr().out
        # Strip ANSI before parsing. The colour codes are what broke the first version of
        # this assertion, and a test that cannot read its own subject is worse than none.
        plain = re.sub(r"\x1b\[[0-9;]*m", "", out)
        years = [
            int(m.group(1))
            for m in (re.match(r"\s+(\d+)\s+\$", line) for line in plain.splitlines())
            if m
        ]
        # First, last, and every fifth. Not all thirty -- a 30-row wall in a terminal is
        # worse than no table, because nobody reads it.
        assert years == [1, 5, 10, 15, 20, 25, 30]

    def test_the_last_year_is_always_shown_even_when_partial(self, capsys):
        """Extra payments end the loan mid-year, and the final row is the payoff. Dropping
        it because 21 is not divisible by 5 would hide the answer."""
        from analyzer.cli import render_amortization

        s = schedule(LOAN, RATE, TERM, extra_monthly=200)
        assert len(s.years) % 5, "pick an extra that does not land on a milestone year"
        render_amortization(s.to_dict(include_payments=False))
        plain = re.sub(r"\x1b\[[0-9;]*m", "", capsys.readouterr().out)
        years = [
            int(m.group(1))
            for m in (re.match(r"\s+(\d+)\s+\$", line) for line in plain.splitlines())
            if m
        ]
        assert years[-1] == len(s.years)
        # And it is flagged as short, so nobody reads a 2-payment year as a full one.
        assert f"({s.years[-1].payments} payments)" in plain

    def test_a_one_payment_final_year_is_singular(self, capsys):
        """`1 pmts` is the kind of thing a reader notices before the arithmetic."""
        from analyzer.cli import render_amortization

        s = schedule(179_000.0, RATE, TERM, extra_monthly=200)
        assert s.years[-1].payments == 1, "fixture no longer exercises the singular branch"
        render_amortization(s.to_dict(include_payments=False))
        out = capsys.readouterr().out
        assert "(1 payment)" in out
        assert "(1 payments)" not in out

    def test_savings_are_only_claimed_when_there_are_extra_payments(self, capsys):
        from analyzer.cli import render_amortization

        render_amortization(schedule(LOAN, RATE, TERM).to_dict(include_payments=False))
        assert "saves" not in capsys.readouterr().out

        render_amortization(
            schedule(LOAN, RATE, TERM, extra_monthly=200).to_dict(include_payments=False)
        )
        assert "saves" in capsys.readouterr().out

    def test_it_says_what_it_excludes(self, capsys):
        """On screen, not just in the payload. The PITI figure is printed a few inches
        above this and the two are different quantities."""
        from analyzer.cli import render_amortization

        render_amortization(schedule(LOAN, RATE, TERM).to_dict(include_payments=False))
        out = capsys.readouterr().out
        assert "principal and interest only" in out
        assert "mortgage insurance" in out

    def test_nothing_is_printed_without_a_schedule(self, capsys):
        from analyzer.cli import render_amortization

        render_amortization(None)
        assert capsys.readouterr().out == ""


class TestEndpoint:
    """POST /amortization. Status codes and shape only."""

    @pytest.fixture
    def client(self):
        pytest.importorskip(
            "fastapi",
            reason="FastAPI is an optional extra -- install with `pip install '.[api]'`",
        )
        from fastapi.testclient import TestClient

        from service.app import create_app

        return TestClient(create_app())

    def test_no_arguments_returns_the_profiles_own_loan(self, client):
        r = client.post("/amortization", json={})
        assert r.status_code == 200
        body = r.json()
        p = load_profile()
        assert body["amortization"]["loan_amount"] == p.target_price - p.down_payment
        assert body["assumptions"]["rate_source"] == "profile"
        assert "down payment" in body["loan_basis"]

    def test_the_360_rows_are_opt_in(self, client):
        """40 KB of JSON nobody asked for is a bad default."""
        assert "payments" not in client.post("/amortization", json={}).json()["amortization"]
        big = client.post("/amortization", json={"include_payments": True}).json()
        assert len(big["amortization"]["payments"]) == 360

    def test_rate_is_a_percentage_over_http(self, client):
        """7.5, not 0.075. Matches /max-price's dti_pct so the API has one convention."""
        body = client.post("/amortization", json={"rate_pct": 7.5}).json()
        assert body["assumptions"]["mortgage_rate"] == 0.075
        assert body["assumptions"]["rate_source"] == "request"

    def test_price_and_loan_amount_together_are_rejected(self, client):
        """Two different beliefs about the down payment, and no safe way to guess which."""
        r = client.post("/amortization", json={"price": 259_000, "loan_amount": 200_000})
        assert r.status_code == 422

    def test_a_price_under_the_down_payment_is_a_422_not_a_500(self, client):
        r = client.post("/amortization", json={"price": 1_000})
        assert r.status_code == 422
        assert "nothing to amortize" in r.json()["detail"]

    def test_unknown_fields_are_rejected(self, client):
        assert client.post("/amortization", json={"rate": 6.67}).status_code == 422

    @pytest.mark.parametrize(
        "payload",
        [
            {"extra_monthly": -1},
            {"price": 0},
            {"price": -5},
            {"rate_pct": 0},
            {"rate_pct": 200},
            {"term_months": 0},
            {"term_months": 9_999},
        ],
    )
    def test_bad_input_is_rejected_at_the_edge(self, client, payload):
        assert client.post("/amortization", json=payload).status_code == 422

    def test_the_response_says_it_is_not_piti(self, client):
        body = client.post("/amortization", json={}).json()
        assert "mortgage insurance" in body["amortization"]["excludes"]
        assert "PITI" in body["note"]

    def test_extra_payments_report_what_they_bought(self, client):
        body = client.post("/amortization", json={"extra_monthly": 250}).json()
        assert body["amortization"]["months_saved"] > 0
        assert body["amortization"]["interest_saved"] > 0

    def test_engine_version_is_stamped(self, client):
        from analyzer.core.analyze import ENGINE_VERSION

        assert client.post("/amortization", json={}).json()["engine_version"] == ENGINE_VERSION
