# ADR 0011 — Saved properties live in a separate append-only SQLite ledger, not in the engine

**Status:** Accepted · 2026-08-19

## Context

Everything before this decision was stateless. You hand the engine an address and a price, it
hands back a document, and nothing remembers the exchange happened. That is the right shape for
the arithmetic and the wrong shape for the actual job, which is shopping for a house over
roughly a year: which houses are we still looking at, what did this one cost in March, what did
we assume about the roof and did that turn out to be true.

Three things made the design non-obvious.

**The engine is pure, and that purity is load-bearing.** ADR 0002 says nothing in `core/`
performs I/O, and ADR 0007 says the CLI, batch runner, HTTP service, and static page are thin
doors onto one engine. Storage is I/O. Put a database call anywhere inside the engine and both
of those claims quietly become marketing.

**A saved analysis is not a fact, it is an observation.** The same house scored 74 in March and
68 in June. That is not a correction to a stored value — both scores were correct when they were
computed. Worse, a score can move for three unrelated reasons: the price changed (a fact about
the market), the engine changed (a fact about our code), or the buyer profile changed (a fact
about us). A schema that overwrites the old score destroys the ability to tell those apart, and
telling them apart is the entire value of keeping history.

**This tool is meant to be forked.** There is a planned California fork for a friend. Anything
Spartanburg-specific baked into storage columns becomes a merge conflict in someone else's repo.

## Decision

A new top-level package, `ledger/`, containing three tables and nothing clever.

```
ledger/
  db.py     schema, migrations, path resolution, connection setup
  repo.py   the Ledger class — the only sanctioned way to write
  cli.py    a fifth door: python -m ledger.cli
```

**1. `ledger/` is its own top-level package. `analyzer/` does not import it.**

The dependency runs one way only. The doors — `analyzer/cli.py --save`, the `/ledger/*` HTTP
endpoints — import both and wire them together. A test asserts this by reading the engine's
source and looking for the import, because a purity claim that is not checked is a comment.

**2. The record is append-only, enforced by the database.**

`analyses` and `journal` have `RAISE(ABORT)` triggers on UPDATE and DELETE. Not `RAISE(IGNORE)`,
which would swallow the write and report success. Not application-level convention, because the
guarantee has to hold for `sqlite3 ledger.db` at a shell prompt too — that is what makes it a
guarantee rather than a habit. Re-analyzing a house appends a row; price-change history is not a
feature, it is a query over rows that were already there.

**3. Store the whole document as JSON, and lift a few columns from it at write time.**

The document is the truth and it round-trips byte-for-byte. `price`, `score`, `verdict`, `piti`,
`dti`, `cash_to_close`, and `degraded_sources` are derived from that document inside
`save_analysis`, so no caller can pass a price that disagrees with the analysis it came from.
The columns exist to make sorting and filtering possible without parsing every blob; they are
an index, not a second source of truth.

This is also what keeps the California fork clean: storage handles documents opaquely. Nothing
in the schema knows what Act 388 is.

**4. `engine_version` and `profile_fingerprint` are stored on every analysis, and a delta is
only reported when both match.**

The fingerprint is `sha256(dataclasses.asdict(profile))[:12]`. `diff()` returns
`comparable: false` with an `incomparable_because` string when either differs, and every door
that displays a score delta honours it. A price delta is still reported in that case, because a
price is a fact about the world and does not care which version of our code observed it.

**5. Identity is the geocoder's `matched_address`, shallowly normalized, and unresolved
addresses get their own namespace.**

Normalization is upper-case, strip punctuation, collapse whitespace — deliberately not an
address parser. A half-built parser is worse than none, because the failure mode is silently
merging two different houses on the same street. When geocoding degraded, the key carries an
`unresolved:` prefix so a guess can never collide with a resolved address.

**6. `properties` is the only mutable table, and only its `status` column.**

Every status change writes a journal entry in the same transaction. There is no state where the
status moved and the reason for moving it does not exist.

**7. `forget_property` refuses once there is more than one analysis or any non-status journal
entry.** It exists to delete a typo, not to delete a decision. A house you rejected is data;
`passed` and `archived` are the answers.

**8. The database defaults to a path outside the repository** — `$HBA_DATA_DIR`, else the XDG
data directory. `./ledger.db` would put real addresses and real financial analyses one
`git add -A` away from a public commit. `.gitignore` covers it anyway, as a second layer.

**9. `/analyze` keeps working with no database at all.** Storage is a door, not a dependency.
`analyzer/cli.py` imports `ledger` inside the `--save` branch, so the stdlib-only install path
never touches it.

## Alternatives considered

| Option | Why not |
|---|---|
| PostgreSQL, or Postgres + PostGIS | A second container, a connection string, and a backup story, in exchange for concurrency and spatial queries this workload does not have. One person, a few hundred rows, and the spatial work already happens upstream in the county's own GIS. SQLite is a file you can copy, and `sqlite3 ledger.db .dump` is a plain-text backup. |
| A module inside `analyzer/` | Convenient imports, and the purity boundary in ADR 0002 becomes unverifiable the first time someone reaches for the connection from inside a scoring function. |
| JSON files on disk, one per property | Genuinely tempting — no schema, diffable in git. Falls apart on "show me every candidate over 70 sorted by DTI", and worse, the git-diffable property invites committing them. |
| Mutable rows with an `updated_at` | Simplest schema, and it silently answers "what did this house score in March" with June's number. |
| A general revision/event-sourcing layer | The right answer at a scale this will never reach. Three tables and two triggers do the job. |
| Storing only the derived columns | Half the size, and every future field means re-analyzing every house because the raw document is gone. |
| A real address parser (usaddress, libpostal) | A dependency and a wheel-building problem on arm64, to solve a matching problem the geocoder already solves better by returning a canonical string. |

## Consequences

**Good.** The history is trustworthy: no row that was ever written can be edited by any client,
including a careless script. A score delta is either meaningful or explicitly labelled as
meaningless, which is a stronger claim than most dashboards make about their own numbers. The
engine stays pure and provably so. Storage adds no runtime dependency — `sqlite3` ships with
CPython, so the arm64 image gains nothing to compile. The decision journal makes the process
reviewable after the fact, which is the point: a good outcome from a bad process is luck.

**Bad.** Append-only means mistakes are permanent and get corrected by adding rows, which reads
as clutter until you need it. Shallow normalization means "606 Andre Ct" and "606 Andre Court"
are two properties if the geocoder is down for one of them, and because the table is append-only
there is no merge path — the remedy is to re-analyze once geocoding works and delete the stub
while it still qualifies as a typo. Single-user with no authentication: anyone who can reach the
API can read the ledger, which is why compose binds to `127.0.0.1` only. And a document stored
today may be missing fields the engine adds tomorrow, so every reader of a historical document
has to tolerate absence.
