"""The hazard block in data.json, and the promise that the page does not score it.

`tools/build_hazard_snapshot.py` fetches from FEMA, so these tests do not run it. They
check the committed artifact and the code that reads it, which is what actually ships.

The load-bearing test in this file is `TestThePageDoesNotScoreHazards`. Everything else
verifies shape; that class verifies the architectural promise from ADR 0009 survived
contact with a browser.
"""

from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SNAPSHOT = json.loads((REPO / "data.json").read_text())
APP_JS = (REPO / "app.js").read_text()
INDEX = (REPO / "index.html").read_text()
with open(REPO / "buyer_profile.toml", "rb") as fh:
    PROFILE = tomllib.load(fh)

HAZ = SNAPSHOT.get("hazards")


def load_builder():
    """Import tools/build_hazard_snapshot.py by path, not by module name.

    `tools/` is a directory of scripts, not an installed package, so `import tools...`
    only works when the repo root happens to be on sys.path. It did locally and did not
    in CI, which is a worse failure than it looks: the tests below would have silently
    stopped covering the checker on any machine that imported differently.
    """
    import importlib.util

    path = REPO / "tools" / "build_hazard_snapshot.py"
    spec = importlib.util.spec_from_file_location("build_hazard_snapshot", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def haz():
    assert HAZ, "data.json has no hazards block; run tools/build_hazard_snapshot.py"
    return HAZ


class TestParityWithTheProfile:
    """The page and the engine must report the same hazards. This is the same failure the
    rules block was written to end -- two lists of hazards would drift the same way two
    copies of the scoring thresholds did."""

    def test_hazard_codes_match_the_profile(self, haz):
        assert tuple(haz["hazard_codes"]) == tuple(PROFILE["risk"]["hazards"])

    def test_caveat_percentile_matches_the_profile(self, haz):
        assert haz["caveat_percentile"] == PROFILE["risk"]["caveat_percentile"]

    def test_every_profile_hazard_has_a_county_entry(self, haz):
        for code in PROFILE["risk"]["hazards"]:
            assert code in haz["county"]["hazards"]

    def test_labels_come_from_the_station_not_retyped(self, haz):
        from analyzer.sources import risk

        for code, label in haz["hazard_labels"].items():
            assert label == risk.HAZARD_LABELS[code]


class TestThePageDoesNotScoreHazards:
    """ADR 0009 in JavaScript. The engine keeps hazard data out of PropertyFacts; the page
    has to keep it out of the scorer, or the two doors disagree about what a score means."""

    def _body(self, name: str) -> str:
        """Crude but adequate: slice from `function name(` to the next top-level
        `function` or `/* ---` banner. A real parser would be better and is not worth a
        dependency for one assertion."""
        start = APP_JS.index(f"function {name}(")
        rest = APP_JS[start + 10 :]
        ends = [i for i in (rest.find("\nfunction "), rest.find("\n/* ---")) if i > 0]
        return rest[: min(ends)] if ends else rest

    def test_the_property_scorer_never_reads_the_hazard_block(self):
        body = self._body("updateProperty").lower()
        assert "data.hazards" not in body
        # NRI hazard names specifically. The bare word "hazard" is not usable here: the
        # FEMA flood zone hard fail says "special flood hazard area" and that one really
        # does score, which is the distinction ADR 0009 draws.
        for word in ("wildfire", "drought", "tornado", "nri", "sovi", "resilience"):
            assert word not in body

    def test_the_submarket_scorecard_never_reads_the_hazard_block(self):
        assert "DATA.hazards" not in self._body("renderScorecard")

    def test_the_hazard_block_is_absent_from_the_compiled_rules(self):
        """`rules` is the contract the scorer evaluates. A hazard key appearing here would
        mean somebody wired risk into the deduction table without an ADR."""
        rules = json.dumps(SNAPSHOT["rules"]).lower()
        for word in ("wildfire", "drought", "tornado", "nri", "sovi", "resilience", "percentile"):
            assert word not in rules
        # "hazard" appears once, in the FEMA flood zone hard fail. That is the only place
        # it may appear -- flood zone is a hard fail, NRI ratings are caveats.
        assert rules.count("hazard") == 1
        assert "special flood hazard area" in rules

    def test_only_the_hazard_renderers_touch_the_hazard_block(self):
        """Whitelist, not a count. If a new function starts reading DATA.hazards this
        fails and the person adding it has to decide whether it scores anything."""
        allowed = {"renderHazards", "updateHazards"}
        for match in re.finditer(r"DATA\.hazards", APP_JS):
            preceding = APP_JS[: match.start()]
            owner = re.findall(r"function (\w+)\(", preceding)[-1]
            assert owner in allowed, f"{owner} reads DATA.hazards"

    def test_the_page_says_out_loud_that_hazards_do_not_score(self):
        assert "None of this scores a house" in INDEX


class TestNotModeledIsNotLowRisk:
    """The same bug as the parcel station, one layer out: a blank rendering as good news."""

    def test_drought_is_modeled_in_a_minority_of_tracts(self, haz):
        """The two-shape design matters when a hazard is not modeled everywhere."""
        drgt = haz["county"]["hazards"]["DRGT"]
        assert 0 < drgt["modeled_tracts"] <= drgt["total_tracts"]

    def test_where_drought_is_modeled_it_is_high(self, haz):
        drgt = haz["county"]["hazards"]["DRGT"]
        if drgt["modeled_tracts"]:
            assert 0.0 <= drgt["min"] <= drgt["max"] <= 100.0

    def test_unmodeled_hazards_carry_no_percentile(self, haz):
        for zip_code, entry in haz["zips"].items():
            for code, h in entry["hazards"].items():
                if not h["modeled"]:
                    assert "percentile" not in h, f"{zip_code}/{code} has a phantom score"

    def test_the_renderer_calls_unmodeled_unknown_not_low(self):
        assert "Unknown, not low." in APP_JS

    def test_unmodeled_sorts_below_every_modeled_hazard(self):
        """hzRank returns -1 for unmodeled. A 0 would tie with a genuine 0.0 percentile
        and put "nobody measured this" next to "measured, and lowest in the country"."""
        assert "return h.modeled ? h.percentile : -1;" in APP_JS


class TestPercentilesNotLabels:
    def test_every_modeled_hazard_has_both_a_number_and_a_label(self, haz):
        for entry in haz["zips"].values():
            for h in entry["hazards"].values():
                if h["modeled"]:
                    assert isinstance(h["percentile"], (int, float))
                    assert isinstance(h["rating"], str)

    def test_percentiles_are_on_a_zero_to_hundred_scale(self, haz):
        for entry in haz["zips"].values():
            for h in entry["hazards"].values():
                if h["modeled"]:
                    assert 0.0 <= h["percentile"] <= 100.0

    def test_hazard_labels_are_not_used_as_cross_hazard_scores(self, haz):
        """The LA profile does not promise a particular label inversion on a chosen ZIP.

        It still commits the data needed to detect one: every modeled profile hazard has
        both the national percentile and FEMA's hazard-specific label.
        """
        for entry in haz["zips"].values():
            modeled = [h for h in entry["hazards"].values() if h["modeled"]]
            assert all("percentile" in h and "rating" in h for h in modeled)

    def test_the_ordering_helper_sorts_on_the_number(self):
        assert "hzRank(b.tract)-hzRank(a.tract)" in APP_JS


class TestResolutionHonesty:
    def test_community_resilience_does_not_vary_within_the_county(self, haz):
        """Measured across four counties. It is published per county and joined onto the
        tract table, so presenting it as a neighbourhood signal would be an invention."""
        assert haz["county"]["community_resilience"]["varies_by_tract"] is False

    def test_social_vulnerability_does_vary(self, haz):
        assert haz["county"]["social_vulnerability"]["varies_by_tract"] is True

    def test_resilience_is_flagged_as_county_resolution(self, haz):
        assert "community_resilience" in haz["county_resolution_fields"]

    def test_the_renderer_says_resilience_is_a_county_figure(self):
        assert "Community resilience is a county figure" in APP_JS

    def test_the_zip_figure_is_labelled_a_sample_not_an_average(self, haz):
        joined = " ".join(haz["caveats"]).lower()
        assert "not an average over it" in joined

    def test_the_index_labels_the_zip_selector_as_a_centroid_tract(self):
        assert "tract at the ZIP centroid" in INDEX


class TestProvenance:
    def test_the_source_url_is_fema(self, haz):
        assert haz["source_url"].startswith("https://www.fema.gov/")

    def test_the_query_url_matches_the_station(self, haz):
        from analyzer.sources import risk

        assert haz["query_url"] == risk.NRI_QUERY

    def test_the_data_version_is_stamped(self, haz):
        assert haz["nri_version"], "an unstamped snapshot cannot be told from a stale one"

    def test_the_retrieval_date_is_stamped(self, haz):
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", haz["retrieved"])

    def test_every_zip_carries_its_tract(self, haz):
        for code, entry in haz["zips"].items():
            assert re.fullmatch(r"\d{11}", entry["tract_fips"]), code

    def test_the_page_renders_the_version_and_retrieval_date(self):
        assert "data version ${H.nri_version}" in APP_JS
        assert "Retrieved ${H.retrieved}" in APP_JS

    def test_every_profile_target_zip_got_a_hazard_entry(self, haz):
        expected = set(PROFILE["market_velocity"]["target_zips"])
        assert set(haz["zips"]) == expected


class TestTheCheckerCatchesDrift:
    def test_check_passes_on_the_committed_snapshot(self, capsys):
        b = load_builder()

        assert b.check(SNAPSHOT) == 0

    def test_check_fails_when_the_key_is_missing(self):
        b = load_builder()

        assert b.check({}) == 1

    def test_check_fails_when_the_codes_drift_from_the_profile(self):
        b = load_builder()

        mutated = json.loads(json.dumps(SNAPSHOT))
        mutated["hazards"]["hazard_codes"] = ["WFIR"]
        assert b.check(mutated) == 1

    def test_check_fails_when_a_county_hazard_is_absent(self):
        b = load_builder()

        mutated = json.loads(json.dumps(SNAPSHOT))
        mutated["hazards"]["county"]["hazards"].pop("WFIR")
        assert b.check(mutated) == 1

    def test_check_does_not_hit_the_network(self, no_network):
        """`--check` is a CI job. A federal dataset being republished is not a build
        failure, and a check that phones FEMA would make it one."""
        b = load_builder()

        assert b.check(SNAPSHOT) == 0
