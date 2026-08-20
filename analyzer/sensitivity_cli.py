"""Rate-band command line door.

Run `python -m analyzer.sensitivity_cli --help`. This module, not `analyzer.core.sensitivity`,
reads the optional mortgage-rate snapshot: the core stays pure and is passed an already labelled
baseline rate. If `data/mortgage30us.json` is absent, the profile rate is used and printed as a
configured assumption rather than a market observation.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

from .core.profile import BuyerProfile, load_profile
from .core.sensitivity import BaselineRate, profile_baseline, rate_band, wait_vs_buy

REPO = Path(__file__).resolve().parents[1]
DEFAULT_SNAPSHOT = REPO / "data" / "mortgage30us.json"


def baseline_from_snapshot(profile: BuyerProfile, path: Path = DEFAULT_SNAPSHOT) -> BaselineRate:
    """Use a local 30-year observation only when the snapshot is actually present.

    A malformed present file is an error, not a reason to quietly substitute the profile. The
    accepted snapshot shape is deliberately small: a top-level `rate`, `value`, or `latest_rate_pct`, or the same
    fields inside `latest`; values from 0 through 1 are decimal fractions and values above 1
    through 100 are percent. `source_url` and `as_of` are carried into the label when supplied.
    """
    if not path.exists():
        return profile_baseline(profile)
    try:
        raw = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"could not read mortgage snapshot at {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ValueError(f"mortgage snapshot at {path} must be a JSON object")
    latest = raw.get("latest") if isinstance(raw.get("latest"), dict) else raw
    value = latest.get("rate", latest.get("value", raw.get("latest_rate_pct")))
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise ValueError(f"mortgage snapshot at {path} has no numeric rate or value")
    if not 0 <= float(value) <= 100:
        raise ValueError(f"mortgage snapshot rate must be from 0 through 100, got {value!r}")
    rate = float(value) / 100.0 if float(value) > 1 else float(value)
    source_url = latest.get("source_url") or raw.get("source_url")
    as_of = (latest.get("as_of") or latest.get("date") or raw.get("as_of") or raw.get("observation_date"))
    label = f"data/mortgage30us.json ({'as of ' + str(as_of) if as_of else 'local snapshot'})"
    return BaselineRate(annual_rate=rate, source=label, source_url=source_url)


def _rate_fraction(percent: float, name: str) -> float:
    if percent < 0 or percent > 100:
        raise argparse.ArgumentTypeError(f"{name} must be from 0 through 100")
    return percent / 100.0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Apply stated mortgage-rate scenarios to the same house. This is arithmetic on "
            "assumptions, not a mortgage-rate or home-price forecast."
        )
    )
    parser.add_argument("--profile", metavar="PATH", help="buyer_profile.toml override")
    parser.add_argument("--price", type=float, help="purchase price; defaults to profile target")
    parser.add_argument("--hoa", type=float, default=0.0, help="monthly HOA dues (default: 0)")
    parser.add_argument("--sqft", type=float, help="square footage for the household max-price reserve")
    parser.add_argument("--year-built", type=int, help="year built for the household max-price reserve")
    parser.add_argument("--year", type=int, default=datetime.now().year, help="model year for maintenance")
    parser.add_argument("--dti-ceiling-pct", type=float, help="override profile front-end DTI ceiling")
    parser.add_argument("--start-rate-pct", type=float, default=5.00, help="band start (default: 5.00)")
    parser.add_argument("--end-rate-pct", type=float, default=7.50, help="band end (default: 7.50)")
    parser.add_argument("--step-pct", type=float, default=0.25, help="band step (default: 0.25)")
    parser.add_argument(
        "--baseline-rate-pct",
        type=float,
        help="explicit anchor; otherwise use data/mortgage30us.json only when it exists",
    )
    parser.add_argument(
        "--snapshot",
        type=Path,
        default=DEFAULT_SNAPSHOT,
        help="optional local 30-year-rate snapshot path (default: data/mortgage30us.json)",
    )
    parser.add_argument("--future-rate-pct", type=float, help="hypothetical wait scenario rate")
    parser.add_argument("--future-price", type=float, help="hypothetical wait scenario price")
    parser.add_argument("--json", action="store_true", help="print the complete JSON document")
    return parser


def render(document: dict[str, object]) -> None:
    band = document["band"]
    assert isinstance(band, dict)
    baseline = band["baseline"]
    assert isinstance(baseline, dict)
    print("\nRate sensitivity — same house, stated assumptions")
    print(f"  House price: ${band['price']:,.0f}")
    print(f"  Baseline: {baseline['annual_rate'] * 100:.2f}% · {baseline['source']}")
    print("\n  Rate     Full PITI/mo    P&I/mo     Term interest     Lender max    Household max")
    for point in band["points"]:
        assert isinstance(point, dict)
        lender = "infeasible" if point["lender_max_price"] is None else f"${point['lender_max_price']:,.0f}"
        household = (
            "infeasible"
            if point["household_max_price"] is None
            else f"${point['household_max_price']:,.0f}"
        )
        print(
            f"  {point['annual_rate'] * 100:>5.2f}%  ${point['piti']:>10,.2f}  "
            f"${point['principal_interest']:>9,.2f}  ${point['total_interest']:>13,.2f}  "
            f"{lender:>12}  {household:>14}"
        )
    if "wait_vs_buy" in document:
        comparison = document["wait_vs_buy"]
        assert isinstance(comparison, dict)
        print("\nWait vs buy — hypothetical, not a forecast")
        print(
            f"  Today: ${comparison['baseline_price']:,.0f} at {comparison['baseline_rate'] * 100:.2f}%"
            f" · later: ${comparison['future_price']:,.0f} at {comparison['future_rate'] * 100:.2f}%"
        )
        print(
            f"  Full-PITI delta: ${comparison['monthly_payment_delta']:+,.2f}/mo"
            f" · lifetime interest delta: ${comparison['lifetime_interest_delta']:+,.2f}"
        )
        print(
            f"  PITI break-even future price: ${comparison['break_even_future_price']:,.2f} "
            f"({comparison['break_even_price_change']:+,.2f} vs today)"
        )
    print(f"\n  Note: {band['note']}")


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    profile = load_profile(args.profile) if args.profile else load_profile()
    price = profile.target_price if args.price is None else args.price
    try:
        if args.baseline_rate_pct is None:
            baseline = baseline_from_snapshot(profile, args.snapshot)
        else:
            baseline = BaselineRate(
                annual_rate=_rate_fraction(args.baseline_rate_pct, "baseline rate"),
                source="explicit --baseline-rate-pct assumption (not a market observation)",
            )
        document: dict[str, object] = {
            "band": rate_band(
                profile,
                price,
                baseline=baseline,
                start_rate=_rate_fraction(args.start_rate_pct, "start rate"),
                end_rate=_rate_fraction(args.end_rate_pct, "end rate"),
                step=_rate_fraction(args.step_pct, "step"),
                dti_ceiling=(
                    None
                    if args.dti_ceiling_pct is None
                    else _rate_fraction(args.dti_ceiling_pct, "DTI ceiling")
                ),
                sqft=args.sqft,
                year_built=args.year_built,
                hoa_monthly=args.hoa,
                current_year=args.year,
            ).to_dict()
        }
        if (args.future_rate_pct is None) != (args.future_price is None):
            raise ValueError("--future-rate-pct and --future-price must be supplied together")
        if args.future_rate_pct is not None:
            document["wait_vs_buy"] = wait_vs_buy(
                profile,
                baseline_rate=baseline.annual_rate,
                baseline_price=price,
                future_rate=_rate_fraction(args.future_rate_pct, "future rate"),
                future_price=args.future_price,
                sqft=args.sqft,
                year_built=args.year_built,
                hoa_monthly=args.hoa,
                current_year=args.year,
            ).to_dict()
    except ValueError as exc:
        print(f"rate sensitivity refused these inputs: {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(document, indent=2))
    else:
        render(document)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
