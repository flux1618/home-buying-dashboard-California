"""Solve the Los Angeles target price with the engine's own solver.

Written because the affordability figures that came out of research used a round 1.25% tax
rate and a $2,600 insurance placeholder, and by the time this ran there were measured
numbers for both: tax rate area 00004 at 1.187380% from the Auditor-Controller's own
schedule, and a California DOI average premium. Re-deriving the target with `cost.solve_max_price`
rather than by hand means the number in `buyer_profile.toml` is the same number the
dashboard will compute, which is the only way the two can be guaranteed to agree.

Run: python3 tools/solve_la_target.py
"""

from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from analyzer.core import cost, rate_area  # noqa: E402
from analyzer.core.profile import BuyerProfile, SchoolPrefs  # noqa: E402

CURRENT_YEAR = 2026

# Measured / researched inputs.
INCOME = 406_480.0
DTI = 0.22
RATE = 0.0667
TERM = 360
TRA = "00004"

# California DOI statewide average, and the LA flatland range from the same research.
INSURANCE_SCENARIOS = {
    "CA DOI statewide average": 1_571.0,
    "LA flatland low": 1_851.0,
    "LA flatland midpoint": 2_058.0,
    "LA flatland high": 2_265.0,
    "hillside / FAIR Plan": 6_000.0,
}

# Typical Boyle Heights / Lincoln Heights single-family house, for the reserve band.
SQFT = 1_400
YEAR_BUILT = 1950


def base_profile(annual_insurance: float, down_payment: float) -> BuyerProfile:
    return BuyerProfile(
        name="LA solver scratch profile",
        gross_annual_income=INCOME,
        monthly_non_housing=9_400.0,
        down_payment=down_payment,
        target_front_end_dti=DTI,
        mortgage_rate=RATE,
        loan_term_months=TERM,
        annual_insurance=annual_insurance,
        target_price=1_000_000.0,
        max_commute_min=20.0,
        require_public_water_sewer=False,
        exclude_flood_zones=("A", "AE", "AO", "AH", "V", "VE"),
        min_beds=3,
        min_baths=2,
        min_sqft=1_400,
        min_garage_spaces=2,
        require_fiber=True,
        max_hoa_monthly=100.0,
        penalties={},
        capex_thresholds={
            "roof_due_age": 15,
            "roof_overdue_age": 20,
            "hvac_due_age": 12,
            "hvac_overdue_age": 18,
        },
        capex_penalties={},
        preferred_year_built_min=1980,
        max_price_over_target_pct=0.10,
        max_price_per_sqft=750.0,
        verdict_take_min=75,
        verdict_watch_min=45,
        unevaluated_score=50,
        tax_rate_area=TRA,
        in_city_of_la=True,
        annual_earthquake_premium=0.0,
        schools=SchoolPrefs(),
    )


def solve(annual_insurance: float) -> tuple[float, float, float]:
    """Converge on the price whose own 20% down payment is the down payment used.

    The solver takes the down payment as fixed and returns a price, but a 20%-down
    household has a down payment that depends on the price. Iterating to a fixed point is
    three lines and removes the circularity, rather than picking a down payment and
    reporting a price that implies a different one.
    """
    down = 200_000.0
    lender = household = 0.0
    for _ in range(40):
        profile = base_profile(annual_insurance, down)
        sol = cost.solve_max_price(
            profile=profile,
            sqft=SQFT,
            year_built=YEAR_BUILT,
            current_year=CURRENT_YEAR,
        )
        if not sol.feasible:
            return 0.0, 0.0, down
        lender = sol.lender_max_price or 0.0
        household = sol.household_max_price or 0.0
        new_down = 0.20 * lender
        if abs(new_down - down) < 100.0:
            down = new_down
            break
        down = new_down
    return lender, household, down


def main() -> None:
    area = rate_area.get_schedule(TRA)
    print(f"Tax rate area {TRA} ({area.city}): {area.total_rate_pct():.6f}% "
          f"for FY {area.fiscal_year}")
    print(f"Income ${INCOME:,.0f}/yr -> ${INCOME / 12:,.2f}/mo; "
          f"{DTI:.0%} ceiling = ${INCOME / 12 * DTI:,.2f}/mo PITI")
    print(f"Rate {RATE:.2%}, {TERM // 12}-year term, 20% down (solved to a fixed point)")
    print()
    header = f"{'insurance scenario':<26} {'annual':>9} {'lender max':>13} {'household max':>14} {'20% down':>11}"
    print(header)
    print("-" * len(header))
    for label, premium in INSURANCE_SCENARIOS.items():
        lender, household, down = solve(premium)
        print(
            f"{label:<26} {premium:>9,.0f} {lender:>13,.0f} {household:>14,.0f} {down:>11,.0f}"
        )

    print()
    chosen = INSURANCE_SCENARIOS["LA flatland midpoint"]
    lender, household, down = solve(chosen)
    profile = replace(base_profile(chosen, down), target_price=household,
                      annual_earthquake_premium=1_800.0)
    breakdown = cost.compute(
        profile, household, SQFT, YEAR_BUILT, 0.0, CURRENT_YEAR, owner_occupied=True
    )
    print("=== chosen basis: LA flatland midpoint insurance, household ceiling ===")
    d = breakdown.to_dict()
    print(f"  price                      {household:,.2f}")
    for key in (
        "loan_amount",
        "principal_interest",
        "monthly_tax",
        "monthly_insurance",
        "monthly_earthquake",
        "piti",
        "front_end_dti",
        "maintenance_reserve_low",
        "maintenance_reserve_high",
        "true_monthly_low",
        "true_monthly_high",
        "cash_to_close",
        "transfer_tax_total",
        "transfer_tax_buyer_share",
    ):
        val = d[key]
        print(f"  {key:<26} {val:,.4f}" if key == "front_end_dti" else f"  {key:<26} {val:,.2f}")
    print(f"  down payment (20%)         {down:,.2f}")


if __name__ == "__main__":
    main()
