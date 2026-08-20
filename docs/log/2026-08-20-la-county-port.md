# 2026-08-20 — Los Angeles County port

## What changed

The decision engine's active scenario moved from Spartanburg, South Carolina to
Los Angeles County, California. The weekday commute anchor is now Keck Hospital
of USC, 1500 San Pablo St, Los Angeles, CA 90033, for a 06:30–07:00 arrival.
This is not a cosmetic address replacement. County records, tax mechanics,
hazards, school geography, and the places where the tool must refuse certainty
all changed with it.

The active pipeline is now **GAFRCBES**: Geocode, Assessor, FEMA, Risk,
Commute, Broadband, Education, Score. Education is a distinct station because
school evidence has its own source boundary and cannot be hidden inside a
neighborhood score.

The port validation baseline is green at 950 passing tests. That matters only
as a regression statement: it does not make public sources, school assignment,
or a future tax bill certain.

## Decisions made

### Replace the millage premise with a buyer-side Proposition 13 estimate

South Carolina's assessment-ratio-and-millage premise was removed from the
active California documentation. A California sale resets assessed value to the
purchase price under Proposition 13, then later increases are capped; the
seller's current bill is a prior-owner fact and systematically understates the
buyer-side market-price scenario. The tax path now requires a verified Los
Angeles County Tax Rate Area (TRA) and emits a blocking task where that evidence
is not available.

The anchor is TRA 00004 at 1.187380% in the fiscal-year 2025–26 county rate
report. The county publishes 13,197 TRAs for that year, so an anchor rate cannot
be used as a county-wide default. The recorded sample parcel assessment of
$203,078 against a $775,000 comparison price, a 0.262 ratio, made the direction
of the error visible enough to justify the blocking task. The comparison price
is the available 90033 median-sale reference, not a claimed listing ask, from
the [Redfin Data Center](https://www.redfin.com/news/data-center/).

Supplemental assessment and the non-transferring homeowners' exemption stay
outside the false precision of the annual estimate. The full $7,000 exemption
requires an eligible homeowner's claim by February 15. The relevant sources are
the [Los Angeles County TRA lookup](https://auditor.lacounty.gov/tax-rate-area-lookup/),
the county's [fiscal-year 2025–26 TRA report](https://auditor.lacounty.gov/wp-content/uploads/2025/09/Highest-Value-TRA-Report-FY-2025-2026_SORTED.xlsx),
the [Board of Equalization Proposition 13 guidance](https://www.boe.ca.gov/proptaxes/proposition-13.htm),
the [supplemental-assessment guidance](https://www.boe.ca.gov/proptaxes/supplemental-assessment/),
and the [homeowners'-exemption guidance](https://boe.ca.gov/pdf/pub800-6.pdf).

This decision is recorded in [ADR 0020](../adr/0020-proposition-13-purchase-price-tax-model.md).

### Treat the assessor roll as incomplete, not inconvenient

The active parcel source is the [Los Angeles County parcel layer](https://public.gis.lacounty.gov/public/rest/services/LACounty_Cache/LACounty_Parcel/MapServer/0/query).
It does not provide owner name, formal lot size, roof type, heat type,
last-sale price, or utility fields. Water/sewer is therefore unavailable rather
than assumed. `require_public_water_sewer` is off because a missing county field
cannot honestly satisfy a hard requirement.

This produces more manual tasks, especially for utilities and condition. That is
the intended cost of not turning an urban-looking location into a public-service
claim.

### Add education without promising enrollment

The [LAUSD attendance-boundary layers](https://maps.lacity.org/lahub/rest/services/LAUSD_Schools/MapServer)
carry numeric keys but no school name. A coordinate-to-polygon result is
measured; the required spatial join to a separate school-point layer is derived.
A polygon is also not an enrollment guarantee.

Zone of Choice makes the high-school relation one-to-many. At the anchor, the
station carries Theodore Roosevelt Senior High School and Felicitas and Gonzalo
Mendez High School, and reports their mean only as a comparison summary. The
source for policy confirmation is [LAUSD Zone of Choice](https://zoc.lausd.org/).

CAASPP proficiency is extracted from server-rendered
[public reports](https://caaspp-elpac.ets.org/caaspp/) through a specific HTML
contract. A redesign can break the parser. The parser therefore fails closed to
`unavailable` with a review task, and the output retains the report URL. CAASPP
is labeled as an enrolled-student outcome signal, not the effect a school will
have on a child transferring in.

This decision is recorded in [ADR 0019](../adr/0019-school-data-measured-inferred-boundary.md).

### Change hazards by configuration, retain caveat semantics

The California profile presents FEMA National Risk Index signals for wildfire,
earthquake, drought, heat wave, and landslide where published by the
[FEMA National Risk Index](https://hazards.fema.gov/nri/). They remain
tract-scale caveats, not property-level loss predictions or hidden point
deductions. ADR 0009's design survived the port; its profile values changed.

## What remains uncertain

- A verified TRA is still required for every candidate parcel. TRA 00004 is
  anchor evidence, not a shortcut for another address.
- The county roll still cannot answer utilities, condition, owner, sale price,
  roof, heat/HVAC, or formal lot size. Separate records and professionals must
  answer those questions.
- Several target ZIPs cross school districts, and elementary-only districts
  feed separate union high-school districts. Every candidate requires an
  address-level school check.
- Zone of Choice, capacity, lotteries, transportation, and board redraws mean
  the education result is not an enrollment determination.
- The CAASPP HTML extractor is deliberately monitored but remains a fragile
  integration point. A passing fixture does not prove the next report page has
  the same shape.
- Stronger measured school signals can be in ZIPs above the buyer's comparison
  range. This is an affordability trade-off to show, not a rule to hide.
- Hazard, route, broadband, insurance, inspection, title, and closing outcomes
  all require sources beyond this engine before an offer decision.

## Documentation changed with the port

The README, customer brief, limitations, threat model, property-analysis spec,
testing guide, ADR index, and this log now describe the active California
scenario. The prior 2026-08-19 entry remains as an historical Spartanburg
baseline; it is not evidence for the active sources or tax model.
