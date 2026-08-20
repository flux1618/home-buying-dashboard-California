# ADR 0010 — Inverse affordability returns two prices, and is bisected not derived

**Status:** Accepted
**Date:** 2026-08-19
**Extends:** [ADR 0002](0002-pure-scoring-core.md), [ADR 0008](0008-browser-rules-are-compiled-not-rewritten.md).

---

## Context

Every door in this project answered the forward question: *given this house at this price, what does it cost me and does it clear my DTI target?* That is the question you ask about a listing you already found.

It is not the question you ask while shopping. That one is inverse: *what is the highest number I can put in the price filter?*

Roadmap Phase 4 called for a max-price solver. Two decisions inside it were not obvious.

## Decision 1 — Bisect over `compute`, do not derive a formula

`solve_max_price` finds the answer by binary search over the real `cost.compute`, not by solving an equation for price.

An algebraic inverse is tempting because the dominant term is linear. It is also wrong, for reasons that only show up in this codebase:

- The **deed recording fee steps in $500 increments**, so cash-to-close is a staircase, not a line.
- `tax` branches on **assessment ratio** (4% owner-occupied vs 6% not) and on **millage district**.
- The **maintenance reserve** is three methods producing a range, and the household figure needs the conservative edge of it.

A closed form would have to restate all of that. The moment `tax.py` changes a millage constant, the formula becomes confidently wrong and nothing fails. Bisecting over `compute` cannot drift from `compute`, because it *is* `compute`.

The cost is about 40 evaluations per solve. `compute` is pure arithmetic with no I/O — see [ADR 0002](0002-pure-scoring-core.md) — so this is microseconds, and the endpoint needs no network at all.

Two properties of the bisection are deliberate rather than incidental:

- **It returns the low edge of the bracket.** Rounding a max price up hands back a number that breaches the ceiling. That is the one direction of error that costs money, so the solver never makes it.
- **The floor is the down payment, not zero.** Below the down payment you are paying cash for part of a house that does not exist. This is not a theoretical guard: the browser implementation initially bracketed from `0` and returned a "max price" of **$13,379** against an **$80,000** down payment. See Decision 3.

## Decision 2 — Return two prices, always

The solver returns `lender_max_price` and `household_max_price`.

| Figure | Includes | Answers |
|---|---|---|
| `lender_max_price` | PITI + HOA | What a pre-approval letter will show |
| `household_max_price` | PITI + HOA + `reserve_high` | What keeps the same share of gross going out the door after a year of ownership |

At a 22% ceiling on the committed profile these are **$1,046,433** and **$894,622** — a **$151,811** gap. Reporting only the first is technically accurate and practically misleading; reporting only the second answers a question no lender asked.

The household figure uses `reserve_high`, the conservative edge of the three-method range, **never an average**. Averaging the three methods would quietly raise the answer and hide the spread that [the maintenance module reports on purpose](../KNOWN_LIMITATIONS.md).

### Mortgage insurance is a property of the answer, not the input

The down payment is fixed while the solved price moves, so **a higher answer is automatically a lower down-payment percentage**. At the profile's $80,000 down, the 22% answer lands at 7.6% down.

Mortgage insurance is modeled nowhere in this project. So when the solved price puts the down payment under 20%, the solution carries a note saying MI would apply at roughly 0.3–1.5% of the loan per year, is excluded, and that the figure is therefore an **upper bound**. The warning is attached to the result rather than validated on the way in, because it is the solver's own output that creates the condition.

### Infeasible names the cause

Insurance and HOA do not scale down with the purchase price. A low enough ceiling therefore has no solution at any price, and the solver returns `feasible: false` with the floor DTI and an explanation — not `0`. Returning zero would imply "buy something cheaper", which is precisely the thing that does not fix it.

## Decision 3 — The browser gets its own solver, and a parity test

The page cannot call Python. Per [ADR 0008](0008-browser-rules-are-compiled-not-rewritten.md) the fix for duplicated logic is normally to compile it into the snapshot, but a bisection is behaviour, not a rule table, and there is nothing to compile.

So `app.js` gets `solveMaxPrice`, bisecting over `pitiParts` — which was **extracted from the existing KPI block** so the page's two readings of PITI are one function rather than a copy. `tests/test_max_price.py::TestTheTwoDoorsAgree` shells out to `node`, runs the browser solver against the same profile, and asserts the two land within $2 across three ceilings and agree on infeasibility.

That test earned its keep immediately: it is what surfaced the $13,379-vs-$80,000 floor bug in the JavaScript, which Python did not have. Both sides had returned a confident number and only one was right — the exact failure mode that a second implementation introduces and only a parity test catches.

The page's figure is the **lender basis only**. It does not have the maintenance reserve, so it says so and points at the CLI and API for the household number.

## Consequences

**Good**

- Four doors, one engine, and the two that solve this now provably agree.
- Answers instantly with every network source down, because it touches none of them.
- Surfaced a finding the forward calculator could not: at this income, DTI is nowhere near the binding constraint. The working target is $310,000 against a $1.05M lender ceiling. **Cash to close and the appraisal are the constraints.**

**Bad**

- The headline number is an upper bound, because MI is unmodeled. Documented in the note, in [KNOWN_LIMITATIONS](../KNOWN_LIMITATIONS.md), and on the page.
- One more piece of arithmetic exists in JavaScript. Mitigated by a parity test, not by hoping.
- `node` is required for four tests. They skip cleanly when it is absent rather than failing.

**Revisit if** MI gets modeled — the two prices become three, or the household figure absorbs it — or if the browser solver drifts often enough to justify compiling a lookup table into the snapshot instead.

## Sources

- FEMA and SC tax mechanics underpinning `compute` are cited in [ADR 0005](0005-capital-expenses-deduct-and-unknowns-pin.md) and [the customer brief](../CUSTOMER_BRIEF.md).
- Mortgage insurance range of 0.3–1.5% of the loan annually: [Consumer Financial Protection Bureau](https://www.consumerfinance.gov/ask-cfpb/what-is-private-mortgage-insurance-en-122/).
