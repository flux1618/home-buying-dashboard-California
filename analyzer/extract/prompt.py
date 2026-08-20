"""Building the request, and getting JSON back out of whatever the model returned.

The prompt is generated from `core.extraction.FIELDS`, never hand-written. A hand-written
prompt drifts from the parser the first time a field is added, and the failure is invisible:
the model dutifully returns the field the prompt asked for, the parser rejects it as
undeclared, and the only evidence is a line in the rejection list nobody reads.

The instructions do three things beyond describing the schema:

  - Tell the model to omit a field rather than guess it. ADR 0004's provenance rule is that an
    absent value stays absent; a model that fills gaps produces data indistinguishable from
    data that was read.
  - Require a verbatim quote per field, because the parser will check it against the document
    and drop anything it cannot find.
  - Tell it not to compute anything. This is belt-and-braces -- `FORBIDDEN_FIELDS` refuses
    those fields regardless -- but a model told not to volunteer a payment estimate mostly
    will not, and a rejection avoided is cheaper than a rejection caught.
"""

from __future__ import annotations

import json
import re
from typing import Any

from ..core.extraction import schema_for_prompt

SYSTEM_PROMPT = """\
You extract facts from real-estate documents. You do not calculate, estimate, or judge.

Rules:
1. Return only fields from the provided schema. Never invent a field.
2. Every field must include a `quote`: text copied EXACTLY from the document, word for word.
   A quote that is not present in the document verbatim will be discarded.
3. If the document does not state something, omit that field entirely. Do not guess, infer,
   or fill a gap. An omitted field is correct; a guessed field is a defect.
4. Never return a dollar amount you calculated, a payment, a tax estimate, a repair cost, a
   property score, or an opinion about whether the property is a good purchase. Those are
   computed elsewhere and will be rejected.
5. Return a JSON object: {"fields": [{"field": ..., "value": ..., "quote": ...}]}
   Return nothing else -- no explanation, no markdown fence.
"""


def build_user_prompt(text: str, *, schema: list[dict[str, Any]] | None = None) -> str:
    fields = schema if schema is not None else schema_for_prompt()
    return (
        "SCHEMA (the only fields you may return):\n"
        + json.dumps(fields, indent=2)
        + "\n\nDOCUMENT:\n"
        + "-----\n"
        + text
        + "\n-----\n\n"
        + 'Return {"fields": [...]} using only the schema above, each with a verbatim quote.'
    )


# Models wrap JSON in ```json fences roughly half the time regardless of instructions, so
# stripping a fence is a parsing concern and not a sign the model misbehaved.
_FENCE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)


class ResponseUnparseable(ValueError):
    """The model's output was not JSON at all.

    Distinct from a schema rejection on purpose. A rejection means the model answered and the
    code refused a field; this means there was no answer to judge. Conflating them would make
    a broken provider look like a well-enforced boundary in the log.
    """


def parse_response_json(raw: str) -> list[Any]:
    """Pull the `fields` list out of a model response.

    Accepts a bare list as well as the documented object form. Smaller models return
    `[{...}]` when asked for `{"fields": [...]}` often enough that refusing it would throw
    away good extractions over a wrapper.
    """
    if not raw or not raw.strip():
        raise ResponseUnparseable("the model returned an empty response")

    text = raw.strip()
    fence = _FENCE.search(text)
    if fence:
        text = fence.group(1).strip()

    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        # Last resort: find the outermost brace or bracket span. Some models prepend a
        # sentence despite instructions, and discarding an otherwise-valid extraction over a
        # leading "Here is the JSON:" would be the parser being precious.
        match = re.search(r"[\[{].*[\]}]", text, re.DOTALL)
        if not match:
            raise ResponseUnparseable(f"no JSON found in the response: {raw[:200]!r}") from None
        try:
            payload = json.loads(match.group(0))
        except json.JSONDecodeError as exc:
            raise ResponseUnparseable(f"response is not valid JSON: {exc}") from exc

    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("fields", "results", "extracted", "data"):
            if isinstance(payload.get(key), list):
                return payload[key]
        raise ResponseUnparseable(
            f"response object has no list of fields; keys were {sorted(payload)[:8]}"
        )
    raise ResponseUnparseable(f"expected an object or list, got {type(payload).__name__}")
