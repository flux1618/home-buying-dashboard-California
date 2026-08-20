# ADR 0002 — Scoring engine is a pure Python package with no framework or network

**Status:** Accepted · 2026-08-19

## Context

ADR 0001 commits to two runtimes sharing one engine. That only works if the engine has no opinion about how it's invoked. Separately, the financial math is the part that must be right — a wrong commute estimate is annoying, a wrong tax calculation is expensive — and math tangled up with HTTP calls is hard to test.

## Decision

```
analyzer/
  core/        pure functions. no I/O, no network, no framework.
               ownership cost, tax ratio, scoring, maintenance reserve.
  sources/     one adapter per upstream API. each independently failable.
               returns Provenance-wrapped values, never bare numbers.
  cli.py       wrapper 1 → writes the committed artifact (Tier A)
  api.py       wrapper 2 → FastAPI, thin (Tier B)
```

Rules that make it hold:

1. **Nothing in `core/` imports `requests`, `httpx`, or `fastapi`.** Enforced by an import-linter check in CI, not by good intentions.
2. **`core/` functions take plain values and return plain values.** Given the same inputs they produce the same outputs, forever.
3. **Every `sources/` adapter returns a provenance-wrapped value** — `{value, source_url, retrieved_at, confidence}` — so no number can enter the system without knowing where it came from.
4. **Each adapter fails independently.** A dead router degrades the commute field; it does not fail the analysis.

## Alternatives considered

| Option | Why not |
|---|---|
| FastAPI app with logic in route handlers | Fastest to a first result, but the math becomes untestable without spinning up a server, and Tier A would need its own copy. |
| Keep everything in JavaScript in `app.js` | Preserves the zero-dependency story, but county GIS and FCC lookups can't run in a browser, and the math would exist twice. |
| Notebook-driven analysis | Fine for exploring, unusable as a product. No tests, no reuse. |

## Consequences

**Good.** The financial math is unit-testable with no network and no fixtures — the highest-value tests are also the cheapest to write. Adding a source means adding one adapter. Swapping the routing provider touches one file. Both runtimes stay honest because neither owns any logic.

**Bad.** More upfront structure than a single-file script. The `Provenance` wrapper makes call sites noisier than passing raw floats.

**Cost of being wrong.** Very low. If the layering turns out to be over-engineered for a project this size, collapsing it later is mechanical. Going the other direction — untangling math from HTTP after the fact — is not.

**Test priority, in order.** Assessment-ratio math first (most expensive to get wrong), then DTI and PITI, then scoring, then maintenance reserve, then adapter contract tests against recorded fixtures.
