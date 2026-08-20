"""Fetch and compile the live geographic layers used by the LA static dashboard.

This deliberately leaves the buyer rules and county-wide hazard snapshot alone.  It
records the source URLs next to the compact artifacts under ``data/la_live`` so the
large third-party geometries do not have to be fetched again to audit the snapshot.

Run this script before ``rebuild_la_snapshot.py``:

    python tools/rebuild_la_geospatial.py
"""

from __future__ import annotations

import json
import math
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import requests


REPO = Path(__file__).resolve().parents[1]
OUT = REPO / "data" / "la_live"
OUT.mkdir(parents=True, exist_ok=True)

ZIPS = [
    "90031", "90032", "90033", "90022", "90023", "90026", "90040", "90041",
    "90042", "90065", "91030", "91106", "91107", "91754", "91755", "91770",
    "91776", "91801", "91803", "91804", "90640",
]
ANCHOR = (-118.203042269976, 34.062379537908)  # lon, lat
STEP = 0.025
# 21 * 25 = 525 routing samples, centered broadly around the target ZIP set.
GRID_LATS = [round(33.85 + STEP * i, 3) for i in range(21)]
GRID_LONS = [round(-118.50 + STEP * i, 3) for i in range(25)]
BBOX = (-118.50, 33.85, -117.875, 34.375)  # xmin, ymin, xmax, ymax
HEADERS = {"User-Agent": "hbd-ca LA dashboard source builder (public data use)"}

CENSUS_ZCTA_URL = (
    "https://tigerweb.geo.census.gov/arcgis/rest/services/TIGERweb/"
    "PUMA_TAD_TAZ_UGA_ZCTA/MapServer/1/query"
)
CENSUS_SCHOOL_URL = (
    "https://tigerweb.geo.census.gov/arcgis/rest/services/TIGERweb/School/MapServer"
)
FEMA_FLOOD_URL = (
    "https://hazards.fema.gov/arcgis/rest/services/public/NFHL/MapServer/28/query"
)
OSRM_URL = "https://router.project-osrm.org/table/v1/driving/"
OVERPASS_URL = "https://overpass-api.de/api/interpreter"
OVERPASS_FALLBACK_URL = "https://overpass.kumi.systems/api/interpreter"


def get_json(url: str, *, params: dict[str, Any], timeout: int = 180) -> Any:
    response = requests.get(url, params=params, headers=HEADERS, timeout=timeout)
    response.raise_for_status()
    payload = response.json()
    if payload.get("error"):
        raise RuntimeError(f"{url}: {payload['error']}")
    return payload


def fetch_geojson(url: str, *, where: str, bbox: tuple[float, ...] | None = None) -> dict[str, Any]:
    params: dict[str, Any] = {
        "f": "geojson",
        "where": where,
        "outFields": "*",
        "returnGeometry": "true",
        "outSR": "4326",
        "geometryPrecision": "5",
    }
    if bbox:
        params.update(
            {
                "geometry": ",".join(map(str, bbox)),
                "geometryType": "esriGeometryEnvelope",
                "spatialRel": "esriSpatialRelIntersects",
            }
        )
    data = get_json(url, params=params)
    if data.get("type") != "FeatureCollection":
        raise RuntimeError(f"Expected GeoJSON FeatureCollection from {url}")
    return data


def centroid(geometry: dict[str, Any]) -> list[float]:
    """Use the TIGER interior-point fields when possible; this is a fallback."""
    coords: list[tuple[float, float]] = []

    def visit(value: Any) -> None:
        if not value:
            return
        if isinstance(value[0], (float, int)):
            coords.append((float(value[0]), float(value[1])))
        else:
            for child in value:
                visit(child)

    visit(geometry["coordinates"])
    return [round(sum(y for _, y in coords) / len(coords), 6), round(sum(x for x, _ in coords) / len(coords), 6)]


def _point_line_distance(point: list[float], start: list[float], end: list[float]) -> float:
    """Planar distance in degrees; sufficient for compact client-side map geometry."""
    px, py = point
    x1, y1 = start
    x2, y2 = end
    dx, dy = x2 - x1, y2 - y1
    if dx == 0 and dy == 0:
        return math.hypot(px - x1, py - y1)
    return abs(dy * px - dx * py + x2 * y1 - y2 * x1) / math.hypot(dx, dy)


def simplify_ring(ring: list[list[float]], tolerance: float = 0.00008) -> list[list[float]]:
    """Douglas-Peucker simplification, retaining closed valid-enough display rings."""
    if len(ring) <= 5:
        return ring
    points = ring[:-1] if ring[0] == ring[-1] else ring[:]
    if len(points) < 4:
        return ring

    def reduce(items: list[list[float]]) -> list[list[float]]:
        if len(items) <= 2:
            return items
        maximum, index = 0.0, 0
        for i in range(1, len(items) - 1):
            distance = _point_line_distance(items[i], items[0], items[-1])
            if distance > maximum:
                maximum, index = distance, i
        if maximum > tolerance:
            return reduce(items[: index + 1])[:-1] + reduce(items[index:])
        return [items[0], items[-1]]

    # A ring has no natural endpoint. Split at a far-apart vertex to avoid treating a
    # whole perimeter as a nearly straight start-to-end segment.
    pivot = max(range(1, len(points)), key=lambda i: math.hypot(points[i][0] - points[0][0], points[i][1] - points[0][1]))
    simplified = reduce(points[: pivot + 1])[:-1] + reduce(points[pivot:] + [points[0]])[:-1]
    if len(simplified) < 3:
        simplified = points[:]
    return simplified + [simplified[0]]


def simplify_geometry(geometry: dict[str, Any]) -> dict[str, Any]:
    geometry = json.loads(json.dumps(geometry))  # do not mutate a response object
    if geometry["type"] == "Polygon":
        geometry["coordinates"] = [simplify_ring(ring) for ring in geometry["coordinates"]]
    elif geometry["type"] == "MultiPolygon":
        geometry["coordinates"] = [
            [simplify_ring(ring) for ring in polygon] for polygon in geometry["coordinates"]
        ]
    return geometry


def compact_zips() -> dict[str, Any]:
    quoted = ",".join(f"'{zip_code}'" for zip_code in ZIPS)
    raw = fetch_geojson(CENSUS_ZCTA_URL, where=f"ZCTA5 IN ({quoted})")
    found = {f["properties"]["ZCTA5"] for f in raw["features"]}
    missing = sorted(set(ZIPS) - found)
    # 91804 is a valid USPS ZIP but has no Census ZCTA.  Do not silently substitute it.
    if missing != ["91804"]:
        raise RuntimeError(f"Unexpected missing TIGER ZCTAs: {missing}")
    features = []
    for feature in raw["features"]:
        p = feature["properties"]
        lat = float(p.get("INTPTLAT") or 0)
        lon = float(p.get("INTPTLON") or 0)
        features.append(
            {
                "type": "Feature",
                "properties": {
                    "zip": p["ZCTA5"],
                    "name": f"ZCTA {p['ZCTA5']}",
                    "centroid": [round(lat, 6), round(lon, 6)] if lat and lon else centroid(feature["geometry"]),
                    "source_url": CENSUS_ZCTA_URL,
                },
                "geometry": simplify_geometry(feature["geometry"]),
            }
        )
    return {"type": "FeatureCollection", "features": sorted(features, key=lambda f: f["properties"]["zip"])}


def compact_districts() -> dict[str, Any]:
    features: list[dict[str, Any]] = []
    kinds = [(0, "unified"), (1, "secondary"), (2, "elementary")]
    for layer, kind in kinds:
        # TIGERweb's School service accepts the state predicate but, unlike its ZCTA
        # service, returns an empty set for a geographic envelope.  Fetch California
        # once and select the local geometries client-side rather than losing districts.
        raw = fetch_geojson(f"{CENSUS_SCHOOL_URL}/{layer}/query", where="STATE='06'")
        for feature in raw["features"]:
            if not geometry_intersects_bbox(feature["geometry"], BBOX):
                continue
            p = feature["properties"]
            features.append(
                {
                    "type": "Feature",
                    "properties": {
                        "name": p["NAME"],
                        "geoid": p["GEOID"],
                        "type": kind,
                        "source_url": f"{CENSUS_SCHOOL_URL}/{layer}/query",
                    },
                    "geometry": simplify_geometry(feature["geometry"]),
                }
            )
    features.sort(key=lambda f: (f["properties"]["type"], f["properties"]["name"]))
    return {"type": "FeatureCollection", "features": features}


def geometry_intersects_bbox(geometry: dict[str, Any], bbox: tuple[float, ...]) -> bool:
    """Fast envelope screen for a GeoJSON geometry without a GIS dependency."""
    xmin, ymin, xmax, ymax = bbox
    seen = False
    lo_x = lo_y = float("inf")
    hi_x = hi_y = float("-inf")

    def visit(value: Any) -> None:
        nonlocal seen, lo_x, lo_y, hi_x, hi_y
        if not value:
            return
        if isinstance(value[0], (float, int)):
            x, y = float(value[0]), float(value[1])
            seen = True
            lo_x, lo_y = min(lo_x, x), min(lo_y, y)
            hi_x, hi_y = max(hi_x, x), max(hi_y, y)
            return
        for child in value:
            visit(child)

    visit(geometry["coordinates"])
    return seen and not (hi_x < xmin or lo_x > xmax or hi_y < ymin or lo_y > ymax)


def compact_flood() -> dict[str, Any]:
    raw = fetch_geojson(FEMA_FLOOD_URL, where="FLD_ZONE NOT IN ('X', 'D')", bbox=BBOX)
    features = []
    for feature in raw["features"]:
        p = feature["properties"]
        zone = p.get("FLD_ZONE")
        features.append(
            {
                "type": "Feature",
                "properties": {
                    "label": f"FEMA Flood Zone {zone}" if zone else "FEMA flood hazard area",
                    "zone": zone,
                    "source_url": FEMA_FLOOD_URL,
                },
                "geometry": simplify_geometry(feature["geometry"]),
            }
        )
    return {"type": "FeatureCollection", "features": features}


def distance_km(lat: float, lon: float) -> float:
    """Haversine distance to the anchor, used only to choose nearby POIs."""
    lon0, lat0 = ANCHOR
    phi1, phi2 = math.radians(lat0), math.radians(lat)
    dphi, dlambda = phi2 - phi1, math.radians(lon - lon0)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 6371.0088 * 2 * math.asin(math.sqrt(a))


def compact_pois() -> tuple[list[dict[str, Any]], str | None]:
    south, west, north, east = BBOX[1], BBOX[0], BBOX[3], BBOX[2]
    query = f"""
[out:json][timeout:180];
(
  nwr["shop"="supermarket"]({south},{west},{north},{east});
  nwr["amenity"~"^(hospital|clinic)$"]({south},{west},{north},{east});
  nwr["highway"="motorway_junction"]({south},{west},{north},{east});
);
out center tags;
"""
    try:
        response = requests.post(OVERPASS_URL, data=query.encode(), headers=HEADERS, timeout=240)
        response.raise_for_status()
        raw = response.json()
    except requests.RequestException as primary_error:
        # The primary shared interpreter occasionally resets long POSTs.  This is a
        # distinct public Overpass mirror, not a blind repeat of the failed request.
        try:
            response = requests.post(OVERPASS_FALLBACK_URL, data=query.encode(), headers=HEADERS, timeout=240)
            response.raise_for_status()
            raw = response.json()
        except requests.RequestException as fallback_error:
            return [], (
                "Overpass primary unavailable "
                f"({type(primary_error).__name__}); fallback unavailable "
                f"({type(fallback_error).__name__})."
            )
    candidates: list[tuple[float, dict[str, Any]]] = []
    seen: set[tuple[float, float, str]] = set()
    for element in raw.get("elements", []):
        tags = element.get("tags", {})
        if tags.get("shop") == "supermarket":
            category = "grocery"
        elif tags.get("amenity") in {"hospital", "clinic"}:
            category = "hospital"
        elif tags.get("highway") == "motorway_junction":
            category = "ramp"
        else:
            continue
        loc = element.get("center") or element
        if "lat" not in loc or "lon" not in loc:
            continue
        lat, lon = round(float(loc["lat"]), 6), round(float(loc["lon"]), 6)
        key = (lat, lon, category)
        if key in seen:
            continue
        seen.add(key)
        name = tags.get("name") or tags.get("ref") or category.title()
        candidates.append((distance_km(lat, lon), {"c": category, "lat": lat, "lon": lon, "n": name}))
    # The old dashboard displayed 228 POIs.  Select the 228 closest live OSM features
    # rather than padding or fabricating locations.
    candidates.sort(key=lambda item: (item[0], item[1]["c"], item[1]["n"]))
    return [poi for _, poi in candidates[:228]], None


def route_points(points: list[tuple[float, float]]) -> list[dict[str, float | None]]:
    """Route each (lon,lat) point to the anchor in bounded OSRM table requests."""
    results: list[dict[str, float | None]] = []
    for start in range(0, len(points), 49):
        chunk = points[start : start + 49]
        coords = [ANCHOR, *chunk]
        path = ";".join(f"{lon:.6f},{lat:.6f}" for lon, lat in coords)
        sources = ";".join(str(i) for i in range(1, len(coords)))
        data = get_json(
            OSRM_URL + path,
            params={"annotations": "duration,distance", "sources": sources, "destinations": "0"},
            timeout=120,
        )
        durations = data.get("durations", [])
        distances = data.get("distances", [])
        if len(durations) != len(chunk):
            raise RuntimeError(f"OSRM returned {len(durations)} rows for {len(chunk)} points")
        for duration_row, distance_row in zip(durations, distances, strict=True):
            duration = duration_row[0] if duration_row else None
            distance = distance_row[0] if distance_row else None
            results.append(
                {
                    "min": round(duration / 60, 1) if duration is not None else None,
                    "mi": round(distance / 1609.344, 1) if distance is not None else None,
                }
            )
        # The public demo server asks callers not to burst large table requests.
        time.sleep(0.25)
    return results


def compact_drivetime() -> dict[str, Any]:
    points = [(lon, lat) for lat in GRID_LATS for lon in GRID_LONS]
    routes = route_points(points)
    grid = [
        {"lat": lat, "lon": lon, "min": route["min"]}
        for (lon, lat), route in zip(points, routes, strict=True)
    ]
    return {
        "anchor": {
            "label": "Keck Hospital of USC, 1500 San Pablo St, Los Angeles CA 90033",
            "lat": ANCHOR[1],
            "lon": ANCHOR[0],
        },
        "step": STEP,
        "grid": grid,
    }


def main() -> None:
    retrieved = datetime.now(UTC).isoformat()
    zips = compact_zips()
    districts = compact_districts()
    flood = compact_flood()
    poi, poi_error = compact_pois()
    drivetime = compact_drivetime()
    bundle = {
        "retrieved_utc": retrieved,
        "sources": {
            "zips": CENSUS_ZCTA_URL,
            "districts": CENSUS_SCHOOL_URL,
            "flood": FEMA_FLOOD_URL,
            "poi": OVERPASS_URL,
            "routing": "https://router.project-osrm.org",
        },
        "zips": zips,
        "districts": districts,
        "flood": flood,
        "poi": poi,
        "poi_error": poi_error,
        "drivetime": drivetime,
    }
    (OUT / "geospatial.json").write_text(json.dumps(bundle, indent=1) + "\n")
    summary = {
        "retrieved_utc": retrieved,
        "zcta_features": len(zips["features"]),
        "zcta_missing": ["91804"],
        "district_features": len(districts["features"]),
        "flood_features": len(flood["features"]),
        "poi": len(poi),
        "poi_error": poi_error,
        "poi_categories": {c: sum(p["c"] == c for p in poi) for c in ("grocery", "hospital", "ramp")},
        "drive_grid": len(drivetime["grid"]),
        "unroutable_grid_points": sum(p["min"] is None for p in drivetime["grid"]),
    }
    (OUT / "summary.json").write_text(json.dumps(summary, indent=1) + "\n")
    print(json.dumps(summary, indent=1))


if __name__ == "__main__":
    main()
