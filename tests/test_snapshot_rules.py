"""The static page must not disagree with the engine.

Before tools/build_snapshot.py existed, app.js carried its own hand-typed copy of the
scoring rules and had drifted into giving the opposite verdict on a real house: it treated
an HOA over $100/mo as disqualifying (the engine deducts 25) and gave an aging roof and
HVAC no penalty at all (the engine deducts 40 between them). 606 Andre Ct read as a
confident TAKE on the page and 52 WATCH from the engine.

These tests are the reason that cannot happen twice. They assert three things:

  1. the committed data.json rules are what the current profile compiles to,
  2. every number in that block traces back to the profile the engine loads,
  3. app.js reads the compiled rules instead of restating them.

Test 3 is the load-bearing one. Tests 1 and 2 only prove the data is right; test 3 proves
the page actually uses it.
"""

from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path

import pytest

from analyzer.core.profile import default_profile_path

REPO = Path(__file__).resolve().parents[1]
SNAPSHOT = REPO / "data.json"
APP_JS = REPO / "app.js"


@pytest.fixture(scope="module")
def rules() -> dict:
    return json.loads(SNAPSHOT.read_text())["rules"]


@pytest.fixture(scope="module")
def js() -> str:
    return APP_JS.read_text()


@pytest.fixture(scope="module")
def profile_raw() -> dict:
    with open(default_profile_path(), "rb") as fh:
        return tomllib.load(fh)


class TestSnapshotIsCurrent:
    def test_build_script_is_idempotent(self, rules):
        """data.json must already equal what the builder would produce.

        Fails when someone edits buyer_profile.toml and forgets to rebuild, which is the
        realistic way this drifts. Fix: python tools/build_snapshot.py
        """
        import sys

        sys.path.insert(0, str(REPO))
        from tools.build_snapshot import build_rules

        with open(REPO / "buyer_profile.toml", "rb") as fh:
            expected = build_rules(tomllib.load(fh))
        assert rules == expected, (
            "data.json is stale. Run `python tools/build_snapshot.py` and commit it."
        )

    def test_builder_preserves_the_rest_of_the_snapshot(self):
        """The builder augments; it must not clobber market data or GeoJSON."""
        snapshot = json.loads(SNAPSHOT.read_text())
        for key in ("global", "submarkets", "geojson", "poi", "drivetime"):
            assert key in snapshot, f"builder dropped {key!r} from data.json"


class TestRulesMatchProfile:
    def test_verdict_bands(self, rules, profile_raw):
        v = profile_raw["verdict"]
        assert rules["verdict"]["take_min"] == v["take_min"]
        assert rules["verdict"]["watch_min"] == v["watch_min"]
        assert rules["verdict"]["unevaluated_score"] == v["unevaluated_score"]

    def test_every_deduction_weight_traces_to_the_profile(self, rules, profile_raw):
        penalties = profile_raw["preferences"]["penalties"]
        schools = profile_raw["schools"]["penalties"]
        expected = {
            "hoa": penalties["hoa_over_max"],
            "beds": penalties["beds_under"],
            "sqft": penalties["sqft_under"],
            "fiber": penalties["no_fiber"],
            "garage": penalties["garage_under"],
            "baths": penalties["baths_under"],
            "school_below_min": schools["below_min"],
            "school_well_below_min": schools["well_below_min"],
            "school_zone_of_choice": schools["zone_of_choice"],
        }
        actual = {d["id"]: d["points"] for d in rules["deductions"]}
        assert actual == expected

    def test_deduction_thresholds_trace_to_the_profile(self, rules, profile_raw):
        pref = profile_raw["preferences"]
        by_id = {d["id"]: d for d in rules["deductions"]}
        assert by_id["hoa"]["threshold"] == pref["max_hoa_monthly"]
        assert by_id["beds"]["threshold"] == pref["min_beds"]
        assert by_id["baths"]["threshold"] == pref["min_baths"]
        assert by_id["sqft"]["threshold"] == pref["min_sqft"]
        assert by_id["garage"]["threshold"] == pref["min_garage_spaces"]

    def test_school_deductions_trace_to_the_profile(self, rules, profile_raw):
        """School quality is scored in the engine, so the compiled rules must expose it."""
        schools = profile_raw["schools"]
        by_id = {d["id"]: d for d in rules["deductions"]}
        assert by_id["school_below_min"]["threshold"] == schools["min_proficiency_pct"]
        assert by_id["school_below_min"]["points"] == schools["penalties"]["below_min"]
        assert by_id["school_well_below_min"]["threshold"] == (
            schools["min_proficiency_pct"] - schools["well_below_margin_pct"]
        )
        assert by_id["school_well_below_min"]["points"] == schools["penalties"]["well_below_min"]
        assert by_id["school_zone_of_choice"]["points"] == schools["penalties"]["zone_of_choice"]
        assert by_id["school_zone_of_choice"]["compare"] == "is_true"

    def test_capex_tiers_trace_to_the_profile(self, rules, profile_raw):
        capex = profile_raw["capital_expenses"]
        pen = capex["penalties"]
        by_id = {c["id"]: c for c in rules["capital_expenses"]}
        assert by_id["roof"]["due_age"] == capex["roof_due_age"]
        assert by_id["roof"]["overdue_age"] == capex["roof_overdue_age"]
        assert by_id["roof"]["due_points"] == pen["roof_due"]
        assert by_id["roof"]["overdue_points"] == pen["roof_overdue"]
        assert by_id["hvac"]["due_age"] == capex["hvac_due_age"]
        assert by_id["hvac"]["overdue_age"] == capex["hvac_overdue_age"]
        assert by_id["hvac"]["due_points"] == pen["hvac_due"]
        assert by_id["hvac"]["overdue_points"] == pen["hvac_overdue"]

    def test_capex_bands_match_the_engine(self, rules):
        """The builder duplicates capex.py's dollar bands. Pin them together."""
        from analyzer.core.capex import hvac_cost, roof_cost

        by_id = {c["id"]: c for c in rules["capital_expenses"]}
        for sqft in (1200, 1600, 2000, 2400, 3000):
            for cid, fn in (("roof", roof_cost), ("hvac", hvac_cost)):
                bands = by_id[cid]["bands"]
                band = next(
                    (b for b in bands if b["max_sqft"] is not None and sqft <= b["max_sqft"]),
                    bands[-1],
                )
                assert (band["low"], band["high"]) == fn(sqft), (
                    f"{cid} band for {sqft} sqft drifted from analyzer/core/capex.py"
                )

    def test_hoa_is_a_deduction_not_a_hard_fail(self, rules):
        """The specific bug this whole mechanism was built to kill.

        An HOA over the ceiling is expensive, not disqualifying. The page used to zero the
        score and print PASS.
        """
        assert any(d["id"] == "hoa" for d in rules["deductions"])
        assert not any("hoa" in h["id"] for h in rules["hard_fails"])

    def test_hard_fails_are_only_the_three_agreed_ones(self, rules, profile_raw):
        assert {h["id"] for h in rules["hard_fails"]} == {
            "commute",
            "water_sewer",
            "flood",
        }
        commute = next(h for h in rules["hard_fails"] if h["id"] == "commute")
        assert commute["threshold"] == profile_raw["hard_fails"]["max_commute_min"]

    def test_build_year_is_a_caveat_and_deducts_nothing(self, rules, profile_raw):
        """Age alone is a prompt to look closer, never evidence of a worse house."""
        assert rules["caveats"]["preferred_year_built_min"] == (
            profile_raw["caveats"]["preferred_year_built_min"]
        )
        ids = {d["id"] for d in rules["deductions"]}
        assert "year_built" not in ids
        assert not any("year" in c["id"] for c in rules["capital_expenses"])


class TestAppJsUsesCompiledRules:
    """Data being correct is worthless if the page ignores it."""

    def test_scorer_reads_the_rules_block(self, js):
        assert "DATA.rules" in js, "app.js does not read the compiled rules at all"

    @pytest.mark.parametrize(
        "literal, why",
        [
            ("score=0; flags.push(`HOA", "the old HOA hard-fail branch"),
            ("exceeds your $100/mo deal-breaker", "the old HOA deal-breaker copy"),
            ("score>=50?", "the old hardcoded 50 WATCH floor, profile says 45"),
        ],
    )
    def test_old_hardcoded_rules_are_gone(self, js, literal, why):
        assert literal not in js, f"app.js still contains {why}"

    def test_no_bare_threshold_literals_in_the_scorer(self, js):
        """Catches a future edit that retypes a threshold instead of reading the profile.

        Scoped to the scorer function only, since thresholds legitimately appear elsewhere
        (chart axes, map zoom levels, the rent-vs-buy model).
        """
        start = js.index("function updateProperty(){")
        end = js.index("/* ---------------- RUNWAY ---------------- */")
        body = js[start:end]
        # Strip comments first: the explanatory comment names the old numbers on purpose.
        body = re.sub(r"/\*.*?\*/", "", body, flags=re.S)
        body = re.sub(r"//[^\n]*", "", body)
        for banned in ("1400", "max_hoa", "> 100", ">100"):
            assert banned not in body, (
                f"{banned!r} is hardcoded in updateProperty; read it from DATA.rules"
            )
