"""ADR 0004 as executable assertions.

The ADR says "the model reads, the code decides". This file is how that stops being a slogan.
Each test drives `parse_findings` with a response a real model plausibly returns and asserts
the code's answer -- accepted, or refused with the right reason.

The important cases are the ones where the model's output is *reasonable*. A response
containing `estimated_value: 312000` is not malformed, is not obviously wrong, and is exactly
what a helpful model volunteers. The boundary exists for that response, not for garbage.
"""

from __future__ import annotations

import pytest

from analyzer.core.extraction import (
    FIELDS,
    FORBIDDEN_FIELDS,
    Finding,
    parse_findings,
    schema_for_prompt,
)

DOC = """INSPECTION SUMMARY
Roof covering: architectural shingle, approximately 18 years old.
HVAC: heat pump, 16 years old per the data plate.
Water source: public water supply.
Sewer: septic tank in the rear yard.
Dues of $85.00 per month per the covenants.
Year built: 1998.
"""


def run(fields, *, text: str = DOC):
    return parse_findings(
        fields,
        sent_text=text,
        document="doc.txt",
        document_sha256="abc123",
        provider="test",
    )


def one(field, value, quote):
    return [{"field": field, "value": value, "quote": quote}]


# -- the accepted path -------------------------------------------------------------------


def test_a_well_formed_field_is_accepted_with_its_line_located():
    result = run(one("roof_age_years", 18, "approximately 18 years old"))
    assert result.refused == 0
    finding = result.findings[0]
    assert finding.value == 18
    assert finding.line == 2
    assert finding.confirmed is False


def test_the_line_number_is_recomputed_and_not_taken_from_the_model():
    """A model's line number is a guess; the quote is evidence.

    Trusting a claimed line number would put a wrong citation in front of a person doing the
    confirmation step, and a citation that points at the wrong line trains them to stop
    checking -- which defeats the only human control in the design.
    """
    result = run(
        [{"field": "roof_age_years", "value": 18, "quote": "approximately 18 years old", "line": 999}]
    )
    assert result.findings[0].line == 2


def test_a_quote_broken_across_a_line_wrap_still_grounds():
    """PDF text wraps mid-sentence, so exact string matching would refuse valid quotes.

    This is the single relaxation in the grounding check, and it is the difference between the
    check working on real inspection PDFs and rejecting nearly everything they contain.
    """
    wrapped = "The roof is approximately\n18 years old based on granule loss.\n"
    result = run(one("roof_age_years", 18, "approximately 18 years old"), text=wrapped)
    assert result.accepted == 1


# -- refusal 1: forbidden by the ADR -----------------------------------------------------


@pytest.mark.parametrize("field_name", sorted(FORBIDDEN_FIELDS))
def test_every_forbidden_field_is_refused_by_name(field_name):
    """Parametrised over the real list so adding a forbidden field cannot skip its test."""
    result = run(one(field_name, 1234, "Dues of $85.00 per month"))
    assert result.accepted == 0
    assert result.rejections[0].reason == "forbidden by ADR 0004"


def test_a_plausible_payment_estimate_is_refused_and_the_reason_cites_the_adr():
    """The case the boundary exists for: a well-formed, grounded, confidently wrong number.

    Nothing about this response is malformed. The quote is real. The value is the right order of
    magnitude. It is refused because a payment is arithmetic, and arithmetic that a probabilistic
    system produced is the exact failure ADR 0004 was written to prevent.
    """
    result = run(one("monthly_payment", 1477, "Dues of $85.00 per month"))
    rejection = result.rejections[0]
    assert "ADR 0004" in rejection.reason
    assert "dollar figure" in rejection.detail
    assert rejection.offered_value == 1477


def test_a_verdict_is_refused_even_when_it_agrees_with_the_engine():
    """Being right does not make it allowed.

    A verdict that happens to match is still unauditable and non-reproducible -- the ADR's
    stated reason -- and accepting it when it agrees means the only time it is refused is when
    nobody can tell.
    """
    result = run(one("recommendation", "watch", "Sewer: septic tank in the rear yard"))
    assert result.accepted == 0
    assert "verdict" in result.rejections[0].detail


def test_hoa_dues_is_currency_and_is_still_allowed():
    """The distinction that keeps the boundary coherent.

    ADR 0004 bans dollar figures the model *computed*, not dollar amounts printed on the page.
    HOA dues appear in the ADR's own permitted list. If this test ever starts failing because
    someone added a blanket currency ban, the fix is to read the ADR, not to delete the test.
    """
    result = run(one("hoa_dues_amount", 85.0, "Dues of $85.00 per month"))
    assert result.accepted == 1
    assert result.findings[0].value == 85.0


# -- refusal 2: undeclared ---------------------------------------------------------------


def test_an_invented_field_gets_no_column_in_the_output():
    result = run(one("zestimate", 300000, "Year built: 1998"))
    assert result.rejections[0].reason == "not in the declared schema"


def test_the_prompt_schema_and_the_parser_cannot_drift():
    """The prompt is generated from FIELDS, so every advertised field is an acceptable field.

    A hand-written prompt drifts the first time a field is added, and the symptom is invisible:
    the model returns what it was asked for and the parser refuses it forever.
    """
    advertised = {entry["field"] for entry in schema_for_prompt()}
    assert advertised == set(FIELDS)


# -- refusal 3: ungrounded citation ------------------------------------------------------


def test_a_fabricated_finding_with_a_fabricated_quote_is_caught():
    """The one deterministic defence against a hallucinated field.

    The value here (1998) is even correct. What gives it away is that the sentence the model
    claims to be quoting does not exist in the document, and a model that invents a finding
    almost never also invents a quote that happens to be present verbatim.
    """
    result = run(one("year_built", 1998, "Built in 1998 per county records"))
    assert result.accepted == 0
    assert result.rejections[0].reason == "citation not found in the document"


def test_a_missing_quote_is_refused_rather_than_accepted_uncited():
    result = run([{"field": "roof_age_years", "value": 18}])
    assert result.rejections[0].reason == "missing citation"


def test_grounding_is_not_fuzzy():
    """A quote that is 85% similar is a quote the model partly wrote.

    Loosening this to a similarity ratio would defeat the check for the exact case it exists to
    catch -- a mostly-copied sentence with the number changed.
    """
    result = run(one("roof_age_years", 25, "approximately 25 years old"))
    assert result.accepted == 0


def test_grounding_runs_against_the_redacted_text_not_the_original():
    """Grounding against the original would accept a quote the model could not have seen.

    That would be a silent failure of the check: it keeps returning "grounded" while no longer
    testing anything, because the haystack contains material that never left the machine.
    """
    from analyzer.core.redact import redact

    original = "Owner: Bao Nguyen. Roof is approximately 18 years old."
    redacted = redact(original, known_names=("Bao Nguyen",))
    result = parse_findings(
        one("roof_material", "Bao Nguyen", "Owner: Bao Nguyen"),
        sent_text=redacted.text,
        document="d.txt",
        document_sha256=redacted.sha256,
        provider="test",
    )
    assert result.accepted == 0
    assert result.rejections[0].reason == "citation not found in the document"


# -- refusal 4: implausible or unparseable ----------------------------------------------


@pytest.mark.parametrize(
    "field_name,value",
    [
        ("roof_age_years", 700),
        ("year_built", 1200),
        ("hvac_age_years", -3),
        ("square_feet", 40),
        ("hoa_dues_amount", 99999),
    ],
)
def test_implausible_numbers_are_refused(field_name, value):
    """Bounds catch unit confusion and parse errors, not judgement calls.

    A roof reported as 700 years old is a model that read a parcel number. Wide bounds are
    deliberate -- the check is for nonsense, and deciding whether an 18-year-old roof is
    acceptable is the scoring engine's job.
    """
    result = run(one(field_name, value, "Year built: 1998"))
    assert result.accepted == 0
    assert result.rejections[0].reason == "implausible or unparseable value"


def test_a_number_with_its_unit_attached_is_parsed_rather_than_refused():
    """Models return "18 years" for an int field constantly. That is a parse job, not a defect."""
    result = run(one("roof_age_years", "approximately 18 years", "approximately 18 years old"))
    assert result.findings[0].value == 18


def test_a_boolean_offered_for_a_number_is_refused():
    """bool subclasses int in Python, so True would silently become 1 without an explicit check.

    A roof "1 year old" because a model answered True is a wrong value that looks entirely
    reasonable in the output, which is the worst kind.
    """
    result = run(one("roof_age_years", True, "approximately 18 years old"))
    assert result.accepted == 0


def test_an_enum_is_normalised_but_an_unknown_choice_is_refused():
    assert run(one("sewer_type", "Septic", "Sewer: septic tank in the rear yard")).findings[0].value == "septic"
    assert run(one("sewer_type", "cesspit", "Sewer: septic tank in the rear yard")).accepted == 0


def test_a_duplicate_field_is_refused_rather_than_overwritten():
    """Two roof ages with two quotes means the document is ambiguous, and that is worth seeing.

    Keeping the last one silently would hide a genuine contradiction in the source -- and the
    person doing confirmation would never learn there was a second answer.
    """
    result = run(
        [
            {"field": "roof_age_years", "value": 18, "quote": "approximately 18 years old"},
            {"field": "roof_age_years", "value": 16, "quote": "HVAC: heat pump, 16 years old"},
        ]
    )
    assert result.accepted == 1
    assert result.rejections[0].reason == "duplicate field"


# -- malformed responses -----------------------------------------------------------------


def test_a_non_list_response_is_a_rejection_not_a_crash():
    result = run({"roof_age_years": 18})
    assert result.accepted == 0
    assert result.rejections[0].reason == "malformed response"


def test_rejections_are_output_and_never_dropped():
    """A run that refused six fields and accepted four is a successful run with a visible boundary.

    Discarding the refusals would throw away the most interesting thing about it, and would make
    the eval log unable to distinguish "the model behaved" from "the model misbehaved and we
    cleaned up".
    """
    result = run(
        [
            {"field": "roof_age_years", "value": 18, "quote": "approximately 18 years old"},
            {"field": "monthly_payment", "value": 1477, "quote": "Dues of $85.00 per month"},
            {"field": "nope", "value": 1, "quote": "Year built: 1998"},
        ]
    )
    assert result.accepted == 1 and result.refused == 2
    assert len(result.to_dict()["rejections"]) == 2


# -- the confirmation gate ---------------------------------------------------------------


def test_an_unconfirmed_finding_cannot_become_a_scoring_value():
    """ADR 0004's human-confirmation rule, enforced by raising rather than by convention.

    A `to_value()` that returned a usable Value with a warning would be ignored, and an
    extracted roof age would reach the score with nobody having read the quote.
    """
    finding = run(one("roof_age_years", 18, "approximately 18 years old")).findings[0]
    with pytest.raises(ValueError, match="ADR 0004"):
        finding.to_value()


def test_confirming_returns_a_new_finding_and_does_not_mutate():
    """Immutability matters because the unconfirmed original is the audit record."""
    finding = run(one("roof_age_years", 18, "approximately 18 years old")).findings[0]
    confirmed = finding.confirm()
    assert confirmed.confirmed is True
    assert finding.confirmed is False
    assert confirmed.value == finding.value


def test_a_confirmed_finding_stays_extracted_and_is_not_promoted_to_measured():
    """Confirmation removes one risk, not two.

    A person agreeing the document says 18 years does not make it a measurement -- the document
    can be wrong. Promoting to `measured` would claim a level of trust the confirmation step did
    not earn, and `measured` in this codebase means "read from a primary source".
    """
    value = run(one("roof_age_years", 18, "approximately 18 years old")).findings[0].confirm().to_value()
    assert value.confidence == "extracted"
    assert "line 2" in (value.note or "")


def test_the_payload_states_that_findings_are_not_scoring_inputs():
    """Spelled out in the output rather than left for a consumer to infer from a flag."""
    note = run(one("roof_age_years", 18, "approximately 18 years old")).to_dict()["note"]
    assert "confirmed=false" in note and "ADR 0004" in note


def test_finding_is_immutable():
    finding = run(one("roof_age_years", 18, "approximately 18 years old")).findings[0]
    with pytest.raises(Exception):
        finding.value = 99  # type: ignore[misc]


def test_every_declared_field_has_a_description_for_the_prompt():
    """An undescribed field in the prompt is a field the model will fill badly or ignore."""
    missing = [name for name, spec in FIELDS.items() if not spec.description.strip()]
    assert not missing, f"fields with no description: {missing}"


def test_no_field_is_both_declared_and_forbidden():
    """A contradiction here would make the schema unenforceable in a way no other test catches."""
    assert not (set(FIELDS) & set(FORBIDDEN_FIELDS))
