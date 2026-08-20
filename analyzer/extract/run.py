"""The orchestration: file in, judged findings out, one log line either way.

The order of operations here is the security property, so it is worth reading as a sequence
rather than as a function:

    load  ->  filter to relevant pages  ->  REDACT  ->  send  ->  judge  ->  log

Redaction sits between the file and the provider with nothing that can route around it. The
provider is handed `redacted.text` and never sees the `Document`, so there is no call site in
which an unredacted string can reach a network -- not by mistake and not by a future
refactor that forgets. And `parse_findings` grounds citations against the same redacted text,
so the grounding check and the model are looking at identical bytes.

A failed call still writes a log line, with `error` set. A log that only records successes
cannot answer "was this document ever sent", which is the question it exists for.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..core.extraction import ExtractionResult, FIELDS, parse_findings
from ..core.redact import RedactedText, redact
from . import calllog
from .documents import Document, load_document
from .prompt import ResponseUnparseable, parse_response_json
from .providers import ExtractionProvider, OfflineProvider, ProviderError


@dataclass
class ExtractionRun:
    """Everything about one extraction, including what was refused and what was redacted."""

    document: Document
    redacted: RedactedText
    result: ExtractionResult
    provider: str
    model: str
    elapsed_ms: int
    log_path: str
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "document": {
                "path": self.document.path,
                "sha256": self.redacted.sha256,
                "pages_total": self.document.pages_total,
                "pages_sent": self.document.pages_sent,
                "chars_sent": self.redacted.chars,
                "filtered": self.document.filtered,
            },
            "redaction": self.redacted.report.to_dict(),
            "provider": {"name": self.provider, "model": self.model, "elapsed_ms": self.elapsed_ms},
            "log": self.log_path,
        }
        payload.update(self.result.to_dict())
        if self.error:
            payload["error"] = self.error
        return payload


def extract_from_document(
    path: str | Path,
    *,
    provider: ExtractionProvider | None = None,
    known_names: tuple[str, ...] = (),
    filter_pages: bool = True,
    log_path: Path | None = None,
) -> ExtractionRun:
    """Run one document through the whole path.

    `provider` defaults to `OfflineProvider` rather than to a network provider, so importing
    and calling this function cannot send a document anywhere the caller did not ask for.
    """
    engine = provider or OfflineProvider()
    document = load_document(path, filter_pages=filter_pages)

    # The only redaction call in the codebase's send path. Everything after this line sees
    # `redacted`, and `document.text` is not passed onward again.
    redacted = redact(document.text, known_names=known_names)

    started = time.monotonic()
    error: str | None = None
    result = ExtractionResult()
    tokens: int | None = None
    model_name = getattr(engine, "model", "unknown")
    elapsed = 0

    try:
        response = engine.complete(redacted.text)
        elapsed = response.elapsed_ms
        tokens = response.tokens
        model_name = response.model
        raw_fields = parse_response_json(response.raw)
        result = parse_findings(
            raw_fields,
            sent_text=redacted.text,
            document=str(document.path),
            document_sha256=redacted.sha256,
            provider=response.provider,
        )
    except (ProviderError, ResponseUnparseable) as exc:
        # Caught rather than raised, matching the station contract in sources/base.py: a
        # source going dark degrades the run and is reported, it does not become a traceback.
        error = str(exc)
        elapsed = int((time.monotonic() - started) * 1000)

    reasons: dict[str, int] = {}
    for rejection in result.rejections:
        reasons[rejection.reason] = reasons.get(rejection.reason, 0) + 1

    record = calllog.record_for(
        document=str(document.path),
        document_sha256=redacted.sha256,
        provider=getattr(engine, "name", "unknown"),
        model=model_name,
        redaction=redacted.report.to_dict(),
        fields_requested=sorted(FIELDS),
        pages_total=document.pages_total,
        pages_sent=document.pages_sent,
        chars_sent=redacted.chars,
        accepted_fields=[f.field_name for f in result.findings],
        rejection_reasons=reasons,
        refused=len(result.rejections),
        elapsed_ms=elapsed,
        tokens=tokens,
        error=error,
    )
    written = calllog.write_record(record, path=log_path)

    return ExtractionRun(
        document=document,
        redacted=redacted,
        result=result,
        provider=getattr(engine, "name", "unknown"),
        model=model_name,
        elapsed_ms=elapsed,
        log_path=str(written),
        error=error,
    )
