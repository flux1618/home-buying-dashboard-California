"""Station M — Freddie Mac's weekly 30-year fixed mortgage average via FRED.

`MORTGAGE30US` is the Freddie Mac Primary Mortgage Market Survey's weekly US average
30-year fixed rate. It is a market baseline, not a quote, a borrower APR, or a promise
that a particular lender will offer the same rate. The sensitivity work reads the committed
snapshot made by :mod:`tools.build_rates_snapshot`; this station owns the source contract
and the parsing shared by that build.

## Snapshot schema

``data/mortgage30us.json`` is intentionally a small, standalone boundary between the
weekly collector and consumers. Its obvious top-level keys are:

* ``series_id`` — always ``MORTGAGE30US``;
* ``latest_rate_pct`` and ``observation_date`` — the current weekly reading;
* ``fetched_at``, ``source_url``, and ``source_kind`` — provenance for that reading;
* ``history`` — approximately the last five years of dated weekly observations;
* ``derived`` — 52-week high/low, four-week percentage-point movement, and percentile
  within that bounded history; and
* ``stale`` / ``staleness_note`` — an explicit warning when the latest Thursday reading
  is more than ten days old.

The documented FRED observations API needs an API key. FRED's graph CSV works without one
but is not its documented API, so it is the default only because it is the available,
repeatable public path. When ``FRED_API_KEY`` is set, this module tries the documented API
first and falls back to the CSV if it cannot answer. The provenance note says which path
actually answered; a consumer must never infer this from configuration alone.

The source layer's shared HTTP helper only parses JSON. The compact CSV reader lives here
rather than teaching a general helper one FRED-specific format, but it converts every
socket and response failure into the same ``SourceUnavailable`` / ``SourceRejected``
types that ``Station.run`` already degrades under ADR 0006.
"""

from __future__ import annotations

import csv
import io
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any

from ..core.provenance import derived, measured, unavailable
from . import http
from .base import Context, Station, StationResult

SERIES_ID = "MORTGAGE30US"
FRED_GRAPH_CSV_URL = f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={SERIES_ID}"
FRED_API_OBSERVATIONS = "https://api.stlouisfed.org/fred/series/observations"
FRED_SERIES_PAGE = f"https://fred.stlouisfed.org/series/{SERIES_ID}"
FRED_API_DOCUMENTATION = "https://fred.stlouisfed.org/docs/api/fred/series_observations.html"

CSV_SOURCE_KIND = "fred_graph_csv_undocumented"
API_SOURCE_KIND = "fred_api_observations"
STALE_AFTER_DAYS = 10
HISTORY_YEARS = 5


@dataclass(frozen=True)
class Observation:
    """One usable FRED observation, after FRED's ``.`` missing marker is removed."""

    observation_date: date
    rate_pct: float


@dataclass(frozen=True)
class SeriesData:
    """Parsed observations plus the safe provenance URL for the path that answered."""

    observations: tuple[Observation, ...]
    source_url: str
    source_kind: str
    fallback_note: str | None = None


def get_csv_text(url: str, *, timeout: float = http.DEFAULT_TIMEOUT) -> str:
    """Fetch FRED's CSV and normalize transport errors to the station contract."""

    request = urllib.request.Request(
        url,
        headers={"User-Agent": http.USER_AGENT, "Accept": "text/csv"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        message = f"HTTP {exc.code} {exc.reason}"
        if exc.code in (401, 403, 429):
            raise http.SourceRejected(message) from exc
        raise http.SourceUnavailable(message) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise http.SourceUnavailable(f"{type(exc).__name__}: {exc}") from exc


def _parse_row(observation_date: Any, raw_rate: Any) -> Observation | None:
    """Return a usable row; FRED's documented missing convention is a literal period."""

    if raw_rate is None or str(raw_rate).strip() in {"", "."}:
        return None
    try:
        parsed_date = date.fromisoformat(str(observation_date).strip())
        rate = float(str(raw_rate).strip())
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid {SERIES_ID} observation {observation_date!r}/{raw_rate!r}") from exc
    if rate < 0 or rate > 100:
        raise ValueError(f"implausible {SERIES_ID} rate {rate!r}")
    return Observation(parsed_date, rate)


def _normalise(observations: list[Observation]) -> tuple[Observation, ...]:
    """Sort the public feed and fail rather than silently accepting an empty series."""

    by_date = {row.observation_date: row for row in observations}
    ordered = tuple(by_date[key] for key in sorted(by_date))
    if not ordered:
        raise ValueError(f"FRED returned no usable {SERIES_ID} observations")
    return ordered


def parse_graph_csv(body: str) -> tuple[Observation, ...]:
    """Parse FRED graph CSV, rejecting a changed or truncated header loudly."""

    reader = csv.DictReader(io.StringIO(body))
    expected = {"observation_date", SERIES_ID}
    if not reader.fieldnames or not expected.issubset(set(reader.fieldnames)):
        raise ValueError(
            f"unexpected FRED CSV header {reader.fieldnames!r}; expected observation_date,{SERIES_ID}"
        )
    return _normalise(
        [
            row
            for item in reader
            if (row := _parse_row(item.get("observation_date"), item.get(SERIES_ID))) is not None
        ]
    )


def parse_api_observations(payload: Any) -> tuple[Observation, ...]:
    """Parse FRED's documented observations response without treating missing as zero."""

    if not isinstance(payload, dict):
        raise ValueError("FRED API response was not an object")
    rows = payload.get("observations")
    if not isinstance(rows, list):
        raise ValueError("FRED API response has no observations list")
    parsed: list[Observation] = []
    for item in rows:
        if not isinstance(item, dict):
            raise ValueError("FRED API observations contained a non-object row")
        row = _parse_row(item.get("date"), item.get("value"))
        if row is not None:
            parsed.append(row)
    return _normalise(parsed)


def _fetch_api(api_key: str) -> SeriesData:
    """Use the documented endpoint, keeping the secret out of all returned provenance."""

    url = http.build_url(
        FRED_API_OBSERVATIONS,
        {
            "series_id": SERIES_ID,
            "api_key": api_key,
            "file_type": "json",
            "sort_order": "asc",
        },
    )
    observations = parse_api_observations(http.get_json(url, cache=False).data)
    return SeriesData(observations, FRED_API_OBSERVATIONS, API_SOURCE_KIND)


def _fetch_csv() -> SeriesData:
    return SeriesData(parse_graph_csv(get_csv_text(FRED_GRAPH_CSV_URL)), FRED_GRAPH_CSV_URL, CSV_SOURCE_KIND)


def fetch_series(api_key: str | None = None) -> SeriesData:
    """Prefer the documented API when configured; never let that disable the public CSV."""

    key = (api_key if api_key is not None else os.environ.get("FRED_API_KEY", "")).strip()
    if not key:
        return _fetch_csv()

    try:
        return _fetch_api(key)
    except (http.SourceUnavailable, http.SourceRejected, TypeError, ValueError) as api_error:
        try:
            csv_data = _fetch_csv()
        except (http.SourceUnavailable, http.SourceRejected, TypeError, ValueError) as csv_error:
            raise http.SourceUnavailable(
                f"documented FRED API failed ({api_error}); graph CSV fallback also failed ({csv_error})"
            ) from csv_error
        return SeriesData(
            csv_data.observations,
            csv_data.source_url,
            csv_data.source_kind,
            fallback_note=f"documented FRED API failed and graph CSV fallback answered: {api_error}",
        )


def bounded_history(
    observations: tuple[Observation, ...], *, today: date | None = None
) -> tuple[Observation, ...]:
    """Keep five years from the newest observed date, so a delayed run stays internally coherent."""

    if not observations:
        raise ValueError("cannot bound an empty observation series")
    # Observation date rather than wall-clock date is deliberate. If FRED pauses a release,
    # truncating at the clock's five-year boundary would quietly remove a week from the
    # comparison window while the snapshot is already warning that it is stale.
    cutoff = observations[-1].observation_date - timedelta(days=HISTORY_YEARS * 365 + 2)
    return tuple(row for row in observations if row.observation_date >= cutoff)


def stale_note(latest_date: date, *, today: date | None = None) -> str | None:
    as_of = today or date.today()
    age = (as_of - latest_date).days
    if age > STALE_AFTER_DAYS:
        return (
            f"STALE: latest {SERIES_ID} observation is {latest_date.isoformat()} "
            f"({age} days old; FRED publishes this weekly series after Thursday's PMMS release)."
        )
    return None


def derive_stats(history: tuple[Observation, ...]) -> dict[str, float | None]:
    """Compute only summary values whose inputs are present in the bounded history."""

    if not history:
        raise ValueError("cannot derive statistics from an empty history")
    latest = history[-1]
    trailing_year = [
        row.rate_pct
        for row in history
        if row.observation_date >= latest.observation_date - timedelta(days=364)
    ]
    four_week_change = (
        round(latest.rate_pct - history[-5].rate_pct, 2) if len(history) >= 5 else None
    )
    percentile = round(
        100 * sum(row.rate_pct <= latest.rate_pct for row in history) / len(history), 1
    )
    return {
        "52_week_high_pct": round(max(trailing_year), 2),
        "52_week_low_pct": round(min(trailing_year), 2),
        "4_week_change_pct_points": four_week_change,
        "5_year_percentile": percentile,
    }


def snapshot_payload(
    series: SeriesData, *, fetched_at: str | None = None, today: date | None = None
) -> dict[str, Any]:
    """Make the committed, consumer-facing snapshot from one successful source result."""

    history = bounded_history(series.observations)
    latest = history[-1]
    stale = stale_note(latest.observation_date, today=today)
    return {
        "series_id": SERIES_ID,
        "latest_rate_pct": latest.rate_pct,
        "observation_date": latest.observation_date.isoformat(),
        "fetched_at": fetched_at
        or datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "source_url": series.source_url,
        "source_kind": series.source_kind,
            "source_note": (
                "Answered through the documented FRED observations API."
                if series.source_kind == API_SOURCE_KIND
                else "Answered through FRED's keyless graph CSV, an undocumented path."
            ),
        "fallback_note": series.fallback_note,
        "history": [
            {"observation_date": row.observation_date.isoformat(), "rate_pct": row.rate_pct}
            for row in history
        ],
        "derived": derive_stats(history),
        "stale": stale is not None,
        "staleness_note": stale,
    }


class MortgageRateStation(Station):
    """Non-fatal, location-independent station for a current rate baseline."""

    name = "rates"
    provides = (
        "mortgage30us_rate_pct",
        "mortgage30us_observation_date",
        "mortgage30us_52_week_high_pct",
        "mortgage30us_52_week_low_pct",
        "mortgage30us_4_week_change_pct_points",
        "mortgage30us_5_year_percentile",
    )
    description = "FRED MORTGAGE30US, Freddie Mac weekly 30-year fixed mortgage average"

    def __init__(self, api_key: str | None = None) -> None:
        self.api_key = api_key

    def skip_reason(self, ctx: Context) -> str | None:
        # This is national weekly market data. Making it depend on geocoding would turn a
        # bad address into a fabricated reason not to know the published mortgage baseline.
        return None

    def fetch(self, ctx: Context) -> StationResult:
        configured_key = self.api_key or ctx.api_keys.get("fred")
        series = fetch_series(configured_key)
        snapshot = snapshot_payload(series)
        latest_note = (
            f"{SERIES_ID} weekly average observed {snapshot['observation_date']}; "
            f"{snapshot['source_note']}"
        )
        if snapshot["fallback_note"]:
            latest_note += f" {snapshot['fallback_note']}"
        if snapshot["staleness_note"]:
            latest_note += f" {snapshot['staleness_note']}"

        values: dict[str, Any] = {
            "mortgage30us_rate_pct": measured(
                snapshot["latest_rate_pct"],
                snapshot["source_url"],
                note=latest_note,
                precision="weekly_average_percent",
            ),
            "mortgage30us_observation_date": measured(
                snapshot["observation_date"],
                snapshot["source_url"],
                note=f"Observation date for the {SERIES_ID} weekly average. {snapshot['source_note']}",
                precision="weekly",
            ),
        }
        for key, value in snapshot["derived"].items():
            values[f"mortgage30us_{key}"] = (
                derived(value, note=f"Computed from bounded {SERIES_ID} weekly history.")
                if value is not None
                else unavailable(f"Not enough {SERIES_ID} observations to compute {key}.")
            )

        facts = {
            "mortgage30us_rate_pct": snapshot["latest_rate_pct"],
            "mortgage30us_observation_date": snapshot["observation_date"],
            **{f"mortgage30us_{key}": value for key, value in snapshot["derived"].items()},
        }
        return StationResult(station=self.name, facts=facts, values=values)
