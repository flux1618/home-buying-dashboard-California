# ADR 0015 — Rate sensitivity is scenario arithmetic, not a rate forecast

**Date:** 2026-08-19
**Status:** Accepted
**Supersedes nothing. Extends:** [ADR 0008](0008-browser-rules-are-compiled-not-rewritten.md), [ADR 0010](0010-inverse-affordability-is-two-answers.md), [ADR 0013](0013-the-page-gets-its-own-schedule-under-a-parity-test.md)

## Context

"Should I wait?" is often asked as though the tool can predict a mortgage-rate move or a house-price move. It cannot. What it can answer honestly is narrower and more useful: hold one house and household constant, apply stated rates, show the full payment and both DTI price ceilings, then show the later house price that would restore today's payment at a stated later rate.

The cost engine already owns the non-obvious parts: PITI, property-tax treatment, insurance, maintenance reserve, and the two max-price answers. The schedule owns the payment calculation. A fresh rate-band formula would create a third version of those calculations, eventually disagreeing with the report.

The static page has the opposite constraint. Its sliders must respond without a local server, so a small rate loop and break-even bisection have to live in JavaScript. That is duplicated behaviour under the rule in ADR 0008, and must be audited rather than trusted.

## Decision

**`analyzer.core.sensitivity` is a pure scenario engine. It sends every rate through `cost.compute`, `cost.monthly_payment`, and `cost.solve_max_price`; the CLI is the I/O door; the page gets a small live mirror under a zero-tolerance parity test.**

The default sweep is 5.00% through 7.50%, inclusive, in 25-basis-point steps. Every point holds price, down payment, term, tax/insurance assumptions, HOA, and household inputs constant. It returns full PITI, principal and interest, term interest, lender max price, household max price, and deltas versus an explicitly labeled baseline.

`wait_vs_buy` takes two supplied rate/price scenarios and solves the future price with the same full PITI as the baseline. Its result is explicitly labeled arithmetic on assumptions, not a mortgage-rate or home-price forecast. A lower rate paired with a sufficiently higher price can still produce a worse payment.

A local `data/mortgage30us.json` observation is used as the CLI anchor only when that file exists and is valid. Otherwise the configured profile rate is retained and labeled as a configured assumption. The snapshot is FRED's presentation of Freddie Mac's weekly 30-year fixed-rate series, which is an average rather than a borrower's quote ([FRED MORTGAGE30US](https://fred.stlouisfed.org/series/MORTGAGE30US), [Freddie Mac PMMS](https://www.freddiemac.com/pmms)). Snapshot reading remains outside the pure core.

`tests/test_sensitivity_parity.py` evaluates the exact JavaScript functions under node and compares the 11 displayed rate rows plus the wait/buy outputs against Python in integer cents. One cent of difference is a failure, not display tolerance.

### This decision forbids

- treating the snapshot, slider, or default range as a forecast or a lender quote;
- silently substituting a hard-coded market rate when no snapshot exists;
- copying the payment, PITI, tax, insurance, or DTI-max-price formulas into the Python sensitivity module;
- adding browser sensitivity arithmetic without updating the zero-tolerance parity test; and
- calling the solved break-even price a predicted future listing price.

## Consequences accepted

- The break-even price equalizes modeled PITI at cent precision, not closing costs, concessions, moving costs, refinancing, or a future appraisal.
- Mortgage insurance remains unmodeled, so a low-down-payment result is incomplete and must not be treated as lender-ready.
- The static page visualizes the slider assumption, not the external snapshot, because a public static page cannot safely imply live data.
- The parity test skips locally if node is unavailable, consistent with the existing page parity tests; `HBA_REQUIRE_NODE` makes that absence fail when required.

## Alternatives rejected

**Forecast rates or prices.** Rejected. A numerical forecast would add false authority and would be less decision-useful than a range that exposes the payment consequence of assumptions.

**Let the page call a live endpoint.** Rejected. ADR 0001 keeps the public dashboard usable as a static snapshot; a live endpoint would make the page fail in its primary deployment.

**Use a one-line payment calculator.** Rejected. It omits tax, insurance, HOA, and the two DTI answers that make the same-house comparison honest.

## Verification

- `tests/test_sensitivity.py` — grid shape, monotonic payment, both max-price answers, baseline deltas, break-even identity, and absurd inputs.
- `tests/test_sensitivity_parity.py` — page/Python equality in integer cents across normal, HOA, and cash-purchase cases.
- `python -m analyzer.sensitivity_cli --help` — documents the standalone CLI door.
