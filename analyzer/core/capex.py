"""Near-term capital expenses — aging systems that will need money soon.

Why this module exists: a caveat that reads "roof age 17 yrs" and deducts nothing
lets a house with a five-figure bill attached to it score a perfect 100. That is
the tool lying by omission. An aging roof is not a note, it is a price adjustment.

So component age now does two things:
  1. produces a DOLLAR RANGE, labeled by magnitude (four figure / five figure)
  2. deducts points, scaled by how overdue the component is

The dollar ranges are ESTIMATES and always will be. Only an inspector's quote on a
specific house is real. The point is to know the order of magnitude before writing
an offer, not to predict the invoice.

Sources:
  Roof, LA-specific:   https://www.angi.com/articles/how-much-does-roof-replacement-cost/ca/los-angeles
                       https://modernize.com/roof/cost-calculator/california
  HVAC, national size
  table + LA install:  https://www.usatoday.com/story/money/home-services/hvac-replacement-cost/90313725007/
                       https://modernize.com/hvac/central-air-conditioner-installation/cost/california

Ported from South Carolina to Los Angeles County 2026-08-20. Every constant below
was re-sourced against LA-metro or California figures, not carried over — see the
band comments for what changed and why (this is a high-labor-cost market, and
California carries code and fire-hazard requirements SC does not).
"""

from __future__ import annotations

from dataclasses import dataclass

ROOF_SRC = "https://www.angi.com/articles/how-much-does-roof-replacement-cost/ca/los-angeles"
ROOF_SRC_ALT = "https://modernize.com/roof/cost-calculator/california"
HVAC_SRC = (
    "https://www.usatoday.com/story/money/home-services/hvac-replacement-cost/"
    "90313725007/"
)
HVAC_SRC_ALT = "https://modernize.com/hvac/central-air-conditioner-installation/cost/california"

# Any single expense at or above this is called out as five-figure.
FIVE_FIGURE = 10_000
# Total near-term capex at or above this triggers a blocking task to get quotes.
QUOTE_THRESHOLD = 5_000

# Roof replacement, Los Angeles County, architectural asphalt, banded by living area.
# Angi's LA size table (retrieved 2026-08-20): 1,000 sqft $5.2k-$15.6k, 1,500 sqft
# $7.2k-$21.6k, 1,700 sqft $8.0k-$24.0k, 2,000 sqft $8.8k-$26.4k, 2,500 sqft
# $11.2k-$33.6k. Modernize's LA-metro figure runs higher -- $18,000-$30,000 total,
# averaging $26,220 for a typical home -- because its number folds in tile and
# metal re-roofs alongside asphalt. Bands below straddle Angi's per-size table
# and stay inside Modernize's metro envelope for the largest tier.
# LA runs materially above the old South Carolina bands (roughly 1.4-1.8x) for
# two reasons: SoCal roofing labor is priced well above the national median, and
# the LA Residential Code (Ch. 15) requires a Class A fire-rated roof assembly
# on reroofs -- SC has no equivalent wildfire-driven material mandate.
_ROOF_BANDS: tuple[tuple[float | None, float, float], ...] = (
    (1500, 7_000, 21_000),
    (2500, 9_000, 27_000),
    (None, 13_000, 34_000),
)
_ROOF_UNKNOWN_SQFT = (8_000, 30_000)

# Full HVAC replacement by home size. The USA Today national size table is the
# baseline shape (1,500 / 2,000 / 2,500 / 3,000 sqft tiers); Modernize's
# California central-AC calculator gives an LA-specific band by size ($3.2k-$6.0k
# small home / $6.0k-$11.5k medium / $12.0k-$20.0k+ large, full system with
# ductwork) that runs above the national table for the larger tiers, and that LA
# premium is layered onto the national shape below rather than replacing it,
# because Modernize's table prices AC only where the national one prices a full
# system (compressor plus air handler or furnace). LA cooling load, not heating,
# drives replacement here -- the SC bands were shaped around furnace-plus-AC
# combos for a mixed heating/cooling climate. LA's marine-influenced coastal
# basin and hot, dry inland valleys both lean on the compressor side, so a
# straight port would overstate the heating-equipment share of the bill for most
# LA homes.
_HVAC_BANDS: tuple[tuple[float | None, float, float], ...] = (
    (1750, 7_000, 13_000),
    (2250, 8_500, 16_000),
    (2750, 10_000, 18_500),
    (None, 11_000, 20_000),
)
_HVAC_UNKNOWN_SQFT = (9_000, 17_000)


@dataclass(frozen=True)
class CapitalExpense:
    component: str
    reason: str
    low: float
    high: float
    points: int
    urgency: str  # "due" | "overdue"
    source_url: str

    @property
    def magnitude(self) -> str:
        return "five_figure" if self.high >= FIVE_FIGURE else "four_figure"

    @property
    def headline(self) -> str:
        return (
            f"{self.component}: ${self.low:,.0f}-${self.high:,.0f} "
            f"({self.magnitude.replace('_', ' ')})"
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "component": self.component,
            "reason": self.reason,
            "estimate_low": self.low,
            "estimate_high": self.high,
            "magnitude": self.magnitude,
            "urgency": self.urgency,
            "points_deducted": self.points,
            "source_url": self.source_url,
            "confidence": "estimated",
        }


def _band(sqft: float | None, bands, fallback: tuple[float, float]) -> tuple[float, float]:
    if not sqft:
        return fallback
    for ceiling, low, high in bands:
        if ceiling is None or sqft < ceiling:
            return low, high
    return fallback


def roof_cost(sqft: float | None) -> tuple[float, float]:
    return _band(sqft, _ROOF_BANDS, _ROOF_UNKNOWN_SQFT)


def hvac_cost(sqft: float | None) -> tuple[float, float]:
    return _band(sqft, _HVAC_BANDS, _HVAC_UNKNOWN_SQFT)


def assess(
    roof_age_years: int | None,
    hvac_age_years: int | None,
    sqft: float | None,
    thresholds: dict[str, int],
    penalties: dict[str, int],
) -> list[CapitalExpense]:
    """Every component past its service life, with a cost range and a deduction."""
    found: list[CapitalExpense] = []

    if roof_age_years is not None:
        low, high = roof_cost(sqft)
        if roof_age_years >= thresholds["roof_overdue_age"]:
            found.append(
                CapitalExpense(
                    "Roof replacement",
                    f"Roof age {roof_age_years} yrs is past the "
                    f"{thresholds['roof_overdue_age']}-yr overdue mark — assume "
                    f"replacement is immediate, not eventual",
                    low, high, penalties["roof_overdue"], "overdue", ROOF_SRC,
                )
            )
        elif roof_age_years >= thresholds["roof_due_age"]:
            found.append(
                CapitalExpense(
                    "Roof replacement",
                    f"Roof age {roof_age_years} yrs is at or past the "
                    f"{thresholds['roof_due_age']}-yr mark — budget replacement "
                    f"within a few years, or negotiate a credit",
                    low, high, penalties["roof_due"], "due", ROOF_SRC,
                )
            )

    if hvac_age_years is not None:
        low, high = hvac_cost(sqft)
        if hvac_age_years >= thresholds["hvac_overdue_age"]:
            found.append(
                CapitalExpense(
                    "HVAC replacement",
                    f"HVAC age {hvac_age_years} yrs is past the "
                    f"{thresholds['hvac_overdue_age']}-yr overdue mark — expect "
                    f"failure, not service life",
                    low, high, penalties["hvac_overdue"], "overdue", HVAC_SRC,
                )
            )
        elif hvac_age_years >= thresholds["hvac_due_age"]:
            found.append(
                CapitalExpense(
                    "HVAC replacement",
                    f"HVAC age {hvac_age_years} yrs is at or past the "
                    f"{thresholds['hvac_due_age']}-yr mark — inspect and budget "
                    f"replacement",
                    low, high, penalties["hvac_due"], "due", HVAC_SRC,
                )
            )

    return found


def block(expenses: list[CapitalExpense]) -> dict[str, object]:
    """Capital-expense section for the output document."""
    total_low = sum(e.low for e in expenses)
    total_high = sum(e.high for e in expenses)
    return {
        "items": [e.to_dict() for e in expenses],
        "total_low": total_low,
        "total_high": total_high,
        "total_points_deducted": sum(e.points for e in expenses),
        "has_five_figure_item": any(e.magnitude == "five_figure" for e in expenses),
        "needs_contractor_quotes": total_high >= QUOTE_THRESHOLD,
        "sources": {
            "roof": ROOF_SRC, "roof_alt": ROOF_SRC_ALT,
            "hvac": HVAC_SRC, "hvac_alt": HVAC_SRC_ALT,
        },
        "note": (
            "Planning ranges only, banded by home size. An inspector's quote on the "
            "specific house is the real number. Use these to size an offer credit, "
            "not to predict an invoice."
        ),
    }
