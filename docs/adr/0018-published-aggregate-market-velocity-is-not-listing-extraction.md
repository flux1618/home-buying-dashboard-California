# ADR 0018 — Published aggregate market velocity is not listing extraction

**Status:** Accepted  
**Date:** 2026-08-19  
**Related:** [0003](0003-no-listing-or-mls-extraction.md) (listing/MLS boundary), [0001](0001-static-snapshot-plus-local-container.md) (committed snapshots), [0006](0006-source-station-contract.md) (never guess)

## Context

The buyer's buy-or-wait question needs more than a price level. The useful market-pacing
measures are median days on market, active inventory and months of supply, new listings, the
share of listings with a price cut, average sale-to-list ratio, and whether those measures are
changing. The requested geography is Spartanburg County, the corresponding Spartanburg market
scope, and the configured ZIP submarkets.

[Realtor.com Economic Research](https://www.realtor.com/research/data/) publishes a documented,
redistributable county/metro/ZIP inventory series. Its core inventory files provide active
inventory, days on market, new listings, and price-reduction share, but not the requested
months-of-supply or sale-to-list fields. Computing months of supply from a different sales series
would create a mismatched definition and a false sense of completeness.

[Redfin Data Center's Market Tracker](https://www.redfin.com/news/data-center/) publishes the
complete aggregate field set: `MEDIAN_DOM`, `MONTHS_OF_SUPPLY`, `INVENTORY`, `NEW_LISTINGS`,
`PRICE_DROPS`, and `AVG_SALE_TO_LIST`, with monthly period boundaries and geography labels. Its
download terms require attribution and restrict the data to non-commercial use. That constraint
is real for this repository; a data product, resale, or sublicensing use would require a new
source decision rather than a quiet expansion.

ADR 0003 already says aggregate Data Center data is different from extracting Redfin listings.
That distinction needs an executable boundary before an aggregate collector is added.

## Decision

### 1. Use a bounded Redfin aggregate snapshot, with attribution and non-commercial scope

`tools/build_velocity_snapshot.py` streams the public county and ZIP gzip TSVs, retains only
Spartanburg County and the target ZIPs, derives the requested comparisons, and writes
`data/market_velocity.json`. It does not retain a raw archive in the repository. The snapshot
contains the source and raw-download URLs, the required attribution — “Data provided by Redfin,
a national real estate brokerage.” — its period coverage, and the non-commercial/no-resale
licence note.

This repository uses the result only as personal, non-commercial home-buying decision support.
It does not sell, sublicense, redistribute the raw downloads, or operate a commercial market-data
product. Any fork or deployment that changes those facts must stop using this artifact until the
licence position is reviewed.

### 2. The permitted unit is an aggregate published statistic; the forbidden unit is a listing

In scope:

- A published Data Center row for county, metro, or ZIP, and a small snapshot of selected
  aggregate rows.
- Arithmetic over those published aggregates, including same-month year-over-year changes and
  3/6/12-month least-squares slopes.
- A station that reads the committed aggregate snapshot locally and reports its provenance.

Out of scope, permanently under ADR 0003:

- Fetching, parsing, automating, or reconstructing individual Redfin/Zillow listings.
- Listing addresses, photos, descriptions, price histories, seller signals, agent data, or
  listing-level price-cut alerts.
- MLS/IDX access without the licensed relationship required by that feed.

An aggregate’s `PRICE_DROPS` share is permitted because it is a published market statistic, not
a collection of which homes cut their price. It must never become a route to identify, monitor,
or rank individual listings.

### 3. Report observations and derivations separately, and make seasonality explicit

Current published figures are `measured` and name their period ending date. Same-calendar-month
year-over-year deltas, linear slopes, and any future computed months-of-supply figure are
`derived`. An absent published field stays `None`; the station does not substitute a neighboring
ZIP, county, or a calculation from incompatible series.

Month-over-month change is intentionally not presented as a trend. The snapshot calls
same-month year-over-year the decision comparison. It retains 13 monthly observations so a reader
can audit the year-over-year comparison and the 3/6/12-month slopes, while warning that the
shorter slopes still contain seasonality.

### 4. Be exact about the “metro” scope

The public Redfin metro download was unavailable at the tested path. The snapshot therefore
labels its `43900` “metro” entry **county-equivalent market scope**: it repeats the published
Spartanburg County aggregate only because the cited [Census CBSA population-estimate file](https://www2.census.gov/programs-surveys/popest/datasets/2020-2022/metro/totals/cbsa-est2022.csv)
maps the Spartanburg CBSA to that county scope. It is not a separately measured Redfin metro row.
The mapping URL and caveat are carried in the artifact and must be rechecked if OMB delineations
change or a usable public metro file appears.

## Alternatives considered

| Option | Why not |
|---|---|
| Realtor.com core inventory files | Strong source, but lacks two of the required measures; synthesizing them from another series would mix definitions. |
| Redfin listing pages or a listing API without a commercial agreement | Violates ADR 0003’s boundary and/or the source terms. |
| Commit the full Redfin county/ZIP archives | The ZIP archive is very large and would mirror rather than bound a source dataset. |
| Call the archive while analyzing every address | Repeats a large external transfer, weakens reproducibility, and makes a source outage look like a property fact. |
| Treat month-over-month movement as market direction | Seasonal inventory and listing cycles make that claim misleading. |

## Consequences

**Good.** The tool gets the full requested market-pacing set from a published aggregate source,
with a small inspectable artifact, explicit attribution, reproducible calculations, and no
listing/MLS collection.

**Bad.** The newest published period can lag the current date. ZIP observations use trailing
90-day windows, short slopes retain seasonality, and county/ZIP figures do not describe a street
or a specific house. The `metro` label is a disclosed county-equivalent scope, not an independent
metro observation.

**Cost of being wrong.** Using a stale or geographically mismatched aggregate to time an offer
could create false confidence. The station therefore degrades after its documented publication
lag, labels each period, preserves `None` for absent geography, and directs the buyer back to
property-specific due diligence rather than inventing local precision.
