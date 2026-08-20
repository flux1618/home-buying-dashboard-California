"""The source stations, against recorded responses.

The tests worth reading here are the failure ones. Any station can parse a good response;
what determines whether this tool is trustworthy is what happens when a government server
times out, returns a shape nobody documented, or answers a question that was never asked.
"""

from __future__ import annotations

import pytest

from analyzer.core.profile import load_profile
from analyzer.sources import http
from analyzer.sources.base import Context, Station, StationResult
from analyzer.sources.broadband import BroadbandStation
from analyzer.sources.commute import CommuteStation
from analyzer.sources.flood import FloodStation, is_sfha
from analyzer.sources.geocode import GeocodeStation
from analyzer.sources.parcel import (
    ParcelStation,
    normalize_tra,
    pick_parcel,
    positive_float,
    positive_int,
    read_buildings,
    situs,
    street_number,
)

from support import load_response


@pytest.fixture
def profile():
    return load_profile()


@pytest.fixture
def located() -> Context:
    """A context as it looks after geocoding succeeded.

    2475 Verde St is a real single-family parcel in 90033, about half a mile from the Keck
    Hospital anchor, and it is the address every recorded fixture in this suite was captured
    against. Coordinates are the Census geocoder's own match, not a hand-placed pin.
    """
    return Context(
        address="2475 Verde St, Los Angeles, CA 90033",
        price=775_000,
        lat=34.057185008756,
        lon=-118.200013900972,
        census_block_geoid="060372031002008",
        county_fips="06037",
    )


# =============================================================================
# The station contract itself
# =============================================================================


class TestStationContract:
    def test_a_station_never_raises_on_an_unreachable_source(self, located):
        class Exploding(Station):
            name = "exploding"

            def fetch(self, ctx):
                raise http.SourceUnavailable("connection reset")

        result = Exploding().run(located)
        assert result.degradation is not None
        assert "unreachable" in result.degradation.reason

    def test_a_rejection_is_reported_differently_from_an_outage(self, located):
        class Refused(Station):
            name = "refused"

            def fetch(self, ctx):
                raise http.SourceRejected("HTTP 403 Forbidden")

        reason = Refused().run(located).degradation.reason
        assert "declined" in reason and "403" in reason

    def test_an_unexpected_response_shape_degrades_rather_than_crashes(self, located):
        class Confused(Station):
            name = "confused"

            def fetch(self, ctx):
                return {"totally": "different"}["features"]

        result = Confused().run(located)
        assert result.degradation is not None
        assert "KeyError" in result.degradation.reason

    def test_a_station_cannot_write_facts_it_did_not_declare(self, located):
        class Sneaky(Station):
            name = "sneaky"
            provides = ("beds",)

            def fetch(self, ctx):
                return StationResult(station="sneaky", facts={"beds": 3, "price": 1})

        with pytest.raises(AssertionError, match="undeclared facts"):
            Sneaky().run(located)

    def test_non_fatal_stations_skip_without_coordinates(self):
        empty = Context(address="somewhere", price=1.0)
        result = FloodStation().run(empty)
        assert result.degradation is not None
        assert "no coordinates" in result.degradation.reason

    def test_skipping_costs_no_request(self, fake_http):
        FloodStation().run(Context(address="x", price=1.0))
        assert fake_http.calls == []


# =============================================================================
# G — geocode
# =============================================================================


class TestGeocode:
    def test_reads_coordinates_and_census_block(self, fake_http):
        fake_http.route("geocoding.geo.census.gov", load_response("census_geocode"))
        ctx = Context(address="2475 Verde St, Los Angeles, CA 90033", price=775_000)
        result = GeocodeStation().run(ctx)

        assert result.ok
        assert result.context_updates["lat"] == pytest.approx(34.057185, abs=1e-5)
        # 06 = California, 037 = Los Angeles County.
        assert result.context_updates["census_block_geoid"].startswith("06037")
        assert result.context_updates["county_fips"] == "06037"

    def test_census_values_are_measured_and_cited(self, fake_http):
        fake_http.route("geocoding.geo.census.gov", load_response("census_geocode"))
        result = GeocodeStation().run(Context(address="606 Andre Ct", price=1.0))
        for value in result.values.values():
            assert value.confidence == "measured"
            assert value.source_url

    def test_falls_back_to_nominatim_when_census_finds_nothing(self, fake_http):
        fake_http.route("geocoding.geo.census.gov", {"result": {"addressMatches": []}})
        fake_http.route(
            "nominatim", [{"lat": "34.94", "lon": "-81.97", "display_name": "somewhere, SC"}]
        )
        result = GeocodeStation().run(Context(address="unmatched road", price=1.0))

        assert result.ok
        assert fake_http.called_with("nominatim")

    def test_the_fallback_is_labelled_an_estimate_not_a_measurement(self, fake_http):
        fake_http.route("geocoding.geo.census.gov", {"result": {"addressMatches": []}})
        fake_http.route("nominatim", [{"lat": "34.94", "lon": "-81.97", "display_name": "x"}])
        result = GeocodeStation().run(Context(address="unmatched road", price=1.0))

        assert result.values["latitude"].confidence == "estimated"
        assert "centroid" in result.values["latitude"].note
        assert any("Census geocoder did not match" in t["task"] for t in result.tasks)

    def test_both_geocoders_failing_is_fatal(self, fake_http):
        fake_http.route("geocoding.geo.census.gov", {"result": {"addressMatches": []}})
        fake_http.route("nominatim", [])
        result = GeocodeStation().run(Context(address="nowhere at all", price=1.0))
        assert result.degradation is not None

    def test_an_empty_address_is_refused_before_any_request(self, fake_http):
        result = GeocodeStation().run(Context(address="   ", price=1.0))
        assert "no address" in result.degradation.reason
        assert fake_http.calls == []


# =============================================================================
# F — flood
# =============================================================================


class TestFlood:
    def test_reads_the_zone_from_a_real_response(self, fake_http, located):
        fake_http.route("NFHL", load_response("nfhl_flood_zone"))
        result = FloodStation().run(located)

        assert result.ok
        assert result.facts["flood_zone"] == "X"
        assert result.values["in_special_flood_hazard_area"].value is False

    def test_an_sfha_zone_produces_a_blocking_task(self, fake_http, located):
        fake_http.route(
            "NFHL",
            {"features": [{"attributes": {"FLD_ZONE": "AE", "SFHA_TF": "T", "STATIC_BFE": 612.0}}]},
        )
        result = FloodStation().run(located)

        assert result.facts["flood_zone"] == "AE"
        assert result.values["in_special_flood_hazard_area"].value is True
        assert any(t["blocking"] and "mandatory" in t["task"] for t in result.tasks)

    def test_unmapped_is_unknown_not_safe(self, fake_http, located):
        """The most dangerous bug this station could have."""
        fake_http.route("NFHL", {"features": []})
        result = FloodStation().run(located)

        assert result.facts["flood_zone"] is None
        assert result.values["flood_zone"].confidence == "unavailable"
        assert "NOT that it is outside" in result.values["flood_zone"].note
        assert any(t["blocking"] for t in result.tasks)

    def test_an_arcgis_error_payload_degrades(self, fake_http, located):
        fake_http.route("NFHL", {"error": {"code": 500, "message": "backend down"}})
        result = FloodStation().run(located)
        assert result.degradation is not None
        assert "backend down" in result.degradation.reason

    @pytest.mark.parametrize(
        "zone,expected",
        [("A", True), ("AE", True), ("AO", True), ("VE", True), ("X", False),
         ("X500", False), ("D", False), ("", False), (None, False)],
    )
    def test_sfha_classification(self, zone, expected):
        assert is_sfha(zone) is expected


# =============================================================================
# C — commute
# =============================================================================


class TestCommute:
    def test_applies_the_congestion_multiplier(self, fake_http, profile, located):
        fake_http.route("router.project-osrm.org", load_response("osrm_route"))
        result = CommuteStation(profile.primary_anchor).run(located)

        assert result.ok
        free_flow = result.values["free_flow_minutes"].value
        assert result.facts["commute_min"] == pytest.approx(free_flow * 1.25, abs=0.1)

    def test_the_free_flow_number_is_measured_and_the_rush_number_is_not(
        self, fake_http, profile, located
    ):
        """The distinction the whole station exists to preserve."""
        fake_http.route("router.project-osrm.org", load_response("osrm_route"))
        result = CommuteStation(profile.primary_anchor).run(located)

        assert result.values["free_flow_minutes"].confidence == "measured"
        assert result.values["rush_hour_minutes"].confidence == "estimated"
        assert "assumption, not a measurement" in result.values["rush_hour_minutes"].note

    def test_a_route_failure_degrades(self, fake_http, profile, located):
        fake_http.route("router.project-osrm.org", {"code": "NoRoute", "routes": []})
        result = CommuteStation(profile.primary_anchor).run(located)
        assert "NoRoute" in result.degradation.reason

    def test_it_always_asks_you_to_actually_drive_it(self, fake_http, profile, located):
        fake_http.route("router.project-osrm.org", load_response("osrm_route"))
        result = CommuteStation(profile.primary_anchor).run(located)
        assert any("Drive the route" in t["task"] for t in result.tasks)


# =============================================================================
# A — parcel
# =============================================================================


class TestFieldReading:
    """Coercion helpers. The Assessor roll uses blanks, zeros and " " for "not recorded"."""

    @pytest.mark.parametrize(
        "raw,expected",
        [(1350, 1350), ("1350", 1350), (0, None), ("0", None), (-5, None),
         (" ", None), ("", None), (None, None)],
    )
    def test_zero_is_not_recorded_rather_than_a_real_zero(self, raw, expected):
        """A parcel with 0 bedrooms has an unrecorded bedroom count, not zero bedrooms.

        This distinction is the whole reason these helpers exist. Reading a roll zero as a
        real measurement would let the scoring engine deduct 20 points for a bedroom count
        the county simply never typed in, which is a fabricated deduction.
        """
        assert positive_int(raw) == expected

    @pytest.mark.parametrize(
        "raw,expected", [(2.0, 2.0), ("2.5", 2.5), (0, None), (" ", None), (None, None)]
    )
    def test_float_coercion_treats_zero_the_same_way(self, raw, expected):
        assert positive_float(raw) == expected

    @pytest.mark.parametrize(
        "raw,expected",
        [("4", "00004"), ("00004", "00004"), (4, "00004"), ("67", "00067"),
         (" ", None), ("", None), (None, None)],
    )
    def test_tax_rate_areas_are_zero_padded_to_five_digits(self, raw, expected):
        """The roll writes "00004" and the county's own lookup form wants "4".

        Both have to normalise to one key or the rate lookup misses and every property
        silently falls back to the countywide average.
        """
        assert normalize_tra(raw) == expected

    def test_situs_prefers_the_full_address_field(self):
        assert situs({"SitusFullAddress": "2475 VERDE ST LOS ANGELES CA 90033"}) == (
            "2475 VERDE ST LOS ANGELES CA 90033"
        )

    def test_a_blank_situs_is_none_not_an_empty_string(self):
        assert situs({"SitusFullAddress": "", "SitusAddress": " "}) is None

    @pytest.mark.parametrize(
        "text,expected",
        [("2475 VERDE ST", "2475"), ("2475 Verde St, Los Angeles", "2475"),
         ("VERDE ST", None), ("", None), (None, None)],
    )
    def test_street_number_extraction(self, text, expected):
        assert street_number(text) == expected


class TestBuildingRollup:
    """The roll stores up to five buildings per parcel in numbered field groups.

    Rolling them up is not cosmetic. A parcel with a house and a garage-conversion ADU has
    its bedrooms split across two building slots, and reading only the first slot would
    report a three-bedroom house as a one-bedroom.
    """

    def test_a_single_building_reads_straight_through(self):
        got = read_buildings(
            {"YearBuilt1": "1957", "Bedrooms1": "3", "Bathrooms1": "2",
             "SQFTmain1": "1350", "Units1": "1"}
        )
        assert got["year_built"] == 1957
        assert got["beds"] == 3
        assert got["baths"] == 2.0
        assert got["living_sqft"] == 1350
        assert got["building_count"] == 1

    def test_multiple_buildings_sum_their_area_and_rooms(self):
        got = read_buildings(
            {"YearBuilt1": "1957", "Bedrooms1": "3", "Bathrooms1": "2",
             "SQFTmain1": "1350", "Units1": "1",
             "YearBuilt2": "1998", "Bedrooms2": "1", "Bathrooms2": "1",
             "SQFTmain2": "480", "Units2": "1"}
        )
        assert got["living_sqft"] == 1830
        assert got["beds"] == 4
        assert got["baths"] == 3.0
        assert got["building_count"] == 2

    def test_the_year_reported_is_the_oldest_building(self):
        """The 1957 house is what needs a roof, not the 1998 addition.

        Taking the newest year would make an old house look new, which is the direction that
        loses money -- it suppresses the capital-expense deductions.
        """
        got = read_buildings({"YearBuilt1": "1998", "YearBuilt2": "1957"})
        assert got["year_built"] == 1957

    def test_an_empty_record_reports_nothing_rather_than_zeros(self):
        got = read_buildings({"YearBuilt1": " ", "Bedrooms1": "0", "SQFTmain1": "0"})
        assert got["year_built"] is None
        assert got["beds"] is None
        assert got["living_sqft"] is None
        assert got["building_count"] == 0


class TestParcelDisambiguation:
    """A 40 m buffer catches neighbours. Picking wrong attaches their house to yours."""

    def test_a_single_candidate_is_returned_unchanged(self):
        one = {"SitusFullAddress": "2475 VERDE ST LOS ANGELES CA 90033"}
        assert pick_parcel([one], "2475 Verde St") is one

    def test_the_street_number_breaks_the_tie(self):
        candidates = [
            {"SitusHouseNo": "2439", "SitusFullAddress": "2439 VERDE ST", "Bedrooms1": "2"},
            {"SitusHouseNo": "2475", "SitusFullAddress": "2475 VERDE ST", "Bedrooms1": "4"},
        ]
        chosen = pick_parcel(candidates, "2475 Verde St, Los Angeles CA 90033")
        assert chosen["Bedrooms1"] == "4"

    def test_no_match_is_flagged_rather_than_guessed_silently(self):
        candidates = [
            {"SitusHouseNo": "2439", "SitusFullAddress": "2439 VERDE ST"},
            {"SitusHouseNo": "2501", "SitusFullAddress": "2501 VERDE ST"},
        ]
        assert "_ambiguous" in pick_parcel(candidates, "2475 Verde St")

    def test_the_real_recorded_response_resolves_to_the_right_parcel(self, located):
        """Eleven parcels come back inside 40 m of this point. Only one is the house.

        This is the test that would have caught a lazy `features[0]`: the first feature in
        the recorded response is 2439 Verde St, a two-unit property next door, and the
        second has no situs address at all.
        """
        features = load_response("parcel_assessor")["features"]
        assert len(features) == 11
        chosen = pick_parcel([f["attributes"] for f in features], located.address)
        assert chosen["AIN"] == "5202009021"
        assert chosen["SitusHouseNo"] == "2475"
        assert "_ambiguous" not in chosen


class TestParcelStation:
    @pytest.fixture
    def stubbed(self, fake_http):
        fake_http.route("public.gis.lacounty.gov", load_response("parcel_assessor"))
        return fake_http

    def test_it_reads_the_house_from_a_real_record(self, stubbed, located):
        result = ParcelStation().run(located)
        assert result.ok
        assert result.facts["year_built"] == 1957
        assert result.facts["beds"] == 3
        assert result.facts["baths"] == 2.0
        assert result.facts["sqft"] == 1350

    def test_assessor_values_are_measured_not_estimated(self, stubbed, located):
        """There is no stale mirror in this port, so nothing here is a snapshot estimate.

        The Spartanburg station fell back to a 2021 ArcGIS mirror and downgraded its values
        to `estimated` to say so. Los Angeles County publishes the live roll, so a value that
        comes back at all is measured -- and if the service is down the station degrades
        rather than quietly serving something old.
        """
        assert result_confidences(ParcelStation().run(located)) == {"measured", "derived"}

    def test_the_tax_rate_area_is_handed_to_the_tax_engine(self, stubbed, located):
        """The parcel's own TRA overrides the profile default. This is the wiring for that."""
        result = ParcelStation().run(located)
        assert result.context_updates["tax_rate_area"] == "00004"
        assert result.values["tax_rate_city"].value == "LOS ANGELES"

    def test_water_and_sewer_is_unavailable_and_says_why(self, stubbed, located):
        """The roll has no utility fields at all. Guessing "public" would be inventing data.

        Reported rather than omitted, because the scoring engine has to be able to tell the
        difference between "we asked and the answer does not exist" and "nobody asked".
        """
        result = ParcelStation().run(located)
        assert result.facts["water_sewer"] is None
        assert "no utility fields" in result.values["water_sewer"].note
        assert any("water and sewer" in t["task"] for t in result.tasks)

    def test_lot_size_and_garage_are_asked_for_rather_than_invented(self, stubbed, located):
        result = ParcelStation().run(located)
        assert "garage_spaces" not in result.facts
        assert any("garage bay count" in t["task"] for t in result.tasks)

    def test_the_prop_13_gap_becomes_a_blocking_task(self, stubbed, located):
        """The single most valuable thing this station produces.

        The seller's assessed value is $203,078 against a $775,000 asking price, because
        their base year is 1986 and Proposition 13 froze it there. Any tax figure on the
        listing is computed off that frozen basis and is therefore too low -- roughly by a
        factor of four here. It is blocking because a buyer who trusts the listing number
        under-budgets their monthly payment by hundreds of dollars.
        """
        result = ParcelStation().run(located)
        assert result.values["current_assessed_value"].value == 203_078.0
        assert result.values["prop_13_base_year"].value == 1986
        assert result.context_updates["current_assessed_value"] == 203_078.0
        blocking = [t for t in result.tasks if t["blocking"]]
        assert any("Ignore the tax figure on the listing" in t["task"] for t in blocking)

    def test_the_assessed_to_price_ratio_quantifies_the_reset(self, stubbed, located):
        ratio = ParcelStation().run(located).values["assessed_to_price_ratio"]
        assert ratio.value == pytest.approx(0.262, abs=0.001)
        assert ratio.confidence == "derived"

    def test_the_sellers_exemption_is_reported_as_not_transferring(self, stubbed, located):
        result = ParcelStation().run(located)
        assert result.values["current_homeowners_exemption"].value == 7000.0
        assert any("February 15" in t["task"] for t in result.tasks)

    def test_no_parcel_found_degrades_rather_than_guessing(self, fake_http, located):
        fake_http.route("public.gis.lacounty.gov", {"features": []})
        result = ParcelStation().run(located)
        assert result.degradation is not None
        assert "no parcel found" in result.degradation.reason

    def test_an_unreachable_assessor_degrades_and_does_not_raise(self, fake_http, located):
        fake_http.route(
            "public.gis.lacounty.gov", http.SourceUnavailable("read timed out")
        )
        result = ParcelStation().run(located)
        assert result.degradation is not None
        assert result.facts == {}


def result_confidences(result) -> set[str]:
    return {v.confidence for v in result.values.values()}


# =============================================================================
# B — broadband
# =============================================================================


class TestBroadband:
    def test_no_api_key_degrades_with_an_instruction(self, fake_http, located):
        result = BroadbandStation(api_key=None).run(located)
        assert result.degradation is not None
        assert "FCC_API_KEY" in result.degradation.reason
        assert fake_http.calls == []

    def test_a_missing_key_never_reports_no_fiber(self, fake_http, located):
        """The bug that would silently deduct 15 points from every house in the county."""
        result = BroadbandStation(api_key=None).run(located)
        assert result.facts == {}

    def test_no_census_block_degrades(self, fake_http, located):
        located.census_block_geoid = None
        result = BroadbandStation(api_key="key").run(located)
        assert "census block" in result.degradation.reason

    def test_fiber_found(self, fake_http, located):
        fake_http.route(
            "broadbandmap.fcc.gov",
            {"data": [{"brand_name": "Spartanburg Fiber", "technology_code": 50,
                       "max_advertised_download_speed": 1000}]},
        )
        result = BroadbandStation(api_key="key").run(located)
        assert result.facts["fiber_available"] is True
        assert "Spartanburg Fiber" in result.values["reporting_providers"].value

    def test_cable_alone_is_not_fiber(self, fake_http, located):
        fake_http.route(
            "broadbandmap.fcc.gov",
            {"data": [{"brand_name": "Cable Co", "technology_code": 40,
                       "max_advertised_download_speed": 940}]},
        )
        result = BroadbandStation(api_key="key").run(located)
        assert result.facts["fiber_available"] is False

    def test_slow_fiber_does_not_count(self, fake_http, located):
        fake_http.route(
            "broadbandmap.fcc.gov",
            {"data": [{"brand_name": "Slow Fiber", "technology_code": 50,
                       "max_advertised_download_speed": 25}]},
        )
        result = BroadbandStation(api_key="key").run(located)
        assert result.facts["fiber_available"] is False

    def test_every_value_is_block_precision_and_says_availability_is_a_claim(
        self, fake_http, located
    ):
        fake_http.route(
            "broadbandmap.fcc.gov",
            {"data": [{"brand_name": "F", "technology_code": 50,
                       "max_advertised_download_speed": 1000}]},
        )
        result = BroadbandStation(api_key="key").run(located)
        value = result.values["fiber_available"]
        assert value.precision == "census_block"
        assert "10 business days" in value.note
        assert "not address level" in value.note

    def test_the_call_the_isp_task_is_permanent_and_blocking(self, fake_http, located):
        fake_http.route(
            "broadbandmap.fcc.gov",
            {"data": [{"brand_name": "Acme Fiber", "technology_code": 50,
                       "max_advertised_download_speed": 1000}]},
        )
        result = BroadbandStation(api_key="key").run(located)
        call = [t for t in result.tasks if "Call" in t["task"]]
        assert len(call) == 1
        assert call[0]["blocking"]
        assert "Acme Fiber" in call[0]["task"]

    def test_no_filing_is_unknown_not_unserved(self, fake_http, located):
        fake_http.route("broadbandmap.fcc.gov", {"data": []})
        result = BroadbandStation(api_key="key").run(located)
        assert result.facts["fiber_available"] is None
        assert "not evidence of no service" in result.values["fiber_available"].note

    def test_a_rejected_key_degrades_rather_than_guessing(self, fake_http, located):
        fake_http.route("broadbandmap.fcc.gov", http.SourceRejected("HTTP 401 Unauthorized"))
        result = BroadbandStation(api_key="stale-key").run(located)
        assert "declined" in result.degradation.reason
        assert result.facts == {}

# The Spartanburg port had a second parcel test class here, covering the fallback from the
# live county server to a retained 2021 ArcGIS mirror. There is no equivalent in this port and
# deliberately so: Los Angeles County publishes the live assessment roll, and serving a
# multi-year-old snapshot of a *current* tax basis would be worse than serving nothing at all
# under Proposition 13, where the basis is the entire question. See docs/adr/0006.

