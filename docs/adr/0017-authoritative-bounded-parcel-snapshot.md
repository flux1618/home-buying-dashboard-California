# ADR 0017 — Authoritative, bounded parcel snapshot

**Status:** Accepted
**Date:** 2026-08-19
**Related:** [0001](0001-static-snapshot-plus-local-container.md) (committed public snapshot), [0002](0002-pure-scoring-core.md) (optional tooling does not enter core), [0006](0006-source-station-contract.md) (no silent fallback)

## Context

The county CAMA record is the best available source for parcel identity, assessed value,
tax district, and recorded sales. The February 2021 ArcGIS Online mirror originally carried
this station because the county host was not reachable from the build environment. On
2026-08-19 the authoritative service answered: its `CAMA_Parcels` FeatureServer reports
ArcGIS 11.5, `maxRecordCount: 2000`, `supportsPagination: true`, and 181,531 parcels.

That correction changes the source decision, but not the repository economics. A full
county refresh at 1,000 rows per page is 182 requests. With the five retained columns, a
full county Parquet is estimated at roughly 2 MB compressed (the 35,171-row candidate
snapshot is 389 KB). A changed binary every night would still add roughly 730 MB of
irreversible Git history per year before metadata and later schema growth. "It fits today"
is not a reason to make every old county correction a permanent clone download.

The buyer profile names a Spartanburg household, anchors the commute at Spartanburg Medical
Center, and states a 20-minute hard limit. It does not name other target cities or school
districts. The artifact must follow that actual scope rather than impersonating a county
archive.

## Decision

### 1. The county service is primary; the 2021 mirror is labelled fallback

`maps.spartanburgcounty.org/.../GIS/CAMA_Parcels/FeatureServer/0` is queried first and its
values are `measured`. The parser keeps distinct field lists because the current server uses
`MAPNUMBER`, `PropertyLocation` / `StreetAddress`, and `PropertyType`; the mirror uses
`TAXPIN`, `PropertyLo`, and `PropertyTy`. One absent ArcGIS `outField` rejects the whole
request, so sharing a field list would turn a successful primary into a false outage.

If the county source cannot answer, the dated mirror remains available as an `estimated`,
partial fallback with a blocking current-assessor verification task. A current value and a
mirror value are not reconciled or averaged. The county value wins while it is available;
a disagreement is evidence of the mirror's vintage, not an uncertainty interval around the
county record.

### 2. Commit a Spartanburg residential candidate index, not all county parcels

`data/parcels.parquet` is scoped to county `City = 'SPARTANBURG'` and `LandUse LIKE
'RESIDENTIAL%'`. This is deliberately a candidate-search artifact for the configured buyer,
not a representation that all possible homes meet the commute constraint. It has five
columns: `tax_pin`, `assessed_value`, `tax_district`, `last_sale_date`, and
`last_sale_amount`.

The live layer has no `TAXPIN` field. The snapshot names `MAPNUMBER` as `tax_pin` and records
that mapping in its sidecar. `assessed_value` is the explicit sum of
`CurrentAssessedLandValue` and `CurrentAssessedBuildingValue`. School attendance zone is
not included: it is neither a CAMA field nor a public school-attendance service in the
county GIS catalog, and assigning one from district-like names would be a guess.

The extractor pages in stable `OBJECTID` order, sends one request at a time with retry and
backoff through the shared HTTP adapter, checkpoints every completed page, and refuses to
publish a partial result. Its sidecar records source URL, service version, retrieval time,
source/output fields, scope, page policy, row count, and any failed pages. It hashes only
the retained rows; an unchanged result does not rewrite the binary merely to refresh a
timestamp, allowing automation to commit only real data changes.

### 3. Escalate rather than expand Git history if scope becomes county-wide

A county-wide request is justified only when the buyer profile itself expands beyond
Spartanburg or a product requirement becomes county-wide. At that point, publish immutable
release assets (preferred for a nightly data product) or use Git LFS with explicit storage,
retention, and clone-cost approval. Do not remove the predicate and continue ordinary Git
commits by default.

## Alternatives considered

| Option | Why not |
|---|---|
| Continue to treat the mirror as primary | It is demonstrably stale, partial, and no longer needed for reachability. |
| Commit all 181,531 parcels nightly | It makes binary history grow permanently for a result outside the configured buyer scope. |
| Use `Districts` as a school attendance zone | The catalog does not document it as an attendance boundary; a familiar label is not evidence. |
| Infer an assessment value from appraised or taxable fields | The live layer provides the assessed land and building fields directly. |
| Reconcile county and mirror discrepancies | There is no shared update time or conflict rule that could make an average meaningful. |

## Consequences

**Good.** The current county record is used when available, the public artifact stays small,
and a county outage cannot manufacture data or invalidate the rest of CI. Schema drift and
mid-run failure are visible rather than silently producing null columns.

**Bad.** A house in a nearby municipality can be within the buyer's actual 20-minute commute
but absent from this static candidate index. Attendance zone remains an explicit unknown.
The API station can still query an individual county parcel live; the committed index is not
its substitute.

**Cost of being wrong.** If the buyer's search area is broader, the scoped predicate must be
revisited with their stated cities/districts, not guessed from a map. If the county changes
field semantics without renaming fields, the snapshot may be internally consistent but
semantically wrong; the live contract test and source-field sidecar make that a named
maintenance task rather than a hidden guarantee.
