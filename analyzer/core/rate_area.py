"""Los Angeles County tax rate areas (TRAs).

The California analogue of a South Carolina millage schedule, but the shape of the
problem is different in one important way.

In SC the interesting question was *which components an owner-occupier stops paying*
(school operating millage is exempt, school bond millage is not), so the schedule had
to be modeled per component with an `exempt_for_primary` flag.

In CA there is no ratio and no per-component exemption. Proposition 13 caps the general
levy at 1% of assessed value, and everything above 1% is voter-approved debt service
that every owner in the TRA pays regardless of occupancy. The owner-occupier break is a
flat $7,000 reduction in assessed value (the homeowners' exemption), not a rate change.
So the per-component modeling here exists for *explanation* -- showing a buyer that in
central Los Angeles roughly 90% of their above-cap tax is school and community-college
bond debt -- rather than for computing two different rates.

Los Angeles County contains 13,197 TRAs. Rates below are the published FY2025-26
figures and are `measured`, not estimated: each one was cross-checked against two
independent county sources that agree to six decimal places.

Sources:
  https://onlineapps.auditor.lacounty.gov/tra/
  https://auditor.lacounty.gov/wp-content/uploads/2025/09/Highest-Value-TRA-Report-FY-2025-2026_SORTED.xlsx
  https://auditor.lacounty.gov/wp-content/uploads/2026/05/2025-2026-Taxpayers-Guide.pdf

Unlike the SC module, the TRA for a specific parcel is not guessed: the LA County
Assessor parcel layer returns a `TaxRateArea` field directly, so the lookup chain
runs parcel geometry -> TaxRateArea -> the published table below without an
intervening estimate. See analyzer/sources/parcel.py.
"""

from __future__ import annotations

from dataclasses import dataclass

TRA_LOOKUP = "https://onlineapps.auditor.lacounty.gov/tra/"
TYPICAL_TRA_REPORT = (
    "https://auditor.lacounty.gov/wp-content/uploads/2025/09/"
    "Highest-Value-TRA-Report-FY-2025-2026_SORTED.xlsx"
)
TAXPAYERS_GUIDE = (
    "https://auditor.lacounty.gov/wp-content/uploads/2026/05/"
    "2025-2026-Taxpayers-Guide.pdf"
)

FISCAL_YEAR = "2025-26"

# Proposition 13's cap on the general levy, as a fraction of assessed value.
# California Constitution, Article XIII A, Section 1.
GENERAL_LEVY_RATE = 0.01
PROP_13_SOURCE = "https://law.justia.com/constitution/california/article-xiii-a/section-1/"

# Kinds of line item on a California tax bill.
GENERAL_LEVY = "general_levy"  # the Prop 13 1% cap
VOTER_DEBT = "voter_debt"  # bond debt service approved above the cap


@dataclass(frozen=True)
class RateComponent:
    """One ad valorem line item on a Los Angeles County tax bill.

    `rate_pct` is a percentage of assessed value, matching how the county publishes
    it (1.0 means 1%). Divide by 100 before multiplying against a value.

    Unlike SC millage there is no `exempt_for_primary` flag, because no California
    component is waived for owner-occupiers. The homeowners' exemption reduces the
    assessed value instead. See analyzer/core/tax.py.
    """

    name: str
    rate_pct: float
    kind: str = VOTER_DEBT


@dataclass(frozen=True)
class RateArea:
    """A published TRA rate schedule for one fiscal year."""

    tra: str
    city: str
    components: tuple[RateComponent, ...]
    source_url: str
    note: str
    fiscal_year: str = FISCAL_YEAR

    def total_rate_pct(self) -> float:
        """Full published rate, as a percentage of assessed value."""
        return round(sum(c.rate_pct for c in self.components), 6)

    def total_rate(self) -> float:
        """Full published rate, as a fraction. This is what tax math uses."""
        return self.total_rate_pct() / 100.0

    def general_levy_pct(self) -> float:
        """The Prop 13 1% general levy portion."""
        return round(
            sum(c.rate_pct for c in self.components if c.kind == GENERAL_LEVY), 6
        )

    def voter_debt_pct(self) -> float:
        """Voter-approved debt service above the 1% cap."""
        return round(sum(c.rate_pct for c in self.components if c.kind == VOTER_DEBT), 6)

    def school_debt_pct(self) -> float:
        """Portion of voter debt attributable to school and community-college bonds.

        Surfaced on its own because it is the single most useful explanatory number
        for a buyer asking why their rate is not simply 1%.
        """
        return round(
            sum(
                c.rate_pct
                for c in self.components
                if c.kind == VOTER_DEBT
                and ("SCHOOL" in c.name.upper() or "COLLEGE" in c.name.upper())
            ),
            6,
        )

    def breakdown(self) -> list[dict[str, object]]:
        return [
            {"name": c.name, "rate_pct": c.rate_pct, "kind": c.kind}
            for c in self.components
        ]


def _city_of_la_components() -> tuple[RateComponent, ...]:
    """FY2025-26 component rates for the City of Los Angeles bundle.

    Verified identical across TRAs 00004, 00005, 00006, 00008 and 00067 -- the
    City of LA + LAUSD + LACCD + Metropolitan Water District combination that
    covers central Los Angeles including 90033. Total 1.187380%.
    """
    return (
        RateComponent("GENERAL (Prop 13 1% cap)", 1.000000, GENERAL_LEVY),
        RateComponent("UNIFIED SCHOOLS (LAUSD debt service)", 0.119605),
        RateComponent("COMMNTY COLLEGE (LACCD debt service)", 0.048543),
        RateComponent("CITY-LOS ANGELES", 0.012232),
        RateComponent("METRO WATER DIST", 0.007000),
    )


_CITY_OF_LA_NOTE = (
    "MEASURED - FY2025-26 published component rates for the City of Los Angeles "
    "bundle (City + LAUSD + LACCD + MWD), cross-checked against the county's TRA "
    "rate lookup and the Typical TRA Listing report. Excludes non-ad-valorem direct "
    "assessments, which are flat dollar charges and are handled separately."
)

# Central Los Angeles, including the 90033 anchor area. The Assessor parcel layer
# returns "00004" for residential parcels near the anchor; "00067" is the county's
# published typical TRA for the City of Los Angeles. Both resolve to the same rate.
_CITY_OF_LA_TRAS = ("00004", "00005", "00006", "00008", "00067")

# Low-numbered central-county TRAs are useful regression anchors because they sit
# next to the City of LA bundle but do not all carry the same debt rate. The
# Auditor lookup publishes their totals; its component display must be consulted
# for a parcel-level agency breakdown.
_EARLY_TRA_RATES: tuple[tuple[str, float], ...] = (
    ("00001", 1.247089),
    ("00002", 1.247089),
    ("00003", 1.181000),
)

# Neighboring cities, from the county's Typical TRA Listing for FY2025-26. These are
# the *typical* TRA for each city, not every TRA within it, so a specific parcel in
# these cities may carry a different TRA. Used for submarket comparison.
_TYPICAL_CITY_RATES: tuple[tuple[str, str, float], ...] = (
    ("04045", "Glendale", 1.085398),
    ("09030", "South Pasadena", 1.099608),
    ("02530", "Burbank", 1.107902),
    ("07500", "Pasadena", 1.120618),
    ("06518", "Commerce", 1.137477),
    ("03170", "Culver City", 1.138777),
    ("08216", "Rosemead", 1.160635),
    ("09642", "Whittier", 1.173245),
    ("01797", "Alhambra", 1.182878),
    ("05500", "Long Beach", 1.269098),
    ("01840", "Monterey Park", 1.274053),
    ("06330", "Montebello", 1.335352),
    ("08467", "San Gabriel", 1.338763),
)


def _build_schedules() -> dict[str, RateArea]:
    schedules: dict[str, RateArea] = {}

    for tra in _CITY_OF_LA_TRAS:
        schedules[tra] = RateArea(
            tra=tra,
            city="Los Angeles",
            components=_city_of_la_components(),
            source_url=TRA_LOOKUP,
            note=_CITY_OF_LA_NOTE,
        )

    for tra, total in _EARLY_TRA_RATES:
        schedules[tra] = RateArea(
            tra=tra,
            city="Los Angeles County",
            components=(
                RateComponent("GENERAL (Prop 13 1% cap)", 1.000000, GENERAL_LEVY),
                RateComponent(
                    "Voter-approved debt service (not itemized)",
                    round(total - 1.000000, 6),
                ),
            ),
            source_url=TRA_LOOKUP,
            note=(
                f"MEASURED total - FY2025-26 published total for TRA {tra} from the "
                "LA County Auditor-Controller TRA lookup. The 1% / debt split is "
                "arithmetic under Prop 13; consult the lookup for the parcel's "
                "agency-level component detail."
            ),
        )

    for tra, city, total in _TYPICAL_CITY_RATES:
        # The county publishes an itemized breakdown per TRA, but only the City of
        # LA bundle was itemized during research. For the comparison set the total
        # is measured while the split between cap and debt is arithmetic: everything
        # above 1.0 is voter debt by definition under Prop 13.
        schedules[tra] = RateArea(
            tra=tra,
            city=city,
            components=(
                RateComponent("GENERAL (Prop 13 1% cap)", 1.000000, GENERAL_LEVY),
                RateComponent(
                    "Voter-approved debt service (not itemized)",
                    round(total - 1.000000, 6),
                ),
            ),
            source_url=TYPICAL_TRA_REPORT,
            note=(
                f"MEASURED total - FY2025-26 typical TRA for {city} from the county's "
                "Typical TRA Listing report. The 1% / debt split is arithmetic under "
                "Prop 13; the individual taxing agencies behind the debt line were not "
                "itemized. This is the city's typical TRA, not necessarily the TRA for "
                "a specific parcel -- read TaxRateArea off the Assessor parcel record."
            ),
        )

    return schedules


SCHEDULES: dict[str, RateArea] = _build_schedules()

# Fallback when a parcel's TRA cannot be resolved. The Auditor-Controller states the
# countywide total "does not exceed 1.25%"; 1.20% sits inside the 1.15-1.25% band that
# covers most LA County residential TRAs. This is the one genuinely estimated rate in
# the module, and it is labeled as such wherever it surfaces.
COUNTYWIDE_FALLBACK = RateArea(
    tra="unresolved",
    city="Los Angeles County",
    components=(
        RateComponent("GENERAL (Prop 13 1% cap)", 1.000000, GENERAL_LEVY),
        RateComponent("Assumed voter-approved debt service", 0.200000),
    ),
    source_url=TAXPAYERS_GUIDE,
    note=(
        "ESTIMATE - no tax rate area resolved for this parcel, so a 1.20% countywide "
        "placeholder is used. LA County has 13,197 TRAs and observed residential "
        "totals in this study range from 1.085% to 1.339%, so this figure can be off "
        "by roughly 15 basis points in either direction. Read the actual TaxRateArea "
        "off the Assessor record before relying on the tax figure for an offer."
    ),
)


def normalize_tra(tra: str | int | None) -> str | None:
    """Canonicalize a TRA to the county's zero-padded 5-digit form.

    The Assessor parcel layer returns "00004" while the Auditor's lookup form wants
    "4", and profile files are written by humans who may use either. Normalizing on
    the way in means the rest of the system only ever sees one shape.
    """
    if tra is None:
        return None
    text = str(tra).strip()
    if not text:
        return None
    if not text.isdigit():
        return None
    return text.zfill(5)


def get_schedule(tra: str | int | None) -> RateArea:
    """Resolve a TRA to its published rate schedule, falling back to the estimate."""
    key = normalize_tra(tra)
    if key is None:
        return COUNTYWIDE_FALLBACK
    return SCHEDULES.get(key, COUNTYWIDE_FALLBACK)


def is_measured(area: RateArea) -> bool:
    """True when the schedule came from the published table rather than the fallback."""
    return area.tra != "unresolved"
