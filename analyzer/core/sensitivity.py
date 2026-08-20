"""Rate sensitivity for one house, not a mortgage-rate forecast.

The useful question is not whether a rate will rise or fall. Nobody here predicts that. It is
what the *same house* costs at a stated set of rates, and how much of a later price change would
cancel a stated rate move. This module therefore takes rates and prices as assumptions, labels
its output as arithmetic rather than a forecast, and calls the existing cost engine rather than
repeating mortgage or PITI formulas.

`rate_band` deliberately builds a changed copy of the profile at each rate, then calls
`cost.compute`, `cost.monthly_payment`, and `cost.solve_max_price`. Keeping those three paths in
the calculation matters: the chart, PITI, and DTI ceiling cannot silently acquire their own tax,
insurance, maintenance, or annuity arithmetic.

This is pure and stdlib-only. Reading a FRED/PMMS snapshot is I/O, so the thin CLI adapter reads
it and passes the measured rate and source label in. The page likewise passes its slider rate.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import Decimal, ROUND_HALF_UP
from math import isfinite

from .cost import compute, monthly_payment, solve_max_price
from .profile import BuyerProfile

DEFAULT_START_RATE = 0.0500
DEFAULT_END_RATE = 0.0750
DEFAULT_STEP = 0.0025
_RATE_SCALE = Decimal("10000")
_CENT = Decimal("0.01")


@dataclass(frozen=True)
class BaselineRate:
    """An explicitly labeled anchor rate, never an implied market measurement."""

    annual_rate: float
    source: str
    source_url: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "annual_rate": self.annual_rate,
            "source": self.source,
            "source_url": self.source_url,
        }


@dataclass(frozen=True)
class RatePoint:
    """One rate applied to the same house and household assumptions."""

    annual_rate: float
    principal_interest: float
    piti: float
    total_interest: float
    lender_max_price: float | None
    household_max_price: float | None
    piti_delta_from_baseline: float
    principal_interest_delta_from_baseline: float
    total_interest_delta_from_baseline: float

    def to_dict(self) -> dict[str, object]:
        return {
            "annual_rate": self.annual_rate,
            "principal_interest": self.principal_interest,
            "piti": self.piti,
            "total_interest": self.total_interest,
            "lender_max_price": self.lender_max_price,
            "household_max_price": self.household_max_price,
            "piti_delta_from_baseline": self.piti_delta_from_baseline,
            "principal_interest_delta_from_baseline": self.principal_interest_delta_from_baseline,
            "total_interest_delta_from_baseline": self.total_interest_delta_from_baseline,
        }


@dataclass(frozen=True)
class RateBand:
    """A rate sweep and its stated anchor. It is scenario arithmetic, not a forecast."""

    price: float
    baseline: BaselineRate
    points: tuple[RatePoint, ...]
    note: str

    def to_dict(self) -> dict[str, object]:
        return {
            "price": self.price,
            "baseline": self.baseline.to_dict(),
            "points": [point.to_dict() for point in self.points],
            "note": self.note,
        }


@dataclass(frozen=True)
class WaitVsBuy:
    """Two supplied scenarios and the future price that equalizes their PITI."""

    baseline_rate: float
    baseline_price: float
    future_rate: float
    future_price: float
    baseline_piti: float
    future_piti: float
    monthly_payment_delta: float
    baseline_total_interest: float
    future_total_interest: float
    lifetime_interest_delta: float
    break_even_future_price: float
    break_even_price_change: float
    note: str

    def to_dict(self) -> dict[str, object]:
        return {
            "baseline_rate": self.baseline_rate,
            "baseline_price": self.baseline_price,
            "future_rate": self.future_rate,
            "future_price": self.future_price,
            "baseline_piti": self.baseline_piti,
            "future_piti": self.future_piti,
            "monthly_payment_delta": self.monthly_payment_delta,
            "baseline_total_interest": self.baseline_total_interest,
            "future_total_interest": self.future_total_interest,
            "lifetime_interest_delta": self.lifetime_interest_delta,
            "break_even_future_price": self.break_even_future_price,
            "break_even_price_change": self.break_even_price_change,
            "note": self.note,
        }


def profile_baseline(profile: BuyerProfile) -> BaselineRate:
    """Return the configured rate without claiming it is a current market observation."""
    _validate_rate(profile.mortgage_rate, "profile mortgage_rate")
    return BaselineRate(
        annual_rate=profile.mortgage_rate,
        source="buyer profile mortgage_rate (configured assumption; no market snapshot supplied)",
    )


def rate_band(
    profile: BuyerProfile,
    price: float,
    *,
    baseline: BaselineRate | None = None,
    start_rate: float = DEFAULT_START_RATE,
    end_rate: float = DEFAULT_END_RATE,
    step: float = DEFAULT_STEP,
    dti_ceiling: float | None = None,
    sqft: float | None = None,
    year_built: int | None = None,
    hoa_monthly: float = 0.0,
    current_year: int,
    owner_occupied: bool = True,
) -> RateBand:
    """Apply a discrete annual-rate range to one unchanged house.

    The dollar figures are rounded to cents at the output boundary, matching how a person can
    pay them. The DTI solver intentionally returns its conservative dollar-level boundary from
    `cost.solve_max_price`; an infeasible ceiling is represented by its two `None` prices, not
    by a fabricated zero.
    """
    _validate_common_inputs(profile, price, start_rate, end_rate, step, hoa_monthly, current_year)
    anchor = baseline or profile_baseline(profile)
    _validate_rate(anchor.annual_rate, "baseline annual_rate")
    if dti_ceiling is not None and (not isfinite(dti_ceiling) or dti_ceiling <= 0 or dti_ceiling > 1):
        raise ValueError("dti_ceiling must be a finite fraction greater than 0 and at most 1")

    rates = _rate_range(start_rate, end_rate, step)
    baseline_quote = _quote(
        profile,
        price,
        anchor.annual_rate,
        sqft=sqft,
        year_built=year_built,
        hoa_monthly=hoa_monthly,
        current_year=current_year,
        owner_occupied=owner_occupied,
    )

    points: list[RatePoint] = []
    for rate in rates:
        rated_profile = replace(profile, mortgage_rate=rate)
        # `compute` owns PITI, `monthly_payment` owns the annuity payment, and the inverse
        # answer comes from `solve_max_price`. Do not turn this into three copied formulas.
        cost = compute(
            rated_profile, price, sqft, year_built, hoa_monthly, current_year, owner_occupied
        )
        payment = monthly_payment(
            max(0.0, price - rated_profile.down_payment), rate, rated_profile.loan_term_months
        )
        solution = solve_max_price(
            rated_profile,
            dti_ceiling=dti_ceiling,
            sqft=sqft,
            year_built=year_built,
            hoa_monthly=hoa_monthly,
            current_year=current_year,
            owner_occupied=owner_occupied,
        )
        principal_interest = _money(payment)
        piti = _money(cost.piti)
        total_interest = _term_interest(price, rated_profile, payment)
        points.append(
            RatePoint(
                annual_rate=rate,
                principal_interest=principal_interest,
                piti=piti,
                total_interest=total_interest,
                lender_max_price=(
                    None if solution.lender_max_price is None else _whole_dollars(solution.lender_max_price)
                ),
                household_max_price=(
                    None
                    if solution.household_max_price is None
                    else _whole_dollars(solution.household_max_price)
                ),
                piti_delta_from_baseline=_money(piti - baseline_quote.piti),
                principal_interest_delta_from_baseline=_money(
                    principal_interest - baseline_quote.principal_interest
                ),
                total_interest_delta_from_baseline=_money(
                    total_interest - baseline_quote.total_interest
                ),
            )
        )

    return RateBand(
        price=_money(price),
        baseline=anchor,
        points=tuple(points),
        note=(
            "Scenario arithmetic only, not a mortgage-rate or home-price forecast. Each point "
            "holds the house price, down payment, term, tax/insurance assumptions, HOA, and "
            "household inputs fixed; mortgage insurance remains unmodeled."
        ),
    )


def wait_vs_buy(
    profile: BuyerProfile,
    *,
    baseline_rate: float,
    baseline_price: float,
    future_rate: float,
    future_price: float,
    sqft: float | None = None,
    year_built: int | None = None,
    hoa_monthly: float = 0.0,
    current_year: int,
    owner_occupied: bool = True,
) -> WaitVsBuy:
    """Compare two user-supplied scenarios; this is arithmetic on assumptions, not a forecast.

    `break_even_future_price` is the future purchase price that makes the future scenario's full
    PITI exactly equal to the baseline scenario's full PITI under the same tax, insurance, HOA,
    down-payment and term assumptions. A lower rate can therefore still be worse when its price
    exceeds that number. It is not a prediction of either rate or price.
    """
    _validate_common_inputs(
        profile, baseline_price, baseline_rate, future_rate, DEFAULT_STEP, hoa_monthly, current_year
    )
    if not isfinite(future_price) or future_price <= 0:
        raise ValueError("future_price must be a finite positive dollar amount")

    today = _quote(
        profile,
        baseline_price,
        baseline_rate,
        sqft=sqft,
        year_built=year_built,
        hoa_monthly=hoa_monthly,
        current_year=current_year,
        owner_occupied=owner_occupied,
    )
    future = _quote(
        profile,
        future_price,
        future_rate,
        sqft=sqft,
        year_built=year_built,
        hoa_monthly=hoa_monthly,
        current_year=current_year,
        owner_occupied=owner_occupied,
    )
    break_even = _price_at_piti(
        profile,
        target_piti_cents=_cents(today.piti),
        rate=future_rate,
        starting_price=baseline_price,
        sqft=sqft,
        year_built=year_built,
        hoa_monthly=hoa_monthly,
        current_year=current_year,
        owner_occupied=owner_occupied,
    )
    return WaitVsBuy(
        baseline_rate=baseline_rate,
        baseline_price=_money(baseline_price),
        future_rate=future_rate,
        future_price=_money(future_price),
        baseline_piti=today.piti,
        future_piti=future.piti,
        monthly_payment_delta=_money(future.piti - today.piti),
        baseline_total_interest=today.total_interest,
        future_total_interest=future.total_interest,
        lifetime_interest_delta=_money(future.total_interest - today.total_interest),
        break_even_future_price=_money(break_even),
        break_even_price_change=_money(break_even - baseline_price),
        note=(
            "Arithmetic on the supplied rate and price assumptions, not a rate or home-price "
            "forecast. Monthly payment means full PITI here; mortgage insurance is not modeled."
        ),
    )


@dataclass(frozen=True)
class _Quote:
    principal_interest: float
    piti: float
    total_interest: float


def _quote(
    profile: BuyerProfile,
    price: float,
    rate: float,
    *,
    sqft: float | None,
    year_built: int | None,
    hoa_monthly: float,
    current_year: int,
    owner_occupied: bool,
) -> _Quote:
    rated_profile = replace(profile, mortgage_rate=rate)
    cost = compute(rated_profile, price, sqft, year_built, hoa_monthly, current_year, owner_occupied)
    payment = monthly_payment(
        max(0.0, price - rated_profile.down_payment), rate, rated_profile.loan_term_months
    )
    return _Quote(
        principal_interest=_money(payment),
        piti=_money(cost.piti),
        total_interest=_term_interest(price, rated_profile, payment),
    )


def _price_at_piti(
    profile: BuyerProfile,
    *,
    target_piti_cents: int,
    rate: float,
    starting_price: float,
    sqft: float | None,
    year_built: int | None,
    hoa_monthly: float,
    current_year: int,
    owner_occupied: bool,
) -> float:
    """Bisect over `compute`, in cents, until a future PITI matches an existing one."""
    rated_profile = replace(profile, mortgage_rate=rate)

    def piti_cents(price_cents: int) -> int:
        cost = compute(
            rated_profile,
            price_cents / 100.0,
            sqft,
            year_built,
            hoa_monthly,
            current_year,
            owner_occupied,
        )
        return _cents(cost.piti)

    lo = 1  # one cent: no invented $0 home and no negative price as a solver artifact
    if piti_cents(lo) > target_piti_cents:
        raise ValueError("fixed tax, insurance, and HOA exceed the baseline PITI at every price")
    hi = max(_cents(starting_price), _cents(profile.down_payment), 100)
    for _ in range(60):
        if piti_cents(hi) >= target_piti_cents:
            break
        hi *= 2
    else:
        raise ValueError("could not bracket a break-even price within 60 expansions")

    # Return the affordable (low) side: its rounded PITI is at or below the target. At cent
    # precision, this gives callers a price they can compare without pretending mill-level
    # float noise is meaningful money.
    while hi - lo > 1:
        mid = (lo + hi) // 2
        if piti_cents(mid) <= target_piti_cents:
            lo = mid
        else:
            hi = mid
    return lo / 100.0


def _term_interest(price: float, profile: BuyerProfile, payment: float) -> float:
    """Interest from the engine's level payment; no second amortization formula lives here."""
    loan_cents = _cents(max(0.0, price - profile.down_payment))
    payment_cents = _cents(payment)
    return (payment_cents * profile.loan_term_months - loan_cents) / 100.0


def _rate_range(start_rate: float, end_rate: float, step: float) -> tuple[float, ...]:
    """Inclusive basis-point grid, so 5.00..7.50 by 0.25 cannot lose 7.50 to float drift."""
    start = _rate_to_bps(start_rate, "start_rate")
    end = _rate_to_bps(end_rate, "end_rate")
    increment = _rate_to_bps(step, "step")
    if start > end:
        raise ValueError("start_rate must be at or below end_rate")
    if increment <= 0:
        raise ValueError("step must be positive")
    if (end - start) % increment:
        raise ValueError("rate range must divide exactly into whole basis-point steps")
    return tuple(bps / 10_000.0 for bps in range(start, end + 1, increment))


def _validate_common_inputs(
    profile: BuyerProfile,
    price: float,
    start_rate: float,
    end_rate: float,
    step: float,
    hoa_monthly: float,
    current_year: int,
) -> None:
    if not isfinite(price) or price <= 0:
        raise ValueError("price must be a finite positive dollar amount")
    if not isfinite(hoa_monthly) or hoa_monthly < 0:
        raise ValueError("hoa_monthly must be a finite non-negative dollar amount")
    if not isinstance(current_year, int) or current_year < 1900 or current_year > 3000:
        raise ValueError("current_year must be a plausible integer year")
    _validate_rate(start_rate, "start_rate")
    _validate_rate(end_rate, "end_rate")
    _validate_rate(step, "step")
    if profile.loan_term_months <= 0:
        raise ValueError("profile loan_term_months must be positive")


def _validate_rate(rate: float, name: str) -> None:
    if not isfinite(rate) or rate < 0 or rate > 1:
        raise ValueError(f"{name} must be a finite decimal fraction from 0 through 1, not a percentage")


def _rate_to_bps(rate: float, name: str) -> int:
    _validate_rate(rate, name)
    bps = Decimal(str(rate)) * _RATE_SCALE
    if bps != bps.to_integral_value():
        raise ValueError(f"{name} must resolve to a whole basis point")
    return int(bps)


def _cents(value: float) -> int:
    return int((Decimal(str(value)).quantize(_CENT, rounding=ROUND_HALF_UP)) * 100)


def _money(value: float) -> float:
    return _cents(value) / 100.0


def _whole_dollars(value: float) -> float:
    return float(Decimal(str(value)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
