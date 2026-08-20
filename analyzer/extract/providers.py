"""The model call. One interface, three implementations, and stdlib HTTP.

`urllib.request` rather than `httpx` or a vendor SDK, for the same reason the rest of the
project avoids dependencies: an OpenAI-compatible chat completion is one POST with a JSON
body, and taking on a package -- plus its transitive tree, plus its release cadence -- to
avoid writing fifteen lines is a bad trade in a repo whose selling point is that the core runs
on a stdlib-only interpreter.

The three providers, and why each exists:

  - `OfflineProvider` -- deterministic, no network, no key. Read its docstring before
    judging it: it is a test fixture that happens to be useful, not a language model.
  - `OllamaProvider` -- a model on the existing K3s cluster. The threat model prefers this
    because the document never leaves the network.
  - `OpenAICompatibleProvider` -- anything speaking `/v1/chat/completions`. Better extraction
    quality, at the cost of sending redacted document text to a third party.

Every provider raises `ProviderError` and nothing else. The station contract in
`sources/base.py` established that pattern for this codebase -- a failure is a value to be
reported, not an exception that escapes to a stack trace in front of a user.
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Protocol

from .prompt import SYSTEM_PROMPT, build_user_prompt


class ProviderError(RuntimeError):
    """The model could not be reached, or returned something unusable."""


@dataclass(frozen=True)
class ProviderResponse:
    raw: str
    provider: str
    model: str
    elapsed_ms: int
    # None when the provider does not report usage, which is most local runtimes. Kept
    # distinct from 0 so the log does not claim a free call was made.
    tokens: int | None = None


class ExtractionProvider(Protocol):
    name: str
    model: str

    def complete(self, text: str) -> ProviderResponse: ...


# --------------------------------------------------------------------------------------
# Offline
# --------------------------------------------------------------------------------------

# Patterns that pull a handful of fields out of typical inspection-report phrasing. Each entry
# is (field, regex, converter). The regex must capture the value in group 1 and the whole match
# is used as the quote, which is what keeps the offline path honest -- it is subject to the
# same citation grounding as a real model, because its quote comes from the document.
_OFFLINE_PATTERNS: list[tuple[str, re.Pattern[str], str]] = [
    ("roof_age_years", re.compile(r"roof[^.\n]{0,60}?(?:approximately|approx\.?|about|est\.?)?\s*(\d{1,2})\s*(?:\+)?\s*years?\s*old", re.I), "int"),
    ("hvac_age_years", re.compile(r"(?:hvac|furnace|heat pump|air handler)[^.\n]{0,60}?(\d{1,2})\s*years?\s*old", re.I), "int"),
    ("water_heater_age_years", re.compile(r"water heater[^.\n]{0,60}?(\d{1,2})\s*years?\s*old", re.I), "int"),
    ("year_built", re.compile(r"(?:year built|built in|constructed in)\s*[:\-]?\s*(\d{4})", re.I), "int"),
    ("square_feet", re.compile(r"(?:heated (?:square )?(?:feet|footage|area)|square footage|living area)\s*[:\-]?\s*([\d,]{3,7})", re.I), "int"),
    # Anchored on "dues"/"assessment" rather than on "HOA", because the sentence that names
    # the association is usually not the sentence that carries the number -- "governed by an
    # HOA. Dues of $85.00 per month" defeated an HOA-anchored pattern that could not cross the
    # full stop.
    ("hoa_dues_amount", re.compile(r"(?:dues|assessment|fee)s?\s*(?:of|are|is|:)?\s*\$\s*([\d,]+(?:\.\d{2})?)", re.I), "float"),
    ("roof_material", re.compile(r"roof(?:ing)?(?:\s*covering)?\s*[:\-]\s*([A-Za-z][A-Za-z /-]{3,40})", re.I), "str"),
    ("foundation_type", re.compile(r"foundation\s*[:\-]\s*([A-Za-z][A-Za-z /-]{3,40})", re.I), "str"),
]

# Boolean and enum fields: (field, value, regex). First match wins.
_OFFLINE_FLAGS: list[tuple[str, Any, re.Pattern[str]]] = [
    ("water_source", "well", re.compile(r"\b(?:private well|well water|water source\s*[:\-]\s*well)\b", re.I)),
    ("water_source", "public", re.compile(r"\b(?:public water|municipal water|city water)\b", re.I)),
    ("sewer_type", "septic", re.compile(r"\bseptic\b[^.\n]{0,40}", re.I)),
    ("sewer_type", "public_sewer", re.compile(r"\b(?:public sewer|municipal sewer|city sewer)\b", re.I)),
    ("known_flooding_mentioned", True, re.compile(r"\b(?:flood(?:ing|ed)?|water intrusion)\b[^.\n]{0,60}", re.I)),
    ("easement_mentioned", True, re.compile(r"\b(?:easement|right[- ]of[- ]way)\b[^.\n]{0,60}", re.I)),
    ("permit_issue_mentioned", True, re.compile(r"\b(?:unpermitted|without a permit|no permit on file)\b[^.\n]{0,60}", re.I)),
    ("hoa_special_assessment_mentioned", True, re.compile(r"\bspecial assessment\b[^.\n]{0,60}", re.I)),
    ("hoa_dues_period", "monthly", re.compile(r"\b(?:per month|monthly|/mo)\b", re.I)),
]


class OfflineProvider:
    """A deterministic stand-in. **This is not a language model and does not pretend to be.**

    Being blunt about what it is, because a reader who mistakes it for one will draw the wrong
    conclusion about the whole feature: it is roughly twenty regexes over common inspection-
    report phrasing. It will miss anything worded unusually, which is most of a real document,
    and that limitation is the entire reason a language model is the right tool here.

    So why does it exist? Because it makes every rule in `core/extraction.py` testable with no
    API key and no network. It produces a response in the same shape a model produces, so
    `pytest` can drive redaction, citation grounding, type coercion, plausibility bounds,
    forbidden-field refusal, and the call log end to end -- on a laptop on a plane, and in CI,
    which has no credentials and should never have any.

    A second, smaller reason: it is the default. Someone who clones this repo gets a working
    demo instead of a key prompt, and can see the boundary enforce itself before deciding
    whether to point it at a real model.

    Note that its quotes are real spans from the document, so it is subject to the same
    grounding check as any provider. Nothing gets a pass for being local.
    """

    name = "offline-regex"
    model = "deterministic-patterns"

    def complete(self, text: str) -> ProviderResponse:
        started = time.monotonic()
        fields: list[dict[str, Any]] = []
        seen: set[str] = set()

        for field_name, pattern, kind in _OFFLINE_PATTERNS:
            if field_name in seen:
                continue
            match = pattern.search(text)
            if not match:
                continue
            captured = match.group(1).strip()
            if kind == "int":
                value: Any = int(captured.replace(",", ""))
            elif kind == "float":
                value = float(captured.replace(",", ""))
            else:
                value = captured.rstrip(" .,;")
            fields.append({"field": field_name, "value": value, "quote": match.group(0).strip()})
            seen.add(field_name)

        for field_name, value, pattern in _OFFLINE_FLAGS:
            if field_name in seen:
                continue
            match = pattern.search(text)
            if not match:
                continue
            fields.append({"field": field_name, "value": value, "quote": match.group(0).strip()})
            seen.add(field_name)

        # Defects are collected as separate findings, but `parse_findings` refuses duplicate
        # fields by design, so only the first survives. That is a real limitation of a
        # one-value-per-field schema meeting a document that lists nine defects, and it is
        # recorded in docs/KNOWN_LIMITATIONS.md rather than worked around here.
        # Two passes, bullets first. A deficiency summary is a bulleted list, whereas the word
        # "recommend" also appears mid-narrative -- and because PDF text wraps at 80 columns, a
        # naive first-match grabs half a sentence like "based on the seller's statement and
        # observed granule loss." Preferring a bullet gets a whole, self-contained finding.
        words = ("recommend", "deficien", "defect", "should be repaired", "safety hazard", "hazard")
        bullets: list[str] = []
        others: list[str] = []
        for line in text.splitlines():
            raw_line = line.rstrip()
            stripped = raw_line.strip(" -*\u2022\t")
            if not (20 < len(stripped) < 380):
                continue
            if not any(word in stripped.lower() for word in words):
                continue
            (bullets if raw_line.lstrip().startswith(("-", "*", "\u2022")) else others).append(stripped)
        candidate = (bullets or others)[:1]
        for stripped in candidate:
            fields.append({"field": "defects", "value": stripped, "quote": stripped})

        elapsed = int((time.monotonic() - started) * 1000)
        return ProviderResponse(
            raw=json.dumps({"fields": fields}),
            provider=self.name,
            model=self.model,
            elapsed_ms=elapsed,
        )


# --------------------------------------------------------------------------------------
# Network providers
# --------------------------------------------------------------------------------------


def _post_json(url: str, payload: dict[str, Any], *, headers: dict[str, str], timeout: int) -> dict[str, Any]:
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=body, method="POST")
    request.add_header("Content-Type", "application/json")
    for key, value in headers.items():
        request.add_header(key, value)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")[:300]
        except Exception:  # pragma: no cover - the error body is best-effort
            pass
        # The status is surfaced because the three that matter have different fixes: 401 is a
        # key, 404 is usually a model name that is not pulled, 429 is a wait.
        raise ProviderError(f"{url} returned HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise ProviderError(f"could not reach {url}: {exc.reason}") from exc
    except TimeoutError as exc:
        raise ProviderError(f"{url} timed out after {timeout}s") from exc
    except json.JSONDecodeError as exc:
        raise ProviderError(f"{url} returned a non-JSON body: {exc}") from exc


@dataclass
class OllamaProvider:
    """A model on the local network -- the threat model's preferred path.

    Preferred because the document never leaves the machine or the cluster, which removes the
    third-party-retention risk entirely instead of mitigating it. The cost is extraction
    quality: a 7B model quantised to run on a Pi will miss fields a frontier model catches.

    `format="json"` is set because Ollama supports constrained JSON output, which removes the
    most common failure with small models -- a perfectly good extraction wrapped in prose.
    """

    model: str = "llama3.1:8b"
    host: str = "http://localhost:11434"
    timeout: int = 180
    name: str = "ollama"

    def complete(self, text: str) -> ProviderResponse:
        started = time.monotonic()
        data = _post_json(
            f"{self.host.rstrip('/')}/api/chat",
            {
                "model": self.model,
                "stream": False,
                "format": "json",
                # temperature 0: extraction should be reproducible. Two runs over the same
                # document returning different fields makes the eval log meaningless.
                "options": {"temperature": 0},
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": build_user_prompt(text)},
                ],
            },
            headers={},
            timeout=self.timeout,
        )
        content = (data.get("message") or {}).get("content")
        if not isinstance(content, str):
            raise ProviderError(f"unexpected Ollama response shape: keys {sorted(data)[:8]}")
        return ProviderResponse(
            raw=content,
            provider=self.name,
            model=self.model,
            elapsed_ms=int((time.monotonic() - started) * 1000),
            tokens=data.get("eval_count"),
        )


@dataclass
class OpenAICompatibleProvider:
    """Any `/v1/chat/completions` endpoint. The key comes from the environment, never a file.

    Environment rather than config because a key in a config file is a key that gets committed.
    This repo is public and already carries real financial figures -- see
    docs/KNOWN_LIMITATIONS.md -- so the one secret involved stays out of the tree entirely.
    """

    model: str = "gpt-4o-mini"
    base_url: str = "https://api.openai.com/v1"
    api_key_env: str = "HBA_LLM_API_KEY"
    timeout: int = 120
    name: str = "openai-compatible"

    def complete(self, text: str) -> ProviderResponse:
        key = os.environ.get(self.api_key_env, "").strip()
        if not key:
            raise ProviderError(
                f"{self.api_key_env} is not set. Set it, or use --provider offline, which "
                "needs no key and exercises the same validation path."
            )
        started = time.monotonic()
        data = _post_json(
            f"{self.base_url.rstrip('/')}/chat/completions",
            {
                "model": self.model,
                "temperature": 0,
                "response_format": {"type": "json_object"},
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": build_user_prompt(text)},
                ],
            },
            headers={"Authorization": f"Bearer {key}"},
            timeout=self.timeout,
        )
        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ProviderError(f"unexpected response shape: keys {sorted(data)[:8]}") from exc
        usage = data.get("usage") or {}
        return ProviderResponse(
            raw=content,
            provider=self.name,
            model=self.model,
            elapsed_ms=int((time.monotonic() - started) * 1000),
            tokens=usage.get("total_tokens"),
        )


def build_provider(spec: str = "offline", *, model: str | None = None) -> ExtractionProvider:
    """Resolve a provider name to an instance.

    `offline` is the default everywhere -- CLI, HTTP door, tests. Defaulting to a network
    provider would mean a fresh clone tries to send a document to a third party on the first
    command someone runs, which is not a decision a default should make on a user's behalf.
    """
    key = (spec or "offline").strip().lower()
    if key in {"offline", "none", "regex"}:
        return OfflineProvider()
    if key == "ollama":
        provider = OllamaProvider(host=os.environ.get("HBA_OLLAMA_HOST", "http://localhost:11434"))
        return OllamaProvider(host=provider.host, model=model or provider.model)
    if key in {"openai", "openai-compatible", "compatible"}:
        base = os.environ.get("HBA_LLM_BASE_URL", "https://api.openai.com/v1")
        default = OpenAICompatibleProvider()
        return OpenAICompatibleProvider(base_url=base, model=model or default.model)
    raise ProviderError(f"unknown provider {spec!r}. Known: offline, ollama, openai")
