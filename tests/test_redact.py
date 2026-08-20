"""Redaction is a security control, so it is tested like one: adversarially.

The threat model promises three things about `core/redact.py` -- deterministic, in core, and
unskippable by a caller. The third is the one a test can most easily let slip, so it is
asserted directly rather than assumed from the type signature.

Every test here names the leak it prevents. A redaction test that says "test_email" tells a
future reader nothing about why the pattern is shaped the way it is, and the shape is the
whole content.
"""

from __future__ import annotations

import inspect

import pytest

from analyzer.core import redact as redact_module
from analyzer.core.redact import RedactedText, redact


def test_a_social_security_number_never_survives():
    """The highest-consequence pattern, tested on its own so its failure is unmissable."""
    out = redact("SSN on file 123-45-6789 for the applicant.")
    assert "123-45-6789" not in out.text
    assert "[SSN]" in out.text
    assert out.report.counts["ssn"] == 1


@pytest.mark.parametrize(
    "raw",
    [
        "(864) 555-0134",
        "864-555-0134",
        "864.555.0134",
        "8645550134",
        "+1 864 555 0134",
    ],
)
def test_phone_numbers_survive_no_formatting_variant(raw):
    """A pattern that only catches the pretty form is a pattern that leaks the ugly form.

    Inspection reports, HOA letters, and seller disclosures are written by five different
    offices with five different house styles, so all five reach the same document set.
    """
    out = redact(f"Call {raw} to schedule.")
    assert raw not in out.text
    assert "[PHONE]" in out.text


def test_an_account_number_goes_but_a_parcel_id_stays():
    """The digit-run rule has to be blunt without being useless.

    A bare 8+ digit run is an account number often enough to redact on sight. A parcel ID --
    `7-16-04-091.00` -- is not sensitive and is load-bearing for the analysis, so the negative
    lookarounds on `.` and `-` exist specifically to leave it alone. Deleting the parcel ID
    would break the one identifier that links a document to a property.
    """
    out = redact("Account 000123456789 for parcel 7-16-04-091.00")
    assert "000123456789" not in out.text
    assert "7-16-04-091.00" in out.text


def test_a_finding_that_looks_like_a_name_is_not_destroyed():
    """The reason names are not redacted by a generic capitalised-word rule.

    "brown water staining" is a defect and "Mr. Brown" is a person. A redactor aggressive
    enough to catch the second by pattern alone eats the first, and an extractor that never
    sees the defects is worse than useless -- it reports a clean house.
    """
    out = redact("Mr. Brown reported brown water staining in the attic.")
    assert "[NAME]" in out.text
    assert "brown water staining" in out.text


def test_a_supplied_name_is_removed_even_mid_sentence():
    """Names the caller already knows are the one class that can be handled exactly.

    The buyer's name comes from the profile, so there is no detection problem -- only a
    matching problem. Case-insensitive and word-bounded.
    """
    out = redact("The report was ordered by bao nguyen on Tuesday.", known_names=("Bao Nguyen",))
    assert "nguyen" not in out.text.lower()
    assert out.report.counts["known_name"] == 1


def test_a_longer_supplied_name_wins_over_a_shorter_one():
    """Sorting by length prevents a half-redacted name.

    With names ("Nguyen", "Bao Nguyen") processed shortest-first, "Bao Nguyen" becomes
    "Bao [NAME]" -- the given name survives, which is exactly the leak the list was meant to
    close.
    """
    out = redact("Prepared for Bao Nguyen.", known_names=("Nguyen", "Bao Nguyen"))
    assert "Bao" not in out.text


def test_the_label_survives_but_the_name_after_it_does_not():
    """Structure is not sensitive; the name is.

    Knowing a document has an `Inspector:` line helps the extractor and tells an attacker
    nothing. Dropping the label with the name would throw away context for no privacy gain.
    """
    out = redact("Inspector: James Ortega, SC License #4471")
    assert "Inspector:" in out.text
    assert "James" not in out.text and "Ortega" not in out.text


def test_the_labelled_name_rule_does_not_swallow_the_rest_of_the_line():
    """Why the labelled-name pattern is bounded to three words.

    An unbounded match after the colon would eat the license number, the date, and any finding
    on the same line. Over-redaction is the safe direction for privacy and the unsafe
    direction for the feature actually working.
    """
    out = redact("Inspector: James Ortega, SC License #4471 noted a displaced vapor barrier")
    assert "displaced vapor barrier" in out.text


def test_the_report_counts_but_never_stores_what_it_removed():
    """The report goes into the call log, and a log of redacted SSNs is a second breach.

    This is asserted rather than trusted because the natural, helpful-feeling implementation --
    keeping the removed values so a user can review them -- is precisely the one that turns a
    privacy control into a privacy hole with a longer retention than the request.
    """
    out = redact("SSN 123-45-6789 and email a@b.com")
    serialized = repr(out.report.to_dict())
    assert "123-45-6789" not in serialized
    assert "a@b.com" not in serialized
    assert out.report.total == 2


def test_the_hash_is_of_the_text_that_was_sent_not_the_original():
    """The call log's document identifier has to describe the bytes that left the machine.

    Hashing the original file would produce a log that answers "which file did I have" when
    the question an audit asks is "what did the provider receive". Those differ by exactly the
    redaction, which is the interesting part.
    """
    original = "SSN 123-45-6789"
    out = redact(original)
    import hashlib

    assert out.sha256 == hashlib.sha256(out.text.encode()).hexdigest()
    assert out.sha256 != hashlib.sha256(original.encode()).hexdigest()


def test_redaction_is_deterministic():
    """Same input, same output, same hash. The log is worthless otherwise."""
    text = "Call (864) 555-0134 or email a@b.com. SSN 123-45-6789."
    first, second = redact(text), redact(text)
    assert first.text == second.text
    assert first.sha256 == second.sha256


def test_redaction_is_idempotent():
    """Redacting twice changes nothing, so a double call cannot corrupt a document.

    Matters because the placeholders themselves must not look like the patterns -- if
    `[PHONE]` contained digits, a second pass would redact its own output and the text would
    degrade on every call.
    """
    once = redact("Call (864) 555-0134 about SSN 123-45-6789")
    twice = redact(once.text)
    assert twice.text == once.text
    assert not twice.report.fired


def test_there_is_no_way_to_ask_redact_to_skip():
    """The unskippable promise, asserted against the signature.

    The threat model says redaction "cannot be skipped by a caller". This test fails the moment
    someone adds a `skip=` or `enabled=` parameter under deadline pressure -- which is the only
    circumstance in which anyone would.
    """
    params = set(inspect.signature(redact).parameters)
    forbidden = {"skip", "skip_redaction", "enabled", "redact", "disable", "raw", "unsafe"}
    assert not (params & forbidden), f"redact() grew an escape hatch: {params & forbidden}"


def test_redact_is_the_only_constructor_used_in_the_send_path():
    """RedactedText can be built directly in Python; that is fine, and it is the point.

    Python has no private constructors, so this cannot be prevented -- only made visible. The
    guarantee is not "impossible", it is "impossible by accident and obvious in a diff". This
    test asserts the send path in `extract/run.py` reaches the provider only via `redact()`,
    which is the property that actually holds.
    """
    from analyzer.extract import run as run_module

    source = inspect.getsource(run_module)
    assert "redact(document.text" in source
    assert "engine.complete(redacted.text)" in source
    # The raw document text must not be handed to the provider anywhere.
    assert "complete(document.text" not in source


def test_core_redact_imports_nothing_that_can_reach_a_network():
    """Belt and braces with test_core_purity, stated locally so the reason lives here.

    Redaction in core is what makes it unskippable: core cannot import a provider, so there is
    no arrangement of core code in which text reaches a model without passing through here.
    """
    source = inspect.getsource(redact_module)
    for banned in ("import requests", "import httpx", "import urllib", "import openai"):
        assert banned not in source


def test_an_empty_document_does_not_explode():
    out = redact("")
    assert out.text == ""
    assert not out.report.fired
    assert isinstance(out, RedactedText)
