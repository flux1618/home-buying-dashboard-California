"""Assemble the output document. Pure — takes facts, returns the result dict.

Network stations (geocode, county GIS, FEMA, OSRM, FCC) populate `PropertyFacts` and
`Degradation` objects and hand them here. This module never fetches anything, which is
why the whole analysis is unit-testable without a single HTTP mock.
"""

from __future__ import annotations

from typing import Any

from . import capex, cost, maintenance, scoring, tax
from .profile import BuyerProfile
from .provenance import Degradation, now_iso
from .scoring import PropertyFacts

ENGINE_VERSION = "0.2.0"


def verification_tasks(
    facts: PropertyFacts, result: scoring.ScoreResult, profile: BuyerProfile
) -> list[dict[str, Any]]:
    """Tasks a human must do. Blocking ones gate an offer."""
    tasks: list[dict[str, Any]] = []

    def add(task: str, blocking: bool, reason: str | None = None) -> None:
        entry: dict[str, Any] = {"task": task, "blocking": blocking}
        if reason:
            entry["reason"] = reason
        tasks.append(entry)

    # Broadband verification is permanent, not conditional. FCC "available" means the
    # provider claims it can install within 10 business days, not that service exists.
    add(
        "Call the ISP with the exact street address to confirm fiber serviceability",
        blocking=True,
        reason="FCC data is census-block precision and provider-reported",
    )

    if facts.commute_min is not None:
        margin = abs(facts.commute_min - profile.max_commute_min)
        if margin <= 2:
            add(
                f"Drive the route at {profile.primary_anchor.arrival_window} before "
                f"trusting the {facts.commute_min:.1f} min estimate",
                blocking=True,
                reason=f"within {margin:.1f} min of the hard limit",
            )

    for item in result.unevaluated_hard_fails:
        add(f"Resolve before offer: {item}", blocking=True)

    # A four- or five-figure bill arriving soon changes the offer, so pricing it is
    # blocking rather than advisory.
    if result.capital_expenses:
        components = ", ".join(e.component.lower() for e in result.capital_expenses)
        add(
            f"Get contractor quotes for {components} and negotiate a credit — "
            f"estimated ${result.capex_low:,.0f}-${result.capex_high:,.0f} total",
            blocking=result.capex_high >= capex.QUOTE_THRESHOLD,
            reason="planning ranges banded by home size, not a quote on this house",
        )
    for expense in result.capital_expenses:
        if expense.urgency == "overdue":
            add(
                f"{expense.component} is overdue — confirm current condition at the "
                f"showing before spending money on an inspection",
                blocking=True,
            )

    add("Get an actual insurance quote before the offer", blocking=False,
        reason="the California DOI statewide average is a placeholder, and Los Angeles "
               "premiums vary by a factor of three between flatland and hillside")
    add("Get a separate earthquake quote — the standard homeowners policy excludes it",
        blocking=False,
        reason="earthquake coverage is optional, uncounted by lenders, and a four-figure "
               "annual cost that the household either pays or absorbs as risk")
    add("Confirm the exact tax rate area on the LA County Assessor parcel record",
        blocking=False,
        reason="the profile's rate area is a default until the parcel resolves its own")
    add("File the homeowners' exemption with the LA County Assessor by February 15 "
        "after closing", blocking=False,
        reason="the exemption does not transfer from the seller and drops to 80% if filed "
               "late")

    if facts.school_proficiency_pct is None:
        add("Resolve the zoned schools and their CAASPP results for this address",
            blocking=False,
            reason="school quality is a scored preference in this profile and no "
                   "proficiency figure was available")
    if facts.school_zone_of_choice:
        add("Check the Zone of Choice application window — the high school here is "
            "assigned by lottery, not by address", blocking=False)

    if facts.roof_age_years is None or facts.hvac_age_years is None:
        add("Get roof and HVAC ages from the seller's disclosure", blocking=False)

    return tasks


def analyze(
    facts: PropertyFacts,
    profile: BuyerProfile,
    current_year: int,
    address: str = "",
    degradations: list[Degradation] | None = None,
) -> dict[str, Any]:
    """Full result document. Deterministic given the same inputs."""
    degradations = degradations or []

    result = scoring.score(facts, profile, current_year)
    costs = cost.compute(
        profile=profile,
        price=facts.price,
        sqft=facts.sqft,
        year_built=facts.year_built,
        hoa_monthly=facts.hoa_monthly or 0.0,
        current_year=current_year,
    )

    return {
        "engine_version": ENGINE_VERSION,
        "analyzed_at": now_iso(),
        "profile": profile.name,
        "input": {
            "address": address,
            "price": facts.price,
            "sqft": facts.sqft,
            "beds": facts.beds,
            "baths": facts.baths,
            "year_built": facts.year_built,
            "garage_spaces": facts.garage_spaces,
            "hoa_monthly": facts.hoa_monthly,
            # Echoed because they drive the capital-expense deductions, and a reader who
            # disagrees with a $28,000 estimate needs to see which ages produced it.
            "roof_age_years": facts.roof_age_years,
            "hvac_age_years": facts.hvac_age_years,
            "water_sewer": facts.water_sewer,
            "flood_zone": facts.flood_zone,
            "commute_min": facts.commute_min,
            "fiber_available": facts.fiber_available,
            "school_district": facts.school_district,
            "school_proficiency_pct": facts.school_proficiency_pct,
            "school_zone_of_choice": facts.school_zone_of_choice,
        },
        "tax": tax.tax_block(facts.price, profile.tax_rate_area),
        "schools": {
            "district": facts.school_district,
            "proficiency_pct": facts.school_proficiency_pct,
            "min_proficiency_pct": profile.schools.min_proficiency_pct,
            "meets_floor": (
                None
                if facts.school_proficiency_pct is None
                else facts.school_proficiency_pct >= profile.schools.min_proficiency_pct
            ),
            "zone_of_choice": facts.school_zone_of_choice,
            "levels_considered": list(profile.schools.levels),
            "metric": (
                "mean of the ELA and math 'met or exceeded standard' percentages, "
                "CAASPP all grades, all students, taken at the weakest zoned level"
            ),
            "source_url": "https://caaspp-elpac.ets.org/caaspp/",
        },
        "maintenance_reserve": maintenance.reserve_block(
            facts.price, facts.sqft, facts.year_built, current_year
        ),
        "capital_expenses": capex.block(result.capital_expenses),
        "cost": costs.to_dict(),
        "commute": {
            "anchor": profile.primary_anchor.label,
            "anchor_address": profile.primary_anchor.address,
            "arrival_window": profile.primary_anchor.arrival_window,
            "rush_hour_min": facts.commute_min,
            "limit_min": profile.max_commute_min,
            "source_url": profile.primary_anchor.source_url,
        },
        "score": result.to_dict(),
        "verification_tasks": verification_tasks(facts, result, profile),
        "degraded_sources": [d.to_dict() for d in degradations],
    }
