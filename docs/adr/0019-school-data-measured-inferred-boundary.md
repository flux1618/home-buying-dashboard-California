# ADR 0019 — Keep school data's measured-versus-inferred boundary explicit

**Status:** Accepted  
**Date:** 2026-08-20  
**Related:** ADR 0002 (pure core), ADR 0006 (station contract), ADR 0013 (cross-runtime parity)

## Context

The provenance model labels every output value as `measured`, `derived`,
`estimated`, `extracted`, or `unavailable`. That vocabulary is useful only if
the school station does not silently promote a convenient inference into a
measurement.

Los Angeles County school data creates three such boundaries.

1. LAUSD's attendance-boundary layers expose numeric keys but no school-name
   field. A coordinate can be measured against a polygon, but mapping that
   numeric key to a school name requires a spatial join to a separate LAUSD
   school-point layer. The join is an inference. It is not a measurement, and
   a boundary polygon is not an enrollment guarantee.
2. LAUSD's Zone of Choice makes an address-to-school relation one-to-many.
   At the Keck Hospital anchor, the high-school boundary resolves to Theodore
   Roosevelt Senior High School and Felicitas and Gonzalo Mendez High School.
   There is no honest single-campus answer.
3. CAASPP school proficiency is presently read from server-rendered HTML with
   a regular expression against named elements, including
   `MainContent_lblDonutElaInnerCnt1` and
   `MainContent_lblDonutMathInnerCnt1`. This is an extraction, not an API
   contract. An ETS page redesign can make the parser fail or, worse, capture
   the wrong field.

The substantive interpretation is constrained too. CAASPP reports the
students currently enrolled at a school. Those outcomes are strongly
associated with household income and are not a measure of what the school
will cause for a child who transfers in. Attendance boundaries are board
policy and can be redrawn.

## Decision

The school station retains the following distinctions in its emitted
provenance and explanation:

| Result | Provenance | Rule |
| --- | --- | --- |
| Coordinate inside an LAUSD polygon | `measured` | The geometry test is directly supported by the published boundary layer. |
| Boundary-key-to-school-name result | `derived` | The separate point-layer spatial join is identified as an inference. |
| School choice set | `derived` | A Zone of Choice result remains a set; it is never collapsed into a promised assignment. |
| CAASPP proficiency value | `extracted` | The value identifies the report URL, report year, school code, and parser dependency. |
| Missing boundary, choice, or CAASPP result | `unavailable` | Missing data is not replaced with a district average or a favorable guess. |

For a one-to-many high-school result, the engine reports the arithmetic mean
of the eligible campus proficiency values and carries the campus list. At the
anchor this means Roosevelt and Mendez, not an invented “assigned school.”
The mean is a comparison aid, not a prediction or a guarantee of access.

The CAASPP parser has a live contract test for the expected elements and
fixtures for known reports. A missing element, malformed percentage, or
unexpected page shape must produce `unavailable` and a review task rather
than zero proficiency or a stale value. The raw report URL and retrieval
timestamp remain in provenance so a parser change can be audited.

The product labels the result as an enrolled-student outcome signal. It does
not call it school quality, school effect, or a forecast for a transferred
child. A buyer must verify enrollment, choice rules, capacity, transportation,
and any later board action with the district.

## Alternatives considered

### Treat the boundary layer as an assignment record

Rejected. The published polygons lack the name needed for that claim, and
LAUSD choice rules make the apparent certainty false.

### Return the first matching school

Rejected. It hides Zone of Choice and makes ordering in a source response
look like policy.

### Score a Zone of Choice by its best campus

Rejected. It creates upside bias. The mean is still only a neutral summary,
so the individual campuses are retained beside it.

### Replace CAASPP HTML parsing with a permanent fixed value

Rejected. It would make the score quietly stale. The fragile extractor is
acceptable only with an explicit failure mode and review signal.

## Consequences

- School outputs are more qualified than parcel or hazard outputs; that is an
  accuracy feature, not a defect.
- A location can receive a useful school comparison while still generating a
  manual enrollment-verification task.
- Boundary redraws, choice-policy changes, parser failures, suppression, and
  missing reports can make a school result unavailable.
- Downstream score consumers must preserve the provenance label and campus
  list rather than display a single school name as settled fact.

## Sources

- LAUSD School and Attendance Boundary map service:
  https://maps.lacity.org/lahub/rest/services/LAUSD_Schools/MapServer
- LAUSD Zone of Choice:
  https://zoc.lausd.org/
- LAUSD Zone of Choice information:
  https://zoc.lausd.org/apps/pages/index.jsp?uREC_ID=4428620&type=d&pREC_ID=2666076
- CAASPP and ELPAC public reports:
  https://caaspp-elpac.ets.org/caaspp/
