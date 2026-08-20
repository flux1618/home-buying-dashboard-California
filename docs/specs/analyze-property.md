# Spec — Analyze a Property

One Los Angeles County address and one purchase-price scenario enter the same
local decision path. The output is a source-linked comparison, not a listing
record, underwriting decision, or school-assignment notice.

## Station contract

**Mnemonic — GAFRCBES:** Geocode, Assessor, FEMA, Risk, Commute, Broadband,
Education, Score.

Each source station is an adapter. It may return evidence, an explicit
unavailable result, and a degradation task; it may not invent a replacement
value. Only a failed geocode prevents analysis. This is the failure contract in
[ADR 0006](../adr/0006-source-station-contract.md).

| Station | Output boundary |
| --- | --- |
| Geocode | Candidate address to coordinates and match evidence. |
| Assessor | Public Los Angeles County parcel fields only; no owner, utility, roof, heat, lot-size, or sale-price invention. |
| FEMA | National Risk Index caveats for the configured California hazards. |
| Risk / cost | Buyer-side ownership-cost scenario, including purchase-price tax and a parcel TRA review. |
| Commute | Route estimate to Keck Hospital of USC, 1500 San Pablo St, for the weekday 06:30–07:00 arrival context. |
| Broadband | Reported availability where a source supports it; otherwise `unavailable`. |
| Education | LAUSD boundary / choice geometry and CAASPP outcome evidence, with the measured-versus-inferred distinction preserved. |
| Score | Pure local evaluation over the available facts and verification tasks. |

## Input

The CLI accepts a real Los Angeles County address and a buyer-selected price
scenario:

```bash
python -m analyzer.cli "2475 Verde St, Los Angeles, CA 90033" 775000
```

The price is an input to evaluate, not extracted listing data. Optional property
facts can supplement the address only when they have a stated source. A missing
fact remains unknown; it must not be filled from neighborhood averages.

## Core source behavior

### Parcel

The [Los Angeles County parcel layer](https://public.gis.lacounty.gov/public/rest/services/LACounty_Cache/LACounty_Parcel/MapServer/0/query)
is the assessor source. It supports assessment and parcel identification, but
it does not carry owner, formal lot-size, roof, heating, last-sale-price, or
utility fields. Water/sewer therefore remains unavailable until separately
verified. See [Known limitations](../KNOWN_LIMITATIONS.md).

### Tax

The California model starts from the buyer's purchase-price scenario and a
verified [Los Angeles County Tax Rate Area](https://auditor.lacounty.gov/tax-rate-area-lookup/),
not the seller's displayed bill. Proposition 13 resets assessed value after a
sale; the seller's current tax is a frozen-base-year observation. Supplemental
assessment and homeowners'-exemption eligibility remain human closing tasks.
See [ADR 0020](../adr/0020-proposition-13-purchase-price-tax-model.md).

### Hazard

The [FEMA National Risk Index](https://hazards.fema.gov/nri/) is a tract-scale
caveat source. The California configuration reports wildfire, earthquake,
drought, heat-wave, and landslide signals where published. A percentile is not
a parcel inspection, flood determination, insurance quote, or prediction of
loss. Hazard facts do not become hidden score deductions; [ADR 0009](../adr/0009-hazard-risk-is-a-caveat.md)
sets that rule.

### Education

The [LAUSD map service](https://maps.lacity.org/lahub/rest/services/LAUSD_Schools/MapServer)
provides attendance geometry but no direct school name in the boundary layers.
The station first measures coordinate-to-polygon containment, then derives a
name by a spatial join to a school-point layer. Zone of Choice may produce a
set of campuses. [CAASPP reports](https://caaspp-elpac.ets.org/caaspp/) supply
an extracted enrolled-student proficiency signal, not a promise of enrollment
or causal estimate of a transferred child's outcome.

At the Keck anchor, the high-school choice set contains Theodore Roosevelt
Senior High School and Felicitas and Gonzalo Mendez High School. The output
carries both campuses and their mean as a comparison summary. Full rules,
parser failure behavior, and provenance labels are in
[ADR 0019](../adr/0019-school-data-measured-inferred-boundary.md).

## Output contract

Every sourced result carries a value (or explicit absence), source URL,
retrieval time where applicable, and one provenance label:

| Label | Meaning |
| --- | --- |
| `measured` | Direct source fact or geometry result. |
| `derived` | Calculation or documented join over source facts. |
| `estimated` | Stated planning assumption. |
| `extracted` | Parsed report value with parser-dependent provenance. |
| `unavailable` | The source cannot support the field. |

A successful response can still contain blocking tasks. Typical examples are a
missing tax-rate area, a purchase-price tax result that cannot be verified, a
missing utility fact, a route that needs an actual drive, or a school choice
set that requires district confirmation.

## Failure behavior

| Station condition | Required behavior |
| --- | --- |
| Geocode cannot resolve the address | Stop; there is no defensible location to analyze. |
| Parcel field does not exist | Emit `unavailable`; do not infer a favorable property fact. |
| TRA cannot be verified | Keep buyer tax as unresolved and emit a blocking task. |
| FEMA source cannot support a signal | Preserve an unknown caveat rather than calling the risk low. |
| Route source fails | Emit an unavailable commute and a verification task. |
| CAASPP HTML changes or suppresses a value | Emit `unavailable` and a parser / source review task. |
| Boundary has no resolvable school point | Preserve the data gap; do not select a nearby school. |
| Any non-geocode station fails | Degrade that station and continue with visible provenance. |

## Acceptance criteria

1. The output distinguishes source fact, calculation, estimate, extraction, and
   absence for every decision-relevant value.
2. A California buyer tax is never copied from the seller's displayed bill.
3. Missing county utility data does not pass a public-water/sewer requirement.
4. A school boundary name and a Zone of Choice result are visibly derived,
   never presented as a guaranteed assignment.
5. A changed CAASPP report shape fails closed to `unavailable`.
6. The scoring core remains network-free and reproducible for the same supplied
   evidence.
