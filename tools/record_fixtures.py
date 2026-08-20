"""Record real API responses as test fixtures.

Run this when a source's response shape needs re-capturing:

    python tools/record_fixtures.py "2475 Verde St, Los Angeles, CA 90033"

Tests must never hit the network — a test suite that fails when a government server has a
bad afternoon teaches you nothing. But fixtures also must not be hand-written, because
hand-written fixtures encode what you *assumed* the API returns. So they are recorded
from the real thing and committed.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from analyzer.sources import commute, flood, geocode, http, parcel, risk, schools  # noqa: E402
from analyzer.core.profile import load_profile  # noqa: E402

OUT = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "responses"


def save(name: str, payload: object) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / f"{name}.json").write_text(json.dumps(payload, indent=1) + "\n")
    print(f"  wrote {name}.json")


def main(address: str) -> int:
    profile = load_profile()
    print(f"recording for: {address}")

    census = http.get_json(
        http.build_url(
            geocode.CENSUS_URL,
            {
                "address": address,
                "benchmark": "Public_AR_Current",
                "vintage": "Current_Current",
                "format": "json",
            },
        )
    ).data
    save("census_geocode", census)

    match = census["result"]["addressMatches"][0]
    lon, lat = match["coordinates"]["x"], match["coordinates"]["y"]

    save(
        "nfhl_flood_zone",
        http.get_json(
            http.build_url(
                flood.NFHL_QUERY,
                {
                    "geometry": f"{lon},{lat}",
                    "geometryType": "esriGeometryPoint",
                    "inSR": 4326,
                    "spatialRel": "esriSpatialRelIntersects",
                    "outFields": "FLD_ZONE,ZONE_SUBTY,SFHA_TF,DFIRM_ID,STATIC_BFE",
                    "returnGeometry": "false",
                    "f": "json",
                },
            )
        ).data,
    )

    save(
        "parcel_assessor",
        http.get_json(
            http.build_url(
                parcel.ASSESSOR_QUERY,
                {
                    "geometry": f"{lon},{lat}",
                    "geometryType": "esriGeometryPoint",
                    "inSR": 4326,
                    "spatialRel": "esriSpatialRelIntersects",
                    "distance": parcel.SEARCH_RADIUS_M,
                    "units": "esriSRUnit_Meter",
                    "outFields": parcel.PARCEL_FIELDS,
                    "returnGeometry": "false",
                    "f": "json",
                },
            )
        ).data,
    )

    def nri(lon_: float, lat_: float, hazards: tuple[str, ...]) -> object:
        nri_fields = list(risk.BASE_FIELDS) + [
            f"{code}_{suffix}" for code in hazards for suffix in risk.HAZARD_SUFFIXES
        ]
        return http.get_json(
            http.build_url(
                risk.NRI_QUERY,
                {
                    "geometry": f"{lon_},{lat_}",
                    "geometryType": "esriGeometryPoint",
                    "inSR": 4326,
                    "spatialRel": "esriSpatialRelIntersects",
                    "outFields": ",".join(nri_fields),
                    "returnGeometry": "false",
                    "f": "json",
                },
            )
        ).data

    save("nri_tract", nri(lon, lat, profile.hazards))

    # The school station needs three fixtures per level, and the boundary polygons are the
    # reason: the attendance-boundary records carry numeric keys and no school name, so the
    # only way to a name is a spatial join, and the only way to test that join offline is to
    # record both halves of it. The high-school polygon runs to hundreds of vertices, which
    # is also why that request is a POST -- it does not fit in a query string.
    for level, layer in schools.BOUNDARY_LAYERS.items():
        rings = http.get_json(
            http.build_url(
                f"{schools.BOUNDARY_SERVICE}/{layer}/query",
                {
                    "geometry": f"{lon},{lat}",
                    "geometryType": "esriGeometryPoint",
                    "inSR": 4326,
                    "outSR": 4326,
                    "spatialRel": "esriSpatialRelIntersects",
                    "outFields": "*",
                    "returnGeometry": "true",
                    "f": "json",
                },
            )
        ).data
        save(f"lausd_boundary_{level}", rings)

    # Paradise, California - tract 06007002100. Recorded deliberately as a second fixture
    # because it is the case that proves ratings and percentiles are different scales:
    # wildfire percentile 95.2, wildfire rating "Relatively Moderate", and a composite
    # rating of "Relatively Low" for the town the Camp Fire destroyed. A Spartanburg
    # fixture alone cannot catch a regression that reintroduces label-based scoring.
    # The hazard list is the California one, not this profile's, because the point of the
    # fixture is to exercise a fork's configuration as well as its geography.
    save(
        "nri_tract_wildfire",
        nri(-121.6219, 39.7596, ("WFIR", "ERQK", "DRGT", "HWAV", "LNDS")),
    )

    anchor = profile.primary_anchor
    save(
        "osrm_route",
        http.get_json(
            f"{commute.OSRM_BASE}/{lon},{lat};{anchor.lon},{anchor.lat}"
            f"?overview=false&alternatives=false"
        ).data,
    )

    print("done")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1] if len(sys.argv) > 1 else "2475 Verde St, Los Angeles, CA 90033"))
