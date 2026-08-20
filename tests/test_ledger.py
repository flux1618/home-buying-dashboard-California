"""The saved-property ledger.

What is worth testing here is not "does an INSERT insert". It is the four claims the package
makes that a reader would otherwise have to take on faith:

1. **The engine does not know the ledger exists.** Asserted by import inspection, not by
   convention, because ADR 0002 is only worth having if it is enforced.
2. **The record is append-only.** Asserted against the database, so the guarantee holds for
   any client including a `sqlite3` shell, not just for callers who use `Ledger`.
3. **Two houses are never silently merged, and one house is never silently split** -- except
   in the one case where splitting is the deliberate choice (a degraded geocoder).
4. **A score delta is only reported as comparable when it actually is.** The engine version
   and the profile fingerprint are the two things that can change a score without the house
   changing at all.

The suite never touches the real database: `no_real_ledger` in conftest.py is autouse, and
these tests use `:memory:` on top of that.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

from ledger import Ledger, connect, database_path
from ledger.db import SCHEMA_VERSION, migrate
from ledger.repo import LedgerError, PropertyNotFound, profile_fingerprint, property_key, rows_to_csv

REPO = Path(__file__).resolve().parents[1]


# =============================================================================
# Support
# =============================================================================


def document(
    *,
    price=268000.0,
    score=74,
    verdict="WATCH",
    matched="606 ANDRE CT, SPARTANBURG, SC, 29301",
    requested="606 Andre Ct, Spartanburg, SC 29301",
    engine="0.2.0",
    lat=34.943051,
    capped=False,
):
    """A minimal document with the keys the ledger actually reads.

    Deliberately not the real pipeline output. The ledger's contract is a handful of keys;
    building these from a live run would make a change to an unrelated part of the document
    break the storage tests, which teaches nothing.
    """
    return {
        "engine_version": engine,
        "analyzed_at": "2026-08-19T12:00:00+00:00",
        "input": {"price": price, "sqft": 1650, "year_built": 1998},
        "cost": {"piti": 1718.0, "front_end_dti": 0.0507, "cash_to_close": 89032.0},
        "score": {
            "value": score, "verdict": verdict,
            "score_pinned": False, "score_capped": capped,
        },
        "degraded_sources": [],
        "location": {
            "requested_address": requested,
            "matched_address": matched,
            "latitude": lat,
            "longitude": -81.97665,
            "county_fips": "45083",
        },
    }


@dataclass(frozen=True)
class FakeProfile:
    """Stands in for BuyerProfile.

    A frozen dataclass rather than a bag of attributes, because that is what the real profile
    is and `profile_fingerprint` goes through `dataclasses.asdict`. A duck-typed stand-in
    would pass these tests through a code path production never uses -- and the one test that
    fingerprints the real `load_profile()` output exists for the same reason.
    """

    name: str = "test"
    mortgage_rate: float = 0.0667
    gross_annual_income: float = 406480.0
    penalties: tuple[tuple[str, int], ...] = ()


@pytest.fixture
def ledger():
    conn = connect(":memory:")
    try:
        yield Ledger(conn)
    finally:
        conn.close()


@pytest.fixture
def profile():
    return FakeProfile()


# =============================================================================
# The boundary
# =============================================================================


class TestTheEngineDoesNotKnowTheLedgerExists:
    """ADR 0002 says the core is pure. Storage is nothing but I/O, so the dependency must
    run one way only. Checked by reading the source, because an import added in a hurry is
    exactly how this rule dies."""

    def test_no_module_under_analyzer_imports_the_ledger(self):
        offenders = []
        for path in (REPO / "analyzer").rglob("*.py"):
            source = path.read_text()
            for line in source.splitlines():
                stripped = line.strip()
                if stripped.startswith(("import ledger", "from ledger")):
                    offenders.append(f"{path.relative_to(REPO)}: {stripped}")
        # analyzer/cli.py is allowed one deferred import inside the --save branch: the CLI
        # is a door, not the engine. It is indented, so the checks above skip it only if it
        # is genuinely inside a function -- which is why this asserts on the stripped line
        # appearing at module level in any file.
        module_level = [
            f"{path.relative_to(REPO)}"
            for path in (REPO / "analyzer").rglob("*.py")
            for line in path.read_text().splitlines()
            if line.startswith(("import ledger", "from ledger"))
        ]
        assert module_level == [], f"the engine imports the ledger at module level: {module_level}"
        assert all("cli.py" in o for o in offenders), f"only the CLI door may reach the ledger: {offenders}"

    def test_the_ledger_does_not_import_the_engine(self):
        """The reverse direction matters too: a store that imports the scoring core could be
        tempted to recompute on read, which would make a stored score a moving target."""
        for path in (REPO / "ledger").rglob("*.py"):
            for line in path.read_text().splitlines():
                assert not line.startswith(("import analyzer", "from analyzer")), (
                    f"{path.name} imports the engine: {line.strip()}"
                )


# =============================================================================
# Schema
# =============================================================================


class TestSchema:
    def test_a_fresh_database_is_at_the_current_version(self, ledger):
        assert ledger.conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION

    def test_migrating_twice_changes_nothing(self, ledger):
        """Idempotence is the whole promise of a version gate. If the second call raised or
        re-ran a CREATE, every container restart would be a coin flip."""
        assert migrate(ledger.conn) == SCHEMA_VERSION
        assert migrate(ledger.conn) == SCHEMA_VERSION

    def test_foreign_keys_are_actually_on(self, ledger):
        """SQLite defaults this OFF. Without the pragma, property_key is a comment."""
        assert ledger.conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        with pytest.raises(sqlite3.IntegrityError):
            ledger.conn.execute(
                "INSERT INTO analyses (property_key, analyzed_at, engine_version, "
                "profile_fingerprint, price, document) VALUES ('ghost', 'now', '0', 'x', 1, '{}')"
            )

    def test_the_default_path_is_outside_the_repository(self, monkeypatch, tmp_path):
        """A default of ./ledger.db is one `git add -A` from publishing every address and
        financial figure in the file, and this repository is public."""
        monkeypatch.delenv("HBA_DATA_DIR", raising=False)
        monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
        resolved = database_path()
        assert REPO not in resolved.parents
        assert resolved.name == "ledger.db"

    def test_an_explicit_path_wins_over_the_environment(self, monkeypatch, tmp_path):
        monkeypatch.setenv("HBA_DATA_DIR", str(tmp_path / "env"))
        assert database_path(tmp_path / "explicit.db") == tmp_path / "explicit.db"


# =============================================================================
# Append-only
# =============================================================================


class TestTheRecordIsAppendOnly:
    """Enforced by triggers rather than by a docstring, so the guarantee survives a client
    that never imports this package."""

    def test_an_analysis_cannot_be_updated(self, ledger, profile):
        ledger.save_analysis(document(), profile=profile)
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            ledger.conn.execute("UPDATE analyses SET price = 1 WHERE id = 1")

    def test_an_analysis_cannot_be_deleted_while_its_house_exists(self, ledger, profile):
        ledger.save_analysis(document(), profile=profile)
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            ledger.conn.execute("DELETE FROM analyses WHERE id = 1")

    def test_a_journal_entry_cannot_be_edited(self, ledger, profile):
        saved = ledger.save_analysis(document(), profile=profile)
        ledger.add_journal_entry(kind="observation", body="dated kitchen", key=saved["property"]["key"])
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            ledger.conn.execute("UPDATE journal SET body = 'nice kitchen' WHERE id = 1")

    def test_re_analyzing_appends_and_keeps_the_old_row(self, ledger, profile):
        first = ledger.save_analysis(document(price=268000), profile=profile)
        second = ledger.save_analysis(document(price=259000), profile=profile)
        assert first["analysis_id"] != second["analysis_id"]
        history = ledger.history(first["property"]["key"])
        assert [row["price"] for row in history] == [268000.0, 259000.0]

    def test_the_cascade_still_lets_a_typo_be_deleted(self, ledger, profile):
        """The DELETE triggers are guarded on the parent still existing. Without that guard,
        ON DELETE CASCADE would trip the trigger and no property could ever be removed --
        including one saved from a mistyped address that nobody ever looked at."""
        saved = ledger.save_analysis(document(), profile=profile)
        result = ledger.forget_property(saved["property"]["key"])
        assert result["forgotten"] is True
        assert ledger.conn.execute("SELECT COUNT(*) FROM analyses").fetchone()[0] == 0

    def test_forgetting_refuses_once_there_is_a_record(self, ledger, profile):
        saved = ledger.save_analysis(document(), profile=profile)
        key = saved["property"]["key"]
        ledger.add_journal_entry(kind="decision", body="passing, no garage", key=key)
        with pytest.raises(LedgerError, match="rather than erasing"):
            ledger.forget_property(key)

    def test_an_automatic_status_line_does_not_block_forgetting(self, ledger, profile):
        """A status change is bookkeeping the ledger wrote itself. Only entries a human
        wrote count as a record worth protecting."""
        saved = ledger.save_analysis(document(), profile=profile)
        key = saved["property"]["key"]
        ledger.set_status(key, "passed")
        assert ledger.forget_property(key)["forgotten"] is True


# =============================================================================
# Identity
# =============================================================================


class TestIdentity:
    def test_punctuation_and_case_do_not_create_a_second_house(self):
        assert property_key("606 Andre Ct, Spartanburg, SC 29301", resolved=True) == property_key(
            "606 ANDRE CT  SPARTANBURG, SC, 29301", resolved=True
        )

    def test_an_unresolved_key_can_never_collide_with_a_resolved_one(self):
        """The point of the prefix. A geocoder outage must not be able to record two
        different houses as the same house -- a visible duplicate is a nuisance, a silent
        merge is a wrong decision."""
        assert property_key("606 Andre Ct", resolved=True) != property_key("606 Andre Ct", resolved=False)

    def test_a_document_with_no_coordinates_is_recorded_as_unresolved(self, ledger, profile):
        saved = ledger.save_analysis(document(matched=None, lat=None), profile=profile)
        assert saved["property"]["resolved"] is False
        assert saved["property"]["key"].startswith("unresolved:")

    def test_the_key_comes_from_the_geocoder_not_from_what_was_typed(self, ledger, profile):
        saved = ledger.save_analysis(document(), profile=profile)
        assert saved["property"]["key"] == "606 ANDRE CT SPARTANBURG SC 29301"
        assert saved["property"]["raw_input"] == "606 Andre Ct, Spartanburg, SC 29301"

    def test_an_empty_address_is_refused_rather_than_keyed_as_blank(self):
        with pytest.raises(LedgerError):
            property_key("   ,,,  ", resolved=True)

    def test_a_document_with_no_address_at_all_is_refused(self, ledger, profile):
        broken = document()
        broken["location"] = {}
        with pytest.raises(LedgerError, match="no address"):
            ledger.save_analysis(broken, profile=profile)

    def test_the_first_thing_you_typed_is_never_overwritten(self, ledger, profile):
        """raw_input is a historical fact too. The second save came from a different
        spelling, and the record of what you originally searched for still stands."""
        ledger.save_analysis(document(), profile=profile)
        ledger.save_analysis(document(requested="606 andre court spartanburg"), profile=profile)
        assert ledger.list_properties()[0]["raw_input"] == "606 Andre Ct, Spartanburg, SC 29301"


# =============================================================================
# Comparability -- the honest part
# =============================================================================


class TestAScoreDeltaIsOnlyReportedWhenItMeansSomething:
    def test_no_diff_on_a_first_analysis(self, ledger, profile):
        assert ledger.save_analysis(document(), profile=profile)["diff"] is None

    def test_a_price_change_is_comparable(self, ledger, profile):
        ledger.save_analysis(document(price=268000), profile=profile)
        diff = ledger.save_analysis(document(price=259000), profile=profile)["diff"]
        assert diff["price_delta"] == -9000.0
        assert diff["price_pct"] == pytest.approx(-3.358, abs=0.01)
        assert diff["comparable"] is True

    def test_an_engine_change_makes_the_score_delta_incomparable(self, ledger, profile):
        """Same house, same price, four points lower -- because the rules changed. Reporting
        that as a score drop would invite a conclusion about the market that the data does
        not support."""
        ledger.save_analysis(document(score=74, engine="0.2.0"), profile=profile)
        diff = ledger.save_analysis(document(score=70, engine="0.3.0"), profile=profile)["diff"]
        assert diff["score_delta"] == -4
        assert diff["comparable"] is False
        assert any("engine changed" in reason for reason in diff["incomparable_because"])

    def test_a_profile_change_makes_the_score_delta_incomparable(self, ledger):
        ledger.save_analysis(document(score=74), profile=FakeProfile(mortgage_rate=0.0667))
        diff = ledger.save_analysis(document(score=68), profile=FakeProfile(mortgage_rate=0.075))["diff"]
        assert diff["comparable"] is False
        assert any("profile changed" in reason for reason in diff["incomparable_because"])

    def test_a_price_change_is_still_reported_when_the_score_is_not(self, ledger, profile):
        """The price is a fact about the house regardless of what our code did, so it stays
        usable even when the score does not."""
        ledger.save_analysis(document(price=268000, engine="0.2.0"), profile=profile)
        diff = ledger.save_analysis(document(price=250000, engine="0.9.0"), profile=profile)["diff"]
        assert diff["price_delta"] == -18000.0
        assert diff["comparable"] is False

    def test_a_verdict_flip_is_flagged(self, ledger, profile):
        ledger.save_analysis(document(verdict="WATCH"), profile=profile)
        diff = ledger.save_analysis(document(verdict="PASS", score=52), profile=profile)["diff"]
        assert diff["verdict_changed"] is True
        assert (diff["verdict_from"], diff["verdict_to"]) == ("WATCH", "PASS")

    def test_the_same_profile_produces_the_same_fingerprint(self):
        assert profile_fingerprint(FakeProfile()) == profile_fingerprint(FakeProfile())

    def test_any_changed_field_changes_the_fingerprint(self):
        """Income, rate, penalty weights -- all of them. A fingerprint that only covered the
        obvious fields would silently call two different rulebooks the same."""
        base = profile_fingerprint(FakeProfile())
        assert profile_fingerprint(FakeProfile(penalties=(("baths_under", 8),))) != base
        assert profile_fingerprint(FakeProfile(gross_annual_income=1.0)) != base

    def test_a_real_buyer_profile_can_be_fingerprinted(self):
        """The frozen dataclass path, not just the duck-typed one. asdict() has to cope with
        the nested Anchor tuple, and a TypeError here would only show up on first save."""
        from analyzer.core.profile import load_profile

        assert len(profile_fingerprint(load_profile())) == 12


# =============================================================================
# Stored, not recomputed
# =============================================================================


class TestWhatIsStoredIsWhatWasSaid:
    def test_the_whole_document_round_trips(self, ledger, profile):
        saved = ledger.save_analysis(document(), profile=profile)
        stored = ledger.latest_document(saved["property"]["key"])
        assert stored["score"]["value"] == 74
        assert stored["input"]["sqft"] == 1650

    def test_the_indexed_columns_are_derived_from_the_document(self, ledger, profile):
        """The caller cannot pass a price that disagrees with the document, because there is
        no parameter for it. If there were, the list view and the detail view would
        eventually disagree about the same house."""
        saved = ledger.save_analysis(document(price=311500), profile=profile)
        row = ledger.conn.execute("SELECT price, score, verdict FROM analyses WHERE id = ?", (saved["analysis_id"],)).fetchone()
        assert (row["price"], row["score"], row["verdict"]) == (311500.0, 74, "WATCH")

    def test_latest_document_returns_the_newest(self, ledger, profile):
        ledger.save_analysis(document(price=268000), profile=profile)
        saved = ledger.save_analysis(document(price=259000), profile=profile)
        assert ledger.latest_document(saved["property"]["key"])["input"]["price"] == 259000.0

    def test_a_capped_score_stays_marked_as_capped(self, ledger, profile):
        """A capped score means the house could not be scored on confirmed facts. Losing that
        flag on the way into storage would turn a caveat into a measurement."""
        saved = ledger.save_analysis(document(capped=True), profile=profile)
        assert saved["property"]["latest"]["score_capped"] is True

    def test_asking_for_a_house_that_was_never_saved_raises(self, ledger):
        with pytest.raises(PropertyNotFound):
            ledger.latest_document("404 NOWHERE ST")


# =============================================================================
# Status and journal
# =============================================================================


class TestStatusAndJournal:
    def test_a_status_change_always_writes_a_journal_entry(self, ledger, profile):
        """Not optional. A status is a current value with no memory; "why did we pass on
        this one" is answerable only if the transition was recorded when it happened."""
        key = ledger.save_analysis(document(), profile=profile)["property"]["key"]
        ledger.set_status(key, "passed", note="septic tank")
        entries = ledger.journal(key=key)
        assert len(entries) == 1
        assert entries[0]["kind"] == "status"
        assert entries[0]["body"] == "candidate -> passed: septic tank"

    def test_a_status_change_without_a_note_is_still_recorded(self, ledger, profile):
        key = ledger.save_analysis(document(), profile=profile)["property"]["key"]
        ledger.set_status(key, "touring")
        assert ledger.journal(key=key)[0]["body"] == "candidate -> touring"

    def test_an_unknown_status_is_refused(self, ledger, profile):
        key = ledger.save_analysis(document(), profile=profile)["property"]["key"]
        with pytest.raises(LedgerError, match="unknown status"):
            ledger.set_status(key, "maybe")

    def test_status_on_a_missing_house_raises_not_found(self, ledger):
        with pytest.raises(PropertyNotFound):
            ledger.set_status("404 NOWHERE ST", "passed")

    def test_an_outcome_can_close_an_assumption(self, ledger, profile):
        """The pair that makes this a decision journal rather than a notes field."""
        key = ledger.save_analysis(document(), profile=profile)["property"]["key"]
        assumption = ledger.add_journal_entry(kind="assumption", body="millage stays 280", key=key)
        assert ledger.open_assumptions()[0]["id"] == assumption["id"]
        ledger.add_journal_entry(kind="outcome", body="millage went to 284", key=key, resolves=assumption["id"])
        assert ledger.open_assumptions() == []

    def test_an_outcome_cannot_resolve_a_status_line(self, ledger, profile):
        """The thing being closed should be a claim someone made, not bookkeeping."""
        key = ledger.save_analysis(document(), profile=profile)["property"]["key"]
        ledger.set_status(key, "touring")
        status_entry = ledger.journal(key=key)[0]["id"]
        with pytest.raises(LedgerError, match="only an assumption"):
            ledger.add_journal_entry(kind="outcome", body="x", key=key, resolves=status_entry)

    def test_resolving_an_entry_that_does_not_exist_is_refused(self, ledger):
        with pytest.raises(LedgerError, match="no such entry"):
            ledger.add_journal_entry(kind="outcome", body="x", resolves=999)

    def test_a_general_entry_needs_no_property(self, ledger):
        entry = ledger.add_journal_entry(kind="observation", body="rates moved to 6.9 this week")
        assert entry["property_key"] is None
        assert ledger.journal()[0]["id"] == entry["id"]

    def test_open_assumptions_includes_general_ones(self, ledger):
        """`NOT IN` over a nullable subquery returns nothing at all if any row has a NULL, so
        this is the regression test for filtering the subquery rather than the outer rows."""
        ledger.add_journal_entry(kind="assumption", body="we can close by March")
        ledger.add_journal_entry(kind="observation", body="unrelated")
        assert [e["body"] for e in ledger.open_assumptions()] == ["we can close by March"]

    def test_an_empty_body_is_refused(self, ledger):
        with pytest.raises(LedgerError, match="needs a body"):
            ledger.add_journal_entry(kind="observation", body="   ")

    def test_an_unknown_kind_is_refused(self, ledger):
        with pytest.raises(LedgerError, match="unknown kind"):
            ledger.add_journal_entry(kind="vibe", body="feels good")

    def test_a_journal_entry_for_a_missing_house_raises(self, ledger):
        with pytest.raises(PropertyNotFound):
            ledger.add_journal_entry(kind="observation", body="x", key="404 NOWHERE ST")


# =============================================================================
# Reading the shortlist
# =============================================================================


class TestTheShortlist:
    def test_it_ranks_by_score(self, ledger, profile):
        ledger.save_analysis(document(matched="A ST", requested="A ST", score=61), profile=profile)
        ledger.save_analysis(document(matched="B ST", requested="B ST", score=88), profile=profile)
        assert [r["key"] for r in ledger.list_properties()] == ["B ST", "A ST"]

    def test_archived_houses_are_hidden_unless_asked_for(self, ledger, profile):
        key = ledger.save_analysis(document(), profile=profile)["property"]["key"]
        ledger.set_status(key, "archived")
        assert ledger.list_properties() == []
        assert len(ledger.list_properties(include_archived=True)) == 1

    def test_a_passed_house_still_shows_because_it_is_data(self, ledger, profile):
        key = ledger.save_analysis(document(), profile=profile)["property"]["key"]
        ledger.set_status(key, "passed")
        assert len(ledger.list_properties()) == 1

    def test_filtering_by_status(self, ledger, profile):
        first = ledger.save_analysis(document(matched="A ST", requested="A ST"), profile=profile)
        ledger.save_analysis(document(matched="B ST", requested="B ST"), profile=profile)
        ledger.set_status(first["property"]["key"], "touring")
        assert [r["key"] for r in ledger.list_properties(status="touring")] == ["A ST"]

    def test_counts_come_back_with_each_row(self, ledger, profile):
        key = ledger.save_analysis(document(), profile=profile)["property"]["key"]
        ledger.save_analysis(document(price=250000), profile=profile)
        ledger.add_journal_entry(kind="observation", body="dated kitchen", key=key)
        row = ledger.list_properties()[0]
        assert (row["analysis_count"], row["journal_count"]) == (2, 1)

    def test_stats_counts_everything(self, ledger, profile):
        key = ledger.save_analysis(document(), profile=profile)["property"]["key"]
        ledger.add_journal_entry(kind="assumption", body="rates hold", key=key)
        stats = ledger.stats()
        assert stats["properties"] == 1
        assert stats["analyses"] == 1
        assert stats["open_assumptions"] == 1
        assert stats["schema_version"] == SCHEMA_VERSION

    def test_csv_export_flattens_the_latest_analysis(self, ledger, profile):
        ledger.save_analysis(document(), profile=profile)
        csv_text = rows_to_csv(ledger.list_properties())
        header, row = csv_text.splitlines()[0], csv_text.splitlines()[1]
        assert "score" in header and "piti" in header
        assert "74" in row
        # A cell containing the whole document would make the file useless in a spreadsheet.
        assert "engine_version" not in header

    def test_csv_export_survives_a_house_with_no_analysis(self, ledger):
        """Possible only through a direct insert today, but a header-only crash on an empty
        `latest` would be a bad afternoon."""
        ledger.conn.execute(
            "INSERT INTO properties (key, raw_input, resolved, status, created_at, updated_at) "
            "VALUES ('X ST', 'X ST', 1, 'candidate', 'now', 'now')"
        )
        assert "X ST" in rows_to_csv(ledger.list_properties())


# =============================================================================
# The doors
# =============================================================================


class TestTheSaveLineTellsTheTruth:
    """Two bugs found by running the tool rather than the tests.

    Both were in the three lines the user reads after a save, which is the worst place for a
    bug to hide: everything above them was correct.
    """

    def test_the_number_counts_this_house_not_every_house(self, ledger, profile):
        """The first save of a third house was printing "#3".

        `analysis_id` is a global rowid. Next to an address, a reader takes the number to mean
        "the third time we looked at this house", which is a different and wrong statement.
        """
        ledger.save_analysis(document(price=268000), profile=profile)
        ledger.save_analysis(document(price=259000), profile=profile)
        other = ledger.save_analysis(
            document(price=315000, matched="900 OTHER RD SPARTANBURG SC 29302"), profile=profile
        )
        assert other["analysis_number"] == 1, "a new house's first analysis is its first"
        assert other["analysis_id"] == 3, "the global id is still 3, and still useful"

    def test_a_reanalysis_increments_the_house_number(self, ledger, profile):
        first = ledger.save_analysis(document(price=268000), profile=profile)
        second = ledger.save_analysis(document(price=259000), profile=profile)
        assert (first["analysis_number"], second["analysis_number"]) == (1, 2)

    def test_the_geocoder_substituting_a_street_is_recoverable_from_the_row(self, ledger, profile):
        """"115 Chestnut Ridge Dr" came back as "115 Chestnut St" in a different ZIP.

        The geocoder matches fuzzily and will hand back a different street for an address it
        cannot find. Every figure in the analysis is then correct for a house nobody asked
        about. The raw input has to survive so the substitution is provable after the fact --
        this is the same reason `save_analysis` never overwrites `raw_input`.
        """
        saved = ledger.save_analysis(
            document(price=315000, matched="115 CHESTNUT ST SPARTANBURG SC 29302"),
            profile=profile,
            raw_input="115 Chestnut Ridge Dr, Spartanburg, SC 29301",
        )
        assert saved["property"]["raw_input"] == "115 Chestnut Ridge Dr, Spartanburg, SC 29301"
        assert "CHESTNUT RIDGE" not in saved["property"]["key"]

    def test_the_cli_says_so_out_loud(self):
        """A silent substitution is the dangerous one, so the renderer has to speak."""
        from analyzer.cli import _street_of

        assert _street_of("115 Chestnut Ridge Dr, Spartanburg, SC 29301") == "115 CHESTNUT RIDGE DR"
        assert _street_of("606 Andre Ct, Spartanburg, SC 29301") == "606 ANDRE CT"

    def test_a_matching_address_does_not_trigger_the_warning(self, capsys):
        """Case and punctuation differences are not substitutions, and crying wolf costs the
        warning its meaning."""
        from analyzer.cli import render_ledger

        render_ledger(
            {
                "key": "606 ANDRE CT SPARTANBURG SC 29301",
                "analysis_id": 1,
                "analysis_number": 1,
                "requested": "606 Andre Ct, Spartanburg, SC 29301",
                "first_time": True,
                "diff": None,
            }
        )
        out = capsys.readouterr().out
        assert "different address" not in out

    def test_a_substituted_address_does(self, capsys):
        from analyzer.cli import render_ledger

        render_ledger(
            {
                "key": "115 CHESTNUT ST SPARTANBURG SC 29302",
                "analysis_id": 1,
                "analysis_number": 1,
                "requested": "115 Chestnut Ridge Dr, Spartanburg, SC 29301",
                "first_time": True,
                "diff": None,
            }
        )
        out = capsys.readouterr().out
        assert "different address" in out
        assert "CHESTNUT RIDGE" in out


class TestTheCliDoor:
    """Runs the CLI as a subprocess, which is the only way to test that argparse, the
    deferred import, and the exit codes all line up. The autouse socket guard does not block
    subprocesses, so these commands must not touch the network -- none of them analyze."""

    def run_cli(self, tmp_path, *args):
        return subprocess.run(
            [sys.executable, "-m", "ledger.cli", "--db", str(tmp_path / "l.db"), *args],
            cwd=REPO, capture_output=True, text=True, timeout=60,
            env={"PATH": "/usr/bin:/bin", "PYTHONPATH": str(REPO), "HOME": str(tmp_path)},
        )

    def test_an_empty_ledger_says_how_to_fill_it(self, tmp_path):
        result = self.run_cli(tmp_path, "list")
        assert result.returncode == 0
        assert "--save" in result.stdout

    def test_a_missing_key_exits_one_and_does_not_traceback(self, tmp_path):
        """A CLI that prints a stack trace when you mistype a key is telling you about its
        internals instead of your mistake."""
        result = self.run_cli(tmp_path, "show", "404 NOWHERE ST")
        assert result.returncode == 1
        assert "Traceback" not in result.stderr
        assert "No saved property" in result.stderr

    def test_an_unknown_status_exits_two_from_argparse(self, tmp_path):
        result = self.run_cli(tmp_path, "status", "X", "maybe")
        assert result.returncode == 2

    def test_where_does_not_create_the_database(self, tmp_path):
        """A command whose only job is to print a path should not have a side effect."""
        result = self.run_cli(tmp_path, "where")
        assert result.returncode == 0
        assert not (tmp_path / "l.db").exists()

    def test_json_output_is_parseable(self, tmp_path):
        result = self.run_cli(tmp_path, "--json", "stats")
        assert json.loads(result.stdout)["properties"] == 0

    def test_flags_work_on_either_side_of_the_subcommand(self, tmp_path):
        """`--json stats` and `stats --json` both have to work.

        This is a regression test for a real bug caught by dry-running the CI step: the
        subparser copies of these flags need `default=argparse.SUPPRESS`, or the subparser's
        own default silently overwrites a value parsed before the subcommand and the output
        comes back human-readable to a script that asked for JSON.
        """
        before = self.run_cli(tmp_path, "--json", "stats")
        after = self.run_cli(tmp_path, "stats", "--json")
        assert json.loads(before.stdout) == json.loads(after.stdout)

    def test_counts_are_not_printed_as_one_properties(self, tmp_path):
        """Cosmetic, and worth a line: this output is the first thing a reader sees."""
        self.run_cli(tmp_path, "note", "assumption", "rates hold")
        result = self.run_cli(tmp_path, "stats")
        assert "1 journal entry" in result.stdout
        assert "1 open assumption\x1b" in result.stdout or "1 open assumption" in result.stdout


class TestTheHttpDoor:
    """Translation only -- status codes and response shape. Scoring lives in test_scoring.py
    and storage behaviour is asserted above, against the ledger rather than through HTTP."""

    @pytest.fixture
    def client(self, monkeypatch, tmp_path):
        fastapi = pytest.importorskip("fastapi", reason="API is an optional extra")
        from fastapi.testclient import TestClient

        from analyzer.pipeline import Degradation, PipelineRun
        from service import app as service_module

        monkeypatch.setenv("HBA_DATA_DIR", str(tmp_path / "http"))

        def stub(address, price, **kwargs):
            return PipelineRun(
                document=document(price=price),
                degradations=[Degradation(station="broadband", reason="no api key")],
                stations_run=["geocode", "parcel", "flood"],
            )

        monkeypatch.setattr(service_module, "run", stub)
        return TestClient(service_module.create_app())

    KEY = "606%20ANDRE%20CT%20SPARTANBURG%20SC%2029301"

    def test_saving_returns_201_and_the_key(self, client):
        response = client.post("/ledger/properties", json={"address": "606 Andre Ct", "price": 268000})
        assert response.status_code == 201
        body = response.json()
        assert body["first_time"] is True
        assert body["diff"] is None
        assert body["property"]["key"] == "606 ANDRE CT SPARTANBURG SC 29301"

    def test_saving_twice_is_still_201_but_not_first_time(self, client):
        """201 either way, because a new *analysis* was created. `first_time` says which."""
        client.post("/ledger/properties", json={"address": "606 Andre Ct", "price": 268000})
        second = client.post("/ledger/properties", json={"address": "606 Andre Ct", "price": 259000})
        assert second.status_code == 201
        assert second.json()["first_time"] is False
        assert second.json()["diff"]["price_delta"] == -9000.0

    def test_the_detail_route_is_not_swallowed_by_the_path_converter(self, client):
        """`{key:path}` is greedy and FastAPI matches in declaration order, so /document and
        /status have to be declared first. This is the test that catches a reordering."""
        client.post("/ledger/properties", json={"address": "606 Andre Ct", "price": 268000})
        assert client.get(f"/ledger/properties/{self.KEY}").status_code == 200
        assert client.get(f"/ledger/properties/{self.KEY}/document").json()["score"]["value"] == 74
        assert client.patch(f"/ledger/properties/{self.KEY}/status", json={"status": "touring"}).status_code == 200

    def test_a_missing_house_is_404_not_500(self, client):
        response = client.get("/ledger/properties/404%20NOWHERE%20ST")
        assert response.status_code == 404
        assert response.json()["error"] == "property_not_in_ledger"

    def test_a_refused_operation_is_422_with_the_reason(self, client):
        """LedgerError messages name the rule and the alternative, so they are returned
        verbatim rather than replaced with a generic string."""
        client.post("/ledger/properties", json={"address": "606 Andre Ct", "price": 268000})
        response = client.post("/ledger/journal", json={"kind": "outcome", "body": "x", "resolves": 999})
        assert response.status_code == 422
        assert "no such entry" in response.json()["detail"]

    def test_forgetting_a_house_with_a_record_is_409(self, client):
        """Nothing about the request is malformed; it conflicts with the state. The fix is a
        different verb, not a corrected payload."""
        client.post("/ledger/properties", json={"address": "606 Andre Ct", "price": 268000})
        client.post("/ledger/journal", json={"kind": "decision", "body": "passing", "property_key": "606 ANDRE CT SPARTANBURG SC 29301"})
        assert client.delete(f"/ledger/properties/{self.KEY}").status_code == 409

    def test_an_unknown_status_is_rejected_by_the_schema(self, client):
        client.post("/ledger/properties", json={"address": "606 Andre Ct", "price": 268000})
        assert client.patch(f"/ledger/properties/{self.KEY}/status", json={"status": "maybe"}).status_code == 422

    def test_an_unexpected_field_is_rejected(self, client):
        """extra="forbid" throughout. A typo'd field silently ignored is a request the caller
        thinks succeeded as written."""
        assert client.post("/ledger/journal", json={"kind": "observation", "body": "x", "auther": "bao"}).status_code == 422

    def test_the_journal_and_open_routes_do_not_collide(self, client):
        client.post("/ledger/properties", json={"address": "606 Andre Ct", "price": 268000})
        client.post("/ledger/journal", json={"kind": "assumption", "body": "rates hold"})
        assert client.get("/ledger/journal").json()["count"] == 1
        assert client.get("/ledger/journal/open").json()["count"] == 1

    def test_stats_reports_the_schema_version_of_the_open_file(self, client):
        """The fastest way to tell a stale container from a stale database."""
        assert client.get("/ledger").json()["schema_version"] == SCHEMA_VERSION

    def test_the_ledger_does_not_appear_in_the_stateless_endpoints(self, client):
        """/analyze must keep working with no database at all. Storage is a door, not a
        dependency of the engine, and someone running the container read-only should still
        get an analysis."""
        response = client.post("/analyze", json={"address": "606 Andre Ct", "price": 268000})
        assert response.status_code == 200
        assert "ledger" not in response.json()
