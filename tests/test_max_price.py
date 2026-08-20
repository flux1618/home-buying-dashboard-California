"""The inverse solver: highest price that holds a DTI ceiling.

The property worth testing here is not "does it return a number" but "is the number the
actual boundary". A bisection that stops one iteration early still looks plausible and is
wrong by hundreds of dollars a month, so most of these tests check the edge rather than
the value: the solved price must satisfy the ceiling, and a dollar more must break it.
"""

from __future__ import annotations

import pytest

from analyzer.core import cost, tax
from analyzer.core.profile import load_profile

YEAR = 2026


@pytest.fixture
def profile():
    return load_profile()


def solve(profile, **kw):
    kw.setdefault("current_year", YEAR)
    return cost.solve_max_price(profile, **kw)


def dti_at(profile, price, sqft=None, hoa=0.0):
    return cost.compute(profile, price, sqft, None, hoa, YEAR).front_end_dti


class TestItReallyIsTheBoundary:
    """A solver is only useful if the answer is tight. These are the tests that catch
    an off-by-one-iteration bisection, which is the failure mode that looks fine."""

    def test_solution_satisfies_the_ceiling(self, profile):
        s = solve(profile, dti_ceiling=0.22, sqft=1650)
        assert dti_at(profile, s.lender_max_price, 1650) <= 0.22

    def test_a_thousand_dollars_more_breaks_it(self, profile):
        # The tolerance is $1, so $1,000 past the answer must be over the line. If this
        # passes, the solver stopped early and is leaving affordability on the table.
        s = solve(profile, dti_ceiling=0.22, sqft=1650)
        assert dti_at(profile, s.lender_max_price + 1000, 1650) > 0.22

    def test_the_boundary_is_within_the_stated_tolerance(self, profile):
        s = solve(profile, dti_ceiling=0.22, sqft=1650)
        over = s.lender_max_price + cost._SOLVER_TOLERANCE * 2
        assert dti_at(profile, over, 1650) > 0.22

    def test_a_lower_ceiling_buys_less_house(self, profile):
        tight = solve(profile, dti_ceiling=0.15, sqft=1650)
        loose = solve(profile, dti_ceiling=0.30, sqft=1650)
        assert tight.lender_max_price < loose.lender_max_price

    def test_monotonic_across_many_ceilings(self, profile):
        prices = [
            solve(profile, dti_ceiling=c / 100, sqft=1650).lender_max_price
            for c in range(10, 36, 5)
        ]
        assert prices == sorted(prices)


class TestTwoAnswersNotOne:
    """A lender underwrites PITI; the household also funds the reserve. Collapsing those
    into one number is the specific dishonesty this dataclass exists to prevent."""

    def test_household_price_is_lower_than_lender_price(self, profile):
        s = solve(profile, dti_ceiling=0.22, sqft=1650)
        assert s.household_max_price < s.lender_max_price

    def test_household_price_holds_the_ceiling_including_reserve(self, profile):
        s = solve(profile, dti_ceiling=0.22, sqft=1650)
        c = cost.compute(profile, s.household_max_price, 1650, None, 0.0, YEAR)
        assert (c.piti + c.reserve_high) / profile.monthly_income <= 0.22

    def test_lender_price_would_break_the_household_ceiling(self, profile):
        # This is the whole point. If it did not, the two numbers would be redundant and
        # the extra column would be noise.
        s = solve(profile, dti_ceiling=0.22, sqft=1650)
        c = cost.compute(profile, s.lender_max_price, 1650, None, 0.0, YEAR)
        assert (c.piti + c.reserve_high) / profile.monthly_income > 0.22

    def test_household_uses_the_conservative_edge_of_the_reserve_range(self, profile):
        """Not the average of the three methods. Averaging would hide the spread that
        `maintenance` reports on purpose, and would quietly raise the answer."""
        s = solve(profile, dti_ceiling=0.22, sqft=1650)
        c = cost.compute(profile, s.household_max_price, 1650, None, 0.0, YEAR)
        assert c.reserve_high >= c.reserve_low
        with_low = (c.piti + c.reserve_low) / profile.monthly_income
        assert with_low < 0.22  # the low method leaves room; the solver did not take it


class TestInfeasible:
    def test_fixed_costs_can_make_every_price_impossible(self, profile):
        # Taxes and insurance do not go away at a $0 loan, so a low enough ceiling has no
        # solution at any price. Returning 0 would imply "buy nothing and you are fine",
        # which is not what the arithmetic says.
        s = solve(profile, dti_ceiling=0.001, sqft=1650)
        assert s.feasible is False
        assert s.lender_max_price is None
        assert s.household_max_price is None

    def test_infeasible_says_why(self, profile):
        s = solve(profile, dti_ceiling=0.001, sqft=1650)
        joined = " ".join(s.notes).lower()
        assert "fixed costs" in joined
        assert "cheaper house does not solve" in joined

    def test_floor_dti_is_reported_so_the_caller_can_see_the_gap(self, profile):
        s = solve(profile, dti_ceiling=0.001, sqft=1650)
        assert s.floor_dti > s.dti_ceiling

    def test_a_ceiling_just_above_the_floor_is_feasible(self, profile):
        floor = solve(profile, dti_ceiling=0.001, sqft=1650).floor_dti
        s = solve(profile, dti_ceiling=floor * 1.05, sqft=1650)
        assert s.feasible is True

    @pytest.mark.parametrize("bad", [0.0, -0.1])
    def test_a_nonsense_ceiling_raises(self, profile, bad):
        with pytest.raises(ValueError, match="must be positive"):
            solve(profile, dti_ceiling=bad)


class TestWarningsThatMatter:
    def test_it_warns_when_the_solved_price_pushes_below_twenty_percent_down(self, profile):
        """Mortgage insurance is not modeled anywhere in this codebase and is real money.
        The solver raising the price is what drives the down-payment share down, so the
        warning belongs to the answer rather than to the inputs."""
        s = solve(profile, dti_ceiling=0.22, sqft=1650)
        assert profile.down_payment / s.lender_max_price < 0.20
        joined = " ".join(s.notes).lower()
        assert "mortgage insurance" in joined
        assert "upper bound" in joined

    def test_no_pmi_warning_when_the_down_payment_clears_twenty_percent(self, profile):
        # Force it by making the ceiling so tight that the fixed down payment is a large
        # share of the answer.
        s = solve(profile, dti_ceiling=0.03, sqft=1650)
        assert s.feasible
        if profile.down_payment / s.lender_max_price >= 0.20:
            assert "mortgage insurance" not in " ".join(s.notes).lower()

    def test_unknown_sqft_is_disclosed(self, profile):
        s = solve(profile, dti_ceiling=0.22, sqft=None)
        assert any("unknown-sqft" in n for n in s.notes)

    def test_known_sqft_does_not_claim_a_fallback(self, profile):
        s = solve(profile, dti_ceiling=0.22, sqft=1650)
        assert not any("unknown-sqft" in n for n in s.notes)


class TestInputsThatMoveTheAnswer:
    def test_hoa_lowers_the_max_price(self, profile):
        no_hoa = solve(profile, dti_ceiling=0.22, sqft=1650, hoa_monthly=0.0)
        with_hoa = solve(profile, dti_ceiling=0.22, sqft=1650, hoa_monthly=250.0)
        assert with_hoa.lender_max_price < no_hoa.lender_max_price

    def test_non_owner_occupied_lowers_the_max_price(self, profile):
        """The California homeowners' exemption applies only to an owner-occupied home.
        If this ever stops being true, the tax module changed and this solver silently
        started quoting the wrong number."""
        primary = solve(profile, dti_ceiling=0.22, sqft=1650, owner_occupied=True)
        rental = solve(profile, dti_ceiling=0.22, sqft=1650, owner_occupied=False)
        assert rental.lender_max_price < primary.lender_max_price

    def test_an_older_house_lowers_the_household_price_only(self, profile):
        """Build year feeds the maintenance reserve, and the reserve is excluded from the
        lender figure by design. So an old house must move one number and not the other --
        if it moves both, the reserve leaked into PITI."""
        new = solve(profile, dti_ceiling=0.22, sqft=1650, year_built=2020)
        old = solve(profile, dti_ceiling=0.22, sqft=1650, year_built=1955)
        assert new.lender_max_price == old.lender_max_price
        assert old.household_max_price <= new.household_max_price

    def test_default_ceiling_comes_from_the_profile(self, profile):
        assert solve(profile, sqft=1650).dti_ceiling == profile.target_front_end_dti

    def test_explicit_ceiling_overrides_the_profile(self, profile):
        assert solve(profile, dti_ceiling=0.31, sqft=1650).dti_ceiling == 0.31


class TestSerialization:
    def test_dict_round_trip_has_the_fields_a_client_needs(self, profile):
        d = solve(profile, dti_ceiling=0.22, sqft=1650).to_dict()
        for key in (
            "dti_ceiling",
            "feasible",
            "lender_max_price",
            "household_max_price",
            "floor_price",
            "floor_dti",
            "notes",
            "note",
        ):
            assert key in d

    def test_prices_serialize_as_whole_dollars(self, profile):
        d = solve(profile, dti_ceiling=0.22, sqft=1650).to_dict()
        assert d["lender_max_price"] == int(d["lender_max_price"])

    def test_the_note_explains_why_there_are_two_prices(self, profile):
        d = solve(profile, dti_ceiling=0.22, sqft=1650).to_dict()
        assert "not an error" in d["note"]

    def test_infeasible_serializes_nulls_not_zeros(self, profile):
        d = solve(profile, dti_ceiling=0.001, sqft=1650).to_dict()
        assert d["lender_max_price"] is None


class TestItDoesNotTouchScoring:
    """The solver reads the profile and calls `compute`. It must not be able to change a
    score, because a max-price question asked mid-shortlist would then reorder the list."""

    def test_solving_does_not_mutate_the_profile(self, profile):
        before = profile.to_dict() if hasattr(profile, "to_dict") else repr(profile)
        solve(profile, dti_ceiling=0.22, sqft=1650)
        after = profile.to_dict() if hasattr(profile, "to_dict") else repr(profile)
        assert before == after

    def test_compute_is_unchanged_after_a_solve(self, profile):
        first = cost.compute(profile, 950_000, 1650, 2004, 0.0, YEAR)
        solve(profile, dti_ceiling=0.22, sqft=1650)
        second = cost.compute(profile, 950_000, 1650, 2004, 0.0, YEAR)
        assert first == second


class TestTheTwoDoorsAgree:
    """The page solves this in JavaScript and the engine solves it in Python. They are two
    implementations of one question, which is exactly the setup that drifts.

    Comparing them is worth the awkwardness of shelling out to node, because the failure
    this catches is silent: both sides return a confident number and only one is right.
    Finding a $13,379 "max price" against a $190,000 down payment is how the page's floor
    bug was found -- Python already floored at the down payment and JavaScript did not.
    """

    HARNESS = r"""
    global.DATA = {global:{tax:{prop13_rate:TAX_RATE,
      homeowners_exemption:HOMEOWNERS_EXEMPTION},insurance:{annual:INS}}};
    const src = require('fs').readFileSync(APPJS,'utf8');
    // pitiParts calls pmt, which lives further down the file next to the schedule. Eval it
    // first or this harness dies with "pmt is not defined" -- which is how this line got
    // written: extracting the payment formula out of pitiParts so the amortization section
    // could share it broke exactly this test, on the first full run after the refactor.
    eval(src.match(/function pmt\([\s\S]*?\n}\n/)[0]);
    eval(src.match(/function pitiParts[\s\S]*?\n}\nfunction solveMaxPrice[\s\S]*?\n}\n/)[0]);
    const s = solveMaxPrice(INC, DOWN, RATE, HOA, DTI);
    console.log(JSON.stringify(s));
    """

    def js_solve(self, profile, dti_pct, hoa=0.0):
        import json
        import os
        import shutil
        import subprocess
        from pathlib import Path

        node = shutil.which("node")
        if not node:
            # A parity test that skips is a green tick that checked nothing. Locally that is the
            # right trade -- not every machine has node and the suite should still run. In CI it
            # is a false pass, so the workflow sets HBA_REQUIRE_NODE=1 and this turns into a
            # failure instead.
            if os.environ.get("HBA_REQUIRE_NODE"):
                pytest.fail("HBA_REQUIRE_NODE is set but node is not installed; page parity would have been skipped")
            pytest.skip("node not installed; page parity unchecked in this environment")
        repo = Path(__file__).resolve().parents[1]
        script = (
            self.HARNESS.replace("TAX_RATE", str(tax.get_schedule(profile.tax_rate_area).total_rate()))
            .replace("HOMEOWNERS_EXEMPTION", str(tax.HOMEOWNERS_EXEMPTION))
            .replace("INS", str(profile.annual_insurance))
            .replace("APPJS", json.dumps(str(repo / "app.js")))
            .replace("INC", str(profile.gross_annual_income))
            .replace("DOWN", str(profile.down_payment))
            .replace("RATE", str(profile.mortgage_rate))
            .replace("HOA", str(hoa))
            .replace("DTI", str(dti_pct))
        )
        out = subprocess.run(
            [node, "-e", script], capture_output=True, text=True, timeout=30, cwd=repo
        )
        assert out.returncode == 0, out.stderr
        return json.loads(out.stdout)

    @pytest.mark.parametrize("dti_pct", [15, 22, 36])
    def test_the_page_and_the_engine_land_on_the_same_price(self, profile, dti_pct):
        # Within $2, not exact. Both bisect to a $1 tolerance from different brackets, so
        # a couple of dollars of disagreement is the algorithm and not a bug. A percentage
        # tolerance would hide a real drift at these price levels.
        js = self.js_solve(profile, dti_pct)
        py = solve(profile, dti_ceiling=dti_pct / 100, sqft=1650)
        assert js["feasible"] is True
        assert abs(js["price"] - py.lender_max_price) <= 2

    def test_both_call_the_same_ceiling_infeasible(self, profile):
        js = self.js_solve(profile, 0.8)
        py = solve(profile, dti_ceiling=0.008, sqft=1650)
        assert js["feasible"] is False and py.feasible is False

    def test_the_page_never_returns_a_price_under_the_down_payment(self, profile):
        """The bug this file was extended to catch. A max price below the cash you are
        putting down is not an answer, it is an artifact of bisecting from zero."""
        js = self.js_solve(profile, 3)
        assert js["price"] >= profile.down_payment
