"""Build the bounded Redfin aggregate market-velocity snapshot.

The three public Market Tracker downloads are gzip-compressed TSVs and the ZIP archive is large.
This script never writes them to disk and never materializes them in memory: it opens each remote
stream, decompresses it incrementally, and retains only Los Angeles County, Los Angeles metro,
and the configured target ZIP rows.  The committed JSON is the small reviewable reduction.

    python tools/build_velocity_snapshot.py
    python tools/build_velocity_snapshot.py --check

``--check`` makes no request.  It validates the committed schema and observation freshness.
A source outage during a scheduled refresh is intentionally a successful no-op: the existing
snapshot remains visible with its old period rather than CI turning an upstream 500 into a red
repository build.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import io
import json
import re
import sys
import tomllib
import urllib.error
import urllib.request
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, BinaryIO, Iterable

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from analyzer.sources import http, velocity  # noqa: E402

SNAPSHOT = REPO / "data" / "market_velocity.json"
PROFILE = REPO / "buyer_profile.toml"
STATIC_SNAPSHOT = REPO / "data.json"

DATA_CENTER_URL = "https://www.redfin.com/news/data-center/"
RAW_URLS = {
    "county": "https://redfin-public-data.s3.us-west-2.amazonaws.com/redfin_market_tracker/county_market_tracker.tsv000.gz",
    "metro": "https://redfin-public-data.s3.us-west-2.amazonaws.com/redfin_market_tracker/redfin_metro_market_tracker.tsv000.gz",
    "zip": "https://redfin-public-data.s3.us-west-2.amazonaws.com/redfin_market_tracker/zip_code_market_tracker.tsv000.gz",
}
STATE_FIPS = "06"
COUNTY_FIPS = "06037"
COUNTY_LABEL = "Los Angeles County, CA"
METRO_CODE = "31080"
METRO_LABEL = "Los Angeles-Long Beach-Anaheim, CA"
# Redfin names this metro in its download rather than using the Census CBSA title.
METRO_SOURCE_LABEL = "Los Angeles, CA"
METRO_GEOGRAPHY_SOURCE = "https://www2.census.gov/programs-surveys/metro-micro/geographies/reference-files/2023/delineation-files/list1_2023.xlsx"

# We deliberately keep only these fields.  Every one answers a buy-or-wait question; retaining
# Redfin's price, price-per-square-foot, and market-rank fields would enlarge the snapshot without
# adding market pace.  ``PRICE_DROPS`` is the published share, despite its noun-like field name.
SOURCE_COLUMNS = {
    "PERIOD_BEGIN",
    "PERIOD_END",
    "PERIOD_DURATION",
    "REGION_TYPE",
    "REGION_TYPE_ID",
    "PROPERTY_TYPE",
    "IS_SEASONALLY_ADJUSTED",
    "REGION",
    "MEDIAN_DOM",
    "MONTHS_OF_SUPPLY",
    "INVENTORY",
    "NEW_LISTINGS",
    "PRICE_DROPS",
    "AVG_SALE_TO_LIST",
    "LAST_UPDATED",
}
COLUMN_TO_METRIC = {
    "MEDIAN_DOM": "median_days_on_market",
    "MONTHS_OF_SUPPLY": "months_of_supply",
    "INVENTORY": "active_inventory",
    "NEW_LISTINGS": "new_listings",
    "PRICE_DROPS": "price_cut_share",
    "AVG_SALE_TO_LIST": "sale_to_list_ratio",
}
METRICS = tuple(COLUMN_TO_METRIC.values())
WINDOWS = (3, 6, 12)


def _read_profile_target_zips() -> tuple[list[str], str]:
    """Read the explicit profile setting once it lands, with an honest compatibility bridge.

    The current profile predates this station and has no target-ZIP block.  Its existing static
    submarket map is the sole previous declaration of the target ZIP universe, so it is a safe
    temporary fallback rather than a geography inferred from an address.  The handoff asks the
    owner to move this list into ``buyer_profile.toml``; after that this branch is unused.
    """

    with open(PROFILE, "rb") as fh:
        profile = tomllib.load(fh)
    configured = profile.get("market_velocity", {}).get("target_zips")
    if isinstance(configured, list) and configured:
        zips = [str(item).zfill(5) for item in configured]
        return sorted(set(zips)), "buyer_profile.toml [market_velocity].target_zips"

    try:
        static = json.loads(STATIC_SNAPSHOT.read_text())
        features = static["geojson"]["zips"]["features"]
        zips = [str(item["properties"]["zip"]).zfill(5) for item in features]
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError(
            "no [market_velocity].target_zips in buyer_profile.toml and no readable data.json ZIP map"
        ) from exc
    if not zips:
        raise ValueError("target ZIP list is empty")
    return sorted(set(zips)), "data.json geojson target ZIPs (compatibility fallback; promote to profile)"


def _open_url(url: str) -> BinaryIO:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": http.USER_AGENT, "Accept": "application/gzip"},
    )
    try:
        return urllib.request.urlopen(request, timeout=90)
    except urllib.error.HTTPError as exc:
        message = f"HTTP {exc.code} {exc.reason}"
        if exc.code in (401, 403, 429):
            raise http.SourceRejected(message) from exc
        raise http.SourceUnavailable(message) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise http.SourceUnavailable(f"{type(exc).__name__}: {exc}") from exc


def _number(value: str | None) -> float | int | None:
    if value is None or value.strip().upper() in {"", "NA", "N/A"}:
        return None
    try:
        parsed = float(value)
    except ValueError as exc:
        raise ValueError(f"invalid numeric Redfin value {value!r}") from exc
    return int(parsed) if parsed.is_integer() else parsed


def _valid_header(fieldnames: list[str] | None) -> None:
    names = set(fieldnames or [])
    missing = sorted(SOURCE_COLUMNS - names)
    if missing:
        raise ValueError(
            "Redfin market tracker columns changed or stream was truncated; missing " + ", ".join(missing)
        )


def _target_identifier(scope: str, row: dict[str, str | None]) -> str | None:
    """Translate Redfin's human-readable REGION into our stable local geography key.

    ``REGION_TYPE_ID`` identifies the *type* (for example, ``2`` for ZIP code), not a
    particular ZIP, county, or CBSA in the current Market Tracker schema.  Treating it as an
    identifier would silently join unrelated observations.  The published REGION label is
    therefore matched exactly for the one county and metro, and parsed strictly for the
    five-digit ZIP label.
    """

    label = (row.get("REGION") or "").strip()
    if scope == "county":
        return COUNTY_FIPS if label == COUNTY_LABEL else None
    if scope == "metro":
        return METRO_CODE if label == METRO_SOURCE_LABEL else None
    match = re.fullmatch(r"Zip Code:\s*(\d{5})", label)
    return match.group(1) if match else None


def stream_rows(scope: str, wanted_ids: set[str], *, opener=_open_url) -> dict[str, list[dict[str, Any]]]:
    """Stream one gzip TSV and retain only all-residential, non-seasonally-adjusted target rows."""

    if scope not in RAW_URLS:
        raise ValueError(f"unknown Redfin scope {scope!r}")
    retained: dict[str, list[dict[str, Any]]] = {item: [] for item in wanted_ids}
    response: BinaryIO | None = None
    try:
        response = opener(RAW_URLS[scope])
        # GzipFile streams from the socket.  It catches no decompression error here: an EOF
        # or CRC failure is evidence the archive is incomplete and must stop the refresh.
        with gzip.GzipFile(fileobj=response, mode="rb") as compressed:
            with io.TextIOWrapper(compressed, encoding="utf-8-sig", newline="") as text:
                reader = csv.DictReader(text, delimiter="\t")
                _valid_header(reader.fieldnames)
                for row in reader:
                    identifier = _target_identifier(scope, row)
                    if identifier not in wanted_ids:
                        continue
                    if (row.get("REGION_TYPE") or "").strip().lower() != (
                        "zip code" if scope == "zip" else scope
                    ):
                        continue
                    if (row.get("PROPERTY_TYPE") or "").strip().lower() != "all residential":
                        continue
                    if (row.get("IS_SEASONALLY_ADJUSTED") or "").strip().lower() == "true":
                        continue
                    period_begin = (row.get("PERIOD_BEGIN") or "").strip()
                    period_end = (row.get("PERIOD_END") or "").strip()
                    # Parsing now turns an upstream shape change into a loud refresh failure
                    # instead of publishing a snapshot whose chronological math is nonsense.
                    date.fromisoformat(period_begin)
                    date.fromisoformat(period_end)
                    retained[identifier].append(
                        {
                            "period_begin": period_begin,
                            "period_end": period_end,
                            "period_duration_days": int(float(row["PERIOD_DURATION"])),
                            "label": (row.get("REGION") or "").strip() or None,
                            "last_updated": (row.get("LAST_UPDATED") or "").strip() or None,
                            **{metric: _number(row.get(column)) for column, metric in COLUMN_TO_METRIC.items()},
                        }
                    )
    except (OSError, EOFError, gzip.BadGzipFile, UnicodeDecodeError, csv.Error) as exc:
        raise http.SourceUnavailable(f"cannot stream Redfin {scope} archive: {type(exc).__name__}: {exc}") from exc
    finally:
        if response is not None:
            response.close()
    return retained


def _month_number(day: date) -> int:
    return day.year * 12 + day.month


def _direction(value: float | None) -> str | None:
    if value is None:
        return None
    if value > 0:
        return "increasing"
    if value < 0:
        return "decreasing"
    return "flat"


def _slope(rows: list[dict[str, Any]], metric: str, months: int) -> dict[str, Any]:
    usable = [row for row in rows if row.get(metric) is not None][-months:]
    if len(usable) != months:
        return {"observations": len(usable), "slope_per_month": None, "direction": None}
    dates = [date.fromisoformat(row["period_begin"]) for row in usable]
    month_numbers = [_month_number(day) for day in dates]
    if any(right - left != 1 for left, right in zip(month_numbers, month_numbers[1:])):
        return {"observations": len(usable), "slope_per_month": None, "direction": None}
    values = [float(row[metric]) for row in usable]
    xbar = (months - 1) / 2
    ybar = sum(values) / months
    denominator = sum((index - xbar) ** 2 for index in range(months))
    slope = sum((index - xbar) * (value - ybar) for index, value in enumerate(values)) / denominator
    slope = round(slope, 6)
    return {"observations": months, "slope_per_month": slope, "direction": _direction(slope)}


def _same_month_prior(rows: list[dict[str, Any]], latest: dict[str, Any], metric: str) -> dict[str, Any]:
    value = latest.get(metric)
    if value is None:
        return {"prior_value": None, "absolute_change": None, "percent_change": None, "direction": None}
    current_date = date.fromisoformat(latest["period_begin"])
    try:
        prior_date = current_date.replace(year=current_date.year - 1)
    except ValueError:  # no Redfin periods begin on leap day, but refuse to silently shift one.
        return {"prior_value": None, "absolute_change": None, "percent_change": None, "direction": None}
    prior = next((row for row in rows if row["period_begin"] == prior_date.isoformat()), None)
    prior_value = prior.get(metric) if prior else None
    if prior_value is None:
        return {"prior_value": None, "absolute_change": None, "percent_change": None, "direction": None}
    change = round(float(value) - float(prior_value), 6)
    percent = None if float(prior_value) == 0 else round(change / float(prior_value), 6)
    return {
        "prior_value": prior_value,
        "absolute_change": change,
        "percent_change": percent,
        "direction": _direction(change),
    }


def summarise(rows: Iterable[dict[str, Any]]) -> dict[str, Any] | None:
    """Reduce one retained geography to latest figures, same-month YoY, and descriptive slopes."""

    ordered = sorted(rows, key=lambda item: item["period_begin"])
    # Duplicates can arise from a Redfin correction.  The source's LAST_UPDATED is retained;
    # sorting by it makes the latest correction win instead of summing incompatible records.
    by_period: dict[str, dict[str, Any]] = {}
    for row in ordered:
        existing = by_period.get(row["period_begin"])
        if existing is None or (row.get("last_updated") or "") >= (existing.get("last_updated") or ""):
            by_period[row["period_begin"]] = row
    ordered = [by_period[key] for key in sorted(by_period)]
    if not ordered:
        return None
    latest = ordered[-1]
    return {
        "label": latest["label"],
        "period_begin": latest["period_begin"],
        "period_end": latest["period_end"],
        "period_duration_days": latest["period_duration_days"],
        "last_updated": latest.get("last_updated"),
        "current": {metric: latest.get(metric) for metric in METRICS},
        "year_over_year": {metric: _same_month_prior(ordered, latest, metric) for metric in METRICS},
        "recent_slopes": {
            metric: {f"{months}_months": _slope(ordered, metric, months) for months in WINDOWS}
            for metric in METRICS
        },
        # Just 13 observations are needed to refresh all three requested slopes and the
        # same-month year-over-year comparison.  Keeping more would be raw-data mirroring.
        "history": [
            {"period_begin": row["period_begin"], **{metric: row.get(metric) for metric in METRICS}}
            for row in ordered[-13:]
        ],
    }


def build(*, opener=_open_url, fetched_at: str | None = None) -> dict[str, Any]:
    target_zips, target_origin = _read_profile_target_zips()
    county = stream_rows("county", {COUNTY_FIPS}, opener=opener)
    metro = stream_rows("metro", {METRO_CODE}, opener=opener)
    zips = stream_rows("zip", set(target_zips), opener=opener)

    county_market = summarise(county[COUNTY_FIPS])
    if county_market is None:
        raise ValueError(f"Redfin returned no county rows for {COUNTY_FIPS}")
    metro_market = summarise(metro[METRO_CODE])
    if metro_market is None:
        raise ValueError(f"Redfin returned no metro rows for CBSA {METRO_CODE}")
    # Census's July 2023 delineation lists both Los Angeles County (06037) and Orange County
    # (06059) in CBSA 31080, so the metro is strictly larger than Los Angeles County. The
    # prior county-as-metro shortcut is invalid here; retain Redfin's separate metro and county
    # archives as distinct series. Source: https://www2.census.gov/programs-surveys/metro-micro/geographies/reference-files/2023/delineation-files/list1_2023.xlsx
    metro_market = {**metro_market, "label": METRO_LABEL, "source_scope": "metro"}
    markets = {
        "metro": {METRO_CODE: metro_market},
        "county": {COUNTY_FIPS: county_market},
        "zip": {code: summarise(zips[code]) for code in target_zips},
    }
    missing = [
        f"{scope}:{identifier}"
        for scope, entries in markets.items()
        for identifier, entry in entries.items()
        if entry is None
    ]
    if missing:
        raise ValueError("Redfin returned no all-residential non-seasonally-adjusted rows for " + ", ".join(missing))

    all_latest = [
        item["period_end"]
        for entries in markets.values()
        for item in entries.values()
        if item is not None
    ]
    return {
        "schema_version": 1,
        "schema": {
            "markets": "metro/county/zip maps keyed by published Redfin identifiers",
            "current": "reported aggregate figures for latest period; null means source did not publish metric",
            "year_over_year": "derived comparison with the same calendar month one year earlier; no month-over-month trend is emitted",
            "recent_slopes": "derived least-squares slope per month across 3, 6, and 12 contiguous observations; short windows remain seasonal context",
            "history": "bounded 13-period input history retained only to audit derived comparisons",
        },
        "source_url": DATA_CENTER_URL,
        "raw_download_urls": RAW_URLS,
        "source_name": "Redfin Data Center Market Tracker",
        "attribution": "Data provided by Redfin, a national real estate brokerage.",
        "license_note": (
            "Redfin Data Center data is used here only for this non-commercial personal decision-support "
            "repository with Redfin attribution. The repository commits a small derived aggregate snapshot, "
            "does not retain or redistribute the raw downloads, and does not sell, sublicense, or use them "
            "to operate a commercial data product."
        ),
        "geography": {
            "metro_code": METRO_CODE,
            "metro_label": METRO_LABEL,
            "metro_scope_note": (
                "CBSA 31080 includes Los Angeles County (06037) and Orange County (06059), "
                "so it is larger than Los Angeles County. Metro and county are separate "
                "Redfin series, not interchangeable aggregates."
            ),
            "metro_scope_source_url": METRO_GEOGRAPHY_SOURCE,
            "state_fips": STATE_FIPS,
            "county_fips": COUNTY_FIPS,
            "county_label": COUNTY_LABEL,
            "target_zips": target_zips,
            "target_zip_source": target_origin,
        },
        "period_covered": {
            "latest_period_end": max(all_latest),
            "earliest_retained_period_begin": min(
                entry["history"][0]["period_begin"]
                for entries in markets.values()
                for entry in entries.values()
            ),
            "zip_period_note": "ZIP rows are Redfin trailing 90-day observations published on a monthly cadence.",
        },
        "seasonality": {
            "decision_comparison": "same-month year-over-year",
            "not_presented": "month-over-month change is intentionally not called a trend",
            "slope_caveat": "3- and 6-month slopes describe recent pace but can retain seasonality; use them with the 12-month slope and YoY comparison.",
        },
        "fetched_at": fetched_at or datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "markets": markets,
    }


def _read_snapshot() -> dict[str, Any]:
    try:
        payload = json.loads(SNAPSHOT.read_text())
    except FileNotFoundError:
        raise ValueError(f"{SNAPSHOT.relative_to(REPO)} is missing") from None
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read {SNAPSHOT.relative_to(REPO)}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("market velocity snapshot root must be an object")
    return payload


def check(snapshot: dict[str, Any], *, today: date | None = None) -> int:
    """Validate the committed artifact without re-downloading an upstream multi-gigabyte file."""

    required = {
        "schema_version", "schema", "source_url", "raw_download_urls", "license_note", "geography",
        "period_covered", "seasonality", "fetched_at", "markets",
    }
    missing = sorted(required - set(snapshot))
    if missing:
        print(f"market velocity snapshot missing keys: {missing}", file=sys.stderr)
        return 1
    if snapshot.get("schema_version") != 1:
        print("market velocity snapshot has unsupported schema_version", file=sys.stderr)
        return 1
    if not str(snapshot.get("source_url", "")).startswith("https://www.redfin.com/"):
        print("market velocity snapshot source_url is not Redfin Data Center", file=sys.stderr)
        return 1
    try:
        target_zips, _ = _read_profile_target_zips()
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 1
    got_zips = snapshot.get("geography", {}).get("target_zips")
    if got_zips != target_zips:
        print("market velocity snapshot target ZIPs drifted from configuration", file=sys.stderr)
        return 1
    try:
        _ = datetime.fromisoformat(str(snapshot["fetched_at"]).replace("Z", "+00:00"))
        latest_end = date.fromisoformat(snapshot["period_covered"]["latest_period_end"])
        if not snapshot["markets"]["county"].get(COUNTY_FIPS):
            raise ValueError(f"missing county {COUNTY_FIPS}")
        if not snapshot["markets"]["metro"].get(METRO_CODE):
            raise ValueError(f"missing metro {METRO_CODE}")
        for code in target_zips:
            market = snapshot["markets"]["zip"].get(code)
            if not market:
                raise ValueError(f"missing ZIP {code}")
            for metric in METRICS:
                if metric not in market.get("current", {}):
                    raise ValueError(f"ZIP {code} missing current {metric}")
                if metric not in market.get("year_over_year", {}):
                    raise ValueError(f"ZIP {code} missing YoY {metric}")
                if metric not in market.get("recent_slopes", {}):
                    raise ValueError(f"ZIP {code} missing slopes {metric}")
        stale = velocity.stale_reason(snapshot, today=today)
    except (KeyError, TypeError, ValueError) as exc:
        print(f"market velocity snapshot malformed: {exc}", file=sys.stderr)
        return 1
    if stale:
        print(stale, file=sys.stderr)
        return 1
    print(
        f"{SNAPSHOT.relative_to(REPO)}: Los Angeles-Long Beach-Anaheim metro + Los Angeles County + "
        f"{len(target_zips)} ZIPs, "
        f"through {latest_end.isoformat()}"
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="validate the committed snapshot without downloading")
    args = parser.parse_args()
    if args.check:
        try:
            return check(_read_snapshot())
        except ValueError as exc:
            print(exc, file=sys.stderr)
            return 1

    try:
        payload = build()
    except (http.SourceUnavailable, http.SourceRejected) as exc:
        # Monthly refresh must not make this repository red merely because Redfin is down.
        # Keep the old artifact (which carries its actual observation period) and report it.
        print(f"market velocity refresh skipped; source unavailable: {exc}")
        return 0
    except (ValueError, KeyError, TypeError) as exc:
        print(f"market velocity refresh refused; source schema changed: {exc}", file=sys.stderr)
        return 1

    SNAPSHOT.parent.mkdir(parents=True, exist_ok=True)
    rendered = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    previous = SNAPSHOT.read_text() if SNAPSHOT.exists() else None
    if rendered == previous:
        print(f"{SNAPSHOT.relative_to(REPO)} is unchanged")
        return 0
    SNAPSHOT.write_text(rendered)
    print(
        f"wrote {SNAPSHOT.relative_to(REPO)}: {len(payload['geography']['target_zips'])} ZIPs, "
        f"through {payload['period_covered']['latest_period_end']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
