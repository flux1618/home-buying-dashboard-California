"""Provenance wrapper. No number enters the system without knowing where it came from.

Per ADR 0002, every value crossing a module boundary is a `Value`, never a bare float.
This is the contract that makes the "every number is sourced" claim enforceable rather
than aspirational.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal

Confidence = Literal[
    "measured",  # read directly from a primary source
    "derived",  # computed from measured values
    "estimated",  # rule of thumb, or coarser precision than the field implies
    "extracted",  # pulled from a document by a model, not yet human-confirmed
    "unavailable",  # source failed; there is no value
]

VALID_CONFIDENCE = {"measured", "derived", "estimated", "extracted", "unavailable"}


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


@dataclass(frozen=True)
class Value:
    """A number (or any value) plus where it came from and how much to trust it."""

    value: Any
    confidence: Confidence
    source_url: str | None = None
    retrieved_at: str | None = None
    note: str | None = None
    precision: str | None = None

    def __post_init__(self) -> None:
        if self.confidence not in VALID_CONFIDENCE:
            raise ValueError(f"invalid confidence: {self.confidence!r}")
        if self.confidence == "unavailable" and self.value is not None:
            raise ValueError("unavailable values must carry value=None")
        if self.confidence in ("measured", "estimated") and not self.source_url:
            raise ValueError(f"{self.confidence} values require a source_url")

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"value": self.value, "confidence": self.confidence}
        for key in ("source_url", "retrieved_at", "note", "precision"):
            val = getattr(self, key)
            if val is not None:
                out[key] = val
        return out

    # -- convenience ---------------------------------------------------------

    @property
    def is_available(self) -> bool:
        return self.confidence != "unavailable"

    def unwrap(self, default: Any = None) -> Any:
        """Raw value, or `default` when unavailable. Use only at call sites that
        genuinely cannot propagate provenance (scoring predicates, formatting)."""
        return default if self.confidence == "unavailable" else self.value


# -- constructors -----------------------------------------------------------


def measured(value: Any, source_url: str, **kw: Any) -> Value:
    return Value(value, "measured", source_url=source_url, retrieved_at=now_iso(), **kw)


def derived(value: Any, **kw: Any) -> Value:
    return Value(value, "derived", retrieved_at=now_iso(), **kw)


def estimated(value: Any, source_url: str, note: str | None = None, **kw: Any) -> Value:
    return Value(
        value, "estimated", source_url=source_url, retrieved_at=now_iso(), note=note, **kw
    )


def unavailable(note: str) -> Value:
    return Value(None, "unavailable", retrieved_at=now_iso(), note=note)


@dataclass
class Degradation:
    """Recorded whenever a source could not be reached.

    `missing` matters as much as `reason`. "The FCC API is down" tells a reader nothing
    actionable; "the FCC API is down, so fiber_available is unknown" tells them exactly
    which line of the report to distrust and what to go verify by hand.
    """

    station: str
    reason: str
    missing: tuple[str, ...] = ()
    at: str = field(default_factory=now_iso)

    def to_dict(self) -> dict[str, object]:
        return {
            "station": self.station,
            "reason": self.reason,
            "missing": list(self.missing),
            "at": self.at,
        }
