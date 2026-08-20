# Testing

The project separates deterministic code verification from live-source contract
checks. They answer different questions and must not be mixed.

```bash
pytest              # offline correctness and parity suite
pytest -m live      # live source-contract checks
```

The California port baseline is green. The offline suite proves what the code
does for recorded evidence; the live suite tells us whether public sources are
still shaped as the adapters expect.

## Offline suite

The default suite blocks network sockets and uses recorded fixtures. That makes
a red run evidence of a code or fixture contract change, not a temporary county
or public-report outage. It also protects the core-purity rule in
[ADR 0002](adr/0002-pure-scoring-core.md): scoring is local and never imports a
network source adapter.

Offline tests cover, at minimum:

| Contract | Why it matters |
| --- | --- |
| Provenance is preserved from station output through scoring | A value without its source boundary is easy to overstate. |
| Missing assessor fields stay unavailable | LA County's absent utility, roof, heat, owner, lot-size, and sale-history fields must not become defaults. |
| Proposition 13 tax uses the buyer scenario | A frozen seller assessment must not become buyer affordability. |
| A missing TRA produces a blocking task | A county-wide default would hide parcel-specific tax differences. |
| Hazard caveats do not silently alter the score | FEMA tract signals are context, not a parcel verdict. |
| Boundary joins and Zone of Choice preserve a campus set | A school name must not masquerade as guaranteed enrollment. |
| CAASPP parser failure becomes unavailable | A redesigned ETS page must not be read as zero proficiency. |
| Python and browser calculations remain in parity | The same supplied facts must not yield different decisions by entry point. |

### Fixtures are recorded evidence

Record fixtures only from supported public endpoints and keep the source URL
with the response. The ordinary address fixture for the port is:

```bash
python tools/record_fixtures.py "2475 Verde St, Los Angeles, CA 90033"
```

A fixture is not an authority after its source changes. It is a reproducible
record of what the source returned at collection time. Update it deliberately,
review the changed fields, and update [Known limitations](KNOWN_LIMITATIONS.md)
when the source contract itself changes.

## Live suite

Live checks are source-contract alarms, not scoring tests. They should run on a
schedule and before relying on a new source path for a decision. A temporary
outage may skip a check; an unexpected successful response with missing or
renamed fields is a drift signal that needs investigation.

| Source | Contract being watched |
| --- | --- |
| [Los Angeles County parcel layer](https://public.gis.lacounty.gov/public/rest/services/LACounty_Cache/LACounty_Parcel/MapServer/0/query) | Parcel lookup still returns the fields the adapter uses, and absent fields remain absent rather than guessed. |
| [LA County TRA lookup](https://auditor.lacounty.gov/tax-rate-area-lookup/) | Parcel-specific tax-rate evidence remains reachable for tax review. |
| [LAUSD school map service](https://maps.lacity.org/lahub/rest/services/LAUSD_Schools/MapServer) | Boundary and school-point layers still support the documented spatial join. |
| [CAASPP reports](https://caaspp-elpac.ets.org/caaspp/) | Expected report elements still resolve for recorded school identifiers. |
| [FEMA National Risk Index](https://hazards.fema.gov/nri/) | Configured California hazard fields remain interpretable. |
| Route and broadband sources | Response shape still supports the documented precision and unknown behavior. |

Do not retry a source-contract check until it turns green. Repeated retries
turn a changed contract into noise. Capture the response, determine whether the
source is down or changed, then either repair the adapter and fixtures or record
why the output must remain unavailable.

## CI split

Per [ADR 0007](adr/0007-http-service-container-and-split-ci.md), CI keeps two
failure meanings separate:

| Workflow class | A red run means |
| --- | --- |
| Offline / push and pull request | A repository change broke deterministic behavior. |
| Live / scheduled | A public source or its contract may have changed. |

The static page is a separate consumer of the rule snapshot. Its checks must
prove both that the generated data is current and that the browser actually
uses it. Source truth sitting beside a stale browser branch is still a product
bug.
