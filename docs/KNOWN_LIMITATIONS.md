# Known Limitations

This document is part of the result, not a disclaimer after it. A score can be
useful only when its unsupported inputs remain visible.

## Read this first

- The engine compares buyer-defined price scenarios. It does not obtain MLS
  data, certify a listing, or make an offer recommendation.
- A source result is time-bound. The same address can change when a county,
  district, FEMA, route, or public report changes.
- `unavailable` means the tool does not have support for a field. It never
  means “probably fine.”
- A review task is not optional prose. It identifies the next fact that can
  materially change the decision.

## Parcel record and utilities

### The Los Angeles County roll is not a complete house record

The public [Los Angeles County parcel layer](https://public.gis.lacounty.gov/public/rest/services/LACounty_Cache/LACounty_Parcel/MapServer/0/query)
does **not** provide owner name, formal lot size, roof type, heat type,
last-sale price, or utility fields. A geometry-area calculation is not a
substitute for a recorded lot-size attribute, and an assessed value is not a
sale price.

Consequences:

- The engine must not show an owner, roof, heat/HVAC, lot size, or sale-history
  value as though the assessor supplied it.
- Water and sewer are `unavailable`, not assumed public. Accordingly,
  `require_public_water_sewer` is off in the buyer profile; an address still
  requires direct utility verification before it can clear that decision.
- Parcel matching may return no record or a nearby record when address
  normalization, condominium structure, or GIS geometry does not align. The
  output must preserve the match evidence and distance.

Use the county record as a starting point, then verify material facts with the
listing disclosure, inspection, title work, utility provider, and buyer's
professionals.

## California property tax

### A seller bill is not a buyer bill

California Proposition 13 starts a new base-year assessment at the purchase
price after a change in ownership and limits later annual assessed-value growth
to 2%. A listing's current tax therefore reflects the seller's frozen base
rather than the buyer's market-price base; for an ordinary purchase-price
scenario it is biased low, not an equally likely high-or-low error. See the
[California Board of Equalization Proposition 13 guidance](https://www.boe.ca.gov/proptaxes/proposition-13.htm).

The engine uses a purchase-price scenario and a parcel-specific Tax Rate Area
(TRA). The [Los Angeles County TRA lookup](https://auditor.lacounty.gov/tax-rate-area-lookup/)
is authoritative for the parcel, while the published county rate report is a
point-in-time reference. A missing or unverified TRA must remain a blocking tax
review, not a default county rate.

### Regular bill, supplemental bill, and exemption are separate questions

A change in ownership can create a prorated supplemental assessment in addition
to the regular secured bill; read the [Board of Equalization supplemental
assessment guidance](https://www.boe.ca.gov/proptaxes/supplemental-assessment/).
The homeowners' exemption is a buyer filing and does not transfer from the
seller. The full $7,000 exemption requires a timely claim by February 15;
[the Board's homeowner guidance](https://boe.ca.gov/pdf/pub800-6.pdf) explains
eligibility and late-claim treatment. The engine cannot decide eligibility,
filing, closing date, or the county's final bill.

## School data: useful signal, not assignment or causal effect

### Boundary names are inferred

The [LAUSD attendance-boundary map service](https://maps.lacity.org/lahub/rest/services/LAUSD_Schools/MapServer)
publishes boundary layers with numeric keys rather than school names. The engine
first measures a coordinate against a polygon and then spatially joins that key
to a separate school-point layer. The resulting school name is `derived` from
two public layers; it is not a measured enrollment assignment. A boundary is
board policy and may be redrawn.

### Zone of Choice is one-to-many

[LAUSD Zone of Choice](https://zoc.lausd.org/) can make an address eligible for
more than one campus. At the Keck anchor, the high-school result is Theodore
Roosevelt Senior High School and Felicitas and Gonzalo Mendez High School. The
engine reports the eligible-campus mean only as a comparison summary and keeps
the campus list. It does not predict a lottery result, capacity, transportation,
or enrollment outcome.

### CAASPP extraction is intentionally fragile and monitored

The [CAASPP public report](https://caaspp-elpac.ets.org/caaspp/) is currently
server-rendered HTML. The station extracts proficiency with a regular expression
against specific element identifiers. An ETS redesign, changed identifier,
suppression, or malformed page can break that method. The mitigation is a live
parser-contract test plus stored fixtures: failure must return `unavailable`
and a review task, never zero proficiency or a silently stale value.

CAASPP describes students currently enrolled at the school. Its outcomes are
strongly associated with household income and do **not** identify the effect of
the school on a child who transfers in. Treat it as a contextual preference
signal, not as a causal ranking or a promise of an individual outcome.

### ZIP and district are not interchangeable

Several target ZIP codes span more than one district and require an
address-level boundary check: **90022, 91754, 91755, 91770, and 91776**.
**91804** is provisional rather than a settled district assignment. Published
[LAUSD boundary data](https://maps.lacity.org/lahub/rest/services/LAUSD_Schools/MapServer)
and [California School Dashboard / CAASPP reporting](https://www.caschooldashboard.org/)
are district and school evidence, not a ZIP-to-school contract.

Garvey, Rosemead, and El Monte City are elementary-only districts. Their
students continue into separate union high-school districts, so one address can
have an elementary district and a different secondary district. Do not collapse
that into a single district label.

### School preference conflicts with affordability

The strongest measured district screen is not automatically an affordable
screen. The documented market snapshot places South Pasadena's 91030 median at
$1.92 million while the project's comparison range is about $1.15–$1.175
million; both figures come from the same [Redfin Data Center market series](https://www.redfin.com/news/data-center/).
The engine must show this tension rather than select a favorable school or
price conclusion by default.

## Hazards and insurance

The [FEMA National Risk Index](https://hazards.fema.gov/nri/) reports broad
risk metrics and percentiles. The California profile surfaces wildfire,
earthquake, drought, heat-wave, and landslide signals as caveats where FEMA
publishes them. It does not determine parcel-level insurability, actual loss,
fire-hardened status, flood-zone requirements, earthquake exposure, or premium.

Insurance is an underwriting question. The tool cannot quote coverage or infer
availability from a FEMA percentile. Obtain property-specific quotes and verify
carrier requirements before treating an estimated monthly cost as executable.

## Commute, routing, and time

A route is an estimate against a selected origin, destination, departure window,
and routing source. It cannot guarantee a particular weekday arrival. Road
closures, hospital parking, shift change, crashes, construction, and future
traffic patterns can dominate the route. Test the commute in the buyer's actual
arrival window before making a location decision.

## Broadband

Availability and technology records can be stale, address-normalized
incorrectly, incomplete for multi-unit buildings, or unavailable from a public
source. A result is evidence about reported service, not a speed test, contract
quote, installation commitment, or building-wiring inspection.

## Market context

Published aggregate market measures are context, not listing evidence. They do
not establish a property's asking price, condition, concessions, days on market,
or negotiation leverage. The project uses public market aggregates only within
the [Redfin Data Center terms and releases](https://www.redfin.com/news/data-center/);
it does not collect listing-page or MLS facts.

## Financial and legal boundaries

The cost schedule is a planning model. It is not a lender approval, closing
disclosure, tax return, appraisal, insurance binder, inspection, title report,
or legal opinion. A buyer must confirm current loan terms, closing costs, tax,
insurance, utilities, title, condition, and enrollment with the appropriate
professional or agency.

## Maintenance rule

When a source changes, add the failure mode here before adding a fallback. A
fallback is acceptable only when it preserves the provenance label and makes the
loss of certainty visible. “Looks plausible” is not a fallback criterion.
