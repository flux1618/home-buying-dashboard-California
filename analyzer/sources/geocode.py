"""Station G — address to coordinates, census block, and county.

The only fatal station. Nothing downstream can run without a latitude and longitude:
flood zone, parcel, and commute are all point queries.

Primary is the Census Bureau geocoder, chosen over Nominatim not because it is more
accurate but because it returns the **census block GEOID** in the same response. That
one field is what makes the broadband station possible without a paid address-level
licence, so a second round trip is avoided for free.

Nominatim is the fallback. It gives coordinates and nothing else, so when it is used the
broadband station degrades — a real dependency chain, made visible rather than hidden.
"""

from __future__ import annotations

from ..core.provenance import estimated, measured
from . import http
from .base import Context, Station, StationResult

CENSUS_URL = "https://geocoding.geo.census.gov/geocoder/geographies/onelineaddress"
CENSUS_DOC = "https://geocoding.geo.census.gov/geocoder/"
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
NOMINATIM_DOC = "https://nominatim.openstreetmap.org/"

BLOCK_KEY = "2020 Census Blocks"


class GeocodeStation(Station):
    name = "geocode"
    provides = ()  # coordinates are context, not scored facts
    fatal = True
    description = "Census Bureau geocoder, falling back to Nominatim"

    def skip_reason(self, ctx: Context) -> str | None:
        return None if ctx.address.strip() else "no address supplied"

    def fetch(self, ctx: Context) -> StationResult:
        try:
            return self._census(ctx)
        except (http.SourceUnavailable, http.SourceRejected, LookupError) as exc:
            fallback = self._nominatim(ctx)
            fallback.tasks.append(
                self.task(
                    "Confirm the address — the Census geocoder did not match it, so "
                    "coordinates came from OpenStreetMap and no census block is known",
                    blocking=False,
                    reason=f"census geocoder unavailable or no match ({exc})",
                )
            )
            return fallback

    # -- primary -------------------------------------------------------------

    def _census(self, ctx: Context) -> StationResult:
        url = http.build_url(
            CENSUS_URL,
            {
                "address": ctx.address,
                "benchmark": "Public_AR_Current",
                "vintage": "Current_Current",
                "format": "json",
            },
        )
        matches = http.get_json(url).data["result"]["addressMatches"]
        if not matches:
            raise LookupError("no address match")

        match = matches[0]
        coords = match["coordinates"]
        geographies = match.get("geographies", {})

        block = (geographies.get(BLOCK_KEY) or [{}])[0].get("GEOID")
        county = (geographies.get("Counties") or [{}])[0].get("GEOID")

        values = {
            "latitude": measured(round(coords["y"], 6), CENSUS_DOC, precision="rooftop_or_range"),
            "longitude": measured(round(coords["x"], 6), CENSUS_DOC, precision="rooftop_or_range"),
            "matched_address": measured(match["matchedAddress"], CENSUS_DOC),
        }
        if block:
            values["census_block_geoid"] = measured(block, CENSUS_DOC, precision="census_block")
        if county:
            values["county_fips"] = measured(county, CENSUS_DOC)

        return StationResult(
            station=self.name,
            values=values,
            context_updates={
                "lat": round(coords["y"], 6),
                "lon": round(coords["x"], 6),
                "matched_address": match["matchedAddress"],
                "census_block_geoid": block,
                "county_fips": county,
            },
        )

    # -- fallback ------------------------------------------------------------

    def _nominatim(self, ctx: Context) -> StationResult:
        url = http.build_url(
            NOMINATIM_URL,
            {"q": ctx.address, "format": "json", "limit": 1, "countrycodes": "us"},
        )
        results = http.get_json(url).data
        if not results:
            raise LookupError(f"neither geocoder matched {ctx.address!r}")

        hit = results[0]
        lat, lon = round(float(hit["lat"]), 6), round(float(hit["lon"]), 6)
        note = "OpenStreetMap fallback — may be a street centroid rather than a rooftop"

        return StationResult(
            station=self.name,
            values={
                "latitude": estimated(lat, NOMINATIM_DOC, note=note, precision="approximate"),
                "longitude": estimated(lon, NOMINATIM_DOC, note=note, precision="approximate"),
                "matched_address": estimated(
                    hit.get("display_name", ctx.address), NOMINATIM_DOC, note=note
                ),
            },
            context_updates={
                "lat": lat,
                "lon": lon,
                "matched_address": hit.get("display_name"),
            },
        )
