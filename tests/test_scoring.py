"""Scoring rules. The behaviour that matters most is what happens when a source fails."""

from __future__ import annotations

import dataclasses

import pytest

from analyzer.core import scoring
from analyzer.core.profile import load_profile
from analyzer.core.scoring import PropertyFacts


@pytest.fixture
def profile():
    return load_profile()


@pytest.fixture
def utilities_profile(profile):
    """The shipped profile with the water/sewer hard fail switched back on.

    The Los Angeles profile disables it, because the County Assessor roll publishes no
    utility fields and leaving it on would pin every score to 50. The engine still has to be
    able to enforce the check when a region can actually answer it, so the rule keeps its
    tests here against a profile that opts in.
    """
    return dataclasses.replace(profile, require_public_water_sewer=True)


def clean(**overrides) -> PropertyFacts:
    """A property that passes everything, so each test changes exactly one thing.

    Los Angeles numbers, not Spartanburg ones: the price sits under the $950,000 target and
    the price per square foot stays under the $750 caveat, both of which the SC figures
    would now trip. `school_proficiency_pct` has to be present and at or above the profile's
    floor for this to be genuinely clean — the profile configures school penalties, so a
    missing proficiency figure caps the verdict and a low one deducts.
    """
    base = dict(
        price=880_000,
        sqft=1780,
        beds=3,
        baths=3,
        garage_spaces=2,
        hoa_monthly=0.0,
        year_built=2016,
        roof_age_years=6,
        hvac_age_years=6,
        flood_zone="X",
        water_sewer="public",
        commute_min=16.4,
        fiber_available=True,
        school_district="Alhambra Unified School District",
        school_proficiency_pct=58.5,
        school_zone_of_choice=False,
    )
    base.update(overrides)
    return PropertyFacts(**base)


class TestBaseline:
    def test_clean_property_scores_100_and_takes(self, profile):
        r = scoring.score(clean(), profile, 2026)
        assert r.value == 100
        assert r.verdict == scoring.VERDICT_TAKE
        assert r.deductions == []
        assert r.hard_fails == []


class TestHardFails:
    @pytest.mark.parametrize("zone", ["A", "AE", "AO", "VE", "ae"])
    def test_sfha_zones_fail(self, profile, zone):
        r = scoring.score(clean(flood_zone=zone), profile, 2026)
        assert r.value == 0
        assert r.verdict == scoring.VERDICT_PASS
        assert len(r.hard_fails) == 1

    @pytest.mark.parametrize("zone", ["X", "X500", "C"])
    def test_non_sfha_zones_pass(self, profile, zone):
        assert scoring.score(clean(flood_zone=zone), profile, 2026).hard_fails == []

    @pytest.mark.parametrize("util", ["well", "septic", "well/septic", "SEPTIC"])
    def test_non_public_utilities_fail_when_the_check_is_enabled(
        self, utilities_profile, util
    ):
        r = scoring.score(clean(water_sewer=util), utilities_profile, 2026)
        assert r.value == 0 and r.verdict == scoring.VERDICT_PASS

    @pytest.mark.parametrize("util", ["public", "City", "MUNICIPAL"])
    def test_public_utilities_pass(self, utilities_profile, util):
        assert (
            scoring.score(clean(water_sewer=util), utilities_profile, 2026).hard_fails == []
        )

    @pytest.mark.parametrize("util", ["well", "septic", None])
    def test_la_profile_does_not_fail_on_utilities_at_all(self, profile, util):
        """The whole point of disabling the check: no hard fail, and no *unevaluated* one.

        The second half matters more than the first. If disabling the rule still left
        `water_sewer=None` counted as an unresolved hard fail, every Los Angeles property
        would pin to 50 and the dashboard would rank nothing — which is the failure mode the
        profile comment describes. This test is the guard on that.
        """
        r = scoring.score(clean(water_sewer=util), profile, 2026)
        assert r.hard_fails == []
        assert r.unevaluated_hard_fails == []
        assert r.score_pinned is False

    def test_commute_over_limit_fails(self, profile):
        r = scoring.score(clean(commute_min=26.3), profile, 2026)
        assert r.value == 0 and r.verdict == scoring.VERDICT_PASS

    def test_commute_exactly_at_limit_passes(self, profile):
        """20.0 is not "over 20"."""
        assert scoring.score(clean(commute_min=20.0), profile, 2026).hard_fails == []

    def test_commute_just_over_fails(self, profile):
        assert scoring.score(clean(commute_min=20.1), profile, 2026).hard_fails

    def test_hard_fail_short_circuits_deductions(self, profile):
        """Score is 0, not 0-minus-deductions. No partial credit either way."""
        r = scoring.score(clean(flood_zone="AE", sqft=900, beds=1), profile, 2026)
        assert r.value == 0
        assert r.deductions == []

    def test_multiple_hard_fails_all_reported(self, utilities_profile):
        r = scoring.score(
            clean(flood_zone="AE", water_sewer="septic", commute_min=30),
            utilities_profile,
            2026,
        )
        assert len(r.hard_fails) == 3


class TestUnevaluatedHardFails:
    """The safety property: missing data must never look like a clean pass."""

    @pytest.mark.parametrize(
        "field", ["flood_zone", "water_sewer", "commute_min"]
    )
    def test_unknown_hard_fail_input_caps_verdict_at_watch(self, utilities_profile, field):
        r = scoring.score(clean(**{field: None}), utilities_profile, 2026)
        assert r.verdict == scoring.VERDICT_WATCH
        assert len(r.unevaluated_hard_fails) == 1

    def test_unknown_pins_the_score_to_fifty(self, profile):
        """Unknown is not a failure and not a pass — it is an unresolved question.

        Pinning to 50 lands it squarely in WATCH: worth following up, not worth an
        offer, and visibly distinct from a house that earned its score.
        """
        r = scoring.score(clean(flood_zone=None), profile, 2026)
        assert r.value == 50
        assert r.score_pinned is True

    def test_pin_only_lowers_never_raises(self, profile):
        """A dead source must not flatter a genuinely weak house up to 50.

        `baths=1` rather than 2, because the Los Angeles profile lowered `min_baths` to 2 and
        a two-bath house no longer misses anything. The house has to be genuinely weak for
        this test to test anything.
        """
        r = scoring.score(
            clean(flood_zone=None, sqft=1200, beds=2, baths=1, garage_spaces=1),
            profile,
            2026,
        )
        assert r.total_deducted == 20 + 20 + 8 + 10
        assert r.value == 42
        assert r.verdict == scoring.VERDICT_PASS

    def test_fully_evaluated_property_is_not_pinned(self, profile):
        assert scoring.score(clean(), profile, 2026).score_pinned is False

    def test_unknown_is_not_recorded_as_a_hard_fail(self, profile):
        r = scoring.score(clean(water_sewer=None), profile, 2026)
        assert r.hard_fails == []

    def test_all_unknown_still_watch_never_take(self, utilities_profile):
        r = scoring.score(
            clean(flood_zone=None, water_sewer=None, commute_min=None),
            utilities_profile,
            2026,
        )
        assert r.verdict == scoring.VERDICT_WATCH
        assert len(r.unevaluated_hard_fails) == 3

    def test_a_real_hard_fail_still_wins_over_an_unknown(self, profile):
        r = scoring.score(clean(flood_zone="AE", commute_min=None), profile, 2026)
        assert r.verdict == scoring.VERDICT_PASS


class TestDeductions:
    @pytest.mark.parametrize(
        "overrides,points",
        [
            ({"hoa_monthly": 150.0}, 25),
            ({"beds": 2}, 20),
            ({"sqft": 1200}, 20),
            ({"fiber_available": False}, 15),
            ({"garage_spaces": 1}, 10),
            ({"baths": 1}, 8),
            # School rules. Alhambra Unified's 58.5% clears the 55% floor; Los Angeles
            # Unified's 41.6% district average is 13.4 points under it, inside the 15-point
            # margin; Belmont High's 21.7% is far outside it.
            ({"school_proficiency_pct": 41.6}, 12),
            ({"school_proficiency_pct": 21.7}, 25),
            ({"school_zone_of_choice": True}, 5),
        ],
    )
    def test_each_rule_deducts_its_weight(self, profile, overrides, points):
        r = scoring.score(clean(**overrides), profile, 2026)
        assert r.value == 100 - points
        assert r.total_deducted == points

    def test_hoa_at_the_ceiling_is_not_penalised(self, profile):
        """The rule is "over $100", so $100 exactly is fine."""
        assert scoring.score(clean(hoa_monthly=100.0), profile, 2026).value == 100

    def test_hoa_is_demoted_from_hard_fail_to_penalty(self, profile):
        """An expensive HOA is a cost, not a disqualification."""
        r = scoring.score(clean(hoa_monthly=400.0), profile, 2026)
        assert r.hard_fails == []
        assert r.value == 75

    def test_sqft_at_the_floor_is_not_penalised(self, profile):
        assert scoring.score(clean(sqft=1400), profile, 2026).value == 100

    def test_deductions_stack(self, profile):
        r = scoring.score(
            clean(sqft=1280, baths=1, garage_spaces=1, fiber_available=False),
            profile,
            2026,
        )
        assert r.total_deducted == 20 + 8 + 10 + 15
        assert r.value == 47

    def test_school_deductions_stack_with_everything_else(self, profile):
        """Schools are one more scored preference, not a parallel system.

        Worth asserting explicitly because the school rules were added last and live in their
        own helper: if they were accidentally scored separately or overwrote the deduction
        list, this is the test that catches it.
        """
        r = scoring.score(
            clean(sqft=1280, school_proficiency_pct=41.6, school_zone_of_choice=True),
            profile,
            2026,
        )
        assert r.total_deducted == 20 + 12 + 5
        assert r.value == 63
        rules = {d["rule"] for d in r.deductions}
        assert rules == {"sqft_under", "school_below_min", "school_zone_of_choice"}

    def test_the_two_school_bands_are_mutually_exclusive(self, profile):
        """A school far below the floor takes the heavy penalty ONLY — not both."""
        r = scoring.score(clean(school_proficiency_pct=21.7), profile, 2026)
        rules = [d["rule"] for d in r.deductions]
        assert rules == ["school_well_below_min"]
        assert r.total_deducted == 25

    def test_school_exactly_at_the_floor_is_not_penalised(self, profile):
        assert scoring.score(clean(school_proficiency_pct=55.0), profile, 2026).value == 100

    def test_unknown_school_deducts_nothing_but_caps_the_verdict(self, profile):
        """You cannot penalise a school nobody measured — but you cannot call it a TAKE.

        This is the same treatment sqft and beds get. The alternative, inventing a penalty
        for missing data, would make a house look worse than a house with a genuinely bad
        school, which is backwards.
        """
        r = scoring.score(clean(school_proficiency_pct=None), profile, 2026)
        assert not any(d["rule"].startswith("school") for d in r.deductions)
        assert "zoned school proficiency" in r.unknown_facts
        assert r.score_capped is True
        assert r.verdict != scoring.VERDICT_TAKE

    def test_a_profile_without_school_penalties_ignores_schools_entirely(self, profile):
        """No school preferences configured means no deduction and no cap.

        A household that never asked about schools should not have every property capped for
        a fact it does not care about.
        """
        indifferent = dataclasses.replace(
            profile, schools=dataclasses.replace(profile.schools, penalties={})
        )
        r = scoring.score(clean(school_proficiency_pct=None), indifferent, 2026)
        assert "zoned school proficiency" not in r.unknown_facts
        assert r.value == 100
        low = scoring.score(clean(school_proficiency_pct=12.0), indifferent, 2026)
        assert low.value == 100

    def test_zero_is_reserved_for_hard_fails(self, profile):
        """Every deduction at once totals 98, so deductions alone cannot reach 0.

        That is deliberate: a score of 0 means "disqualified", not "scored badly".
        A reader seeing 0 can trust that a hard fail fired.
        """
        r = scoring.score(
            clean(
                hoa_monthly=500.0,
                beds=1,
                sqft=600,
                fiber_available=False,
                garage_spaces=0,
                baths=1,
                school_proficiency_pct=58.5,
            ),
            profile,
            2026,
        )
        assert r.total_deducted == 98
        assert r.value == 2
        assert r.verdict == scoring.VERDICT_PASS
        assert r.hard_fails == []

    def test_score_clamps_and_never_goes_negative(self, profile):
        """Guard for future weight changes that could sum past 100."""
        inflated = dict(profile.penalties)
        inflated["sqft_under"] = 500
        object.__setattr__(profile, "penalties", inflated)
        r = scoring.score(clean(sqft=600), profile, 2026)
        assert r.value == 0

    def test_unknown_optional_field_is_not_a_deduction(self, profile):
        """Missing sqft is unknown, not "small" — no deduction is recorded for it."""
        r = scoring.score(clean(sqft=None), profile, 2026)
        assert r.total_deducted == 0
        assert not any(d["rule"] == "sqft_under" for d in r.deductions)

    def test_unknown_fiber_does_not_deduct(self, profile):
        r = scoring.score(clean(fiber_available=None), profile, 2026)
        assert r.total_deducted == 0

    def test_every_deduction_names_its_rule(self, profile):
        r = scoring.score(clean(sqft=1200, baths=2), profile, 2026)
        assert all(d["rule"] and d["reason"] for d in r.deductions)


class TestUnknownFactsCap:
    """Absence of evidence must not read as evidence of quality.

    Batch mode surfaced this: two properties with no county record at all scored a
    perfect 100 and outranked the one house we had full data on, because every
    deduction is guarded by `is not None` and none of them fired.
    """

    def test_fully_known_property_can_still_reach_take(self, profile):
        r = scoring.score(clean(), profile, 2026)
        assert r.unknown_facts == []
        assert r.score_capped is False
        assert r.value == 100
        assert r.verdict == "TAKE"

    @pytest.mark.parametrize(
        "field,label",
        [
            ("sqft", "heated square footage"),
            ("beds", "bedroom count"),
            ("baths", "bathroom count"),
        ],
    )
    def test_any_missing_physical_fact_blocks_take(self, profile, field, label):
        r = scoring.score(clean(**{field: None}), profile, 2026)
        assert r.unknown_facts == [label]
        assert r.score_capped is True
        assert r.value == profile.verdict_take_min - 1
        assert r.verdict == "WATCH"

    def test_no_facts_at_all_does_not_score_100(self, profile):
        """The exact regression: an empty county record used to look perfect."""
        r = scoring.score(clean(sqft=None, beds=None, baths=None), profile, 2026)
        assert r.value < 100
        assert r.verdict != "TAKE"
        assert len(r.unknown_facts) == 3

    def test_cap_is_one_directional(self, profile):
        """A property already below the cap keeps its own worse score."""
        r = scoring.score(clean(sqft=None, beds=1, hoa_monthly=400.0), profile, 2026)
        assert r.value < profile.verdict_take_min - 1
        assert r.score_capped is False

    def test_known_property_outranks_unknown_one(self, profile):
        """The ranking property that actually matters for a shortlist."""
        known = scoring.score(clean(baths=2), profile, 2026)
        unknown = scoring.score(clean(sqft=None, beds=None, baths=None), profile, 2026)
        assert known.value > unknown.value

    def test_cap_explains_itself_in_a_caveat(self, profile):
        r = scoring.score(clean(sqft=None), profile, 2026)
        assert any("capped" in c for c in r.caveats)

    def test_cap_is_reported_in_the_document(self, profile):
        d = scoring.score(clean(beds=None), profile, 2026).to_dict()
        assert d["score_capped"] is True
        assert d["unknown_facts"] == ["bedroom count"]


class TestVerdictBands:
    @pytest.mark.parametrize(
        "overrides,verdict",
        [
            ({}, scoring.VERDICT_TAKE),                       # 100
            ({"hoa_monthly": 150.0}, scoring.VERDICT_TAKE),   # 75, at the boundary
            ({"sqft": 1200, "beds": 2}, scoring.VERDICT_WATCH),  # 60
            ({"sqft": 1200, "beds": 2, "baths": 1}, scoring.VERDICT_WATCH),  # 52
            ({"sqft": 1200, "beds": 2, "fiber_available": False}, scoring.VERDICT_WATCH),  # 45, floor
            ({"sqft": 1200, "beds": 2, "fiber_available": False, "baths": 1}, scoring.VERDICT_PASS),  # 37
            # A house that is fine on every physical count and fails only on its school.
            # 100 - 25 = 75 is still a TAKE, which is the intended weighting: schools matter
            # enough to cost a quarter of the score and not enough to disqualify alone.
            ({"school_proficiency_pct": 21.7}, scoring.VERDICT_TAKE),  # 75, at the boundary
            # Add one physical miss to the same weak school and it drops out of TAKE.
            (
                {"school_proficiency_pct": 21.7, "sqft": 1200},
                scoring.VERDICT_WATCH,
            ),  # 55
        ],
    )
    def test_bands(self, profile, overrides, verdict):
        assert scoring.score(clean(**overrides), profile, 2026).verdict == verdict

    def test_watch_floor_is_forty_five(self, profile):
        """Lowered from 50 so a stack of soft misses still earns a showing."""
        assert profile.verdict_watch_min == 45
        r = scoring.score(
            clean(sqft=1280, baths=1, garage_spaces=1, fiber_available=False),
            profile,
            2026,
        )
        assert r.value == 47
        assert r.verdict == scoring.VERDICT_WATCH


class TestCaveats:
    def test_old_house_is_a_caveat_not_a_deduction(self, profile):
        """Explicit decision: an old house is noted, never excluded.

        The threshold moved from 2000 to 1960 in the California profile — the median house in
        the target neighbourhoods predates 1950, so a year-2000 line would caveat every
        candidate and inform nothing. 1948 is under the new line; 1978 is not.
        """
        r = scoring.score(clean(year_built=1948), profile, 2026)
        assert r.value == 100
        assert r.verdict == scoring.VERDICT_TAKE
        assert any("1948" in c for c in r.caveats)
        assert not any(
            "1978" in c for c in scoring.score(clean(year_built=1978), profile, 2026).caveats
        )

    def test_unknown_component_ages_on_an_old_house_are_flagged(self, profile):
        """Missing ages mean the capex tier could not run, so the score is optimistic."""
        r = scoring.score(
            clean(year_built=1985, roof_age_years=None, hvac_age_years=None),
            profile,
            2026,
        )
        assert any("optimistic" in c for c in r.caveats)

    def test_unknown_ages_on_a_new_house_are_not_flagged(self, profile):
        r = scoring.score(
            clean(year_built=2024, roof_age_years=None, hvac_age_years=None),
            profile,
            2026,
        )
        assert not any("optimistic" in c for c in r.caveats)

    def test_price_over_target_flags(self, profile):
        """$1,100,000 is more than 10% over the $950,000 target."""
        r = scoring.score(clean(price=1_100_000), profile, 2026)
        assert r.value == 100
        assert any("above the" in c for c in r.caveats)

    def test_price_within_ten_percent_does_not_flag(self, profile):
        """$1,000,000 is 5.3% over, inside the tolerance."""
        r = scoring.score(clean(price=1_000_000), profile, 2026)
        assert not any("above the" in c for c in r.caveats)

    def test_price_per_sqft_flags(self, profile):
        """$1,200,000 over 1,400 sqft is $857/sqft, past the $750 California ceiling."""
        r = scoring.score(clean(price=1_200_000, sqft=1400), profile, 2026)
        assert any("/sqft" in c for c in r.caveats)

    def test_unknown_broadband_produces_a_caveat(self, profile):
        r = scoring.score(clean(fiber_available=None), profile, 2026)
        assert any("Broadband unknown" in c for c in r.caveats)

    def test_caveats_are_attached_even_to_hard_fails(self, profile):
        """A rejected house should still explain itself fully."""
        r = scoring.score(clean(flood_zone="AE", year_built=1965), profile, 2026)
        assert r.value == 0
        assert r.caveats
