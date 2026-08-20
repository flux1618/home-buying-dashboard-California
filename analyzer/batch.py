"""Batch mode: a CSV shortlist in, a ranked comparison out.

    python -m analyzer.batch shortlist.csv --out-dir runs/2026-08-19

Why this exists. Scoring one house at a time answers "is this one any good?". The question
you actually face on a Saturday is "which three of these nine are worth driving to?", and
that is a different question — it needs the same rules applied to every row, at the same
moment, with the disagreements visible side by side.

Three rules inherited from the station contract (ADR 0006), because they matter more in
batch than they do for a single run:

  1. **One bad row never kills the run.** A malformed price on line 4 must not throw away
     the eight rows that parsed. Bad rows are collected and reported, not raised.
  2. **Nothing is guessed.** A blank optional column is unknown, and unknown scores
     differently from bad. A blank *required* column is a row error.
  3. **Every output is reproducible.** Row order in, deterministic ranking out, full
     document written per property so any number in the summary can be traced back.

The CSV is deliberately forgiving about headers and strict about values. You will be
pasting this together from a listing site at 11pm, so `List Price`, `list_price`, and
`price` all work — but `$268,000.00` being parsed as a number is not a favour, it is the
minimum, whereas `call for price` is an error you need to see.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence, TextIO

from .core.profile import BuyerProfile, load_profile
from .pipeline import PipelineAborted, PipelineRun, run

# =============================================================================
# Column handling
# =============================================================================

# Every spelling maps to one canonical field. Listing sites and spreadsheets are
# inconsistent, and correcting headers by hand is exactly the tedium this tool exists to
# remove. The canonical name is the pipeline's own argument name.
ALIASES: dict[str, str] = {
    "address": "address",
    "street address": "address",
    "property": "address",
    "property address": "address",
    "location": "address",
    "price": "price",
    "list price": "price",
    "listing price": "price",
    "asking price": "price",
    "asking": "price",
    "offer price": "price",
    "hoa": "hoa_monthly",
    "hoa monthly": "hoa_monthly",
    "hoa dues": "hoa_monthly",
    "hoa fee": "hoa_monthly",
    "monthly hoa": "hoa_monthly",
    "roof age": "roof_age_years",
    "roof age years": "roof_age_years",
    "roof": "roof_age_years",
    "hvac age": "hvac_age_years",
    "hvac age years": "hvac_age_years",
    "hvac": "hvac_age_years",
    "garage": "garage_spaces",
    "garage spaces": "garage_spaces",
    "garage bays": "garage_spaces",
    "bays": "garage_spaces",
    "mls": "reference",
    "mls number": "reference",
    "mls id": "reference",
    "reference": "reference",
    "id": "reference",
    "notes": "notes",
    "note": "notes",
    "comment": "notes",
    "comments": "notes",
}

REQUIRED = ("address", "price")
NUMERIC_INT = ("roof_age_years", "hvac_age_years", "garage_spaces")
PASSTHROUGH = ("reference", "notes")


def normalise_header(header: str) -> str:
    """`  List Price ($) ` -> `list price`, so aliasing has something predictable to match."""
    text = header.replace("\ufeff", "").strip().lower()
    text = re.sub(r"\(.*?\)", " ", text)          # drop parenthetical units
    text = re.sub(r"[^a-z0-9]+", " ", text)       # underscores, hyphens, symbols -> space
    return re.sub(r"\s+", " ", text).strip()


def map_columns(fieldnames: Sequence[str] | None) -> tuple[dict[str, str], list[str]]:
    """Map real headers to canonical fields. Returns the mapping and any unrecognised headers."""
    mapping: dict[str, str] = {}
    unknown: list[str] = []
    for name in fieldnames or []:
        canonical = ALIASES.get(normalise_header(name))
        if canonical:
            mapping.setdefault(canonical, name)
        elif normalise_header(name):
            unknown.append(name)
    return mapping, unknown


# =============================================================================
# Value parsing
# =============================================================================


class RowError(ValueError):
    """A row that cannot be analysed. Collected and reported, never raised past the batch."""


def _to_float(raw: str, field_name: str) -> float:
    """`$268,000` and `268000.00` are the same number. `call for price` is an error."""
    text = (raw or "").strip().replace("$", "").replace(",", "").replace("_", "")
    if not text:
        raise RowError(f"{field_name} is blank")
    try:
        return float(text)
    except ValueError:
        raise RowError(f"{field_name} is not a number: {raw!r}") from None


def parse_money(raw: str, field_name: str) -> float:
    """A required amount. Must be present and above zero — a free house is a typo."""
    value = _to_float(raw, field_name)
    if value <= 0:
        raise RowError(f"{field_name} must be positive, got {value:,.0f}")
    if value > 100_000_000:
        raise RowError(f"{field_name} looks like a typo: {value:,.0f}")
    return value


def parse_optional_money(raw: str, field_name: str) -> float:
    """Blank or zero HOA both mean no dues, still the common case for LA single-family resales.

    Zero has to be allowed explicitly. Routing this through `parse_money` would reject a
    perfectly sensible `0` in an HOA column, which is the kind of validation that teaches
    people to leave columns blank rather than state a fact.
    """
    if not (raw or "").strip():
        return 0.0
    value = _to_float(raw, field_name)
    if value < 0:
        raise RowError(f"{field_name} cannot be negative: {value:,.0f}")
    if value > 10_000:
        raise RowError(f"{field_name} of {value:,.0f} per month looks like a typo")
    return value


def parse_optional_int(raw: str, field_name: str, *, maximum: int) -> int | None:
    """Blank means unknown. Unknown is not zero, and the engine treats them differently."""
    text = (raw or "").strip()
    if not text or text.lower() in {"unknown", "unk", "n/a", "na", "?", "-"}:
        return None
    try:
        value = int(float(text))
    except ValueError:
        raise RowError(f"{field_name} is not a whole number: {raw!r}") from None
    if value < 0:
        raise RowError(f"{field_name} cannot be negative: {value}")
    if value > maximum:
        raise RowError(f"{field_name} of {value} is out of range (max {maximum})")
    return value


@dataclass
class ShortlistRow:
    """One validated row, ready to hand to the pipeline."""

    line: int
    address: str
    price: float
    hoa_monthly: float = 0.0
    roof_age_years: int | None = None
    hvac_age_years: int | None = None
    garage_spaces: int | None = None
    reference: str = ""
    notes: str = ""

    @property
    def label(self) -> str:
        return f"{self.reference} {self.address}".strip()


@dataclass
class RejectedRow:
    line: int
    problem: str
    raw: dict[str, str] = field(default_factory=dict)

    @property
    def address(self) -> str:
        for key, value in self.raw.items():
            if normalise_header(key) in {"address", "street address", "property", "location"}:
                return (value or "").strip()
        return ""


def read_shortlist(path: Path) -> tuple[list[ShortlistRow], list[RejectedRow], list[str]]:
    """Parse and validate a shortlist file, touching no network.

    Thin wrapper over `parse_shortlist_text` so the filename can appear in the error
    message. The parsing itself takes text rather than a path because the HTTP service
    receives an upload, not a file on disk, and duplicating the validation for that
    caller is exactly how the CLI and the API drift apart.
    """
    return parse_shortlist_text(path.read_text(encoding="utf-8-sig"), source=path.name)


def parse_shortlist_text(
    text: str, *, source: str = "shortlist"
) -> tuple[list[ShortlistRow], list[RejectedRow], list[str]]:
    """Parse and validate shortlist CSV text.

    Returns accepted rows, rejected rows with reasons, and unrecognised headers. Reading is
    fully separated from analysing so `--dry-run` can validate a file offline in
    milliseconds — worth having when the alternative is discovering a bad column on row 9
    of a run that has already made forty HTTP requests.
    """
    reader = csv.DictReader(text.splitlines())
    mapping, unknown = map_columns(reader.fieldnames)

    missing = [name for name in REQUIRED if name not in mapping]
    if missing:
        found = ", ".join(reader.fieldnames or []) or "nothing"
        raise RowError(
            f"{source} is missing a required column: {', '.join(missing)}. "
            f"Found: {found}"
        )

    accepted: list[ShortlistRow] = []
    rejected: list[RejectedRow] = []

    for offset, raw in enumerate(reader, start=2):   # line 1 is the header
        if not any((value or "").strip() for value in raw.values()):
            continue                                  # blank separator line
        try:
            accepted.append(_build_row(offset, raw, mapping))
        except RowError as exc:
            rejected.append(RejectedRow(line=offset, problem=str(exc), raw=dict(raw)))

    return accepted, rejected, unknown


def _build_row(line: int, raw: dict[str, str], mapping: dict[str, str]) -> ShortlistRow:
    def cell(canonical: str) -> str:
        column = mapping.get(canonical)
        return (raw.get(column) or "") if column else ""

    address = cell("address").strip()
    if not address:
        raise RowError("address is blank")

    return ShortlistRow(
        line=line,
        address=address,
        price=parse_money(cell("price"), "price"),
        hoa_monthly=parse_optional_money(cell("hoa_monthly"), "hoa"),
        roof_age_years=parse_optional_int(cell("roof_age_years"), "roof age", maximum=150),
        hvac_age_years=parse_optional_int(cell("hvac_age_years"), "hvac age", maximum=100),
        garage_spaces=parse_optional_int(cell("garage_spaces"), "garage", maximum=10),
        reference=cell("reference").strip(),
        notes=cell("notes").strip(),
    )


# =============================================================================
# Running the batch
# =============================================================================

VERDICT_ORDER = {"TAKE": 0, "WATCH": 1, "PASS": 2}


@dataclass
class BatchEntry:
    """One row's outcome: scored, or failed with a reason."""

    row: ShortlistRow
    document: dict[str, Any] | None = None
    degraded: list[str] = field(default_factory=list)
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.document is not None

    @property
    def verdict(self) -> str:
        return self.document["score"]["verdict"] if self.ok else "ERROR"

    @property
    def score(self) -> int | None:
        return self.document["score"]["value"] if self.ok else None

    @property
    def sort_key(self) -> tuple[int, int, float]:
        """Verdict first, then score descending, then cheaper first as the tiebreak.

        Verdict leads rather than score because the categories are not interchangeable: a
        PASS is a hard fail or an unresolved hard fail, and no amount of points should let
        one outrank a WATCH.
        """
        if not self.ok:
            return (99, 0, 0.0)
        return (VERDICT_ORDER.get(self.verdict, 50), -(self.score or 0), self.row.price)


@dataclass
class BatchResult:
    entries: list[BatchEntry]
    rejected: list[RejectedRow]
    unknown_headers: list[str]
    profile_name: str

    @property
    def ranked(self) -> list[BatchEntry]:
        return sorted(self.entries, key=lambda entry: entry.sort_key)

    @property
    def scored(self) -> list[BatchEntry]:
        return [entry for entry in self.entries if entry.ok]

    def by_verdict(self, verdict: str) -> list[BatchEntry]:
        return [entry for entry in self.ranked if entry.verdict == verdict]


def analyse_shortlist(
    rows: Iterable[ShortlistRow],
    *,
    profile: BuyerProfile | None = None,
    progress: Callable[[ShortlistRow, BatchEntry], None] | None = None,
    runner: Callable[..., PipelineRun] | None = None,
) -> list[BatchEntry]:
    """Analyse every row. A row that fails outright becomes an entry with an error.

    Sequential on purpose. These are free public endpoints — OSRM's demo server and
    Nominatim both ask for polite use, and the HTTP layer already throttles per host.
    Firing ten concurrent requests at a county GIS server to save nine seconds on a
    weekend shortlist is how a useful public service gets locked down.
    """
    # Resolved at call time, not bound as a default argument. `runner: Callable = run`
    # captures whatever `run` was at import, which quietly defeats monkeypatching the
    # module attribute — a test that thought it had stubbed the pipeline would make real
    # requests to the Census geocoder and pass. The socket guard in conftest caught
    # exactly that, and late binding is the fix rather than a louder warning.
    resolved_runner = runner if runner is not None else run
    resolved = profile or load_profile()
    entries: list[BatchEntry] = []

    for row in rows:
        entry = _analyse_row(row, resolved, resolved_runner)
        entries.append(entry)
        if progress:
            progress(row, entry)

    return entries


def _analyse_row(
    row: ShortlistRow, profile: BuyerProfile, runner: Callable[..., PipelineRun]
) -> BatchEntry:
    try:
        result = runner(
            row.address,
            row.price,
            profile=profile,
            hoa_monthly=row.hoa_monthly,
            roof_age_years=row.roof_age_years,
            hvac_age_years=row.hvac_age_years,
            garage_spaces=row.garage_spaces,
        )
    except PipelineAborted as exc:
        # Geocoding failed, so there is no location to ask anything about. Report it
        # against the row and keep going: one unrecognised address must not cost you the
        # rest of the shortlist.
        return BatchEntry(row=row, error=str(exc))
    except OSError as exc:
        # Whole-network failure rather than a single dead source. Same treatment.
        return BatchEntry(row=row, error=f"network unavailable: {exc}")

    document = dict(result.document)
    if row.reference or row.notes:
        document["shortlist"] = {
            "reference": row.reference,
            "notes": row.notes,
            "csv_line": row.line,
        }
    return BatchEntry(row=row, document=document, degraded=result.degraded_stations)


# =============================================================================
# Output
# =============================================================================

SUMMARY_COLUMNS = [
    "rank", "verdict", "score", "score_pinned", "score_capped", "unknown_facts",
    "address", "matched_address", "reference",
    "price", "piti", "true_monthly_low", "true_monthly_high", "cash_to_close",
    "front_end_dti_pct", "sqft", "beds", "baths", "year_built", "flood_zone",
    "water_sewer", "commute_min", "fiber", "capex_low", "capex_high",
    "hard_fails", "unresolved_hard_fails", "blocking_tasks", "degraded_sources", "notes",
]


def summary_rows(result: BatchResult) -> list[dict[str, Any]]:
    """Flatten each analysis into one comparable row.

    A CSV is the right shape for the shortlist question because it goes straight into a
    spreadsheet, which is where a household actually argues about houses. The full
    document is written alongside it so no number here is a dead end.
    """
    rows = []
    for rank, entry in enumerate(result.ranked, start=1):
        if not entry.ok:
            rows.append({
                "rank": rank, "verdict": "ERROR", "score": "",
                "address": entry.row.address, "reference": entry.row.reference,
                "price": f"{entry.row.price:.0f}", "notes": entry.row.notes,
                "hard_fails": entry.error or "",
            })
            continue

        doc = entry.document
        score, cost, inputs = doc["score"], doc["cost"], doc["input"]
        location = doc.get("location", {})
        fiber = inputs.get("fiber_available")

        rows.append({
            "rank": rank,
            "verdict": score["verdict"],
            "score": score["value"],
            "score_pinned": "yes" if score.get("score_pinned") else "",
            # Two different reasons a score is being held back, kept separate on purpose.
            # `pinned` means a hard-fail check could not be run. `capped` means the house
            # may deserve deductions we could not calculate. Collapsing them into one
            # "incomplete" flag would hide which one you can go and fix.
            "score_capped": "yes" if score.get("score_capped") else "",
            "unknown_facts": "; ".join(score.get("unknown_facts") or []),
            "address": entry.row.address,
            "matched_address": location.get("matched_address", ""),
            "reference": entry.row.reference,
            "price": f"{inputs['price']:.0f}",
            "piti": f"{cost['piti']:.0f}",
            "true_monthly_low": f"{cost['true_monthly_low']:.0f}",
            "true_monthly_high": f"{cost['true_monthly_high']:.0f}",
            "cash_to_close": f"{cost['cash_to_close']:.0f}",
            "front_end_dti_pct": f"{cost['front_end_dti'] * 100:.1f}",
            "sqft": inputs.get("sqft") or "",
            "beds": inputs.get("beds") or "",
            "baths": inputs.get("baths") or "",
            "year_built": inputs.get("year_built") or "",
            "flood_zone": inputs.get("flood_zone") or "unknown",
            "water_sewer": inputs.get("water_sewer") or "unknown",
            "commute_min": inputs.get("commute_min") or "",
            # Three states, not two. Blank would read as False in a spreadsheet.
            "fiber": {True: "yes", False: "no", None: "unknown"}[fiber],
            "capex_low": f"{score.get('capex_estimate_low') or 0:.0f}",
            "capex_high": f"{score.get('capex_estimate_high') or 0:.0f}",
            "hard_fails": "; ".join(score.get("hard_fails") or []),
            "unresolved_hard_fails": "; ".join(score.get("unevaluated_hard_fails") or []),
            "blocking_tasks": sum(1 for t in doc["verification_tasks"] if t.get("blocking")),
            "degraded_sources": "; ".join(entry.degraded),
            "notes": entry.row.notes,
        })
    return rows


def write_summary_stream(result: BatchResult, handle: TextIO) -> None:
    """Write the summary to any text stream.

    Streams rather than paths because the HTTP service returns the CSV in a response
    body and never has a directory to write into. Same rows, same column order, one
    implementation.
    """
    writer = csv.DictWriter(handle, fieldnames=SUMMARY_COLUMNS, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(summary_rows(result))


def write_summary_csv(result: BatchResult, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        write_summary_stream(result, handle)


def slug(text: str) -> str:
    cleaned = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return cleaned[:60] or "property"


def write_documents(result: BatchResult, directory: Path) -> list[Path]:
    """One full document per scored property, so every summary number is traceable."""
    directory.mkdir(parents=True, exist_ok=True)
    written = []
    for entry in result.scored:
        path = directory / f"{entry.row.line:03d}-{slug(entry.row.address)}.json"
        path.write_text(json.dumps(entry.document, indent=2) + "\n", encoding="utf-8")
        written.append(path)
    return written


def write_markdown(result: BatchResult, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_markdown(result), encoding="utf-8")


def render_markdown(result: BatchResult) -> str:
    """A comparison table you can read on a phone in a driveway."""
    lines = [
        "# Shortlist comparison",
        "",
        f"Profile: **{result.profile_name}**  ",
        f"{len(result.scored)} of {len(result.entries)} rows scored"
        + (f", {len(result.rejected)} rejected before analysis" if result.rejected else "")
        + ".",
        "",
        "Ranked by verdict first, then score. A PASS never outranks a WATCH regardless of "
        "points, because a PASS means a hard fail or an unresolved one.",
        "",
        "| # | Verdict | Score | Address | Price | True monthly | Commute | Capex | Blocking |",
        "|---|---|---|---|---|---|---|---|---|",
    ]

    for rank, entry in enumerate(result.ranked, start=1):
        if not entry.ok:
            lines.append(
                f"| {rank} | ERROR | — | {entry.row.address} | "
                f"${entry.row.price:,.0f} | — | — | — | {entry.error} |"
            )
            continue
        doc, score, cost = entry.document, entry.document["score"], entry.document["cost"]
        commute = doc["input"].get("commute_min")
        capex_high = score.get("capex_estimate_high") or 0
        blocking = sum(1 for t in doc["verification_tasks"] if t.get("blocking"))
        pin = "\\*" if score.get("score_pinned") else ""
        lines.append(
            f"| {rank} | {score['verdict']} | {score['value']}{pin} | "
            f"{doc.get('location', {}).get('matched_address') or entry.row.address} | "
            f"${doc['input']['price']:,.0f} | "
            f"${cost['true_monthly_low']:,.0f}–${cost['true_monthly_high']:,.0f} | "
            f"{f'{commute:.1f} min' if commute else 'unknown'} | "
            f"{f'${capex_high:,.0f}' if capex_high else '—'} | {blocking} |"
        )

    if any(e.ok and e.document['score'].get('score_pinned') for e in result.entries):
        lines += ["", "\\* Score pinned because a hard-fail input could not be resolved. "
                      "The pin only lowers a score, never raises one."]

    for verdict, heading in (("TAKE", "Worth an offer"), ("WATCH", "Worth a visit")):
        entries = result.by_verdict(verdict)
        if not entries:
            continue
        lines += ["", f"## {heading}", ""]
        for entry in entries:
            doc = entry.document
            address = doc.get("location", {}).get("matched_address") or entry.row.address
            lines.append(f"### {address}")
            if entry.row.notes:
                lines.append(f"\n{entry.row.notes}")
            blocking = [t for t in doc["verification_tasks"] if t.get("blocking")]
            if blocking:
                lines += ["", "Before an offer:", ""]
                lines += [f"- {task['task']}" for task in blocking]
            if entry.degraded:
                lines += ["", f"Degraded sources: {', '.join(entry.degraded)}"]
            lines.append("")

    passes = result.by_verdict("PASS")
    if passes:
        lines += ["", "## Ruled out", ""]
        for entry in passes:
            reasons = entry.document["score"].get("hard_fails") or \
                entry.document["score"].get("unevaluated_hard_fails") or ["score below floor"]
            lines.append(f"- **{entry.row.address}** — {'; '.join(reasons)}")

    if result.rejected:
        lines += ["", "## Rows that could not be read", ""]
        lines += [f"- line {r.line}: {r.problem}" for r in result.rejected]

    if result.unknown_headers:
        lines += ["", "## Ignored columns", "",
                  f"These headers were not recognised and had no effect: "
                  f"{', '.join(result.unknown_headers)}."]

    return "\n".join(lines) + "\n"


# =============================================================================
# CLI
# =============================================================================

BOLD, DIM, OFF = "\033[1m", "\033[2m", "\033[0m"
GREEN, GOLD, RED = "\033[32m", "\033[33m", "\033[31m"
COLOURS = {"TAKE": GREEN, "WATCH": GOLD, "PASS": RED, "ERROR": RED}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Score a shortlist of properties from a CSV.",
        epilog="Required columns: address, price. Optional: hoa, roof age, hvac age, "
               "garage, mls, notes. Header spelling is forgiving.",
    )
    parser.add_argument("csv_path", type=Path, help="shortlist CSV")
    parser.add_argument("--out-dir", type=Path, default=Path("runs"),
                        help="where to write summary.csv, shortlist.md, and per-property JSON")
    parser.add_argument("--dry-run", action="store_true",
                        help="validate the CSV without making any request")
    parser.add_argument("--profile", default=None, help="path to a buyer profile TOML")
    args = parser.parse_args(argv)

    if not args.csv_path.exists():
        print(f"{RED}No such file:{OFF} {args.csv_path}", file=sys.stderr)
        return 2

    try:
        rows, rejected, unknown = read_shortlist(args.csv_path)
    except RowError as exc:
        print(f"{RED}Cannot read the shortlist:{OFF} {exc}", file=sys.stderr)
        return 2

    print(f"\n{BOLD}{args.csv_path.name}{OFF}  {DIM}{len(rows)} rows to analyse{OFF}")
    if unknown:
        print(f"{DIM}  ignoring unrecognised columns: {', '.join(unknown)}{OFF}")
    for bad in rejected:
        print(f"  {RED}line {bad.line} rejected:{OFF} {bad.problem}")

    if args.dry_run:
        print(f"\n{BOLD}Dry run.{OFF} {len(rows)} rows would be analysed, "
              f"{len(rejected)} rejected. No requests made.\n")
        return 1 if rejected else 0

    if not rows:
        print(f"\n{RED}Nothing to analyse.{OFF}\n", file=sys.stderr)
        return 2

    profile = load_profile(args.profile) if args.profile else load_profile()

    def report(row: ShortlistRow, entry: BatchEntry) -> None:
        colour = COLOURS.get(entry.verdict, "")
        if entry.ok:
            flags = f" {DIM}degraded: {', '.join(entry.degraded)}{OFF}" if entry.degraded else ""
            print(f"  {colour}{entry.verdict:<5}{OFF} {entry.score:>3}  {row.address}{flags}")
        else:
            print(f"  {RED}{'ERROR':<5}{OFF}   —  {row.address} {DIM}{entry.error}{OFF}")

    print(f"\n{BOLD}Analysing{OFF} {DIM}(sequential — these are free public endpoints){OFF}")
    entries = analyse_shortlist(rows, profile=profile, progress=report)

    result = BatchResult(
        entries=entries, rejected=rejected, unknown_headers=unknown,
        profile_name=profile.name,
    )

    out = args.out_dir
    write_summary_csv(result, out / "summary.csv")
    write_markdown(result, out / "shortlist.md")
    documents = write_documents(result, out / "properties")

    takes, watches, passes = (len(result.by_verdict(v)) for v in ("TAKE", "WATCH", "PASS"))
    errors = len([e for e in entries if not e.ok])

    print(f"\n{BOLD}Result{OFF}  "
          f"{GREEN}{takes} take{OFF} · {GOLD}{watches} watch{OFF} · {RED}{passes} pass{OFF}"
          + (f" · {RED}{errors} error{OFF}" if errors else ""))
    print(f"{DIM}  {out / 'summary.csv'}\n  {out / 'shortlist.md'}\n"
          f"  {len(documents)} documents in {out / 'properties'}{OFF}\n")

    return 0 if result.scored else 1


if __name__ == "__main__":
    raise SystemExit(main())
