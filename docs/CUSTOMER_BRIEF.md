# Customer Brief — Los Angeles County purchase decision

## Purpose

Compare Los Angeles County homes as decision cases, not as listings. The tool
should make a buyer's monthly-cost pressure, commute trade-off, property-data
holes, hazard caveats, and school uncertainty visible before emotional momentum
turns them into an assumption.

## Fixed context

| Item | Decision context |
| --- | --- |
| Work anchor | Keck Hospital of USC, 1500 San Pablo St, Los Angeles, CA 90033 |
| Weekday arrival | 06:30–07:00 |
| Geography | Los Angeles County candidates, with address-level checks where a ZIP crosses a district boundary |
| Tax jurisdiction | California Proposition 13, using a parcel-specific Los Angeles County TRA |
| School evidence | LAUSD boundary / choice geography plus CAASPP public reports |

The commute is a planning estimate, not a promise about a particular weekday's
traffic. The address and arrival window are buyer inputs, not public-source
claims.

## Decision questions

1. Can the household carry the buyer-side payment under the configured profile?
2. Does the weekday commute to the Keck anchor fit the arrival window without
   pretending that a routing estimate is a guarantee?
3. Does the assessor record support the claimed property facts, or does it
   leave a decision-relevant field unavailable?
4. Does the parcel's verified TRA support a Proposition 13 estimate based on
   the purchase scenario rather than the seller's frozen assessment?
5. What do school-boundary, choice, and CAASPP records actually support—and
   what do they not support?
6. Which hazards, insurance questions, and missing data require a human before
   an offer decision?

## School-data posture

School evidence is a preference signal with hard limits:

- An LAUSD boundary polygon does not itself name a school. The resulting
  boundary-to-school join is a derived inference from separate public layers.
- Zone of Choice can return a set of campuses rather than an assignment. The
  buyer must verify eligibility, capacity, lotteries, transportation, and
  enrollment directly with the district.
- CAASPP proficiency describes currently enrolled students. It is not a
  prediction of a transferred child's experience or a causal measure of school
  effect. [CAASPP reports](https://caaspp-elpac.ets.org/caaspp/) remain the
  cited evidence, while [LAUSD Zone of Choice](https://zoc.lausd.org/) remains
  the policy check.

A strong school screen can conflict with the affordability screen. The market
comparison is meant to expose that trade-off rather than force a false
compromise; see [Known limitations](KNOWN_LIMITATIONS.md).

## Non-negotiable evidence rules

- Unknown is a valid result. The tool must not convert an absent water/sewer,
  lot-size, roof, HVAC, owner, or sale-history field into a favorable default.
- Public water/sewer is not an automated hard constraint because the Los
  Angeles County parcel roll does not supply a utility field. It remains a
  buyer verification item.
- A current property-tax bill is not the buyer's annual tax estimate after a
  California sale. The model starts from the scenario purchase price and a
  verified TRA.
- The score may organize trade-offs; it cannot waive a blocking tax, utility,
  enrollment, inspection, title, or insurance question.

## Out of scope

The tool does not source listings, obtain MLS information, determine title or
zoning rights, quote insurance, guarantee school enrollment, forecast rates or
prices, or give legal, tax, lending, or investment advice.

## Source basis

- [Los Angeles County Assessor parcel layer](https://public.gis.lacounty.gov/public/rest/services/LACounty_Cache/LACounty_Parcel/MapServer/0/query)
- [Los Angeles County Auditor-Controller TRA lookup](https://auditor.lacounty.gov/tax-rate-area-lookup/)
- [LAUSD school and attendance-boundary map service](https://maps.lacity.org/lahub/rest/services/LAUSD_Schools/MapServer)
- [CAASPP public reports](https://caaspp-elpac.ets.org/caaspp/)
