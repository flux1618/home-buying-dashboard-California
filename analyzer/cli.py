"""Command line entry point. `python -m analyzer.cli "<address>" <price>`.

Prints the human-readable analysis, or the whole result document with --json.
"""

from __future__ import annotations

import argparse
import json
import sys

from datetime import datetime

from .core.amortization import schedule as build_schedule

# Imported as `solve_max_price` rather than the module, because `render` already binds a
# local named `cost` from the document and shadowing the module there would be a trap.
from .core.cost import solve_max_price
from .core.profile import load_profile
from .pipeline import PipelineAborted, run

BOLD, DIM, OFF = "\033[1m", "\033[2m", "\033[0m"
GREEN, GOLD, RED = "\033[32m", "\033[33m", "\033[31m"

COLOURS = {"TAKE": GREEN, "WATCH": GOLD, "PASS": RED}


def render(doc: dict) -> None:
    score = doc["score"]
    verdict = score["verdict"]
    colour = COLOURS.get(verdict, "")

    location = doc.get("location", {})
    print(f"\n{BOLD}{location.get('matched_address') or doc['input']['address']}{OFF}")
    if location:
        print(
            f"{DIM}{location['latitude']}, {location['longitude']}"
            f"{'  ·  block ' + location['census_block_geoid'] if location.get('census_block_geoid') else ''}{OFF}"
        )

    pinned = "  (pinned — unresolved inputs)" if score.get("score_pinned") else ""
    print(f"\n  {BOLD}Score {colour}{score['value']}{OFF}  →  {colour}{verdict}{OFF}{pinned}")

    stations = doc.get("stations", {})
    if stations.get("degraded"):
        print(f"\n  {GOLD}Degraded stations:{OFF} {', '.join(stations['degraded'])}")
        for entry in doc.get("degraded_sources", []):
            print(f"    {DIM}{entry['station']}: {entry['reason']}{OFF}")

    for label, key in (("Hard fails", "hard_fails"), ("Unresolved", "unevaluated_hard_fails")):
        if score.get(key):
            print(f"\n  {BOLD}{label}{OFF}")
            for item in score[key]:
                print(f"    {RED}·{OFF} {item}")

    if score.get("deductions"):
        print(f"\n  {BOLD}Deductions{OFF} {DIM}(-{score['total_deducted']}){OFF}")
        for entry in score["deductions"]:
            print(f"    {GOLD}-{entry['points']:<3}{OFF} {entry['reason']}")

    if score.get("capital_expenses"):
        print(f"\n  {BOLD}Near-term capital expenses{OFF} {DIM}(-{score['capex_deducted']}){OFF}")
        for entry in score["capital_expenses"]:
            print(
                f"    {GOLD}-{entry['points_deducted']:<3}{OFF} {entry['component']}  "
                f"${entry['estimate_low']:,.0f}-${entry['estimate_high']:,.0f}"
            )

    if score.get("caveats"):
        print(f"\n  {BOLD}Caveats{OFF} {DIM}(no points){OFF}")
        for caveat in score["caveats"]:
            print(f"    {DIM}·{OFF} {caveat}")

    cost = doc["cost"]
    print(f"\n  {BOLD}Monthly{OFF}")
    print(f"    PITI            ${cost['piti']:>10,.0f}   front-end DTI {cost['front_end_dti']*100:.1f}%")
    print(f"    True monthly    ${cost['true_monthly_low']:>10,.0f} - ${cost['true_monthly_high']:,.0f}")
    print(f"    Cash to close   ${cost['cash_to_close']:>10,.0f}")

    render_hazards(doc.get("hazard_profile"))
    render_max_price(doc.get("max_price"))

    blocking = [t for t in doc["verification_tasks"] if t.get("blocking")]
    advisory = [t for t in doc["verification_tasks"] if not t.get("blocking")]
    print(f"\n  {BOLD}Before an offer{OFF} {DIM}({len(blocking)} blocking){OFF}")
    for task in blocking:
        print(f"    {RED}!{OFF} {task['task']}")
        if task.get("reason"):
            print(f"      {DIM}{task['reason']}{OFF}")
    if advisory:
        print(f"\n  {BOLD}Worth doing{OFF} {DIM}({len(advisory)}){OFF}")
        for task in advisory:
            print(f"    {DIM}·{OFF} {task['task']}")
    print()


def render_max_price(solution: dict | None) -> None:
    """The inverse question: not "can I afford this house" but "what can I offer".

    Printed under Monthly because it is the same arithmetic read backwards, and it uses
    the sqft and build year the pipeline just looked up rather than asking again.

    Both numbers are shown, always. The lender figure is larger and is the one people
    quote; the household figure is the one that survives a year of ownership. Printing
    only the first would be technically accurate and practically misleading.
    """
    if not solution:
        return

    ceiling = solution["dti_ceiling"]
    print(f"\n  {BOLD}Max price at {ceiling * 100:.0f}% front-end DTI{OFF}")

    if not solution["feasible"]:
        print(f"    {RED}No price clears this ceiling.{OFF}")
        for note in solution["notes"]:
            print(f"      {DIM}{note}{OFF}")
        return

    lender = solution["lender_max_price"]
    household = solution["household_max_price"]
    print(f"    Lender basis     ${lender:>10,.0f}   {DIM}PITI only, what an approval shows{OFF}")
    print(f"    Household basis  ${household:>10,.0f}   {DIM}PITI + maintenance reserve{OFF}")
    for note in solution["notes"]:
        print(f"    {DIM}· {note}{OFF}")


def _money(value: float) -> str:
    """Dollars with the sign attached, so right-aligning a column keeps them together."""
    return f"${value:,.0f}"


def render_amortization(amort: dict | None) -> None:
    """The year table, not the 360 rows.

    Nobody reads 360 lines in a terminal, and the thing worth seeing is how little the
    balance moves early on. Twelve-month rollups make that obvious in one screen. The full
    row set is still in the JSON output and over HTTP for anyone who wants to plot it.

    Every figure here is principal and interest only, which is printed rather than assumed:
    it is a smaller number than the PITI line a few inches above it, and a reader who
    mistakes one for the other underestimates the real monthly cost by hundreds of dollars.
    """
    if not amort:
        return

    print(f"\n  {BOLD}Amortization{OFF}  {DIM}principal and interest only{OFF}")
    print(
        f"    ${amort['loan_amount']:,.0f} at {amort['annual_rate'] * 100:.2f}% "
        f"over {amort['term_months'] // 12} years"
    )

    extra = amort["extra_monthly"]
    if extra:
        print(
            f"    Payment  ${amort['scheduled_payment']:,.2f}"
            f" + ${extra:,.0f} extra = ${amort['scheduled_payment'] + extra:,.2f}"
        )
    else:
        print(f"    Payment  ${amort['scheduled_payment']:,.2f}")

    print(f"    Interest over the life   {_money(amort['total_interest']):>12}")
    print(f"    Total paid               {_money(amort['total_paid']):>12}")

    if extra:
        years = amort["months_saved"] // 12
        months = amort["months_saved"] % 12
        span = f"{years}y {months}m" if years else f"{months}m"
        print(
            f"    {GREEN}The extra saves ${amort['interest_saved']:,.0f} "
            f"and ends it {span} early{OFF}"
        )

    # The single most surprising number in the table, so it gets its own line rather than
    # being left for the reader to find by scanning the columns.
    if amort["crossover_month"]:
        cm = amort["crossover_month"]
        basis = " including the extra" if extra else ""
        print(
            f"    {DIM}Principal first exceeds interest at payment {cm} "
            f"(year {cm / 12:.1f}){basis}{OFF}"
        )

    print(f"\n    {DIM}Year   Interest    Principal      Balance{OFF}")
    for y in amort["years"]:
        # Every fifth year, the first, and the last. A 30-row table in a terminal is a wall;
        # this keeps the shape of the curve visible without printing all of it.
        if y["year"] % 5 and y["year"] != 1 and y is not amort["years"][-1]:
            continue
        n = y["payments"]
        partial = "" if n == 12 else f"  {DIM}({n} payment{'' if n == 1 else 's'}){OFF}"
        print(
            f"    {y['year']:>4}  {_money(y['interest']):>10}  {_money(y['principal']):>11}"
            f"  {_money(y['ending_balance']):>12}{partial}"
        )

    print(f"    {DIM}Excludes {', '.join(amort['excludes'])}.{OFF}")


def _street_of(address: str) -> str:
    """The house number and street name, upper-cased, punctuation dropped.

    Used only to decide whether the geocoder substituted a different address. Deliberately
    crude: it compares the part a person would recognise and ignores the ZIP, because a
    corrected ZIP is a fix and a corrected street name is a substitution.
    """
    head = address.upper().split(",")[0]
    return "".join(ch for ch in head if ch.isalnum() or ch == " ").strip()


def render_ledger(saved: dict) -> None:
    """Confirm what was written, then report what moved since last time.

    The delta is the whole reason to save at all, so it prints unprompted. The `comparable`
    flag is honoured strictly: when the engine version or the buyer profile changed between
    the two rows, the score delta is printed as unusable rather than shown next to the price
    change, because a reader will otherwise attribute a change in our own rules to the market.
    """
    print(f"\n  {BOLD}Ledger{OFF}")
    label = "Saved as" if saved["first_time"] else "Appended to"
    print(f"    {label:<12} {saved['key']}  {DIM}#{saved['analysis_number']}{OFF}")

    # The geocoder does fuzzy matching, and it will quietly hand back a *different street* for
    # an address it cannot find -- "115 Chestnut Ridge Dr" came back as "115 Chestnut St" in a
    # different ZIP. Every number below it was then computed for a house nobody asked about.
    # Silence here is the dangerous case, so say it out loud whenever the match is not the
    # request. Compared loosely because case and punctuation differences are not substitutions.
    requested = (saved.get("requested") or "").upper()
    if requested and _street_of(requested) not in saved["key"]:
        print(f"    {GOLD}Matched a different address{OFF}")
        print(f"    {DIM}you typed {requested} - verify this is the same house before trusting anything above{OFF}")

    diff = saved.get("diff")
    if not diff:
        print(f"    {DIM}First analysis of this address - nothing to compare against yet.{OFF}")
        return

    price = diff["price_delta"]
    if price:
        direction = "Price down" if price < 0 else "Price up"
        pct = f" ({diff['price_pct']:+.1f}%)" if diff["price_pct"] is not None else ""
        print(f"    {direction:<12} ${abs(price):,.0f}{pct}   {DIM}since {diff['from_at'][:10]}{OFF}")
    else:
        print(f"    {DIM}Price unchanged since {diff['from_at'][:10]}.{OFF}")

    if diff["comparable"]:
        if diff["score_delta"]:
            print(f"    {'Score':<12} {diff['score_delta']:+d}")
        if diff["verdict_changed"]:
            print(f"    {'Verdict':<12} {diff['verdict_from']} -> {diff['verdict_to']}")
    else:
        print(f"    {GOLD}Score is not comparable to the earlier run:{OFF}")
        for reason in diff["incomparable_because"]:
            print(f"      {DIM}{reason}{OFF}")


def render_hazards(profile: dict | None) -> None:
    """FEMA National Risk Index for the census tract, printed after the money.

    Deliberately below Monthly and above the task list, because that is what this data
    is: it does not change the score, it changes what you go ask an insurance agent.

    Percentiles are shown and FEMA's rating labels are not, with one exception. The
    labels are binned separately for each hazard, so a "Relatively Moderate" wildfire
    rating and a "Relatively Moderate" heat rating are nowhere near the same national
    position — printing them side by side in a column would invite exactly the wrong
    comparison. The number is the comparable thing.
    """
    if not profile:
        return

    hazards = profile.get("hazards") or {}
    modeled = {c: h for c, h in hazards.items() if h.get("modeled")}
    unmodeled = [h["label"] for h in hazards.values() if not h.get("modeled")]
    if not modeled and not unmodeled and not profile.get("nri_composite_risk"):
        return

    tract = profile.get("tract_fips") or "unknown tract"
    print(f"\n  {BOLD}Hazard risk{OFF} {DIM}(FEMA NRI · tract {tract} · no score effect){OFF}")

    for label, key in (
        ("Social vulnerability", "social_vulnerability"),
        ("Community resilience", "community_resilience"),
    ):
        entry = profile.get(key)
        if entry and entry.get("percentile") is not None:
            print(
                f"    {label:<22} {entry['percentile']:>5.1f}"
                f"  {DIM}{entry.get('rating', '')}{OFF}"
            )

    # Sorted worst-first: the reason to read this section is to find the one hazard that
    # is out of line, and an alphabetical list buries it.
    ranked = sorted(modeled.values(), key=lambda h: h["percentile"], reverse=True)
    for hazard in ranked:
        pct = hazard["percentile"]
        colour = RED if pct >= 90 else GOLD if pct >= 75 else ""
        # OFF only when a colour was actually opened. Emitting a reset unconditionally
        # leaves a visible escape sequence in any terminal or log that is not
        # interpreting ANSI, which is most places this output gets pasted.
        suffix = OFF if colour else ""
        print(f"    {hazard['label']:<22} {colour}{pct:>5.1f}{suffix}")

    if unmodeled:
        print(
            f"    {DIM}not modeled here: {', '.join(sorted(unmodeled))} "
            f"— unknown, not low{OFF}"
        )

    # FEMA's composite risk index is deliberately not printed as a headline number. It
    # averages all 18 hazards, and most hazards do not apply to any given place, so a
    # tract can be extreme in the one hazard that will actually happen to it and still
    # rate low overall. The tract containing Paradise, California scores 32nd percentile
    # composite and 95th for wildfire. The composite is only worth surfacing when it
    # disagrees with the worst hazard, and then only to say so out loud.
    #
    # Both conditions are required, and the second one was added because the first alone
    # was wrong. A gap test by itself fires on Spartanburg, where the composite reads 18.6
    # and the worst hazard is hail at 51.7 — a 33-point gap and nothing anyone needs to
    # act on. Warning that a composite "understates" a middling hazard is noise, and noise
    # in a caveat channel teaches people to skip the caveats. The note only earns its
    # place when the hidden hazard is genuinely elevated.
    composite = profile.get("nri_composite_risk") or {}
    composite_pct = composite.get("percentile")
    if composite_pct is not None and ranked:
        worst = ranked[0]
        if worst["percentile"] >= 75.0 and worst["percentile"] - composite_pct >= 25.0:
            print(
                f"    {DIM}FEMA's all-hazard composite reads {composite_pct:.1f} for this "
                f"tract, which understates {worst['label']} at "
                f"{worst['percentile']:.1f} — the composite averages 18 hazards, most of "
                f"which do not apply here{OFF}"
            )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Analyse one property end to end.")
    parser.add_argument("address")
    parser.add_argument("price", type=float)
    parser.add_argument("--hoa", type=float, default=0.0, help="monthly HOA dues")
    parser.add_argument("--roof-age", type=int, default=None)
    parser.add_argument("--hvac-age", type=int, default=None)
    parser.add_argument("--garage", type=int, default=None, help="garage bay count")
    parser.add_argument("--profile", default=None)
    parser.add_argument(
        "--max-price",
        nargs="?",
        type=float,
        const=-1.0,
        default=None,
        metavar="DTI_PCT",
        help=(
            "also solve for the highest price that holds front-end DTI at or under a "
            "ceiling, given as a percent. Bare --max-price uses the profile target."
        ),
    )
    parser.add_argument(
        "--save",
        action="store_true",
        help=(
            "append this analysis to the local ledger (Phase 3). Never overwrites an earlier "
            "one: re-running the same address records a second row and reports what moved."
        ),
    )
    parser.add_argument(
        "--db",
        default=None,
        metavar="PATH",
        help="ledger database file; defaults to $HBA_DATA_DIR or the XDG data directory",
    )
    parser.add_argument(
        "--amortize",
        action="store_true",
        help="show the amortization schedule for the loan this price implies",
    )
    parser.add_argument(
        "--extra-monthly",
        type=float,
        default=0.0,
        # The reason anyone opens an amortization table: what does paying a bit more do.
        help="extra principal per month, to see what it saves (implies --amortize)",
    )
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    profile = load_profile(args.profile) if args.profile else load_profile()

    try:
        result = run(
            args.address,
            args.price,
            profile=profile,
            hoa_monthly=args.hoa,
            roof_age_years=args.roof_age,
            hvac_age_years=args.hvac_age,
            garage_spaces=args.garage,
        )
    except PipelineAborted as exc:
        print(f"{RED}Could not analyse this address:{OFF} {exc}", file=sys.stderr)
        return 1

    if args.max_price is not None:
        # Reuses the sqft and year built the pipeline already looked up, so the reserve
        # inside the solved price is the same reserve reported above it.
        ceiling = None if args.max_price < 0 else args.max_price / 100.0
        facts = result.document["input"]
        result.document["max_price"] = solve_max_price(
            profile,
            dti_ceiling=ceiling,
            sqft=facts.get("sqft"),
            year_built=facts.get("year_built"),
            hoa_monthly=args.hoa,
            current_year=datetime.now().year,
        ).to_dict()

    if args.amortize or args.extra_monthly:
        # The loan, not the price. Down payment comes from the profile, so this schedule is
        # the one that goes with the PITI figure printed above it rather than a generic
        # illustration -- and the loan is derived from the price actually passed in.
        loan = args.price - profile.down_payment
        if loan <= 0:
            print(
                f"{DIM}No schedule: the down payment covers the whole price.{OFF}",
                file=sys.stderr,
            )
        else:
            # include_payments=False. 360 rows would bury the analysis document, and the
            # annual rollup plus the totals is what a person reads. The HTTP door returns
            # the full rows for anyone who wants to plot them.
            result.document["amortization"] = build_schedule(
                loan,
                profile.mortgage_rate,
                profile.loan_term_months,
                extra_monthly=args.extra_monthly,
            ).to_dict(include_payments=False)

    if args.save:
        # Imported here, not at module scope. The ledger is the only part of this CLI that
        # touches a filesystem outside the cache, and an unconditional import would mean a
        # plain `analyze` run creates a database directory it never uses.
        from ledger import Ledger, connect

        conn = connect(args.db)
        try:
            saved = Ledger(conn).save_analysis(result.document, profile=profile)
        finally:
            conn.close()
        result.document["ledger"] = {
            "key": saved["property"]["key"],
            "analysis_id": saved["analysis_id"],
            "analysis_number": saved["analysis_number"],
            # What was typed, kept next to what it matched, so the renderer can point out a
            # geocoder substitution. Stored in the document too -- a saved analysis of the
            # wrong house should carry the evidence of how it got there.
            "requested": saved["property"]["raw_input"],
            "first_time": saved["created"],
            "diff": saved["diff"],
        }

    if args.json:
        print(json.dumps(result.document, indent=2))
    else:
        render(result.document)
        render_amortization(result.document.get("amortization"))
        if args.save:
            render_ledger(result.document["ledger"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
