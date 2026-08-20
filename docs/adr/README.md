# Architecture Decision Records

Short records of consequential choices: what was decided, what else was considered, and what it costs if the decision turns out to be wrong.

Format is deliberately lightweight — context, decision, alternatives, consequences. If a record can't state the cost of being wrong, the decision probably wasn't understood well enough to write down.

| # | Decision | Status |
|---|---|---|
| [0001](0001-static-snapshot-plus-local-container.md) | Public site is a committed snapshot; the API runs locally in a container | Accepted |
| [0002](0002-pure-scoring-core.md) | Scoring engine is a pure Python package with no framework or network | Accepted |
| [0003](0003-no-listing-or-mls-extraction.md) | No Zillow/Redfin extraction and no MLS integration | Accepted |
| [0004](0004-llm-scope-boundary.md) | LLMs do extraction only and never arithmetic | Accepted |
| [0005](0005-capital-expenses-deduct-and-unknowns-pin.md) | Aging roof/HVAC deduct with a dollar range; unknown hard-fail inputs pin the score to 50 | Accepted |
| [0006](0006-source-station-contract.md) | Every external source is a station with one failure contract: never raise, never guess | Accepted |
| [0007](0007-http-service-container-and-split-ci.md) | CLI, batch, and HTTP are three thin doors on one engine; CI splits offline-per-push from live-nightly | Accepted |
| [0008](0008-browser-rules-are-compiled-not-rewritten.md) | The static page is the fourth door; its scoring rules are compiled from the profile, never hand-written | Accepted |
| [0009](0009-hazard-risk-is-a-caveat.md) | FEMA National Risk Index reports percentiles as caveats and never deducts points; hazards are profile configuration | Accepted |
| [0010](0010-inverse-affordability-is-two-answers.md) | Max-price is bisected over the real cost engine, returns a lender and a household price, and the browser copy is held honest by a parity test | Accepted |
| [0011](0011-ledger-is-append-only-and-separate.md) | Saved properties live in a separate append-only SQLite ledger; a score delta is only reported when the engine version and profile fingerprint match | Accepted |
| [0012](0012-money-is-integer-cents-in-the-schedule.md) | The amortization schedule holds money as integer cents, closes the loan on its term month, and states in its payload that it excludes tax, insurance, HOA, and mortgage insurance | Accepted |
| [0013](0013-the-page-gets-its-own-schedule-under-a-parity-test.md) | The browser gets its own amortization loop because there is no rule table to compile; a zero-tolerance parity test checks all 360 rows against Python | Accepted |
| [0014](0014-extraction-is-refused-by-default-and-grounded-in-a-quote.md) | Document extraction refuses every field that is not declared, permitted, quoted from the text that was sent, and plausible; redaction is a type rather than a flag, and the default provider sends nothing anywhere | Accepted |
| [0015](0015-rate-sensitivity-is-scenarios-not-a-rate-forecast.md) | Rate sensitivity applies stated same-house scenarios through the existing cost engine and never forecasts rates or prices | Accepted |
| [0016](0016-keyless-fred-csv-fallback.md) | FRED's documented API is preferred when keyed; a bounded keyless graph-CSV snapshot is the explicit fallback | Accepted |
| [0017](0017-authoritative-bounded-parcel-snapshot.md) | County CAMA is primary; a bounded parcel index is committed rather than a full county binary refresh | Accepted |
| [0018](0018-published-aggregate-market-velocity-is-not-listing-extraction.md) | Bounded published aggregate market velocity is committed as context, not treated as listing or MLS data | Accepted |
| [0019](0019-school-data-measured-inferred-boundary.md) | School data keeps the boundary-to-name join, choice set, and HTML extraction visibly distinct from measurement | Accepted |
| [0020](0020-proposition-13-purchase-price-tax-model.md) | California tax starts from the buyer's purchase price and verified TRA, not the seller's frozen assessment | Accepted |
