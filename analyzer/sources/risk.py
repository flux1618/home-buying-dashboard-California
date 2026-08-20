"""Station R — FEMA National Risk Index, at the census tract.

This is the multi-hazard layer that Phase 1 actually asked for. The flood station (F)
answers one yes/no question that happens to be a hard fail; this one answers "what else
does this tract get hit by," across 18 hazards, plus how vulnerable the surrounding
population is and how well the community recovers.

It is deliberately **not** a scoring input. Nothing here moves a score. Every finding
arrives as a caveat and a verification task, the same treatment a pre-2000 build year
gets, for two reasons: hazard risk is a reason to price insurance and ask questions, not
a reason to reject a house; and bolting new deductions onto the engine would silently
change every score already recorded in the decision journal. If a fork wants wildfire to
deduct — and a California fork should — that belongs in the profile and in the engine,
not smuggled in through a source. See ADR 0009.

Two things measured against the live service on 2026-08-19 shaped everything below.

**1. Score on the percentile, never on the rating label.**

Paradise, California — tract 06007002100, the town the 2018 Camp Fire destroyed — returns
a wildfire score of 95.2 and a wildfire rating of "Relatively Moderate". Those describe
the same tract. Sampling 2,000 California tracts showed why: the rating is a monotonic
function of the score, but the bins are cut per hazard, not on a shared scale.

    wildfire   Very Low  reaches 68.5   Relatively Moderate spans 88.4 - 96.2
    composite  Very Low  reaches 20.1   Relatively Moderate spans 55.9 - 84.3

So "Relatively Moderate" means the 60th percentile of composite risk and the 95th
percentile of wildfire risk. Comparing those two strings, or worse, mapping the five
labels onto five deduction tiers, understates wildfire by roughly 35 percentile points in
exactly the places wildfire matters. The station reports both and uses the number.

(The 2,000-tract sample is ordered by object id, not random, so it leans toward
early-alphabet counties. It is enough to establish that the bins differ per hazard, which
is the claim being made, and not enough to publish exact national cut points.)

**2. A zero is not good news.**

900 of those 2,000 tracts return a wildfire score of exactly 0.0 with the rating
"No Rating". That is FEMA saying the hazard is not modeled for this tract — usually dense
urban land with no wildfire exposure area. It is not a claim of safety. Treating it as a
low score would make an unmodeled tract outrank a genuinely low-risk one, which is the
same bug the parcel station shipped in Session 1: absence of evidence scoring as evidence
of absence. Those come back as `unavailable`, never as a value.
"""

from __future__ import annotations

from typing import Any

from ..core.provenance import measured, unavailable
from . import http
from .base import Context, Station, StationResult

NRI_QUERY = (
    "https://services.arcgis.com/XG15cJAlne2vxtgt/arcgis/rest/services/"
    "National_Risk_Index_Census_Tracts/FeatureServer/0/query"
)
NRI_DOC = "https://www.fema.gov/flood-maps/products-tools/national-risk-index"

# All 18 hazards the NRI models, keyed by FEMA's four-letter code. The full set is here
# rather than only the ones this buyer cares about, because the hazards worth reporting
# are a property of the region, not of the code — see `hazards` in buyer_profile.toml.
HAZARD_LABELS: dict[str, str] = {
    "AVLN": "avalanche",
    "CFLD": "coastal flooding",
    "CWAV": "cold wave",
    "DRGT": "drought",
    "ERQK": "earthquake",
    "HAIL": "hail",
    "HRCN": "hurricane",
    "HWAV": "heat wave",
    "ISTM": "ice storm",
    "LNDS": "landslide",
    "LTNG": "lightning",
    # FEMA's NRI has no "IFLD" code, which is what this table used to claim. Riverine
    # flooding is RFLD; the coastal counterpart is CFLD above. The wrong code was harmless
    # in South Carolina only because no profile requested it — it would have passed this
    # module's own validation and then been rejected field-by-field by the service.
    # Correcting it matters more here: a Los Angeles River corridor address has real
    # riverine exposure worth asking about.
    "RFLD": "riverine flooding",
    "SWND": "strong wind",
    "TRND": "tornado",
    "TSUN": "tsunami",
    "VLCN": "volcanic activity",
    "WFIR": "wildfire",
    "WNTW": "winter weather",
}

# Codes the NRI publishes, kept as a separate assertion so a typo in HAZARD_LABELS shows
# up as a test failure rather than as a request the service rejects at query time.
NRI_HAZARD_CODES = frozenset(
    {
        "AVLN", "CFLD", "CWAV", "DRGT", "ERQK", "HAIL", "HRCN", "HWAV", "ISTM",
        "LNDS", "LTNG", "RFLD", "SWND", "TRND", "TSUN", "VLCN", "WFIR", "WNTW",
    }
)

# FEMA's way of saying "not modeled here". Case-folded before comparison.
NOT_MODELED = frozenset(
    {"no rating", "not applicable", "insufficient data", "no expected annual losses", ""}
)

# Identity and composite fields, requested for every query.
BASE_FIELDS = (
    "TRACTFIPS",
    "COUNTY",
    "STATE",
    "NRI_VER",
    "RISK_SCORE",
    "RISK_RATNG",
    "RISK_SPCTL",
    "SOVI_SCORE",
    "SOVI_RATNG",
    "RESL_SCORE",
    "RESL_RATNG",
)

# Per-hazard suffixes. Verified present for all 18 codes on the live layer — several
# hazards are missing EALB/EALP/EALA (drought has no building or population loss model at
# all), so requesting those would 400 the whole query for the sake of one hazard.
HAZARD_SUFFIXES = ("RISKS", "RISKR", "EALT")


def is_modeled(rating: Any) -> bool:
    """False when FEMA is declining to rate, which is not the same as rating it low."""
    if rating is None:
        return False
    return str(rating).strip().lower() not in NOT_MODELED


class RiskStation(Station):
    name = "risk"
    provides = ("hazard_profile",)
    description = "FEMA National Risk Index, census tract containing the point"

    def __init__(
        self,
        hazards: tuple[str, ...] = (),
        caveat_percentile: float = 90.0,
    ) -> None:
        # An unknown code is a profile typo, and failing loudly at construction beats
        # silently reporting 3 hazards when the profile asked for 4.
        unknown = [h for h in hazards if h not in HAZARD_LABELS]
        if unknown:
            raise ValueError(
                f"unknown NRI hazard code(s) {unknown}; valid codes are "
                f"{sorted(HAZARD_LABELS)}"
            )
        self.hazards = hazards
        self.caveat_percentile = caveat_percentile

    def fetch(self, ctx: Context) -> StationResult:
        fields = list(BASE_FIELDS) + [
            f"{code}_{suffix}" for code in self.hazards for suffix in HAZARD_SUFFIXES
        ]
        url = http.build_url(
            NRI_QUERY,
            {
                "geometry": f"{ctx.lon},{ctx.lat}",
                "geometryType": "esriGeometryPoint",
                "inSR": 4326,
                "spatialRel": "esriSpatialRelIntersects",
                "outFields": ",".join(fields),
                "returnGeometry": "false",
                "f": "json",
            },
        )
        payload = http.get_json(url).data
        if "error" in payload:
            raise http.SourceUnavailable(
                str(payload["error"].get("message", "ArcGIS error"))
            )

        features = payload.get("features", [])
        if not features:
            return self._no_tract()

        attrs = features[0].get("attributes", {})
        tract = (attrs.get("TRACTFIPS") or "").strip() or None
        if not tract:
            return self._no_tract()

        version = (attrs.get("NRI_VER") or "").strip() or None
        stamp = f"NRI data version {version}" if version else None
        values: dict[str, Any] = {}
        profile: dict[str, Any] = {"tract_fips": tract, "hazards": {}}

        values["nri_tract_fips"] = measured(
            tract, NRI_DOC, note=stamp, precision="census_tract"
        )
        county, state = attrs.get("COUNTY"), attrs.get("STATE")
        if county and state:
            profile["tract_label"] = f"{county} County, {state}"

        # Composite risk, social vulnerability, community resilience. Same not-modeled
        # rule applies: a rating FEMA declined to give is unknown, not favourable.
        for key, score_f, rating_f, precision in (
            ("nri_composite_risk", "RISK_SCORE", "RISK_RATNG", "national_percentile"),
            ("social_vulnerability", "SOVI_SCORE", "SOVI_RATNG", "national_percentile"),
            ("community_resilience", "RESL_SCORE", "RESL_RATNG", "national_percentile"),
        ):
            score, rating = attrs.get(score_f), attrs.get(rating_f)
            if score is None or not is_modeled(rating):
                values[key] = unavailable(
                    f"FEMA returns no {key.replace('_', ' ')} rating for tract {tract}"
                )
                continue
            values[key] = measured(
                round(float(score), 1),
                NRI_DOC,
                note=f"FEMA rating {rating!r}" + (f"; {stamp}" if stamp else ""),
                precision=precision,
            )
            profile[key] = {"percentile": round(float(score), 1), "rating": rating}

        tasks: list[dict[str, Any]] = []
        for code in self.hazards:
            label = HAZARD_LABELS[code]
            score = attrs.get(f"{code}_RISKS")
            rating = attrs.get(f"{code}_RISKR")
            key = f"hazard_{label.replace(' ', '_')}"

            if score is None or not is_modeled(rating):
                values[key] = unavailable(
                    f"FEMA does not model {label} for tract {tract} "
                    f"(rating {rating!r}). Not modeled is not the same as low risk."
                )
                profile["hazards"][code] = {"label": label, "modeled": False}
                continue

            pct = round(float(score), 1)
            eal = attrs.get(f"{code}_EALT")
            note = f"FEMA rating {rating!r}"
            if eal is not None:
                note += f"; expected annual loss ${round(float(eal)):,} tract-wide"
            values[key] = measured(
                pct, NRI_DOC, note=note, precision="national_percentile_within_hazard"
            )
            profile["hazards"][code] = {
                "label": label,
                "modeled": True,
                "percentile": pct,
                "rating": rating,
            }

            # Threshold on the number, not the label. This is the Paradise case: rating
            # "Relatively Moderate", percentile 95.2, and only one of those two prompts
            # anybody to get a wildfire insurance quote.
            if pct >= self.caveat_percentile:
                tasks.append(
                    self.task(
                        f"{label.capitalize()} risk is in the {pct:.0f}th percentile "
                        f"nationally for this tract — get a specific insurance quote "
                        f"and ask the seller about past claims",
                        blocking=False,
                        reason=(
                            f"FEMA labels this {rating!r}, which reads mild; the "
                            f"percentile is what the label is hiding"
                        ),
                    )
                )

        sovi = profile.get("social_vulnerability", {}).get("percentile")
        resl = profile.get("community_resilience", {}).get("percentile")
        if sovi is not None and resl is not None and sovi >= 75.0 and resl < 50.0:
            # High vulnerability plus low resilience is the combination that turns a
            # moderate hazard into a slow recovery. Worth a sentence, never a deduction.
            tasks.append(
                self.task(
                    f"Tract social vulnerability is {sovi:.0f}th percentile while "
                    f"community resilience is {resl:.0f}th — recovery after a hazard "
                    f"event tends to be slower here. Ask the insurer about claim "
                    f"history in this tract",
                    blocking=False,
                    reason="a caveat about recovery time, not a judgement on the house",
                )
            )

        return StationResult(
            station=self.name,
            facts={"hazard_profile": profile},
            values=values,
            tasks=tasks,
        )

    def _no_tract(self) -> StationResult:
        """The point fell outside every NRI tract polygon. Offshore, or a bad geocode."""
        note = (
            "No NRI census tract intersects this point. The NRI covers all 50 states, "
            "DC and the territories, so this usually means the geocode landed in water "
            "or outside the United States — not that the tract has no hazard risk."
        )
        return StationResult(
            station=self.name,
            facts={"hazard_profile": None},
            values={"nri_tract_fips": unavailable(note)},
            tasks=[
                self.task(
                    "No FEMA National Risk Index tract matched this point — check the "
                    "geocoded coordinates before trusting any other location fact",
                    blocking=False,
                    reason="a point outside every tract usually means a bad geocode",
                )
            ],
        )
