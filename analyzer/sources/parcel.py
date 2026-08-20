"""Station A — Los Angeles County Assessor parcel record: the physical facts.

The county publishes its Assessor roll as a queryable ArcGIS layer at
``public.gis.lacounty.gov``, reachable from the build environment as of 2026-08-19. It is
the Assessor's own current roll rather than a historical export, so values it returns are
``measured``. There is no second-choice mirror here, and deliberately no fallback to a
stale extract: the county layer either answers or the station degrades. A fabricated
fallback would be worse than a missing report.

**One request, not three.** The layer advertises ``supportsQueryWithDistance``, and the
same feature carries the address, the tax rate area, the building characteristics, and the
assessed roll values. The Spartanburg edition needed the tax district from one place and
the CAMA card from another; here a single point query returns all of it, which removes an
entire class of "two sources disagree about which parcel this is" bug.

**What this layer does not have.** Owner name, formal lot size or acreage, roof cover, and
heat type are all absent from the schema — they existed in the Spartanburg CAMA record and
simply do not exist here. Sale price lives in a separate, sparsely populated layer that
returned nothing at either test point, so it is not queried. Those facts come back unknown
and raise tasks. Utility service is likewise not a field: see ``water_sewer`` below.

**Buildings are numbered, not merged.** Characteristics repeat across five slots
(``YearBuilt1``..``YearBuilt5``, ``SQFTmain1``..``SQFTmain5``) because a parcel can hold
more than one structure. A single-family lot populates slot 1 only. Slots are summed for
area and counts and the earliest year is taken for age, but a parcel with more than one
populated slot is flagged, because "1,350 sq ft" and "two buildings totalling 1,350 sq ft"
are different houses to buy.

**The useful accident, California edition.** Spartanburg encoded the assessment ratio in
the leading digit of the property-type code, which revealed whether the listing's tax
figure was about to change. California has no assessment ratio, so that trick has no
analogue — but it has something more valuable. ``Roll_LandBaseYear`` is the year the
seller's Proposition 13 base year value was set, and ``Roll_LandValue`` plus
``Roll_ImpValue`` is the assessed value that has been drifting up at 2% a year ever since.
Together with the asking price those three fields quantify, in dollars, exactly how far
the listing's tax figure will move at close. A 1986 base year on a house selling today is
not a footnote; it is the single largest recurring cost surprise in a California purchase.
``Roll_HomeOwnersExemp`` additionally shows whether the current owner claims the $7,000
homeowners' exemption, which is the closest thing here to Spartanburg's 4%/6% signal.
"""

from __future__ import annotations

from typing import Any

from ..core.provenance import derived, measured
from . import http
from .base import Context, Station, StationResult

# The cached public layer. Faster and more reliably up than the uncached mirror, and it
# carries the full roll schema rather than a trimmed display subset.
ASSESSOR_QUERY = (
    "https://public.gis.lacounty.gov/public/rest/services/LACounty_Cache/"
    "LACounty_Parcel/MapServer/0/query"
)
ASSESSOR_DOC = "https://assessor.lacounty.gov/"
EXEMPTION_DOC = "https://assessor.lacounty.gov/homeowners/homeowners-exemption"

# Same reasoning as the Spartanburg edition: the Census geocoder interpolates a point
# along the street centreline, which routinely lands just outside the parcel it names.
# A strict point-in-polygon query misses real parcels. Buffer, then disambiguate by
# street number.
SEARCH_RADIUS_M = 40

# Up to five buildings per parcel. Requesting `*` would work but pins the station to
# whatever the county adds later; an explicit list makes a schema change visible.
_BUILDING_SLOTS = (1, 2, 3, 4, 5)
_PER_BUILDING = ("YearBuilt", "EffectiveYear", "Units", "Bedrooms", "Bathrooms", "SQFTmain")

PARCEL_FIELDS = ",".join(
    [
        "AIN",
        "APN",
        "SitusHouseNo",
        "SitusStreet",
        "SitusAddress",
        "SitusCity",
        "SitusZIP",
        "SitusFullAddress",
        "TaxRateArea",
        "TaxRateCity",
        "UseCode",
        "UseType",
        "UseDescription",
        "Roll_Year",
        "Roll_LandValue",
        "Roll_ImpValue",
        "Roll_HomeOwnersExemp",
        "Roll_LandBaseYear",
        "Roll_ImpBaseYear",
    ]
    + [f"{field}{slot}" for slot in _BUILDING_SLOTS for field in _PER_BUILDING]
)

# Assessor use codes beginning 01 are single-family residential.
SFR_PREFIX = "01"


def clean(raw: Any) -> str | None:
    """ArcGIS pads unset strings with spaces. Treat those as absent."""
    if raw is None:
        return None
    text = str(raw).strip()
    return text or None


def positive_int(raw: Any) -> int | None:
    """Zero in this dataset means 'not recorded', not 'zero of them'."""
    try:
        number = int(float(raw))
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def positive_float(raw: Any) -> float | None:
    try:
        number = float(raw)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def normalize_tra(raw: Any) -> str | None:
    """Zero-pad the tax rate area to the five digits the county's own tables use.

    The parcel layer returns ``"00004"`` but the Auditor-Controller's rate tables and
    lookup application both accept and emit ``"4"``. Normalising here means the tax engine
    receives one shape regardless of which side supplied it.
    """
    text = clean(raw)
    if not text:
        return None
    digits = "".join(ch for ch in text if ch.isdigit())
    if not digits:
        return None
    return digits.zfill(5)


def read_buildings(attrs: dict[str, Any]) -> dict[str, Any]:
    """Collapse the five numbered building slots into one set of facts.

    Returns living area, bed and bath counts, unit count, the earliest year built, and
    ``building_count`` so the caller can flag a multi-structure parcel rather than quietly
    presenting the sum as though it described one house.
    """
    populated: list[int] = []
    sqft_total = 0.0
    beds_total = 0
    baths_total = 0.0
    units_total = 0
    years: list[int] = []

    for slot in _BUILDING_SLOTS:
        sqft = positive_float(attrs.get(f"SQFTmain{slot}"))
        year = positive_int(attrs.get(f"YearBuilt{slot}"))
        beds = positive_int(attrs.get(f"Bedrooms{slot}"))
        baths = positive_float(attrs.get(f"Bathrooms{slot}"))
        units = positive_int(attrs.get(f"Units{slot}"))

        if not any(v is not None for v in (sqft, year, beds, baths, units)):
            continue

        populated.append(slot)
        sqft_total += sqft or 0.0
        beds_total += beds or 0
        baths_total += baths or 0.0
        units_total += units or 0
        if year is not None:
            years.append(year)

    return {
        "building_count": len(populated),
        "living_sqft": int(round(sqft_total)) if sqft_total > 0 else None,
        "beds": beds_total or None,
        "baths": baths_total or None,
        "units": units_total or None,
        # Oldest structure drives the capital-expense clock: the roof that fails first is
        # on the building that has been standing longest.
        "year_built": min(years) if years else None,
    }


def street_number(text: str | None) -> str | None:
    """Leading house number, or None. Used to disambiguate buffered matches."""
    if not text:
        return None
    parts = text.strip().split()
    first = parts[0] if parts else ""
    return first if first.isdigit() else None


def situs(attrs: dict[str, Any]) -> str | None:
    """Best available street address for the parcel.

    ``SitusFullAddress`` includes city and ZIP, which is what a person wants to read, but
    ``SitusHouseNo`` is the field to trust for number matching because it is a discrete
    column rather than a substring.
    """
    return clean(attrs.get("SitusFullAddress")) or clean(attrs.get("SitusAddress"))


def pick_parcel(candidates: list[dict[str, Any]], address: str) -> dict[str, Any]:
    """Choose among parcels inside the search buffer.

    A 40 m buffer on a dense Los Angeles block can return a dozen lots — the Boyle
    Heights test point returned thirteen — so taking the first one would attach a
    neighbour's bedroom count to this house far more often than it did in Spartanburg.
    The county gives us ``SitusHouseNo`` as its own column, which makes the tiebreak an
    exact comparison rather than string surgery. If nothing matches, the caller still gets
    a candidate but the ambiguity is recorded and raises a blocking task.
    """
    if len(candidates) == 1:
        return candidates[0]

    wanted = street_number(address)
    if wanted:
        for candidate in candidates:
            house_no = clean(candidate.get("SitusHouseNo")) or street_number(
                situs(candidate)
            )
            if house_no and house_no.lstrip("0") == wanted.lstrip("0"):
                return candidate

    chosen = dict(candidates[0])
    chosen["_ambiguous"] = [situs(c) for c in candidates]
    return chosen


class ParcelStation(Station):
    name = "parcel"
    provides = ("sqft", "beds", "baths", "year_built", "water_sewer")
    description = "Los Angeles County Assessor parcel roll, queried at the point"

    def fetch(self, ctx: Context) -> StationResult:
        attrs = self._query(ctx)
        return self._build(attrs, ctx)

    # -- fetching ------------------------------------------------------------

    def _query(self, ctx: Context) -> dict[str, Any]:
        url = http.build_url(
            ASSESSOR_QUERY,
            {
                "geometry": f"{ctx.lon},{ctx.lat}",
                "geometryType": "esriGeometryPoint",
                "inSR": 4326,
                "spatialRel": "esriSpatialRelIntersects",
                "distance": SEARCH_RADIUS_M,
                "units": "esriSRUnit_Meter",
                "outFields": PARCEL_FIELDS,
                "returnGeometry": "false",
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
            raise LookupError(
                f"no parcel found within {SEARCH_RADIUS_M} m of the geocoded point"
            )
        return pick_parcel([f.get("attributes", {}) for f in features], ctx.address)

    # -- shaping -------------------------------------------------------------

    def _build(self, attrs: dict[str, Any], ctx: Context) -> StationResult:
        b = read_buildings(attrs)
        sqft = b["living_sqft"]
        beds = b["beds"]
        baths = b["baths"]
        year_built = b["year_built"]

        tra = normalize_tra(attrs.get("TaxRateArea"))
        use_code = clean(attrs.get("UseCode"))
        roll_land = positive_float(attrs.get("Roll_LandValue")) or 0.0
        roll_imp = positive_float(attrs.get("Roll_ImpValue")) or 0.0
        current_assessed = (roll_land + roll_imp) or None
        homeowners_exemption = positive_float(attrs.get("Roll_HomeOwnersExemp"))
        base_year = positive_int(attrs.get("Roll_LandBaseYear"))

        values: dict[str, Any] = {}
        for key, value in (
            ("parcel_id", clean(attrs.get("AIN")) or clean(attrs.get("APN"))),
            ("situs_address", situs(attrs)),
            ("situs_zip", clean(attrs.get("SitusZIP"))),
            ("tax_rate_area", tra),
            ("tax_rate_city", clean(attrs.get("TaxRateCity"))),
            ("use_code", use_code),
            ("use_description", clean(attrs.get("UseDescription"))),
            ("year_built", year_built),
            ("living_sqft", sqft),
            ("beds", beds),
            ("baths", baths),
            ("units", b["units"]),
            ("roll_year", positive_int(attrs.get("Roll_Year"))),
            ("current_assessed_value", current_assessed),
            ("prop_13_base_year", base_year),
        ):
            if value is not None:
                values[key] = measured(value, ASSESSOR_DOC)

        # The Assessor roll has no utility columns at all — unlike the Spartanburg CAMA
        # record, which carried three free-text utility slots. Rather than infer "public"
        # from the fact that the parcel sits in a dense city, the fact stays unknown and
        # the LA profile turns the hard fail off explicitly, with its reasoning written
        # down in the profile. Inferring it here would launder a guess into a hard pass.
        values["water_sewer"] = derived(
            None,
            note=(
                "the Los Angeles County Assessor roll has no utility fields; water and "
                "sewer service must be confirmed from the seller disclosure or the "
                "LADWP / LA Sanitation account"
            ),
        )

        if homeowners_exemption:
            values["current_homeowners_exemption"] = measured(
                homeowners_exemption,
                EXEMPTION_DOC,
                note=(
                    "the current owner claims the homeowners' exemption, so this is "
                    "their primary residence — the exemption does not transfer and must "
                    "be re-filed after closing"
                ),
            )

        if base_year and current_assessed:
            values["assessed_to_price_ratio"] = derived(
                round(current_assessed / ctx.price, 4) if ctx.price else None,
                note=(
                    f"assessed at ${current_assessed:,.0f} on a {base_year} Proposition 13 "
                    f"base year against a ${ctx.price:,.0f} price — the tax bill resets to "
                    f"the purchase price at close"
                ),
            )

        if b["building_count"] > 1:
            values["multiple_buildings"] = derived(
                b["building_count"],
                note=(
                    f"the Assessor records {b['building_count']} structures on this "
                    f"parcel; area, bed and bath counts above are totals across all of "
                    f"them, not one house"
                ),
            )

        tasks = self._tasks(
            sqft=sqft,
            beds=beds,
            use_code=use_code,
            base_year=base_year,
            current_assessed=current_assessed,
            price=ctx.price,
            homeowners_exemption=homeowners_exemption,
            building_count=b["building_count"],
        )

        ambiguous = attrs.get("_ambiguous")
        if ambiguous:
            neighbours = ", ".join(a for a in ambiguous if a)
            values["parcel_match_ambiguous"] = derived(
                True,
                note=(
                    f"street number did not match any parcel in the search radius; "
                    f"candidates were: {neighbours}"
                ),
            )
            tasks.insert(
                0,
                self.task(
                    f"Confirm which parcel this is — the search radius returned several "
                    f"and none matched the street number ({neighbours})",
                    blocking=True,
                    reason="physical facts may belong to a neighbouring lot",
                ),
            )

        context_updates: dict[str, Any] = {}
        if tra:
            context_updates["tax_rate_area"] = tra
        if current_assessed:
            context_updates["current_assessed_value"] = current_assessed

        return StationResult(
            station=self.name,
            facts={
                "sqft": sqft,
                "beds": beds,
                "baths": baths,
                "year_built": year_built,
                "water_sewer": None,
            },
            values=values,
            context_updates=context_updates,
            tasks=tasks,
        )

    def _tasks(
        self,
        *,
        sqft,
        beds,
        use_code,
        base_year,
        current_assessed,
        price,
        homeowners_exemption,
        building_count,
    ) -> list[dict[str, Any]]:
        tasks: list[dict[str, Any]] = []

        # Every LA run needs this one, because the roll simply cannot answer it.
        tasks.append(
            self.task(
                "Confirm water and sewer service from the seller disclosure — the "
                "Assessor roll carries no utility fields, so this was not scored",
                blocking=False,
                reason="a dense-city address is not evidence of a public connection",
            )
        )
        tasks.append(
            self.task(
                "Get the lot size and garage bay count from the listing — neither is in "
                "the Assessor roll schema",
                blocking=False,
                reason="lot size and garage count exist in the listing, not in this layer",
            )
        )

        if sqft is None:
            tasks.append(
                self.task(
                    "Get living area from the listing or an appraisal — the Assessor "
                    "record left every SQFTmain slot blank",
                    blocking=False,
                    reason="square footage drives both the score and the reserve estimate",
                )
            )
        if beds is None:
            tasks.append(self.task("Confirm bedroom count from the listing", blocking=False))

        if building_count > 1:
            tasks.append(
                self.task(
                    f"Walk the lot — the Assessor records {building_count} structures, so "
                    f"the totals above may include a garage conversion or ADU",
                    blocking=True,
                    reason="an ADU changes the price, the insurance, and possibly the use code",
                )
            )

        if use_code and not use_code.startswith(SFR_PREFIX):
            tasks.append(
                self.task(
                    f"Verify the property type — the Assessor use code is {use_code!r}, "
                    f"which is not single-family residential",
                    blocking=True,
                    reason="use code drives financing eligibility and the tax treatment",
                )
            )

        if base_year and current_assessed and price:
            multiple = price / current_assessed if current_assessed else 0.0
            if multiple >= 1.5:
                tasks.append(
                    self.task(
                        f"Ignore the tax figure on the listing — it reflects a {base_year} "
                        f"Proposition 13 base year of ${current_assessed:,.0f}, and the "
                        f"purchase resets the assessment to about ${price:,.0f}, roughly "
                        f"{multiple:.1f}x higher",
                        blocking=True,
                        reason=(
                            "a stale base year makes the advertised tax cost systematically "
                            "too low, and only in that direction"
                        ),
                    )
                )
            tasks.append(
                self.task(
                    "Budget for a supplemental tax bill after closing — the Assessor "
                    "bills the difference between the old and new assessment separately "
                    "from the annual bill, and a January-to-May closing generates two",
                    blocking=False,
                    reason="the supplemental bill is not in escrow and arrives unannounced",
                )
            )

        tasks.append(
            self.task(
                "File the homeowners' exemption with the Assessor by February 15 after "
                "closing"
                + (
                    " — the seller's exemption is on the current roll but does not transfer"
                    if homeowners_exemption
                    else ""
                ),
                blocking=False,
                reason=(
                    "the $7,000 exemption is worth about $70 a year and drops to 80% if "
                    "filed after February 15"
                ),
            )
        )
        return tasks
