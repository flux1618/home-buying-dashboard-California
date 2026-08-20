"""Near-term capital expenses.

These tests exist because of a real review finding: a house built in 1978 with a
17-year roof and 14-year HVAC scored a perfect 100/TAKE, because component age was a
caveat that deducted nothing. A five-figure bill has to move the number.

The sample house is a 1957 build here rather than the original 1978, because this fork's
profile sets the year-built floor at 1960 -- most of the housing stock near the anchor
predates it, and a 1978 house is unremarkable in Boyle Heights. The finding is unchanged:
the *age* caveat must not deduct, and the *component* bill must.
"""

from __future__ import annotations

import pytest

from analyzer.core import capex, scoring
from analyzer.core.profile import load_profile
from analyzer.core.scoring import PropertyFacts


@pytest.fixture
def profile():
    return load_profile()


def clean(**overrides) -> PropertyFacts:
    base = dict(
        price=880_000, sqft=1620, beds=3, baths=3, garage_spaces=2, hoa_monthly=0.0,
        year_built=1957, roof_age_years=6, hvac_age_years=6, flood_zone="X",
        water_sewer="public", commute_min=11.2, fiber_available=True,
        # A school figure is supplied so these tests measure capital expense alone. Leaving
        # it unknown caps the score at 74 and every assertion below would be reading a
        # school data gap rather than the roof.
        school_district="Alhambra Unified School District",
        school_proficiency_pct=58.5, school_zone_of_choice=False,
    )
    base.update(overrides)
    return PropertyFacts(**base)


class TestCostBands:
    def test_roof_scales_with_home_size(self):
        assert capex.roof_cost(1200) < capex.roof_cost(2000) < capex.roof_cost(3000)

    def test_hvac_scales_with_home_size(self):
        assert capex.hvac_cost(1500) < capex.hvac_cost(2000) < capex.hvac_cost(3000)

    def test_unknown_sqft_falls_back_to_a_countywide_range(self):
        assert capex.roof_cost(None) == (8_000, 30_000)
        assert capex.hvac_cost(None) == (9_000, 17_000)

    def test_ranges_are_ordered(self):
        for sqft in (None, 1000, 1600, 2400, 3200):
            low, high = capex.roof_cost(sqft)
            assert low < high


class TestAssessment:
    def test_healthy_components_produce_nothing(self, profile):
        found = capex.assess(6, 6, 1620, profile.capex_thresholds, profile.capex_penalties)
        assert found == []

    def test_roof_at_threshold_is_due(self, profile):
        found = capex.assess(15, 6, 1620, profile.capex_thresholds, profile.capex_penalties)
        assert len(found) == 1
        assert found[0].urgency == "due"
        assert found[0].points == 25

    def test_roof_past_overdue_escalates(self, profile):
        found = capex.assess(21, 6, 1620, profile.capex_thresholds, profile.capex_penalties)
        assert found[0].urgency == "overdue"
        assert found[0].points == 35

    def test_hvac_tiers(self, profile):
        due = capex.assess(6, 12, 1620, profile.capex_thresholds, profile.capex_penalties)
        overdue = capex.assess(6, 19, 1620, profile.capex_thresholds, profile.capex_penalties)
        assert due[0].points == 15
        assert overdue[0].points == 22

    def test_unknown_age_produces_no_expense(self, profile):
        assert capex.assess(None, None, 1620, profile.capex_thresholds, profile.capex_penalties) == []

    def test_both_components_reported_independently(self, profile):
        found = capex.assess(17, 14, 1620, profile.capex_thresholds, profile.capex_penalties)
        assert {e.component for e in found} == {"Roof replacement", "HVAC replacement"}

    def test_every_expense_cites_a_source(self, profile):
        found = capex.assess(17, 14, 1620, profile.capex_thresholds, profile.capex_penalties)
        assert all(e.source_url.startswith("https://") for e in found)


class TestMagnitude:
    def test_five_figure_is_labelled(self, profile):
        found = capex.assess(17, 6, 1620, profile.capex_thresholds, profile.capex_penalties)
        assert found[0].magnitude == "five_figure"

    def test_magnitude_appears_in_the_headline(self, profile):
        found = capex.assess(17, 6, 1620, profile.capex_thresholds, profile.capex_penalties)
        assert "five figure" in found[0].headline

    def test_block_flags_a_five_figure_item(self, profile):
        found = capex.assess(17, 14, 1620, profile.capex_thresholds, profile.capex_penalties)
        assert capex.block(found)["has_five_figure_item"] is True

    def test_empty_block_flags_nothing(self):
        block = capex.block([])
        assert block["has_five_figure_item"] is False
        assert block["needs_contractor_quotes"] is False
        assert block["total_low"] == 0


class TestTheReviewFinding:
    """The specific case that prompted this module."""

    def test_the_old_house_with_tired_systems_no_longer_scores_a_hundred(self, profile):
        r = scoring.score(clean(roof_age_years=17, hvac_age_years=14), profile, 2026)
        assert r.value == 60
        assert r.verdict == scoring.VERDICT_WATCH

    def test_deduction_is_at_least_forty_points(self, profile):
        r = scoring.score(clean(roof_age_years=17, hvac_age_years=14), profile, 2026)
        assert r.capex_deducted >= 40

    def test_the_dollar_range_is_surfaced(self, profile):
        r = scoring.score(clean(roof_age_years=17, hvac_age_years=14), profile, 2026)
        assert r.capex_low == 16_000
        assert r.capex_high == 40_000

    def test_age_alone_still_does_not_deduct(self, profile):
        """Built 1957 with young systems remains a full-score TAKE, per the rule."""
        r = scoring.score(clean(roof_age_years=3, hvac_age_years=3), profile, 2026)
        assert r.value == 100
        assert r.verdict == scoring.VERDICT_TAKE
        assert any("1957" in c for c in r.caveats)


class TestInteractionWithOtherTiers:
    def test_capex_is_separate_from_preference_deductions(self, profile):
        r = scoring.score(clean(roof_age_years=17, garage_spaces=1), profile, 2026)
        assert r.total_deducted == 10
        assert r.capex_deducted == 25
        assert r.value == 65

    def test_hard_fail_still_wins(self, profile):
        r = scoring.score(
            clean(roof_age_years=25, hvac_age_years=25, flood_zone="AE"), profile, 2026
        )
        assert r.value == 0
        assert r.capital_expenses == []

    def test_capex_can_drag_a_score_below_the_watch_floor(self, profile):
        r = scoring.score(
            clean(roof_age_years=25, hvac_age_years=25, sqft=1200, baths=2),
            profile,
            2026,
        )
        assert r.verdict == scoring.VERDICT_PASS
