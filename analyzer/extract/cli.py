"""The extractor's door: `python -m analyzer.extract.cli <command>`.

Its own entry point rather than a flag on `analyzer.cli`, for the same reason the ledger has
one: `analyzer.cli` requires a positional address and price, and extracting fields from a PDF
has neither. A `--extract` flag there would mean inventing an address to read a document.

Three commands, and the second two exist because the call log is only useful if something
reads it:

    read      run one document through the extractor and print what was accepted and refused
    log       the last N calls, newest last
    stats     acceptance rate and the histogram of refusal reasons

The default output is written for a person doing the confirmation step ADR 0004 requires, so
the quote is printed under every value. Confirming a value without seeing the sentence it came
from is rubber-stamping, and a rubber-stamped confirmation is worse than none because it
launders an extraction into something the score will treat as reviewed.

`--json` on everything, so this is scriptable and not only readable.
"""

from __future__ import annotations

import argparse
import json
import sys

from .calllog import default_log_path, read_records, summarize
from .documents import DocumentError
from .providers import ProviderError, build_provider
from .run import extract_from_document

BOLD, DIM, OFF = "\033[1m", "\033[2m", "\033[0m"
GREEN, GOLD, RED = "\033[32m", "\033[33m", "\033[31m"

# Refusals are grouped by cause when printed, because they mean different things about the
# provider. A run full of "citation not found" is a fabricating model; a run full of "forbidden
# by ADR 0004" is a model doing arithmetic it was told not to do. Both are fine for the code
# and bad for the provider, and they need different responses.
REASON_COLOUR = {
    "forbidden by ADR 0004": GOLD,
    "citation not found in the document": RED,
    "not in the declared schema": DIM,
    "implausible or unparseable value": RED,
    "missing citation": RED,
    "duplicate field": DIM,
}


def render_run(run) -> None:
    doc = run.document
    print()
    print(f"{BOLD}{doc.path}{OFF}")
    pages = f"{doc.pages_sent} of {doc.pages_total} pages"
    print(f"  {DIM}sent{OFF} {pages}, {run.redacted.chars:,} chars  {DIM}sha256{OFF} {run.redacted.sha256[:12]}")

    report = run.redacted.report
    if report.fired:
        detail = ", ".join(f"{k}×{v}" for k, v in sorted(report.counts.items()))
        print(f"  {GREEN}redacted{OFF} {report.total} items before sending — {detail}")
    else:
        # Worth saying out loud rather than printing nothing. "No redaction fired" on an
        # inspection report is mildly suspicious -- real ones carry an inspector's phone number
        # -- and silence would read as "redaction ran and found nothing" or as "redaction did
        # not run", which are very different.
        print(f"  {DIM}redacted nothing — no patterns matched in this document{OFF}")
    print(f"  {DIM}provider{OFF} {run.provider} / {run.model}  {run.elapsed_ms} ms")

    if run.error:
        print(f"\n  {RED}the call failed:{OFF} {run.error}")
        print(f"  {DIM}logged anyway, so the record of what was sent is complete{OFF}")

    if run.result.findings:
        print(f"\n  {BOLD}Accepted — {run.result.accepted} field(s), none of them confirmed yet{OFF}")
        for finding in run.result.findings:
            print(f"    {GREEN}·{OFF} {BOLD}{finding.field_name}{OFF} = {finding.value}")
            print(f'      {DIM}line {finding.line}: "{finding.quote[:100]}"{OFF}')

    if run.result.rejections:
        print(f"\n  {BOLD}Refused — {run.result.refused} field(s){OFF}")
        for rejection in run.result.rejections:
            colour = REASON_COLOUR.get(rejection.reason, DIM)
            offered = "" if rejection.offered_value is None else f" (offered {rejection.offered_value!r})"
            print(f"    {colour}·{OFF} {rejection.field_name}{offered}")
            print(f"      {DIM}{rejection.reason} — {rejection.detail[:110]}{OFF}")

    if not run.result.findings and not run.error:
        print(f"\n  {DIM}nothing extracted. The document may not state these fields, or the")
        print(f"  page filter may have dropped the page that does — retry with --all-pages.{OFF}")

    print()
    print(f"  {GOLD}Nothing here has affected a score.{OFF} Per ADR 0004 every field above is")
    print(f"  {DIM}confidence=extracted and needs a person to read the quote and confirm it.{OFF}")
    print(f"  {DIM}log: {run.log_path}{OFF}")
    print()


def render_log(records: list[dict]) -> None:
    if not records:
        print(f"  {DIM}no calls logged yet at {default_log_path()}{OFF}")
        return
    print()
    print(f"  {DIM}{'when':21} {'provider':22} {'sha':10} {'sent':>7} {'ok':>4} {'no':>4}{OFF}")
    for record in records:
        flag = f" {RED}error{OFF}" if record.get("error") else ""
        print(
            f"  {record.get('at', '')[:19]:21} "
            f"{str(record.get('provider'))[:21]:22} "
            f"{str(record.get('document_sha256'))[:8]:10} "
            f"{record.get('chars_sent', 0):>7,} "
            f"{record.get('accepted', 0):>4} "
            f"{record.get('refused', 0):>4}{flag}"
        )
    print()


def render_stats(stats: dict) -> None:
    print()
    print(f"  {BOLD}{stats['calls']}{OFF} calls, {stats['errors']} of them failed")
    print(f"  {stats['fields_offered']} fields offered — {GREEN}{stats['accepted']} accepted{OFF}, {stats['refused']} refused")
    if stats["acceptance_rate"] is not None:
        print(f"  {BOLD}acceptance rate {stats['acceptance_rate']:.0%}{OFF}")
        print(f"  {DIM}the number to watch when swapping providers: a drop means more fields{OFF}")
        print(f"  {DIM}the code is refusing, and the histogram below says which rule fired.{OFF}")
    print(f"  redaction fired on {stats['redaction_fired_on']} of {stats['calls']} calls")
    if stats["rejection_reasons"]:
        print(f"\n  {BOLD}Why fields were refused{OFF}")
        for reason, count in stats["rejection_reasons"].items():
            print(f"    {REASON_COLOUR.get(reason, DIM)}{count:>4}{OFF}  {reason}")
    if stats["providers"]:
        print(f"\n  {BOLD}Providers used{OFF}")
        for name, count in sorted(stats["providers"].items(), key=lambda kv: -kv[1]):
            print(f"    {count:>4}  {name}")
    print()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m analyzer.extract.cli",
        description=(
            "Pull declared fields out of a property document. The model reads and the code "
            "decides — see docs/adr/0004-llm-scope-boundary.md."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    read = sub.add_parser("read", help="extract fields from one document")
    read.add_argument("path", help="a .txt, .md, or .pdf file (PDF needs pdftotext installed)")
    read.add_argument(
        "--provider",
        default="offline",
        help=(
            "offline (default, deterministic, no key, no network), ollama (a model on your "
            "own cluster), or openai (any /v1/chat/completions endpoint)"
        ),
    )
    read.add_argument("--model", default=None, help="override the provider's default model")
    read.add_argument(
        "--name",
        action="append",
        default=[],
        metavar="NAME",
        help=(
            "a personal name to redact before sending. Repeatable. Free-standing names are "
            "not detected automatically — see docs/KNOWN_LIMITATIONS.md."
        ),
    )
    read.add_argument(
        "--all-pages",
        action="store_true",
        help=(
            "send every page instead of only those mentioning a schema keyword. Widens what "
            "leaves the machine, so it is opt-in."
        ),
    )
    read.add_argument("--json", action="store_true")

    log = sub.add_parser("log", help="the last N model calls")
    log.add_argument("-n", "--limit", type=int, default=20)
    log.add_argument("--json", action="store_true")

    stats = sub.add_parser("stats", help="acceptance rate and why fields were refused")
    stats.add_argument("--json", action="store_true")

    args = parser.parse_args(argv)

    if args.command == "read":
        try:
            provider = build_provider(args.provider, model=args.model)
            run = extract_from_document(
                args.path,
                provider=provider,
                known_names=tuple(args.name),
                filter_pages=not args.all_pages,
            )
        except (DocumentError, ProviderError) as exc:
            # Exit 1 with a message rather than a traceback. A stack trace when you mistype a
            # filename tells you about the program's internals instead of your mistake.
            print(f"{RED}{exc}{OFF}", file=sys.stderr)
            return 1
        if args.json:
            print(json.dumps(run.to_dict(), indent=2))
        else:
            render_run(run)
        # A failed provider call is exit 1 even though the log line was written, because a
        # script that pipes this into something needs to know the extraction did not happen.
        return 1 if run.error else 0

    if args.command == "log":
        records = read_records(limit=args.limit)
        print(json.dumps(records, indent=2)) if args.json else render_log(records)
        return 0

    if args.command == "stats":
        stats_payload = summarize(read_records())
        print(json.dumps(stats_payload, indent=2)) if args.json else render_stats(stats_payload)
        return 0

    return 2  # pragma: no cover - argparse enforces a subcommand


if __name__ == "__main__":
    raise SystemExit(main())
