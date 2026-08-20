"""The call log. Append-only JSONL, one line per model call.

`docs/THREAT_MODEL.md` commits to logging "timestamp, document hash, fields requested,
provider, and whether redaction fired" and notes that this doubles as the eval log. Both jobs
are served by the same record, but they ask different questions of it:

  - **As an audit log** it answers "what left this machine, when, and to whom" -- which is the
    question that matters if a provider is later found to have been retaining input.
  - **As an eval log** it answers "how often does this provider return something the code
    refuses" -- accepted and refused counts per run, with reasons.

## What is deliberately not in it

The document text. Not redacted text either. A log that stores what it sent is a second copy
of the sensitive material, in a file with a longer retention than the request, and it converts
a logging feature into the largest data-at-rest risk in the project. The `sha256` is enough to
answer "was this document sent" for any document you still have, which is the only question an
audit needs.

Extracted *values* are not stored either, for the same reason -- a redacted document whose
extracted defects are logged in full has leaked the interesting part. Field names and rejection
reasons are stored; values and quotes are not.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from ..core.provenance import now_iso


def default_log_path() -> Path:
    """`$HBA_DATA_DIR/llm_calls.jsonl`, matching where the ledger already lives.

    One data directory rather than two. The container mounts a single volume, and a log that
    lands somewhere else is a log that is not in the backup and not in the mount.
    """
    base = os.environ.get("HBA_DATA_DIR", "").strip()
    root = Path(base) if base else Path.home() / ".home-buying-analyzer"
    return root / "llm_calls.jsonl"


@dataclass
class CallRecord:
    """One call. Field order is the order it appears in the JSONL, so `head` is readable."""

    at: str
    document: str
    document_sha256: str
    provider: str
    model: str
    redaction_fired: bool
    redaction_counts: dict[str, int] = field(default_factory=dict)
    fields_requested: list[str] = field(default_factory=list)
    pages_total: int = 0
    pages_sent: int = 0
    chars_sent: int = 0
    accepted: int = 0
    refused: int = 0
    accepted_fields: list[str] = field(default_factory=list)
    rejection_reasons: dict[str, int] = field(default_factory=dict)
    elapsed_ms: int = 0
    tokens: int | None = None
    error: str | None = None

    def to_json(self) -> str:
        return json.dumps(asdict(self), separators=(",", ":"))


def write_record(record: CallRecord, *, path: Path | None = None) -> Path:
    """Append one record. Creates the directory and file if needed.

    Failures here are swallowed on purpose, and this is the one place in the project where
    that is the right call. The log is an observability feature; a full disk or a read-only
    mount should not fail an extraction the user asked for and already paid for. The trade is
    stated so it is a decision rather than an accident: a silently-missing log line is
    possible, and if the log is being used for audit that matters -- which is why the path is
    printed by the CLI on every run, so its absence is noticeable.
    """
    target = path or default_log_path()
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8") as handle:
            handle.write(record.to_json() + "\n")
    except OSError:
        pass
    return target


def read_records(*, path: Path | None = None, limit: int | None = None) -> list[dict[str, Any]]:
    """Read the log back, newest last. Malformed lines are skipped, not fatal.

    Skipped rather than fatal because the file is append-only from possibly-interrupted
    processes: a half-written final line is a normal thing to find after a kill -9, and it
    should not make the whole log unreadable.
    """
    target = path or default_log_path()
    if not target.exists():
        return []
    out: list[dict[str, Any]] = []
    for line in target.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out[-limit:] if limit else out


def summarize(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate for the eval view: acceptance rate and the most common refusals.

    Acceptance rate is the number to watch when swapping providers. A model whose rate drops
    is producing more fields the code refuses, and the reason histogram says which rule --
    `citation not found` means it is fabricating, `forbidden by ADR 0004` means it is
    volunteering arithmetic, `implausible` usually means unit confusion.
    """
    total_accepted = sum(int(r.get("accepted") or 0) for r in records)
    total_refused = sum(int(r.get("refused") or 0) for r in records)
    reasons: dict[str, int] = {}
    providers: dict[str, int] = {}
    errors = 0
    for record in records:
        for reason, count in (record.get("rejection_reasons") or {}).items():
            reasons[reason] = reasons.get(reason, 0) + int(count)
        key = f"{record.get('provider')}/{record.get('model')}"
        providers[key] = providers.get(key, 0) + 1
        if record.get("error"):
            errors += 1
    offered = total_accepted + total_refused
    return {
        "calls": len(records),
        "errors": errors,
        "fields_offered": offered,
        "accepted": total_accepted,
        "refused": total_refused,
        "acceptance_rate": round(total_accepted / offered, 3) if offered else None,
        "rejection_reasons": dict(sorted(reasons.items(), key=lambda kv: -kv[1])),
        "providers": providers,
        "redaction_fired_on": sum(1 for r in records if r.get("redaction_fired")),
    }


def record_for(
    *,
    document: str,
    document_sha256: str,
    provider: str,
    model: str,
    redaction: dict[str, Any],
    fields_requested: list[str],
    pages_total: int,
    pages_sent: int,
    chars_sent: int,
    accepted_fields: list[str],
    rejection_reasons: dict[str, int],
    refused: int,
    elapsed_ms: int,
    tokens: int | None = None,
    error: str | None = None,
) -> CallRecord:
    return CallRecord(
        at=now_iso(),
        document=document,
        document_sha256=document_sha256,
        provider=provider,
        model=model,
        redaction_fired=bool(redaction.get("fired")),
        redaction_counts=dict(redaction.get("counts") or {}),
        fields_requested=fields_requested,
        pages_total=pages_total,
        pages_sent=pages_sent,
        chars_sent=chars_sent,
        accepted=len(accepted_fields),
        refused=refused,
        accepted_fields=accepted_fields,
        rejection_reasons=rejection_reasons,
        elapsed_ms=elapsed_ms,
        tokens=tokens,
        error=error,
    )
