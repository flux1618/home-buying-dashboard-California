"""Operations on the ledger: save, list, history, diff, status, journal.

One class, `Ledger`, wrapping a connection. Not a set of free functions taking a connection
as the first argument, because every caller would then be responsible for remembering to
pass the same one, and not an ORM because the queries here are ten lines of SQL that read
better than any expression tree of them would.

The interesting logic in this file is not the SQL. It is two questions the SQL cannot answer:

1. **What is the identity of a house?** Addresses are typed by humans and normalized by a
   geocoder that is sometimes down. See `property_key`.
2. **What does it mean when a saved score changes?** Three different things, and conflating
   them would turn this tool into a liar. See `diff`.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import re
import sqlite3
from datetime import datetime, timezone
from typing import Any, Iterable

# Where a house sits in the process. Deliberately short: a status list long enough to need a
# diagram is a workflow tool, and this is a decision aid.
STATUSES = ("candidate", "touring", "offer", "passed", "archived")

# What a journal entry is *for*. `assumption` and `outcome` are the pair that makes this a
# decision journal rather than a notes field -- an assumption recorded now can be closed
# later by an outcome that points back at it.
JOURNAL_KINDS = ("assumption", "decision", "observation", "outcome", "status")

_UNRESOLVED_PREFIX = "unresolved:"


class LedgerError(RuntimeError):
    """A caller asked for something the ledger will not do."""


class PropertyNotFound(LedgerError):
    """No property with that key. Separate from LedgerError so the HTTP door can map it to 404."""


# =============================================================================
# Identity
# =============================================================================


def property_key(address: str, *, resolved: bool) -> str:
    """The stable identity of a house, derived from its address.

    Normalization is intentionally shallow: upper-case, strip punctuation, collapse
    whitespace. It is *not* an address parser. "606 Andre Ct" and "606 ANDRE COURT" produce
    different keys, and that is a known limitation rather than an oversight -- USPS-grade
    normalization is a library and a data file, and getting it half right is worse than not
    doing it, because a half-normalizer silently merges two different houses on the same
    street. Instead, the canonical form comes from the geocoder: when a lookup succeeds we
    key on *its* output, which is already normalized consistently by someone whose job that
    is.

    When geocoding was degraded, the key is prefixed. An unresolved key can therefore never
    collide with a resolved one, so a network outage cannot cause two houses to be recorded
    as the same house. The cost is a duplicate row for the same house once geocoding recovers,
    which is visible, cheap, and the right way round: a visible duplicate is a nuisance, a
    silent merge is a wrong decision.

    Note that the two cannot be merged after the fact -- `analyses` is append-only, so the
    rows cannot be re-pointed at a different key. The remedy is to re-analyze the address
    once geocoding works and delete the unresolved stub, which is only allowed while no
    history is attached to it worth keeping. See docs/KNOWN_LIMITATIONS.md.
    """
    cleaned = re.sub(r"[^A-Z0-9 ]+", " ", address.upper())
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if not cleaned:
        raise LedgerError("cannot key a property on an empty address")
    return cleaned if resolved else f"{_UNRESOLVED_PREFIX}{cleaned}"


def profile_fingerprint(profile: Any) -> str:
    """A short hash of every field of the buyer profile.

    This is the second half of "is this score comparable to that score". The engine version
    tells you the *rules* were the same; the fingerprint tells you the *assumptions* were --
    income, down payment, rate, penalty weights, hard-fail list. Change the mortgage rate in
    `buyer_profile.toml` and every score in the ledger becomes historical. Without this
    column you would compare them anyway and conclude the market moved.

    Truncated to 12 hex characters. This is a change-detector, not a security control; a
    collision means two profiles look identical, and 48 bits is far past enough for a file
    that will hold dozens of rows.
    """
    payload = dataclasses.asdict(profile) if dataclasses.is_dataclass(profile) else dict(profile)
    encoded = json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:12]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# =============================================================================
# The ledger
# =============================================================================


class Ledger:
    """Read and write the saved-property store.

    Owns no connection lifecycle beyond what it is handed: whoever opened the connection
    closes it. That keeps the class usable from a request handler, a CLI invocation, and a
    test fixture without three different context-manager behaviours.
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    # -- writes ---------------------------------------------------------------

    def save_analysis(
        self,
        document: dict[str, Any],
        *,
        profile: Any,
        raw_input: str | None = None,
        degraded: bool | None = None,
    ) -> dict[str, Any]:
        """Record one analysis. Returns the property row, the new analysis id, and the diff.

        The document is stored whole and the indexed columns are *derived from it here*, in
        one place. A caller that could pass a price different from `document["input"]["price"]`
        would eventually pass a wrong one, and then the list view and the detail view would
        disagree about the same house.

        Saving is always an insert. Re-analyzing an address you already saved does not update
        anything -- it appends, and the previous row remains the record of what you knew then.
        """
        location = document.get("location") or {}
        matched = location.get("matched_address")
        requested = raw_input or location.get("requested_address") or matched
        if not requested:
            raise LedgerError("document has no address to key on")

        # Resolution is inferred from whether the geocoder produced a canonical address and
        # coordinates, unless the caller states it. Inferring rather than trusting a flag
        # means a partially degraded document cannot be recorded as resolved by accident.
        if degraded is None:
            resolved = bool(matched and location.get("latitude") is not None)
        else:
            resolved = not degraded and bool(matched)

        key = property_key(matched if resolved else requested, resolved=resolved)
        score = document.get("score") or {}
        cost = document.get("cost") or {}
        inputs = document.get("input") or {}
        now = _now()

        with self.conn:
            existing = self.conn.execute(
                "SELECT key FROM properties WHERE key = ?", (key,)
            ).fetchone()
            if existing is None:
                self.conn.execute(
                    """
                    INSERT INTO properties
                        (key, raw_input, matched_address, resolved, latitude, longitude,
                         county_fips, status, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, 'candidate', ?, ?)
                    """,
                    (
                        key,
                        requested,
                        matched,
                        1 if resolved else 0,
                        location.get("latitude"),
                        location.get("longitude"),
                        location.get("county_fips"),
                        now,
                        now,
                    ),
                )
            else:
                # Only the timestamp moves. Not `raw_input` -- the first thing you typed is a
                # historical fact too, and not `matched_address`, because a geocoder returning
                # something different for the same key is a signal worth noticing, not a
                # value worth silently overwriting.
                self.conn.execute(
                    "UPDATE properties SET updated_at = ? WHERE key = ?", (now, key)
                )

            cursor = self.conn.execute(
                """
                INSERT INTO analyses
                    (property_key, analyzed_at, engine_version, profile_fingerprint, price,
                     score, verdict, score_pinned, score_capped, piti, front_end_dti,
                     cash_to_close, degraded_sources, document)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    key,
                    document.get("analyzed_at") or now,
                    document.get("engine_version") or "unknown",
                    profile_fingerprint(profile),
                    float(inputs.get("price") or 0.0),
                    score.get("value"),
                    score.get("verdict"),
                    1 if score.get("score_pinned") else 0,
                    1 if score.get("score_capped") else 0,
                    cost.get("piti"),
                    cost.get("front_end_dti"),
                    cost.get("cash_to_close"),
                    json.dumps(document.get("degraded_sources") or []),
                    json.dumps(document, sort_keys=True),
                ),
            )
            analysis_id = int(cursor.lastrowid or 0)
            # Two different numbers, and confusing them is how a demo reads wrong. `analysis_id`
            # is the global rowid -- stable, useful for linking, and meaningless next to an
            # address. `analysis_number` is how many times *this house* has been analyzed, which
            # is what "#2" means to a reader looking at one property. The first save of the
            # third house was printing "#3", which reads as its third analysis.
            analysis_number = int(
                self.conn.execute(
                    "SELECT COUNT(*) FROM analyses WHERE property_key = ?", (key,)
                ).fetchone()[0]
            )

        return {
            "property": self.get_property(key)["property"],
            "analysis_id": analysis_id,
            "analysis_number": analysis_number,
            "created": existing is None,
            "diff": self.diff(key),
        }

    def set_status(self, key: str, status: str, *, note: str | None = None) -> dict[str, Any]:
        """Move a house through the process, and journal the move.

        The journal entry is not optional and not a convenience. A status is a current value
        with no memory; "why did we pass on this one" is answerable only if the transition was
        recorded when it happened. Writing both in one transaction means there is no state
        where the status moved and the reason did not.
        """
        if status not in STATUSES:
            raise LedgerError(f"unknown status {status!r}; expected one of {', '.join(STATUSES)}")
        row = self.conn.execute(
            "SELECT status FROM properties WHERE key = ?", (key,)
        ).fetchone()
        if row is None:
            raise PropertyNotFound(key)

        previous = row["status"]
        now = _now()
        body = f"{previous} -> {status}"
        if note:
            body = f"{body}: {note}"

        with self.conn:
            self.conn.execute(
                "UPDATE properties SET status = ?, updated_at = ? WHERE key = ?",
                (status, now, key),
            )
            self.conn.execute(
                "INSERT INTO journal (property_key, created_at, kind, body, author) "
                "VALUES (?, ?, 'status', ?, ?)",
                (key, now, body, "ledger"),
            )
        return {"key": key, "previous": previous, "status": status}

    def add_journal_entry(
        self,
        *,
        kind: str,
        body: str,
        key: str | None = None,
        resolves: int | None = None,
        author: str | None = None,
    ) -> dict[str, Any]:
        """Append a journal entry. `resolves` closes the loop on an earlier one.

        An `outcome` that resolves an `assumption` is the whole reason `kind` exists. It lets
        the ledger answer a question no dashboard can: were the reasons you gave at the time
        the reasons it actually turned on.
        """
        if kind not in JOURNAL_KINDS:
            raise LedgerError(f"unknown kind {kind!r}; expected one of {', '.join(JOURNAL_KINDS)}")
        if not body.strip():
            raise LedgerError("a journal entry needs a body")
        if key is not None:
            if self.conn.execute("SELECT 1 FROM properties WHERE key = ?", (key,)).fetchone() is None:
                raise PropertyNotFound(key)
        if resolves is not None:
            target = self.conn.execute(
                "SELECT id, kind FROM journal WHERE id = ?", (resolves,)
            ).fetchone()
            if target is None:
                raise LedgerError(f"cannot resolve journal entry {resolves}: no such entry")
            # Resolving a status line or another outcome is almost always a mistake -- the
            # thing being closed should be a claim someone made.
            if target["kind"] not in ("assumption", "decision", "observation"):
                raise LedgerError(
                    f"entry {resolves} is a {target['kind']}; only an assumption, decision, "
                    "or observation can be resolved"
                )

        with self.conn:
            cursor = self.conn.execute(
                "INSERT INTO journal (property_key, created_at, kind, body, resolves, author) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (key, _now(), kind, body.strip(), resolves, author),
            )
        return self.journal_entry(int(cursor.lastrowid or 0))

    def forget_property(self, key: str) -> dict[str, Any]:
        """Delete a property and everything attached to it. For typos, not for decisions.

        Deliberately narrow: this refuses once the house has more than one analysis or any
        journal entry that is not an automatic status line. At that point you have thought
        about it, and the record of thinking about it is the asset. Use `passed` or `archived`
        instead -- a house you rejected is data.
        """
        row = self.conn.execute("SELECT key FROM properties WHERE key = ?", (key,)).fetchone()
        if row is None:
            raise PropertyNotFound(key)
        analyses = self.conn.execute(
            "SELECT COUNT(*) AS n FROM analyses WHERE property_key = ?", (key,)
        ).fetchone()["n"]
        substantive = self.conn.execute(
            "SELECT COUNT(*) AS n FROM journal WHERE property_key = ? AND kind != 'status'",
            (key,),
        ).fetchone()["n"]
        if analyses > 1 or substantive > 0:
            raise LedgerError(
                f"{key} has {analyses} analyses and {substantive} journal entries; "
                "set its status to 'passed' or 'archived' rather than erasing the record"
            )
        with self.conn:
            # The DELETE triggers on the child tables are guarded on the parent still
            # existing, so the parent goes first and the cascade runs unopposed.
            self.conn.execute("DELETE FROM properties WHERE key = ?", (key,))
        return {"key": key, "forgotten": True, "analyses_removed": analyses}

    # -- reads ----------------------------------------------------------------

    def list_properties(self, *, status: str | None = None, include_archived: bool = False) -> list[dict[str, Any]]:
        """Every saved house with a summary of its most recent analysis.

        One query with a correlated subquery for the latest analysis, rather than N+1 reads.
        Archived houses are excluded unless asked for -- the shortlist you look at daily
        should be the live one.
        """
        clauses: list[str] = []
        params: list[Any] = []
        if status is not None:
            if status not in STATUSES:
                raise LedgerError(f"unknown status {status!r}")
            clauses.append("p.status = ?")
            params.append(status)
        elif not include_archived:
            clauses.append("p.status != 'archived'")
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""

        rows = self.conn.execute(
            f"""
            SELECT p.*,
                   a.id AS latest_id, a.analyzed_at AS latest_at, a.price, a.score,
                   a.verdict, a.score_capped, a.piti, a.front_end_dti, a.cash_to_close,
                   a.engine_version, a.profile_fingerprint,
                   (SELECT COUNT(*) FROM analyses WHERE property_key = p.key) AS analysis_count,
                   (SELECT COUNT(*) FROM journal WHERE property_key = p.key) AS journal_count
            FROM properties p
            LEFT JOIN analyses a
              ON a.id = (SELECT id FROM analyses
                          WHERE property_key = p.key
                          ORDER BY analyzed_at DESC, id DESC LIMIT 1)
            {where}
            ORDER BY COALESCE(a.score, -1) DESC, p.key
            """,
            params,
        ).fetchall()
        return [self._summarize(row) for row in rows]

    def get_property(self, key: str) -> dict[str, Any]:
        """One house: its identity, its status, its latest analysis, and its diff."""
        row = self.conn.execute(
            """
            SELECT p.*,
                   a.id AS latest_id, a.analyzed_at AS latest_at, a.price, a.score,
                   a.verdict, a.score_capped, a.piti, a.front_end_dti, a.cash_to_close,
                   a.engine_version, a.profile_fingerprint,
                   (SELECT COUNT(*) FROM analyses WHERE property_key = p.key) AS analysis_count,
                   (SELECT COUNT(*) FROM journal WHERE property_key = p.key) AS journal_count
            FROM properties p
            LEFT JOIN analyses a
              ON a.id = (SELECT id FROM analyses
                          WHERE property_key = p.key
                          ORDER BY analyzed_at DESC, id DESC LIMIT 1)
            WHERE p.key = ?
            """,
            (key,),
        ).fetchone()
        if row is None:
            raise PropertyNotFound(key)
        return {
            "property": self._summarize(row),
            "history": self.history(key),
            "journal": self.journal(key=key),
            "diff": self.diff(key),
        }

    def latest_document(self, key: str) -> dict[str, Any]:
        """The full analysis document as it was stored, not recomputed.

        Recomputing on read would be a different answer under a newer engine, which is
        precisely the thing this package exists to avoid.
        """
        row = self.conn.execute(
            "SELECT document FROM analyses WHERE property_key = ? ORDER BY analyzed_at DESC, id DESC LIMIT 1",
            (key,),
        ).fetchone()
        if row is None:
            raise PropertyNotFound(key)
        return json.loads(row["document"])

    def history(self, key: str) -> list[dict[str, Any]]:
        """Every analysis of one house, oldest first. The price-change history is this list."""
        rows = self.conn.execute(
            """
            SELECT id, analyzed_at, engine_version, profile_fingerprint, price, score,
                   verdict, score_capped, piti, front_end_dti, cash_to_close, degraded_sources
            FROM analyses WHERE property_key = ? ORDER BY analyzed_at ASC, id ASC
            """,
            (key,),
        ).fetchall()
        return [
            {
                "id": r["id"],
                "analyzed_at": r["analyzed_at"],
                "engine_version": r["engine_version"],
                "profile_fingerprint": r["profile_fingerprint"],
                "price": r["price"],
                "score": r["score"],
                "verdict": r["verdict"],
                "score_capped": bool(r["score_capped"]),
                "piti": r["piti"],
                "front_end_dti": r["front_end_dti"],
                "cash_to_close": r["cash_to_close"],
                "degraded_sources": json.loads(r["degraded_sources"]),
            }
            for r in rows
        ]

    def diff(self, key: str) -> dict[str, Any] | None:
        """Compare the two most recent analyses -- and say *why* they differ.

        This is the honest part of the package. A score that fell four points has three
        possible causes and they mean completely different things:

        * the **price** moved, which is a market fact about the house;
        * the **engine** changed, which is a fact about your code;
        * your **profile** changed, which is a fact about you.

        A dashboard that shows "score -4" without distinguishing those is worse than one that
        shows nothing, because it invites a conclusion the data does not support. So the score
        delta is only reported as comparable when the engine version and the profile
        fingerprint both match; otherwise it is returned with `comparable: false` and the
        reason, and the caller is expected to say so.
        """
        rows = self.conn.execute(
            """
            SELECT id, analyzed_at, engine_version, profile_fingerprint, price, score, verdict
            FROM analyses WHERE property_key = ? ORDER BY analyzed_at DESC, id DESC LIMIT 2
            """,
            (key,),
        ).fetchall()
        if len(rows) < 2:
            return None
        new, old = rows[0], rows[1]

        engine_changed = new["engine_version"] != old["engine_version"]
        profile_changed = new["profile_fingerprint"] != old["profile_fingerprint"]
        reasons: list[str] = []
        if engine_changed:
            reasons.append(
                f"engine changed {old['engine_version']} -> {new['engine_version']}"
            )
        if profile_changed:
            reasons.append("buyer profile changed since the earlier analysis")

        price_delta = (new["price"] or 0.0) - (old["price"] or 0.0)
        score_delta = None
        if new["score"] is not None and old["score"] is not None:
            score_delta = new["score"] - old["score"]

        return {
            "from_id": old["id"],
            "to_id": new["id"],
            "from_at": old["analyzed_at"],
            "to_at": new["analyzed_at"],
            "price_delta": price_delta,
            "price_pct": (price_delta / old["price"] * 100.0) if old["price"] else None,
            "score_delta": score_delta,
            "verdict_changed": new["verdict"] != old["verdict"],
            "verdict_from": old["verdict"],
            "verdict_to": new["verdict"],
            "comparable": not (engine_changed or profile_changed),
            "incomparable_because": reasons,
        }

    def journal(self, *, key: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
        """Journal entries, newest first. `key=None` returns everything, including general entries."""
        if key is None:
            rows = self.conn.execute(
                "SELECT * FROM journal ORDER BY created_at DESC, id DESC LIMIT ?", (limit,)
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM journal WHERE property_key = ? ORDER BY created_at DESC, id DESC LIMIT ?",
                (key, limit),
            ).fetchall()
        return [self._journal_row(r) for r in rows]

    def journal_entry(self, entry_id: int) -> dict[str, Any]:
        row = self.conn.execute("SELECT * FROM journal WHERE id = ?", (entry_id,)).fetchone()
        if row is None:
            raise LedgerError(f"no journal entry {entry_id}")
        return self._journal_row(row)

    def open_assumptions(self) -> list[dict[str, Any]]:
        """Assumptions and decisions nothing has come back to close.

        The most useful view in the whole package and the cheapest to build: every claim you
        made and never checked. `NOT IN` over a nullable column would silently return nothing
        if any row had a NULL `resolves`, so this filters the subquery.
        """
        rows = self.conn.execute(
            """
            SELECT * FROM journal
            WHERE kind IN ('assumption', 'decision')
              AND id NOT IN (SELECT resolves FROM journal WHERE resolves IS NOT NULL)
            ORDER BY created_at ASC, id ASC
            """
        ).fetchall()
        return [self._journal_row(r) for r in rows]

    def stats(self) -> dict[str, Any]:
        counts = {
            row["status"]: row["n"]
            for row in self.conn.execute(
                "SELECT status, COUNT(*) AS n FROM properties GROUP BY status"
            ).fetchall()
        }
        return {
            "properties": sum(counts.values()),
            "by_status": counts,
            "analyses": self.conn.execute("SELECT COUNT(*) AS n FROM analyses").fetchone()["n"],
            "journal_entries": self.conn.execute("SELECT COUNT(*) AS n FROM journal").fetchone()["n"],
            "open_assumptions": len(self.open_assumptions()),
            "schema_version": self.conn.execute("PRAGMA user_version").fetchone()[0],
        }

    # -- shaping --------------------------------------------------------------

    @staticmethod
    def _summarize(row: sqlite3.Row) -> dict[str, Any]:
        latest = None
        if row["latest_id"] is not None:
            latest = {
                "id": row["latest_id"],
                "analyzed_at": row["latest_at"],
                "price": row["price"],
                "score": row["score"],
                "verdict": row["verdict"],
                "score_capped": bool(row["score_capped"]),
                "piti": row["piti"],
                "front_end_dti": row["front_end_dti"],
                "cash_to_close": row["cash_to_close"],
                "engine_version": row["engine_version"],
                "profile_fingerprint": row["profile_fingerprint"],
            }
        return {
            "key": row["key"],
            "raw_input": row["raw_input"],
            "matched_address": row["matched_address"],
            "resolved": bool(row["resolved"]),
            "latitude": row["latitude"],
            "longitude": row["longitude"],
            "county_fips": row["county_fips"],
            "status": row["status"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "analysis_count": row["analysis_count"],
            "journal_count": row["journal_count"],
            "latest": latest,
        }

    @staticmethod
    def _journal_row(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "property_key": row["property_key"],
            "created_at": row["created_at"],
            "kind": row["kind"],
            "body": row["body"],
            "resolves": row["resolves"],
            "author": row["author"],
        }


def rows_to_csv(rows: Iterable[dict[str, Any]]) -> str:
    """Flatten the shortlist to CSV for a spreadsheet, because that is where decisions get argued.

    Only the summary columns. Exporting the full document would produce a cell containing
    forty kilobytes of JSON, which no spreadsheet handles usefully.
    """
    import csv
    import io

    fields = [
        "key", "status", "matched_address", "resolved", "price", "score", "verdict",
        "score_capped", "piti", "front_end_dti", "cash_to_close", "analysis_count",
        "journal_count", "updated_at",
    ]
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=fields, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        flat = dict(row)
        flat.pop("latest", None)
        flat.update({k: v for k, v in (row.get("latest") or {}).items() if k in fields})
        writer.writerow(flat)
    return buffer.getvalue()
