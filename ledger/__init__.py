"""The saved-property ledger: what you looked at, what the engine said, what you decided.

Phase 3 of the roadmap. A separate top-level package rather than a module inside
`analyzer/`, for one architectural reason worth stating up front:

    The engine does not know this package exists.

`analyzer.core` is pure by decision (ADR 0002) -- no network, no clock, no filesystem, so
that a score is a function of its inputs and nothing else. A store is nothing but I/O. If
`analyzer` imported `ledger`, that purity would be a claim rather than a property. So the
dependency runs one way only: `ledger` knows how to persist an analysis document, and
`analyzer` has never heard of it. The doors -- CLI, HTTP -- wire the two together.

You can check that claim rather than trust it: `tests/test_core_purity.py` already asserts
`analyzer.core` imports nothing that does I/O, and `test_ledger.py` asserts nothing under
`analyzer/` imports `ledger`.

Why "ledger" and not "store" or "db": the shape of the thing is append-only. An analysis is
an observation made at a moment, under a specific engine version and a specific set of
household assumptions. Overwriting it would destroy the only record of what you knew when
you made a decision -- which is the entire point of keeping it. Prices move, rates move, and
the scoring rules themselves move. A ledger records; it does not restate.

Two consequences of that shape:

* **Price-change history is not a feature, it is a query.** The same address analyzed twice
  is two rows. The history *is* the table.
* **A saved score is a historical fact, never a current answer.** If the engine version or
  the profile fingerprint has changed since a row was written, that row's score is not
  comparable to today's. Every read path that shows a score delta says which of the three
  possible causes it is: the price moved, the engine changed, or your assumptions changed.

Append-only is enforced by SQLite triggers, not by convention -- see `ledger/db.py`.
"""

from __future__ import annotations

from .db import DEFAULT_DB_ENV, SCHEMA_VERSION, connect, database_path, migrate
from .repo import (
    Ledger,
    LedgerError,
    PropertyNotFound,
    STATUSES,
    property_key,
)

__all__ = [
    "DEFAULT_DB_ENV",
    "SCHEMA_VERSION",
    "STATUSES",
    "Ledger",
    "LedgerError",
    "PropertyNotFound",
    "connect",
    "database_path",
    "migrate",
    "property_key",
]
