"""Connection, location, and schema for the ledger database.

Three choices are made here and each one is a decision, not a default.

**SQLite, from the standard library, with no ORM.** The whole point of ADR 0001 is that this
tool runs as a container on a Raspberry Pi that Bao points a browser at. A single-file
database is one bind mount and one backup file. Postgres would mean a second container, a
second thing to keep running, and a second thing to restore -- for a dataset that will hold
tens of houses. `sqlite3` is stdlib, so the API extra stays as thin as it is today and the
arm64 image needs no new wheels.

**Schema versioning through `PRAGMA user_version`, migrations as ordered plain SQL.** Alembic
is the right answer for a schema that many people change. For one, a numbered list of
forward migrations applied inside a transaction is fewer moving parts and no dependency.
Every migration is idempotent-safe because the version gate runs first.

**Append-only is enforced by triggers.** `analyses` and `journal` reject UPDATE and DELETE at
the database level. A convention documented in a docstring survives exactly as long as the
next person who is in a hurry, and this is the one property the whole package is for: the
record of what the engine said when you made a decision must not be quietly editable. A
trigger makes the guarantee checkable from any client, including a `sqlite3` shell.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

# Read at call time, not import time. A module-level `os.environ` read means a test that
# sets the variable in a fixture has already lost -- the same import-time freezing bug that
# a default argument caused in the pipeline earlier.
DEFAULT_DB_ENV = "HBA_DATA_DIR"

SCHEMA_VERSION = 1

_FILENAME = "ledger.db"


def database_path(explicit: str | os.PathLike[str] | None = None) -> Path:
    """Where the ledger lives, in priority order: argument, env var, XDG data dir.

    The default is deliberately **outside the repository**. This file will contain real
    addresses of houses being considered and full financial analyses of them, and the
    repository is public. A default of `./ledger.db` would put it one forgotten `git add -A`
    away from being published -- and `git add -A` is exactly what gets typed at the end of a
    long session. `.gitignore` covers it too, because two guards cost nothing.
    """
    if explicit is not None:
        return Path(explicit).expanduser()

    env = os.environ.get(DEFAULT_DB_ENV)
    if env:
        return Path(env).expanduser() / _FILENAME

    # XDG_DATA_HOME rather than a dotfile in $HOME, so it lands somewhere a backup tool is
    # already looking.
    xdg = os.environ.get("XDG_DATA_HOME")
    base = Path(xdg).expanduser() if xdg else Path.home() / ".local" / "share"
    return base / "home-buying-analyzer" / _FILENAME


def connect(path: str | os.PathLike[str] | None = None, *, create_dirs: bool = True) -> sqlite3.Connection:
    """Open a connection with the pragmas this schema assumes, and migrate it.

    `:memory:` is passed through untouched, which is what the test suite uses. Every test
    gets its own empty database and none of them can reach the real one.
    """
    target = ":memory:" if str(path) == ":memory:" else database_path(path)
    if target != ":memory:" and create_dirs:
        Path(target).parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(target))
    conn.row_factory = sqlite3.Row

    # Foreign keys are OFF by default in SQLite -- a historical compatibility default, not a
    # recommendation. Without this, `analyses.property_key` would be a comment.
    conn.execute("PRAGMA foreign_keys = ON")
    # WAL survives an unclean container stop better than the default rollback journal, and
    # this database is written from one process at a time.
    if target != ":memory:":
        conn.execute("PRAGMA journal_mode = WAL")

    migrate(conn)
    return conn


def migrate(conn: sqlite3.Connection) -> int:
    """Apply every migration newer than the file's `user_version`. Returns the new version.

    Wrapped in one transaction per migration so a failure leaves the version behind rather
    than half-applied. Returning the version makes "did anything happen" testable.
    """
    current = conn.execute("PRAGMA user_version").fetchone()[0]
    for version, statements in enumerate(_MIGRATIONS, start=1):
        if version <= current:
            continue
        with conn:
            for statement in statements:
                conn.execute(statement)
            # PRAGMA does not accept a bound parameter, hence the f-string. `version` is a
            # loop index over a literal list, never caller input.
            conn.execute(f"PRAGMA user_version = {version}")
    return conn.execute("PRAGMA user_version").fetchone()[0]


# =============================================================================
# Migrations
# =============================================================================
#
# Append only. Never edit a statement below once it has shipped -- an existing database has
# already run it, so an edit produces two different schemas that both claim the same
# version. That is the failure mode migrations exist to prevent.

_M001 = (
    # -------------------------------------------------------------------------
    # properties -- the identity of a house, and the only mutable table
    # -------------------------------------------------------------------------
    #
    # Mutable in exactly one respect: `status`. Where a house sits in your process is a
    # current fact, not an observation, and keeping a status *history* here would duplicate
    # what the journal already records. Every status change writes a journal entry, so the
    # audit trail exists without a second history table.
    """
    CREATE TABLE IF NOT EXISTS properties (
        key             TEXT PRIMARY KEY,
        raw_input       TEXT NOT NULL,
        matched_address TEXT,
        -- 0 when the geocoder was degraded and the key came from raw input. Unresolved
        -- rows are never auto-merged with resolved ones; see ledger/repo.py.
        resolved        INTEGER NOT NULL DEFAULT 0 CHECK (resolved IN (0, 1)),
        latitude        REAL,
        longitude       REAL,
        county_fips     TEXT,
        status          TEXT NOT NULL DEFAULT 'candidate',
        created_at      TEXT NOT NULL,
        updated_at      TEXT NOT NULL
    )
    """,
    # -------------------------------------------------------------------------
    # analyses -- append-only observations
    # -------------------------------------------------------------------------
    #
    # The full document is stored as JSON *and* a handful of values are lifted into columns.
    # That is deliberate duplication: the columns exist so listing 40 houses does not mean
    # parsing 40 documents, and the document exists because a column list is a guess about
    # which fields will matter later and the guess is always wrong. The columns are derived
    # from the document at write time and are never the source of truth.
    #
    # `engine_version` and `profile_fingerprint` are the reason this table is worth keeping.
    # A score is only comparable to another score computed by the same engine under the same
    # household assumptions. Without these two columns, a score delta is uninterpretable.
    """
    CREATE TABLE IF NOT EXISTS analyses (
        id                  INTEGER PRIMARY KEY AUTOINCREMENT,
        property_key        TEXT NOT NULL REFERENCES properties(key) ON DELETE CASCADE,
        analyzed_at         TEXT NOT NULL,
        engine_version      TEXT NOT NULL,
        profile_fingerprint TEXT NOT NULL,
        price               REAL NOT NULL,
        score               INTEGER,
        verdict             TEXT,
        score_pinned        INTEGER NOT NULL DEFAULT 0,
        score_capped        INTEGER NOT NULL DEFAULT 0,
        piti                REAL,
        front_end_dti       REAL,
        cash_to_close       REAL,
        degraded_sources    TEXT NOT NULL DEFAULT '[]',
        document            TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_analyses_property ON analyses(property_key, analyzed_at)",
    # -------------------------------------------------------------------------
    # journal -- append-only decisions, and the assumptions behind them
    # -------------------------------------------------------------------------
    #
    # `kind` and `resolves` are what separate this from a notes field. An assumption written
    # down at the time can later be closed by an outcome entry that points back at it, which
    # is how you find out whether your reasoning was good or you got lucky. A free-text notes
    # column cannot answer that question.
    #
    # `property_key` is nullable: entries about the market or the plan belong here too and
    # are not about one house.
    """
    CREATE TABLE IF NOT EXISTS journal (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        property_key TEXT REFERENCES properties(key) ON DELETE CASCADE,
        created_at   TEXT NOT NULL,
        kind         TEXT NOT NULL,
        body         TEXT NOT NULL,
        resolves     INTEGER REFERENCES journal(id),
        author       TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_journal_property ON journal(property_key, created_at)",
    # -------------------------------------------------------------------------
    # Append-only, enforced
    # -------------------------------------------------------------------------
    #
    # RAISE(ABORT) rather than IGNORE. A silently dropped UPDATE is worse than an error: the
    # caller believes it succeeded. The message names the table and the alternative so
    # whoever hits it knows what to do instead of reaching for a workaround.
    """
    CREATE TRIGGER IF NOT EXISTS analyses_are_append_only
    BEFORE UPDATE ON analyses
    BEGIN
        SELECT RAISE(ABORT, 'analyses is append-only: record a new analysis instead of editing one');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS analyses_are_not_deletable
    BEFORE DELETE ON analyses
    WHEN (SELECT COUNT(*) FROM properties WHERE key = OLD.property_key) > 0
    BEGIN
        SELECT RAISE(ABORT, 'analyses is append-only: archive the property instead of deleting its history');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS journal_is_append_only
    BEFORE UPDATE ON journal
    BEGIN
        SELECT RAISE(ABORT, 'journal is append-only: add a correcting entry instead of editing one');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS journal_is_not_deletable
    BEFORE DELETE ON journal
    WHEN (SELECT COUNT(*) FROM properties WHERE key = OLD.property_key) > 0
    BEGIN
        SELECT RAISE(ABORT, 'journal is append-only: add a correcting entry instead of deleting one');
    END
    """,
)

# The DELETE triggers are conditional on the parent property still existing. Without that
# guard, ON DELETE CASCADE from `properties` would trip its own trigger and make deleting a
# property impossible -- which sounds like a feature until you consider a mistyped address
# saved once with no decisions attached to it. Removing a house you never really looked at is
# legitimate; silently rewriting the history of one you did is not.

_MIGRATIONS: tuple[tuple[str, ...], ...] = (_M001,)
