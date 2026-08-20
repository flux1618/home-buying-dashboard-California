"""Station F — FEMA flood zone. One of the three hard fails.

Queries the National Flood Hazard Layer directly at the point. A Special Flood Hazard
Area means mandatory flood insurance on a federally backed mortgage, so this is a
disqualifier at any price, not a deduction.

The one subtlety worth knowing: **no polygon returned does not mean no flood risk.**
Large parts of the country simply have not been mapped. An unmapped point and a Zone X
point look identical if you only check whether the feature list is empty, so this station
distinguishes them explicitly — unmapped comes back as unknown, which pins the score to
50 and raises a question, rather than as a silent pass.
"""

from __future__ import annotations

from ..core.provenance import measured, unavailable
from . import http
from .base import Context, Station, StationResult

NFHL_QUERY = (
    "https://hazards.fema.gov/arcgis/rest/services/public/NFHL/MapServer/28/query"
)
NFHL_DOC = "https://msc.fema.gov/portal/home"

# Zones beginning with A or V are Special Flood Hazard Areas.
SFHA_PREFIXES = ("A", "V")


def is_sfha(zone: str | None) -> bool:
    return bool(zone) and zone.strip().upper().startswith(SFHA_PREFIXES)


class FloodStation(Station):
    name = "flood"
    provides = ("flood_zone",)
    description = "FEMA National Flood Hazard Layer, queried at the point"

    def fetch(self, ctx: Context) -> StationResult:
        url = http.build_url(
            NFHL_QUERY,
            {
                "geometry": f"{ctx.lon},{ctx.lat}",
                "geometryType": "esriGeometryPoint",
                "inSR": 4326,
                "spatialRel": "esriSpatialRelIntersects",
                "outFields": "FLD_ZONE,ZONE_SUBTY,SFHA_TF,DFIRM_ID,STATIC_BFE",
                "returnGeometry": "false",
                "f": "json",
            },
        )
        payload = http.get_json(url).data
        if "error" in payload:
            raise http.SourceUnavailable(str(payload["error"].get("message", "ArcGIS error")))

        features = payload.get("features", [])
        if not features:
            return self._unmapped()

        attrs = features[0].get("attributes", {})
        zone = (attrs.get("FLD_ZONE") or "").strip() or None
        if not zone:
            return self._unmapped()

        subtype = (attrs.get("ZONE_SUBTY") or "").strip() or None
        firm = (attrs.get("DFIRM_ID") or "").strip() or None
        bfe = attrs.get("STATIC_BFE")
        sfha = is_sfha(zone)

        note_parts = [subtype] if subtype else []
        if firm:
            note_parts.append(f"FIRM panel {firm}")
        note = "; ".join(note_parts) or None

        values = {
            "flood_zone": measured(zone, NFHL_DOC, note=note, precision="point_in_polygon"),
            "in_special_flood_hazard_area": measured(sfha, NFHL_DOC),
        }
        if bfe is not None and bfe > -9000:
            values["base_flood_elevation_ft"] = measured(bfe, NFHL_DOC)

        tasks = []
        if sfha:
            tasks.append(
                self.task(
                    f"Zone {zone} is a Special Flood Hazard Area — flood insurance is "
                    f"mandatory on a federally backed mortgage. Get a quote before "
                    f"considering an offer",
                    blocking=True,
                )
            )
        else:
            tasks.append(
                self.task(
                    f"Confirm zone {zone} on the seller's elevation certificate or the "
                    f"FEMA Map Service Center",
                    blocking=False,
                    reason="digital NFHL can lag a LOMA or a map revision",
                )
            )

        return StationResult(
            station=self.name, facts={"flood_zone": zone}, values=values, tasks=tasks
        )

    def _unmapped(self) -> StationResult:
        """No polygon at this point. Unknown, emphatically not 'safe'."""
        note = (
            "No NFHL polygon intersects this point. This means the area is unmapped, "
            "NOT that it is outside a flood hazard area — the two are different and "
            "only one of them is good news."
        )
        return StationResult(
            station=self.name,
            facts={"flood_zone": None},
            values={"flood_zone": unavailable(note)},
            tasks=[
                self.task(
                    "Flood zone is unmapped in the NFHL — check the FEMA Map Service "
                    "Center and the county floodplain administrator before an offer",
                    blocking=True,
                    reason="unmapped is not the same as low risk",
                )
            ],
        )
