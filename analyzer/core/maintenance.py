"""Maintenance reserve: three rules of thumb, deliberately not averaged.

Averaging three rules of thumb produces one number that looks precise and isn't. The
spread between them is the honest answer, so all three are returned and the caller is
expected to show the range.

All three are ESTIMATES. None has empirical backing for a specific house.
"""

from __future__ import annotations

from dataclasses import dataclass

# No authoritative source exists for rules of thumb, so cite the reasoning instead of
# dressing a heuristic up as a measurement.
RULE_OF_THUMB_SOURCE = "https://github.com/flux1618/home-buying-dashboard/blob/main/docs/KNOWN_LIMITATIONS.md"

PERCENT_OF_PRICE_RATE = 0.01
PER_SQFT_ANNUAL = 1.00

AGE_BANDS = ((10, 0.010), (30, 0.015), (None, 0.020))


@dataclass(frozen=True)
class ReserveEstimate:
    method: str
    monthly: float
    annual: float
    basis: str

    def to_dict(self) -> dict[str, object]:
        return {
            "method": self.method,
            "monthly": round(self.monthly, 2),
            "annual": round(self.annual, 2),
            "basis": self.basis,
            "confidence": "estimated",
        }


def percent_of_price(price: float) -> ReserveEstimate:
    annual = price * PERCENT_OF_PRICE_RATE
    return ReserveEstimate(
        "percent_of_price", annual / 12.0, annual, "1% of purchase price per year"
    )


def per_sqft(sqft: float | None) -> ReserveEstimate | None:
    if not sqft:
        return None
    annual = sqft * PER_SQFT_ANNUAL
    return ReserveEstimate(
        "per_sqft", annual / 12.0, annual, f"$1.00/sqft/yr on {int(sqft):,} sqft"
    )


def age_scaled_rate(age_years: int) -> float:
    for ceiling, rate in AGE_BANDS:
        if ceiling is None or age_years < ceiling:
            return rate
    return AGE_BANDS[-1][1]


def age_scaled(
    price: float, year_built: int | None, current_year: int
) -> ReserveEstimate | None:
    if not year_built:
        return None
    age = max(0, current_year - year_built)
    rate = age_scaled_rate(age)
    annual = price * rate
    return ReserveEstimate(
        "age_scaled",
        annual / 12.0,
        annual,
        f"{rate * 100:.1f}% of price per year (built {year_built}, age {age})",
    )


def all_methods(
    price: float, sqft: float | None, year_built: int | None, current_year: int
) -> list[ReserveEstimate]:
    candidates = [
        percent_of_price(price),
        per_sqft(sqft),
        age_scaled(price, year_built, current_year),
    ]
    return [c for c in candidates if c is not None]


def reserve_block(
    price: float, sqft: float | None, year_built: int | None, current_year: int
) -> dict[str, object]:
    methods = all_methods(price, sqft, year_built, current_year)
    monthlies = [m.monthly for m in methods]
    return {
        "methods": {m.method: m.to_dict() for m in methods},
        "monthly_low": round(min(monthlies), 2),
        "monthly_high": round(max(monthlies), 2),
        "source_url": RULE_OF_THUMB_SOURCE,
        "note": (
            "Three industry rules of thumb, shown separately and never averaged. The "
            "spread is the honest answer. All are estimates."
        ),
    }
