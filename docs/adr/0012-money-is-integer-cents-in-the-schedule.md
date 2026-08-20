# ADR 0012 — The amortization schedule counts in integer cents, and the last payment closes the loan

**Status:** Accepted
**Date:** 2026-08-19
**Extends:** [ADR 0002](0002-pure-scoring-core.md), [ADR 0010](0010-inverse-affordability-is-two-answers.md).

---

## Context

`cost.monthly_payment` has always returned the level payment, and `cost.compute` has always returned PITI. Neither says where the money goes.

That gap matters more than it sounds. The payment on the target loan is $1,151.49 a month, and $995 of the first one is interest. The month where principal finally exceeds interest is payment 236 — year 19.7 of a 30-year note. Paying $200 extra a month ends the loan 9 years 11 months early and saves $89,642. None of those numbers are derivable from a single payment figure, and all four of them change what a person decides to do.

Roadmap Phase 4 item 1 asked for an amortization calculator. Writing one is a twelve-line loop. Writing one that is *correct* raised three decisions that were not obvious, and each has a matching failure that looks fine on a chart.

## Decision 1 — Money is integer cents, not floats

`analyzer/core/amortization.py` holds the running balance as an `int` count of cents and converts to float only at the boundary, when building the output dataclasses.

A schedule is not one calculation. It is 360 sequential operations on the same running balance, each feeding the next. Float error accumulates over that many steps, and the tells are specific:

- a final balance of `-0.004` instead of `0.0`
- a total-interest figure a dollar or two off a lender's amortization notice
- `sum(p.principal for p in payments)` not equal to the loan amount

Integer cents cannot drift, because there is nothing to round after the fact. Interest is rounded once per month, at the moment it is computed, which is also what a servicer does.

The rounding is `Decimal` with `ROUND_HALF_UP`, not Python's `round()`. Python uses banker's rounding: `round(0.125, 2)` is `0.12`, and a lender rounds it to `0.13`. One cent a month is 360 cents, which is enough to make the totals disagree with a real statement — and the whole value of this table is that a buyer can hold it next to a lender's paperwork.

**Rejected:** floats with a final `round()` on the totals. It hides the drift in the summary while leaving it visible in the rows, which is the worst of both — the table would not add up to its own footer.

## Decision 2 — The final scheduled payment is the balance plus interest, not the level payment

A 30-year note has 360 payments. Getting that right requires an explicit clamp at the term boundary.

The level payment is rounded to the cent, so 360 of them do not land on zero. On the target loan they leave $3.31 behind, and a naive `while balance > 0` loop bills a **361st payment of $3.31**. That is exactly how real notes handle it in reverse: the last scheduled payment is whatever clears the loan, absorbing the rounding remainder — $1,374.78 instead of $1,371.49 on the 30-year example.

The failure mode is why this got a clamp rather than a tolerance. A 361-row table for a 30-year loan is not off by a rounding error, it is off by a row, and a reader who spots that stops trusting the other 360 lines. `test_payoff_takes_exactly_the_term` is parameterized across six loans including a 0% one and a 12-month one at 30%, and it failed at 361 before the clamp existed.

Extra payments legitimately finish early, and in that case the clamp is never reached because the balance is already gone.

## Decision 3 — The loop has a hard stop, and negative amortization raises

Two guards, both about failing fast rather than plausibly:

- `MAX_MONTHS = 1200`. Not a business rule — a stop, so a pathological input fails in milliseconds instead of hanging. An unbounded `while balance > 0` reachable from an HTTP endpoint is an outage, not a bug.
- If the payment does not cover the month's interest, the balance grows every month and the loan never amortizes. That raises immediately, naming both numbers, instead of running 1,200 iterations to discover it.

A percentage passed where a fraction belongs is also rejected: `annual_rate > 1` raises. `schedule(loan, 6.67, 360)` would otherwise produce a complete, confident, internally consistent schedule at 667% — and a wrong answer that passes its own invariants is the dangerous kind.

## Decision 4 — The schedule is principal and interest only, and says so in the payload

Taxes, insurance, HOA dues, and mortgage insurance do not amortize. They ride alongside the loan and they do not shrink when you pay extra principal.

So every figure in this module is smaller than the PITI figure `cost.compute` returns for the same house — $1,151.49 against $1,652. A reader who conflates them underestimates the real monthly cost by hundreds of dollars, and this tool prints both within a few inches of each other.

The mitigation is not a docstring. The output dict carries an `excludes` list naming all four, the CLI header says "principal and interest only", the CLI footer repeats the list, and the HTTP response carries a `note` that names PITI explicitly. A test asserts the list has exactly four entries, so trimming it fails rather than quietly dropping a caveat.

Mortgage insurance is on that list and is genuinely unmodeled — see [KNOWN_LIMITATIONS](../KNOWN_LIMITATIONS.md). A schedule is the natural place to model PMI drop-off at 78% LTV, since the balance is right there. It is not built, and the caveat says so rather than implying the payment is complete.

## Decision 5 — The payment formula is imported, not reimplemented

`schedule()` calls `cost.monthly_payment`. It does not contain its own annuity formula.

The temptation is real — it is four lines and the schedule needs it on the first line. But the PITI figure and the schedule appear on the same screen, describing the same loan. Two implementations of the same formula eventually disagree, and the visible symptom would be a $2 difference between two numbers that must be identical. `test_scheduled_payment_matches_the_cost_engine` asserts the tie holds.

Same reasoning as bisecting over `compute` in [ADR 0010](0010-inverse-affordability-is-two-answers.md): reuse the thing rather than restate it.

## Decision 6 — Extra payments are monthly only, and savings are measured not estimated

`extra_monthly` is the one prepayment knob. A one-time lump sum at month N is not supported.

That is a scope call, not a limitation of the loop — the loop would take a per-month map with no change in shape. It is left out because the question a buyer actually asks is "what does another $200 a month do", and a `dict[int, float]` in the request schema is a worse door for that. It is recorded in KNOWN_LIMITATIONS so it is a known gap rather than an oversight.

When `extra_monthly` is set, `months_saved` and `interest_saved` come from running the same loop again with the extra set to zero and subtracting. Not from a formula. Same reason as Decision 5: the comparison is against the schedule this module would actually produce, so it cannot be right about a loan the module does not generate.

With no extra payment, both fields are `0` rather than a comparison of a schedule against itself.

## Consequences

- The schedule is pure, so it needs no network and works entirely offline, like `/max-price`.
- Two new doors: `--amortize` / `--extra-monthly` on `analyzer.cli`, and `POST /amortization`.
- The CLI prints a 7-row milestone view (year 1, every fifth, and the payoff year), because 30 rows in a terminal is a wall nobody reads. The full 360 rows are available in `--json` and over HTTP behind `include_payments`, which defaults to false since 360 rows is roughly 40 KB of JSON.
- `crossover_month` counts extra payments toward the principal side, and the CLI says "including the extra" when it does. Two defensible readings exist; the payer-experience one was chosen and labeled rather than left ambiguous.
- **The static page does not have this yet.** Same gap as the ledger — the schedule is reachable from a terminal and an HTTP call, not from the site. Under [ADR 0008](0008-browser-rules-are-compiled-not-rewritten.md) a browser version would need to be compiled from this module, not hand-ported, or it becomes a second implementation like the page's max-price solver already is.
