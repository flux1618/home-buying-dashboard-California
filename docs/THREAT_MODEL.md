# Threat Model

## Scope

This project is a local-first home-buying decision engine for Los Angeles
County. It accepts an address and buyer-controlled scenario inputs, queries
public sources through bounded stations, and can save an append-only local
ledger. It is not a public account system or a listing platform.

## Assets to protect

| Asset | Why it matters |
| --- | --- |
| Candidate addresses and coordinates | Reveal a household's intended move, commute, and potentially sensitive preferences. |
| Buyer profile and price scenarios | Reveal finances, risk tolerance, and negotiation posture. |
| Saved ledger | Combines locations, decisions, and timestamps into a useful behavioral record. |
| Source provenance and snapshots | Needed to explain a result and detect a stale or changed public source. |
| School-choice and boundary results | Can be misread as an enrollment guarantee if stripped from their provenance. |

## Trust boundaries

| Boundary | Data crossing it | Main risk | Control |
| --- | --- | --- | --- |
| User to local CLI / browser | Address and scenario inputs | Local history, shell history, browser autofill | Keep the service local; do not log raw requests beyond the configured ledger. |
| Local service to public stations | Address or coordinate, parcel query, route endpoints | Source hosts and network observers learn a candidate location | Send only what the source needs; document source use; allow an offline snapshot path where available. |
| Local service to LAUSD map service | Coordinate and boundary-layer query | Municipal map host can associate a location query with an IP address | Query only the candidate coordinate; never send buyer profile or saved ledger. |
| Local service to CAASPP report host | School identifier and report request | Parser changes or public reports are misinterpreted as assignment facts | Send school identifiers, not the buyer address; preserve report URL and mark output `extracted`. |
| Local service to ledger | Decision record and profile fingerprint | Unauthorized local read or silent history rewrite | Append-only records; separate engine output from saved history; protect the host account. |
| Static snapshot to a viewer | Published aggregate data and rules | Snapshot is mistaken for live data | State snapshot date / provenance and keep no buyer-specific data in the site. |

## Threats and mitigations

### Candidate-location disclosure

Geocoding, county parcel lookup, routing, and LAUSD boundary queries can expose
a candidate address or coordinate to their respective public-service hosts.
The CAASPP request follows the derived school identifier and should not receive
the buyer's address. This exposure is accepted only because a useful live
result needs the location; the tool must not forward the buyer profile, saved
ledger, or unrelated personal fields.

### False certainty from school data

An attacker need not compromise a system to cause harm: a clean-looking school
name can be mistaken for a guaranteed assignment. LAUSD polygons require a
separate school-point join, and Zone of Choice can return multiple campuses.
CAASPP describes enrolled students and is parsed from public HTML. The control
is visible provenance, campus lists, `unavailable` on parser failure, and a
manual enrollment task—not a stronger-looking score.

### Stale or manipulated upstream data

Public sources can change, return partial records, throttle requests, or alter
HTML without notice. Stations never raise into the scoring core and never
invent a value. Provenance carries the source URL and retrieval time. A parser
contract test guards CAASPP element changes; failure is an unknown and review
item, not a zero value.

### California tax understatement

A seller's Proposition 13 tax bill can look authoritative while representing a
prior owner's frozen base year. The buyer tax model starts from purchase price
and a verified parcel TRA, then emits a blocking task when that evidence is
missing. This reduces a systematic affordability error but does not substitute
for the county bill or closing disclosure.

### Local ledger exposure or tampering

A local SQLite file is not encrypted merely because it is local. A person with
access to the host account may read it; malware may copy it; a local user may
attempt edits. The ledger is separated from scoring output and append-only at
the application boundary, but filesystem permissions, disk encryption,
backups, and device security remain operator responsibilities.

### Misuse of a decision aid as professional advice

Scores and source excerpts can be copied without their caveats. The interface
and exports must retain blocking tasks, unknowns, source URLs, and the
statement that the tool is not a lender, assessor, district, inspector,
insurer, or attorney.

## Non-goals

- No account identity, advertising profile, or user analytics.
- No MLS or listing-page collection.
- No automatic offer, enrollment, tax filing, or insurance action.
- No claim that an address is entitled to a named school.

## Residual risk

The operator still chooses whether to submit an address to live public sources.
A local tool cannot hide network metadata from those sources. Public parcel,
school, hazard, and tax records can be incomplete or change after retrieval.
The remaining control is honest output: source-linked, time-stamped evidence
and explicit unknowns.

## Source services

- [LA County parcel layer](https://public.gis.lacounty.gov/public/rest/services/LACounty_Cache/LACounty_Parcel/MapServer/0/query)
- [LAUSD school and boundary map service](https://maps.lacity.org/lahub/rest/services/LAUSD_Schools/MapServer)
- [LAUSD Zone of Choice](https://zoc.lausd.org/)
- [CAASPP public reports](https://caaspp-elpac.ets.org/caaspp/)
- [LA County Auditor-Controller TRA lookup](https://auditor.lacounty.gov/tax-rate-area-lookup/)
- [FEMA National Risk Index](https://hazards.fema.gov/nri/)
