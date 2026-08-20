"""Buyer profile: constraints as configuration, not as constants in code.

Parameterizing the household's rules is what turns "a tool for one person" into a
decision engine that can be pointed at a different buyer. Adding a persona is a TOML
file, not a code change.

TOML is read with stdlib `tomllib` (3.11+), so `core/` stays dependency-free per ADR 0002.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

# The rulebook is deployment configuration, not package data, so it has to be *found*
# rather than assumed. Three places are searched, in this order:
#
#   1. $HBA_PROFILE          — an explicit answer always wins
#   2. next to the package   — correct in a source checkout
#   3. the working directory — correct when the package is installed but config is local
#
# Step 2 alone was the original implementation and it is subtly wrong: once the package is
# pip-installed, `parents[2]` resolves inside site-packages, where no profile lives. That
# broke the container (it started fine and failed on the first request) and then broke CI,
# where installing the package made the tests import from site-packages. Step 3 is what
# makes "run it from the repo" work regardless of how the code was installed.
#
# Resolved at call time, not import time. An import-time constant cannot be influenced by a
# test fixture or a late environment change, which is a genuinely confusing failure: you set
# the variable, and the value was frozen three imports ago.
#
# stdlib `os` only, so ADR 0002's purity rule still holds.
def default_profile_path() -> Path:
    override = os.environ.get("HBA_PROFILE")
    if override:
        return Path(override)

    beside_package = Path(__file__).resolve().parents[2] / "buyer_profile.toml"
    if beside_package.exists():
        return beside_package

    return Path.cwd() / "buyer_profile.toml"


@dataclass(frozen=True)
class Anchor:
    """A commute destination. A list of these, so a second campus is config."""

    label: str
    address: str
    lat: float
    lon: float
    arrival_window: str
    source_url: str


@dataclass(frozen=True)
class SchoolPrefs:
    """School quality as a scored preference, not a footnote.

    `min_proficiency_pct` is the household's floor on the average of the ELA and Math
    "met or exceeded standard" percentages from CAASPP, California's own assessment.
    A measured state test percentage is used rather than a third-party 1-10 composite
    so the number that drives the score can be traced to a primary source.

    The `zone_of_choice` penalty key exists because a large part of LAUSD does not assign
    a single school by address. Inside a Zone of Choice a family ranks preferences and
    receives a lottery result, so a good zoned school is a possibility rather than a
    guarantee. Scoring that uncertainty is more honest than reporting the best nearby
    school as though it were assigned.
    """

    min_proficiency_pct: float = 55.0
    well_below_margin_pct: float = 15.0
    levels: tuple[str, ...] = ("elementary", "middle", "high")
    penalties: dict[str, int] = field(default_factory=dict)

    def penalty(self, key: str, default: int = 0) -> int:
        return int(self.penalties.get(key, default))


@dataclass(frozen=True)
class BuyerProfile:
    name: str
    # finance
    gross_annual_income: float
    monthly_non_housing: float
    down_payment: float
    target_front_end_dti: float
    mortgage_rate: float
    loan_term_months: int
    annual_insurance: float
    target_price: float
    # hard fails
    max_commute_min: float
    require_public_water_sewer: bool
    exclude_flood_zones: tuple[str, ...]
    # scored preferences -> penalty points
    min_beds: int
    min_baths: int
    min_sqft: int
    min_garage_spaces: int
    require_fiber: bool
    max_hoa_monthly: float
    penalties: dict[str, int]
    # near-term capital expenses — age thresholds and their deductions
    capex_thresholds: dict[str, int]
    capex_penalties: dict[str, int]
    # caveats
    preferred_year_built_min: int
    max_price_over_target_pct: float
    max_price_per_sqft: float
    # verdict bands
    verdict_take_min: int
    verdict_watch_min: int
    # score assigned when a hard fail could not be evaluated
    unevaluated_score: int
    anchors: tuple[Anchor, ...] = field(default_factory=tuple)

    # Los Angeles County tax rate area, zero-padded 5 digits (e.g. "00004"). Optional:
    # the Assessor parcel record carries a TaxRateArea field, so a per-parcel run
    # resolves this from data rather than config. The profile value is the default used
    # for submarket-level modeling before a specific parcel is known.
    tax_rate_area: str | None = None

    # True when the subject property sits inside the City of Los Angeles, which levies
    # its own documentary transfer tax on top of the county rate. Set False for
    # Alhambra, South Pasadena, Monterey Park and the other independent cities.
    in_city_of_la: bool = True

    # Earthquake coverage is excluded from every standard California homeowners policy,
    # so it is a separate optional premium. Defaults to 0.0 so a profile that declines
    # coverage still loads, but declining it is a decision, not an absence of cost.
    annual_earthquake_premium: float = 0.0

    # School quality preferences. Defaults are permissive so a profile written before
    # the school station existed still loads and simply applies no school penalties.
    schools: SchoolPrefs = field(default_factory=SchoolPrefs)

    # Which FEMA NRI hazards to report, as four-letter codes, and the percentile at
    # which one earns a caveat. Regional, not personal: South Carolina cares about wind
    # and tornado, California cares about wildfire and earthquake, and neither list is a
    # code change. Defaults are empty and 90.0 so a profile written before the risk
    # station existed still loads and simply reports no hazards.
    hazards: tuple[str, ...] = ()
    hazard_caveat_percentile: float = 90.0

    @property
    def monthly_income(self) -> float:
        return self.gross_annual_income / 12.0

    @property
    def primary_anchor(self) -> Anchor:
        if not self.anchors:
            raise ValueError("profile has no commute anchors")
        return self.anchors[0]


def load_profile(path: Path | str | None = None) -> BuyerProfile:
    path = Path(path) if path else default_profile_path()
    if not path.exists():
        # A bare FileNotFoundError pointing into site-packages sends people looking for a
        # packaging bug. The actual problem is almost always "the config is somewhere else".
        raise FileNotFoundError(
            f"buyer profile not found at {path}. Set HBA_PROFILE to its location, or run "
            f"from a directory containing buyer_profile.toml."
        )
    with open(path, "rb") as fh:
        raw = tomllib.load(fh)

    fin = raw["finance"]
    hard = raw["hard_fails"]
    pref = raw["preferences"]
    capex = raw["capital_expenses"]
    cav = raw["caveats"]
    verdict = raw["verdict"]
    # Optional: a profile predating the risk station has no [risk] table at all.
    risk = raw.get("risk", {})
    # Same for schools. Absent table means "no school preferences", not "score zero".
    sch = raw.get("schools", {})
    schools = SchoolPrefs(
        min_proficiency_pct=float(sch.get("min_proficiency_pct", 55.0)),
        well_below_margin_pct=float(sch.get("well_below_margin_pct", 15.0)),
        levels=tuple(sch.get("levels", ("elementary", "middle", "high"))),
        penalties=dict(sch.get("penalties", {})),
    )

    tax_cfg = raw.get("tax", {})

    anchors = tuple(
        Anchor(
            label=a["label"],
            address=a["address"],
            lat=a["lat"],
            lon=a["lon"],
            arrival_window=a["arrival_window"],
            source_url=a["source_url"],
        )
        for a in raw.get("anchor", [])
    )

    return BuyerProfile(
        name=raw["name"],
        gross_annual_income=fin["gross_annual_income"],
        monthly_non_housing=fin["monthly_non_housing"],
        down_payment=fin["down_payment"],
        target_front_end_dti=fin["target_front_end_dti"],
        mortgage_rate=fin["mortgage_rate"],
        loan_term_months=fin["loan_term_months"],
        annual_insurance=fin["annual_insurance"],
        target_price=fin["target_price"],
        max_commute_min=hard["max_commute_min"],
        require_public_water_sewer=hard["require_public_water_sewer"],
        exclude_flood_zones=tuple(hard["exclude_flood_zones"]),
        min_beds=pref["min_beds"],
        min_baths=pref["min_baths"],
        min_sqft=pref["min_sqft"],
        min_garage_spaces=pref["min_garage_spaces"],
        require_fiber=pref["require_fiber"],
        max_hoa_monthly=pref["max_hoa_monthly"],
        penalties=dict(pref["penalties"]),
        capex_thresholds={k: v for k, v in capex.items() if k.endswith("_age")},
        capex_penalties=dict(capex["penalties"]),
        preferred_year_built_min=cav["preferred_year_built_min"],
        max_price_over_target_pct=cav["max_price_over_target_pct"],
        max_price_per_sqft=cav["max_price_per_sqft"],
        verdict_take_min=verdict["take_min"],
        verdict_watch_min=verdict["watch_min"],
        unevaluated_score=verdict["unevaluated_score"],
        anchors=anchors,
        tax_rate_area=tax_cfg.get("tax_rate_area"),
        in_city_of_la=bool(tax_cfg.get("in_city_of_la", True)),
        annual_earthquake_premium=float(fin.get("annual_earthquake_premium", 0.0)),
        schools=schools,
        hazards=tuple(risk.get("hazards", ())),
        hazard_caveat_percentile=float(risk.get("caveat_percentile", 90.0)),
    )
