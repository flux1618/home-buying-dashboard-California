"""Deterministic redaction of document text before a model ever sees it.

`docs/THREAT_MODEL.md` commits to three properties, and each one is a design constraint
rather than a nice-to-have:

  1. Redaction happens in `core/`. So this module is stdlib-only and imports nothing that
     can reach a network, which `tests/test_core_purity.py` enforces.
  2. Redaction is deterministic. Regex, not a model. A probabilistic redactor that misses a
     social security number 1% of the time is not a control, and you cannot unit-test it.
  3. Redaction cannot be skipped by a caller. There is no `redact=False`. The extractor
     takes a `RedactedText`, and the only way to construct one is through `redact()`.

That third property is the whole point of this file existing separately. If redaction were a
keyword argument, some future call site under deadline pressure would set it to False, and
nothing would fail.

## What this does not solve

Personal names. The threat model asks for name redaction "via deterministic pattern
matching", and the honest position is that free-standing name detection is not a regex
problem -- "Mr. Brown" is a name and "brown water staining" is a finding, and an inspection
report contains both. Three narrower things are done instead:

  - Names the caller already knows (the buyer, the co-buyer) are redacted by literal match.
  - Honorific-prefixed names are redacted: `Mr. Nguyen`, `Ms. Chen`.
  - Labelled names are redacted: `Prepared for: Bao Nguyen`, `Owner: ...`, `Seller: ...`,
    `Inspector: ...`, `Agent: ...`.

Anything else -- a name appearing bare in a sentence -- survives. That gap is recorded in
`docs/KNOWN_LIMITATIONS.md` rather than papered over, because a redactor that claims to
remove all names and does not is worse than one that states its scope.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field

# Each rule is (name, compiled pattern, replacement). Order matters: the more specific
# patterns run first, so an SSN is not partially eaten by the generic long-digit-run rule.
#
# Every pattern is anchored on structure rather than on a keyword, because a document that
# writes "Acct 4111111111111111" without the word "account" still needs the number gone.
_RULES: list[tuple[str, re.Pattern[str], str]] = [
    (
        "email",
        re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"),
        "[EMAIL]",
    ),
    (
        # Before the phone rule: 123-45-6789 would otherwise be left alone by the phone
        # pattern and then caught by nothing.
        "ssn",
        re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),
        "[SSN]",
    ),
    (
        # 16 digits in groups, which is a card. Kept ahead of the generic digit-run rule so
        # the label is accurate in the redaction report.
        "card",
        re.compile(r"\b(?:\d{4}[ -]?){3}\d{4}\b"),
        "[CARD]",
    ),
    (
        "phone",
        re.compile(
            r"(?<![\d-])(?:\+1[ .-]?)?(?:\(\d{3}\)|\d{3})[ .-]?\d{3}[ .-]?\d{4}(?![\d-])"
        ),
        "[PHONE]",
    ),
    (
        # A bare run of 8 or more digits. Account and routing numbers look like this, and so
        # does very little else in an inspection report. Parcel/tax IDs are the false
        # positive worth knowing about, which is why the report names the rule that fired --
        # see the note in `RedactionReport`.
        "long_digit_run",
        re.compile(r"(?<![\d.-])\d{8,}(?![\d.-])"),
        "[NUMBER]",
    ),
    (
        "honorific_name",
        re.compile(r"\b(?:Mr|Mrs|Ms|Dr|Miss)\.?\s+[A-Z][a-z]+(?:\s+[A-Z][a-z]+)?"),
        "[NAME]",
    ),
    (
        # A labelled name, up to three capitalised words after the colon. Bounded on purpose:
        # an unbounded match would swallow the rest of the line, including findings.
        "labelled_name",
        re.compile(
            r"\b(?:Prepared\s+for|Prepared\s+by|Owner|Seller|Buyer|Inspector|Agent|Realtor|"
            r"Client|Contact)\s*:\s*[A-Z][A-Za-z'-]+(?:\s+[A-Z][A-Za-z'-]+){0,2}"
        ),
        None,  # replacement built at runtime -- keeps the label, drops the name
    ),
]

_LABEL_SPLIT = re.compile(r"^([^:]+:)\s*")


def _labelled_replacement(match: re.Match[str]) -> str:
    """Keep `Inspector:` and drop the name after it.

    Dropping the label too would destroy structure the extractor may want -- knowing a
    document has an `Inspector:` line is not sensitive, the name after it is.
    """
    text = match.group(0)
    label = _LABEL_SPLIT.match(text)
    return f"{label.group(1)} [NAME]" if label else "[NAME]"


@dataclass(frozen=True)
class RedactionReport:
    """What fired, and how often.

    Counts rather than the removed values, for an obvious reason: this report goes into the
    call log, and a log containing the social security numbers it redacted is not a control,
    it is a second copy of the problem.

    `counts` keys are rule names. A `long_digit_run` count is the one to look at sceptically
    -- a tax parcel ID is 8+ digits and not sensitive, so a document with many of those will
    show a high count. That is over-redaction, which is the safe direction, but it can cost
    the extractor a field it needed.
    """

    counts: dict[str, int] = field(default_factory=dict)
    names_supplied: int = 0

    @property
    def fired(self) -> bool:
        return bool(self.counts)

    @property
    def total(self) -> int:
        return sum(self.counts.values())

    def to_dict(self) -> dict[str, object]:
        return {
            "fired": self.fired,
            "total": self.total,
            "counts": dict(sorted(self.counts.items())),
            "names_supplied": self.names_supplied,
        }


@dataclass(frozen=True)
class RedactedText:
    """Document text that has been through `redact()`.

    The extractor accepts this type and nothing else. That is the enforcement mechanism for
    "redaction cannot be skipped by a caller" -- skipping it means constructing this class
    directly, which is a thing a reviewer can see in a diff, unlike passing `redact=False`.

    `sha256` is of the text *as sent*, not of the original file. It is the identifier in the
    call log, and it has to describe the bytes that actually left the machine, otherwise the
    log is answering a different question than the one an audit asks.
    """

    text: str
    report: RedactionReport
    sha256: str
    original_chars: int

    @property
    def chars(self) -> int:
        return len(self.text)


def redact(text: str, *, known_names: tuple[str, ...] = ()) -> RedactedText:
    """Remove mechanically-detectable personal data. The only constructor of RedactedText.

    `known_names` are literal strings the caller already knows are names -- typically from
    the buyer profile. Matched case-insensitively and word-bounded, longest first, so
    redacting "Bao Nguyen" does not leave a stray "Nguyen" behind from a shorter entry.
    """
    original_chars = len(text)
    counts: dict[str, int] = {}
    out = text

    # Supplied names first. They are literal and unambiguous, and doing them before the
    # honorific rule means "Mr. Bao Nguyen" is handled by the name list rather than being
    # partially matched by the honorific pattern.
    supplied = 0
    for name in sorted({n.strip() for n in known_names if n and n.strip()}, key=len, reverse=True):
        pattern = re.compile(rf"\b{re.escape(name)}\b", re.IGNORECASE)
        out, n = pattern.subn("[NAME]", out)
        if n:
            counts["known_name"] = counts.get("known_name", 0) + n
        supplied += 1

    for rule_name, pattern, replacement in _RULES:
        if replacement is None:
            out, n = pattern.subn(_labelled_replacement, out)
        else:
            out, n = pattern.subn(replacement, out)
        if n:
            counts[rule_name] = counts.get(rule_name, 0) + n

    return RedactedText(
        text=out,
        report=RedactionReport(counts=counts, names_supplied=supplied),
        sha256=hashlib.sha256(out.encode("utf-8")).hexdigest(),
        original_chars=original_chars,
    )
