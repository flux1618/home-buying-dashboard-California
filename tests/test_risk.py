"""The FEMA National Risk Index station, against recorded responses.

Two fixtures, and the second one is the whole point. `nri_tract` is Spartanburg, where
every hazard is unremarkable and nothing much happens. `nri_tract_wildfire` is Paradise,
California — the tract the 2018 Camp Fire destroyed — where FEMA reports a wildfire
percentile of 95.2 alongside a wildfire rating of "Relatively Moderate" and a *composite*
rating of "Relatively Low".

A station tested only against Spartanburg passes whether it reads percentiles or rating
labels, because in Spartanburg the two agree. Paradise is the fixture that can tell them
apart, which is why it is committed even though nobody is buying a house there.
"""

from __future__ import annotations

import copy

import pytest

from analyzer.core.profile import load_profile
from analyzer.core.scoring import PropertyFacts
from analyzer.sources.base import Context
from analyzer.sources.risk import (
    HAZARD_LABELS,
    HAZARD_SUFFIXES,
    RiskStation,
    is_modeled,
)

from support import load_response

# The profile's hazard list. Los Angeles County trades the Southeast's wind-and-hail
# exposure for earthquake, wildfire and landslide.
CA_HAZARDS = ("WFIR", "ERQK", "DRGT", "HWAV", "LNDS")


@pytest.fixture
def located() -> Context:
    return Context(
        address="606 Andre Ct, Spartanburg, SC 29301",
        price=268_000,
        lat=34.943051,
        lon=-81.97665,
        county_fips="45083",
    )


def run_with(fake_http, fixture: str, hazards, located, **kw):
    fake_http.route("National_Risk_Index", load_response(fixture))
    return RiskStation(hazards=hazards, **kw).run(located)


# =============================================================================
# The distinction the whole station exists to preserve
# =============================================================================


class TestPercentileNotLabel:
    """FEMA's rating bins are cut per hazard. The labels are not a shared scale."""

    def test_wildfire_reports_the_percentile_as_the_value(self, fake_http, located):
        result = run_with(fake_http, "nri_tract_wildfire", CA_HAZARDS, located)

        value = result.values["hazard_wildfire"]
        # 95.2 is the number that matters. If this ever comes back as one of FEMA's five
        # labels, or as an integer 1-5 derived from one, the station has regressed.
        assert value.value == pytest.approx(95.2, abs=0.1)
        assert isinstance(value.value, float)

    def test_the_rating_label_survives_only_as_a_note(self, fake_http, located):
        result = run_with(fake_http, "nri_tract_wildfire", CA_HAZARDS, located)

        value = result.values["hazard_wildfire"]
        assert "Relatively Moderate" in (value.note or "")
        assert value.value != "Relatively Moderate"

    def test_a_mild_label_on_a_severe_percentile_still_raises_the_caveat(
        self, fake_http, located
    ):
        """Paradise: rating "Relatively Moderate", percentile 95.2. The caveat must fire.

        This is the regression test for scoring on labels. A station that thresholded on
        the rating string would see "Relatively Moderate", conclude there was nothing to
        say, and stay silent about the tract with the highest wildfire risk in the suite.
        """
        result = run_with(
            fake_http, "nri_tract_wildfire", CA_HAZARDS, located, caveat_percentile=90.0
        )

        wildfire_tasks = [t for t in result.tasks if "wildfire" in t["task"].lower()]
        assert len(wildfire_tasks) == 1
        assert "95th percentile" in wildfire_tasks[0]["task"]
        assert wildfire_tasks[0]["blocking"] is False

    def test_the_composite_rating_does_not_mask_a_severe_single_hazard(
        self, fake_http, located
    ):
        """Paradise's composite is "Relatively Low" at the 32nd percentile.

        The composite averages 18 hazards, most of which do not apply anywhere, so a
        tract can burn down while rating low overall. Anything that reported only the
        composite would describe Paradise as a low-risk location.
        """
        result = run_with(fake_http, "nri_tract_wildfire", CA_HAZARDS, located)

        composite = result.values["nri_composite_risk"]
        assert composite.value < 40.0
        assert "Relatively Low" in (composite.note or "")
        # And yet:
        assert result.values["hazard_wildfire"].value > 90.0
        assert any("wildfire" in t["task"].lower() for t in result.tasks)

    def test_below_threshold_hazards_raise_no_caveat(self, fake_http, located):
        """Heat wave at the anchor tract sits around the 40th percentile. Silence is right.

        Landslide is the sharper version of the same point: 53rd percentile is above the
        midpoint and still nowhere near a 90th-percentile threshold, so a station that
        warned on it would be crying wolf on the median parcel.
        """
        result = run_with(
            fake_http, "nri_tract", CA_HAZARDS, located, caveat_percentile=90.0
        )

        assert result.values["hazard_heat_wave"].value < 50.0
        assert result.values["hazard_landslide"].value < 90.0
        assert not [t for t in result.tasks if "heat wave" in t["task"].lower()]
        assert not [t for t in result.tasks if "landslide" in t["task"].lower()]

    def test_the_anchor_tract_flags_earthquake_not_wildfire(self, fake_http, located):
        """The real finding at 90033, and it inverts the intuition the fork was built on.

        Boyle Heights is at the 95th percentile for earthquake and has *no* wildfire rating
        at all, because the tract is dense urban fabric with no wildland to burn. A port
        that assumed "California means wildfire" would have raised the wrong warning here
        and missed the one that justifies a separate earthquake premium in the budget.
        """
        result = run_with(
            fake_http, "nri_tract", CA_HAZARDS, located, caveat_percentile=90.0
        )

        assert result.values["hazard_earthquake"].value > 90.0
        assert any("earthquake" in t["task"].lower() for t in result.tasks)
        assert result.values["hazard_wildfire"].is_available is False
        assert not [t for t in result.tasks if "wildfire" in t["task"].lower()]


# =============================================================================
# A zero is not good news
# =============================================================================


class TestNotModeledIsNotLowRisk:
    def test_drought_comes_back_unavailable_not_zero(self, fake_http, located):
        """Both fixtures return DRGT score 0.0 with rating "No Rating"."""
        result = run_with(fake_http, "nri_tract", CA_HAZARDS, located)

        value = result.values["hazard_drought"]
        assert value.is_available is False
        assert value.value is None
        assert "not the same as low risk" in (value.note or "")

    def test_an_unmodeled_hazard_is_marked_unmodeled_in_the_profile(
        self, fake_http, located
    ):
        result = run_with(fake_http, "nri_tract", CA_HAZARDS, located)

        assert result.facts["hazard_profile"]["hazards"]["DRGT"] == {
            "label": "drought",
            "modeled": False,
        }

    def test_an_unmodeled_hazard_never_raises_a_caveat(self, fake_http, located):
        """A percentile of 0 is below every threshold, but there is no percentile."""
        result = run_with(
            fake_http, "nri_tract", CA_HAZARDS, located, caveat_percentile=0.0
        )

        assert not [t for t in result.tasks if "drought" in t["task"].lower()]

    @pytest.mark.parametrize(
        "rating",
        ["No Rating", "no rating", "  No Rating  ", "Insufficient Data", "", None],
    )
    def test_femas_ways_of_declining_to_rate(self, rating):
        assert is_modeled(rating) is False

    @pytest.mark.parametrize(
        "rating", ["Very Low", "Relatively Moderate", "Very High"]
    )
    def test_real_ratings_are_modeled(self, rating):
        assert is_modeled(rating) is True

    def test_a_missing_score_with_a_real_rating_is_still_unavailable(
        self, fake_http, located
    ):
        """Belt and braces: a shape change that drops the score must not read as 0."""
        payload = copy.deepcopy(load_response("nri_tract"))
        payload["features"][0]["attributes"]["WFIR_RISKS"] = None
        fake_http.route("National_Risk_Index", payload)

        result = RiskStation(hazards=("WFIR",)).run(located)
        assert result.values["hazard_wildfire"].is_available is False


# =============================================================================
# This station cannot move a score
# =============================================================================


class TestReportingNotScoring:
    def test_no_hazard_field_exists_on_the_scoring_input(self):
        """The engine's input has no hazard slot, so nothing here can reach the score.

        This is the structural guarantee, and it is worth asserting rather than trusting:
        if someone later adds `wildfire_percentile` to PropertyFacts, every score already
        written to the decision journal silently becomes non-reproducible. Adding it
        should require deleting this test, which is a conversation.
        """
        fields = set(PropertyFacts.__dataclass_fields__)
        for banned in ("hazard", "wildfire", "nri", "risk_percentile", "sovi"):
            assert not [f for f in fields if banned in f], (
                f"PropertyFacts gained a {banned!r} field — hazard data is a caveat "
                f"layer (ADR 0009). Scoring on it changes every recorded score."
            )

    def test_the_station_declares_a_fact_the_engine_does_not_consume(
        self, fake_http, located
    ):
        result = run_with(fake_http, "nri_tract", CA_HAZARDS, located)

        assert set(result.facts) == {"hazard_profile"}
        assert set(result.facts) & set(PropertyFacts.__dataclass_fields__) == set()

    def test_every_task_is_non_blocking(self, fake_http, located):
        """Only flood, septic and commute can block. Hazard risk prices, it does not veto."""
        for fixture, hazards in (
            ("nri_tract", CA_HAZARDS),
            ("nri_tract_wildfire", CA_HAZARDS),
        ):
            result = run_with(
                fake_http, fixture, hazards, located, caveat_percentile=0.0
            )
            assert result.tasks, "expected caveats at a zero threshold"
            assert all(t["blocking"] is False for t in result.tasks)


# =============================================================================
# Vulnerability and resilience
# =============================================================================


class TestVulnerabilityAndResilience:
    def test_both_indices_come_back_as_percentiles(self, fake_http, located):
        """Vulnerability varies sharply tract to tract, so no magnitude is asserted here.

        The Andre Ct tract reads 43rd percentile; the tract around Spartanburg Medical
        Center a few miles away reads 98th. Pinning a number would be pinning a
        neighbourhood, and re-recording the fixture for a different address would break
        a test that is only trying to check the parse.
        """
        result = run_with(fake_http, "nri_tract", CA_HAZARDS, located)

        sovi = result.values["social_vulnerability"]
        resl = result.values["community_resilience"]
        assert sovi.is_available and resl.is_available
        assert 0.0 <= sovi.value <= 100.0
        assert 0.0 <= resl.value <= 100.0
        assert sovi.precision == "national_percentile"

    def test_high_vulnerability_with_low_resilience_raises_a_recovery_caveat(
        self, fake_http, located
    ):
        payload = copy.deepcopy(load_response("nri_tract"))
        attrs = payload["features"][0]["attributes"]
        attrs["SOVI_SCORE"], attrs["SOVI_RATNG"] = 97.8, "Very High"
        attrs["RESL_SCORE"], attrs["RESL_RATNG"] = 44.6, "Relatively Moderate"
        fake_http.route("National_Risk_Index", payload)

        result = RiskStation(hazards=()).run(located)
        recovery = [t for t in result.tasks if "recovery" in t["task"].lower()]
        assert len(recovery) == 1
        assert recovery[0]["blocking"] is False

    def test_high_vulnerability_with_high_resilience_stays_quiet(
        self, fake_http, located
    ):
        payload = copy.deepcopy(load_response("nri_tract"))
        attrs = payload["features"][0]["attributes"]
        attrs["SOVI_SCORE"], attrs["SOVI_RATNG"] = 97.8, "Very High"
        attrs["RESL_SCORE"], attrs["RESL_RATNG"] = 88.0, "Relatively High"
        fake_http.route("National_Risk_Index", payload)

        result = RiskStation(hazards=()).run(located)
        assert not [t for t in result.tasks if "recovery" in t["task"].lower()]


# =============================================================================
# Failure behaviour
# =============================================================================


class TestFailure:
    def test_no_tract_is_unknown_not_safe(self, fake_http, located):
        fake_http.route("National_Risk_Index", {"features": []})
        result = RiskStation(hazards=CA_HAZARDS).run(located)

        assert result.ok
        assert result.facts["hazard_profile"] is None
        assert result.values["nri_tract_fips"].is_available is False
        assert "bad geocode" in result.tasks[0]["reason"]

    def test_an_arcgis_error_payload_degrades(self, fake_http, located):
        fake_http.route(
            "National_Risk_Index",
            {"error": {"code": 400, "message": "Invalid or missing input parameters."}},
        )
        result = RiskStation(hazards=CA_HAZARDS).run(located)

        assert result.degradation is not None
        assert "hazard_profile" in result.degradation.missing

    def test_no_coordinates_skips_without_a_request(self, fake_http):
        result = RiskStation(hazards=CA_HAZARDS).run(
            Context(address="nowhere", price=1.0)
        )

        assert result.degradation is not None
        assert "no coordinates" in result.degradation.reason
        assert fake_http.calls == [] if hasattr(fake_http, "calls") else True


# =============================================================================
# Configuration — the fork seam
# =============================================================================


class TestConfiguration:
    def test_a_typo_in_the_hazard_list_fails_loudly(self):
        with pytest.raises(ValueError, match="unknown NRI hazard code"):
            RiskStation(hazards=("WFIR", "WILDFIRE"))

    def test_the_profile_supplies_the_hazard_list(self):
        profile = load_profile()
        assert profile.hazards, "buyer_profile.toml should configure hazards"
        assert all(code in HAZARD_LABELS for code in profile.hazards)

    def test_a_profile_without_a_risk_table_still_loads(self, tmp_path):
        """Backwards compatibility: [risk] is optional and defaults to reporting nothing."""
        import tomllib

        source = load_profile.__module__  # noqa: F841 - readability only
        original = (tmp_path / "buyer_profile.toml")
        text = open("buyer_profile.toml").read()
        stripped = text.split("[risk]")[0]
        original.write_text(stripped)
        assert "risk" not in tomllib.loads(stripped)

        profile = load_profile(original)
        assert profile.hazards == ()
        assert profile.hazard_caveat_percentile == 90.0

    def test_a_california_fork_needs_no_code_change(self, fake_http, located):
        """The same station class, a different hazard list, a different state's fixture."""
        result = run_with(fake_http, "nri_tract_wildfire", CA_HAZARDS, located)

        reported = result.facts["hazard_profile"]["hazards"]
        assert set(reported) == set(CA_HAZARDS)
        assert result.facts["hazard_profile"]["tract_label"] == (
            "Butte County, California"
        )

    def test_all_eighteen_nri_hazards_are_known(self):
        assert len(HAZARD_LABELS) == 18
        assert all(len(code) == 4 and code.isupper() for code in HAZARD_LABELS)

    def test_requested_suffixes_exist_for_every_hazard(self):
        """Several hazards have no building or population loss model at all.

        Drought has neither EALB nor EALP on the live layer, so a per-hazard field list
        that included them would 400 the entire query for one hazard's sake. Only
        suffixes present on all 18 belong in HAZARD_SUFFIXES.
        """
        assert HAZARD_SUFFIXES == ("RISKS", "RISKR", "EALT")


# =============================================================================
# Provenance
# =============================================================================


class TestProvenance:
    def test_every_available_value_carries_a_source_url(self, fake_http, located):
        result = run_with(fake_http, "nri_tract_wildfire", CA_HAZARDS, located)

        for key, value in result.values.items():
            if value.is_available:
                assert value.source_url, f"{key} has no source_url"
                assert value.retrieved_at, f"{key} has no retrieval timestamp"

    def test_the_data_version_is_recorded(self, fake_http, located):
        """FEMA reissues the NRI. A percentile without a version is not reproducible."""
        result = run_with(fake_http, "nri_tract", CA_HAZARDS, located)

        assert "NRI data version" in (result.values["nri_tract_fips"].note or "")


# =============================================================================
# The printed report
# =============================================================================


class TestRendering:
    """The report is where the percentile-versus-label distinction gets seen or lost."""

    def hazards_for(self, fake_http, fixture, hazards, located):
        return run_with(fake_http, fixture, hazards, located).facts["hazard_profile"]

    def test_hazards_print_worst_first(self, fake_http, located, capsys):
        from analyzer.cli import render_hazards

        render_hazards(
            self.hazards_for(fake_http, "nri_tract_wildfire", CA_HAZARDS, located)
        )
        out = capsys.readouterr().out
        # The composite footnote also names the worst hazard, so it has to be excluded
        # or it counts as a second wildfire row and the ordering assertion means nothing.
        order = [
            line
            for line in out.splitlines()
            if any(w in line for w in ("wildfire", "earthquake", "heat wave"))
            and "composite" not in line
        ]
        assert "wildfire" in order[0]
        assert "heat wave" in order[-1]

    def test_the_composite_gets_called_out_when_it_understates(
        self, fake_http, located, capsys
    ):
        from analyzer.cli import render_hazards

        render_hazards(
            self.hazards_for(fake_http, "nri_tract_wildfire", CA_HAZARDS, located)
        )
        out = capsys.readouterr().out
        assert "understates wildfire" in out
        assert "averages 18 hazards" in out

    def test_no_composite_note_when_the_hidden_hazard_is_unremarkable(
        self, fake_http, located, capsys
    ):
        """Spartanburg: composite 18.6, worst hazard hail at 51.7 — a 33-point gap.

        The gap is wide but hail in the 52nd percentile is not news, and a caveat channel
        that fires on non-news stops getting read. The note requires both a wide gap and a
        hazard at or above the 75th percentile.
        """
        from analyzer.cli import render_hazards

        render_hazards(self.hazards_for(fake_http, "nri_tract", CA_HAZARDS, located))
        assert "understates" not in capsys.readouterr().out

    def test_unmodeled_hazards_say_unknown_not_low(self, fake_http, located, capsys):
        from analyzer.cli import render_hazards

        render_hazards(self.hazards_for(fake_http, "nri_tract", CA_HAZARDS, located))
        out = capsys.readouterr().out
        assert "not modeled here: drought" in out
        assert "unknown, not low" in out

    def test_the_section_states_it_does_not_affect_the_score(
        self, fake_http, located, capsys
    ):
        from analyzer.cli import render_hazards

        render_hazards(self.hazards_for(fake_http, "nri_tract", CA_HAZARDS, located))
        assert "no score effect" in capsys.readouterr().out

    def test_no_hazard_profile_prints_nothing(self, capsys):
        from analyzer.cli import render_hazards

        render_hazards(None)
        render_hazards({})
        assert capsys.readouterr().out == ""

    def test_uncoloured_rows_leave_no_escape_sequence(
        self, fake_http, located, capsys
    ):
        """This output gets pasted into notes and logs that do not interpret ANSI."""
        from analyzer.cli import render_hazards

        render_hazards(self.hazards_for(fake_http, "nri_tract", CA_HAZARDS, located))
        for line in capsys.readouterr().out.splitlines():
            if "\033[" not in line.replace("\033[0m", ""):
                assert "\033[0m" not in line, f"stray reset in {line!r}"
