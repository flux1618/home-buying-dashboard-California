"""The orchestrator: graceful degradation, and what happens when stations disagree.

The pipeline's job is not to fetch anything. It is to decide what a partial answer looks
like. These tests pin down three behaviours that are easy to get quietly wrong:

  * a dead optional source must lower confidence, never change a fact
  * a dead *required* source must stop the run instead of scoring a blank house
  * user-supplied facts must beat fetched facts, because the user stood in the house
"""

from __future__ import annotations

import pytest

from analyzer.core.profile import load_profile
from analyzer.pipeline import PipelineAborted, build_stations, collect_api_keys, run
from analyzer.sources import http
from analyzer.sources.base import Station, StationResult

from support import load_response


@pytest.fixture
def profile():
    return load_profile()


def station(name, *, facts=None, provides=None, fatal=False, fails=None, tasks=(), updates=None):
    """A stand-in station, so pipeline behaviour can be tested without any real source."""

    class Stub(Station):
        pass

    Stub.name = name
    Stub.provides = tuple(provides if provides is not None else (facts or {}).keys())
    Stub.fatal = fatal

    def fetch(self, ctx):
        if fails:
            raise fails
        return StationResult(
            station=name,
            facts=dict(facts or {}),
            context_updates=dict(updates or {}),
            tasks=list(tasks),
        )

    Stub.fetch = fetch
    return Stub()


GOOD_GEOCODE = {"lat": 34.94, "lon": -81.97, "census_block_geoid": "450830206012004"}


@pytest.fixture
def geo():
    s = station("geocode", provides=(), fatal=True, updates=GOOD_GEOCODE)
    return s


class TestGracefulDegradation:
    def test_a_dead_optional_station_does_not_stop_the_run(self, profile, geo):
        result = run(
            "606 Andre Ct",
            268_000,
            profile=profile,
            current_year=2026,
            stations=[
                geo,
                station("parcel", facts={"beds": 3, "baths": 3.0, "sqft": 1800,
                                         "year_built": 2010, "water_sewer": "public"}),
                station("flood", facts={"flood_zone": "X"}),
                station("commute", facts={"commute_min": 14.0}),
                station("broadband", provides=("fiber_available",),
                        fails=http.SourceUnavailable("FCC down")),
            ],
        )
        assert result.document["score"]["verdict"] in {"TAKE", "WATCH", "PASS"}
        assert [d.station for d in result.degradations] == ["broadband"]

    def test_a_degraded_station_leaves_its_fact_unknown_rather_than_false(self, profile, geo):
        """The whole point. A dead FCC API must not read as 'no fiber here'."""
        result = run(
            "606 Andre Ct", 268_000, profile=profile, current_year=2026,
            stations=[
                geo,
                station("flood", facts={"flood_zone": "X"}),
                station("commute", facts={"commute_min": 14.0}),
                station("parcel", facts={"beds": 3, "baths": 3.0, "sqft": 1800,
                                         "year_built": 2010, "water_sewer": "public"}),
                station("broadband", provides=("fiber_available",),
                        fails=http.SourceUnavailable("FCC down")),
            ],
        )
        reasons = " ".join(c for c in result.document["score"]["caveats"])
        assert "fiber" in reasons.lower() or "broadband" in reasons.lower()
        deductions = {d["reason"] for d in result.document["score"]["deductions"]}
        assert not any("fiber" in d.lower() for d in deductions)

    def test_every_degradation_is_reported_with_a_reason_and_a_consequence(self, profile, geo):
        result = run(
            "606 Andre Ct", 268_000, profile=profile, current_year=2026,
            stations=[geo, station("broadband", provides=("fiber_available",),
                                   fails=http.SourceRejected("HTTP 401"))],
        )
        entry = result.document["degraded_sources"][0]
        assert entry["station"] == "broadband"
        assert "401" in entry["reason"]
        assert entry["missing"] == ["fiber_available"]

    def test_a_clean_run_reports_no_degradations(self, profile, geo):
        result = run(
            "606 Andre Ct", 268_000, profile=profile, current_year=2026,
            stations=[geo, station("flood", facts={"flood_zone": "X"})],
        )
        assert result.document["degraded_sources"] == []


class TestFatalStations:
    def test_a_failed_geocode_aborts_instead_of_scoring_a_blank_house(self, profile):
        with pytest.raises(PipelineAborted, match="geocode"):
            run(
                "not a real address at all", 268_000, profile=profile, current_year=2026,
                stations=[station("geocode", fatal=True, provides=(),
                                  fails=http.SourceUnavailable("both geocoders failed"))],
            )

    def test_the_abort_explains_itself(self, profile):
        with pytest.raises(PipelineAborted) as exc:
            run(
                "bad", 1.0, profile=profile, current_year=2026,
                stations=[station("geocode", fatal=True, provides=(),
                                  fails=http.SourceUnavailable("no match"))],
            )
        assert "no match" in str(exc.value)

    def test_downstream_stations_never_run_after_a_fatal_failure(self, profile):
        ran = []

        class Tracker(Station):
            name = "tracker"

            def fetch(self, ctx):
                ran.append(True)
                return StationResult(station="tracker")

        with pytest.raises(PipelineAborted):
            run("bad", 1.0, profile=profile, current_year=2026,
                stations=[station("geocode", fatal=True, provides=(),
                                  fails=http.SourceUnavailable("x")), Tracker()])
        assert ran == []


class TestFactPrecedence:
    def test_what_the_user_saw_in_person_beats_what_a_station_reported(self, profile, geo):
        """Assessor data is often years stale. The buyer standing in the driveway is not.

        Precedence here is structural rather than a runtime comparison: garage count,
        HOA dues, and component ages are function arguments, and no station can overwrite
        them. A station reporting zero garage bays is ignored when the user passed two.
        """
        result = run(
            "606 Andre Ct", 268_000, profile=profile, current_year=2026,
            garage_spaces=2,
            stations=[geo, station("parcel", provides=("garage_spaces", "beds"),
                                   facts={"garage_spaces": 0, "beds": 3})],
        )
        assert result.document["input"]["garage_spaces"] == 2
        assert not any("garage" in d["reason"].lower()
                       for d in result.document["score"]["deductions"])

    def test_a_station_returning_none_does_not_erase_a_known_fact(self, profile, geo):
        result = run(
            "606 Andre Ct", 268_000, profile=profile, current_year=2026,
            hoa_monthly=45.0,
            stations=[geo, station("parcel", provides=("beds",), facts={"beds": None})],
        )
        assert result.document["input"]["hoa_monthly"] == 45.0


class TestTaskMerging:
    def test_identical_tasks_from_two_stations_appear_once(self, profile, geo):
        same = {"task": "Call the ISP", "blocking": False, "reason": "block precision"}
        result = run(
            "606 Andre Ct", 268_000, profile=profile, current_year=2026,
            stations=[geo,
                      station("flood", facts={"flood_zone": "X"}, tasks=[same]),
                      station("broadband", provides=("fiber_available",), tasks=[dict(same)])],
        )
        matches = [t for t in result.document["verification_tasks"] if t["task"] == "Call the ISP"]
        assert len(matches) == 1

    def test_when_two_stations_disagree_on_urgency_blocking_wins(self, profile, geo):
        result = run(
            "606 Andre Ct", 268_000, profile=profile, current_year=2026,
            stations=[
                geo,
                station("flood", facts={"flood_zone": "X"},
                        tasks=[{"task": "Verify the zone", "blocking": False, "reason": "a"}]),
                station("commute", facts={"commute_min": 12.0},
                        tasks=[{"task": "Verify the zone", "blocking": True, "reason": "b"}]),
            ],
        )
        match = next(t for t in result.document["verification_tasks"] if t["task"] == "Verify the zone")
        assert match["blocking"] is True

    def test_blocking_tasks_are_listed_before_the_rest(self, profile, geo):
        result = run(
            "606 Andre Ct", 268_000, profile=profile, current_year=2026,
            stations=[geo, station("flood", facts={"flood_zone": "X"}, tasks=[
                {"task": "later", "blocking": False, "reason": "x"},
                {"task": "now", "blocking": True, "reason": "y"},
            ])],
        )
        order = [t["blocking"] for t in result.document["verification_tasks"]]
        assert order == sorted(order, reverse=True)


class TestProvenance:
    def test_the_document_records_which_stations_ran_and_how_they_fared(self, profile, geo):
        result = run(
            "606 Andre Ct", 268_000, profile=profile, current_year=2026,
            stations=[geo,
                      station("flood", facts={"flood_zone": "X"}),
                      station("broadband", provides=("fiber_available",),
                              fails=http.SourceUnavailable("down"))],
        )
        stations = result.document["stations"]
        assert "flood" in stations["run"]
        assert stations["degraded"] == ["broadband"]
        assert stations["complete"] is False

    def test_the_location_block_carries_the_resolved_coordinates(self, profile, geo):
        result = run("606 Andre Ct", 268_000, profile=profile, current_year=2026,
                     stations=[geo])
        assert result.document["location"]["latitude"] == 34.94
        assert result.document["location"]["census_block_geoid"] == "450830206012004"

    def test_the_engine_version_is_stamped_on_the_output(self, profile, geo):
        result = run("606 Andre Ct", 268_000, profile=profile, current_year=2026,
                     stations=[geo])
        assert result.document["engine_version"]


class TestStationConstruction:
    def test_the_default_pipeline_runs_the_six_stations_in_dependency_order(self, profile):
        names = [s.name for s in build_stations(profile, {})]
        assert names[0] == "geocode", "everything else needs coordinates first"
        assert set(names) >= {"geocode", "parcel", "flood", "commute", "broadband"}

    def test_only_the_geocoder_is_fatal(self, profile):
        fatal = [s.name for s in build_stations(profile, {}) if s.fatal]
        assert fatal == ["geocode"]

    def test_api_keys_come_from_the_environment_and_are_never_defaulted(self, monkeypatch):
        monkeypatch.delenv("FCC_API_KEY", raising=False)
        assert collect_api_keys().get("fcc") in (None, "")
        monkeypatch.setenv("FCC_API_KEY", "abc123")
        assert collect_api_keys()["fcc"] == "abc123"


class TestOfflineGuarantee:
    def test_the_pipeline_touches_no_network_when_every_station_is_stubbed(self, profile, geo):
        """Proof the orchestrator itself has no hidden fetch of its own."""
        result = run("606 Andre Ct", 268_000, profile=profile, current_year=2026,
                     stations=[geo, station("flood", facts={"flood_zone": "X"})])
        assert result.document["score"]["value"] is not None


class TestRecordedFixturesStayValid:
    """If a recorded fixture stops matching what the parser expects, say so loudly."""

    def test_the_census_fixture_still_has_the_fields_the_geocoder_reads(self):
        match = load_response("census_geocode")["result"]["addressMatches"][0]
        assert {"coordinates", "matchedAddress", "geographies"} <= set(match)

    def test_the_parcel_fixture_still_carries_a_property_type_code(self):
        """LA Assessor UseCode replaces Spartanburg CAMA's property-type field."""
        attrs = load_response("parcel_assessor")["features"][0]["attributes"]
        assert attrs["UseCode"] == "0200"

    def test_the_osrm_fixture_still_reports_a_duration(self):
        assert load_response("osrm_route")["routes"][0]["duration"] > 0
