"""Station B — fibre availability, and a lesson in what "available" means.

Two hard truths shape this station, and both are permanent rather than temporary.

**First: the FCC's National Broadband Map API requires credentials.** Unauthenticated
requests come back 401. There is no free anonymous tier. So without a key this station
degrades — cleanly, loudly, with an instruction for how to fix it — rather than silently
reporting no fibre and quietly deducting 15 points from every house in the county.

**Second: even with a key, "available" is a claim, not a connection.** In FCC Broadband
Data Collection filings, a provider reporting service at a location asserts it could
complete a standard installation within ten business days of a request. That is a
statement about intent and capability from a party with an incentive to overstate both.

Which is why the verification task this station emits is **permanent and blocking**. It
is not a stopgap waiting for better data. There is no dataset that replaces phoning the
ISP with the exact street address, because the thing being verified is a promise.

Address-level data needs a licensed Broadband Serviceable Location Fabric subscription,
so the free path goes through the census block GEOID that station G already retrieved:

    address -> census block GEOID (station G) -> block-level availability (here)

Block-level means a neighbour's fibre can make the whole block look served. The precision
field says `census_block` on every value for exactly that reason.
"""

from __future__ import annotations

from ..core.provenance import estimated, unavailable
from . import http
from .base import Context, Station, StationResult

FCC_AVAILABILITY = "https://broadbandmap.fcc.gov/nbm/map/api/national/availability"
FCC_DOC = "https://www.fcc.gov/BroadbandData"
FCC_TEN_DAY_RULE = "https://docs.fcc.gov/public/attachments/DOC-400675A1.txt"
FABRIC_NOTE = "https://www.fcc.gov/sites/default/files/Availability-Challenge-Starter-Kit.pdf"

FIBRE_CODES = {50}  # BDC technology code 50 = Fiber to the Premises
MIN_DOWN_MBPS = 100

BLOCK_PRECISION_NOTE = (
    "census-block level, not address level — a served neighbour can make an unserved "
    "house look covered. Address-level data requires a licensed FCC Fabric subscription"
)
CLAIM_NOTE = (
    "FCC 'available' means the provider claims it could complete a standard install "
    "within 10 business days — a capability claim, not a verified connection"
)


class BroadbandStation(Station):
    name = "broadband"
    provides = ("fiber_available",)
    description = "FCC Broadband Data Collection availability, by census block"

    def __init__(self, api_key: str | None = None) -> None:
        self.api_key = api_key

    def skip_reason(self, ctx: Context) -> str | None:
        base = super().skip_reason(ctx)
        if base:
            return base
        key = self.api_key or ctx.api_keys.get("fcc")
        if not key:
            return (
                "no FCC API key configured — the National Broadband Map has no anonymous "
                "tier. Set FCC_API_KEY to enable this station; fibre stays unknown without it"
            )
        if not ctx.census_block_geoid:
            return (
                "no census block GEOID — the free path to FCC data runs through the "
                "Census geocoder, which did not return one for this address"
            )
        return None

    def fetch(self, ctx: Context) -> StationResult:
        key = self.api_key or ctx.api_keys["fcc"]
        url = http.build_url(
            FCC_AVAILABILITY,
            {
                "geography_type": "block",
                "geography_id": ctx.census_block_geoid,
                "technology_type": "fixed",
                "format": "json",
            },
        )
        payload = http.get_json(url, headers={"username": "api", "hash_value": key}).data

        records = payload.get("data") or payload.get("results") or []
        if not records:
            return self._no_record(ctx)

        providers = []
        for record in records:
            tech = record.get("technology_code") or record.get("tech_code")
            down = record.get("max_advertised_download_speed") or record.get("max_down")
            try:
                tech_int, down_float = int(tech), float(down)
            except (TypeError, ValueError):
                continue
            providers.append(
                {
                    "name": record.get("brand_name") or record.get("provider_name") or "unknown",
                    "technology_code": tech_int,
                    "down_mbps": down_float,
                    "up_mbps": record.get("max_advertised_upload_speed"),
                    "is_fiber": tech_int in FIBRE_CODES,
                }
            )

        fibre = [p for p in providers if p["is_fiber"] and p["down_mbps"] >= MIN_DOWN_MBPS]
        available = bool(fibre)
        names = sorted({p["name"] for p in fibre}) or sorted({p["name"] for p in providers})

        note = f"{BLOCK_PRECISION_NOTE}. {CLAIM_NOTE}"
        return StationResult(
            station=self.name,
            facts={"fiber_available": available},
            values={
                "fiber_available": estimated(
                    available, FCC_DOC, note=note, precision="census_block"
                ),
                "reporting_providers": estimated(
                    names, FCC_DOC, note=CLAIM_NOTE, precision="census_block"
                ),
                "provider_detail": estimated(
                    providers, FCC_DOC, note=note, precision="census_block"
                ),
            },
            tasks=[self._call_task(names)],
        )

    # -- degraded paths ------------------------------------------------------

    def _no_record(self, ctx: Context) -> StationResult:
        note = (
            f"No FCC filing found for census block {ctx.census_block_geoid}. Absence of a "
            f"filing is not evidence of no service — it can mean no provider reported."
        )
        return StationResult(
            station=self.name,
            facts={"fiber_available": None},
            values={"fiber_available": unavailable(note)},
            tasks=[self._call_task([])],
        )

    def _call_task(self, provider_names: list[str]) -> dict:
        who = ", ".join(provider_names) if provider_names else "every local ISP"
        return self.task(
            f"Call {who} with the exact street address and confirm a serviceable fibre "
            f"install, with a date",
            blocking=True,
            reason=f"{CLAIM_NOTE}. Address-level verification requires a Fabric licence "
                   f"({FABRIC_NOTE})",
        )
