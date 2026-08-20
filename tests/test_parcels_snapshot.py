"""Offline tests for the bounded Los Angeles County Assessor parcel snapshot.

The builder's live work is deliberately behind a small callable transport. Tests use the
same no-network discipline as source-station tests: fixtures describe ArcGIS responses and
an unexpected request fails before it can become a real county request.
"""

from __future__ import annotations

import importlib.util
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

pytest.importorskip("pyarrow", reason="parcel snapshot writer is an optional pyarrow extra")

REPO = Path(__file__).resolve().parents[1]


def load_builder():
    path = REPO / "tools" / "build_parcels_snapshot.py"
    spec = importlib.util.spec_from_file_location("build_parcels_snapshot", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def builder(tmp_path, monkeypatch):
    b = load_builder()
    monkeypatch.setattr(b, "PARQUET", tmp_path / "parcels.parquet")
    monkeypatch.setattr(b, "META", tmp_path / "parcels.meta.json")
    monkeypatch.setattr(b, "CHECKPOINT", tmp_path / ".parcels.checkpoint.json")
    return b


def metadata(builder, *, missing: str | None = None):
    fields = [{"name": field} for field in builder.REQUIRED_FIELDS if field != missing]
    return {
        "name": "Parcels",
        "currentVersion": 10.91,
        "maxRecordCount": 1000,
        "advancedQueryCapabilities": {"supportsPagination": True},
        "fields": fields,
    }


def attrs(n: int) -> dict:
    return {
        "OBJECTID": n,
        "AIN": f"000000{n:03d}",
        "APN": f"5202-009-{n:03d}",
        "SitusHouseNo": str(2400 + n),
        "SitusAddress": f"{2400 + n} VERDE ST",
        "SitusFullAddress": f"{2400 + n} VERDE ST LOS ANGELES CA 90033",
        "SitusZIP": "90033-1427",
        "TaxRateArea": "4",
        "UseCode": "0100",
        "Roll_LandValue": 1000.00,
        "Roll_ImpValue": float(n),
        "Roll_LandBaseYear": "1994",
        "YearBuilt1": "1922",
        "SQFTmain1": 1564,
        "Bedrooms1": 3,
        "Bathrooms1": 2.5,
        "Units1": 1,
        **{
            f"{field}{slot}": None
            for slot in range(2, 6)
            for field in ("YearBuilt", "EffectiveYear", "Units", "Bedrooms", "Bathrooms", "SQFTmain")
        },
        "EffectiveYear1": "1922",
    }


class CountyTransport:
    """A request-aware ArcGIS stub, parallel to tests.conftest.FakeHTTP."""

    def __init__(self, builder, *, count: int, fail_offset: int | None = None, missing: str | None = None):
        self.builder = builder
        self.count = count
        self.fail_offset = fail_offset
        self.layer = metadata(builder, missing=missing)
        self.calls: list[dict[str, list[str]]] = []

    def __call__(self, url: str) -> dict:
        parsed = urlparse(url)
        params = parse_qs(parsed.query)
        self.calls.append(params)
        if parsed.path.endswith("/0"):
            return self.layer
        if params.get("returnCountOnly") == ["true"]:
            return {"count": self.count}
        offset = int(params["resultOffset"][0])
        if offset == self.fail_offset:
            raise RuntimeError("county closed the connection")
        limit = int(params["resultRecordCount"][0])
        return {
            "features": [
                {"attributes": attrs(i)}
                for i in range(offset + 1, min(offset + limit, self.count) + 1)
            ]
        }


class TestPagedExtraction:
    def test_reads_all_pages_in_stable_objectid_order(self, builder):
        transport = CountyTransport(builder, count=2_501)

        layer, rows, failures = builder.extract(transport)

        assert layer["name"] == "Parcels"
        assert len(rows) == 2_501
        assert [r["tax_pin"] for r in rows[:2]] == ["000000001", "000000002"]
        assert rows[-1]["assessed_value"] == 3501.0
        assert rows[0]["tax_rate_area"] == "00004"
        assert rows[0]["year_built"] == 1922
        assert rows[0]["baths"] == 2.5
        assert failures == []
        offsets = [int(c["resultOffset"][0]) for c in transport.calls if "resultOffset" in c]
        assert offsets == [0, 1000, 2000]
        assert all(c.get("orderByFields") == ["OBJECTID ASC"] for c in transport.calls if "resultOffset" in c)

    def test_scope_is_profile_zips_and_assessor_single_family_codes(self, builder):
        transport = CountyTransport(builder, count=1)
        builder.extract(transport)
        count_query = next(c for c in transport.calls if c.get("returnCountOnly") == ["true"])
        where = count_query["where"][0]
        assert f"UseCode LIKE '{builder.parcel.SFR_PREFIX}%'" in where
        assert "SitusZIP IN" in where
        assert "'90033'" in where

    def test_mid_run_page_failure_keeps_a_resume_checkpoint_and_refuses_output(self, builder):
        transport = CountyTransport(builder, count=2_001, fail_offset=1_000)

        with pytest.raises(builder.SnapshotError, match="offset 1000"):
            builder.extract(transport)

        checkpoint = json.loads(builder.CHECKPOINT.read_text())
        assert checkpoint["next_offset"] == 1_000
        assert len(checkpoint["rows"]) == 1_000
        assert checkpoint["failed_pages"] == [
            {"offset": 1_000, "limit": 1_000, "error": "county closed the connection"}
        ]
        assert not builder.PARQUET.exists(), "a partial page must never be publishable"

        recovered = CountyTransport(builder, count=2_001)
        _, rows, failures = builder.extract(recovered)
        assert len(rows) == 2_001
        assert failures == []
        resumed_offsets = [int(c["resultOffset"][0]) for c in recovered.calls if "resultOffset" in c]
        assert resumed_offsets == [1_000, 2_000]

    def test_a_disappearing_required_field_stops_before_the_count_or_any_page(self, builder):
        transport = CountyTransport(builder, count=1, missing="Roll_ImpValue")

        with pytest.raises(builder.SnapshotError, match=r"field\(s\) disappeared.*Roll_ImpValue"):
            builder.extract(transport)

        assert len(transport.calls) == 1

    def test_empty_scope_is_a_valid_empty_parquet_not_a_failed_count(self, builder):
        transport = CountyTransport(builder, count=0)
        layer, rows, failures = builder.extract(transport)
        builder.write_snapshot(layer, rows, failures)

        assert rows == []
        meta = json.loads(builder.META.read_text())
        assert meta["row_count"] == 0
        assert meta["failed_pages"] == []
        import pyarrow.parquet as pq

        assert pq.read_table(builder.PARQUET).num_rows == 0


class TestOfflineCheck:
    def _complete_meta(self, builder, fetched_at: datetime) -> None:
        builder.PARQUET.write_bytes(b"not read by check")
        builder.META.write_text(
            json.dumps(
                {
                    "status": "complete",
                    "fetched_at": fetched_at.isoformat().replace("+00:00", "Z"),
                    "row_count": 12,
                    "field_list": list(builder.OUTPUT_FIELDS),
                    "failed_pages": [],
                }
            )
        )

    def test_check_passes_a_fresh_complete_snapshot_without_transport(self, builder):
        now = datetime(2026, 8, 20, tzinfo=UTC)
        self._complete_meta(builder, now - timedelta(hours=1))

        assert builder.check(now=now) == 0

    def test_check_marks_an_old_artifact_stale_without_hitting_the_county(self, builder):
        now = datetime(2026, 8, 20, tzinfo=UTC)
        self._complete_meta(builder, now - builder.MAX_AGE - timedelta(seconds=1))

        assert builder.check(now=now) == 1


@pytest.mark.live
def test_live_county_layer_still_exposes_the_bounded_snapshot_contract():
    """A real endpoint contract, excluded from normal CI like other source live tests."""
    b = load_builder()
    layer = b._metadata(b.fetch_json)

    assert layer["currentVersion"] >= 10
    assert layer["advancedQueryCapabilities"]["supportsPagination"] is True
    assert layer["maxRecordCount"] >= b.PAGE_SIZE
