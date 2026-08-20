# 0005 — Aging systems deduct with a dollar range, and unknowns pin the score to 50

**Status:** Accepted
**Date:** 2026-08-19
**Supersedes:** the caveat-only treatment of roof and HVAC age in [0002](0002-pure-scoring-core.md)'s original scoring table

## Context

The first scoring run over nine fixtures produced three results that failed review — and the review was the point of running it.

**A 1978 house with a 17-year roof and 14-year HVAC scored 100/TAKE.** Roof and HVAC age were caveat text: printed, never deducted. So the engine's top recommendation was a property carrying roughly $14,000–$28,000 of replacement work inside a few years. The score was arithmetically correct and practically wrong.

**A house missing several preferences scored 47 and landed in PASS.** 1,280 sqft, two baths, one-car garage, no fiber — a house worth walking through, filtered out because the watch floor sat at 50 and the deductions stacked to 53.

**A house with every hard-fail input unknown scored 100/WATCH.** The verdict was correctly downgraded, but the number next to it said perfect. Nothing had been verified. A 100 that means "we know nothing" is worse than no score at all, because the number is what gets skimmed.

The common failure across all three: **the engine's confidence did not track the engine's knowledge.**

## Decision

### 1. Capital expenses are their own scoring tier

Roof and HVAC age move out of caveats into a tier that deducts and reports money:

| Condition | Points | Urgency |
|---|---|---|
| Roof ≥ 15 yrs / ≥ 20 yrs | −25 / −35 | due / overdue |
| HVAC ≥ 12 yrs / ≥ 18 yrs | −15 / −22 | due / overdue |

Each item carries a cost band scaled by living area, sourced from South Carolina replacement data ([This Old House](https://www.thisoldhouse.com/roofing/roof-replacement-cost-south-carolina), [Modernize](https://modernize.com/roof/cost-calculator/south-carolina)) and home-size HVAC pricing ([USA Today](https://www.usatoday.com/story/money/home-services/hvac-replacement-cost/90313725007/)). Items reaching $10,000 are tagged `five_figure`; items reaching $5,000 make the contractor-quote task blocking. This is the explicit request: a near four- or five-figure expense must be *called out*, not just subtracted.

Roof plus HVAC both due is −40, which drops a 100 to a 60 — out of TAKE, into WATCH, exactly where a house with two replacements pending belongs.

**Build year remains a caveat only.** That rule is untouched. What deducts is a specific dated component, not the decade on the deed. A 1978 house with a three-year roof and new HVAC still scores 100.

### 2. Unevaluated hard fails pin the score to 50

If flood zone, water/sewer, or commute is *unknown* rather than *failing*: name every unknown, pin the score to 50, set `score_pinned`, force WATCH, and emit a follow-up task per unknown.

**The pin is one-directional — it can only lower.** This is a deliberate deviation from "fixed at 50." A house that earned 42 on its own merits keeps 42 rather than being raised. Raising a score because data is missing would let a dead API make a weak house look better, which is the exact failure mode this ADR exists to close. Lowering is safe; the pin still lands every plausible candidate in WATCH as intended.

### 3. The watch floor drops from 50 to 45

45–74 is WATCH. Below 45 is PASS. Soft misses stack faster than they deserve to; a house short on square footage and fiber is a negotiation, not a rejection.

## Alternatives considered

**Keep age as a caveat and trust the reader.** Rejected — the whole purpose of a score is to survive skimming. If the number says 100, the caveat does not get read.

**Deduct a fixed penalty with no dollar range.** Rejected — a $6,000 roof on a small ranch and a $24,000 roof on a large two-storey are not the same decision, and the dollar figure is what makes the number negotiable with a seller.

**Model the expense as a cash-flow adjustment instead of a score deduction.** Rejected for now. Amortising replacements into the monthly true cost is more honest financially, but it hides the decision inside a number already crowded with tax, insurance, and reserve. Deferred as a possible future addition alongside, not instead of.

**Fix the unknown score at exactly 50 in both directions.** Rejected — see above. One-directional pinning is the safer half of the request.

**Estimate component age from build year when age is unknown.** Rejected — that is a guess presented as a measurement, and it violates the provenance contract. Instead an unknown age on a house 12 years or older raises a caveat stating the score is optimistic by omission.

## Consequences

**Good.** The score now tracks knowledge: verified and sound is high, expensive is lower, unverified is exactly 50. Every capital expense arrives with a dollar range, a source, and a blocking task, which is what makes it actionable in a negotiation rather than a note in a margin.

**Cost.** Cost bands are statewide planning ranges banded by square footage, not quotes on a specific house — a real roof can fall outside them, and the output says so on every item. Four new thresholds and four new penalties are more configuration to justify, all of it exposed in `buyer_profile.toml` rather than buried in code. The pin's one-directional behaviour needs explaining every time someone reads "fixed at 50" and finds a 42.

**If this is wrong,** the symptom is watching good houses get filtered out over replacements that are cheap relative to purchase price, or watching everything on the market show up as WATCH because roof age is rarely published. The fix is threshold and penalty tuning in the profile, not code — which is why they live there.

Enforced by `tests/test_capex.py` and `tests/test_scoring.py::TestUnevaluatedHardFails`. The 1978 fixture is a regression test now: if it ever scores 100 again, the suite fails.
