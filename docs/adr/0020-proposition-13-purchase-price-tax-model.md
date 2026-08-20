# ADR 0020 — Replace South Carolina millage with a Proposition 13 purchase-price model

**Status:** Accepted  
**Date:** 2026-08-20  
**Related:** ADR 0006 (station contract), ADR 0017 (baseline tax-policy treatment)

## Context

The South Carolina model assessed a fraction of market value and multiplied it
by a millage rate. A listing's stated tax could therefore differ from a buyer's
result in either direction.

That premise does not survive the California port. Under Proposition 13,
assessed value resets to purchase price upon a change in ownership, then grows
by no more than 2% per year. The seller's advertised tax reflects the seller's
frozen base-year assessment. For a market-price purchase, it is systematically
too low as an estimate of the buyer's ongoing base tax; it is not a neutral
listing fact.

The recorded sample parcel illustrates the scale: assessed land plus
improvements total $203,078, while the comparison purchase scenario is
$775,000. That scenario uses the available 90033 median-sale reference rather
than a claimed listing ask. The assessed-to-price ratio is 0.262. Treating the
current roll tax as the buyer's cost would be material understatement, not
rounding error.

Los Angeles County does not apply one county-wide all-in rate. It publishes
tax-rate areas (TRAs): 13,197 for fiscal year 2025–26. The Keck Hospital
anchor is in TRA 00004, whose published total rate is 1.187380%.

## Decision

The tax model uses the scenario purchase price as the starting assessed value,
applies the applicable TRA rate, and labels later years as capped-growth
estimates. It does not use a seller's displayed bill as a buyer estimate.

The parcel station must emit a **BLOCKING** review task when a California
purchase scenario lacks a verified TRA or when the buyer tax cannot be
reconciled to purchase price. The task explains that an assessor roll is a
seller-history record, not a closing-cost quote.

The output separately calls out:

- **Supplemental assessment:** after a sale, the difference between the new
  assessment and the prior assessment can generate a prorated supplemental
  bill in addition to the regular secured bill.
- **Homeowners' exemption:** the $7,000 exemption belongs to an eligible
  owner-occupant who claims it; it does not transfer from the seller. A claim
  filed by February 15 receives the full exemption for the assessment year.
- **TRA uncertainty:** TRA is parcel-specific. TRA 00004 at 1.187380% is the
  anchor rate, not a substitute for a verified rate at every candidate
  address.

The model reports these facts as a planning estimate and a task list. It does
not present a tax calculation as the county's future bill.

## Alternatives considered

### Carry forward listing tax

Rejected. Under Proposition 13 it is biased downward for a buyer after a
market-price sale, precisely when the tool is meant to prevent an affordability
mistake.

### Apply a single 1% statewide rate

Rejected. Proposition 13's 1% general levy omits voter-approved debt and
other TRA components.

### Infer a new tax bill from the seller's assessed-to-listing ratio

Rejected. The ratio describes the seller's tenure, not the buyer's new
assessment. The 0.262 sample ratio demonstrates why it is not portable.

### Treat the homeowners' exemption as already present

Rejected. Eligibility and timely filing are buyer actions, not a property
attribute, and the seller's claim does not carry over.

## Consequences

- California affordability results may be higher than listing portals suggest.
- A verified parcel and TRA are required before a tax result can clear its
  blocking task.
- Supplemental tax and the exemption deadline remain closing workflow items,
  even when the annual estimate is available.
- This ADR intentionally supersedes the South Carolina millage premise for
  California scenarios only; it does not rewrite historical South Carolina
  results.

## Sources

- California State Board of Equalization, Proposition 13:
  https://www.boe.ca.gov/proptaxes/proposition-13.htm
- California State Board of Equalization, supplemental assessments:
  https://www.boe.ca.gov/proptaxes/supplemental-assessment/
- California State Board of Equalization, homeowners' exemption:
  https://boe.ca.gov/pdf/pub800-6.pdf
- Los Angeles County Auditor-Controller, Tax Rate Area lookup:
  https://auditor.lacounty.gov/tax-rate-area-lookup/
- Los Angeles County Auditor-Controller, fiscal year 2025–26 taxpayers'
  guide:
  https://auditor.lacounty.gov/wp-content/uploads/2026/05/2025-2026-Taxpayers-Guide.pdf
- Los Angeles County Auditor-Controller, fiscal year 2025–26 TRA report:
  https://auditor.lacounty.gov/wp-content/uploads/2025/09/Highest-Value-TRA-Report-FY-2025-2026_SORTED.xlsx
- Los Angeles County Parcel layer:
  https://public.gis.lacounty.gov/public/rest/services/LACounty_Cache/LACounty_Parcel/MapServer/0/query
- Redfin Data Center, ZIP Code Market Tracker:
  https://www.redfin.com/news/data-center/
