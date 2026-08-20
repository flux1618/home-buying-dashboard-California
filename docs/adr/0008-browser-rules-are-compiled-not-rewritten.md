# 0008 — The browser is the fourth door, and its rules are compiled rather than rewritten

**Status:** Accepted
**Date:** 2026-08-19
**Supersedes:** nothing
**Related:** [0001](0001-static-snapshot-plus-local-container.md) (static snapshot + local container), [0002](0002-pure-scoring-core.md) (pure core), [0005](0005-capital-expenses-deduct-and-unknowns-pin.md) (capex deductions and unknowns), [0007](0007-http-service-container-and-split-ci.md) (three doors on one engine)

## Context

ADR 0007 said this, in its own words, about writing a second implementation of the scoring
logic:

> That is how a codebase ends up with two subtly different implementations of the same
> scoring — the CLI's and the API's — that drift until they disagree about the same house.

It then made the CLI, the batch runner, and the HTTP API share one core, and considered the
problem solved. It counted three doors. There were four.

`index.html` and `app.js` — the public snapshot from ADR 0001 — contain a property scorer
too. It predates the Python engine, it was written by hand in JavaScript, and 0007 never
looked at it. It had been drifting the entire time the engine was being built.

By the time anyone checked, the two disagreed like this:

| Rule | The page said | The engine says |
|---|---|---|
| HOA above $100/mo | score 0, **PASS**, disqualifying | −25, still a candidate |
| Roof at 17 years | a note, no points | −25 |
| Roof at 22 years | a note, no points | −35 |
| HVAC at 14 years | a note, no points | −15 |
| HVAC at 19 years | a note, no points | −22 |
| WATCH floor | 50 | 45 |
| Facts not known | scored as if the answer were good | capped below TAKE |
| Built before 2000 | not mentioned | a caveat, never a deduction |

Two of those are not rounding differences, they are opposite answers:

- **606 Andre Ct** (roof 17, HVAC 14, 2.5 baths, size not recorded by the county) scored a
  confident **TAKE** on the published page. The engine scores it **52 WATCH**. The page was
  recommending an inspection on a house carrying an estimated $13,500–$33,500 of near-term
  roof and HVAC work, because an aging system produced a sentence instead of a deduction.
- **A house with a $150/mo HOA and nothing else wrong** was rejected outright by the page —
  score 0, PASS. The engine scores it **75 TAKE**. The page was throwing away good houses
  over a $50 overage, which is the exact mistake the HOA rule was rewritten to stop making.

Neither is a coding error. Both are the predictable result of writing the same rules twice.
The page was correct when it shipped; the profile moved and the page didn't.

The complication is that the browser cannot be given the same fix the API got. ADR 0001
committed to a public snapshot with no backend — static files, no keys, works offline — and
ADR 0002 keeps the engine a pure Python package. A static page cannot import Python. "Just
share the core" is not available here.

## Decision

**The browser does not get its own copy of the rules. It gets a compiled one.**

`tools/build_snapshot.py` reads `buyer_profile.toml` — the same file the engine loads — and
writes a declarative `rules` block into `data.json`: hard fails, deduction thresholds and
weights, capital-expense tiers with their dollar bands, the caveat limits, and the verdict
bands. `app.js` **evaluates** that block. It does not restate a single threshold.

The evaluation order in `updateProperty()` mirrors `analyzer/core/scoring.py` step for step:
hard fails, then deductions, then capital expenses, then the unknown-facts cap, then the
verdict bands. That ordering is not cosmetic — applying the cap after the bands, or the
deductions before the hard fails, changes answers.

Three things enforce it, and the third is the one that matters:

1. `python tools/build_snapshot.py --check` exits non-zero when `data.json` no longer matches
   the profile. It runs in CI on every push.
2. `tests/test_snapshot_rules.py` traces every threshold, weight, capex tier, and dollar band
   in the shipped `data.json` back to the profile and to `analyzer/core/capex.py`.
3. The same file asserts that `app.js` reads `DATA.rules`, that the old hardcoded branches are
   gone, and that no bare threshold literal has reappeared inside the scorer function.

Checks 1 and 2 only prove the data is right. Check 3 proves the page actually uses it, which
is the failure this ADR exists to prevent — correct data sitting next to a function that
ignores it looks exactly like a working system.

**What is deliberately not compiled.** The browser does not get the engine. Tax millage and
the SC 4%/6% assessment reset, amortisation, insurance proration, cash-to-close, the three
maintenance-reserve methods, and every source station stay in Python. The browser gets only
the genuinely declarative part: threshold comparisons and verdict bands. The page now says so
plainly in a "Run the full engine" section and links to the CLI, the batch runner, and the
container.

That boundary is the entire discipline of this ADR. Compiling a little more each time — the
tax model, then the amortisation — rebuilds the second implementation this record was written
to delete, one reasonable-looking commit at a time.

## Alternatives considered

**Fix the JavaScript numbers by hand.** An hour of work, and it restores the exact condition
that caused the bug: two hand-maintained copies, correct on the day they are synced. It would
have drifted again the next time a weight was tuned, and the next drift would be harder to
spot because everyone would remember having fixed it once.

**Delete the scorer from the public page.** Honest, and genuinely tempting — the page cannot
look up flood zones or drive times, so its scores are only as good as what the visitor types.
Rejected because the scorer is the part of the snapshot that demonstrates what the tool
actually decides. A snapshot showing only market charts is a report, not a decision tool. The
"Run the full engine" section is the compromise: keep the scorer, and be explicit about the
line between typed facts and looked-up ones.

**Compile the Python engine to WebAssembly (Pyodide).** Perfect fidelity, one implementation,
no drift possible. Rejected on cost: a multi-megabyte runtime download on a page whose entire
appeal is that it is static, fast, and works offline, in exchange for arithmetic the visitor
cannot supply the inputs for anyway. Worth revisiting only if the snapshot ever needs the full
financial model in the browser.

**Serve the API publicly and have the page call it.** Contradicts ADR 0001 directly, and the
service is unauthenticated by design (localhost only). It also puts a rate-limited public
dependency in front of the thing that currently cannot break.

## Consequences

**Good.** One rulebook, four doors, and a build that fails when they disagree. Changing a
threshold is now a one-line profile edit plus a rebuild, and the CI check names the fix in the
error message. The page explains its own reasoning better than before: it shows each deduction
with its point value, the capital-expense dollar range with a source link, and says out loud
when a score was capped for missing data.

**Bad.** There is now a build step between the profile and the published page, and forgetting
it produces a stale snapshot. That is mitigated but not eliminated — `--check` catches it in
CI, so the failure is loud and cheap rather than silent and expensive. The generated `rules`
block also makes `data.json` diffs noisier.

**Duplicated on purpose, narrowly.** `build_snapshot.py` repeats the roof and HVAC dollar
bands from `analyzer/core/capex.py` rather than importing them, so the build script does not
require the package to be installed. `test_capex_bands_match_the_engine` pins the two together
across five square-footage values, so the duplication cannot drift quietly.

**If this is wrong.** The likely way it fails is scope creep — someone needs one more number
in the browser, adds it to the rule block, and repeats until the compiled subset is a
reimplementation with extra steps. The signal to watch for is a rule that cannot be expressed
as a threshold comparison. When that appears, the answer is to send the user to the local
engine, not to grow the compiler.

## Sources

- Roof replacement cost, South Carolina: [This Old House](https://www.thisoldhouse.com/roofing/roof-replacement-cost-south-carolina), [Modernize](https://modernize.com/roof/cost-calculator/south-carolina)
- HVAC replacement cost by home size: [USA Today](https://www.usatoday.com/story/money/home-services/hvac-replacement-cost/90313725007/)
- SC assessment ratio, 4% legal residence vs 6%: [SC Department of Revenue](https://dor.sc.gov/lgs/property-tax-basics)
