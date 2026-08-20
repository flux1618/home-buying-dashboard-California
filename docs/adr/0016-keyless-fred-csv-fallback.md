# 0016 — Use FRED's keyless graph CSV as the default mortgage-rate path

**Status:** Accepted
**Date:** 2026-08-19
**Supersedes:** nothing
**Related:** [0001](0001-static-snapshot-plus-local-container.md) (committed snapshots), [0006](0006-source-station-contract.md) (source failure contract)

## Context

The rate-sensitivity work needs a weekly baseline for the Freddie Mac 30-year fixed
mortgage average, published in FRED as
[`MORTGAGE30US`](https://fred.stlouisfed.org/series/MORTGAGE30US). Freddie Mac publishes
the PMMS reading on Thursdays, so a snapshot can be refreshed weekly without pretending it
is a real-time borrower quote.

FRED's documented
[series observations API](https://fred.stlouisfed.org/docs/api/fred/series_observations.html)
requires an API key. The same series is currently returned, without a key, by the graph
CSV endpoint:
[`https://fred.stlouisfed.org/graph/fredgraph.csv?id=MORTGAGE30US`](https://fred.stlouisfed.org/graph/fredgraph.csv?id=MORTGAGE30US).
That endpoint is useful but is not the documented API contract. Treating its current
availability as a permanent promise would recreate the silent-source failure ADR 0006 was
written to prevent.

## Decision

The weekly builder stores a bounded, committed
[`data/mortgage30us.json`](../../data/mortgage30us.json) snapshot. The default path is the
keyless graph CSV so a clean checkout can refresh it without a secret. It parses the CSV's
literal `.` missing-value marker as unknown, never as zero, and rejects a changed header or
empty usable series.

When `FRED_API_KEY` exists, the collector prefers the documented observations API. If that
call fails, it falls back to the graph CSV rather than letting a bad or expired key stop
the public weekly data. The snapshot and `Value` provenance identify which path actually
answered; the API key is never written into either.

The latest rate remains `measured` because it is a direct FRED observation. Its note names
the Thursday observation date and explicitly says `STALE` when that date is more than ten
days old. High/low, four-week movement, and the five-year percentile are `derived`.
`--check` makes malformed, drifted, or stale committed data fail CI without making CI
dependent on FRED answering at check time.

## Alternatives considered

### Require a FRED API key

This uses only the documented contract, but turns a harmless public weekly baseline into
an account-setup dependency. It also makes a new clone unable to refresh the snapshot and
does not solve a revoked key during scheduled automation.

### Query Freddie Mac directly

The underlying PMMS source is more direct, but the task needs the FRED series identifier
as the stable integration boundary and its observations history for five-year context.
Changing to a Freddie Mac integration later is possible, but it needs a separate parser,
provenance decision, and contract test.

### Fetch on every property analysis

The rate is national weekly data, not an address fact. Re-requesting it for every
analysis would multiply dependency failures, make results less reproducible, and add no
precision. The snapshot is therefore collected separately and is not registered in the
property-analysis pipeline.

## Consequences

The dashboard gains a small, reviewable weekly baseline with a stale-data alarm, and users
can refresh it without a secret. The cost is an explicit dependency on an undocumented CSV
shape: FRED can change or retire it without the compatibility expectation of its API.

The parser, opt-in live contract test, and Thursday automation make that risk visible;
they cannot remove it. If the CSV stops answering and no API key is configured, the builder
fails rather than inventing a rate or preserving an old observation as current.
