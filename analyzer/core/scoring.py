"""The score. Pure function, no network, no model, fully reproducible.

Four tiers, and the distinctions matter:

  hard fails        -> score 0, verdict PASS. Disqualifying at any price.
  capital expenses  -> points off, scaled by how overdue. Carry a dollar range.
  deductions        -> points off 100. A house can fail these and still be a candidate.
  caveats           -> zero points. Attention, not penalty.

Capital expenses are separated from ordinary deductions because they are different in
kind. A small garage is a permanent compromise you accept at the price. A 17-year-old
roof is a four- or five-figure bill arriving on a schedule, and it belongs in the
number rather than in a footnote.

A hard fail that could not be EVALUATED (missing input, dead source) never silently
becomes a pass. The score is pinned to `unevaluated_score` (50, landing in WATCH) and
the unresolved question is named. Pinning rather than raising: the pin can only lower a
score, never inflate one, so an unknown never helps a property.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from . import capex
from .capex import CapitalExpense
from .profile import BuyerProfile

VERDICT_TAKE = "TAKE"
VERDICT_WATCH = "WATCH"
VERDICT_PASS = "PASS"


@dataclass
class PropertyFacts:
    """Everything scoring needs. Sources populate this; scoring never fetches.

    `None` means unknown, which is different from a failing value and is treated
    differently.
    """

    price: float
    sqft: float | None = None
    beds: int | None = None
    baths: float | None = None
    garage_spaces: int | None = None
    hoa_monthly: float | None = 0.0
    year_built: int | None = None
    roof_age_years: int | None = None
    hvac_age_years: int | None = None
    # hard-fail inputs — None means "could not evaluate"
    flood_zone: str | None = None
    water_sewer: str | None = None
    commute_min: float | None = None
    fiber_available: bool | None = None
    # school inputs — a scored preference, not a hard fail. A weak zoned school is a
    # compromise a household can accept at the right price, the way a small garage is;
    # it is not disqualifying the way a flood zone is.
    school_district: str | None = None
    school_proficiency_pct: float | None = None
    school_zone_of_choice: bool | None = None


@dataclass
class ScoreResult:
    value: int
    verdict: str
    hard_fails: list[str] = field(default_factory=list)
    unevaluated_hard_fails: list[str] = field(default_factory=list)
    deductions: list[dict[str, Any]] = field(default_factory=list)
    capital_expenses: list[CapitalExpense] = field(default_factory=list)
    caveats: list[str] = field(default_factory=list)
    score_pinned: bool = False
    unknown_facts: list[str] = field(default_factory=list)
    score_capped: bool = False

    @property
    def total_deducted(self) -> int:
        """Preference deductions only. Capital expenses are counted separately."""
        return sum(d["points"] for d in self.deductions)

    @property
    def capex_deducted(self) -> int:
        return sum(e.points for e in self.capital_expenses)

    @property
    def capex_low(self) -> float:
        return sum(e.low for e in self.capital_expenses)

    @property
    def capex_high(self) -> float:
        return sum(e.high for e in self.capital_expenses)

    def to_dict(self) -> dict[str, Any]:
        return {
            "value": self.value,
            "verdict": self.verdict,
            "score_pinned": self.score_pinned,
            "score_capped": self.score_capped,
            "unknown_facts": self.unknown_facts,
            "hard_fails": self.hard_fails,
            "unevaluated_hard_fails": self.unevaluated_hard_fails,
            "deductions": self.deductions,
            "total_deducted": self.total_deducted,
            "capital_expenses": [e.to_dict() for e in self.capital_expenses],
            "capex_deducted": self.capex_deducted,
            "capex_estimate_low": self.capex_low,
            "capex_estimate_high": self.capex_high,
            "caveats": self.caveats,
        }


def _is_flood_zone(zone: str | None, excluded: tuple[str, ...]) -> bool:
    if not zone:
        return False
    return zone.strip().upper() in {z.upper() for z in excluded}


def score(facts: PropertyFacts, profile: BuyerProfile, current_year: int) -> ScoreResult:
    result = ScoreResult(value=100, verdict=VERDICT_TAKE)

    # -- hard fails ----------------------------------------------------------

    if facts.flood_zone is None:
        result.unevaluated_hard_fails.append(
            "Flood zone unknown — cannot confirm the property is outside a Special "
            "Flood Hazard Area"
        )
    elif _is_flood_zone(facts.flood_zone, profile.exclude_flood_zones):
        result.hard_fails.append(
            f"FEMA flood zone {facts.flood_zone} is a Special Flood Hazard Area"
        )

    if profile.require_public_water_sewer:
        if facts.water_sewer is None:
            result.unevaluated_hard_fails.append(
                "Water/sewer type unknown — cannot confirm public utilities"
            )
        elif facts.water_sewer.strip().lower() not in ("public", "city", "municipal"):
            result.hard_fails.append(
                f"Water/sewer is '{facts.water_sewer}' — public water and sewer required"
            )

    if facts.commute_min is None:
        result.unevaluated_hard_fails.append(
            "Commute unknown — cannot evaluate the 20-minute limit"
        )
    elif facts.commute_min > profile.max_commute_min:
        result.hard_fails.append(
            f"Rush-hour commute {facts.commute_min:.1f} min exceeds the "
            f"{profile.max_commute_min:.0f}-min limit"
        )

    if result.hard_fails:
        result.value = 0
        result.verdict = VERDICT_PASS
        _add_caveats(result, facts, profile, current_year)
        return result

    # -- deductions ----------------------------------------------------------

    p = profile.penalties

    def deduct(key: str, reason: str) -> None:
        points = p.get(key, 0)
        if points:
            result.deductions.append({"reason": reason, "points": points, "rule": key})

    hoa = facts.hoa_monthly or 0.0
    if hoa > profile.max_hoa_monthly:
        deduct(
            "hoa_over_max",
            f"HOA ${hoa:,.0f}/mo exceeds the ${profile.max_hoa_monthly:,.0f}/mo "
            f"preference — still a candidate, but it is a permanent cost",
        )

    if facts.beds is not None and facts.beds < profile.min_beds:
        deduct("beds_under", f"{facts.beds} bedrooms, want {profile.min_beds}")
    if facts.sqft is not None and facts.sqft < profile.min_sqft:
        deduct(
            "sqft_under",
            f"{facts.sqft:,.0f} sqft below the {profile.min_sqft:,} floor",
        )
    if profile.require_fiber and facts.fiber_available is False:
        deduct("no_fiber", "No fiber reported — remote work depends on it")
    if (
        facts.garage_spaces is not None
        and facts.garage_spaces < profile.min_garage_spaces
    ):
        deduct(
            "garage_under",
            f"{facts.garage_spaces}-car garage, want {profile.min_garage_spaces}",
        )
    if facts.baths is not None and facts.baths < profile.min_baths:
        deduct("baths_under", f"{facts.baths:g} baths, want {profile.min_baths}")

    _score_schools(result, facts, profile)

    # -- near-term capital expenses ------------------------------------------
    # Separate tier: these carry a dollar range and deduct proportionally to how
    # overdue the component is. See capex.py for the cost sources.

    result.capital_expenses = capex.assess(
        roof_age_years=facts.roof_age_years,
        hvac_age_years=facts.hvac_age_years,
        sqft=facts.sqft,
        thresholds=profile.capex_thresholds,
        penalties=profile.capex_penalties,
    )

    total = result.total_deducted + result.capex_deducted
    result.value = max(0, min(100, 100 - total))

    # -- what we do not know -------------------------------------------------
    # Every deduction above is guarded by `is not None`, which is correct — you cannot
    # deduct for a small house without knowing its size. The side effect is that a
    # property with *no* physical facts collects no deductions and scores 100.
    #
    # Batch mode made that visible immediately: two properties whose size, bedroom count,
    # and bathroom count were all unknown ranked above the one house with a full county
    # record, purely because the record was missing. Silence read as perfection.
    #
    # So absence of evidence is now tracked explicitly and gates the verdict below.

    for value, label in (
        (facts.sqft, "heated square footage"),
        (facts.beds, "bedroom count"),
        (facts.baths, "bathroom count"),
    ):
        if value is None:
            result.unknown_facts.append(label)

    # School quality joins that list only when the household actually configured school
    # penalties. A profile with no school preferences should not have every property
    # capped for a fact it never asked about; a profile that made schools a priority
    # should not get a clean TAKE on an address whose zoned school nobody looked up. The
    # cap therefore follows the stated priority rather than being unconditional.
    if profile.schools.penalties and facts.school_proficiency_pct is None:
        result.unknown_facts.append("zoned school proficiency")

    # -- verdict -------------------------------------------------------------

    # An unverified hard fail pins the score rather than letting it stand. Pinning is
    # one-directional: it can only lower, so a missing data source never inflates a
    # property's score into looking better than it is.
    if result.unevaluated_hard_fails:
        result.value = min(result.value, profile.unevaluated_score)
        result.score_pinned = True

    # The same principle applied to physical facts. TAKE means "worth writing an offer",
    # and that claim cannot be made about a house whose size is unknown. The cap lands it
    # in WATCH — go find the missing number, then re-score. One-directional as well, so a
    # property already scoring below the cap keeps its own lower score.
    if result.unknown_facts:
        cap = profile.verdict_take_min - 1
        if result.value > cap:
            result.value = cap
            result.score_capped = True

    if result.value >= profile.verdict_take_min:
        result.verdict = VERDICT_TAKE
    elif result.value >= profile.verdict_watch_min:
        result.verdict = VERDICT_WATCH
    else:
        result.verdict = VERDICT_PASS

    # Belt and braces: whatever the arithmetic said, an unresolved hard fail is never
    # a clean TAKE.
    if result.unevaluated_hard_fails and result.verdict == VERDICT_TAKE:
        result.verdict = VERDICT_WATCH

    _add_caveats(result, facts, profile, current_year)
    return result


def _score_schools(
    result: ScoreResult, facts: PropertyFacts, profile: BuyerProfile
) -> None:
    """Deduct for a zoned school below the household's floor.

    Two graded bands rather than one, because the difference between just under the floor
    and far under it is real. A school two points below a 55% threshold is a rounding
    argument; one twenty points below is a different decision.

    Nothing is deducted when the percentage is unknown, matching every other deduction in
    this module — you cannot penalise a school nobody measured. The consequence is handled
    where the other unknowns are handled, by capping the verdict rather than by inventing a
    penalty.

    The Zone of Choice deduction is separate and additive on purpose. It is not a statement
    about quality; it is the cost of uncertainty. Inside a Zone of Choice the family submits
    preferences and receives a lottery result, so the school named on the property card is
    one possible outcome rather than the assignment. A household comparing two addresses
    with identical proficiency should see the one that guarantees its school rank higher.
    """
    sch = profile.schools
    if not sch.penalties:
        return

    pct = facts.school_proficiency_pct
    if pct is not None:
        gap = sch.min_proficiency_pct - pct
        if gap > sch.well_below_margin_pct:
            points = sch.penalty("well_below_min")
            if points:
                result.deductions.append(
                    {
                        "reason": (
                            f"Zoned school proficiency {pct:.1f}% is {gap:.1f} points "
                            f"below the {sch.min_proficiency_pct:.0f}% floor — far enough "
                            f"below to be a standing compromise, not a rounding argument"
                        ),
                        "points": points,
                        "rule": "school_well_below_min",
                    }
                )
        elif gap > 0:
            points = sch.penalty("below_min")
            if points:
                result.deductions.append(
                    {
                        "reason": (
                            f"Zoned school proficiency {pct:.1f}% is below the "
                            f"{sch.min_proficiency_pct:.0f}% floor by {gap:.1f} points"
                        ),
                        "points": points,
                        "rule": "school_below_min",
                    }
                )

    if facts.school_zone_of_choice:
        points = sch.penalty("zone_of_choice")
        if points:
            result.deductions.append(
                {
                    "reason": (
                        "Address sits in a Zone of Choice — the high school is assigned by "
                        "ranked-preference lottery, so the school shown is a possible "
                        "outcome rather than a guarantee"
                    ),
                    "points": points,
                    "rule": "school_zone_of_choice",
                }
            )


def _add_caveats(
    result: ScoreResult,
    facts: PropertyFacts,
    profile: BuyerProfile,
    current_year: int,
) -> None:
    """Flags that inform but never deduct."""
    if result.score_capped:
        result.caveats.append(
            f"Score capped at {profile.verdict_take_min - 1} because "
            f"{', '.join(result.unknown_facts)} "
            f"{'is' if len(result.unknown_facts) == 1 else 'are'} unknown. "
            f"A house cannot be a TAKE on facts nobody has confirmed — the missing "
            f"deductions may simply not have been applied yet. Fill these in and re-score."
        )
    elif result.unknown_facts:
        result.caveats.append(
            f"{', '.join(result.unknown_facts).capitalize()} unknown, so any related "
            f"deduction is unapplied rather than passed."
        )

    if facts.year_built and facts.year_built < profile.preferred_year_built_min:
        result.caveats.append(
            f"Built {facts.year_built}, before {profile.preferred_year_built_min} — "
            f"age alone is a caveat, not a deduction. Expect older wiring and "
            f"insulation. Specific aging systems are scored separately."
        )

    # Ages that are UNKNOWN on an older house are their own risk: the capital-expense
    # tier could not run, so the score is optimistic by omission. Say so.
    old_enough_to_matter = (
        facts.year_built is not None
        and current_year - facts.year_built >= profile.capex_thresholds["hvac_due_age"]
    )
    if old_enough_to_matter:
        unknown = [
            name
            for name, val in (("roof", facts.roof_age_years), ("HVAC", facts.hvac_age_years))
            if val is None
        ]
        if unknown:
            result.caveats.append(
                f"{' and '.join(unknown)} age unknown on a "
                f"{current_year - facts.year_built}-year-old house — no capital-expense "
                f"deduction could be applied, so this score is optimistic until the "
                f"seller's disclosure fills the gap"
            )

    over = profile.target_price * (1 + profile.max_price_over_target_pct)
    if facts.price > over:
        pct = (facts.price / profile.target_price - 1) * 100
        result.caveats.append(
            f"Price ${facts.price:,.0f} is {pct:.0f}% above the "
            f"${profile.target_price:,.0f} target — negotiate"
        )

    if facts.sqft:
        ppsf = facts.price / facts.sqft
        if ppsf > profile.max_price_per_sqft:
            result.caveats.append(
                f"${ppsf:,.0f}/sqft is above ${profile.max_price_per_sqft:,.0f} — "
                f"check comps in the same ZIP"
            )

    if facts.fiber_available is None:
        result.caveats.append(
            "Broadband unknown — FCC data is census-block precision at best; "
            "call the ISP with the exact address"
        )

    # Said on every property that has a school figure, whatever that figure is. The number
    # is easy to read as a verdict on the school, and it is not one.
    if facts.school_proficiency_pct is not None:
        result.caveats.append(
            f"Zoned school proficiency {facts.school_proficiency_pct:.1f}% is the measured "
            f"average outcome of students currently enrolled, not the school's effect on a "
            f"child who transfers in — CAASPP results track household income closely. "
            f"Attendance boundaries are board policy and can be redrawn."
        )
