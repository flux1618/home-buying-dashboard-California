# Home-Buying Decision Engine — Los Angeles County

**Live dashboard:** <https://flux1618.github.io/home-buying-dashboard-California/>

A local-first decision engine for comparing a buyer's **all-in** monthly cost,
property evidence, commute fit, risk caveats, and school-data uncertainty. This
is not a listing browser and does not make an offer recommendation.

The published site is the static frontend only (`index.html`, `app.js`,
`data.json`); the Python engine, tests, and ledger under `analyzer/`, `service/`,
and `ledger/` are meant to be run locally against your own inputs.

The current scenario is anchored at **Keck Hospital of USC, 1500 San Pablo St,
Los Angeles, CA 90033**, with a weekday arrival target of 06:30–07:00. A result
is useful only when it says both what was measured and what still needs human
verification.

## What it does

Given an address and a purchase-price scenario, the engine:

- normalizes and geocodes the address;
- reads the Los Angeles County assessor parcel record where one is available;
- estimates California purchase costs using Proposition 13 and the parcel's tax
  rate area (TRA), rather than copying the seller's displayed tax;
- adds FEMA National Risk Index caveats for California's configured hazards;
- estimates a weekday drive to the Keck anchor;
- reports broadband only when the source can support it;
- resolves LAUSD boundary and choice data, then reads CAASPP proficiency as an
  enrolled-student outcome signal; and
- produces a score, explanation, review tasks, and an append-only saved-record
  ledger.

The station mnemonic is **GAFRCBES**:

| Letter | Station | Question it answers |
| --- | --- | --- |
| G | Geocode | Did the candidate address resolve? |
| A | Assessor | What does the county parcel record actually contain? |
| F | FEMA | Which hazard percentiles are caveats? |
| R | Risk / cost | What are the buyer-side recurring-cost assumptions? |
| C | Commute | Does the weekday trip fit the chosen arrival window? |
| B | Broadband | Is qualifying service evidenced, unavailable, or unknown? |
| E | Education | What does the boundary, choice, and CAASPP evidence say? |
| S | Score | What is supported, missing, or blocking? |

`E` is deliberately not “school assignment.” LAUSD attendance polygons need a
separate point-layer join to produce names, and Zone of Choice can return more
than one campus. See [ADR 0019](docs/adr/0019-school-data-measured-inferred-boundary.md).

## Quick start

```bash
python -m analyzer.cli "2475 Verde St, Los Angeles, CA 90033" 775000
```

The address is a real Los Angeles address. The price is a scenario input, not a
claim about a listing. Run the command from the repository environment with the
project dependencies installed.

A result may contain a **BLOCKING** tax task. That is expected when the TRA is
not verified or when a seller's Proposition 13 assessment cannot support a
buyer-side tax estimate. It is better to stop than to repeat a frozen seller
bill as a buyer cost. See [ADR 0020](docs/adr/0020-proposition-13-purchase-price-tax-model.md).

## Evidence model

Every source station either returns a value with provenance or returns an
explicit unknown. The core uses these labels:

| Label | Meaning |
| --- | --- |
| `measured` | Directly supported by the source record or geometry. |
| `derived` | Calculated or joined from measured inputs. |
| `estimated` | A stated planning assumption, not an observed future bill. |
| `extracted` | Parsed from a public report with a documented parser dependency. |
| `unavailable` | The source cannot support the field; the engine does not guess. |

This distinction matters most for utilities, school assignment, and tax. For
example, the County parcel layer does not supply a water/sewer field, so the
engine reports it as unavailable rather than calling every urban parcel public
service. Read [Known limitations](docs/KNOWN_LIMITATIONS.md) before relying on
any output.

## Primary sources

| Need | Primary source | How the engine treats it |
| --- | --- | --- |
| Parcel assessment and characteristics | [Los Angeles County Assessor parcel layer](https://public.gis.lacounty.gov/public/rest/services/LACounty_Cache/LACounty_Parcel/MapServer/0/query) | A public roll record with important absent fields; not a title, utility, or sale-history record. |
| School outcomes | [CAASPP public reports](https://caaspp-elpac.ets.org/caaspp/) | Extracted enrolled-student proficiency signal; not a causal school-effect score. |
| Attendance and choice geography | [LAUSD school and boundary layers](https://maps.lacity.org/lahub/rest/services/LAUSD_Schools/MapServer) and [Zone of Choice](https://zoc.lausd.org/) | Boundary-to-name join and choice set are explicitly inferred / derived. |
| Buyer property tax | [LA County Auditor-Controller TRA lookup](https://auditor.lacounty.gov/tax-rate-area-lookup/) | Purchase-price estimate using the verified parcel TRA; seller tax is not carried forward. |
| Natural hazards | [FEMA National Risk Index](https://hazards.fema.gov/nri/) | California hazard percentiles are caveats, never hidden score deductions. |

The California hazard profile includes wildfire, earthquake, drought, heat
wave, and landslide signals where FEMA publishes them. Their presence is a
reason to investigate insurance, mitigation, and local conditions; it is not a
claim that a specific house will suffer a loss.

## Boundaries

- The engine does not collect MLS data, reproduce listing descriptions, or
  forecast prices, rates, school assignment, insurance underwriting, or taxes.
- It does not replace a lender disclosure, insurance quote, title review,
  inspection, district enrollment confirmation, or tax professional.
- Static outputs are evidence snapshots. Refreshing a source may change a
  result without changing the address.

## Documentation

- [Customer brief](docs/CUSTOMER_BRIEF.md) — decision context and priorities.
- [Known limitations](docs/KNOWN_LIMITATIONS.md) — uncertainty that changes how
  an output must be used.
- [Threat model](docs/THREAT_MODEL.md) — data handling and source exposure.
- [Architecture decisions](docs/adr/README.md) — durable design choices.
- [Port log: 2026-08-20](docs/log/2026-08-20-la-county-port.md) — what changed
  in the California port and what remains unresolved.
