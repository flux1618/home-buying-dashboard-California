"""California property tax under Proposition 13. Both scenarios, always.

Three facts drive every calculation here:

1. Prop 13 caps the general levy at 1% of assessed value. Anything above 1% on a bill
   is voter-approved debt service, which varies by tax rate area.
2. Assessed value is a *base year value* set at the purchase price, then factored
   upward by at most 2% per year for as long as the owner holds the property.
3. A change in ownership resets the base year value to the new purchase price.

Together those produce the inverse of the South Carolina problem this codebase was
originally written for. In SC the listing's tax line could mislead in either direction,
because the 4%/6% assessment ratio might be about to fall as well as rise. In California
it misleads in essentially one direction: a seller who has held the property for years
has a base year value far below market, so the tax figure on the listing is
systematically *too low* for the buyer, sometimes by a factor of three or more.

That asymmetry is the single most valuable thing this module can tell a buyer, so it is
computed explicitly rather than left as a caveat. See `listing_basis_comparison()`.

Sources:
  https://law.justia.com/constitution/california/article-xiii-a/section-1/
  https://www.boe.ca.gov/proptaxes/pdf/pub29.pdf
  https://www.boe.ca.gov/proptaxes/pdf/ah401.pdf
"""

from __future__ import annotations

from dataclasses import dataclass

from .provenance import Value, estimated, measured
from .rate_area import (
    GENERAL_LEVY_RATE,
    PROP_13_SOURCE,
    RateArea,
    get_schedule,
    is_measured,
)

BOE_PUB_29 = "https://www.boe.ca.gov/proptaxes/pdf/pub29.pdf"
BOE_AH_401 = "https://www.boe.ca.gov/proptaxes/pdf/ah401.pdf"
BOE_PUB_800_6 = "https://boe.ca.gov/pdf/pub800-6.pdf"
RTC_75_41 = "https://codes.findlaw.com/ca/revenue-and-taxation-code/rtc-sect-75-41/"
LA_ASSESSOR_EXEMPTIONS = "https://assessor.lacounty.gov/homeowners-exemption"

# The homeowners' exemption reduces assessed value by a flat $7,000. It has not been
# adjusted since 1972, which is why it is worth roughly $70/yr rather than anything
# material -- a fact worth stating plainly instead of implying it is a real break.
HOMEOWNERS_EXEMPTION = 7_000.0

# Maximum annual inflation factor on a base year value. California Constitution
# Article XIII A Section 2: the lesser of 2% or the change in CPI.
MAX_ANNUAL_FACTOR = 0.02

# Proration factors for a supplemental assessment, by month the change in ownership
# occurs. Revenue & Taxation Code Section 75.41. A fiscal year runs July-June, so a
# July closing is assessed for the full remaining year (1.00) while a June closing is
# assessed for almost none of it (0.08).
SUPPLEMENTAL_PRORATION: dict[int, float] = {
    1: 0.50,
    2: 0.42,
    3: 0.33,
    4: 0.25,
    5: 0.17,
    6: 0.08,
    7: 1.00,
    8: 0.92,
    9: 0.83,
    10: 0.75,
    11: 0.67,
    12: 0.58,
}


@dataclass(frozen=True)
class TaxScenario:
    """One assessed-value scenario and the annual tax it produces."""

    label: str
    market_value: float
    exemption: float
    assessed_value: float
    applicable_rate: float
    annual_tax: float
    monthly_tax: float

    def to_dict(self) -> dict[str, object]:
        return {
            "label": self.label,
            "market_value": round(self.market_value, 2),
            "exemption": round(self.exemption, 2),
            "assessed_value": round(self.assessed_value, 2),
            "applicable_rate": round(self.applicable_rate, 8),
            "applicable_rate_pct": round(self.applicable_rate * 100.0, 6),
            "annual_tax": round(self.annual_tax, 2),
            "monthly_tax": round(self.monthly_tax, 2),
        }


def base_year_value(price: float) -> float:
    """Base year value is the purchase price at a change in ownership."""
    if price < 0:
        raise ValueError("price cannot be negative")
    return price


def factored_base_year_value(
    base: float, years_held: int, annual_factor: float = MAX_ANNUAL_FACTOR
) -> float:
    """Base year value grown by the inflation factor, compounded annually.

    This is what makes a long-held California property cheap to own: the cap compounds
    at 2% while the market does not.
    """
    if base < 0:
        raise ValueError("base cannot be negative")
    if years_held < 0:
        raise ValueError("years_held cannot be negative")
    # Callers may pass an observed CPI change, but Article XIII A permits no
    # more than 2% growth in a year. The cap is a ceiling, not an assumption
    # that every year grows at exactly 2%.
    capped_factor = min(annual_factor, MAX_ANNUAL_FACTOR)
    return base * ((1.0 + capped_factor) ** years_held)


def taxable_value(factored_value: float, market_value: float | None = None) -> float:
    """Taxable value is the lesser of the factored base year value and market value.

    The second half of that rule is Proposition 8, and it only matters in a falling
    market. Ignoring it would overstate tax after a downturn.
    """
    if factored_value < 0:
        raise ValueError("factored_value cannot be negative")
    if market_value is None:
        return factored_value
    return min(factored_value, market_value)


def assessed_value(price: float, exemption: float = 0.0) -> float:
    """Assessed value net of exemptions. Never negative."""
    if price < 0:
        raise ValueError("price cannot be negative")
    return max(0.0, price - exemption)


def annual_tax(price: float, rate: float, exemption: float = 0.0) -> float:
    """annual tax = (assessed value - exemptions) x total TRA rate."""
    if rate < 0:
        raise ValueError("rate cannot be negative")
    return assessed_value(price, exemption) * rate


def with_exemption(price: float, area: RateArea) -> TaxScenario:
    """Owner-occupied: base year value resets to price, less the $7,000 exemption."""
    rate = area.total_rate()
    annual = annual_tax(price, rate, HOMEOWNERS_EXEMPTION)
    return TaxScenario(
        label="Owner-occupied (homeowners' exemption filed)",
        market_value=price,
        exemption=HOMEOWNERS_EXEMPTION,
        assessed_value=assessed_value(price, HOMEOWNERS_EXEMPTION),
        applicable_rate=rate,
        annual_tax=annual,
        monthly_tax=annual / 12.0,
    )


def without_exemption(price: float, area: RateArea) -> TaxScenario:
    """Rental or second home: same rate, no exemption. There is no ratio penalty."""
    rate = area.total_rate()
    annual = annual_tax(price, rate, 0.0)
    return TaxScenario(
        label="No exemption (rental or second home)",
        market_value=price,
        exemption=0.0,
        assessed_value=assessed_value(price, 0.0),
        applicable_rate=rate,
        annual_tax=annual,
        monthly_tax=annual / 12.0,
    )


def both_scenarios(
    price: float, tra: str | int | None = None
) -> tuple[TaxScenario, TaxScenario, RateArea]:
    """Exemption and no-exemption scenarios plus the resolved rate area."""
    area = get_schedule(tra)
    return with_exemption(price, area), without_exemption(price, area), area


def supplemental_tax(
    new_assessed: float,
    prior_assessed: float,
    rate: float,
    closing_month: int,
) -> float:
    """One-off supplemental bill for the year of purchase.

    (new AV - old AV) x rate x proration factor. Returns 0.0 when the reset lowers
    the assessment, which produces a refund rather than a bill and is out of scope.
    """
    if closing_month not in SUPPLEMENTAL_PRORATION:
        raise ValueError(f"closing_month must be 1-12, got {closing_month!r}")
    if rate < 0:
        raise ValueError("rate cannot be negative")
    delta = new_assessed - prior_assessed
    if delta <= 0:
        return 0.0
    return delta * rate * SUPPLEMENTAL_PRORATION[closing_month]


def supplemental_block(
    price: float,
    prior_assessed: float | None,
    tra: str | int | None = None,
    closing_month: int = 7,
) -> dict[str, object]:
    """Supplemental assessment estimate, with the two-bill warning where it applies."""
    area = get_schedule(tra)
    rate = area.total_rate()

    if prior_assessed is None:
        return {
            "available": False,
            "note": (
                "No current assessed value known for this parcel, so the supplemental "
                "bill cannot be estimated. It is the difference between your purchase "
                "price and the seller's assessed value, prorated for the remainder of "
                "the fiscal year."
            ),
            "source_url": RTC_75_41,
        }

    amount = supplemental_tax(
        assessed_value(price, HOMEOWNERS_EXEMPTION), prior_assessed, rate, closing_month
    )
    two_bills = closing_month in (1, 2, 3, 4, 5)

    return {
        "available": True,
        "closing_month": closing_month,
        "proration_factor": SUPPLEMENTAL_PRORATION[closing_month],
        "prior_assessed_value": round(prior_assessed, 2),
        "estimated_amount": round(amount, 2),
        "two_bills_expected": two_bills,
        "note": (
            "A supplemental bill arrives separately from the annual bill, is not "
            "collected through an escrow impound account by default, and is easy to "
            "mistake for a duplicate. Budget cash for it."
            + (
                " Closing between January and May triggers TWO supplemental bills, one "
                "for the current fiscal year and one for the next."
                if two_bills
                else ""
            )
        ),
        "source_url": RTC_75_41,
    }


def listing_basis_comparison(
    price: float,
    current_assessed: float | None,
    tra: str | int | None = None,
) -> dict[str, object]:
    """Compare the seller's Prop 13 basis to the buyer's reset basis.

    This is the California-specific insight and the inverse of the SC ratio problem.
    Because a base year value is frozen at the seller's purchase and grows at most 2%
    a year, the tax figure shown on a listing reflects the seller's basis, not yours.
    Quantifying the gap turns a vague warning into a number a buyer can budget for.
    """
    area = get_schedule(tra)
    rate = area.total_rate()

    if current_assessed is None or current_assessed <= 0:
        return {
            "available": False,
            "note": (
                "The seller's current assessed value is unknown, so the gap between "
                "the listing's tax figure and yours cannot be quantified. Assume the "
                "listed figure understates your bill."
            ),
            "source_url": BOE_PUB_29,
        }

    seller_annual = current_assessed * rate
    buyer_annual = annual_tax(price, rate, HOMEOWNERS_EXEMPTION)
    delta = buyer_annual - seller_annual
    multiple = (buyer_annual / seller_annual) if seller_annual > 0 else None

    return {
        "available": True,
        "seller_assessed_value": round(current_assessed, 2),
        "seller_annual_tax": round(seller_annual, 2),
        "buyer_assessed_value": round(assessed_value(price, HOMEOWNERS_EXEMPTION), 2),
        "buyer_annual_tax": round(buyer_annual, 2),
        "delta_annual": round(delta, 2),
        "delta_monthly": round(delta / 12.0, 2),
        "multiple": round(multiple, 2) if multiple is not None else None,
        "direction": "up" if delta > 0 else ("flat" if delta == 0 else "down"),
        "note": (
            "The seller's assessed value is locked to their purchase year and grows at "
            "most 2% annually under Prop 13. Your assessment resets to the purchase "
            "price at closing. Any tax figure on the listing describes the seller's "
            "basis, not yours."
        ),
        "source_url": BOE_PUB_29,
    }


def tax_block(
    price: float,
    tra: str | int | None = None,
    current_assessed: float | None = None,
    closing_month: int = 7,
) -> dict[str, object]:
    """Provenance-wrapped tax section for the output document."""
    primary, non_primary, area = both_scenarios(price, tra)
    resolved = is_measured(area)

    def wrap(scenario: TaxScenario) -> dict[str, object]:
        body = scenario.to_dict()
        monthly = round(scenario.monthly_tax, 2)
        # The rate is measured when the TRA resolved, but the tax figure it produces
        # is still an estimate: direct assessments are excluded and the rate is
        # fiscal-year-scoped. Measured input does not make a derived total measured.
        body["monthly_tax_value"] = estimated(
            monthly, source_url=area.source_url, note=area.note
        ).to_dict()
        return body

    rate_value = (
        measured(area.total_rate_pct(), source_url=area.source_url, note=area.note)
        if resolved
        else estimated(area.total_rate_pct(), source_url=area.source_url, note=area.note)
    )

    return {
        "rate_area": area.tra,
        "rate_area_city": area.city,
        "fiscal_year": area.fiscal_year,
        "rate_resolved": resolved,
        "rate": {
            "total_rate_pct": area.total_rate_pct(),
            "general_levy_pct": area.general_levy_pct(),
            "voter_debt_pct": area.voter_debt_pct(),
            "school_debt_pct": area.school_debt_pct(),
            "breakdown": area.breakdown(),
            "source_url": area.source_url,
            "total_rate_value": rate_value.to_dict(),
        },
        "scenario_owner_occupied": wrap(primary),
        "scenario_no_exemption": wrap(non_primary),
        "delta_annual": round(non_primary.annual_tax - primary.annual_tax, 2),
        "homeowners_exemption": {
            "amount": HOMEOWNERS_EXEMPTION,
            "annual_value": round(HOMEOWNERS_EXEMPTION * area.total_rate(), 2),
            "note": (
                "The $7,000 homeowners' exemption has not been adjusted since 1972, so "
                "it is worth about $70 a year. File by February 15 for the full amount; "
                "filing between February 16 and December 10 yields 80% ($5,600). LA "
                "County form ASSR-515."
            ),
            "source_url": BOE_PUB_800_6,
        },
        "listing_basis": listing_basis_comparison(price, current_assessed, tra),
        "supplemental": supplemental_block(price, current_assessed, tra, closing_month),
        "rules_source": PROP_13_SOURCE,
        "caveat": (
            "Assessed value resets to the purchase price at closing and is then capped "
            "at 2% growth a year. The seller's current bill is almost always LOWER than "
            "yours will be, not higher. Expect a separate supplemental bill in the year "
            "you buy, and note that direct assessments (flat per-parcel charges) are "
            "excluded from the rate above."
        ),
    }


def basis_reset_note(current_assessed: float | None, price: float) -> Value | None:
    """Flag the direction a listing's tax line will move for a buyer.

    The SC version of this function checked an assessment *ratio*. In California the
    equivalent signal is how far the seller's basis sits below the purchase price.
    """
    if current_assessed is None or current_assessed <= 0:
        return None
    if price <= 0:
        return None

    ratio = current_assessed / price
    if ratio < 0.75:
        return estimated(
            "up_sharply",
            source_url=BOE_PUB_29,
            note=(
                f"The seller's assessed value is about {ratio:.0%} of the asking price, "
                "so their Prop 13 basis is well below market. The listed tax figure "
                "materially understates what you would pay."
            ),
        )
    if ratio < 0.98:
        return estimated(
            "up",
            source_url=BOE_PUB_29,
            note=(
                f"The seller's assessed value is about {ratio:.0%} of the asking price. "
                "Your bill will be somewhat higher than the listed figure."
            ),
        )
    return estimated(
        "flat",
        source_url=BOE_PUB_29,
        note=(
            "The seller's assessed value is close to the asking price, so the property "
            "likely changed hands recently and the listed tax figure is roughly "
            "predictive."
        ),
    )
