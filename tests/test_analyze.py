"""End-to-end assembly, plus the graceful-degradation behaviour."""

from __future__ import annotations

import json

import pytest

from analyzer.core.analyze import analyze
from analyzer.core.profile import load_profile
from analyzer.core.provenance import Degradation
from analyzer.core.scoring import PropertyFacts


@pytest.fixture
def profile():
    return load_profile()


@pytest.fixture
def facts():
    return PropertyFacts(
        price=312_000, sqft=1780, beds=3, baths=3, garage_spaces=2, hoa_monthly=0.0,
        year_built=2016, roof_age_years=6, hvac_age_years=6, flood_zone="X",
        water_sewer="public", commute_min=16.4, fiber_available=True,
    )


class TestDocumentShape:
    def test_has_every_required_section(self, facts, profile):
        doc = analyze(facts, profile, 2026, address="412 Maple Ridge Dr")
        for key in (
            "engine_version", "analyzed_at", "profile", "input", "tax",
            "maintenance_reserve", "capital_expenses", "cost", "commute", "score",
            "verification_tasks", "degraded_sources",
        ):
            assert key in doc, f"missing section: {key}"

    def test_is_json_serialisable(self, facts, profile):
        json.dumps(analyze(facts, profile, 2026))

    def test_echoes_the_input_for_auditability(self, facts, profile):
        doc = analyze(facts, profile, 2026, address="412 Maple Ridge Dr")
        assert doc["input"]["address"] == "412 Maple Ridge Dr"
        assert doc["input"]["price"] == 312_000

    def test_names_the_profile_used(self, facts, profile):
        assert analyze(facts, profile, 2026)["profile"] == profile.name

    def test_commute_block_names_the_anchor(self, facts, profile):
        commute = analyze(facts, profile, 2026)["commute"]
        assert commute["anchor"] == "Keck Hospital of USC"
        assert commute["arrival_window"] == "weekday 06:30-07:00"


class TestDeterminism:
    def test_same_inputs_produce_the_same_analysis(self, facts, profile):
        a = analyze(facts, profile, 2026)
        b = analyze(facts, profile, 2026)
        for key in ("score", "cost", "tax", "maintenance_reserve"):
            assert a[key] == b[key]


class TestVerificationTasks:
    def test_broadband_call_is_always_blocking(self, facts, profile):
        tasks = analyze(facts, profile, 2026)["verification_tasks"]
        broadband = [t for t in tasks if "ISP" in t["task"]]
        assert len(broadband) == 1
        assert broadband[0]["blocking"] is True

    def test_borderline_commute_adds_a_blocking_drive_test(self, profile):
        facts = PropertyFacts(price=300_000, commute_min=19.5, flood_zone="X",
                              water_sewer="public", sqft=1600)
        tasks = analyze(facts, profile, 2026)["verification_tasks"]
        assert any("Drive the route" in t["task"] and t["blocking"] for t in tasks)

    def test_comfortable_commute_adds_no_drive_test(self, profile):
        facts = PropertyFacts(price=300_000, commute_min=11.0, flood_zone="X",
                              water_sewer="public", sqft=1600)
        tasks = analyze(facts, profile, 2026)["verification_tasks"]
        assert not any("Drive the route" in t["task"] for t in tasks)

    def test_unevaluated_hard_fail_becomes_a_blocking_task(self, profile):
        facts = PropertyFacts(price=300_000, sqft=1600, flood_zone=None,
                              water_sewer="public", commute_min=12.0)
        doc = analyze(facts, profile, 2026)
        blocking = [t["task"] for t in doc["verification_tasks"] if t["blocking"]]
        assert any("Resolve before offer" in t for t in blocking)


class TestGracefulDegradation:
    def test_degradations_are_reported_not_swallowed(self, facts, profile):
        degradations = [Degradation("fema", "NFHL returned 503")]
        doc = analyze(facts, profile, 2026, degradations=degradations)
        assert len(doc["degraded_sources"]) == 1
        assert doc["degraded_sources"][0]["station"] == "fema"

    def test_capex_quotes_task_is_blocking_when_expensive(self, profile):
        f = PropertyFacts(price=268_000, sqft=1620, beds=3, baths=3, garage_spaces=2,
                          year_built=1978, roof_age_years=17, hvac_age_years=14,
                          flood_zone="X", water_sewer="public", commute_min=11.2,
                          fiber_available=True)
        doc = analyze(f, profile, 2026)
        quotes = [t for t in doc["verification_tasks"] if "contractor quotes" in t["task"]]
        assert len(quotes) == 1
        assert quotes[0]["blocking"] is True
        # LA bands, not the old SC ones: 1,620 sqft lands in the middle roof tier and the
        # smallest HVAC tier, so the combined overdue estimate is $16,000-$40,000.
        assert "$16,000-$40,000" in quotes[0]["task"]

    def test_overdue_component_adds_a_showing_check(self, profile):
        f = PropertyFacts(price=262_000, sqft=2100, beds=3, baths=3, garage_spaces=2,
                          year_built=1996, roof_age_years=24, hvac_age_years=21,
                          flood_zone="X", water_sewer="public", commute_min=13.5,
                          fiber_available=True)
        doc = analyze(f, profile, 2026)
        overdue = [t for t in doc["verification_tasks"] if "is overdue" in t["task"]]
        assert len(overdue) == 2
        assert all(t["blocking"] for t in overdue)

    def test_healthy_house_gets_no_capex_tasks(self, facts, profile):
        doc = analyze(facts, profile, 2026)
        assert not any("contractor quotes" in t["task"] for t in doc["verification_tasks"])
        assert doc["capital_expenses"]["items"] == []

    def test_dead_source_yields_watch_not_take(self, profile):
        """A FEMA outage must not let a flood-zone house through as a TAKE."""
        facts = PropertyFacts(price=300_000, sqft=1780, beds=3, baths=3,
                              garage_spaces=2, year_built=2016, flood_zone=None,
                              water_sewer="public", commute_min=15.0,
                              fiber_available=True)
        doc = analyze(facts, profile, 2026,
                      degradations=[Degradation("fema", "timeout")])
        assert doc["score"]["verdict"] == "WATCH"

    def test_analysis_completes_with_almost_no_data(self, profile):
        """Price alone must not crash the engine."""
        doc = analyze(PropertyFacts(price=300_000), profile, 2026)
        assert doc["score"]["verdict"] == "WATCH"
        assert doc["score"]["value"] == 50
        assert doc["score"]["score_pinned"] is True
        assert doc["cost"]["piti"] > 0
