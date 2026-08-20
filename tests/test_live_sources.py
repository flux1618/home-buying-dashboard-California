"""Contract tests against the real endpoints. Excluded from the default run.

    pytest -m live

The offline suite proves the parsers handle the responses we recorded. It cannot tell you
that a county changed a field name last Tuesday. That is what these are for: run them
before trusting a report, and on a schedule in CI. A failure here is news about the world,
not a bug in the code, so it must never break the normal suite.
"""

from __future__ import annotations

import pytest

from analyzer.core.profile import load_profile
from analyzer.pipeline import run
from analyzer.sources import http, parcel
from analyzer.sources.base import Context
from analyzer.sources.commute import CommuteStation
from analyzer.sources.flood import FloodStation
from analyzer.sources.geocode import GeocodeStation
from analyzer.sources.risk import HAZARD_LABELS, HAZARD_SUFFIXES, RiskStation

pytestmark = pytest.mark.live

ADDRESS = "606 Andre Ct, Spartanburg, SC 29301"


@pytest.fixture(scope="module")
def located() -> Context:
    ctx = Context(address=ADDRESS, price=268_000)
    result = GeocodeStation().run(ctx)
    if not result.ok:
        pytest.skip(f"census geocoder unavailable: {result.degradation.reason}")
    for key, value in result.context_updates.items():
        setattr(ctx, key, value)
    return ctx


def test_the_census_geocoder_still_returns_a_census_block(located):
    assert located.lat and located.lon
    assert len(located.census_block_geoid) == 15
    assert located.county_fips == "45083", "Spartanburg County FIPS"


def test_fema_still_answers_a_point_query(located):
    result = FloodStation().run(located)
    if not result.ok:
        pytest.skip(f"FEMA NFHL unavailable: {result.degradation.reason}")
    assert result.facts["flood_zone"]


def test_osrm_still_routes_to_the_hospital(located):
    result = CommuteStation(load_profile().primary_anchor).run(located)
    if not result.ok:
        pytest.skip(f"OSRM unavailable: {result.degradation.reason}")
    assert 0 < result.facts["commute_min"] < 120


def test_the_parcel_mirror_still_has_the_fields_the_parser_reads(located):
    """The named fields are the ones a rename would silently break."""
    try:
        attrs = ParcelProbe()._query(parcel.MIRROR, located)
    except (http.SourceUnavailable, http.SourceRejected, LookupError) as exc:
        pytest.skip(f"parcel mirror unavailable: {exc}")

    for field in ("PropertyLo", "YearBuilt", "BedRooms", "FullBaths", "PropertyTy", "Utility1"):
        assert field in attrs, f"the mirror stopped returning {field}"
    assert attrs["PropertyTy"][0] in "46", "assessment-ratio code shape changed"


class ParcelProbe(parcel.ParcelStation):
    """Exists only so the live test can query the mirror directly, skipping the county."""


def test_the_authoritative_county_server_still_returns_current_schema():
    """A changed live field name would otherwise send every report to the 2021 fallback."""
    ctx = Context(address=ADDRESS, price=1.0, lat=34.943051, lon=-81.97665)
    try:
        attrs = parcel.ParcelStation()._query(parcel.COUNTY_PRIMARY, ctx)
    except (http.SourceUnavailable, http.SourceRejected, LookupError) as exc:
        pytest.skip(f"authoritative county GIS unavailable: {exc}")

    for field in ("MAPNUMBER", "PropertyType", "PropertyLocation", "District", "YearBuilt"):
        assert field in attrs, f"the current county layer stopped returning {field}"
    assert attrs["PropertyType"][0] in "46", "assessment-ratio code shape changed"


def test_a_full_run_produces_a_scored_report(located):
    result = run(ADDRESS, 268_000, roof_age_years=17, hvac_age_years=14)
    assert result.document["score"]["verdict"] in {"TAKE", "WATCH", "PASS"}
    assert result.document["location"]["census_block_geoid"]
    for entry in result.document["degraded_sources"]:
        assert entry["reason"], "a degradation must always explain itself"
        assert "missing" in entry


# =============================================================================
# FEMA National Risk Index
# =============================================================================

SC_HAZARDS = ("SWND", "TRND", "HAIL", "WNTW", "HWAV", "DRGT", "WFIR")


def test_the_nri_layer_still_answers_for_a_spartanburg_point(located):
    result = RiskStation(hazards=SC_HAZARDS).run(located)
    if result.degradation:
        pytest.skip(f"NRI unavailable: {result.degradation.reason}")

    profile = result.facts["hazard_profile"]
    assert profile["tract_fips"].startswith("45083"), "not a Spartanburg County tract"
    assert len(profile["tract_fips"]) == 11, "census tract FIPS should be 11 digits"


def test_percentiles_still_come_back_on_a_0_to_100_scale():
    """If FEMA ever switches these to 0-1, every threshold in the profile silently breaks.

    Nothing in the code would raise. Every hazard would simply read below every caveat
    threshold and the tool would report that everywhere in America is fine.
    """
    ctx = Context(address="Paradise, CA", price=1.0, lat=39.7596, lon=-121.6219)
    result = RiskStation(hazards=("WFIR",)).run(ctx)
    if result.degradation:
        pytest.skip(f"NRI unavailable: {result.degradation.reason}")

    wildfire = result.values["hazard_wildfire"]
    assert wildfire.is_available, "Paradise CA should have a modeled wildfire risk"
    assert 1.0 < wildfire.value <= 100.0, (
        f"wildfire percentile {wildfire.value} is not on a 0-100 scale"
    )
    # The Camp Fire tract. If this drops below 75 the data has changed meaningfully.
    assert wildfire.value > 75.0


def test_every_hazard_code_still_exists_on_the_layer():
    """A renamed or retired hazard code would 400 the whole query, not just one field.

    ArcGIS rejects the entire request if any requested outField is unknown, so one dead
    code takes the whole hazard profile down. Asking for all 18 at once is the cheapest
    way to find out which one moved.
    """
    fields = [
        f"{code}_{suffix}" for code in HAZARD_LABELS for suffix in HAZARD_SUFFIXES
    ]
    from analyzer.sources import risk

    try:
        payload = http.get_json(
            http.build_url(
                risk.NRI_QUERY,
                {
                    "geometry": "-81.97665,34.943051",
                    "geometryType": "esriGeometryPoint",
                    "inSR": 4326,
                    "spatialRel": "esriSpatialRelIntersects",
                    "outFields": ",".join(fields),
                    "returnGeometry": "false",
                    "f": "json",
                },
            )
        ).data
    except (http.SourceUnavailable, http.SourceRejected) as exc:
        pytest.skip(f"NRI unavailable: {exc}")

    assert "error" not in payload, (
        f"NRI rejected the hazard field list, so a code or suffix changed: "
        f"{payload.get('error')}"
    )
    attrs = payload["features"][0]["attributes"]
    missing = [f for f in fields if f not in attrs]
    assert not missing, f"fields no longer returned: {missing}"


def test_the_nri_data_version_is_still_the_one_the_fixtures_were_recorded_from():
    """Not a failure condition \u2014 a prompt to re-record fixtures and re-read the notes.

    FEMA reissues the index. When it does, percentiles shift for reasons that have nothing
    to do with any house, and a decision journal comparing scores across versions is
    comparing two different scales.
    """
    from analyzer.sources import risk

    ctx = Context(address="Spartanburg", price=1.0, lat=34.943051, lon=-81.97665)
    result = risk.RiskStation(hazards=()).run(ctx)
    if result.degradation:
        pytest.skip(f"NRI unavailable: {result.degradation.reason}")

    note = result.values["nri_tract_fips"].note or ""
    assert "December 2025" in note, (
        f"NRI data version changed: {note}. Re-record tests/fixtures/responses/nri_*.json "
        f"and check docs/KNOWN_LIMITATIONS.md."
    )
