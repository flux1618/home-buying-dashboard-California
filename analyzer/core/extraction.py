"""The declared schema for document extraction, and the code that judges a model's answer.

[ADR 0004](../../docs/adr/0004-llm-scope-boundary.md) draws the line: **the model reads, the
code decides.** This module is the deciding half, and it is in `core/` so it is stdlib-only
and cannot reach a provider. Nothing here talks to a model. It takes a parsed response and a
copy of the exact text that was sent, and it either accepts a field or refuses it with a
reason.

Four refusals, and each one corresponds to a way the boundary gets crossed in practice:

1. **Undeclared field.** Only fields in `FIELDS` exist. A model returning `estimated_value`
   does not get a new column in the output; it gets a rejection.

2. **Forbidden field.** A subset of undeclared fields are worth naming explicitly rather than
   lumping in with typos, because they are the ones a capable model will volunteer
   unprompted: a monthly payment, a tax figure, a score, a verdict. ADR 0004 forbids all of
   them. The rejection reason says so by name, so the log shows the boundary being enforced
   rather than a vague schema miss.

3. **Ungrounded citation.** Every field must come with a verbatim quote from the document.
   The quote is checked against the text that was actually sent -- if it is not in there, the
   field is refused. This is the one deterministic defence against a fabricated finding, and
   it is cheap: a model that invents "roof replaced 2019" almost never also invents a quote
   that happens to appear in the file.

4. **Implausible value.** A roof is not 700 years old. Ranges are deliberately wide -- they
   catch unit confusion and parse errors, not judgement calls.

Everything that survives becomes a `Finding` with `confidence="extracted"` and
`confirmed=False`. ADR 0004 requires human confirmation before an extracted field affects a
score, and `Finding.to_value()` raises if called on an unconfirmed finding. That is the
enforcement: scoring cannot read a finding it has not been told a person looked at.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from typing import Any, Literal

from .provenance import Value, now_iso

FieldKind = Literal["int", "float", "bool", "str", "enum"]


@dataclass(frozen=True)
class FieldSpec:
    """One extractable field.

    `low`/`high` are sanity bounds, not business rules. The distinction matters: a bound here
    rejects "the model misread the units", it does not rejectize "this house is a bad buy".
    """

    name: str
    kind: FieldKind
    description: str = ""
    unit: str | None = None
    low: float | None = None
    high: float | None = None
    choices: tuple[str, ...] = ()
    max_chars: int = 400


def _f(name: str, kind: FieldKind, description: str, **kw: Any) -> FieldSpec:
    return FieldSpec(name=name, kind=kind, description=description, **kw)


# The declared schema. Everything a model is allowed to return, and nothing else.
#
# Chosen from what an inspection report, a seller's disclosure, and a set of HOA bylaws
# actually contain, cross-checked against the fields the scoring engine and the deal-breaker
# rules already care about -- roof age, HVAC age, HOA dues, well/septic, flood history.
# Extraction that produces fields nothing consumes is a demo, not a feature.
FIELDS: dict[str, FieldSpec] = {
    f.name: f
    for f in (
        _f("roof_age_years", "int", "Age of the roof covering in years", unit="years", low=0, high=120),
        _f("roof_material", "str", "Roof covering material as written", max_chars=80),
        _f("hvac_age_years", "int", "Age of the primary heating/cooling system", unit="years", low=0, high=80),
        _f("water_heater_age_years", "int", "Age of the water heater", unit="years", low=0, high=60),
        _f("year_built", "int", "Year the structure was built", unit="year", low=1700, high=2030),
        _f("square_feet", "int", "Heated square footage as stated", unit="sqft", low=200, high=20000),
        # A dollar amount, which looks like it contradicts ADR 0004's ban on dollar figures.
        # It does not, and the distinction is the whole boundary: ADR 0004's permitted list
        # names "HOA dues" explicitly. Reading a number that is printed on a page is
        # extraction. Producing a number that was printed nowhere -- a payment, a tax bill, a
        # repair estimate -- is arithmetic, and arithmetic is what is forbidden. The test is
        # not "is it currency", it is "did the document already say it".
        _f("hoa_dues_amount", "float", "HOA dues figure as printed in the document", unit="currency", low=0, high=5000),
        _f("hoa_dues_period", "enum", "Billing period for the HOA dues", choices=("monthly", "quarterly", "annual", "unknown")),
        _f("hoa_special_assessment_mentioned", "bool", "Whether a special assessment is mentioned"),
        _f("water_source", "enum", "Domestic water source", choices=("public", "well", "shared_well", "unknown")),
        _f("sewer_type", "enum", "Waste disposal", choices=("public_sewer", "septic", "unknown")),
        _f("foundation_type", "str", "Foundation as described", max_chars=80),
        _f("known_flooding_mentioned", "bool", "Whether past flooding or water intrusion is disclosed"),
        _f("easement_mentioned", "bool", "Whether an easement or right-of-way is mentioned"),
        _f("defects", "str", "One disclosed defect or deficiency, verbatim or lightly trimmed", max_chars=400),
        _f("permit_issue_mentioned", "bool", "Whether unpermitted work is mentioned"),
    )
}

# Fields a model volunteers unprompted, all of them forbidden by ADR 0004. Named so the
# rejection reason can cite the ADR instead of saying "unknown field", which would make an
# enforced architectural boundary look like a schema typo in the log.
FORBIDDEN_FIELDS: dict[str, str] = {
    "monthly_payment": "a dollar figure",
    "mortgage_payment": "a dollar figure",
    "property_tax": "a dollar figure",
    "estimated_taxes": "a dollar figure",
    "insurance_cost": "a dollar figure",
    "closing_costs": "a dollar figure",
    "maintenance_reserve": "a dollar figure",
    "estimated_value": "a dollar figure",
    "market_value": "a dollar figure",
    "repair_cost": "a dollar figure",
    "repair_estimate": "a dollar figure",
    "dti": "a dollar figure",
    "score": "the property score",
    "property_score": "the property score",
    "recommendation": "the buy/watch/pass verdict",
    "verdict": "the buy/watch/pass verdict",
    "decision": "the buy/watch/pass verdict",
    "is_good_deal": "the buy/watch/pass verdict",
}

_WS = re.compile(r"\s+")


def _normalize(text: str) -> str:
    """Collapse whitespace and lowercase, for quote grounding.

    Needed because PDF text extraction inserts line breaks mid-sentence, so a model quoting
    a sentence it read across a line wrap produces a string that is character-for-character
    absent from the source while being unmistakably present to a human. Normalising
    whitespace is the smallest relaxation that fixes that.

    Deliberately *not* relaxed further -- no punctuation stripping, no fuzzy ratio. A quote
    that only matches at 85% similarity is a quote the model partly wrote itself, and that is
    the case this check exists to catch.
    """
    return _WS.sub(" ", text).strip().lower()


@dataclass(frozen=True)
class Finding:
    """One accepted extracted field, with the evidence it came from.

    `confirmed` is the load-bearing field. ADR 0004 requires a human to confirm an extracted
    value before it affects a score, and `to_value()` enforces that by raising rather than by
    returning something scoring would quietly accept.
    """

    field_name: str
    value: Any
    quote: str
    line: int
    document: str
    document_sha256: str
    provider: str
    extracted_at: str
    confirmed: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "field": self.field_name,
            "value": self.value,
            "quote": self.quote,
            "line": self.line,
            "document": self.document,
            "document_sha256": self.document_sha256,
            "provider": self.provider,
            "extracted_at": self.extracted_at,
            "confidence": "extracted",
            "confirmed": self.confirmed,
        }

    def confirm(self) -> Finding:
        """A person has read the quote and agrees. Returns a new Finding; does not mutate."""
        return replace(self, confirmed=True)

    def to_value(self) -> Value:
        """The scoring-engine representation. Raises unless confirmed.

        Note the confidence stays `extracted` even after confirmation, rather than being
        promoted to `measured`. A person agreeing that a document says the roof is 18 years
        old does not make it a measurement -- the document could be wrong. Confirmation
        removes the "a model might have made this up" risk, not the "the source might be
        wrong" risk, and the confidence label should only claim the second one is gone.
        """
        if not self.confirmed:
            raise ValueError(
                f"{self.field_name!r} is unconfirmed; ADR 0004 requires human confirmation "
                "before an extracted field can affect a score"
            )
        return Value(
            value=self.value,
            confidence="extracted",
            note=f'confirmed from {self.document} line {self.line}: "{self.quote[:120]}"',
            retrieved_at=self.extracted_at,
        )


@dataclass(frozen=True)
class Rejection:
    """A field the code refused, and why. Kept, not dropped.

    Rejections are output, not errors. A run that refused six fields and accepted four is a
    successful run with a visible boundary, and throwing the refusals away would hide the
    single most interesting thing about it.
    """

    field_name: str
    reason: str
    detail: str = ""
    offered_value: Any = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "field": self.field_name,
            "reason": self.reason,
            "detail": self.detail,
            "offered_value": self.offered_value,
        }


@dataclass
class ExtractionResult:
    findings: list[Finding] = field(default_factory=list)
    rejections: list[Rejection] = field(default_factory=list)

    @property
    def accepted(self) -> int:
        return len(self.findings)

    @property
    def refused(self) -> int:
        return len(self.rejections)

    def to_dict(self) -> dict[str, Any]:
        return {
            "findings": [f.to_dict() for f in self.findings],
            "rejections": [r.to_dict() for r in self.rejections],
            "accepted": self.accepted,
            "refused": self.refused,
            # Spelled out rather than left for a reader to infer. Every consumer of this
            # payload needs to know the findings are not scoring inputs yet.
            "note": (
                "Every finding is confidence=extracted and confirmed=false. Per ADR 0004 a "
                "human must confirm a finding before it can affect a score, and no dollar "
                "figure, score, or verdict is ever extracted from a document."
            ),
        }


def _coerce(spec: FieldSpec, raw: Any) -> tuple[Any, str | None]:
    """Turn the model's value into the declared type, or explain why it cannot be.

    Models return "18 years" and "approximately 18" for an int field. Trimming a trailing
    unit is a parse concern and gets handled; anything vaguer is refused, because guessing at
    what a fuzzy value meant is the code doing the model's job badly.
    """
    if raw is None:
        return None, "value was null"

    if spec.kind == "bool":
        if isinstance(raw, bool):
            return raw, None
        if isinstance(raw, str) and raw.strip().lower() in {"true", "yes", "y"}:
            return True, None
        if isinstance(raw, str) and raw.strip().lower() in {"false", "no", "n"}:
            return False, None
        return None, f"expected a boolean, got {raw!r}"

    if spec.kind == "enum":
        if not isinstance(raw, str):
            return None, f"expected one of {spec.choices}, got {raw!r}"
        candidate = raw.strip().lower().replace(" ", "_").replace("-", "_")
        if candidate in spec.choices:
            return candidate, None
        return None, f"{raw!r} is not one of {spec.choices}"

    if spec.kind == "str":
        if not isinstance(raw, str) or not raw.strip():
            return None, f"expected a non-empty string, got {raw!r}"
        text = raw.strip()
        if len(text) > spec.max_chars:
            return None, f"string is {len(text)} chars, limit is {spec.max_chars}"
        return text, None

    # int / float
    if isinstance(raw, bool):  # bool is an int subclass; catching this early avoids True -> 1
        return None, f"expected a number, got boolean {raw!r}"
    number: float | int | None = None
    if isinstance(raw, (int, float)):
        number = raw
    elif isinstance(raw, str):
        match = re.search(r"-?\d+(?:\.\d+)?", raw.replace(",", ""))
        if match:
            number = float(match.group(0))
    if number is None:
        return None, f"expected a number, got {raw!r}"
    if spec.kind == "int":
        if float(number) != int(number):
            return None, f"expected a whole number, got {raw!r}"
        number = int(number)
    if spec.low is not None and number < spec.low:
        return None, f"{number} is below the plausible minimum of {spec.low}"
    if spec.high is not None and number > spec.high:
        return None, f"{number} is above the plausible maximum of {spec.high}"
    return number, None


def parse_findings(
    raw_fields: Any,
    *,
    sent_text: str,
    document: str,
    document_sha256: str,
    provider: str,
    extracted_at: str | None = None,
) -> ExtractionResult:
    """Judge a model's parsed response.

    `sent_text` must be the exact text the model was given -- the redacted text, not the
    original file. Grounding a quote against the original would accept a quote containing a
    social security number the model could not have seen, which means grounding had silently
    stopped testing anything.
    """
    result = ExtractionResult()
    stamp = extracted_at or now_iso()

    if not isinstance(raw_fields, list):
        result.rejections.append(
            Rejection(
                field_name="(response)",
                reason="malformed response",
                detail=f"expected a list of field objects, got {type(raw_fields).__name__}",
            )
        )
        return result

    haystack = _normalize(sent_text)
    lines = sent_text.splitlines()
    seen: set[str] = set()

    for item in raw_fields:
        if not isinstance(item, dict):
            result.rejections.append(
                Rejection("(item)", "malformed item", f"expected an object, got {type(item).__name__}")
            )
            continue

        name = str(item.get("field", "")).strip()
        offered = item.get("value")

        if not name:
            result.rejections.append(Rejection("(unnamed)", "missing field name", offered_value=offered))
            continue

        if name in FORBIDDEN_FIELDS:
            result.rejections.append(
                Rejection(
                    name,
                    "forbidden by ADR 0004",
                    f"{FORBIDDEN_FIELDS[name]} is never extracted from a document; the model "
                    "reads and the code decides",
                    offered,
                )
            )
            continue

        spec = FIELDS.get(name)
        if spec is None:
            result.rejections.append(
                Rejection(name, "not in the declared schema", "no such field in FIELDS", offered)
            )
            continue

        # Repeats are refused rather than overwritten. Two different roof ages with two
        # different quotes is a real signal that the document is ambiguous, and silently
        # keeping the last one would hide that.
        if name in seen:
            result.rejections.append(
                Rejection(name, "duplicate field", "already extracted earlier in this response", offered)
            )
            continue

        quote = item.get("quote")
        if not isinstance(quote, str) or not quote.strip():
            result.rejections.append(
                Rejection(name, "missing citation", "ADR 0004 requires a verbatim quote per field", offered)
            )
            continue
        quote = quote.strip()

        if _normalize(quote) not in haystack:
            result.rejections.append(
                Rejection(
                    name,
                    "citation not found in the document",
                    f'the quoted text is not present in what was sent: "{quote[:120]}"',
                    offered,
                )
            )
            continue

        value, problem = _coerce(spec, offered)
        if problem is not None:
            result.rejections.append(Rejection(name, "implausible or unparseable value", problem, offered))
            continue

        # Line number is recomputed here rather than trusted from the response. A model's
        # line number is a guess; the quote is evidence, and the quote is what we located.
        line = _locate(quote, lines)

        seen.add(name)
        result.findings.append(
            Finding(
                field_name=name,
                value=value,
                quote=quote,
                line=line,
                document=document,
                document_sha256=document_sha256,
                provider=provider,
                extracted_at=stamp,
            )
        )

    return result


def _locate(quote: str, lines: list[str]) -> int:
    """1-based line of the quote's first word run, or 0 if it spans lines untraceably.

    Returns 0 rather than raising or guessing. A citation whose line cannot be pinned is
    still a grounded citation -- grounding already passed against the whole document -- and
    0 reads as "not located" to anyone looking at the output.
    """
    needle = _normalize(quote)
    if not needle:
        return 0
    for i, line in enumerate(lines, start=1):
        if needle in _normalize(line):
            return i
    # Spans a line break. Fall back to the line containing the quote's opening words.
    head = " ".join(needle.split()[:4])
    for i, line in enumerate(lines, start=1):
        if head and head in _normalize(line):
            return i
    return 0


def schema_for_prompt() -> list[dict[str, Any]]:
    """The declared schema in a form a prompt can carry.

    Generated from `FIELDS` rather than written out in the prompt string, so the prompt
    cannot drift from what `parse_findings` will accept. A prompt asking for a field the
    parser rejects is a guaranteed rejection on every run, and that failure is invisible
    until someone reads the rejection list.
    """
    out: list[dict[str, Any]] = []
    for spec in FIELDS.values():
        entry: dict[str, Any] = {"field": spec.name, "type": spec.kind, "description": spec.description}
        if spec.unit:
            entry["unit"] = spec.unit
        if spec.choices:
            entry["choices"] = list(spec.choices)
        if spec.low is not None or spec.high is not None:
            entry["range"] = [spec.low, spec.high]
        out.append(entry)
    return out
