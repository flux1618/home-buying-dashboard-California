"""Batch shortlist mode.

Two jobs are tested separately because they fail differently:

  1. Reading a CSV a human typed. Forgiving about shape, strict about meaning, and
     never partially applied — a bad row is reported, not guessed at.
  2. Ranking the results so the list you read top-down is the list you should tour
     in that order.

Every test here uses a stub runner. The pipeline itself is covered in test_pipeline.py,
and mixing the two would make a CSV-parsing failure look like a network failure.
"""

from __future__ import annotations

import csv
import json

import pytest

from analyzer import batch
from analyzer.core.profile import load_profile
from analyzer.pipeline import Degradation, PipelineAborted, PipelineRun


# =============================================================================
# Support
# =============================================================================


def write_csv(tmp_path, text: str, name: str = "shortlist.csv"):
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def fake_document(*, score=80, verdict="TAKE", **overrides):
    """The shape batch.py reads out of a pipeline document, and nothing more."""
    doc = {
        "score": {
            "value": score,
            "verdict": verdict,
            "score_pinned": False,
            "score_capped": False,
            "unknown_facts": [],
            "hard_fails": [],
            "unevaluated_hard_fails": [],
            "capex_estimate_low": 0.0,
            "capex_estimate_high": 0.0,
        },
        "cost": {
            "piti": 1718.0,
            "true_monthly_low": 1941.0,
            "true_monthly_high": 2165.0,
            "cash_to_close": 89032.0,
            "front_end_dti": 0.057,
        },
        "input": {
            "price": 268000.0,
            "sqft": 1780,
            "beds": 3,
            "baths": 2.0,
            "year_built": 1998,
            "flood_zone": "X",
            "water_sewer": "public",
            "commute_min": 12.5,
            "fiber_available": None,
        },
        "location": {"matched_address": "606 ANDRE CT, SPARTANBURG, SC, 29301"},
        "verification_tasks": [
            {"task": "Call the ISP", "blocking": False},
            {"task": "Confirm flood zone", "blocking": True},
        ],
    }
    doc.update(overrides)
    return doc


def stub_runner(*results, degraded=()):
    """A runner that returns canned documents in order and records its calls.

    Using a callable seam rather than monkeypatching the network means these tests
    assert on the arguments batch mode *passes down*, which is where the real bugs
    live — a dropped `hoa_monthly` would otherwise be invisible.
    """
    calls = []
    queue = list(results)

    def runner(address, price, **kwargs):
        calls.append({"address": address, "price": price, **kwargs})
        outcome = queue.pop(0) if queue else fake_document()
        if isinstance(outcome, Exception):
            raise outcome
        return PipelineRun(
            document=outcome,
            degradations=[Degradation(station=name, reason="stubbed") for name in degraded],
        )

    runner.calls = calls
    return runner


@pytest.fixture
def profile():
    return load_profile()


# =============================================================================
# Reading the file
# =============================================================================


class TestHeaderHandling:
    def test_canonical_headers(self, tmp_path):
        path = write_csv(tmp_path, "address,price\n1 Main St,268000\n")
        rows, rejected, unknown = batch.read_shortlist(path)
        assert [r.address for r in rows] == ["1 Main St"]
        assert rejected == []
        assert unknown == []

    @pytest.mark.parametrize(
        "header",
        [
            "Address,Price",
            "ADDRESS,PRICE",
            " address , price ",
            "Street Address,List Price",
            "Property,Asking Price ($)",
        ],
    )
    def test_forgiving_header_spellings(self, tmp_path, header):
        """A shortlist is exported from a dozen tools. All of these mean the same thing."""
        path = write_csv(tmp_path, f"{header}\n1 Main St,268000\n")
        rows, rejected, _ = batch.read_shortlist(path)
        assert rejected == []
        assert rows[0].address == "1 Main St"
        assert rows[0].price == 268000

    def test_bom_is_stripped(self, tmp_path):
        """Excel writes a BOM. Without this the first column is never recognised."""
        path = tmp_path / "s.csv"
        path.write_text("address,price\n1 Main St,268000\n", encoding="utf-8-sig")
        rows, rejected, _ = batch.read_shortlist(path)
        assert rejected == []
        assert rows[0].address == "1 Main St"

    def test_unrecognised_columns_are_reported_not_fatal(self, tmp_path):
        path = write_csv(tmp_path, "address,price,Zestimate\n1 Main St,268000,271000\n")
        rows, rejected, unknown = batch.read_shortlist(path)
        assert len(rows) == 1
        assert rejected == []
        assert unknown == ["Zestimate"]

    def test_missing_required_column_is_a_hard_error(self, tmp_path):
        path = write_csv(tmp_path, "address,beds\n1 Main St,3\n")
        with pytest.raises(ValueError, match="price"):
            batch.read_shortlist(path)

    def test_blank_lines_are_skipped_silently(self, tmp_path):
        path = write_csv(tmp_path, "address,price\n1 Main St,268000\n\n\n2 Oak Ave,280000\n")
        rows, rejected, _ = batch.read_shortlist(path)
        assert len(rows) == 2
        assert rejected == []


class TestValueParsing:
    @pytest.mark.parametrize(
        "raw,expected",
        [("268000", 268000.0), ("$268,000", 268000.0), ("268000.00", 268000.0),
         (" 268000 ", 268000.0), ("268_000", 268000.0)],
    )
    def test_money_formats(self, tmp_path, raw, expected):
        path = write_csv(tmp_path, f'address,price\n1 Main St,"{raw}"\n')
        rows, rejected, _ = batch.read_shortlist(path)
        assert rejected == []
        assert rows[0].price == expected

    def test_zero_hoa_is_a_fact_not_an_error(self, tmp_path):
        """Regression. An explicit 0 used to be rejected as "must be positive",
        which punished the user for stating the most common answer in the county."""
        path = write_csv(tmp_path, "address,price,hoa\n1 Main St,268000,0\n")
        rows, rejected, _ = batch.read_shortlist(path)
        assert rejected == []
        assert rows[0].hoa_monthly == 0.0

    def test_blank_hoa_also_means_zero(self, tmp_path):
        path = write_csv(tmp_path, "address,price,hoa\n1 Main St,268000,\n")
        rows, _, _ = batch.read_shortlist(path)
        assert rows[0].hoa_monthly == 0.0

    def test_blank_age_is_unknown_not_zero(self, tmp_path):
        """A brand-new roof and an unrecorded roof are not the same house."""
        path = write_csv(tmp_path, "address,price,roof age\n1 Main St,268000,\n")
        rows, _, _ = batch.read_shortlist(path)
        assert rows[0].roof_age_years is None

    @pytest.mark.parametrize("word", ["unknown", "n/a", "N/A", "?", "-", "  "])
    def test_words_meaning_unknown(self, tmp_path, word):
        path = write_csv(tmp_path, f'address,price,roof age\n1 Main St,268000,"{word}"\n')
        rows, rejected, _ = batch.read_shortlist(path)
        assert rejected == []
        assert rows[0].roof_age_years is None

    def test_zero_age_is_kept_as_zero(self, tmp_path):
        """A roof replaced this year is age 0, and that must survive parsing."""
        path = write_csv(tmp_path, "address,price,roof age\n1 Main St,268000,0\n")
        rows, _, _ = batch.read_shortlist(path)
        assert rows[0].roof_age_years == 0


class TestRowRejection:
    """A bad row is reported against its line number and never guessed at."""

    @pytest.mark.parametrize(
        "line,fragment",
        [
            (",268000", "address"),
            ("1 Main St,call for price", "price"),
            ("1 Main St,0", "positive"),
            ("1 Main St,-5000", "positive"),
            ("1 Main St,999999999999", "typo"),
        ],
    )
    def test_rejects_with_a_reason(self, tmp_path, line, fragment):
        path = write_csv(tmp_path, f"address,price\n{line}\n")
        rows, rejected, _ = batch.read_shortlist(path)
        assert rows == []
        assert len(rejected) == 1
        assert fragment in rejected[0].problem

    def test_negative_hoa_is_rejected(self, tmp_path):
        path = write_csv(tmp_path, "address,price,hoa\n1 Main St,268000,-5\n")
        _, rejected, _ = batch.read_shortlist(path)
        assert "negative" in rejected[0].problem

    def test_non_numeric_age_is_rejected(self, tmp_path):
        path = write_csv(tmp_path, "address,price,roof age\n1 Main St,268000,abc\n")
        _, rejected, _ = batch.read_shortlist(path)
        assert "whole number" in rejected[0].problem

    def test_absurd_garage_count_is_rejected(self, tmp_path):
        path = write_csv(tmp_path, "address,price,garage\n1 Main St,268000,99\n")
        _, rejected, _ = batch.read_shortlist(path)
        assert "out of range" in rejected[0].problem

    def test_line_numbers_match_the_file(self, tmp_path):
        """Reported against the real line so you can find it in a spreadsheet."""
        path = write_csv(
            tmp_path,
            "address,price\n1 Main St,268000\n,300000\n3 Elm St,290000\n",
        )
        rows, rejected, _ = batch.read_shortlist(path)
        assert [r.line for r in rows] == [2, 4]
        assert rejected[0].line == 3

    def test_one_bad_row_does_not_lose_the_good_ones(self, tmp_path):
        path = write_csv(
            tmp_path,
            "address,price\n1 Main St,268000\nbad,nope\n3 Elm St,290000\n",
        )
        rows, rejected, _ = batch.read_shortlist(path)
        assert len(rows) == 2
        assert len(rejected) == 1

    def test_reading_never_touches_the_network(self, tmp_path):
        """Guarded by the autouse no_network fixture — this documents the intent."""
        path = write_csv(tmp_path, "address,price\n1 Main St,268000\n")
        assert batch.read_shortlist(path)[0]


# =============================================================================
# Analysing
# =============================================================================


class TestAnalyseShortlist:
    def test_inputs_reach_the_pipeline(self, tmp_path, profile):
        path = write_csv(
            tmp_path,
            "address,price,hoa,roof age,hvac age,garage\n"
            "1 Main St,268000,125,17,14,1\n",
        )
        rows, _, _ = batch.read_shortlist(path)
        runner = stub_runner()
        batch.analyse_shortlist(rows, profile=profile, runner=runner)
        call = runner.calls[0]
        assert call["address"] == "1 Main St"
        assert call["price"] == 268000
        assert call["hoa_monthly"] == 125
        assert call["roof_age_years"] == 17
        assert call["hvac_age_years"] == 14
        assert call["garage_spaces"] == 1

    def test_unknown_ages_are_passed_as_none(self, tmp_path, profile):
        """Not zero. The engine treats unknown as its own risk, so it must arrive intact."""
        path = write_csv(tmp_path, "address,price\n1 Main St,268000\n")
        rows, _, _ = batch.read_shortlist(path)
        runner = stub_runner()
        batch.analyse_shortlist(rows, profile=profile, runner=runner)
        assert runner.calls[0]["roof_age_years"] is None
        assert runner.calls[0]["hvac_age_years"] is None

    def test_a_failed_row_becomes_an_error_entry(self, tmp_path, profile):
        path = write_csv(tmp_path, "address,price\nNowhere,268000\n")
        rows, _, _ = batch.read_shortlist(path)
        runner = stub_runner(PipelineAborted("could not geocode"))
        entries = batch.analyse_shortlist(rows, profile=profile, runner=runner)
        assert entries[0].ok is False
        assert "geocode" in entries[0].error

    def test_a_failed_row_does_not_stop_the_batch(self, tmp_path, profile):
        """The point of batch mode. One unrecognised address must not cost you the run."""
        path = write_csv(
            tmp_path,
            "address,price\nNowhere,268000\n2 Oak Ave,280000\n3 Elm St,290000\n",
        )
        rows, _, _ = batch.read_shortlist(path)
        runner = stub_runner(
            PipelineAborted("could not geocode"), fake_document(), fake_document()
        )
        entries = batch.analyse_shortlist(rows, profile=profile, runner=runner)
        assert len(entries) == 3
        assert [e.ok for e in entries] == [False, True, True]

    def test_network_failure_is_caught_per_row(self, tmp_path, profile):
        path = write_csv(tmp_path, "address,price\n1 Main St,268000\n2 Oak Ave,280000\n")
        rows, _, _ = batch.read_shortlist(path)
        runner = stub_runner(OSError("connection reset"), fake_document())
        entries = batch.analyse_shortlist(rows, profile=profile, runner=runner)
        assert "network unavailable" in entries[0].error
        assert entries[1].ok

    def test_progress_is_reported_per_row(self, tmp_path, profile):
        path = write_csv(tmp_path, "address,price\n1 Main St,268000\n2 Oak Ave,280000\n")
        rows, _, _ = batch.read_shortlist(path)
        seen = []
        batch.analyse_shortlist(
            rows,
            profile=profile,
            runner=stub_runner(),
            progress=lambda row, entry: seen.append(row.address),
        )
        assert seen == ["1 Main St", "2 Oak Ave"]

    def test_notes_are_carried_into_the_document(self, tmp_path, profile):
        path = write_csv(
            tmp_path, "address,price,mls,notes\n1 Main St,268000,555,Roof looks new\n"
        )
        rows, _, _ = batch.read_shortlist(path)
        entries = batch.analyse_shortlist(rows, profile=profile, runner=stub_runner())
        assert entries[0].document["shortlist"]["notes"] == "Roof looks new"
        assert entries[0].document["shortlist"]["reference"] == "555"
        assert entries[0].document["shortlist"]["csv_line"] == 2


# =============================================================================
# Ranking
# =============================================================================


def entry_for(verdict, score, price=268000.0):
    row = batch.ShortlistRow(line=2, address=f"{verdict} {score}", price=price)
    return batch.BatchEntry(row=row, document=fake_document(score=score, verdict=verdict))


class TestRanking:
    def result_of(self, *entries):
        return batch.BatchResult(
            entries=list(entries), rejected=[], unknown_headers=[], profile_name="test"
        )

    def test_higher_score_ranks_first(self):
        result = self.result_of(entry_for("TAKE", 80), entry_for("TAKE", 95))
        assert [e.score for e in result.ranked] == [95, 80]

    def test_pass_never_outranks_watch(self):
        """A PASS is a hard fail. No score should let it climb above a live candidate."""
        result = self.result_of(entry_for("PASS", 99), entry_for("WATCH", 46))
        assert [e.verdict for e in result.ranked] == ["WATCH", "PASS"]

    def test_take_outranks_watch(self):
        result = self.result_of(entry_for("WATCH", 70), entry_for("TAKE", 76))
        assert [e.verdict for e in result.ranked] == ["TAKE", "WATCH"]

    def test_cheaper_house_wins_a_tie(self):
        result = self.result_of(
            entry_for("TAKE", 80, price=300000), entry_for("TAKE", 80, price=268000)
        )
        assert [e.row.price for e in result.ranked] == [268000, 300000]

    def test_errors_sink_to_the_bottom(self):
        broken = batch.BatchEntry(
            row=batch.ShortlistRow(line=2, address="Nowhere", price=268000.0),
            error="could not geocode",
        )
        result = self.result_of(broken, entry_for("PASS", 10))
        assert result.ranked[-1] is broken

    def test_by_verdict_filters(self):
        result = self.result_of(
            entry_for("TAKE", 80), entry_for("WATCH", 60), entry_for("TAKE", 90)
        )
        assert len(result.by_verdict("TAKE")) == 2
        assert len(result.by_verdict("WATCH")) == 1

    def test_scored_excludes_errors(self):
        broken = batch.BatchEntry(
            row=batch.ShortlistRow(line=2, address="Nowhere", price=268000.0),
            error="boom",
        )
        result = self.result_of(broken, entry_for("TAKE", 80))
        assert len(result.scored) == 1


# =============================================================================
# Output
# =============================================================================


class TestOutput:
    def result_of(self, *entries, rejected=(), unknown=()):
        return batch.BatchResult(
            entries=list(entries),
            rejected=list(rejected),
            unknown_headers=list(unknown),
            profile_name="Spartanburg test profile",
        )

    def test_summary_csv_has_a_stable_header(self, tmp_path):
        path = tmp_path / "summary.csv"
        batch.write_summary_csv(self.result_of(entry_for("TAKE", 80)), path)
        header = next(csv.reader(path.open()))
        assert header == batch.SUMMARY_COLUMNS

    def test_summary_csv_is_ranked(self, tmp_path):
        path = tmp_path / "summary.csv"
        result = self.result_of(entry_for("WATCH", 50), entry_for("TAKE", 90))
        batch.write_summary_csv(result, path)
        rows = list(csv.DictReader(path.open()))
        assert [r["rank"] for r in rows] == ["1", "2"]
        assert rows[0]["verdict"] == "TAKE"

    def test_unknown_fiber_reads_as_unknown_not_blank(self, tmp_path):
        """Blank in a spreadsheet gets read as "no". Three states must stay three."""
        path = tmp_path / "summary.csv"
        batch.write_summary_csv(self.result_of(entry_for("TAKE", 80)), path)
        assert list(csv.DictReader(path.open()))[0]["fiber"] == "unknown"

    def test_capped_score_is_flagged_separately_from_pinned(self, tmp_path):
        """Two different fixable problems. One column each."""
        doc = fake_document(score=74, verdict="WATCH")
        doc["score"]["score_capped"] = True
        doc["score"]["unknown_facts"] = ["heated square footage"]
        entry = batch.BatchEntry(
            row=batch.ShortlistRow(line=2, address="1 Main St", price=268000.0),
            document=doc,
        )
        path = tmp_path / "summary.csv"
        batch.write_summary_csv(self.result_of(entry), path)
        row = list(csv.DictReader(path.open()))[0]
        assert row["score_capped"] == "yes"
        assert row["score_pinned"] == ""
        assert row["unknown_facts"] == "heated square footage"

    def test_error_rows_appear_in_the_csv(self, tmp_path):
        """A row that failed is still your problem. It must not vanish from the report."""
        broken = batch.BatchEntry(
            row=batch.ShortlistRow(line=2, address="Nowhere", price=268000.0),
            error="could not geocode",
        )
        path = tmp_path / "summary.csv"
        batch.write_summary_csv(self.result_of(broken), path)
        row = list(csv.DictReader(path.open()))[0]
        assert row["verdict"] == "ERROR"
        assert "geocode" in row["hard_fails"]

    def test_documents_are_written_one_per_property(self, tmp_path):
        result = self.result_of(entry_for("TAKE", 90), entry_for("WATCH", 50))
        written = batch.write_documents(result, tmp_path / "properties")
        assert len(written) == 2
        assert all(p.suffix == ".json" for p in written)
        assert json.loads(written[0].read_text())["score"]["value"] == 90

    def test_document_filenames_follow_the_csv_line_not_the_rank(self, tmp_path):
        """Deliberate. Naming by rank would rename every file whenever a score moved,
        so last week's notes would point at this week's different house. The CSV line
        is stable across re-runs, which is what you want when you re-score a shortlist
        every weekend. Ranking lives in summary.csv, where it can churn freely."""
        low = entry_for("WATCH", 50)
        high = entry_for("TAKE", 90)
        low.row.line, high.row.line = 2, 3
        written = batch.write_documents(self.result_of(low, high), tmp_path / "properties")
        assert [p.name.split("-")[0] for p in written] == ["002", "003"]

    def test_markdown_reports_every_section(self, tmp_path):
        path = tmp_path / "shortlist.md"
        result = self.result_of(
            entry_for("TAKE", 90),
            entry_for("PASS", 10),
            rejected=[batch.RejectedRow(line=5, problem="price is blank")],
            unknown=["Zestimate"],
        )
        batch.write_markdown(result, path)
        text = path.read_text()
        assert "TAKE" in text and "PASS" in text
        assert "price is blank" in text
        assert "Zestimate" in text

    def test_markdown_survives_an_empty_batch(self, tmp_path):
        """Every row rejected is a real outcome, not a crash."""
        path = tmp_path / "shortlist.md"
        result = self.result_of(rejected=[batch.RejectedRow(line=2, problem="bad price")])
        batch.write_markdown(result, path)
        assert "bad price" in path.read_text()


# =============================================================================
# CLI
# =============================================================================


class TestCLI:
    def test_dry_run_makes_no_requests_and_succeeds(self, tmp_path, capsys):
        path = write_csv(tmp_path, "address,price\n1 Main St,268000\n")
        assert batch.main([str(path), "--dry-run"]) == 0
        assert "Dry run" in capsys.readouterr().out

    def test_dry_run_exits_nonzero_when_rows_were_rejected(self, tmp_path):
        """Fails loudly so a scheduled job cannot quietly analyse half a shortlist."""
        path = write_csv(tmp_path, "address,price\n1 Main St,268000\n,300000\n")
        assert batch.main([str(path), "--dry-run"]) == 1

    def test_missing_file_exits_two(self, tmp_path, capsys):
        assert batch.main([str(tmp_path / "nope.csv"), "--dry-run"]) == 2
        assert "nope.csv" in capsys.readouterr().err

    def test_missing_required_column_exits_two(self, tmp_path, capsys):
        path = write_csv(tmp_path, "address,beds\n1 Main St,3\n")
        assert batch.main([str(path), "--dry-run"]) == 2
        assert "price" in capsys.readouterr().err
