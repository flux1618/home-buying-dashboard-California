"""Rate sensitivity has to prove its shape, not merely print a plausible table.

The dangerous failure is a hand-written payment shortcut that looks monotonic at the target rate
but omits tax, insurance, or the DTI solver. These tests hold the sweep to the real engine and
exercise the point where a lower rate is offset by a higher price.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from analyzer.core.cost import compute
from analyzer.core.profile import load_profile
from analyzer.core.sensitivity import BaselineRate, rate_band, wait_vs_buy

YEAR = 2026
PRICE = 310_000.0


@pytest.fixture
def profile():
    return load_profile()


class TestRateBand:
    def test_default_band_is_the_inclusive_five_to_seven_point_five_grid(self, profile):
        band = rate_band(profile, PRICE, current_year=YEAR)
        assert [point.annual_rate for point in band.points] == [
            0.05,
            0.0525,
            0.055,
            0.0575,
            0.06,
            0.0625,
            0.065,
            0.0675,
            0.07,
            0.0725,
            0.075,
        ]

    def test_payment_rises_with_rate_for_the_same_house(self, profile):
        band = rate_band(profile, PRICE, current_year=YEAR)
        piti = [point.piti for point in band.points]
        principal_interest = [point.principal_interest for point in band.points]
        interest = [point.total_interest for point in band.points]
        assert piti == sorted(piti)
        assert principal_interest == sorted(principal_interest)
        assert interest == sorted(interest)
        assert len(set(piti)) == len(piti), "a rate move must not flatten into an unchanged PITI"

    def test_higher_rate_reduces_both_dti_price_answers(self, profile):
        band = rate_band(profile, PRICE, current_year=YEAR, sqft=1_650, year_built=2015)
        lender = [point.lender_max_price for point in band.points]
        household = [point.household_max_price for point in band.points]
        assert lender == sorted(lender, reverse=True)
        assert household == sorted(household, reverse=True)
        assert all(h < l for h, l in zip(household, lender))

    def test_baseline_delta_is_zero_when_the_anchor_is_a_band_point(self, profile):
        band = rate_band(
            profile,
            PRICE,
            baseline=BaselineRate(0.065, "test anchor"),
            current_year=YEAR,
        )
        anchor = next(point for point in band.points if point.annual_rate == 0.065)
        assert anchor.piti_delta_from_baseline == 0
        assert anchor.principal_interest_delta_from_baseline == 0
        assert anchor.total_interest_delta_from_baseline == 0

    def test_band_does_not_mutate_the_profile_rate(self, profile):
        before = profile.mortgage_rate
        rate_band(profile, PRICE, current_year=YEAR)
        assert profile.mortgage_rate == before


class TestWaitVsBuy:
    def test_break_even_price_reproduces_baseline_piti_to_the_cent(self, profile):
        comparison = wait_vs_buy(
            profile,
            baseline_rate=0.0675,
            baseline_price=PRICE,
            future_rate=0.0625,
            future_price=325_000,
            current_year=YEAR,
        )
        baseline = compute(
            replace(profile, mortgage_rate=comparison.baseline_rate), PRICE, None, None, 0.0, YEAR
        )
        break_even = compute(
            replace(profile, mortgage_rate=comparison.future_rate),
            comparison.break_even_future_price,
            None,
            None,
            0.0,
            YEAR,
        )
        assert round(break_even.piti, 2) == round(baseline.piti, 2)

    def test_lower_rate_can_absorb_only_a_finite_price_increase(self, profile):
        comparison = wait_vs_buy(
            profile,
            baseline_rate=0.0675,
            baseline_price=PRICE,
            future_rate=0.0625,
            future_price=340_000,
            current_year=YEAR,
        )
        assert comparison.break_even_future_price > PRICE
        assert comparison.break_even_price_change > 0
        assert comparison.monthly_payment_delta > 0, "the supplied later price is beyond break-even"

    def test_note_refuses_to_frame_the_scenarios_as_a_forecast(self, profile):
        comparison = wait_vs_buy(
            profile,
            baseline_rate=0.0675,
            baseline_price=PRICE,
            future_rate=0.065,
            future_price=PRICE,
            current_year=YEAR,
        )
        assert "not a rate or home-price forecast" in comparison.note


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"price": 0}, "positive"),
        ({"price": -1}, "positive"),
        ({"start_rate": -0.01}, "fraction"),
        ({"end_rate": 6.5}, "fraction"),  # 650%, the percentage/fraction mistake
        ({"step": 0.0}, "positive"),
        ({"start_rate": 0.05, "end_rate": 0.071, "step": 0.0025}, "divide exactly"),
        ({"hoa_monthly": -1}, "non-negative"),
        ({"current_year": 1800}, "plausible"),
    ],
)
def test_band_refuses_absurd_inputs(profile, kwargs, message):
    with pytest.raises(ValueError, match=message):
        rate_band(profile, kwargs.pop("price", PRICE), current_year=kwargs.pop("current_year", YEAR), **kwargs)


def test_wait_vs_buy_refuses_an_absurd_future_price(profile):
    with pytest.raises(ValueError, match="future_price"):
        wait_vs_buy(
            profile,
            baseline_rate=0.0675,
            baseline_price=PRICE,
            future_rate=0.0625,
            future_price=float("nan"),
            current_year=YEAR,
        )
