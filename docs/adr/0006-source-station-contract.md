# 0006 — Every external source is a station with the same failure contract

**Status:** Accepted
**Date:** 2026-08-19
**Supersedes:** nothing
**Related:** [0002](0002-pure-scoring-core.md) (pure core), [0005](0005-capital-expenses-deduct-and-unknowns-pin.md) (unknown hard-fail inputs pin the score)

## Context

The scoring engine is pure and stdlib-only by ADR 0002. Something still has to fetch the
facts it scores, and the six sources this project needs behave nothing alike:

| Station | Source | Behaviour observed in practice |
|---|---|---|
| **G**eocode | [US Census geocoder](https://geocoding.geo.census.gov/geocoder/) | Reliable, but silently returns zero matches for new construction |
| **A**ssessor / parcel | Spartanburg County ArcGIS | Serves an **incomplete TLS certificate chain**; every request fails `SSL: CERTIFICATE_VERIFY_FAILED` |
| **F**lood | [FEMA NFHL](https://hazards.fema.gov/femaportal/NFHL/) | Works; returns zero features for unmapped areas, which is not the same as "safe" |
| **C**ommute | [OSRM demo server](https://router.project-osrm.org/) | Works; free-flow only, no traffic model, rate-limited |
| **B**roadband | [FCC National Broadband Map](https://broadbandmap.fcc.gov/) | Requires an API key; **no anonymous tier**; returns HTTP 401 without one |
| **S**chools / hazard extras | FEMA National Risk Index | Endpoint unreachable at time of writing |

Two of six are degraded or unavailable on any given day. A design that assumes success
would have produced a confident report about a house using no county data at all.

The naive alternative — `try/except` around each call, defaults filled in — is how you get
a report that says "no fiber available" because a government API returned 401. That is
worse than no report, because it looks like an answer.

## Decision

Every external source is a `Station` subclass obeying one contract:

1. **A station never raises.** `Station.run()` catches `SourceUnavailable`,
   `SourceRejected`, `LookupError`, `TypeError`, and `ValueError` and converts each into a
   `Degradation` carrying a human-readable reason. The pipeline cannot be crashed by a
   remote server.
2. **A station never guesses.** A failed or empty fetch yields `None` — never a default,
   never a zero, never `False`. `None` means unknown, and unknown is scored differently
   from bad (ADR 0005).
3. **A station declares what it provides.** `provides` lists the `PropertyFacts` fields it
   is responsible for. Writing anything else raises `AssertionError` in-process, and a
   degradation reports exactly which facts were lost in `missing`.
4. **One station is fatal, the rest are optional.** Geocode failure aborts the run with
   `PipelineAborted`, because scoring a house whose location is unknown is meaningless.
   Every other failure lowers confidence and continues.
5. **Every value carries provenance.** `Value(value, confidence, source_url, note,
   precision)` — `measured`, `estimated`, `derived`, or `unavailable`. The confidence label
   is part of the data, not a formatting choice made at render time.

### Sub-decisions worth recording

**Stale data is an estimate, not a measurement.** The county's own server is unreachable, so
the parcel station falls back to a public ArcGIS mirror of the **February 2021** Parcel and
CAMA extract. Those values are labelled `estimated` with the vintage in the note, and the
fallback always emits a **blocking** task to pull the current parcel card by hand. A
five-year-old snapshot of a currently-true field is still an estimate.

**The geocoded point is buffered by 40 metres before querying parcels.** The Census
geocoder interpolates along the street centreline, so the returned point for 606 Andre Ct
sits roughly 3×10⁻⁵ degrees outside the parcel polygon — a strict point-in-polygon query
found nothing for a house that plainly exists. The buffer fixes that, and creates a new
problem: on a suburban street it can return several neighbouring lots. Disambiguation is by
street number, and when no candidate matches, the chosen record is flagged with a
**blocking** task naming the alternatives. Attaching a neighbour's bedroom count to your
house silently is the failure mode being designed against.

**Garage bay count is never inferred.** The CAMA `Garage` field takes exactly six values —
`CARPORT ATT`, `CARPORT DET`, `GARAGE ATT`, `GARAGE DET`, `GARAGEBSMT`, `NONE`. No count
exists in the dataset, so `garage_spaces` stays a user input. Reading "GARAGE ATT" as two
bays would be inventing data to satisfy a scoring rule.

**A missing API key is a degradation, not a zero.** Without `FCC_API_KEY` the broadband
station reports itself degraded and writes no facts at all. It never reports "no fiber",
which would deduct 15 points from every house in the county. Even with a key, broadband
values are marked `precision="census_block"` with a permanent blocking task to phone the
ISP with the exact street address, because Broadband Data Collection filings are
provider-reported claims of *availability* — a provider must connect a claimed location
within 10 business days of a request, which is a legal obligation, not a fact about wiring
already in the ground.

**Tests never touch the network; fixtures are recorded, never written.** `tools/record_fixtures.py`
captures real responses into `tests/fixtures/responses/`. The offline suite runs in under a
second with sockets blocked by an autouse fixture. Separately, `tests/test_live_sources.py`
is marked `live`, excluded from the default run, and asserts the real endpoints still
return the fields the parsers read. A field rename at the county shows up as a live-test
failure — news about the world — without ever reddening the normal suite.

## Alternatives considered

**A single fetch function with `try/except` per source.** Less code today. Rejected because
the failure policy ends up scattered across six call sites, and the fifth one written at
11pm is the one that returns `False` instead of `None`.

**A third-party property-data API.** One vendor, one key, clean JSON. Rejected: it costs
money, it hides where numbers come from, and reproducing this project would then require
someone else's paid account. Public data with visible provenance is the point.

**Disabling TLS verification to reach the county server.** Two lines and the authoritative
source works. Rejected — that is precisely the shortcut a threat model exists to forbid.
The stale mirror plus a blocking manual-verification task is honest; a silently
unverified connection is not.

**Caching in a database.** Rejected as premature. A 7-day file cache under
`~/.cache/home-buying-dashboard` keyed by URL is enough for a tool analysing a handful of
houses a week, and it keeps the repo dependency-free.

## Consequences

**Good**

- The pipeline produces a usable report with two of six sources down, and says exactly
  which two and what they cost.
- Every number can be traced to a source URL and a confidence label.
- The offline suite runs in ~0.5s, so tests are cheap enough to run constantly.
- Adding a source is a subclass with a `fetch` method — no orchestrator changes.

**Bad**

- More ceremony than a direct fetch. Roughly 200 extra lines of contract for six stations.
- Parcel data is up to five years stale until the county fixes its certificate chain, so
  every report carries a manual-verification task that a working county server would remove.
- The buffered parcel query can pick the wrong lot on dense streets. It is flagged, not
  solved, and the flag depends on the address containing a street number.
- Broadband is unavailable to anyone without an FCC key, which makes fiber effectively a
  manual input for most users.

**Cost if wrong**

If the contract is too rigid — if some future source genuinely needs to raise — the escape
hatch is a station that marks itself `fatal`. That already exists for geocode. The real
risk is the opposite: that `None`-means-unknown discipline erodes, one default at a time,
until the report is confidently wrong. `tests/test_sources.py` guards each specific case
(unmapped flood zone, blank utilities, missing API key, no FCC filing) precisely because
those are the erosions that would be invisible in output review.
