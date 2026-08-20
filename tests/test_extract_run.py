"""The whole extraction path, with no API key and no network.

This file is the argument that the feature is testable. Every assertion below runs on a machine
that has never had a model provider configured, because the provider is an interface and the
default implementation is deterministic. CI has no credentials and should never have any.

Two things get special attention:

  - **Order of operations.** Redaction must sit between the file and the provider with no way
    around it. A spy provider records what it was handed, which is the only way to assert that
    from the outside.
  - **What the call log does not contain.** The log must never hold document text or extracted
    values, because it outlives the request.
"""

from __future__ import annotations

import json
import pathlib

import pytest

from analyzer.extract import extract_from_document, load_document
from analyzer.extract.calllog import read_records, summarize
from analyzer.extract.documents import DocumentError
from analyzer.extract.providers import (
    OfflineProvider,
    ProviderError,
    build_provider,
)
from analyzer.extract.run import ExtractionRun

FIXTURE = pathlib.Path(__file__).resolve().parents[1] / "analyzer" / "fixtures" / "sample_inspection.txt"


@pytest.fixture
def log(tmp_path) -> pathlib.Path:
    return tmp_path / "llm_calls.jsonl"


def go(tmp_log, **kw) -> ExtractionRun:
    return extract_from_document(FIXTURE, log_path=tmp_log, **kw)


# -- the fixture is a real document ------------------------------------------------------


def test_the_sample_report_is_shaped_like_a_real_one():
    """If the fixture is sanitised, the tests prove nothing about real input.

    It deliberately contains an email, a phone number, an account number, two personal names,
    and a parcel-style ID -- the exact mix the redactor has to sort correctly.
    """
    text = FIXTURE.read_text()
    assert "@" in text and "555-0134" in text
    assert "Inspector:" in text
    assert "public sewer" in text.lower()


# -- order of operations -----------------------------------------------------------------


def test_the_provider_is_handed_redacted_text_and_never_the_original():
    """The security property, asserted from outside rather than inferred from the code.

    A spy is the only way to prove what actually crossed the boundary. Reading `run.py` and
    concluding it looks right is what code review does; this survives a refactor.
    """

    class Spy(OfflineProvider):
        name = "spy"
        seen = ""

        def complete(self, text):
            Spy.seen = text
            return super().complete(text)

    go(None, provider=Spy())
    assert Spy.seen, "the provider was never called"
    assert "james@ortegainspect.com" not in Spy.seen
    assert "555-0134" not in Spy.seen
    assert "Ortega" not in Spy.seen
    # And the substance survived, which is the other half of a working redactor.
    assert "public sewer" in Spy.seen.lower()
    assert "18 years old" in Spy.seen


def test_the_buyers_name_is_gone_when_the_profile_supplies_it():
    class Spy(OfflineProvider):
        seen = ""

        def complete(self, text):
            Spy.seen = text
            return super().complete(text)

    go(None, provider=Spy(), known_names=("Bao Nguyen",))
    assert "Nguyen" not in Spy.seen


def test_grounding_and_the_model_read_identical_bytes():
    """Findings are grounded against what was sent, so a finding cannot cite unsent text.

    Verified by the fact that every accepted quote appears in the redacted text -- if grounding
    used a different haystack, a quote could pass here and be absent from what the model saw.
    """
    run = go(None)
    for finding in run.result.findings:
        collapsed = " ".join(finding.quote.split()).lower()
        assert collapsed in " ".join(run.redacted.text.split()).lower()


# -- the offline provider ----------------------------------------------------------------


def test_the_offline_provider_extracts_the_fields_that_matter_with_no_key():
    """The portable utility facts are extracted without inventing an unsupported fail.

    In the LA profile the Assessor feed does not expose utility service, so the score does not
    pretend this inspection result is a parcel-level hard-fail fact. The extraction still has
    to retain the real public-water/public-sewer evidence for the buyer to review.
    """
    run = go(None)
    found = {f.field_name: f.value for f in run.result.findings}
    assert found["sewer_type"] == "public_sewer"
    assert found["water_source"] == "public"
    assert found["roof_age_years"] == 18
    assert found["hoa_dues_amount"] == 85.0
    assert run.ok


def test_the_offline_provider_is_deterministic():
    """Two runs, identical findings. Required for the eval log to mean anything."""
    first = [f.to_dict() for f in go(None).result.findings]
    second = [f.to_dict() for f in go(None).result.findings]
    for a, b in zip(first, second):
        a.pop("extracted_at"), b.pop("extracted_at")
    assert first == second


def test_the_offline_provider_prefers_a_bulleted_deficiency_over_a_wrapped_sentence():
    """A bug the first version shipped: it returned half a sentence from the roofing narrative.

    Real reports wrap at 80 columns, so the first line containing "recommend" is usually a
    fragment. The deficiency summary lists whole findings as bullets, so bullets win.
    """
    run = go(None)
    defects = [f.value for f in run.result.findings if f.field_name == "defects"]
    assert defects, "no defect extracted"
    assert defects[0].startswith("The crawl space vapor barrier")


def test_the_offline_provider_is_subject_to_the_same_grounding_check():
    """Nothing gets a pass for being local.

    Its quotes are real spans from the document, so if it ever starts synthesising a quote the
    grounding check refuses it exactly as it would refuse a model.
    """
    response = OfflineProvider().complete("Roof is approximately 18 years old.")
    payload = json.loads(response.raw)
    for entry in payload["fields"]:
        assert entry["quote"] in "Roof is approximately 18 years old."


def test_the_offline_provider_finds_nothing_in_an_unrelated_document():
    """It must not invent fields when the document does not contain them.

    ADR 0004's provenance rule -- an absent value stays absent -- applies to the offline path
    too. A regex provider that pattern-matched loosely enough to always return something would
    be manufacturing data.
    """
    response = OfflineProvider().complete("The quarterly newsletter deadline is Friday.")
    assert json.loads(response.raw)["fields"] == []


def test_the_offline_provider_is_honest_about_not_being_a_model():
    """Its docstring is load-bearing documentation, not decoration.

    A reader who mistakes twenty regexes for a language model draws the wrong conclusion about
    the whole feature, so the class says what it is in its first line.
    """
    doc = OfflineProvider.__doc__ or ""
    assert "not a language model" in doc.lower()


# -- provider resolution -----------------------------------------------------------------


def test_the_default_provider_is_offline_so_a_fresh_clone_sends_nothing_anywhere():
    """A default that reaches a third party is a decision a default should not make."""
    assert build_provider().name == "offline-regex"
    assert build_provider("").name == "offline-regex"


def test_an_unknown_provider_fails_with_the_list_of_known_ones():
    with pytest.raises(ProviderError, match="offline"):
        build_provider("gpt-9")


def test_a_cloud_provider_with_no_key_says_which_variable_and_offers_the_offline_path():
    """An error message that names the fix is the difference between a stop and a detour."""
    provider = build_provider("openai")
    with pytest.raises(ProviderError) as exc:
        provider.complete("anything")
    assert "HBA_LLM_API_KEY" in str(exc.value)
    assert "offline" in str(exc.value)


def test_a_provider_failure_degrades_the_run_instead_of_raising():
    """The station contract from sources/base.py, applied here.

    A source going dark is a value to report, not a traceback in front of a user. The run
    completes, `ok` is False, and the error text survives to the log.
    """

    class Broken(OfflineProvider):
        name = "broken"

        def complete(self, text):
            raise ProviderError("connection refused")

    run = go(None, provider=Broken())
    assert run.ok is False
    assert "connection refused" in (run.error or "")
    assert run.result.accepted == 0


def test_an_unparseable_response_is_reported_separately_from_a_refusal():
    """Conflating them would make a broken provider look like a well-enforced boundary.

    "The model answered and the code refused six fields" and "there was no answer at all" are
    different operational problems with different fixes.
    """

    class Chatty(OfflineProvider):
        name = "chatty"

        def complete(self, text):
            response = super().complete(text)
            return type(response)(raw="I'm sorry, I can't help with that.", provider="chatty", model="x", elapsed_ms=1)

    run = go(None, provider=Chatty())
    assert run.ok is False
    assert "no JSON" in (run.error or "")
    assert run.result.refused == 0


# -- the call log ------------------------------------------------------------------------


def test_the_log_records_what_the_threat_model_promised(log):
    """Timestamp, document hash, fields requested, provider, and whether redaction fired."""
    go(log)
    record = read_records(path=log)[-1]
    assert record["at"] and record["document_sha256"]
    assert record["provider"] == "offline-regex"
    assert record["redaction_fired"] is True
    assert "roof_age_years" in record["fields_requested"]
    assert record["accepted"] >= 4


def test_the_log_never_contains_document_text_or_extracted_values(log):
    """The log outlives the request, so anything in it is data at rest.

    A log holding the redacted text is a second copy of the sensitive material with a longer
    retention. A log holding extracted defect descriptions has leaked the interesting part of
    the document. Field names and reason counts are enough for both jobs the log has.
    """
    go(log, known_names=("Bao Nguyen",))
    blob = log.read_text()
    assert "vapor barrier" not in blob
    assert "architectural" not in blob
    assert "Ortega" not in blob and "Nguyen" not in blob
    assert "606 Andre" not in blob
    assert "roof_age_years" in blob  # the field name is fine; the value is not


def test_the_redaction_counts_are_logged_without_the_redacted_values(log):
    go(log)
    record = read_records(path=log)[-1]
    assert record["redaction_counts"]["email"] == 1
    assert "555-0134" not in json.dumps(record)


def test_a_failed_call_still_writes_a_line(log):
    """A log of successes only cannot answer "was this document ever sent"."""

    class Broken(OfflineProvider):
        def complete(self, text):
            raise ProviderError("boom")

    go(log, provider=Broken())
    record = read_records(path=log)[-1]
    assert record["error"] == "boom"
    assert record["document_sha256"]


def test_the_log_is_append_only_across_runs(log):
    go(log)
    go(log)
    assert len(read_records(path=log)) == 2


def test_a_half_written_final_line_does_not_make_the_log_unreadable(log):
    """A kill -9 mid-append is normal, and it must not destroy the history above it."""
    go(log)
    with log.open("a") as handle:
        handle.write('{"at": "truncated"')
    assert len(read_records(path=log)) == 1


def test_a_log_write_failure_does_not_fail_the_extraction(tmp_path):
    """Observability must not break the thing it observes.

    A read-only mount should not lose an extraction the user asked for. The trade is that a
    silently-missing line is possible, which is why the CLI prints the log path every run.
    """
    unwritable = tmp_path / "nope"
    unwritable.touch(mode=0o444)
    run = extract_from_document(FIXTURE, log_path=unwritable / "sub" / "log.jsonl")
    assert run.result.accepted > 0


def test_the_summary_reports_an_acceptance_rate_and_why_fields_were_refused(log):
    """The eval half of the log: the number to watch when swapping providers."""

    class Sloppy(OfflineProvider):
        def complete(self, text):
            return type(super().complete(text))(
                raw=json.dumps(
                    {
                        "fields": [
                            {"field": "roof_age_years", "value": 18, "quote": "approximately 18 years old"},
                            {"field": "monthly_payment", "value": 1477, "quote": "Dues of $85.00 per month"},
                            {"field": "year_built", "value": 1998, "quote": "a sentence that is not there"},
                        ]
                    }
                ),
                provider="sloppy",
                model="m",
                elapsed_ms=1,
            )

    go(log, provider=Sloppy())
    stats = summarize(read_records(path=log))
    assert stats["accepted"] == 1 and stats["refused"] == 2
    assert stats["acceptance_rate"] == pytest.approx(1 / 3, abs=0.01)
    assert "forbidden by ADR 0004" in stats["rejection_reasons"]
    assert "citation not found in the document" in stats["rejection_reasons"]


def test_reading_a_log_that_does_not_exist_returns_nothing_rather_than_raising(tmp_path):
    assert read_records(path=tmp_path / "absent.jsonl") == []
    assert summarize([])["calls"] == 0


# -- document loading --------------------------------------------------------------------


def test_page_filtering_reports_how_much_was_dropped():
    """A run that sent 6 of 41 pages and found nothing is a different problem from one that sent all 41."""
    doc = load_document(FIXTURE)
    assert doc.pages_total == 1 and doc.pages_sent == 1


def test_a_filter_that_would_drop_everything_sends_everything_instead():
    """A keyword list matching nothing means the list is wrong, not that the document is empty.

    Sending an empty string to a model would report "no findings" for what is really a filter
    bug -- a silent wrong answer instead of a loud one.
    """
    import tempfile

    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as handle:
        handle.write("Page one about nothing.\fPage two about nothing either.")
        name = handle.name
    doc = load_document(name)
    assert "Page one" in doc.text and "Page two" in doc.text
    assert doc.filtered is False


def test_only_relevant_pages_survive_when_some_are_relevant():
    import tempfile

    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as handle:
        handle.write("Cover page, photographs, and contact details.\fSewer: septic tank in the rear.")
        name = handle.name
    doc = load_document(name)
    assert "septic" in doc.text
    assert "photographs" not in doc.text
    assert doc.pages_sent == 1 and doc.pages_total == 2


def test_a_missing_file_says_so_plainly():
    with pytest.raises(DocumentError, match="no such file"):
        load_document("/nope/absent.txt")


def test_an_unsupported_type_names_what_is_supported():
    import tempfile

    with tempfile.NamedTemporaryFile("w", suffix=".docx", delete=False) as handle:
        handle.write("x")
        name = handle.name
    with pytest.raises(DocumentError, match="pdftotext"):
        load_document(name)


def test_every_schema_field_could_plausibly_be_located_by_a_page_keyword():
    """A field with no keyword is a field whose page gets filtered out before the model sees it.

    That failure is invisible: extraction "works", the field is simply never found, and the
    reason is in a page filter three modules away.
    """
    from analyzer.core.extraction import FIELDS
    from analyzer.extract.documents import RELEVANT_KEYWORDS

    # Matching on word stems in both directions: a field word may be longer than the keyword
    # ("flooding" vs "flood") or shorter ("water" vs "water heater"). A first-word-only check
    # was tried and wrongly flagged known_flooding_mentioned, which "flood" covers fine.
    noise = {"mentioned", "years", "type", "age", "amount", "period", "known", "source", "issue"}
    unreachable = []
    for name in FIELDS:
        words = [w for w in name.split("_") if len(w) >= 4 and w not in noise]
        reachable = any(
            keyword in word or word.startswith(keyword) or keyword.startswith(word)
            for word in words
            for keyword in RELEVANT_KEYWORDS
        )
        if not reachable:
            unreachable.append(name)
    assert not unreachable, f"no page keyword can surface: {unreachable}"
