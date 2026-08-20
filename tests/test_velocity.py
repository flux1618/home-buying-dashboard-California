"""Bounded Redfin market-velocity snapshot and station contract.

The production downloads are large, so the offline cases feed real gzip/TSV-shaped bytes into
the builder's injected transport.  This tests the parser and failure behavior without making the
regular suite dependent on Redfin.
"""

from __future__ import annotations

import csv
import gzip
import importlib.util
import io
import json
import urllib.error
import urllib.request
from datetime import date
from pathlib import Path

import pytest

from analyzer.sources import http
from analyzer.sources.base import Context
from analyzer.sources.velocity import MarketVelocityStation

REPO = Path(__file__).resolve().parents[1]
SNAPSHOT = json.loads((REPO / "data" / "market_velocity.json").read_text())


def load_builder():
    """Load the executable tool by path, matching the hazard snapshot tests."""

    path = REPO / "tools" / "build_velocity_snapshot.py"
    spec = importlib.util.spec_from_file_location("build_velocity_snapshot", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def month_starts() -> list[str]:
    """Thirteen contiguous months: enough for all requested slopes plus same-month YoY."""

    return [
        "2025-01-01", "2025-02-01", "2025-03-01", "2025-04-01", "2025-05-01",
        "2025-06-01", "2025-07-01", "2025-08-01", "2025-09-01", "2025-10-01",
        "2025-11-01", "2025-12-01", "2026-01-01",
    ]


def gzip_tsv(
    builder,
    *,
    scope: str,
    zip_code: str = "90031",
    median_dom_offset: int = 0,
    renamed: bool = False,
) -> bytes:
    """A transport fixture with the source's actual delimiter, labels, and required columns."""

    fields = sorted(builder.SOURCE_COLUMNS)
    if renamed:
        fields[fields.index("MEDIAN_DOM")] = "MEDIAN_DAYS_ON_MARKET"
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=fields, delimiter="\t", lineterminator="\n")
    writer.writeheader()
    for index, period in enumerate(month_starts()):
        year, month, _ = period.split("-")
        end = f"{year}-{month}-28"
        row = {
            "PERIOD_BEGIN": period,
            "PERIOD_END": end,
            "PERIOD_DURATION": "90" if scope == "zip" else "31",
            "REGION_TYPE": "zip code" if scope == "zip" else scope,
            # These are source type codes, deliberately not the ZIP/FIPS identifier.
            "REGION_TYPE_ID": "2" if scope == "zip" else ("5" if scope == "county" else "-2"),
            "PROPERTY_TYPE": "All Residential",
            "IS_SEASONALLY_ADJUSTED": "false",
            "REGION": (
                f"Zip Code: {zip_code}" if scope == "zip"
                else "Los Angeles County, CA" if scope == "county"
                else "Los Angeles, CA"
            ),
            "MEDIAN_DOM": str(20 + index + median_dom_offset),
            "MONTHS_OF_SUPPLY": str(2 + index / 10),
            "INVENTORY": str(100 + index),
            "NEW_LISTINGS": str(50 + index),
            "PRICE_DROPS": str(0.1 + index / 100),
            "AVG_SALE_TO_LIST": str(0.98 + index / 1000),
            "LAST_UPDATED": "2026-02-15 00:00:00",
        }
        if renamed:
            row["MEDIAN_DAYS_ON_MARKET"] = row.pop("MEDIAN_DOM")
        writer.writerow(row)
    return gzip.compress(output.getvalue().encode())


class GzipTransport:
    """Small injected transport following the suite's explicit-stub principle."""

    def __init__(
        self,
        builder,
        *,
        county: bytes | BaseException,
        metro: bytes | BaseException,
        zip_rows: bytes | BaseException,
    ):
        self.builder = builder
        self.county = county
        self.metro = metro
        self.zip_rows = zip_rows
        self.calls: list[str] = []

    def __call__(self, url: str):
        self.calls.append(url)
        payload = (
            self.zip_rows if "zip_code" in url
            else self.metro if "metro_market_tracker" in url
            else self.county
        )
        if isinstance(payload, BaseException):
            raise payload
        return io.BytesIO(payload)


def test_normal_parse_streams_and_reduces_realistic_gzip(monkeypatch):
    builder = load_builder()
    monkeypatch.setattr(builder, "_read_profile_target_zips", lambda: (["90031"], "test"))
    transport = GzipTransport(
        builder,
        county=gzip_tsv(builder, scope="county"),
        metro=gzip_tsv(builder, scope="metro", median_dom_offset=100),
        zip_rows=gzip_tsv(builder, scope="zip"),
    )

    result = builder.build(opener=transport, fetched_at="2026-02-16T00:00:00+00:00")

    assert result["markets"]["county"]["06037"]["current"]["median_days_on_market"] == 32
    assert result["markets"]["metro"]["31080"]["label"] == "Los Angeles-Long Beach-Anaheim, CA"
    assert result["markets"]["metro"]["31080"]["source_scope"] == "metro"
    assert result["markets"]["metro"]["31080"]["current"]["median_days_on_market"] == 132
    assert result["markets"]["zip"]["90031"]["current"]["months_of_supply"] == pytest.approx(3.2)
    assert result["markets"]["zip"]["90031"]["year_over_year"]["new_listings"]["absolute_change"] == 12
    assert result["markets"]["zip"]["90031"]["recent_slopes"]["active_inventory"]["12_months"]["direction"] == "increasing"
    assert len(transport.calls) == 3
    assert result["geography"]["metro_scope_note"] == (
        "CBSA 31080 includes Los Angeles County (06037) and Orange County (06059), "
        "so it is larger than Los Angeles County. Metro and county are separate "
        "Redfin series, not interchangeable aggregates."
    )
    assert result["geography"]["metro_scope_source_url"] == (
        "https://www2.census.gov/programs-surveys/metro-micro/geographies/reference-files/"
        "2023/delineation-files/list1_2023.xlsx"
    )


def test_missing_geography_stays_none_and_requests_no_nearby_substitute(tmp_path):
    path = tmp_path / "market_velocity.json"
    path.write_text(json.dumps(SNAPSHOT))
    station = MarketVelocityStation(path, today=date(2026, 8, 19))

    result = station.run(Context(address="Unknown, CA 99999", price=1, county_fips="06001"))

    assert result.ok
    market = result.facts["market_velocity"]
    assert market["county"] is None
    assert market["zip"] is None
    assert any("do not infer" in task["task"] for task in result.tasks)
    assert any("do not substitute" in task["task"] for task in result.tasks)


def test_renamed_upstream_column_is_refused_not_guessed():
    builder = load_builder()
    renamed = gzip_tsv(builder, scope="county", renamed=True)
    transport = GzipTransport(builder, county=renamed, metro=renamed, zip_rows=renamed)

    with pytest.raises(ValueError, match="columns changed"):
        builder.stream_rows("county", {"06037"}, opener=transport)


def test_truncated_gzip_is_a_clean_source_outage():
    builder = load_builder()
    complete = gzip_tsv(builder, scope="county")
    transport = GzipTransport(builder, county=complete[:-12], metro=complete, zip_rows=complete)

    with pytest.raises(http.SourceUnavailable, match="cannot stream"):
        builder.stream_rows("county", {"06037"}, opener=transport)


def test_http_500_is_a_clean_source_outage(monkeypatch):
    builder = load_builder()

    def fail(_request, timeout):
        raise urllib.error.HTTPError("https://example.invalid", 500, "Internal Server Error", {}, None)

    monkeypatch.setattr(urllib.request, "urlopen", fail)
    with pytest.raises(http.SourceUnavailable, match="HTTP 500"):
        builder._open_url("https://example.invalid")


def test_stale_snapshot_degrades_instead_of_pretending_market_is_current(tmp_path):
    stale = json.loads(json.dumps(SNAPSHOT))
    stale["period_covered"]["latest_period_end"] = "2026-01-01"
    path = tmp_path / "market_velocity.json"
    path.write_text(json.dumps(stale))

    result = MarketVelocityStation(path, today=date(2026, 8, 19)).run(
        Context(address="1500 San Pablo St, Los Angeles, CA 90033", price=1, county_fips="06037")
    )

    assert result.degradation is not None
    assert "refresh is due" in result.degradation.reason


def test_malformed_snapshot_degrades_under_the_station_never_raise_contract(tmp_path):
    path = tmp_path / "market_velocity.json"
    path.write_text(json.dumps({"schema_version": 1, "source_url": "https://example.invalid", "markets": {}}))

    result = MarketVelocityStation(path).run(Context(address="x 90031", price=1, county_fips="06037"))

    assert result.degradation is not None
    assert "period_covered" in result.degradation.reason


def test_seasonality_never_labels_month_over_month_as_a_trend(tmp_path):
    snapshot = json.loads(json.dumps(SNAPSHOT))
    snapshot["markets"]["county"]["06037"] = snapshot["markets"]["county"].pop("45083")
    snapshot["markets"]["metro"]["31080"] = snapshot["markets"]["metro"].pop("43900")
    path = tmp_path / "market_velocity.json"
    path.write_text(json.dumps(snapshot))
    station = MarketVelocityStation(path, today=date(2026, 8, 19))
    result = station.run(Context(address="1500 San Pablo St, Los Angeles, CA 90033", price=1, county_fips="06037"))

    assert result.ok
    seasonality = result.facts["market_velocity"]["seasonality"]
    assert seasonality["decision_comparison"] == "same-month year-over-year"
    assert "month-over-month" in seasonality["not_presented"]
    assert not any("month_over_month" in key for key in result.values)
    assert "not a month-over-month claim" in next(
        value.note for key, value in result.values.items() if key.endswith("_year_over_year_change")
    )


@pytest.mark.live
def test_live_redfin_county_header_contract():
    """A small ranged read catches a renamed source column without downloading the archive."""

    builder = load_builder()
    request = urllib.request.Request(
        builder.RAW_URLS["county"],
        headers={"Range": "bytes=0-1048575", "User-Agent": http.USER_AGENT},
    )
    with urllib.request.urlopen(request, timeout=90) as response:
        import gzip

        with gzip.GzipFile(fileobj=response, mode="rb") as compressed:
            header_line = compressed.readline().decode("utf-8-sig").rstrip("\r\n")
    header = next(csv.reader([header_line], delimiter="\t"))
    assert builder.SOURCE_COLUMNS <= set(header)
