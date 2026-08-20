"""The line: address in, scored analysis out. GAFRCBES.

    G  geocode     address -> lat/lon + census block          FATAL
    A  assess      LA County Assessor parcel roll
    F  flood       FEMA National Flood Hazard Layer
    R  risk        FEMA National Risk Index, 18 hazards at the tract
    C  commute     OSRM drive time to the anchor
    B  broadband   FCC availability by census block
    E  education   LAUSD attendance boundary + CAASPP proficiency
    S  score       the pure engine from Session 1

R sits next to F because they are the same agency answering two different questions, and
keeping them adjacent makes it obvious that only one of them can fail a house. Flood zone
is a hard fail; everything the risk station returns is a caveat.

E is last among the fetchers because it is the slowest by a wide margin: resolving a zone
means a boundary query, a spatial join, and then one HTML page per candidate school from a
state portal with no API. Putting it at the end means a run that is going to fail on
something cheap fails before paying that cost.

Stations run in that order because each depends on what came before, and only G is
fatal — without coordinates there is nothing to ask anyone about. Any other station going
dark subtracts a fact and adds a Degradation. The analysis still completes, the missing
input stays `None`, and the scoring engine's unknown-handling takes over from there:
unknown hard-fail inputs pin the score to 50 and name themselves as open questions.

The important structural point: **this module contains no domain logic.** It moves facts
from stations into `PropertyFacts` and calls `analyze()`. Every rule about what a fact
means lives in `analyzer/core/`, which has no network access at all (ADR 0002). Swapping
Spartanburg County for another county means writing stations, not touching the engine.

One engine, many delivery trucks.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

from .core.analyze import analyze
from .core.profile import BuyerProfile, load_profile
from .core.provenance import Degradation, Value
from .core.scoring import PropertyFacts
from .sources.base import Context, Station
from .sources.broadband import BroadbandStation
from .sources.commute import CommuteStation
from .sources.flood import FloodStation
from .sources.geocode import GeocodeStation
from .sources.parcel import ParcelStation
from .sources.risk import RiskStation
from .sources.schools import SchoolStation


class PipelineAborted(RuntimeError):
    """A fatal station failed. Only geocoding is fatal."""


@dataclass
class PipelineRun:
    """Everything that happened, not just what succeeded."""

    document: dict[str, Any]
    degradations: list[Degradation] = field(default_factory=list)
    values: dict[str, Value] = field(default_factory=dict)
    stations_run: list[str] = field(default_factory=list)

    @property
    def complete(self) -> bool:
        return not self.degradations

    @property
    def degraded_stations(self) -> list[str]:
        return [d.station for d in self.degradations]


def build_stations(profile: BuyerProfile, api_keys: dict[str, str]) -> list[Station]:
    """Station order is dependency order. Geocode is always first and always fatal.

    MarketVelocityStation deliberately is not in this line. Its committed aggregate snapshot is
    reporting-only and has no property fact or score input; reading it for every address would
    add local I/O and a stale-snapshot degradation to every property report. It remains available
    through its explicit snapshot endpoint and station contract instead of becoming a hidden
    per-property dependency.
    """
    return [
        GeocodeStation(),
        ParcelStation(),
        FloodStation(),
        RiskStation(
            hazards=profile.hazards,
            caveat_percentile=profile.hazard_caveat_percentile,
        ),
        CommuteStation(profile.primary_anchor),
        BroadbandStation(api_key=api_keys.get("fcc")),
        SchoolStation(levels=profile.schools.levels),
    ]


def collect_api_keys() -> dict[str, str]:
    """Read from the environment only. No key is ever written to a file or a result."""
    keys = {}
    fcc = os.environ.get("FCC_API_KEY", "").strip()
    if fcc:
        keys["fcc"] = fcc
    return keys


def run(
    address: str,
    price: float,
    *,
    profile: BuyerProfile | None = None,
    current_year: int | None = None,
    hoa_monthly: float = 0.0,
    roof_age_years: int | None = None,
    hvac_age_years: int | None = None,
    garage_spaces: int | None = None,
    api_keys: dict[str, str] | None = None,
    stations: list[Station] | None = None,
) -> PipelineRun:
    """Analyse one address.

    The keyword arguments are the facts no public source will tell you. HOA dues,
    component ages, and garage bay count come off the listing, the seller's disclosure,
    or a phone call — so they are inputs, not fetches. Leaving them out is safe: `None`
    means unknown, and the engine treats unknown differently from bad.
    """
    from datetime import date

    profile = profile or load_profile()
    current_year = current_year or date.today().year
    keys = api_keys if api_keys is not None else collect_api_keys()

    ctx = Context(address=address, price=price, api_keys=keys)
    line = stations if stations is not None else build_stations(profile, keys)

    degradations: list[Degradation] = []
    values: dict[str, Value] = {}
    extra_tasks: list[dict[str, Any]] = []
    ran: list[str] = []

    for station in line:
        result = station.run(ctx)
        ran.append(station.name)

        if result.degradation:
            degradations.append(result.degradation)
            if station.fatal:
                raise PipelineAborted(
                    f"{station.name} is required and failed: {result.degradation.reason}"
                )
            continue

        ctx.facts.update(result.facts)
        values.update(result.values)
        extra_tasks.extend(result.tasks)
        for key, value in result.context_updates.items():
            setattr(ctx, key, value) if hasattr(ctx, key) else ctx.facts.__setitem__(key, value)

    facts = PropertyFacts(
        price=price,
        sqft=ctx.facts.get("sqft"),
        beds=ctx.facts.get("beds"),
        baths=ctx.facts.get("baths"),
        garage_spaces=garage_spaces,
        hoa_monthly=hoa_monthly,
        year_built=ctx.facts.get("year_built"),
        roof_age_years=roof_age_years,
        hvac_age_years=hvac_age_years,
        flood_zone=ctx.facts.get("flood_zone"),
        water_sewer=ctx.facts.get("water_sewer"),
        commute_min=ctx.facts.get("commute_min"),
        fiber_available=ctx.facts.get("fiber_available"),
        school_district=ctx.facts.get("school_district"),
        school_proficiency_pct=ctx.facts.get("school_proficiency_pct"),
        school_zone_of_choice=ctx.facts.get("school_zone_of_choice"),
    )

    document = analyze(
        facts,
        profile,
        current_year,
        address=ctx.matched_address or address,
        degradations=degradations,
    )

    # Station tasks come first: they are the ones tied to a specific source finding.
    document["verification_tasks"] = _merge_tasks(
        extra_tasks, document.get("verification_tasks", [])
    )
    document["sources"] = {key: value.to_dict() for key, value in sorted(values.items())}
    document["stations"] = {
        "run": ran,
        "degraded": [d.station for d in degradations],
        "complete": not degradations,
    }
    # Carried on the document rather than into PropertyFacts on purpose: the hazard
    # profile is reporting, not scoring, and PropertyFacts is the scoring engine's input.
    # Putting it there would make it look like it counts.
    hazard_profile = ctx.facts.get("hazard_profile")
    if hazard_profile:
        document["hazard_profile"] = hazard_profile

    if ctx.lat is not None:
        document["location"] = {
            "requested_address": address,
            "matched_address": ctx.matched_address,
            "latitude": ctx.lat,
            "longitude": ctx.lon,
            "census_block_geoid": ctx.census_block_geoid,
            "county_fips": ctx.county_fips,
        }

    return PipelineRun(
        document=document, degradations=degradations, values=values, stations_run=ran
    )


def _merge_tasks(
    station_tasks: list[dict[str, Any]], engine_tasks: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Deduplicate by task text, blocking first, source-specific ahead of generic."""
    merged: dict[str, dict[str, Any]] = {}
    for task in list(station_tasks) + list(engine_tasks):
        existing = merged.get(task["task"])
        if existing is None or (task.get("blocking") and not existing.get("blocking")):
            merged[task["task"]] = task
    return sorted(merged.values(), key=lambda t: (not t.get("blocking"), t["task"]))
