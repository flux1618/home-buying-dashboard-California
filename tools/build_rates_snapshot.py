"""Fetch and commit the weekly FRED ``MORTGAGE30US`` mortgage-rate snapshot.

The snapshot is a bounded, reviewable baseline for the later rate-sensitivity module — it
is not a live rate quote. It uses the documented FRED observations API when ``FRED_API_KEY``
is available and otherwise uses FRED's working graph CSV endpoint, which is keyless but
undocumented. See ADR 0016 for the cost of that fallback.

    python tools/build_rates_snapshot.py
    python tools/build_rates_snapshot.py --check

``--check`` never contacts FRED. It exits nonzero when the committed artifact is malformed,
older than ten days, or does not match its derived values. That makes stale scheduled work a
visible CI failure instead of a silently old number dressed as a current rate.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
SNAPSHOT = REPO / "data" / "mortgage30us.json"

from analyzer.sources import rates  # noqa: E402  (repo root must be set before this import)


def _read_snapshot() -> dict[str, Any]:
    try:
        payload = json.loads(SNAPSHOT.read_text())
    except FileNotFoundError:
        raise ValueError(f"{SNAPSHOT.relative_to(REPO)} is missing") from None
    except json.JSONDecodeError as exc:
        raise ValueError(f"{SNAPSHOT.relative_to(REPO)} is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("mortgage snapshot root must be an object")
    return payload


def check(snapshot: dict[str, Any], *, today: date | None = None) -> int:
    """Validate committed shape, freshness, and derived-metric parity without a request."""

    required = {
        "series_id",
        "latest_rate_pct",
        "observation_date",
        "fetched_at",
        "source_url",
        "source_kind",
        "source_note",
        "history",
        "derived",
        "stale",
        "staleness_note",
    }
    missing = sorted(required - set(snapshot))
    if missing:
        print(f"mortgage snapshot missing keys: {missing}", file=sys.stderr)
        return 1
    if snapshot["series_id"] != rates.SERIES_ID:
        print(f"unexpected series_id {snapshot['series_id']!r}", file=sys.stderr)
        return 1
    try:
        history = tuple(
            rates.Observation(date.fromisoformat(row["observation_date"]), float(row["rate_pct"]))
            for row in snapshot["history"]
        )
        history = rates._normalise(list(history))
        latest = history[-1]
        fetched = datetime.fromisoformat(str(snapshot["fetched_at"]).replace("Z", "+00:00"))
    except (KeyError, TypeError, ValueError, IndexError) as exc:
        print(f"mortgage snapshot has invalid history or timestamps: {exc}", file=sys.stderr)
        return 1
    if fetched.tzinfo is None:
        print("mortgage snapshot fetched_at must be timezone-aware", file=sys.stderr)
        return 1
    if snapshot["observation_date"] != latest.observation_date.isoformat():
        print("mortgage snapshot observation_date does not match the last history row", file=sys.stderr)
        return 1
    if float(snapshot["latest_rate_pct"]) != latest.rate_pct:
        print("mortgage snapshot latest_rate_pct does not match the last history row", file=sys.stderr)
        return 1
    expected_derived = rates.derive_stats(history)
    if snapshot["derived"] != expected_derived:
        print(
            "mortgage snapshot derived values drifted from its history; "
            "run python tools/build_rates_snapshot.py",
            file=sys.stderr,
        )
        return 1
    expected_stale = rates.stale_note(latest.observation_date, today=today)
    if bool(snapshot["stale"]) != (expected_stale is not None):
        print("mortgage snapshot stale flag does not match its observation date", file=sys.stderr)
        return 1
    if snapshot["staleness_note"] != expected_stale:
        print("mortgage snapshot staleness note does not match its observation date", file=sys.stderr)
        return 1

    print(
        f"{SNAPSHOT.relative_to(REPO)}: {snapshot['latest_rate_pct']:.2f}% "
        f"on {snapshot['observation_date']} ({len(history)} observations)"
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="validate the committed snapshot without contacting FRED",
    )
    args = parser.parse_args()

    if args.check:
        try:
            return check(_read_snapshot())
        except ValueError as exc:
            print(exc, file=sys.stderr)
            return 1

    series = rates.fetch_series()
    payload = rates.snapshot_payload(
        series,
        fetched_at=datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
    )
    SNAPSHOT.parent.mkdir(parents=True, exist_ok=True)
    rendered = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    previous = SNAPSHOT.read_text() if SNAPSHOT.exists() else None
    if rendered == previous:
        print(f"{SNAPSHOT.relative_to(REPO)} is unchanged")
        return 0
    SNAPSHOT.write_text(rendered)
    print(
        f"wrote {SNAPSHOT.relative_to(REPO)}: {payload['latest_rate_pct']:.2f}% "
        f"on {payload['observation_date']} via {payload['source_kind']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
