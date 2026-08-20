"""Eyeball the scoring engine against fixtures. No network.

    python -m analyzer.demo                 # summary table
    python -m analyzer.demo --detail 4      # full breakdown for one property
    python -m analyzer.demo --json 1        # raw output document

The point of this is to check the weights feel right BEFORE any API is wired up.
"""

from __future__ import annotations

import argparse
import json
from datetime import date
from pathlib import Path

from .core.analyze import analyze
from .core.profile import load_profile
from .core.scoring import PropertyFacts

FIXTURES = Path(__file__).parent / "fixtures" / "sample_properties.json"

GREEN, GOLD, RED, DIM, BOLD, OFF = (
    "\033[92m", "\033[93m", "\033[91m", "\033[2m", "\033[1m", "\033[0m",
)


def load_fixtures() -> list[dict]:
    with open(FIXTURES) as fh:
        return json.load(fh)


def facts_from(row: dict) -> PropertyFacts:
    return PropertyFacts(
        price=row["price"],
        sqft=row.get("sqft"),
        beds=row.get("beds"),
        baths=row.get("baths"),
        garage_spaces=row.get("garage_spaces"),
        hoa_monthly=row.get("hoa_monthly") or 0.0,
        year_built=row.get("year_built"),
        roof_age_years=row.get("roof_age_years"),
        hvac_age_years=row.get("hvac_age_years"),
        flood_zone=row.get("flood_zone"),
        water_sewer=row.get("water_sewer"),
        commute_min=row.get("commute_min"),
        fiber_available=row.get("fiber_available"),
        school_district=row.get("school_district"),
        school_proficiency_pct=row.get("school_proficiency_pct"),
        school_zone_of_choice=row.get("school_zone_of_choice"),
    )


def colour(verdict: str) -> str:
    return {"TAKE": GREEN, "WATCH": GOLD, "PASS": RED}.get(verdict, "")


def summary(rows: list[dict], profile, year: int) -> None:
    print(f"\n{BOLD}Profile:{OFF} {profile.name}")
    print(
        f"{DIM}Rate {profile.mortgage_rate * 100:.2f}%  ·  down "
        f"${profile.down_payment:,.0f}  ·  DTI ceiling "
        f"{profile.target_front_end_dti * 100:.0f}%  ·  commute limit "
        f"{profile.max_commute_min:.0f} min ({profile.primary_anchor.label}, "
        f"{profile.primary_anchor.arrival_window}){OFF}\n"
    )

    head = f"{'#':>2}  {'Property':<40} {'Price':>9} {'School':>6} {'Score':>5} {'Verdict':<8} {'PITI':>8} {'True mo.':>16} {'DTI':>6} {'Capex':>9}"
    print(BOLD + head + OFF)
    print(DIM + "-" * len(head) + OFF)

    for i, row in enumerate(rows, 1):
        doc = analyze(facts_from(row), profile, year, address=row["address"])
        s, c = doc["score"], doc["cost"]
        v = s["verdict"]
        label = row["label"][:40]
        true_mo = f"${c['true_monthly_low']:,.0f}-{c['true_monthly_high']:,.0f}"
        dti_flag = "" if c["dti_within_target"] else "!"
        capex_hi = s["capex_estimate_high"]
        capex_col = f"${capex_hi:,.0f}" if capex_hi else "-"
        pin = "*" if s["score_pinned"] else " "
        school = row.get("school_proficiency_pct")
        school_col = f"{school:.1f}%" if school is not None else "?"
        print(
            f"{i:>2}  {label:<40} ${row['price']:>8,} "
            f"{school_col:>6} "
            f"{colour(v)}{s['value']:>4}{pin}{OFF} {colour(v)}{v:<8}{OFF} "
            f"${c['piti']:>7,.0f} {true_mo:>16} "
            f"{c['front_end_dti'] * 100:>5.1f}%{dti_flag} {capex_col:>9}"
        )

    print(f"\n{DIM}True mo. includes the maintenance reserve range; PITI and DTI do not.{OFF}")
    print(f"{DIM}Capex is the high end of estimated near-term replacements (roof, HVAC).{OFF}")
    print(f"{DIM}* score pinned to 50 because a hard-fail input could not be evaluated.{OFF}")
    print(f"{DIM}Run with --detail N for the full breakdown.{OFF}\n")


def detail(row: dict, profile, year: int) -> None:
    doc = analyze(facts_from(row), profile, year, address=row["address"])
    s, c, t, m = doc["score"], doc["cost"], doc["tax"], doc["maintenance_reserve"]
    v = s["verdict"]

    print(f"\n{BOLD}{row['label']}{OFF}")
    print(f"{DIM}{row['address']}{OFF}\n")
    print(f"  Score    {colour(v)}{BOLD}{s['value']}{OFF}  →  {colour(v)}{BOLD}{v}{OFF}")

    if s["hard_fails"]:
        print(f"\n  {RED}{BOLD}Hard fails{OFF}")
        for f in s["hard_fails"]:
            print(f"    {RED}x{OFF} {f}")
    if s["unevaluated_hard_fails"]:
        print(f"\n  {GOLD}{BOLD}Could not evaluate{OFF} {DIM}(verdict capped at WATCH){OFF}")
        for f in s["unevaluated_hard_fails"]:
            print(f"    {GOLD}?{OFF} {f}")
    if s["deductions"]:
        print(f"\n  {BOLD}Deductions{OFF} {DIM}(-{s['total_deducted']} total){OFF}")
        for d in s["deductions"]:
            print(f"    {GOLD}-{d['points']:<3}{OFF} {d['reason']}")
    if s["capital_expenses"]:
        print(
            f"\n  {BOLD}Near-term capital expenses{OFF} "
            f"{DIM}(-{s['capex_deducted']} pts, "
            f"${s['capex_estimate_low']:,.0f}-${s['capex_estimate_high']:,.0f}){OFF}"
        )
        for e in s["capital_expenses"]:
            tag = RED if e["urgency"] == "overdue" else GOLD
            mag = e["magnitude"].replace("_", " ")
            money = f"${e['estimate_low']:,.0f}-${e['estimate_high']:,.0f}"
            print(
                f"    {tag}-{e['points_deducted']:<3}{OFF} {e['component']}  "
                f"{BOLD}{money}{OFF}  {tag}[{mag}]{OFF}"
            )
            print(f"          {DIM}{e['reason']}{OFF}")
    if s["caveats"]:
        print(f"\n  {BOLD}Caveats{OFF} {DIM}(no points deducted){OFF}")
        for cv in s["caveats"]:
            print(f"    {DIM}·{OFF} {cv}")

    print(f"\n  {BOLD}Monthly{OFF}")
    print(f"    P&I              ${c['principal_interest']:>9,.0f}")
    print(f"    Tax (Prop 13)    ${c['monthly_tax']:>9,.0f}   {DIM}TRA {t['rate_area']}, "
          f"{t['rate']['total_rate_pct']:.4f}%{OFF}")
    print(f"    Insurance        ${c['monthly_insurance']:>9,.0f}   {DIM}CA planning figure — get a quote{OFF}")
    print(f"    HOA              ${c['monthly_hoa']:>9,.0f}")
    print(f"    {BOLD}PITI             ${c['piti']:>9,.0f}{OFF}   {DIM}front-end DTI {c['front_end_dti'] * 100:.1f}%{OFF}")
    print(f"    Reserve          ${c['maintenance_reserve_low']:>9,.0f} - ${c['maintenance_reserve_high']:,.0f}")
    print(f"    {BOLD}True monthly     ${c['true_monthly_low']:>9,.0f} - ${c['true_monthly_high']:,.0f}{OFF}")
    print(f"    Cash to close    ${c['cash_to_close']:>9,.0f}")

    print(f"\n  {BOLD}Maintenance reserve — three estimates, not averaged{OFF}")
    for meth in m["methods"].values():
        print(f"    ${meth['monthly']:>6,.0f}/mo   {DIM}{meth['basis']}{OFF}")

    print(f"\n  {BOLD}Tax scenarios{OFF} {DIM}(assessed value resets to sale price){OFF}")
    for key in ("scenario_owner_occupied", "scenario_no_exemption"):
        sc = t[key]
        # California has no millage. The rate is a percentage applied to assessed value,
        # and the only difference between these two scenarios is the $7,000 exemption.
        print(
            f"    ${sc['annual_tax']:>7,.0f}/yr  {sc['label']:<44} "
            f"{DIM}{sc['applicable_rate_pct']:.6f}% on ${sc['assessed_value']:,.0f}{OFF}"
        )
    print(f"    {DIM}Delta ${t['delta_annual']:,.0f}/yr between scenarios{OFF}")

    schools = doc["schools"]
    proficiency = schools["proficiency_pct"]
    school_value = f"{proficiency:.1f}%" if proficiency is not None else "unknown"
    print(f"\n  {BOLD}Schools{OFF}")
    print(f"    District         {schools['district'] or 'unknown'}")
    print(f"    Proficiency      {school_value}   {DIM}floor {schools['min_proficiency_pct']:.0f}%{OFF}")
    if schools["zone_of_choice"]:
        print(f"    {GOLD}Zone of Choice — assigned by ranked-preference lottery{OFF}")

    blocking = [t_ for t_ in doc["verification_tasks"] if t_["blocking"]]
    other = [t_ for t_ in doc["verification_tasks"] if not t_["blocking"]]
    print(f"\n  {BOLD}Before an offer{OFF} {DIM}(blocking){OFF}")
    for t_ in blocking:
        print(f"    {RED}!{OFF} {t_['task']}")
    print(f"\n  {BOLD}Also{OFF}")
    for t_ in other:
        print(f"    {DIM}·{OFF} {t_['task']}")
    print()


def main() -> None:
    ap = argparse.ArgumentParser(description="Score fixture properties offline.")
    ap.add_argument("--detail", type=int, metavar="N", help="full breakdown for property N")
    ap.add_argument("--json", type=int, metavar="N", help="raw output document for property N")
    ap.add_argument("--profile", type=str, default=None, help="path to a buyer profile TOML")
    args = ap.parse_args()

    rows = load_fixtures()
    profile = load_profile(args.profile)
    year = date.today().year

    if args.json:
        row = rows[args.json - 1]
        doc = analyze(facts_from(row), profile, year, address=row["address"])
        print(json.dumps(doc, indent=2))
    elif args.detail:
        detail(rows[args.detail - 1], profile, year)
    else:
        summary(rows, profile, year)


if __name__ == "__main__":
    main()
