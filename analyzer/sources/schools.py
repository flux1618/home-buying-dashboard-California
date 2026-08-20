"""Station S — attendance zone and measured school quality.

The Spartanburg edition had no school station, and the reason was not that schools did
not matter. Spartanburg County publishes no public attendance-boundary layer, so the only
way to put a school on a property card would have been to name the nearest one, which is
not the same thing as the assigned one and would have been wrong often enough to be
dangerous. Los Angeles publishes the boundaries, so the station exists here.

Two sources, joined on one key:

  * **LAUSD attendance boundaries** (City of LA GIS) answer "which school is this address
    zoned to" by point-in-polygon.
  * **CAASPP** (California Assessment of Student Performance and Progress) answers "how
    is that school doing" with the state's own test results.

The join key is the 14-digit CDS code, which both sides carry. That is the whole reason
this is possible without a commercial ranking API.

**Why state test percentages and not a 1-10 rating.** A composite rating from a
third-party site is a derived number whose formula can change without notice, and most of
them are licensed rather than public. CAASPP publishes the percentage of students who met
or exceeded the standard, measured, by school, by the state that administers the test. It
is a narrower claim than "school quality" and that narrowness is the point: it is
checkable. Anything sourced from a vendor composite would be marked ``estimated``; nothing
here is.

**Three things this station refuses to smooth over.**

1. *The boundary layers do not name their schools.* Layers 4, 5 and 6 carry only numeric
   keys (``EKEY_5``, ``MKEY_5``, ``HKEY_5``) and their ``TOOLTIP`` and ``NLA_URL`` fields
   are empty on every record. There is no published key-to-name table. So the station
   pulls the boundary polygon's geometry and spatially joins it back to the school-point
   layer, filtering by grade level. That works, but it is an inference, not a lookup: the
   polygon can contain campuses that are not the one it belongs to, so the level filter
   and the charter/magnet exclusion are doing real work and any residual ambiguity is
   reported rather than resolved.

2. *A high-school answer is often plural.* Much of LAUSD runs Zones of Choice, where a
   family ranks campuses and receives a lottery result instead of an assignment. The test
   point used to develop this station returned Woodrow Wilson High School and Abraham
   Lincoln High School from the same polygon, and neither is "the" answer. The station
   returns every traditional campus it finds and scores the **average**, because the
   expected outcome of a lottery is the mean of what you might draw and not the best of
   it. Reporting the strongest nearby school as though it were assigned would flatter
   every Zone of Choice address in the district.

3. *A missing middle school is a data gap, not a good sign.* Some middle-school boundaries
   contain no traditional middle-school point at all. That comes back unknown.

**What the number does and does not mean.** Test scores track household income closely, so
a proficiency percentage describes the measured average outcome of the students currently
enrolled — not the school's effect on a child who transfers in. It is reported as what it
is. It is also worth knowing that boundaries are set by board policy with no fixed redraw
cadence, so a zone that holds today is not a guarantee for a kindergartner.
"""

from __future__ import annotations

import json
import re
from typing import Any

from ..core.provenance import derived, measured, unavailable
from . import http
from .base import Context, Station, StationResult

BOUNDARY_SERVICE = (
    "https://maps.lacity.org/lahub/rest/services/LAUSD_Schools/MapServer"
)
BOUNDARY_DOC = "https://geohub.lacity.org/"
CAASPP_DOC = "https://caaspp-elpac.ets.org/caaspp/"
CDE_DIRECTORY = "https://www.cde.ca.gov/schooldirectory/"
ZONE_OF_CHOICE_DOC = "https://zoc.lausd.org/"

# Layer 0 is the school-point layer; 4, 5 and 6 are the attendance boundaries.
SCHOOL_POINT_LAYER = 0
BOUNDARY_LAYERS = {"elementary": 4, "middle": 5, "high": 6}

# `MPD_DESC` values that count as the traditional, address-assigned campus for a level.
# A magnet or charter on the same site serves a different, application-based population,
# so including it would answer a question nobody asked.
LEVEL_MPD_DESC = {
    "elementary": {"Elementary School"},
    "middle": {"Middle School"},
    "high": {"High School"},
}
# `MAP_DESC` values that disqualify a campus from being the zoned school.
EXCLUDED_MAP_DESC_PREFIXES = ("Independent Charter", "Magnet School")

# CAASPP publishes this as a server-rendered page with no JSON API. `lstGrade=13` is the
# all-grades rollup, `lstTestType=B` is both subjects, `lstGroup=1`/`lstSubGroup=1` is all
# students. Year is the spring of the reported school year.
CAASPP_YEAR = 2025
CAASPP_URL = (
    "https://caaspp-elpac.ets.org/caaspp/DashViewReportSB"
    "?ps=true&lstTestYear={year}&lstTestType=B&lstGroup=1&lstSubGroup=1"
    "&lstGrade=13&lstSchoolType=A&lstCounty={cc}&lstDistrict={dd}-000&lstSchool={ss}"
)

# The percentage sits inside a label whose id is stable but which carries other
# attributes, so the pattern has to tolerate anything between the id and the closing
# bracket. An earlier version anchored directly on `">` and silently matched nothing,
# which is exactly the failure mode that makes HTML parsing worth a comment.
_ELA_PCT = re.compile(r'id="MainContent_lblDonutElaInnerCnt1"[^>]*>\s*([\d.]+)\s*%')
_MATH_PCT = re.compile(r'id="MainContent_lblDonutMathInnerCnt1"[^>]*>\s*([\d.]+)\s*%')

# CAASPP suppresses results for fewer than 11 tested students.
SUPPRESSION_FLOOR = 11


def split_cds(cds: str) -> tuple[str, str, str]:
    """Split a 14-digit CDS code into county, district and school components."""
    text = "".join(ch for ch in str(cds) if ch.isdigit())
    if len(text) != 14:
        raise ValueError(f"CDS code must be 14 digits, got {len(text)}: {cds!r}")
    return text[:2], text[2:7], text[7:]


def is_traditional(attrs: dict[str, Any], level: str) -> bool:
    """True when this campus is the address-assigned school for the level."""
    mpd = (attrs.get("MPD_DESC") or "").strip()
    map_desc = (attrs.get("MAP_DESC") or "").strip()
    if attrs.get("CHARTER") == "Y":
        return False
    if map_desc.startswith(EXCLUDED_MAP_DESC_PREFIXES):
        return False
    return mpd in LEVEL_MPD_DESC[level]


def proficiency(ela: float | None, math: float | None) -> float | None:
    """Average of the two subject percentages, or None if either is missing.

    Deliberately not "whichever subject reported". Math proficiency runs far below ELA in
    this district — Wilson High School measured 67.28% ELA against 21.98% Math — so
    averaging one subject alone would produce a number that is not comparable to a
    two-subject average anywhere else in the table.
    """
    if ela is None or math is None:
        return None
    return round((ela + math) / 2.0, 2)


class SchoolStation(Station):
    name = "schools"
    provides = ("school_district", "school_proficiency_pct", "school_zone_of_choice")
    description = (
        "LAUSD attendance boundaries joined to CAASPP proficiency on the CDS code"
    )

    def __init__(self, levels: tuple[str, ...] = ("elementary", "middle", "high")):
        unknown = [lvl for lvl in levels if lvl not in BOUNDARY_LAYERS]
        if unknown:
            raise ValueError(
                f"unknown school level(s) {unknown}; valid levels are "
                f"{sorted(BOUNDARY_LAYERS)}"
            )
        self.levels = levels

    def fetch(self, ctx: Context) -> StationResult:
        values: dict[str, Any] = {}
        tasks: list[dict[str, Any]] = []
        by_level: dict[str, dict[str, Any]] = {}
        district: str | None = None
        zone_of_choice = False

        for level in self.levels:
            resolved = self._resolve_level(level, ctx)
            by_level[level] = resolved
            if resolved["schools"]:
                district = district or "Los Angeles Unified School District"
            if resolved["is_zone_of_choice"]:
                zone_of_choice = True

        # Every level came back empty. The address is very likely outside LAUSD — the
        # surrounding independent districts (Alhambra, South Pasadena, Montebello and the
        # rest) publish their boundaries separately and are not in this service. That is a
        # coverage limit, not a property with no schools, and it is reported as such.
        if not any(r["schools"] for r in by_level.values()):
            return StationResult(
                station=self.name,
                facts={
                    "school_district": None,
                    "school_proficiency_pct": None,
                    "school_zone_of_choice": None,
                },
                values={
                    "school_district": unavailable(
                        note=(
                            "no LAUSD attendance boundary contains this point; the address "
                            "is probably served by one of the independent districts "
                            "(Alhambra, South Pasadena, Pasadena, Montebello, Monterey "
                            "Park, San Gabriel, Rosemead), whose boundaries are not in "
                            "this service"
                        )
                    ),
                },
                tasks=[
                    self.task(
                        "Identify the serving district and zoned schools directly — this "
                        "address falls outside the LAUSD boundary layer",
                        blocking=True,
                        reason=(
                            "only LAUSD boundaries are queryable; independent districts "
                            "publish theirs separately"
                        ),
                    )
                ],
            )

        # -- shape the per-level detail ------------------------------------------
        level_scores: dict[str, float] = {}
        for level, resolved in by_level.items():
            names = [s["name"] for s in resolved["schools"]]
            scored = [s for s in resolved["schools"] if s["proficiency_pct"] is not None]

            if not names:
                values[f"{level}_school"] = unavailable(
                    note=(
                        f"the {level} attendance boundary at this point contains no "
                        f"traditional {level} campus; this is a gap in the published data, "
                        f"not evidence that no school serves the address"
                    )
                )
                tasks.append(
                    self.task(
                        f"Confirm the assigned {level} school with LAUSD — the boundary "
                        f"layer returned no traditional campus",
                        blocking=False,
                        reason="the published boundary and point layers disagree here",
                    )
                )
                continue

            values[f"{level}_school"] = measured(
                names if len(names) > 1 else names[0],
                BOUNDARY_DOC,
                note=(
                    f"resolved by point-in-polygon against the LAUSD {level} attendance "
                    f"boundary, then spatially joined to the school-point layer because "
                    f"the boundary records carry numeric keys and no school name"
                    + (
                        f"; {len(names)} traditional campuses share this boundary, which "
                        f"indicates a Zone of Choice — assignment is by preference lottery, "
                        f"not by address"
                        if len(names) > 1
                        else ""
                    )
                ),
            )

            if scored:
                pcts = [s["proficiency_pct"] for s in scored]
                avg = round(sum(pcts) / len(pcts), 2)
                level_scores[level] = avg
                detail = "; ".join(
                    f"{s['name']}: {s['ela_pct']:.2f}% ELA / {s['math_pct']:.2f}% math"
                    for s in scored
                )
                values[f"{level}_proficiency_pct"] = measured(
                    avg,
                    CAASPP_DOC,
                    note=(
                        f"mean of the ELA and math 'met or exceeded standard' percentages, "
                        f"CAASPP {CAASPP_YEAR} all grades, all students ({detail})"
                        + (
                            f" — averaged across {len(scored)} campuses because a lottery "
                            f"outcome is the mean of what you might draw, not the best of it"
                            if len(scored) > 1
                            else ""
                        )
                    ),
                )
            else:
                values[f"{level}_proficiency_pct"] = unavailable(
                    note=(
                        f"CAASPP reported no usable percentages for the zoned {level} "
                        f"school; results are suppressed below {SUPPRESSION_FLOOR} tested "
                        f"students, and the portal is server-rendered HTML rather than an "
                        f"API, so a page-shape change looks the same as suppression"
                    )
                )

            if resolved["unscored"]:
                tasks.append(
                    self.task(
                        f"Look up CAASPP results by hand for {', '.join(resolved['unscored'])} "
                        f"— no percentages were parsed from the state portal",
                        blocking=False,
                        reason="suppressed results and a changed page look identical",
                    )
                )

        # -- the single scoring input --------------------------------------------
        # The weakest level, not the average of levels. A household passes a child through
        # all three in sequence, so a strong elementary school does not compensate for the
        # middle school that follows it; the family either moves or accepts it. Averaging
        # the levels would let one good campus hide a bad one for six years.
        overall: float | None = None
        if level_scores:
            weakest_level = min(level_scores, key=lambda k: level_scores[k])
            overall = level_scores[weakest_level]
            values["school_proficiency_pct"] = derived(
                overall,
                note=(
                    f"the weakest of the zoned levels ({weakest_level} at {overall:.2f}%) "
                    f"out of "
                    + ", ".join(f"{k} {v:.2f}%" for k, v in sorted(level_scores.items()))
                    + " — scored on the weakest because a child passes through every level "
                    "in turn and a strong elementary school does not offset the middle "
                    "school that follows it"
                ),
            )

        if district:
            values["school_district"] = measured(
                district,
                CDE_DIRECTORY,
                note="the only district whose attendance boundaries this service publishes",
            )

        if zone_of_choice:
            values["school_zone_of_choice"] = measured(
                True,
                ZONE_OF_CHOICE_DOC,
                note=(
                    "more than one traditional campus serves this boundary, so the "
                    "high-school assignment is a ranked-preference lottery; a good zoned "
                    "school here is a possibility rather than a guarantee"
                ),
            )
            tasks.append(
                self.task(
                    "Read the Zone of Choice rules and application deadlines for this "
                    "area before committing to the address",
                    blocking=False,
                    reason="the school shown is one of several possible outcomes",
                )
            )

        # Attached to every run, because it is the caveat most likely to be forgotten.
        values["school_score_meaning"] = derived(
            "measured average outcome of currently enrolled students",
            note=(
                "CAASPP proficiency correlates strongly with household income, so this "
                "figure describes the population now enrolled rather than the school's "
                "effect on a transferring child; attendance boundaries are board policy "
                "with no fixed redraw cadence and can change"
            ),
        )

        return StationResult(
            station=self.name,
            facts={
                "school_district": district,
                "school_proficiency_pct": overall,
                "school_zone_of_choice": zone_of_choice if district else None,
            },
            values=values,
            tasks=tasks,
        )

    # -- resolution ----------------------------------------------------------

    def _resolve_level(self, level: str, ctx: Context) -> dict[str, Any]:
        layer = BOUNDARY_LAYERS[level]
        rings = self._boundary_rings(layer, ctx)
        if not rings:
            return {"schools": [], "unscored": [], "is_zone_of_choice": False}

        candidates = self._schools_in(rings, level)
        schools: list[dict[str, Any]] = []
        unscored: list[str] = []

        for attrs in candidates:
            name = (attrs.get("FULLNAME") or "").strip()
            cds = (attrs.get("CDSCODE") or "").strip()
            ela = math = None
            if cds:
                ela, math = self._caaspp(cds)
            pct = proficiency(ela, math)
            if pct is None and name:
                unscored.append(name)
            schools.append(
                {
                    "name": name,
                    "cds": cds or None,
                    "ela_pct": ela,
                    "math_pct": math,
                    "proficiency_pct": pct,
                    "address": (attrs.get("ADDRESS") or "").strip() or None,
                }
            )

        # Deduplicate on CDS code: the point layer repeats a campus that appears more than
        # once in the service, and the El Sereno probe returned one charter three times.
        seen: set[str] = set()
        unique: list[dict[str, Any]] = []
        for s in schools:
            key = s["cds"] or s["name"]
            if key in seen:
                continue
            seen.add(key)
            unique.append(s)

        return {
            "schools": unique,
            "unscored": sorted(set(unscored)),
            # Only high school runs Zones of Choice, so a plural result at another level
            # means the spatial join was ambiguous rather than that a lottery applies.
            "is_zone_of_choice": level == "high" and len(unique) > 1,
        }

    def _boundary_rings(self, layer: int, ctx: Context) -> list[Any]:
        url = http.build_url(
            f"{BOUNDARY_SERVICE}/{layer}/query",
            {
                "geometry": f"{ctx.lon},{ctx.lat}",
                "geometryType": "esriGeometryPoint",
                "inSR": 4326,
                "spatialRel": "esriSpatialRelIntersects",
                "outFields": "OBJECTID",
                "returnGeometry": "true",
                "outSR": 4326,
                "f": "json",
            },
        )
        payload = http.get_json(url).data
        if "error" in payload:
            raise http.SourceUnavailable(
                str(payload["error"].get("message", "ArcGIS error"))
            )
        features = payload.get("features") or []
        if not features:
            return []
        return features[0].get("geometry", {}).get("rings") or []

    def _schools_in(self, rings: list[Any], level: str) -> list[dict[str, Any]]:
        # POST rather than GET: a high-school boundary came back with 838 vertices, which
        # is far past what a query string will carry.
        payload = http.post_json(
            f"{BOUNDARY_SERVICE}/{SCHOOL_POINT_LAYER}/query",
            {
                "geometry": json.dumps(
                    {"rings": rings, "spatialReference": {"wkid": 4326}}
                ),
                "geometryType": "esriGeometryPolygon",
                "inSR": 4326,
                "spatialRel": "esriSpatialRelIntersects",
                "outFields": "FULLNAME,CDSCODE,MPD_DESC,MAP_DESC,CHARTER,ADDRESS,CITY,ZIP",
                "returnGeometry": "false",
                "f": "json",
            },
        ).data
        if "error" in payload:
            raise http.SourceUnavailable(
                str(payload["error"].get("message", "ArcGIS error"))
            )
        return [
            f.get("attributes", {})
            for f in payload.get("features") or []
            if is_traditional(f.get("attributes", {}), level)
        ]

    def _caaspp(self, cds: str) -> tuple[float | None, float | None]:
        """Read the two subject percentages for one school, or (None, None).

        Never raises. A suppressed result and a changed page shape are indistinguishable
        from here, and neither is a reason to fail the whole analysis, so both come back
        as absent and the caller records the gap.
        """
        try:
            cc, dd, ss = split_cds(cds)
        except ValueError:
            return None, None
        try:
            html = http.get_text(
                CAASPP_URL.format(year=CAASPP_YEAR, cc=cc, dd=dd, ss=ss)
            ).data
        except (http.SourceUnavailable, http.SourceRejected):
            return None, None
        ela = _ELA_PCT.search(html)
        math = _MATH_PCT.search(html)
        return (
            float(ela.group(1)) if ela else None,
            float(math.group(1)) if math else None,
        )
