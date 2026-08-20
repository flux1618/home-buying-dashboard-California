"""FRED mortgage-rate station and committed-snapshot checks.

The normal tests replace both transports. FRED changing its graph CSV at breakfast must
not turn a local test run into an internet-dependent event; the one opt-in live test is
where that contract is checked deliberately.
"""

from __future__ import annotations

import importlib.util
import json
from datetime import date
from pathlib import Path

import pytest

from analyzer.sources import http, rates
from analyzer.sources.base import Context

REPO = Path(__file__).resolve().parents[1]


class FakeCSV:
    """The text equivalent of conftest's FakeHTTP, local because shared HTTP is JSON-only."""

    def __init__(self) -> None:
        self.payload: str | BaseException | None = None
        self.calls: list[str] = []

    def __call__(self, url: str, **kwargs) -> str:
        self.calls.append(url)
        if self.payload is None:
            raise AssertionError(f"unstubbed CSV request to {url}")
        if isinstance(self.payload, BaseException):
            raise self.payload
        return self.payload


@pytest.fixture
def fake_csv(monkeypatch) -> FakeCSV:
    fake = FakeCSV()
    monkeypatch.setattr(rates, "get_csv_text", fake)
    monkeypatch.delenv("FRED_API_KEY", raising=False)
    return fake


def csv_body(*rows: tuple[str, str]) -> str:
    return "observation_date,MORTGAGE30US\n" + "\n".join(",".join(row) for row in rows) + "\n"


def load_builder():
    path = REPO / "tools" / "build_rates_snapshot.py"
    spec = importlib.util.spec_from_file_location("build_rates_snapshot", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestMortgageRateStation:
    def test_normal_csv_is_measured_and_location_independent(self, fake_csv):
        fake_csv.payload = csv_body(
            ("2026-07-16", "6.75"),
            ("2026-07-23", "6.74"),
            ("2026-07-30", "6.72"),
            ("2026-08-06", "6.70"),
            ("2026-08-13", "6.67"),
        )

        result = rates.MortgageRateStation().run(Context(address="unlocated", price=1.0))

        assert result.ok
        assert result.facts["mortgage30us_rate_pct"] == 6.67
        latest = result.values["mortgage30us_rate_pct"]
        assert latest.confidence == "measured"
        assert latest.source_url == rates.FRED_GRAPH_CSV_URL
        assert "2026-08-13" in latest.note
        assert result.values["mortgage30us_4_week_change_pct_points"].confidence == "derived"
        assert fake_csv.calls == [rates.FRED_GRAPH_CSV_URL]

    def test_period_missing_marker_is_not_a_zero_rate(self, fake_csv):
        fake_csv.payload = csv_body(
            ("2026-08-06", "."),
            ("2026-08-13", "6.67"),
        )

        series = rates.fetch_series()

        assert [(row.observation_date.isoformat(), row.rate_pct) for row in series.observations] == [
            ("2026-08-13", 6.67)
        ]

    @pytest.mark.parametrize(
        "body",
        [
            "not a CSV response",
            "observation_date,MORTGAGE30US\n2026-08-13,not-a-rate\n",
        ],
    )
    def test_garbage_or_truncated_shape_degrades(self, fake_csv, body):
        fake_csv.payload = body

        result = rates.MortgageRateStation().run(Context(address="x", price=1.0))

        assert result.degradation is not None
        assert "ValueError" in result.degradation.reason
        assert result.degradation.missing == rates.MortgageRateStation.provides

    def test_empty_series_degrades_instead_of_inventing_a_rate(self, fake_csv):
        fake_csv.payload = "observation_date,MORTGAGE30US\n"

        result = rates.MortgageRateStation().run(Context(address="x", price=1.0))

        assert result.degradation is not None
        assert "no usable" in result.degradation.reason

    @pytest.mark.parametrize(
        "failure",
        [
            http.SourceUnavailable("HTTP 500 Internal Server Error"),
            http.SourceUnavailable("TimeoutError: timed out"),
        ],
        ids=["500", "timeout"],
    )
    def test_server_error_or_timeout_degrades(self, fake_csv, failure):
        fake_csv.payload = failure

        result = rates.MortgageRateStation().run(Context(address="x", price=1.0))

        assert result.degradation is not None
        assert "source unreachable" in result.degradation.reason
        assert str(failure) in result.degradation.reason

    def test_stale_observation_is_labelled_without_changing_its_measured_status(self, fake_csv):
        fake_csv.payload = csv_body(
            ("2026-07-01", "6.70"),
            ("2026-07-08", "6.71"),
            ("2026-07-15", "6.72"),
            ("2026-07-22", "6.73"),
            ("2026-07-29", "6.74"),
        )

        result = rates.MortgageRateStation().run(Context(address="x", price=1.0))

        value = result.values["mortgage30us_rate_pct"]
        assert value.confidence == "measured"
        assert "STALE:" in value.note
        assert "2026-07-29" in value.note

    def test_documented_api_is_preferred_when_a_key_exists(self, monkeypatch, fake_http, fake_csv):
        monkeypatch.setenv("FRED_API_KEY", "not-exposed-in-provenance")
        fake_http.route(
            "api.stlouisfed.org",
            {
                "observations": [
                    {"date": "2026-08-06", "value": "6.69"},
                    {"date": "2026-08-13", "value": "6.67"},
                ]
            },
        )

        result = rates.MortgageRateStation().run(Context(address="x", price=1.0))

        value = result.values["mortgage30us_rate_pct"]
        assert value.source_url == rates.FRED_API_OBSERVATIONS
        assert "documented FRED observations API" in value.note
        assert fake_csv.calls == []

    def test_api_failure_falls_back_to_csv_and_says_so(self, monkeypatch, fake_http, fake_csv):
        monkeypatch.setenv("FRED_API_KEY", "key")
        fake_http.route("api.stlouisfed.org", http.SourceRejected("HTTP 401 Unauthorized"))
        fake_csv.payload = csv_body(("2026-08-13", "6.67"))

        result = rates.MortgageRateStation().run(Context(address="x", price=1.0))

        value = result.values["mortgage30us_rate_pct"]
        assert value.source_url == rates.FRED_GRAPH_CSV_URL
        assert "fallback answered" in value.note


class TestCommittedSnapshot:
    def test_committed_snapshot_is_currently_valid(self):
        builder = load_builder()
        snapshot = json.loads((REPO / "data" / "mortgage30us.json").read_text())

        assert builder.check(snapshot) == 0

    def test_checker_rejects_drifted_derived_value(self):
        builder = load_builder()
        snapshot = json.loads((REPO / "data" / "mortgage30us.json").read_text())
        snapshot["derived"]["52_week_high_pct"] = 0

        assert builder.check(snapshot) == 1

    def test_checker_rejects_a_stale_snapshot(self):
        builder = load_builder()
        snapshot = rates.snapshot_payload(
            rates.SeriesData(
                (
                    rates.Observation(date(2026, 6, 5), 6.7),
                    rates.Observation(date(2026, 6, 12), 6.6),
                ),
                rates.FRED_GRAPH_CSV_URL,
                rates.CSV_SOURCE_KIND,
            ),
            fetched_at="2026-06-12T12:00:00+00:00",
            today=date(2026, 6, 30),
        )

        assert snapshot["stale"] is True
        assert builder.check(snapshot, today=date(2026, 6, 30)) == 0
        snapshot["stale"] = False
        assert builder.check(snapshot, today=date(2026, 6, 30)) == 1


@pytest.mark.live
def test_fred_graph_csv_still_returns_a_recent_mortgage30us_observation(monkeypatch):
    """Opt-in contract: this must touch FRED, unlike every test above."""

    monkeypatch.delenv("FRED_API_KEY", raising=False)
    result = rates.MortgageRateStation().run(Context(address="not used", price=1.0))

    assert result.ok, result.degradation.reason if result.degradation else ""
    value = result.values["mortgage30us_rate_pct"]
    assert value.confidence == "measured"
    assert 0.0 < value.value < 20.0
    assert result.facts["mortgage30us_observation_date"] >= "2020-01-01"
