"""Amortization schedule: where the money actually goes, month by month.

This answers a question the rest of the tool cannot. `cost.compute` says the payment is
$1,652 a month. It does not say that $1,371 of the first one is interest, that the crossover
where principal finally exceeds interest is 16 years away, or that $200 extra a month ends
the loan 6 years early and saves $63,000. Those are the numbers that change behaviour.

Three things about this module are deliberate and easy to get wrong.

**Money is integer cents, not floats.** A schedule is 360 sequential operations on the same
running balance. Floats accumulate error over that many steps, and the tell is a final
balance of -0.004 or a total interest figure that misses by a dollar. Integer cents cannot
drift, and a lender's statement is in cents anyway, so this is also the only way the output
can match a real amortization notice.

**The last payment is adjusted, not scheduled.** The level payment is a rounded number, so
after 359 of them the balance is a few cents away from zero rather than on it. Real lenders
resolve this by making the final payment whatever clears the loan. A schedule that ends with
a nonzero balance is wrong in a way people notice immediately.

**This is principal and interest only.** Taxes, insurance, HOA, and mortgage insurance do
not amortize -- they are costs that ride alongside the loan, and paying extra principal does
not shorten them. Every figure here is smaller than the PITI figure elsewhere in the tool,
and mixing them up would understate the real monthly cost by hundreds of dollars. The output
carries `excludes` for exactly that reason.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from .cost import monthly_payment

# A schedule cannot outrun this many months. Not a business rule -- a stop on the loop, so a
# pathological input fails in milliseconds with a readable message instead of hanging.
MAX_MONTHS = 1200


def _cents(value: float | Decimal) -> int:
    """Round to the nearest cent, half up.

    Not Python's `round()`, which is banker's rounding: `round(0.125, 2)` gives 0.12, and a
    lender rounds it to 0.13. Over 360 months that difference is visible in the total.
    """
    return int(Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP) * 100)


@dataclass(frozen=True)
class Payment:
    """One month. `payment` is the total leaving the account, including any extra."""

    number: int
    payment: float
    interest: float
    principal: float
    extra: float
    balance: float

    def to_dict(self) -> dict[str, object]:
        return {
            "number": self.number,
            "payment": round(self.payment, 2),
            "interest": round(self.interest, 2),
            "principal": round(self.principal, 2),
            "extra": round(self.extra, 2),
            "balance": round(self.balance, 2),
        }


@dataclass(frozen=True)
class Year:
    """A twelve-month rollup.

    The point of this table is that nobody reads 360 rows. A year-by-year view is the one a
    person actually uses to see how slowly the balance moves early on.
    """

    year: int
    payments: int
    interest: float
    principal: float
    extra: float
    ending_balance: float

    def to_dict(self) -> dict[str, object]:
        return {
            "year": self.year,
            "payments": self.payments,
            "interest": round(self.interest, 2),
            "principal": round(self.principal, 2),
            "extra": round(self.extra, 2),
            "ending_balance": round(self.ending_balance, 2),
        }


@dataclass(frozen=True)
class Schedule:
    loan_amount: float
    annual_rate: float
    term_months: int
    scheduled_payment: float
    extra_monthly: float
    payments: tuple[Payment, ...]
    years: tuple[Year, ...]
    total_interest: float
    total_principal: float
    total_paid: float
    months_to_payoff: int
    final_payment: float
    # First month where the principal portion is at least the interest portion. On a 30-year
    # loan at current rates this is well past year 15, which is the single most surprising
    # number in the whole table.
    crossover_month: int | None
    # Only populated when extra_monthly > 0: what the extra bought, measured against the same
    # loan with no extra payments.
    months_saved: int
    interest_saved: float

    def to_dict(self, *, include_payments: bool = True) -> dict[str, object]:
        out: dict[str, object] = {
            "loan_amount": round(self.loan_amount, 2),
            "annual_rate": self.annual_rate,
            "term_months": self.term_months,
            "scheduled_payment": round(self.scheduled_payment, 2),
            "extra_monthly": round(self.extra_monthly, 2),
            "total_interest": round(self.total_interest, 2),
            "total_principal": round(self.total_principal, 2),
            "total_paid": round(self.total_paid, 2),
            "months_to_payoff": self.months_to_payoff,
            "final_payment": round(self.final_payment, 2),
            "crossover_month": self.crossover_month,
            "months_saved": self.months_saved,
            "interest_saved": round(self.interest_saved, 2),
            "years": [y.to_dict() for y in self.years],
            # Stated in the payload, not just in a docstring. A consumer that renders this
            # next to a PITI figure needs to know these are different quantities.
            "excludes": [
                "property tax",
                "homeowners insurance",
                "HOA dues",
                "mortgage insurance",
            ],
        }
        if include_payments:
            out["payments"] = [p.to_dict() for p in self.payments]
        return out


def _run(
    loan_amount_cents: int,
    monthly_rate: Decimal,
    payment_cents: int,
    extra_cents: int,
    term_months: int,
):
    """The loop. Returns rows of (interest_cents, principal_cents, extra_cents, balance_cents).

    Separated from `schedule()` so the no-extra baseline can be run for comparison without
    duplicating the arithmetic or recursing.
    """
    balance = loan_amount_cents
    rows: list[tuple[int, int, int, int]] = []

    while balance > 0:
        if len(rows) >= MAX_MONTHS:
            raise ValueError(
                f"loan did not amortize within {MAX_MONTHS} months; "
                "check the rate and payment"
            )

        interest = _cents(Decimal(balance) / 100 * monthly_rate)

        # Negative amortization. If the level payment does not cover the month's interest the
        # balance grows every month and the loop never ends. Caught here rather than after
        # 1200 iterations, because the useful message is about the inputs.
        if payment_cents + extra_cents <= interest and balance > payment_cents + extra_cents:
            raise ValueError(
                "payment does not cover the monthly interest, so the balance would grow: "
                f"interest ${interest / 100:,.2f} vs payment ${(payment_cents + extra_cents) / 100:,.2f}"
            )

        principal = payment_cents - interest
        extra = extra_cents

        # A 30-year note has 360 payments, and this is the line that makes that true.
        #
        # The level payment is rounded to the cent, so 360 of them do not land exactly on
        # zero -- at 6.67% on $213,200 they leave $3.31 behind, and the loop would happily
        # bill a 361st payment for it. Real notes do not work that way: the last scheduled
        # payment is the remaining balance plus that month's interest, so it absorbs whatever
        # the rounding left over. Without this the schedule is off by one row, which is the
        # kind of error that makes a reader stop trusting every other number on the page.
        #
        # Extra payments legitimately finish the loan early, and in that case this branch is
        # never reached because the balance is already gone.
        if len(rows) + 1 >= term_months:
            principal = balance
            extra = 0
        # Otherwise the final payment is whatever clears the loan. Scheduled principal is
        # clamped first, then the extra, so a large extra cannot drive the balance negative
        # and hand back a refund that never happened.
        elif principal >= balance:
            principal = balance
            extra = 0
        elif principal + extra >= balance:
            extra = balance - principal

        balance -= principal + extra
        rows.append((interest, principal, extra, balance))

    return rows


def schedule(
    loan_amount: float,
    annual_rate: float,
    term_months: int,
    *,
    extra_monthly: float = 0.0,
) -> Schedule:
    """Build the full schedule.

    `annual_rate` is a decimal fraction (0.0667, not 6.67) to match `BuyerProfile` and
    `cost.monthly_payment`. Mixing those two conventions produces a plausible-looking
    schedule at a hundred times the real rate, so there is a guard below.
    """
    if loan_amount <= 0:
        raise ValueError("loan_amount must be positive")
    if term_months <= 0:
        raise ValueError("term_months must be positive")
    if annual_rate < 0:
        raise ValueError("annual_rate must not be negative")
    if annual_rate > 1:
        raise ValueError(
            f"annual_rate {annual_rate} looks like a percentage; pass 0.0667 for 6.67%"
        )
    if extra_monthly < 0:
        raise ValueError("extra_monthly must not be negative")

    # One formula, one place. `cost.monthly_payment` is what the rest of the tool quotes, and
    # a schedule built on a second implementation of it would eventually disagree with the
    # PITI figure on the same screen.
    scheduled = monthly_payment(loan_amount, annual_rate, term_months)
    scheduled_cents = _cents(scheduled)
    loan_cents = _cents(loan_amount)
    extra_cents = _cents(extra_monthly)
    monthly_rate = Decimal(str(annual_rate)) / 12

    rows = _run(loan_cents, monthly_rate, scheduled_cents, extra_cents, term_months)

    payments: list[Payment] = []
    crossover: int | None = None
    for i, (interest_c, principal_c, extra_c, balance_c) in enumerate(rows, start=1):
        if crossover is None and principal_c + extra_c >= interest_c:
            crossover = i
        payments.append(
            Payment(
                number=i,
                payment=(interest_c + principal_c + extra_c) / 100,
                interest=interest_c / 100,
                principal=principal_c / 100,
                extra=extra_c / 100,
                balance=balance_c / 100,
            )
        )

    years: list[Year] = []
    for start in range(0, len(payments), 12):
        chunk = payments[start : start + 12]
        years.append(
            Year(
                year=start // 12 + 1,
                payments=len(chunk),
                interest=sum(p.interest for p in chunk),
                principal=sum(p.principal for p in chunk),
                extra=sum(p.extra for p in chunk),
                ending_balance=chunk[-1].balance,
            )
        )

    total_interest_c = sum(r[0] for r in rows)
    total_principal_c = sum(r[1] + r[2] for r in rows)

    months_saved = 0
    interest_saved = 0.0
    if extra_cents:
        baseline = _run(loan_cents, monthly_rate, scheduled_cents, 0, term_months)
        months_saved = len(baseline) - len(rows)
        interest_saved = (sum(r[0] for r in baseline) - total_interest_c) / 100

    return Schedule(
        loan_amount=loan_cents / 100,
        annual_rate=annual_rate,
        term_months=term_months,
        scheduled_payment=scheduled_cents / 100,
        extra_monthly=extra_cents / 100,
        payments=tuple(payments),
        years=tuple(years),
        total_interest=total_interest_c / 100,
        total_principal=total_principal_c / 100,
        total_paid=(total_interest_c + total_principal_c) / 100,
        months_to_payoff=len(rows),
        final_payment=payments[-1].payment,
        crossover_month=crossover,
        months_saved=months_saved,
        interest_saved=interest_saved,
    )
