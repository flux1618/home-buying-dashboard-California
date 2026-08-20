# Capex cost-band port — South Carolina → Los Angeles County

**Scope:** `analyzer/core/capex.py` and `tests/test_capex.py` only, per task ownership rules.
No other file was edited. ADR 0002 (pure, stdlib-only `core/`, no network) and ADR 0012's
"no float drift" *principle* were both checked against the module: `capex.py` has never
carried money as integer cents — it stores planning-range dollars as `float` and always has
(see the `low: float` / `high: float` fields on `CapitalExpense`, unchanged). ADR 0012 is
scoped explicitly to `analyzer/core/amortization.py`'s payment schedule, not to this module,
so the float-dollar style was matched exactly rather than converted.

## What the module actually models

`capex.assess()` only ever consumes `roof_age_years` and `hvac_age_years`. Those are the only
two components ported. The codebase separately extracts `water_heater_age_years` as a raw
property fact (`analyzer/core/extraction.py`, `analyzer/extract/providers.py`) but never feeds
it into `capex.assess()` or `scoring.py` — it is not currently a scored component. There is no
electrical-panel or sewer-lateral field anywhere in the engine. Wiring new components into the
score is a `scoring.py` / `buyer_profile.toml` change, which is outside this task's file
ownership, so I did not add them to the module. Research on those three is included below as
requested, for whoever picks up that follow-on work.

## Cost bands: old (SC) vs. new (LA County)

| Component | Old SC figure | New LA/CA figure | Source |
|---|---|---|---|
| Roof, <1,500 sqft | $6,000–$14,000 | $7,000–$21,000 | [Angi, LA-specific size table](https://www.angi.com/articles/how-much-does-roof-replacement-cost/ca/los-angeles) |
| Roof, 1,500–2,499 sqft | $8,000–$18,000 | $9,000–$27,000 | [Angi, LA-specific size table](https://www.angi.com/articles/how-much-does-roof-replacement-cost/ca/los-angeles) |
| Roof, ≥2,500 sqft | $12,000–$24,000 | $13,000–$34,000 | [Angi](https://www.angi.com/articles/how-much-does-roof-replacement-cost/ca/los-angeles); upper bound checked against [Modernize's LA-metro total ($18,000–$30,000, avg $26,220)](https://modernize.com/roof/cost-calculator/california) |
| Roof, unknown sqft | $6,000–$19,000 | $8,000–$30,000 | [Angi](https://www.angi.com/articles/how-much-does-roof-replacement-cost/ca/los-angeles) / [Modernize CA](https://modernize.com/roof/cost-calculator/california) |
| HVAC, <1,750 sqft | $6,000–$10,000 | $7,000–$13,000 | [USA Today national size table](https://www.usatoday.com/story/money/home-services/hvac-replacement-cost/90313725007/) shape, LA premium applied from [Modernize CA central-AC calculator](https://modernize.com/hvac/central-air-conditioner-installation/cost/california) |
| HVAC, 1,750–2,249 sqft | $7,000–$12,000 | $8,500–$16,000 | same two sources |
| HVAC, 2,250–2,749 sqft | $8,000–$14,000 | $10,000–$18,500 | same two sources |
| HVAC, ≥2,750 sqft | $9,000–$16,000 | $11,000–$20,000 | same two sources |
| HVAC, unknown sqft | $7,500–$14,500 | $9,000–$17,000 | same two sources |

Sample-house total (1,620 sqft, roof + HVAC both due/overdue, matching
`tests/test_capex.py`'s `clean()` fixture): moved from **$14,000–$28,000** (SC) to
**$16,000–$40,000** (LA).

### Why LA runs higher, by driver

- **Roof:** LA is a high-cost labor market — Angi's own copy for the LA page attributes its
  premium mainly to labor. Separately, the LA Residential Code (Chapter 15, tied to the
  California Building Code / wildfire-urban-interface rules) requires a Class A fire-rated
  roof assembly on reroofs; South Carolina has no equivalent wildfire-driven material mandate.
  Multiple independent LA-specific sources ([RoofVista](https://roofvista.com/resources/guides/roof-replacement-cost-los-angeles-2026), [flatroofcalculator.com](https://flatroofcalculator.com/roof-replacement-cost/california/los-angeles/)) separately estimated LA running ~15–25% above the California state average and ~20% above the national average, consistent with the Angi/Modernize numbers used here.
- **HVAC:** the module's docstring itself flags that LA cooling load, not heating, is what
  matters — a straight port of a furnace-plus-AC SC band would overstate the heating-equipment
  share of an LA replacement bill. Modernize's California-specific calculator prices AC
  installation alone at a premium over the national full-system table for the medium/large
  tiers, which is the LA-specific evidence used to scale the bands up rather than inventing a
  multiplier.

Both bands keep the USA Today national size-shape as the skeleton (four sqft tiers) because no
equally granular LA-specific *size table* for HVAC was found — only overall CA/LA totals and an
LA-specific size table for AC-only installation. That gap is called out in the module comment
rather than silently presenting a national shape as fully LA-sourced.

## Research on components not wired into the module (informational only)

These are **not** implemented in `capex.py` — reported here per the task's research step, for
a future scoring change, not added to the module itself (that would require touching
`scoring.py` and `buyer_profile.toml`, which this task does not own).

| Component | LA figure found | Source | Note |
|---|---|---|---|
| Water heater, gas tank (40–50 gal) | ~$1,150–$3,200 installed | [water-heater-near-me.com, LA-specific](https://water-heater-near-me.com/california/los-angeles) | |
| Water heater, heat-pump (hybrid) | ~$3,200–$6,400 installed (before rebates) | [ventahvac.com, LA-specific](https://ventahvac.com/services/water-heater/); consistent with [homeupgradespecialist.com](https://homeupgradespecialist.com/heat-pump-water-heater-installation-los-angeles/) at $5,500–$8,500 gross | California (via SoCalGas/CEC) is actively pushing heat-pump water heaters — SoCalGas rebates of up to $3,800 pull the net cost of a heat-pump unit close to or below a standard gas tank replacement in some cases, per the same source. |
| Electrical panel upgrade, 100A→200A | ~$2,900–$5,100 typical, up to ~$7,000 if service mast/meter also needs replacement | [saiyanelectric.com, LA-specific](https://saiyanelectric.com/electrical-panel-upgrade-cost-los-angeles/); consistent with [aplaconstruction.com](https://aplaconstruction.com/electrical-panel-upgrade-cost-los-angeles/) ($2,800–$5,500) and [shaffercon.com](https://shaffercon.com/industry-insights/electrical-service-upgrade-100-to-200-amp-guide/) ($3,000–$6,000) | Directly relevant to this fork's anchor: the sample parcel is built 1957, and 1950s-era 60–100A panels are common in that vintage of LA housing stock. This is a good candidate for a future scored component given how frequently it will trigger. |
| Sewer lateral replacement | ~$4,130–$14,160 typical LA project (city permit $215) | [alertplumber.com, LA-specific](https://alertplumber.com/sewer-line-replacement/los-angeles-california/); consistent with [expertiseplumbing.com](https://expertiseplumbing.com/sewer-line-replacement-cost-2026/) noting LA's older clay/cast-iron lateral stock specifically | Los Angeles Bureau of Sanitation can require lateral inspection/replacement at time of sale in some circumstances — this is a real LA-specific risk the SC-era engine had no equivalent for, and is worth scoring once a wiring point exists. |

Typical service life, for reference if ages are later encoded for these components: gas tank
water heaters ~8–12 years, heat-pump water heaters ~10–15 years (manufacturer/CEC guidance
cited across the water-heater sources above); electrical panels are not usually replaced on a
fixed age schedule but 1950s–60s fusebox/60A services are flagged categorically rather than by
year; clay sewer laterals in LA's older housing stock (pre-1960s) are frequently 60–90+ years
old and past typical clay-pipe service life, which is exactly why city point-of-sale inspection
rules target them.

## Tests

- `tests/test_capex.py`: updated the two assertions that pinned the old SC dollar figures
  (`test_unknown_sqft_falls_back_to_a_statewide_range` → renamed
  `test_unknown_sqft_falls_back_to_a_countywide_range`, now asserting `(8_000, 30_000)` /
  `(9_000, 17_000)`; `test_the_dollar_range_is_surfaced` now asserts `16_000` / `40_000`).
  The 1957/880,000/school-facts sample house set by a prior agent was left untouched.
  **`tests/test_capex.py` result: 22 passed.**

- Full suite (`python3 -m pytest -q`): **948 passed, 2 failed, 13 deselected** — down from the
  950-passed baseline by exactly the 2 tests that hardcode the old South Carolina numbers
  outside files this task owns:
  - `tests/test_analyze.py::TestGracefulDegradation::test_capex_quotes_task_is_blocking_when_expensive`
    asserts the literal string `"$14,000-$28,000"` in a blocking-task message. That figure is
    the sample-house total under the old SC bands; it is not read from `capex.py`, it's
    retyped in the test file itself.
  - `tests/test_snapshot_rules.py::TestRulesMatchProfile::test_capex_bands_match_the_engine`
    fails because `tools/build_snapshot.py` (lines ~60–75) hardcodes its **own copy** of
    `ROOF_BANDS`, `ROOF_UNKNOWN`, `ROOF_SRC`, `HVAC_BANDS`, `HVAC_UNKNOWN`, `HVAC_SRC` — a
    second, disconnected copy of the exact constants this task ported. The test's own
    docstring says "the builder duplicates capex.py's dollar bands. Pin them together" — it
    is designed to fail exactly when `capex.py`'s bands move without a matching update to
    `tools/build_snapshot.py`. That file is not `analyzer/core/capex.py` or
    `tests/test_capex.py`, so per the task's ownership boundary I did not edit it.

  **Action needed from the owner of `tools/build_snapshot.py` and `tests/test_analyze.py`:**
  update `ROOF_BANDS`/`ROOF_UNKNOWN`/`ROOF_SRC`/`HVAC_BANDS`/`HVAC_UNKNOWN`/`HVAC_SRC` in
  `tools/build_snapshot.py` to match the new constants in `analyzer/core/capex.py` (values
  listed in the table above), and update the `"$14,000-$28,000"` literal in
  `tests/test_analyze.py` to `"$16,000-$40,000"`.

  I also observed `tests/test_velocity.py::test_normal_parse_streams_and_reduces_realistic_gzip`
  fail once during a baseline run before I touched anything, then pass on every subsequent run
  (in isolation and in two full-suite reruns) — a pre-existing flaky/test-ordering issue
  unrelated to capex and not caused by this change. Confirmed the clean baseline (before any
  edits) was `950 passed, 13 deselected`.

## Files touched

- `analyzer/core/capex.py` — cost bands, source constants, module/section comments.
- `tests/test_capex.py` — two assertions updated to match new bands; one test renamed to
  reflect "countywide" rather than "statewide" language.
