# ADR 0009 — Hazard risk is a caveat, never a deduction

**Status:** Accepted
**Date:** 2026-08-19
**Supersedes:** nothing. Extends [ADR 0005](0005-capital-expenses-deduct-and-unknowns-pin.md) and [ADR 0006](0006-source-station-contract.md).

---

## Context

The roadmap asked for the [FEMA National Risk Index](https://www.fema.gov/flood-maps/products-tools/national-risk-index) at the census tract: wind, wildfire, heat and drought, plus a social-vulnerability and a community-resilience rating.

The obvious implementation is a scoring one. FEMA publishes five tidy rating labels per hazard — Very Low, Relatively Low, Relatively Moderate, Relatively High, Very High — which map so naturally onto a deduction table that writing one feels like the intended use. Wildfire "Very High" costs 20 points, "Relatively High" costs 10, and the score gets richer.

Three things found while querying the live layer say that is wrong.

### 1. The rating labels are not a shared scale

Ratings are binned **per hazard**, against that hazard's own national distribution. They are not comparable across hazards.

Sampling 2,000 California tracts off the live layer:

| Rating | Wildfire percentile range | Composite risk percentile range |
|---|---|---|
| Very Low | up to 68.5 | up to 20.1 |
| Relatively Moderate | 88.4 – 96.2 | 55.9 – 84.3 |

A "Relatively Moderate" wildfire rating and a "Relatively Moderate" heat rating describe positions roughly 30 percentile points apart. The census tract containing **Paradise, California** — destroyed by the 2018 Camp Fire — returns wildfire percentile **95.2** with a wildfire rating of **"Relatively Moderate"**.

A deduction table keyed on the label would have charged Paradise the mid-tier penalty. So would any table keyed on the composite, which for that same tract reads **32.3** and rates "Relatively Low", because the composite averages all 18 hazards and 15 of them do not apply in Butte County.

The sample was ordered by object ID rather than randomly, so those ranges are enough to prove the bins differ per hazard and not enough to publish national cut points. Both conclusions point the same direction: do not build points on top of the labels.

### 2. A zero is not good news

900 of those 2,000 tracts return a wildfire score of exactly `0.0` with rating `"No Rating"`. That means the hazard is **not modeled there**, not that it is absent. This is the same class of bug as the parcel station's early behaviour, where a missing record read as a clean record.

A deduction system handles this badly in a specific and dangerous way: a not-modeled hazard reads as 0, 0 is below every threshold, and the house is silently *credited* for a hazard nobody measured.

### 3. Nothing here is a property fact

Every existing deduction describes the house or the lot — 2.5 baths, an 18-year-old roof, no fiber. Hazard risk describes the **census tract**, which around here spans several square miles and thousands of homes. The tract cannot distinguish the house on the ridge from the house in the draw a quarter mile away. Deducting points from a specific address for a tract-wide average asserts a precision the data does not have, which is the thing [ADR 0006](0006-source-station-contract.md) exists to prevent.

There is also a cheaper argument. The financial consequence of hazard exposure is already quantified by someone whose job it is: the insurance premium. The right move is not to guess a points penalty, it is to go get a quote.

---

## Decision

**The risk station reports. It does not score.**

1. `RiskStation` provides exactly one fact, `hazard_profile`, and `PropertyFacts` has no field of that name. The fact travels on the analysis document for a human to read and is structurally invisible to the scoring engine.
2. **Percentiles are the value. Rating labels are a note.** Every hazard, and both indices, are stored as a `0-100` national percentile with `precision="national_percentile"`. FEMA's label is preserved in the `note` so the report can be reconciled against FEMA's own map, and nothing thresholds on it.
3. **Not modeled is `unavailable`, not zero.** A score of 0 paired with `"No Rating"`, `"Insufficient Data"`, `"Not Applicable"`, or an empty rating is recorded as unavailable with the note that unknown is not the same as low risk. It raises no caveat, because there is nothing to caveat.
4. **Caveats are non-blocking, always.** A hazard at or above the configured percentile — 90 by default — emits an advisory verification task pointing at an insurance quote. There are still exactly three hard fails: flood zone, well/septic, commute over 20 minutes.
5. **The composite is not a headline.** It is surfaced only when it understates a hazard that is itself at or above the 75th percentile by 25 or more points, and then the note says explicitly that the composite averages 18 hazards.
6. **Which hazards to report is profile configuration.** `[risk] hazards` in `buyer_profile.toml`, using FEMA's four-letter codes, validated at construction so a typo fails loudly instead of silently dropping a hazard.

## Consequences

**Good.**

- The score stays reproducible and every deduction stays traceable to a property fact. A score written to the decision journal in August still means the same thing in December, when FEMA reissues the index and every percentile shifts.
- The caveat channel stays credible. It fires for Paradise's wildfire and stays quiet about Spartanburg's hail.
- The California fork is a configuration change. Swapping `hazards = ["SWND","TRND","HAIL","WNTW","HWAV","DRGT","WFIR"]` for `["WFIR","ERQK","DRGT","HWAV","LNDS"]` reports a different hazard set with no code change, because the NRI is a national dataset and the parsing is hazard-agnostic. Fixtures for both a South Carolina and a California tract are committed.
- Two structural tests defend the boundary: one asserts `PropertyFacts` never gains a hazard field, and one asserts nothing in the reporting-only exemption list is simultaneously a scoring input.

**Bad, and accepted.**

- A house in a genuinely dangerous tract can still score TAKE. That is the honest outcome: the tool's answer is "this is affordable and convenient, and you need to price the wildfire risk before you offer," not a number that pretends to have already priced it.
- The report is longer and asks the user to think. Percentiles require more of a reader than a five-label scale does. The alternative is a scale that is confidently wrong about Paradise.
- Tract resolution is a real ceiling and no amount of presentation fixes it. Recorded in [KNOWN_LIMITATIONS](../KNOWN_LIMITATIONS.md).

## Reversing this

If hazard risk should later affect the score, the honest path is not a label-keyed deduction table. It is a **per-hazard percentile threshold with published national cut points**, derived from a proper random national sample rather than the 2,000-tract ordered slice used here, and applied only to hazards with `modeled=True`. That would require deleting `test_no_hazard_field_exists_on_the_scoring_input`, which is deliberate: the deletion is the conversation.

---

## Sources

- FEMA National Risk Index — <https://www.fema.gov/flood-maps/products-tools/national-risk-index>
- Live layer queried for every empirical claim above — <https://services.arcgis.com/XG15cJAlne2vxtgt/arcgis/rest/services/National_Risk_Index_Census_Tracts/FeatureServer/0>
- NFHL flood layer, the hard fail this station is deliberately kept separate from — <https://msc.fema.gov/portal/home>
