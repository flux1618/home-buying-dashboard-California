"""The two doors onto the extractor: the CLI and the HTTP endpoints.

Same principle as `test_service.py` -- these assert the *translation* layer only. Whether a
field is refused is settled in `test_extraction_boundary.py`, and re-asserting it here would
mean a change to a plausibility bound breaks the web tests, which teaches nothing.

What is worth asserting at this layer, and is asserted below:

  - The default provider is `offline` at both doors. Over HTTP this matters most: the endpoint
    accepts a file from anyone who can reach the port, and a default that forwarded it to a
    third party would turn one open port into an exfiltration path.
  - The uploaded document does not outlive the request on disk.
  - The response never hides the refusals, because they are the evidence the boundary works.
"""

from __future__ import annotations

import json
import pathlib

import pytest

FIXTURE = pathlib.Path(__file__).resolve().parents[1] / "analyzer" / "fixtures" / "sample_inspection.txt"


# =============================================================================
# CLI
# =============================================================================


def test_read_prints_the_quote_under_every_value(capsys):
    """The confirmation step ADR 0004 requires is only real if the evidence is on screen.

    Confirming a value without seeing the sentence it came from is rubber-stamping, and a
    rubber-stamped confirmation is worse than none -- it launders an extraction into something
    the score treats as reviewed.
    """
    from analyzer.extract.cli import main

    assert main(["read", str(FIXTURE)]) == 0
    out = capsys.readouterr().out
    assert "roof_age_years" in out
    assert "18 years old" in out  # the quote, not just the value
    assert "line" in out


def test_read_says_plainly_that_nothing_has_affected_a_score(capsys):
    """The output has to close the loop the ADR opens, or a user will assume it counted."""
    from analyzer.extract.cli import main

    main(["read", str(FIXTURE)])
    out = capsys.readouterr().out
    assert "Nothing here has affected a score" in out
    assert "ADR 0004" in out


def test_read_reports_what_redaction_removed(capsys):
    from analyzer.extract.cli import main

    main(["read", str(FIXTURE), "--name", "Bao Nguyen"])
    out = capsys.readouterr().out
    assert "redacted" in out
    assert "known_name" in out
    # And not the removed values themselves.
    assert "Nguyen" not in out


def test_read_says_so_when_redaction_found_nothing(capsys, tmp_path):
    """Silence would be ambiguous between "ran and found nothing" and "did not run".

    On a real inspection report, no redaction firing is mildly suspicious -- they carry an
    inspector's phone number -- so the absence is worth a line of its own.
    """
    from analyzer.extract.cli import main

    plain = tmp_path / "plain.txt"
    plain.write_text("Sewer: septic tank in the rear yard.\n")
    main(["read", str(plain)])
    assert "redacted nothing" in capsys.readouterr().out


def test_read_prints_refusals_with_their_reason(capsys, tmp_path, monkeypatch):
    from analyzer.extract import cli as cli_module
    from analyzer.extract.providers import OfflineProvider, ProviderResponse

    class Sloppy(OfflineProvider):
        name = "sloppy"

        def complete(self, text):
            return ProviderResponse(
                raw=json.dumps(
                    {"fields": [{"field": "monthly_payment", "value": 1477, "quote": "Dues of $85.00"}]}
                ),
                provider="sloppy",
                model="m",
                elapsed_ms=1,
            )

    monkeypatch.setattr(cli_module, "build_provider", lambda spec, model=None: Sloppy())
    cli_module.main(["read", str(FIXTURE)])
    out = capsys.readouterr().out
    assert "Refused" in out
    assert "monthly_payment" in out
    assert "forbidden by ADR 0004" in out


def test_read_defaults_to_the_offline_provider(capsys):
    from analyzer.extract.cli import main

    main(["read", str(FIXTURE)])
    assert "offline-regex" in capsys.readouterr().out


def test_a_missing_file_is_exit_1_with_a_message_not_a_traceback(capsys):
    """A stack trace when you mistype a filename describes the program, not your mistake."""
    from analyzer.extract.cli import main

    assert main(["read", "/nope/absent.pdf"]) == 1
    assert "no such file" in capsys.readouterr().err


def test_a_failed_provider_call_is_exit_1_even_though_it_was_logged(capsys, monkeypatch):
    """A script piping this needs to know the extraction did not happen.

    The log line is still written -- that is the audit requirement -- but exit 0 would tell a
    caller the fields are available when none are.
    """
    from analyzer.extract import cli as cli_module
    from analyzer.extract.providers import OfflineProvider, ProviderError

    class Broken(OfflineProvider):
        def complete(self, text):
            raise ProviderError("connection refused")

    monkeypatch.setattr(cli_module, "build_provider", lambda spec, model=None: Broken())
    assert cli_module.main(["read", str(FIXTURE)]) == 1
    assert "the call failed" in capsys.readouterr().out


def test_json_output_is_machine_readable(capsys):
    from analyzer.extract.cli import main

    main(["read", str(FIXTURE), "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["accepted"] >= 4
    assert payload["findings"][0]["confirmed"] is False
    assert payload["findings"][0]["confidence"] == "extracted"
    assert "redaction" in payload


def test_log_and_stats_survive_an_empty_log(capsys, tmp_path, monkeypatch):
    """A fresh clone runs `stats` before it has run anything, and must not traceback."""
    from analyzer.extract import cli as cli_module

    monkeypatch.setenv("HBA_DATA_DIR", str(tmp_path))
    assert cli_module.main(["log"]) == 0
    assert cli_module.main(["stats"]) == 0
    assert "0" in capsys.readouterr().out


def test_stats_reports_the_acceptance_rate(capsys, tmp_path, monkeypatch):
    from analyzer.extract import cli as cli_module

    monkeypatch.setenv("HBA_DATA_DIR", str(tmp_path))
    cli_module.main(["read", str(FIXTURE)])
    capsys.readouterr()
    cli_module.main(["stats", "--json"])
    stats = json.loads(capsys.readouterr().out)
    assert stats["calls"] == 1
    assert stats["acceptance_rate"] == 1.0


def test_all_pages_is_opt_in_because_it_widens_what_is_sent():
    """Redaction narrows and has no escape hatch; the page filter widens, so it needs a flag.

    The asymmetry is the point: a caller may need to send a page the keyword list dropped, and
    that is an explicit decision recorded in the command they typed.
    """
    import argparse
    import contextlib
    import io as _io

    from analyzer.extract.cli import main

    buffer = _io.StringIO()
    with contextlib.suppress(SystemExit), contextlib.redirect_stdout(buffer):
        main(["read", "--help"])
    help_text = buffer.getvalue()
    assert "--all-pages" in help_text
    assert "opt-in" in help_text or "Widens" in help_text
    assert isinstance(argparse.ArgumentParser(), argparse.ArgumentParser)


# =============================================================================
# HTTP
# =============================================================================

fastapi = pytest.importorskip(
    "fastapi",
    reason="FastAPI is an optional extra — install with `pip install '.[api]'`",
)
from fastapi.testclient import TestClient  # noqa: E402


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("HBA_DATA_DIR", str(tmp_path))
    from service.app import create_app

    return TestClient(create_app())


def upload(name: str = "inspection.txt", body: bytes | None = None):
    return {"file": (name, body if body is not None else FIXTURE.read_bytes())}


def test_the_schema_endpoint_publishes_what_is_refused_as_well_as_what_is_attempted(client):
    """A list of what the boundary rejects describes it better than prose about it does."""
    payload = client.get("/extract/schema").json()
    assert payload["count"] >= 10
    forbidden = {entry["field"] for entry in payload["forbidden"]}
    assert "monthly_payment" in forbidden and "verdict" in forbidden
    assert "the model reads, the code decides" in payload["rule"]


def test_extract_returns_findings_that_are_explicitly_unconfirmed(client):
    response = client.post("/extract", files=upload())
    assert response.status_code == 200
    payload = response.json()
    assert payload["accepted"] >= 4
    assert all(f["confirmed"] is False for f in payload["findings"])
    assert "ADR 0004" in payload["note"]


def test_extract_defaults_to_offline_so_an_open_port_is_not_an_exfiltration_path(client):
    """The most important default in the file.

    This endpoint takes a file from whoever can reach the port. If the default provider were a
    cloud model, exposing the container would mean every uploaded document leaving the network
    without anyone choosing that.
    """
    payload = client.post("/extract", files=upload()).json()
    assert payload["provider"]["name"] == "offline-regex"


def test_extract_redacts_before_sending_and_reports_it(client):
    payload = client.post("/extract?name=Bao+Nguyen", files=upload()).json()
    assert payload["redaction"]["fired"] is True
    assert payload["redaction"]["counts"]["known_name"] == 1
    body = json.dumps(payload)
    assert "james@ortegainspect.com" not in body
    assert "555-0134" not in body


def test_the_response_reports_the_uploaded_filename_not_the_temp_path(client):
    """The temp path leaks the container's filesystem layout and tells the caller nothing."""
    payload = client.post("/extract", files=upload("my-report.txt")).json()
    assert payload["document"]["path"] == "my-report.txt"
    assert "/tmp" not in json.dumps(payload["document"])


def test_the_uploaded_document_does_not_outlive_the_request_on_disk(client, tmp_path):
    """Deleted in a `finally`, so an exception mid-extraction does not leave a copy behind."""
    before = set(pathlib.Path("/tmp").glob("*.txt"))
    client.post("/extract", files=upload())
    after = set(pathlib.Path("/tmp").glob("*.txt"))
    assert not (after - before)


def test_an_empty_upload_is_400(client):
    assert client.post("/extract", files=upload(body=b"")).status_code == 400


def test_an_unsupported_type_is_415_and_names_what_works(client):
    response = client.post("/extract", files=upload("report.docx"))
    assert response.status_code == 415
    assert ".pdf" in response.json()["detail"]


def test_an_unknown_provider_is_422_rather_than_a_500(client):
    response = client.post("/extract?provider=gpt-9", files=upload())
    assert response.status_code == 422
    assert "offline" in response.json()["detail"]


def test_a_provider_failure_is_a_200_with_an_error_field(client, monkeypatch):
    """The degradation rule from the top of service/app.py, applied to extraction.

    The caller still gets the redaction report and the record of what was sent. Returning a 503
    would throw that away and say only "something went wrong".
    """
    import service.app as app_module
    from analyzer.extract.providers import OfflineProvider, ProviderError

    class Broken(OfflineProvider):
        def complete(self, text):
            raise ProviderError("ollama unreachable")

    monkeypatch.setattr(app_module, "build_provider", lambda spec, model=None: Broken())
    payload = client.post("/extract", files=upload()).json()
    assert "ollama unreachable" in payload["error"]
    assert payload["redaction"]["fired"] is True


def test_the_log_endpoint_returns_a_summary_and_no_document_text(client):
    client.post("/extract", files=upload())
    payload = client.get("/extract/log").json()
    assert payload["count"] == 1
    assert payload["summary"]["acceptance_rate"] is not None
    body = json.dumps(payload)
    assert "vapor barrier" not in body
    assert "architectural" not in body


def test_extraction_endpoints_appear_in_the_openapi_schema(client):
    """The container's own documentation is how someone else discovers this door."""
    paths = client.get("/openapi.json").json()["paths"]
    assert "/extract" in paths and "/extract/schema" in paths and "/extract/log" in paths
