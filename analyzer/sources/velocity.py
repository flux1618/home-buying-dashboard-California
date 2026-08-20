"""Station V — bounded Redfin market-velocity snapshot.

This is a market layer, not a listing layer.  It reads ``data/market_velocity.json``, a
committed reduction of Redfin's published aggregate Market Tracker, rather than calling a
listing page or MLS feed while analysing an address.  ADR 0018 records that boundary.

The source publishes a calendar-month series (ZIP observations use trailing 90-day windows).
A one-month movement is deliberately *not* called a trend: seasonality makes that misleading.
The station reports the same-month year-over-year comparison and labels 3/6/12-month slopes as
recent descriptive slopes.  The latter can be useful context, but only the former compares like
seasonal periods.
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime
from pathlib import Path
from typing import Any

from ..core.provenance import derived, measured, unavailable
from .base import Context, Station, StationResult

SNAPSHOT = Path(__file__).resolve().parents[2] / "data" / "market_velocity.json"
# Redfin's published observations can arrive roughly a quarter after the period end.  Permit that
# documented publication lag plus one monthly cadence; any older snapshot is not current context.
# CBSA 31080, Los Angeles-Long Beach-Anaheim. Was 43900 (Spartanburg, SC) before the
# California port. Named as a constant rather than repeated as a literal because it appeared
# twice and a half-updated metro code is the kind of bug that reports real numbers for the
# wrong city.
METRO_CBSA = "31080"

STALE_AFTER_DAYS = 110
ZIP_RE = re.compile(r"(?<!\d)(\d{5})(?:-\d{4})?(?!\d)")

# Kept in one declaration because a missing metric must remain visibly absent, not become a
# zero through a formatter's fallback.  Redfin's PRICE_DROPS is a share, not a count.
METRICS: dict[str, str] = {
    "median_days_on_market": "median days on market",
    "months_of_supply": "months of supply",
    "active_inventory": "active inventory",
    "new_listings": "new listings",
    "price_cut_share": "share of listings with a price cut",
    "sale_to_list_ratio": "average sale-to-list ratio",
}


def address_zip(address: str) -> str | None:
    """Read an explicitly supplied ZIP; never infer one from a coordinate or city name."""

    match = ZIP_RE.search(address or "")
    return match.group(1) if match else None


def _date(value: Any, label: str) -> date:
    try:
        return date.fromisoformat(str(value))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"market velocity snapshot has invalid {label}: {value!r}") from exc


def read_snapshot(path: Path = SNAPSHOT) -> dict[str, Any]:
    """Read and minimally validate the committed boundary, translating disk failures.

    ``Station.run`` deliberately catches data-shape errors but not arbitrary ``OSError``.
    Converting every local read failure to ``ValueError`` preserves ADR 0006's never-raise
    promise for a missing mount, a partial checkout, or a malformed hand edit.
    """

    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read market velocity snapshot {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("market velocity snapshot root must be an object")
    required = {"schema_version", "source_url", "period_covered", "markets"}
    missing = sorted(required - set(payload))
    if missing:
        raise ValueError(f"market velocity snapshot missing keys: {missing}")
    if payload["schema_version"] != 1:
        raise ValueError(f"unsupported market velocity schema {payload['schema_version']!r}")
    if not isinstance(payload.get("markets"), dict):
        raise ValueError("market velocity snapshot markets must be an object")
    if not isinstance(payload.get("period_covered"), dict):
        raise ValueError("market velocity snapshot period_covered must be an object")
    if not isinstance(payload.get("source_url"), str) or not payload["source_url"].startswith("https://"):
        raise ValueError("market velocity snapshot source_url must be an HTTPS URL")
    _date(payload["period_covered"].get("latest_period_end"), "latest_period_end")
    return payload


def stale_reason(snapshot: dict[str, Any], *, today: date | None = None) -> str | None:
    """The observation period, not download time, decides whether market context is stale."""

    latest = _date(snapshot["period_covered"].get("latest_period_end"), "latest_period_end")
    age = ((today or date.today()) - latest).days
    if age > STALE_AFTER_DAYS:
        return (
            f"market velocity snapshot ends {latest.isoformat()} ({age} days old; "
            f"refresh is due after {STALE_AFTER_DAYS} days)"
        )
    return None


def _market(snapshot: dict[str, Any], scope: str, identifier: str) -> dict[str, Any] | None:
    rows = snapshot.get("markets", {}).get(scope, {})
    item = rows.get(identifier) if isinstance(rows, dict) else None
    return item if isinstance(item, dict) else None


def _present_market(item: dict[str, Any] | None) -> dict[str, Any] | None:
    """Keep the snapshot's public shape while making absent metrics explicitly ``None``."""

    if item is None:
        return None
    current = item.get("current") if isinstance(item.get("current"), dict) else {}
    metrics = {
        name: (current.get(name) if name in current else None)
        for name in METRICS
    }
    return {
        "label": item.get("label"),
        "period_begin": item.get("period_begin"),
        "period_end": item.get("period_end"),
        "period_duration_days": item.get("period_duration_days"),
        "metrics": metrics,
        "year_over_year": item.get("year_over_year"),
        "recent_slopes": item.get("recent_slopes"),
    }


class MarketVelocityStation(Station):
    """Report county, metro and ZIP market pace from the bounded local snapshot."""

    name = "velocity"
    provides = ("market_velocity",)
    description = "Redfin aggregate Market Tracker velocity, county/metro/ZIP snapshot"

    def __init__(self, snapshot_path: Path = SNAPSHOT, *, today: date | None = None) -> None:
        self.snapshot_path = snapshot_path
        self.today = today

    def skip_reason(self, ctx: Context) -> str | None:
        # Unlike parcel/flood/commute, this layer is not point resolution.  County FIPS is
        # sufficient, and an explicitly typed ZIP is sufficient for its submarket row.
        if ctx.county_fips or address_zip(ctx.address):
            return None
        return "no county FIPS or explicit ZIP available to resolve market geography"

    def fetch(self, ctx: Context) -> StationResult:
        snapshot = read_snapshot(self.snapshot_path)
        if reason := stale_reason(snapshot, today=self.today):
            # Treat a stale snapshot as unavailable rather than labeling an old market as
            # current.  The measured historical values remain in git for review; they are
            # simply not allowed to influence a current buy-or-wait read.
            return self.degrade(reason)

        county_fips = ctx.county_fips
        zip_code = address_zip(ctx.address)
        county = _market(snapshot, "county", county_fips) if county_fips else None
        zip_market = _market(snapshot, "zip", zip_code) if zip_code else None
        metro = _market(snapshot, "metro", METRO_CBSA)

        result: dict[str, Any] = {
            "source_url": snapshot["source_url"],
            "license_note": snapshot.get("license_note"),
            "period_covered": snapshot["period_covered"],
            "seasonality": snapshot.get("seasonality"),
            "metro": _present_market(metro),
            "county": _present_market(county),
            "zip": _present_market(zip_market),
        }
        values: dict[str, Any] = {}
        for scope, identifier, item in (
            ("metro", METRO_CBSA, metro),
            ("county", county_fips or "unknown", county),
            ("zip", zip_code or "unknown", zip_market),
        ):
            if item is None:
                continue
            prefix = f"market_velocity_{scope}_{identifier}"
            period = item.get("period_end")
            current = item.get("current") if isinstance(item.get("current"), dict) else {}
            for metric, label in METRICS.items():
                value = current.get(metric)
                if value is None:
                    values[f"{prefix}_{metric}"] = unavailable(
                        f"Redfin did not publish {label} for {scope} {identifier} in {period}."
                    )
                else:
                    values[f"{prefix}_{metric}"] = measured(
                        value,
                        snapshot["source_url"],
                        note=f"Reported {label} for {scope} {identifier}, period ending {period}.",
                        precision=("trailing_90_days" if item.get("period_duration_days") == 90 else "monthly"),
                    )
            for metric, windows in (item.get("recent_slopes") or {}).items():
                if not isinstance(windows, dict):
                    continue
                for window, detail in windows.items():
                    slope = detail.get("slope_per_month") if isinstance(detail, dict) else None
                    key = f"{prefix}_{metric}_{window}_slope_per_month"
                    values[key] = (
                        derived(
                            slope,
                            note=(
                                f"Linear slope over the last {window.replace('_', ' ')} for {metric}; "
                                "descriptive only because short windows retain seasonality."
                            ),
                        )
                        if slope is not None
                        else unavailable(f"Not enough contiguous observations for {metric} {window} slope.")
                    )
            for metric, comparison in (item.get("year_over_year") or {}).items():
                diff = comparison.get("absolute_change") if isinstance(comparison, dict) else None
                key = f"{prefix}_{metric}_year_over_year_change"
                values[key] = (
                    derived(
                        diff,
                        note=(
                            f"Same-month year-over-year change for {metric}; this is the station's "
                            "seasonally comparable direction, not a month-over-month claim."
                        ),
                    )
                    if diff is not None
                    else unavailable(f"No same-month prior-year observation for {metric}.")
                )

        tasks: list[dict[str, Any]] = []
        if county_fips and county is None:
            tasks.append(self.task(
                f"No market-velocity county row is committed for FIPS {county_fips}; do not infer it from a nearby county.",
                reason="the snapshot is intentionally bounded to Los Angeles County",
            ))
        if zip_code and zip_market is None:
            tasks.append(self.task(
                f"No market-velocity submarket row is committed for ZIP {zip_code}; do not substitute a nearby ZIP.",
                reason="the snapshot contains only configured target ZIPs",
            ))
        return StationResult(station=self.name, facts={"market_velocity": result}, values=values, tasks=tasks)
