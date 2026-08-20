"""Station C — drive time to the anchor. One of the three hard fails.

OSRM returns a **free-flow** duration: the road network with nobody else on it. The
constraint that actually matters is a 06:30–07:00 arrival at Keck Hospital of USC for a
hospital shift change, which is not free-flow. In Los Angeles that gap is far wider than
it was in the Spartanburg edition of this tool: a free-flow number on the 5 or the 10 at
06:45 is close to fiction, so the multiplier below carries more weight here and deserves
more scepticism.

So the honest thing is not to pretend OSRM answered a question it was never asked. This
station reports the free-flow number as measured, applies a stated congestion multiplier,
and labels the result an estimate with the multiplier written into the note. Anyone
reading the output can see exactly what was assumed and disagree with it.

The multiplier is a documented assumption, not a measurement. Verifying it means driving
the route at 06:40 on a Tuesday, which is a task the station emits rather than a number
it invents.
"""

from __future__ import annotations

from ..core.profile import Anchor
from ..core.provenance import derived, estimated, measured
from . import http
from .base import Context, Station, StationResult

OSRM_BASE = "https://router.project-osrm.org/route/v1/driving"
OSRM_DOC = "https://project-osrm.org/"

# Peak-direction congestion on the I-85 / I-26 corridor. A stated assumption.
RUSH_HOUR_MULTIPLIER = 1.25
MULTIPLIER_RATIONALE = (
    "free-flow routing multiplied by 1.25 for peak-direction congestion on the "
    "I-85/I-26 corridor — an assumption, not a measurement"
)


class CommuteStation(Station):
    name = "commute"
    provides = ("commute_min",)
    description = "OSRM free-flow drive time plus a stated congestion allowance"

    def __init__(self, anchor: Anchor, multiplier: float = RUSH_HOUR_MULTIPLIER) -> None:
        self.anchor = anchor
        self.multiplier = multiplier

    def fetch(self, ctx: Context) -> StationResult:
        coords = f"{ctx.lon},{ctx.lat};{self.anchor.lon},{self.anchor.lat}"
        url = f"{OSRM_BASE}/{coords}?overview=false&alternatives=false"

        payload = http.get_json(url).data
        if payload.get("code") != "Ok":
            raise http.SourceUnavailable(f"OSRM returned {payload.get('code')!r}")

        routes = payload.get("routes") or []
        if not routes:
            raise http.SourceUnavailable("OSRM returned no route")

        route = routes[0]
        free_flow_min = round(route["duration"] / 60.0, 1)
        miles = round(route["distance"] / 1609.344, 1)
        rush_min = round(free_flow_min * self.multiplier, 1)

        return StationResult(
            station=self.name,
            facts={"commute_min": rush_min},
            values={
                "distance_miles": measured(miles, OSRM_DOC),
                "free_flow_minutes": measured(
                    free_flow_min, OSRM_DOC, precision="free_flow_no_traffic"
                ),
                "congestion_multiplier": derived(self.multiplier, note=MULTIPLIER_RATIONALE),
                "rush_hour_minutes": estimated(
                    rush_min,
                    OSRM_DOC,
                    note=MULTIPLIER_RATIONALE,
                    precision="modelled_not_measured",
                ),
                "anchor": derived(self.anchor.label, note=self.anchor.address),
            },
            tasks=[
                self.task(
                    f"Drive the route to {self.anchor.label} during the real "
                    f"{self.anchor.arrival_window} window before an offer — the "
                    f"{rush_min:g} min figure is modelled, not driven",
                    blocking=False,
                    reason=MULTIPLIER_RATIONALE,
                )
            ],
        )
