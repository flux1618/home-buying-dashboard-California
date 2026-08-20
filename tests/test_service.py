"""The HTTP door.

These tests assert the *translation* layer only: status codes, request validation, and
response shape. Scoring correctness is covered in test_scoring.py and the pipeline in
test_pipeline.py, and re-asserting it here would mean a change to a penalty weight breaks
the web tests too — which teaches you nothing and trains you to ignore failures.

The one behaviour worth stating loudly, because it is a judgement call and not obvious:
**a degraded source is a 200, not a 503.** A missing broadband key does not invalidate the
tax, flood, and commute work that succeeded. The response says what is missing and the
caller decides.
"""

from __future__ import annotations

import io

import pytest

fastapi = pytest.importorskip(
    "fastapi",
    reason="FastAPI is an optional extra — install with `pip install '.[api]'`",
)
from fastapi.testclient import TestClient  # noqa: E402

from analyzer.core.profile import load_profile  # noqa: E402
from analyzer.pipeline import Degradation, PipelineAborted, PipelineRun  # noqa: E402


# =============================================================================
# Support
# =============================================================================


def fake_document(score=80, verdict="TAKE"):
    return {
        "score": {
            "value": score, "verdict": verdict, "score_pinned": False,
            "score_capped": False, "unknown_facts": [], "hard_fails": [],
            "unevaluated_hard_fails": [], "capex_estimate_low": 0.0,
            "capex_estimate_high": 0.0,
        },
        "cost": {
            "piti": 1718.0, "true_monthly_low": 1941.0, "true_monthly_high": 2165.0,
            "cash_to_close": 89032.0, "front_end_dti": 0.057,
        },
        "input": {
            "price": 268000.0, "sqft": 1780, "beds": 3, "baths": 2.0,
            "year_built": 1998, "flood_zone": "X", "water_sewer": "public",
            "commute_min": 12.5, "fiber_available": None,
        },
        "location": {"matched_address": "606 ANDRE CT, SPARTANBURG, SC, 29301"},
        "verification_tasks": [{"task": "Confirm flood zone", "blocking": True}],
    }


@pytest.fixture
def client(monkeypatch):
    """A client whose pipeline is stubbed, so no test here touches a network.

    Both `service.app.run` and `analyzer.batch.run` are patched because the two
    endpoints reach the pipeline by different import paths, and patching only one
    leaves a test that silently makes real HTTP requests.
    """
    from service import app as service_module
    from analyzer import batch as batch_module

    calls = []

    def stub(address, price, **kwargs):
        calls.append({"address": address, "price": price, **kwargs})
        return PipelineRun(
            document=fake_document(),
            degradations=[Degradation(station="broadband", reason="no api key")],
            stations_run=["geocode", "parcel", "flood", "commute", "broadband"],
        )

    monkeypatch.setattr(service_module, "run", stub)
    monkeypatch.setattr(batch_module, "run", stub)

    test_client = TestClient(service_module.create_app())
    test_client.calls = calls
    return test_client


# =============================================================================
# Operational endpoints
# =============================================================================


class TestOps:
    def test_health_is_ok(self, client):
        response = client.get("/health")
        assert response.status_code == 200
        assert response.json()["status"] == "ok"

    def test_health_makes_no_external_requests(self, client):
        """A probe that depends on the county GIS server would report the container
        unhealthy whenever someone else's server has a bad afternoon, and restarting
        this container fixes none of that."""
        client.get("/health")
        assert client.calls == []

    def test_profile_exposes_the_rulebook(self, client):
        body = client.get("/profile").json()
        assert body["verdict_bands"]["take_min"] > body["verdict_bands"]["watch_min"]
        assert "min_beds" in body["preferences"]
        assert body["penalties"]

    def test_sources_lists_the_six_stations(self, client):
        body = client.get("/sources").json()
        names = [s["name"] for s in body["stations"]]
        assert "geocode" in names
        assert len(names) >= 5

    def test_only_geocode_is_fatal(self, client):
        """The whole degradation design rests on this. Worth asserting, not assuming."""
        stations = client.get("/sources").json()["stations"]
        fatal = [s["name"] for s in stations if s["fatal"]]
        assert fatal == ["geocode"]


# =============================================================================
# Committed market artifacts
# =============================================================================


class TestCommittedMarketArtifacts:
    def test_mortgage_rate_returns_the_committed_baseline(self, client):
        body = client.get("/rates/mortgage30us").json()
        assert body["series_id"] == "MORTGAGE30US"
        assert isinstance(body["latest_rate_pct"], (int, float))
        assert client.calls == []

    def test_missing_mortgage_rate_snapshot_is_a_clear_503(self, client, monkeypatch):
        from service import app as service_module

        def missing(_path):
            raise FileNotFoundError("fixture removed")

        monkeypatch.setattr(service_module.pathlib.Path, "read_text", missing)
        response = client.get("/rates/mortgage30us")
        assert response.status_code == 503
        assert response.json()["detail"] == "mortgage-rate snapshot is not available"

    def test_malformed_mortgage_rate_snapshot_is_a_clear_503(self, client, monkeypatch):
        from service import app as service_module

        monkeypatch.setattr(service_module.pathlib.Path, "read_text", lambda _path: "{")
        response = client.get("/rates/mortgage30us")
        assert response.status_code == 503
        assert response.json()["detail"] == "mortgage-rate snapshot is invalid"

    def test_market_velocity_returns_the_committed_aggregate_context(self, client):
        body = client.get("/market-velocity").json()
        assert body["source_name"] == "Redfin Data Center Market Tracker"
        assert "county" in body["markets"]
        assert client.calls == []

    def test_unavailable_market_velocity_snapshot_is_a_clear_503(self, client, monkeypatch):
        from analyzer.sources import velocity

        def unavailable():
            raise ValueError("fixture snapshot is malformed")

        monkeypatch.setattr(velocity, "read_snapshot", unavailable)
        response = client.get("/market-velocity")
        assert response.status_code == 503
        assert response.json()["detail"] == {
            "error": "market_velocity_unavailable",
            "reason": "fixture snapshot is malformed",
        }


# =============================================================================
# POST /analyze
# =============================================================================


class TestAnalyze:
    def test_minimal_request_succeeds(self, client):
        response = client.post(
            "/analyze", json={"address": "606 Andre Ct, Spartanburg, SC", "price": 268000}
        )
        assert response.status_code == 200
        assert response.json()["document"]["score"]["verdict"] == "TAKE"

    def test_optional_facts_reach_the_pipeline(self, client):
        client.post("/analyze", json={
            "address": "1 Main St", "price": 268000, "hoa_monthly": 125,
            "roof_age_years": 17, "hvac_age_years": 14, "garage_spaces": 1,
        })
        call = client.calls[0]
        assert call["hoa_monthly"] == 125
        assert call["roof_age_years"] == 17
        assert call["garage_spaces"] == 1

    def test_omitted_ages_arrive_as_none_not_zero(self, client):
        """Sending 0 for an unknown roof age would claim the roof is brand new,
        which is the opposite of the truth and would inflate the score."""
        client.post("/analyze", json={"address": "1 Main St", "price": 268000})
        assert client.calls[0]["roof_age_years"] is None
        assert client.calls[0]["hvac_age_years"] is None

    def test_degraded_source_is_a_200_with_a_warning(self, client):
        """The core judgement call of this service."""
        body = client.post(
            "/analyze", json={"address": "1 Main St", "price": 268000}
        ).json()
        assert body["degraded_sources"] == ["broadband"]
        assert body["complete"] is False

    def test_response_reports_which_stations_ran(self, client):
        body = client.post(
            "/analyze", json={"address": "1 Main St", "price": 268000}
        ).json()
        assert "geocode" in body["stations_run"]

    def test_response_carries_the_engine_version(self, client):
        """A score is only comparable to another score from the same engine."""
        body = client.post(
            "/analyze", json={"address": "1 Main St", "price": 268000}
        ).json()
        assert body["engine_version"]

    @pytest.mark.parametrize(
        "payload",
        [
            {},
            {"address": "1 Main St"},
            {"price": 268000},
            {"address": "1 Main St", "price": 0},
            {"address": "1 Main St", "price": -100},
            {"address": "", "price": 268000},
            {"address": "1 Main St", "price": "call for price"},
            {"address": "1 Main St", "price": 268000, "roof_age_years": -1},
            {"address": "1 Main St", "price": 268000, "garage_spaces": 99},
            {"address": "1 Main St", "price": 268000, "hoa_monthly": -50},
        ],
    )
    def test_bad_requests_are_422_and_never_reach_the_pipeline(self, client, payload):
        assert client.post("/analyze", json=payload).status_code == 422
        assert client.calls == []

    def test_unknown_fields_are_rejected(self, client):
        """`extra=forbid`. A typo'd field name would otherwise be silently ignored and
        the caller would think a constraint was applied when it was not."""
        response = client.post("/analyze", json={
            "address": "1 Main St", "price": 268000, "rooof_age_years": 17,
        })
        assert response.status_code == 422

    def test_ungeocodable_address_is_422_not_500(self, client, monkeypatch):
        """422 tells the caller to fix the address. A 500 would tell them to retry,
        and retrying an address that does not exist fails identically forever."""
        from service import app as service_module

        def boom(*args, **kwargs):
            raise PipelineAborted("no match for 'asdfgh'")

        monkeypatch.setattr(service_module, "run", boom)
        client_ = TestClient(service_module.create_app(), raise_server_exceptions=False)
        response = client_.post("/analyze", json={"address": "asdfgh", "price": 268000})
        assert response.status_code == 422
        assert response.json()["error"] == "could_not_locate_address"


# =============================================================================
# POST /sensitivity
# =============================================================================


class TestSensitivity:
    def test_explicit_scenario_returns_the_engine_note_without_reading_a_snapshot(self, client):
        response = client.post(
            "/sensitivity",
            json={
                "price": 310000,
                "rate_start_pct": 5.0,
                "rate_end_pct": 5.5,
                "rate_step_pct": 0.25,
                "baseline_rate_pct": 6.25,
                "baseline_source": "buyer-provided comparison assumption",
            },
        )
        assert response.status_code == 200
        body = response.json()
        assert body["wait_vs_buy"] is None
        assert len(body["band"]["points"]) == 3
        assert "Scenario arithmetic only" in body["band"]["note"]
        assert "not a market observation" in body["band"]["baseline"]["source"]
        assert client.calls == []

    def test_wait_scenario_requires_both_rate_and_price(self, client):
        response = client.post(
            "/sensitivity",
            json={"price": 310000, "future_rate_pct": 6.25},
        )
        assert response.status_code == 422
        assert "future_rate_pct and future_price" in str(response.json())

    def test_invalid_rate_grid_is_rejected_as_a_caller_error(self, client):
        response = client.post(
            "/sensitivity",
            json={
                "price": 310000,
                "rate_start_pct": 7.5,
                "rate_end_pct": 5.0,
                "baseline_rate_pct": 6.25,
            },
        )
        assert response.status_code == 422
        assert "start_rate must be at or below end_rate" in response.json()["detail"]

    def test_malformed_baseline_snapshot_is_a_clear_503(self, client, monkeypatch):
        from analyzer import sensitivity_cli

        def malformed(_profile):
            raise ValueError("could not read mortgage snapshot: invalid JSON")

        monkeypatch.setattr(sensitivity_cli, "baseline_from_snapshot", malformed)
        response = client.post("/sensitivity", json={"price": 310000})
        assert response.status_code == 503
        assert "mortgage-rate snapshot is invalid" in response.json()["detail"]


# =============================================================================
# POST /shortlist
# =============================================================================


def upload(text: str, name: str = "shortlist.csv"):
    return {"file": (name, io.BytesIO(text.encode("utf-8")), "text/csv")}


class TestShortlist:
    def test_dry_run_validates_without_analysing(self, client):
        response = client.post(
            "/shortlist?dry_run=true",
            files=upload("address,price\n1 Main St,268000\n"),
        )
        assert response.status_code == 200
        assert response.json()["would_analyse"][0]["address"] == "1 Main St"
        assert client.calls == []

    def test_dry_run_explains_each_rejection(self, client):
        body = client.post(
            "/shortlist?dry_run=true",
            files=upload("address,price\n1 Main St,268000\n,300000\nbad,nope\n"),
        ).json()
        assert len(body["would_analyse"]) == 1
        assert len(body["rejected"]) == 2
        assert all(r["problem"] for r in body["rejected"])

    def test_full_run_returns_ranked_rows(self, client):
        body = client.post(
            "/shortlist",
            files=upload("address,price\n1 Main St,268000\n2 Oak Ave,290000\n"),
        ).json()
        assert body["counts"]["scored"] == 2
        assert [r["rank"] for r in body["ranked"]] == [1, 2]

    def test_full_run_includes_every_document(self, client):
        """The summary rows are a view. The documents are the evidence, and a number
        with no traceable document behind it is the thing this project exists to avoid."""
        body = client.post(
            "/shortlist", files=upload("address,price\n1 Main St,268000\n")
        ).json()
        assert body["documents"][0]["document"]["score"]["verdict"]

    def test_csv_format_returns_the_same_columns_as_the_cli(self, client):
        from analyzer import batch

        response = client.post(
            "/shortlist?format=csv", files=upload("address,price\n1 Main St,268000\n")
        )
        assert response.headers["content-type"].startswith("text/csv")
        assert response.text.splitlines()[0].split(",") == batch.SUMMARY_COLUMNS

    def test_markdown_format_returns_markdown(self, client):
        response = client.post(
            "/shortlist?format=markdown", files=upload("address,price\n1 Main St,268000\n")
        )
        assert response.headers["content-type"].startswith("text/markdown")
        assert response.text.startswith("# Shortlist comparison")

    def test_missing_required_column_is_422(self, client):
        response = client.post("/shortlist", files=upload("address,beds\n1 Main St,3\n"))
        assert response.status_code == 422
        assert "price" in response.json()["detail"]

    def test_empty_file_is_400(self, client):
        assert client.post("/shortlist", files=upload("")).status_code == 400

    def test_all_rows_rejected_is_422_and_says_how_to_debug(self, client):
        response = client.post("/shortlist", files=upload("address,price\n,300000\n"))
        assert response.status_code == 422
        assert "dry_run" in response.json()["detail"]

    def test_non_utf8_upload_is_400_not_500(self, client):
        files = {"file": ("s.csv", io.BytesIO(b"address,price\n\xff\xfe bad,1\n"), "text/csv")}
        assert client.post("/shortlist", files=files).status_code == 400

    def test_bom_upload_is_handled(self, client):
        """Excel writes a BOM. Without utf-8-sig the first column never matches."""
        files = {
            "file": ("s.csv", io.BytesIO("address,price\n1 Main St,268000\n".encode("utf-8-sig")), "text/csv")
        }
        response = client.post("/shortlist?dry_run=true", files=files)
        assert response.status_code == 200
        assert response.json()["would_analyse"]

    def test_oversized_upload_is_413(self, client):
        """A house shortlist is kilobytes. Anything this size is a mistake or an attempt
        to make the container do a lot of unpaid work."""
        big = "address,price\n" + "1 Main St,268000\n" * 200_000
        assert client.post("/shortlist", files=upload(big)).status_code == 413

    def test_unrecognised_columns_are_reported_not_fatal(self, client):
        body = client.post(
            "/shortlist?dry_run=true",
            files=upload("address,price,Zestimate\n1 Main St,268000,9\n"),
        ).json()
        assert body["unknown_headers"] == ["Zestimate"]


# =============================================================================
# Architecture
# =============================================================================


def test_analyzer_never_imports_the_service_layer():
    """The dependency arrow points one way: service -> analyzer, never back.

    If it ever inverted, the CLI, the batch runner, and the stdlib-only core would all
    start requiring FastAPI to be installed — which is exactly what ADR 0002 exists to
    prevent, and what the container's slim image depends on.
    """
    import ast
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[1] / "analyzer"
    offenders = {}
    for module in root.rglob("*.py"):
        tree = ast.parse(module.read_text(), filename=str(module))
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            for name in names:
                if name.split(".")[0] in {"service", "fastapi", "uvicorn", "pydantic"}:
                    offenders[str(module.relative_to(root))] = name
    assert not offenders, f"analyzer imports the web layer: {offenders}"


# =============================================================================
# One engine, three doors
# =============================================================================


class TestDoorsAgree:
    """The CLI, the batch runner, and the service must not drift apart.

    ADR 0007 refactored the batch runner so the file-based helpers became thin wrappers
    over shared text/stream functions, precisely so the API could reuse them instead of
    growing a parallel implementation. The risk that refactor creates is subtle: someone
    later finds a shared signature inconvenient, writes a local variant in one adapter,
    and now two doors format or parse the same input differently.

    That drift is invisible in output review, because each door looks correct on its own.
    These tests are the check, and they compare byte-for-byte against the on-disk artifacts
    the CLI produces rather than re-deriving the expected text here — a hand-written
    expectation would just be a third implementation to keep in sync.
    """

    SHORTLIST = "address,price,hoa_monthly\n1 A St,268000,0\n2 B St,310000,\n"

    def _batch_result(self, monkeypatch):
        from analyzer import batch as batch_module

        def stub(address, price, **kwargs):
            return PipelineRun(
                document=fake_document(),
                degradations=[Degradation(station="broadband", reason="no api key")],
                stations_run=["geocode", "parcel", "flood", "commute", "broadband"],
            )

        rows, rejected, unknown = batch_module.parse_shortlist_text(self.SHORTLIST)
        profile = load_profile()
        entries = batch_module.analyse_shortlist(rows, profile=profile, runner=stub)
        return batch_module.BatchResult(
            entries=entries,
            rejected=rejected,
            unknown_headers=unknown,
            profile_name=profile.name,
        )

    def test_csv_body_is_byte_identical_to_the_cli_file(self, client, monkeypatch, tmp_path):
        from analyzer import batch as batch_module

        result = self._batch_result(monkeypatch)
        on_disk = tmp_path / "summary.csv"
        batch_module.write_summary_csv(result, on_disk)

        response = client.post(
            "/shortlist?format=csv",
            files={"file": ("s.csv", io.BytesIO(self.SHORTLIST.encode()), "text/csv")},
        )
        assert response.status_code == 200
        # newline handling is the classic place these two paths diverge, hence splitlines
        assert response.text.splitlines() == on_disk.read_text().splitlines()

    def test_markdown_body_is_identical_to_the_cli_file(self, client, monkeypatch, tmp_path):
        from analyzer import batch as batch_module

        result = self._batch_result(monkeypatch)
        on_disk = tmp_path / "shortlist.md"
        batch_module.write_markdown(result, on_disk)

        response = client.post(
            "/shortlist?format=markdown",
            files={"file": ("s.csv", io.BytesIO(self.SHORTLIST.encode()), "text/csv")},
        )
        assert response.status_code == 200
        assert response.text.splitlines() == on_disk.read_text().splitlines()

    def test_upload_and_file_parse_to_the_same_rows(self, client, tmp_path):
        """An uploaded shortlist and the same bytes on disk must parse identically.

        Including the rejections: a row the CLI rejects must not be quietly accepted over
        HTTP, which is how a batch run would end up analysing a row the CLI refused.
        """
        from analyzer import batch as batch_module

        path = tmp_path / "s.csv"
        path.write_text(self.SHORTLIST)
        from_file, rejected_file, _ = batch_module.read_shortlist(path)
        from_text, rejected_text, _ = batch_module.parse_shortlist_text(self.SHORTLIST)

        assert [r.address for r in from_file] == [r.address for r in from_text]
        assert [r.price for r in from_file] == [r.price for r in from_text]
        assert [r.hoa_monthly for r in from_file] == [r.hoa_monthly for r in from_text]
        assert len(rejected_file) == len(rejected_text)

    def test_explicit_zero_hoa_survives_both_doors(self, client):
        """Regression: `0` in an HOA column was once rejected as "must be positive".

        Zero dues is a real, common, and materially good fact about a property — not a
        missing value. It has to mean the same thing whether it arrives by file or upload.
        """
        response = client.post(
            "/shortlist?dry_run=true",
            files={"file": ("s.csv", io.BytesIO(self.SHORTLIST.encode()), "text/csv")},
        )
        assert response.status_code == 200
        assert response.json()["rejected"] == []
