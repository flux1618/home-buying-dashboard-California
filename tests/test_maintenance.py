"""Maintenance reserve. The key property is that the three methods stay separate."""

from __future__ import annotations

import pytest

from analyzer.core import maintenance


class TestPercentOfPrice:
    def test_one_percent_annually(self):
        est = maintenance.percent_of_price(300_000)
        assert est.annual == pytest.approx(3_000.0)
        assert est.monthly == pytest.approx(250.0)


class TestPerSqft:
    def test_one_dollar_per_sqft(self):
        est = maintenance.per_sqft(1800)
        assert est is not None
        assert est.annual == pytest.approx(1_800.0)

    @pytest.mark.parametrize("sqft", [None, 0])
    def test_missing_sqft_yields_no_estimate(self, sqft):
        assert maintenance.per_sqft(sqft) is None


class TestAgeScaled:
    @pytest.mark.parametrize(
        "age,rate", [(0, 0.010), (9, 0.010), (10, 0.015), (29, 0.015), (30, 0.020), (60, 0.020)]
    )
    def test_bands(self, age, rate):
        assert maintenance.age_scaled_rate(age) == rate

    def test_new_build_uses_one_percent(self):
        est = maintenance.age_scaled(300_000, 2020, 2026)
        assert est is not None and est.annual == pytest.approx(3_000.0)

    def test_forty_year_old_uses_two_percent(self):
        est = maintenance.age_scaled(300_000, 1985, 2026)
        assert est is not None and est.annual == pytest.approx(6_000.0)

    def test_missing_year_built_yields_no_estimate(self):
        assert maintenance.age_scaled(300_000, None, 2026) is None

    def test_future_year_built_clamps_to_age_zero(self):
        est = maintenance.age_scaled(300_000, 2030, 2026)
        assert est is not None and est.annual == pytest.approx(3_000.0)


class TestAllMethods:
    def test_returns_three_when_all_inputs_present(self):
        assert len(maintenance.all_methods(300_000, 1800, 2015, 2026)) == 3

    def test_degrades_to_one_when_inputs_missing(self):
        assert len(maintenance.all_methods(300_000, None, None, 2026)) == 1

    def test_methods_are_never_averaged(self):
        """Three distinct numbers must survive to the output."""
        block = maintenance.reserve_block(300_000, 1800, 1985, 2026)
        monthlies = {m["monthly"] for m in block["methods"].values()}
        assert len(monthlies) == 3
        assert block["monthly_low"] == min(monthlies)
        assert block["monthly_high"] == max(monthlies)

    def test_all_estimates_are_labelled_estimated(self):
        block = maintenance.reserve_block(300_000, 1800, 1985, 2026)
        assert all(m["confidence"] == "estimated" for m in block["methods"].values())

    def test_older_home_widens_the_range(self):
        new = maintenance.reserve_block(300_000, 1800, 2022, 2026)
        old = maintenance.reserve_block(300_000, 1800, 1970, 2026)
        assert old["monthly_high"] > new["monthly_high"]
