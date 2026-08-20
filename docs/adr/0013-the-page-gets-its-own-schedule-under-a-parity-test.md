# ADR 0013 — The page gets its own schedule, under a zero-tolerance parity test

**Date:** 2026-08-19
**Status:** Accepted
**Supersedes nothing. Extends:** [ADR 0008](0008-browser-rules-are-compiled-not-rewritten.md), [ADR 0012](0012-money-is-integer-cents-in-the-schedule.md)

## Context

The amortization schedule shipped as a Python module with two doors: a CLI flag and an HTTP
endpoint. Neither is visible to anyone who opens the site. The most legible output this
project produces — the point where a payment stops being mostly interest — required a
terminal to see.

[ADR 0008](0008-browser-rules-are-compiled-not-rewritten.md) says duplicated logic in the
browser is not allowed: scoring rules are computed in Python and compiled into `data.json`,
and the page reads values rather than reimplementing rules. That decision exists because a
rule expressed twice drifts, and a drifted rule on a public page is a wrong answer nobody
audits.

A schedule does not fit that pattern. It is not a table of rule outputs, it is a loop whose
input is three slider positions. Precomputing it would mean compiling a schedule for every
combination of price, down payment, rate and extra payment — combinatorially absurd, and
the sliders would stop being live. There is nothing to compile.

## Decision

**`app.js` gets its own `amortize()`, and `tests/test_amortization_parity.py` compares it to
the Python module row for row with zero tolerance.**

This is the same arrangement already in place for `solveMaxPrice`, which is also a solver
rather than a rule table, and which is also checked against Python under `node`. That
precedent is what makes this consistent with ADR 0008 rather than an exception to it. The
rule ADR 0008 actually encodes is *no unaudited second implementation*. A parity test is the
audit.

### The tolerance is zero, and that is the substance of this ADR

Not "within a dollar", not a percentage. Every one of the 360 rows must match to the cent
across interest, principal, extra and closing balance, plus the annual rollup, across nine
parameter sets including a 0% rate, a 15-year term, a 30%-APR twelve-month loan, and a rate
that divides cleanly enough for a half-cent to actually occur.

That is defensible only because both sides do the same arithmetic on purpose:

- Money is integer cents on both sides ([ADR 0012](0012-money-is-integer-cents-in-the-schedule.md)).
- Python rounds with `Decimal` and `ROUND_HALF_UP`. JavaScript's `Math.round` is half-up for
  positive numbers, and money here is always positive. They agree by construction.
- The final scheduled payment is the balance plus interest on both sides, so the browser
  cannot reintroduce the 361st payment.

So a one-cent difference is not tolerable noise, it is evidence that the construction broke.
A loose tolerance would hide precisely the bug this file exists to find. The failure mode is
silent: both implementations return a complete, internally consistent schedule, nothing on
the page looks wrong, and only one of them is right.

### Consequences accepted

- **The parity test skips when `node` is absent.** It is a real gap and it is shared with the
  four existing max-price parity tests. CI has node, so the gap is local only.
- **Chart.js is fed 360 points per dataset.** Measured fine, including on a 375px viewport.
  No downsampling, because downsampling the curve is another place for the two views to
  disagree about where the crossover is.
- **The extra-payment slider redraws only this section**, not the whole page. `bindGlobals`
  redraws everything on any global input; that is right for price and rate, and wrong for a
  control that affects one section, because redrawing the Leaflet map on a slider drag
  stutters visibly on a Pi.

### One refactor this forced

`pitiParts` had the payment formula inline. The schedule needs the same formula, and a second
copy of it in the same file would be indefensible, so it moved into `pmt()` and both call it
— mirroring the Python side, where the schedule imports `cost.monthly_payment` rather than
reimplementing it.

That refactor broke `tests/test_max_price.py`, whose harness extracts `pitiParts` and
`solveMaxPrice` from `app.js` by regex and evals them under `node`. `pitiParts` suddenly
referenced a function the harness had not loaded. The test failed loudly on the first full
run and the fix was one extra `eval`. Worth recording: the regex-extraction harness is
brittle by design, and its brittleness did its job.

## Alternatives rejected

**Serve the schedule from the HTTP endpoint and have the page fetch it.** Correct engineering
and it deletes the duplication entirely. Rejected because [ADR 0001](0001-static-snapshot-plus-local-container.md)
commits to a static snapshot that works with no backend — that is the whole deployment story,
and the GitHub Pages build has no server to call. The page would go blank in exactly the
deployment the project leads with.

**Precompute schedules for a grid of inputs into `data.json`.** Strictly ADR-0008-compliant,
and it kills the sliders. Rejected: the interactivity is the point.

**Skip the browser version, link to the CLI.** What the project had. Rejected because the
tool has to work for the person buying the house, and that person is not going to `pip
install` anything before deciding whether to prepay.

## Verification

`tests/test_amortization_parity.py` — 30 tests: every row, the headline figures, the annual
rollup, term-month closure, the loop guard, and the real profile loan.
