"""Build the bounded Los Angeles County single-family parcel snapshot.

The Los Angeles County Assessor's current roll is the authoritative parcel source used by
``analyzer.sources.parcel``.  Its full county layer contains millions of parcels, so the
snapshot is deliberately bounded to the buyer profile's target ZIPs and Assessor use codes
beginning ``01`` (single-family residential).  It is an offline candidate index for this
household's actual search area, not a county archive.

California's current roll does not expose sale date or sale amount in this layer.  This
artifact consequently does not manufacture those columns.  It records the current assessed
value and Proposition 13 base year instead: those are the fields that explain why a listing's
tax figure can differ sharply from the buyer's post-close tax bill.

Usage:
    python tools/build_parcels_snapshot.py
    python tools/build_parcels_snapshot.py --check

``--check`` is offline.  It verifies the committed artifact and freshness boundary; a county
outage is not a CI failure.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import tomllib
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Callable

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from analyzer.sources import http, parcel  # noqa: E402

LAYER_URL = parcel.ASSESSOR_QUERY.rsplit("/query", 1)[0]
QUERY_URL = parcel.ASSESSOR_QUERY
SOURCE_DOCUMENTATION = parcel.ASSESSOR_DOC
LAYER_NAME = "Los Angeles County Assessor Parcels"

PARQUET = REPO / "data" / "parcels.parquet"
META = REPO / "data" / "parcels.meta.json"
CHECKPOINT = REPO / "data" / ".parcels.checkpoint.json"

MAX_IN_FLIGHT = 1
PAGE_SIZE = 1_000
MAX_AGE = timedelta(hours=36)
USER_AGENT = "home-buying-dashboard-parcels/0.1 (+https://github.com/flux1618/home-buying-dashboard)"


def _target_zips() -> tuple[str, ...]:
    """Read the profile-owned bounded search area rather than repeat ZIPs in this tool."""
    with open(REPO / "buyer_profile.toml", "rb") as fh:
        profile = tomllib.load(fh)
    zips = tuple(str(zip_code) for zip_code in profile["market_velocity"]["target_zips"])
    if not zips:
        raise SnapshotError("buyer_profile.toml has no [market_velocity] target_zips")
    return zips


TARGET_ZIPS = _target_zips()
_ZIP_SQL = ", ".join(f"'{zip_code}'" for zip_code in TARGET_ZIPS)
# Assessor use codes beginning 01 are single-family residential; see parcel.SFR_PREFIX.
SCOPE_WHERE = f"UseCode LIKE '{parcel.SFR_PREFIX}%' AND SitusZIP IN ({_ZIP_SQL})"

# Request the source station's explicit field list, plus the layer object id used solely for
# stable pagination.  This is deliberately not ``*``: renamed roll fields must stop a build.
REQUIRED_FIELDS = ("OBJECTID",) + tuple(parcel.PARCEL_FIELDS.split(","))
OUT_FIELDS = ",".join(REQUIRED_FIELDS)
OUTPUT_FIELDS = (
    "tax_pin",
    "situs_address",
    "use_code",
    "assessed_value",
    "tax_rate_area",
    "prop_13_base_year",
    "year_built",
    "living_sqft",
    "beds",
    "baths",
    "units",
)


class SnapshotError(RuntimeError):
    """The extractor refuses to publish a partial or schema-drifted snapshot."""


Fetch = Callable[[str], dict[str, Any]]


def fetch_json(url: str) -> dict[str, Any]:
    """Use the shared stdlib adapter: retries/backoff plus an explicit project UA."""
    payload = http.get_json(
        url,
        cache=False,
        headers={"User-Agent": USER_AGENT},
    ).data
    if not isinstance(payload, dict):
        raise SnapshotError(f"county response was {type(payload).__name__}, not a JSON object")
    if "error" in payload:
        error = payload["error"]
        raise SnapshotError(f"county ArcGIS error: {error.get('message', error)!s}")
    return payload


def _url(base: str, params: dict[str, Any]) -> str:
    return http.build_url(base, params)


def _metadata(fetch: Fetch) -> dict[str, Any]:
    metadata = fetch(_url(LAYER_URL, {"f": "json"}))
    advanced = metadata.get("advancedQueryCapabilities") or {}
    if not advanced.get("supportsPagination"):
        raise SnapshotError("Assessor parcel layer does not advertise supportsPagination")
    if not metadata.get("maxRecordCount"):
        raise SnapshotError("Assessor parcel metadata did not provide maxRecordCount")
    names = {field.get("name") for field in metadata.get("fields", [])}
    missing = [field for field in REQUIRED_FIELDS if field not in names]
    if missing:
        raise SnapshotError(
            "Assessor parcel field(s) disappeared: " + ", ".join(missing)
        )
    return metadata


def _checkpoint_load() -> dict[str, Any] | None:
    try:
        saved = json.loads(CHECKPOINT.read_text())
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError):
        return None
    # A changed scope or output schema is a different run. Never blend pages from both.
    if saved.get("scope_where") != SCOPE_WHERE or saved.get("output_fields") != list(OUTPUT_FIELDS):
        return None
    return saved


def _checkpoint_save(
    metadata: dict[str, Any],
    rows: list[dict[str, Any]],
    next_offset: int,
    failed_pages: list[dict[str, Any]],
) -> None:
    CHECKPOINT.write_text(
        json.dumps(
            {
                "scope_where": SCOPE_WHERE,
                "output_fields": list(OUTPUT_FIELDS),
                "layer_fields": [f.get("name") for f in metadata.get("fields", [])],
                "rows": rows,
                "next_offset": next_offset,
                "failed_pages": failed_pages,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )


def shape(attrs: dict[str, Any]) -> dict[str, Any]:
    """Shape raw Assessor fields using the same normalization as the live parcel station."""
    buildings = parcel.read_buildings(attrs)
    land = parcel.positive_float(attrs.get("Roll_LandValue")) or 0.0
    improvement = parcel.positive_float(attrs.get("Roll_ImpValue")) or 0.0
    assessed = (land + improvement) or None
    return {
        "tax_pin": parcel.clean(attrs.get("AIN")) or parcel.clean(attrs.get("APN")),
        "situs_address": parcel.situs(attrs),
        "use_code": parcel.clean(attrs.get("UseCode")),
        "assessed_value": assessed,
        "tax_rate_area": parcel.normalize_tra(attrs.get("TaxRateArea")),
        "prop_13_base_year": parcel.positive_int(attrs.get("Roll_LandBaseYear")),
        "year_built": buildings["year_built"],
        "living_sqft": buildings["living_sqft"],
        "beds": buildings["beds"],
        "baths": buildings["baths"],
        "units": buildings["units"],
    }


def extract(fetch: Fetch = fetch_json) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    """Fetch every scoped page, checkpointing after each one for safe resumption."""
    metadata = _metadata(fetch)
    resumed = _checkpoint_load()
    rows = list(resumed.get("rows", [])) if resumed else []
    offset = int(resumed.get("next_offset", 0)) if resumed else 0
    failed_pages: list[dict[str, Any]] = []

    count_payload = fetch(_url(QUERY_URL, {"where": SCOPE_WHERE, "returnCountOnly": "true", "f": "json"}))
    expected = count_payload.get("count")
    if not isinstance(expected, int):
        raise SnapshotError("county count query did not return an integer `count`")
    if expected == 0:
        return metadata, [], failed_pages

    while offset < expected:
        params = {
            "where": SCOPE_WHERE,
            "outFields": OUT_FIELDS,
            "returnGeometry": "false",
            "orderByFields": "OBJECTID ASC",
            "resultOffset": offset,
            "resultRecordCount": min(PAGE_SIZE, expected - offset),
            "f": "json",
        }
        try:
            payload = fetch(_url(QUERY_URL, params))
            features = payload.get("features")
            if not isinstance(features, list):
                raise SnapshotError("county page did not include a feature list")
            if not features:
                raise SnapshotError(f"county page at offset {offset} was unexpectedly empty")
            page_rows = [shape(feature.get("attributes") or {}) for feature in features]
        except Exception as exc:
            failure = {"offset": offset, "limit": min(PAGE_SIZE, expected - offset), "error": str(exc)}
            failed_pages.append(failure)
            _checkpoint_save(metadata, rows, offset, failed_pages)
            raise SnapshotError(f"parcel extraction stopped at page offset {offset}: {exc}") from exc

        rows.extend(page_rows)
        offset += len(page_rows)
        _checkpoint_save(metadata, rows, offset, failed_pages)
        if len(page_rows) < min(PAGE_SIZE, expected - (offset - len(page_rows))) and offset < expected:
            raise SnapshotError(f"county returned a short page at offset {offset - len(page_rows)}")

    if len(rows) != expected:
        raise SnapshotError(f"county count was {expected}, but extraction shaped {len(rows)} rows")
    return metadata, rows, failed_pages


def _require_pyarrow():
    try:
        import pyarrow as pa
        import pyarrow.parquet as pq
    except ImportError as exc:
        raise SnapshotError(
            "pyarrow is required to write parcels.parquet; install the planned `.[parcels]` extra"
        ) from exc
    return pa, pq


def _content_sha256(rows: list[dict[str, Any]]) -> str:
    encoded = json.dumps(rows, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    return hashlib.sha256(encoded).hexdigest()


def write_snapshot(metadata: dict[str, Any], rows: list[dict[str, Any]], failed_pages: list[dict[str, Any]]) -> bool:
    content_sha256 = _content_sha256(rows)
    try:
        existing = json.loads(META.read_text())
    except (OSError, json.JSONDecodeError):
        existing = {}
    if PARQUET.exists() and existing.get("content_sha256") == content_sha256:
        CHECKPOINT.unlink(missing_ok=True)
        return False

    pa, pq = _require_pyarrow()
    table = pa.Table.from_pylist(
        rows,
        schema=pa.schema(
            [
                pa.field("tax_pin", pa.string()),
                pa.field("situs_address", pa.string()),
                pa.field("use_code", pa.string()),
                pa.field("assessed_value", pa.float64()),
                pa.field("tax_rate_area", pa.string()),
                pa.field("prop_13_base_year", pa.int64()),
                pa.field("year_built", pa.int64()),
                pa.field("living_sqft", pa.int64()),
                pa.field("beds", pa.int64()),
                pa.field("baths", pa.float64()),
                pa.field("units", pa.int64()),
            ]
        ),
    )
    temp_parquet = PARQUET.with_suffix(".parquet.tmp")
    pq.write_table(table, temp_parquet, compression="zstd")
    temp_parquet.replace(PARQUET)

    now = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    meta = {
        "source_url": QUERY_URL,
        "source_documentation_url": SOURCE_DOCUMENTATION,
        "layer_name": metadata.get("name"),
        "service_version": metadata.get("currentVersion"),
        "fetched_at": now,
        "row_count": len(rows),
        "content_sha256": content_sha256,
        "field_list": list(OUTPUT_FIELDS),
        "source_field_list": [field.get("name") for field in metadata.get("fields", [])],
        "scope_where": SCOPE_WHERE,
        "scope_note": (
            "Los Angeles County Assessor single-family parcels (UseCode 01*) in the "
            "buyer profile's target ZIPs; not a county-wide archive."
        ),
        "field_notes": {
            "tax_pin": "AIN, falling back to APN when AIN is blank",
            "situs_address": "SitusFullAddress, falling back to SitusAddress",
            "use_code": "UseCode; scope admits Assessor single-family residential prefix 01",
            "assessed_value": "Roll_LandValue + Roll_ImpValue",
            "tax_rate_area": "TaxRateArea normalized to a five-digit TRA string",
            "prop_13_base_year": "Roll_LandBaseYear",
            "year_built": "earliest populated YearBuilt1..YearBuilt5 slot",
            "living_sqft": "sum of populated SQFTmain1..SQFTmain5 slots",
            "beds": "sum of populated Bedrooms1..Bedrooms5 slots",
            "baths": "sum of populated Bathrooms1..Bathrooms5 slots",
            "units": "sum of populated Units1..Units5 slots",
            "last_sale": "not included: the current Assessor roll layer does not expose sale date or amount",
        },
        "pagination": {
            "supports_pagination": bool((metadata.get("advancedQueryCapabilities") or {}).get("supportsPagination")),
            "max_record_count": metadata.get("maxRecordCount"),
            "page_size": PAGE_SIZE,
            "max_in_flight": MAX_IN_FLIGHT,
        },
        "failed_pages": failed_pages,
        "status": "complete",
    }
    META.write_text(json.dumps(meta, indent=2, sort_keys=True) + "\n")
    CHECKPOINT.unlink(missing_ok=True)
    return True


def check(now: datetime | None = None) -> int:
    """Offline CI check: structural correctness and an explicit freshness boundary."""
    if not PARQUET.exists() or not META.exists():
        print("parcel snapshot or metadata is missing; run tools/build_parcels_snapshot.py", file=sys.stderr)
        return 1
    try:
        meta = json.loads(META.read_text())
        fetched = datetime.fromisoformat(meta["fetched_at"].replace("Z", "+00:00"))
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        print(f"parcel metadata is unreadable: {exc}", file=sys.stderr)
        return 1
    problems: list[str] = []
    if meta.get("status") != "complete":
        problems.append("snapshot metadata is not complete")
    if meta.get("field_list") != list(OUTPUT_FIELDS):
        problems.append("snapshot fields do not match the bounded parcel contract")
    if not isinstance(meta.get("row_count"), int) or meta["row_count"] < 0:
        problems.append("snapshot row count is missing or invalid")
    if meta.get("failed_pages"):
        problems.append("snapshot records failed pages")
    age = (now or datetime.now(UTC)) - fetched
    if age > MAX_AGE:
        problems.append(f"snapshot is stale ({age.days} days old; maximum is {MAX_AGE})")
    if problems:
        print("parcel snapshot check failed: " + "; ".join(problems), file=sys.stderr)
        return 1
    print(f"parcel snapshot: {meta['row_count']} rows, fetched {meta['fetched_at']}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="verify committed snapshot offline")
    args = parser.parse_args()
    if args.check:
        return check()
    try:
        metadata, rows, failed_pages = extract()
        changed = write_snapshot(metadata, rows, failed_pages)
    except (SnapshotError, http.SourceUnavailable, http.SourceRejected) as exc:
        print(f"parcel snapshot degraded: {exc}", file=sys.stderr)
        return 2
    action = "wrote" if changed else "left unchanged"
    print(f"{action} {PARQUET.relative_to(REPO)} with {len(rows)} scoped parcels")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
