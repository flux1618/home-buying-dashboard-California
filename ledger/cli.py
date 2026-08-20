"""The ledger's own door: `python -m ledger.cli <command>`.

Separate from `analyzer.cli` rather than bolted onto it, for a plain reason: the analyze CLI
takes an address and a price and answers one question about one house. Everything here is
about the *set* of houses and the record of decisions over them. Merging the two would mean a
required positional address on commands that have nothing to do with an address.

Subcommands are verbs about the record, not about the engine:

    list        the shortlist, best score first
    show        one house: history, journal, and what moved
    status      move a house through the process (writes a journal entry)
    note        append an assumption, decision, observation, or outcome
    open        assumptions and decisions nothing has come back to close
    stats       counts, and the schema version
    export      the shortlist as CSV, because that is where decisions get argued
    forget      delete a typo, refuses once there is a record worth keeping

Every command takes `--json`, so this door is scriptable and not only readable. The exit code
is 1 on a ledger error rather than a traceback -- a CLI that prints a stack trace when you
mistype a status is telling you about its internals instead of your mistake.
"""

from __future__ import annotations

import argparse
import json
import sys

from .db import connect, database_path
from .repo import JOURNAL_KINDS, STATUSES, Ledger, LedgerError, PropertyNotFound, rows_to_csv

BOLD, DIM, OFF = "\033[1m", "\033[2m", "\033[0m"
GREEN, GOLD, RED = "\033[32m", "\033[33m", "\033[31m"
COLOURS = {"TAKE": GREEN, "WATCH": GOLD, "PASS": RED}


# =============================================================================
# Rendering
# =============================================================================


def _money(value: float | None) -> str:
    return f"${value:,.0f}" if value is not None else "—"


# Irregular plurals, written out. A general pluralizer for four nouns would be a library
# nobody asked for; a lookup is honest about the size of the problem.
_PLURALS = {
    "analysis": "analyses",
    "journal entry": "journal entries",
    "open assumption": "open assumptions",
    "property": "properties",
}


def _count(n: int, noun: str) -> str:
    """Say "1 property" rather than "1 properties". Small, but this output is the first thing a
    reader sees, and sloppiness there reads as sloppiness underneath."""
    return f"{n} {noun if n == 1 else _PLURALS.get(noun, noun + 's')}"


def render_list(rows: list[dict]) -> None:
    if not rows:
        print(f"{DIM}Nothing saved yet. Run: python -m analyzer.cli \"<address>\" <price> --save{OFF}")
        return

    print(f"\n  {BOLD}{'SCORE':<7}{'VERDICT':<9}{'PRICE':<12}{'PITI':<10}{'STATUS':<11}ADDRESS{OFF}")
    for row in rows:
        latest = row["latest"] or {}
        verdict = latest.get("verdict") or "—"
        colour = COLOURS.get(verdict, "")
        score = latest.get("score")
        # A capped score is marked here rather than only in the detail view. The cap means
        # the house could not be scored on confirmed facts, and a bare number in a ranked
        # list reads as a measurement.
        mark = "*" if latest.get("score_capped") else " "
        shown = f"{score}{mark}" if score is not None else "—"
        print(
            f"  {colour}{shown:<7}{verdict:<9}{OFF}"
            f"{_money(latest.get('price')):<12}{_money(latest.get('piti')):<10}"
            f"{row['status']:<11}{row['matched_address'] or row['raw_input']}"
        )
        if row["analysis_count"] > 1 or row["journal_count"]:
            print(
                f"  {DIM}{'':<7}{row['analysis_count']} analyses, "
                f"{row['journal_count']} journal entries{OFF}"
            )
    if any((r["latest"] or {}).get("score_capped") for r in rows):
        print(f"\n  {DIM}* score capped because a fact is unconfirmed, not measured{OFF}")


def render_diff(diff: dict | None) -> None:
    if not diff:
        return
    print(f"\n  {BOLD}Since {diff['from_at'][:10]}{OFF}")
    if diff["price_delta"]:
        pct = f" ({diff['price_pct']:+.1f}%)" if diff["price_pct"] is not None else ""
        print(f"    Price       {diff['price_delta']:+,.0f}{pct}")
    else:
        print(f"    {DIM}Price unchanged{OFF}")
    if diff["comparable"]:
        # "+0" is noise dressed up as information. Say it did not move.
        if diff["score_delta"]:
            print(f"    Score       {diff['score_delta']:+d}")
        elif diff["score_delta"] == 0:
            print(f"    {DIM}Score unchanged{OFF}")
        if diff["verdict_changed"]:
            print(f"    Verdict     {diff['verdict_from']} -> {diff['verdict_to']}")
    else:
        # Said out loud in every door that shows a delta. See Ledger.diff.
        print(f"    {GOLD}Score is not comparable across these two runs:{OFF}")
        for reason in diff["incomparable_because"]:
            print(f"      {DIM}{reason}{OFF}")


def render_show(detail: dict) -> None:
    prop = detail["property"]
    latest = prop["latest"] or {}
    verdict = latest.get("verdict") or "—"
    colour = COLOURS.get(verdict, "")

    print(f"\n  {BOLD}{prop['matched_address'] or prop['raw_input']}{OFF}")
    print(f"  {DIM}{prop['key']}{OFF}")
    if not prop["resolved"]:
        print(
            f"  {GOLD}Address was never geocoded — this row is keyed on what you typed and "
            f"may duplicate a resolved one.{OFF}"
        )
    print(
        f"  {colour}{latest.get('score', '—')} {verdict}{OFF}   "
        f"{_money(latest.get('price'))}   PITI {_money(latest.get('piti'))}   "
        f"status {prop['status']}"
    )
    if latest:
        print(
            f"  {DIM}engine {latest['engine_version']}  profile {latest['profile_fingerprint']}  "
            f"analyzed {latest['analyzed_at'][:16].replace('T', ' ')}{OFF}"
        )

    history = detail["history"]
    if len(history) > 1:
        print(f"\n  {BOLD}History{OFF}")
        for row in history:
            flags = " capped" if row["score_capped"] else ""
            print(
                f"    {row['analyzed_at'][:10]}  {_money(row['price']):>11}  "
                f"score {str(row['score'] or '—'):>3}{flags}  {DIM}engine {row['engine_version']} "
                f"profile {row['profile_fingerprint']}{OFF}"
            )

    render_diff(detail["diff"])

    if detail["journal"]:
        print(f"\n  {BOLD}Journal{OFF}")
        for entry in detail["journal"]:
            render_entry(entry, indent="    ", show_property=False)


def render_entry(entry: dict, *, indent: str = "  ", show_property: bool = True) -> None:
    """`show_property=False` under a single house's heading.

    Repeating the address on every line of that house's own journal is noise that pushes the
    part you are reading -- the note -- off to the right.
    """
    tail = f" {DIM}resolves #{entry['resolves']}{OFF}" if entry["resolves"] else ""
    where = ""
    if show_property and entry["property_key"]:
        where = f" {DIM}[{entry['property_key']}]{OFF}"
    print(
        f"{indent}{DIM}#{entry['id']} {entry['created_at'][:10]}{OFF} "
        f"{BOLD}{entry['kind']}{OFF}{where}  {entry['body']}{tail}"
    )


# =============================================================================
# Commands
# =============================================================================


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m ledger.cli",
        description="The saved-property ledger: shortlist, history, and decision journal.",
    )
    parser.add_argument("--db", default=None, metavar="PATH", help="ledger file; default is $HBA_DATA_DIR")
    parser.add_argument("--json", action="store_true", help="machine-readable output")

    # `--db` and `--json` are accepted on either side of the subcommand, because both
    # `ledger.cli --json stats` and `ledger.cli stats --json` are what people actually type
    # and having one of them fail is a pointless thing to remember.
    #
    # `default=argparse.SUPPRESS` on the copies is the whole trick. Without it, the
    # subparser's own default would *overwrite* a value already parsed from before the
    # subcommand, so `--json stats` would silently print human output. SUPPRESS means the
    # attribute is only set when the flag is actually present.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--db", default=argparse.SUPPRESS, metavar="PATH", help="ledger file; default is $HBA_DATA_DIR")
    common.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help="machine-readable output")

    sub = parser.add_subparsers(dest="command", required=True, parser_class=argparse.ArgumentParser)

    p_list = sub.add_parser("list", help="the shortlist, best score first", parents=[common])
    p_list.add_argument("--status", choices=STATUSES, default=None)
    p_list.add_argument("--all", action="store_true", help="include archived")

    p_show = sub.add_parser("show", help="one house in full", parents=[common])
    p_show.add_argument("key")
    p_show.add_argument("--document", action="store_true", help="print the stored analysis document")

    p_status = sub.add_parser("status", help="move a house through the process", parents=[common])
    p_status.add_argument("key")
    p_status.add_argument("status", choices=STATUSES)
    p_status.add_argument("--note", default=None, help="why — recorded in the journal with the change")

    p_note = sub.add_parser("note", help="append a journal entry", parents=[common])
    p_note.add_argument("kind", choices=JOURNAL_KINDS)
    p_note.add_argument("body")
    p_note.add_argument("--key", default=None, help="attach to one house; omit for a general entry")
    p_note.add_argument("--resolves", type=int, default=None, metavar="ID", help="close an earlier entry")
    p_note.add_argument("--author", default=None)

    sub.add_parser("open", help="assumptions and decisions nothing has closed", parents=[common])
    sub.add_parser("stats", help="counts and schema version", parents=[common])
    sub.add_parser("where", help="print the path to the database file", parents=[common])
    sub.add_parser("export", help="the shortlist as CSV on stdout", parents=[common])

    p_forget = sub.add_parser("forget", help="delete a typo; refuses once there is a record", parents=[common])
    p_forget.add_argument("key")

    args = parser.parse_args(argv)

    if args.command == "where":
        # Answered without opening anything. `connect` would create the directory, and a
        # command whose only job is to tell you a path should not have a side effect.
        print(database_path(args.db))
        return 0

    conn = connect(args.db)
    ledger = Ledger(conn)
    try:
        return _dispatch(args, ledger)
    except PropertyNotFound as exc:
        print(f"{RED}No saved property with key:{OFF} {exc}", file=sys.stderr)
        print(f"{DIM}Run `list` to see the keys that exist.{OFF}", file=sys.stderr)
        return 1
    except LedgerError as exc:
        print(f"{RED}{exc}{OFF}", file=sys.stderr)
        return 1
    finally:
        conn.close()


def _dispatch(args: argparse.Namespace, ledger: Ledger) -> int:
    if args.command == "list":
        rows = ledger.list_properties(status=args.status, include_archived=args.all)
        print(json.dumps(rows, indent=2)) if args.json else render_list(rows)
        return 0

    if args.command == "show":
        if args.document:
            print(json.dumps(ledger.latest_document(args.key), indent=2))
            return 0
        detail = ledger.get_property(args.key)
        print(json.dumps(detail, indent=2)) if args.json else render_show(detail)
        return 0

    if args.command == "status":
        moved = ledger.set_status(args.key, args.status, note=args.note)
        if args.json:
            print(json.dumps(moved, indent=2))
        else:
            print(f"  {moved['previous']} -> {BOLD}{moved['status']}{OFF}  {moved['key']}")
            print(f"  {DIM}Recorded in the journal.{OFF}")
        return 0

    if args.command == "note":
        entry = ledger.add_journal_entry(
            kind=args.kind, body=args.body, key=args.key,
            resolves=args.resolves, author=args.author,
        )
        print(json.dumps(entry, indent=2)) if args.json else render_entry(entry)
        return 0

    if args.command == "open":
        entries = ledger.open_assumptions()
        if args.json:
            print(json.dumps(entries, indent=2))
        elif not entries:
            print(f"{DIM}Nothing open. Every assumption on record has been closed by an outcome.{OFF}")
        else:
            print(f"\n  {BOLD}Open — claimed, never checked{OFF}")
            for entry in entries:
                render_entry(entry, indent="    ")
            print(f"\n  {DIM}Close one with: note outcome \"...\" --resolves <id>{OFF}")
        return 0

    if args.command == "stats":
        stats = ledger.stats()
        if args.json:
            print(json.dumps(stats, indent=2))
        else:
            print(f"\n  {BOLD}Ledger{OFF}  {DIM}schema v{stats['schema_version']}{OFF}")
            print(f"    {_count(stats['properties'], 'property')}, "
                  f"{_count(stats['analyses'], 'analysis')}, "
                  f"{_count(stats['journal_entries'], 'journal entry')}")
            for status, count in sorted(stats["by_status"].items()):
                print(f"    {DIM}{status:<11}{count}{OFF}")
            if stats["open_assumptions"]:
                print(f"    {GOLD}{_count(stats['open_assumptions'], 'open assumption')}{OFF}")
        return 0

    if args.command == "export":
        # CSV goes to stdout with no colour and no header text, so `> shortlist.csv` produces
        # a file a spreadsheet opens rather than one it complains about.
        print(rows_to_csv(ledger.list_properties(include_archived=True)), end="")
        return 0

    if args.command == "forget":
        result = ledger.forget_property(args.key)
        print(json.dumps(result, indent=2)) if args.json else print(f"  Forgot {result['key']}")
        return 0

    raise LedgerError(f"unknown command {args.command!r}")


if __name__ == "__main__":
    raise SystemExit(main())
