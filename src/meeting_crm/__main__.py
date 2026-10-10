"""CLI: ingest -> extract -> review -> sync, plus show, audit, and eval."""
from __future__ import annotations

import argparse
import getpass
import sys
import textwrap
from pathlib import Path

from meeting_crm.config import Config, ConfigError, load_config
from meeting_crm.crm import AuditEntry, SQLiteCRM
from meeting_crm.evals import render_report, run_eval
from meeting_crm.extract import Extractor
from meeting_crm.ingest import IngestError, parse_file
from meeting_crm.models import Fact
from meeting_crm.review import (
    APPROVED,
    LOW_CONFIDENCE,
    PENDING,
    REJECTED,
    QueueEntry,
    QueueError,
    ReviewQueue,
    gate,
)
from meeting_crm.sync import SyncResult, Syncer
from meeting_crm.workspace import Workspace


def _crm(args: argparse.Namespace) -> SQLiteCRM:
    return SQLiteCRM(Path(args.db) if args.db else Path(args.workdir) / "crm.db")


def _short(text: str, width: int) -> str:
    return text if len(text) <= width else text[: width - 3] + "..."


def _cite(fact: Fact) -> str:
    return f"{fact.transcript_id} {fact.citations[0].render()}"


def cmd_ingest(args: argparse.Namespace, config: Config) -> int:
    ws = Workspace(Path(args.workdir))
    failed = 0
    for name in args.files:
        try:
            result = parse_file(Path(name), config.default_deal_name, config.internal_speakers)
        except (IngestError, OSError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            failed += 1
            continue
        t = result.transcript
        ws.save_transcript(t)
        external = [s for s in t.speakers if not t.is_internal(s)]
        print(f"ingested {t.transcript_id}  {t.call_date}  {t.title}")
        print(f"  {len(t.utterances)} utterances, customer speakers: {', '.join(external) or 'none'}")
        for w in result.warnings:
            print(f"  warning: {w}", file=sys.stderr)
    return 1 if failed else 0


def cmd_extract(args: argparse.Namespace, config: Config) -> int:
    ws = Workspace(Path(args.workdir))
    transcripts = (
        [ws.load_transcript(tid) for tid in args.transcripts] if args.transcripts else ws.transcripts()
    )
    if not transcripts:
        print("nothing to extract: run `ingest` first", file=sys.stderr)
        return 1
    extractor = Extractor(config)
    queue = ReviewQueue(ws.queue_path)
    crm = _crm(args)
    try:
        for t in transcripts:
            gated = [gate(f, config) for f in extractor.extract(t)]
            ws.save_facts(t.transcript_id, gated)
            n_review = sum(1 for g in gated if g.gate == "review")
            print(f"\n{t.transcript_id}  {t.title}  ({len(gated)} facts: "
                  f"{len(gated) - n_review} auto, {n_review} to review)")
            with crm.transaction():
                for g in gated:
                    f = g.fact
                    label = "AUTO  " if g.gate == "auto" else "REVIEW"
                    print(f"  {label} {f.confidence:.2f}  {f.fact_id[:8]}  {_short(f.summary(), 64)}")
                    if args.verbose:
                        print(f"           cite: {f.citations[0].render()}")
                        print(f"           why:  {'; '.join(f.reasons)}")
                    if g.gate == "review":
                        reason = f"confidence {f.confidence:.2f} below {f.kind} threshold {g.threshold:.2f}"
                        if queue.enqueue(f, LOW_CONFIDENCE, reason):
                            c = f.citations[0]
                            crm.record_audit(AuditEntry(
                                "pipeline", "queued", "fact", f.fact_id, new_value=f.summary(),
                                fact_id=f.fact_id, transcript_id=f.transcript_id, quote=c.quote,
                                quote_timestamp=c.timestamp, speaker=c.speaker, detail=reason,
                            ))
        queue.save()
    finally:
        crm.close()
    print(f"\nreview queue: {len(queue.pending())} pending ({ws.queue_path})")
    return 0


def _print_entry(e: QueueEntry) -> None:
    f = e.fact
    print(f"  {f.fact_id[:8]}  [{e.status}] {e.review_type}  conf {f.confidence:.2f}  {f.summary()}")
    print(f"      reason: {e.reason}")
    for c in f.citations:
        print(f"      cite:   {f.transcript_id} {c.render()}")
    print(textwrap.fill("why: " + "; ".join(f.reasons), width=100,
                        initial_indent="      ", subsequent_indent="           "))
    if e.status != PENDING:
        note = f" ({e.note})" if e.note else ""
        print(f"      {e.status} by {e.decided_by} at {e.decided_at}{note}")


def cmd_review_list(args: argparse.Namespace, config: Config) -> int:
    queue = ReviewQueue(Workspace(Path(args.workdir)).queue_path)
    entries = queue.all() if args.all else queue.pending()
    if not entries:
        print("review queue is empty" if args.all else "no facts pending review")
        return 0
    print(f"{len(entries)} {'queued' if args.all else 'pending review'}:")
    for e in entries:
        _print_entry(e)
    return 0


def _decide(args: argparse.Namespace, status: str) -> int:
    ws = Workspace(Path(args.workdir))
    queue = ReviewQueue(ws.queue_path)
    try:
        entry = queue.decide(args.fact_id, status, args.by, args.note)
    except QueueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    crm = _crm(args)
    try:
        c = entry.fact.citations[0]
        crm.record_audit(AuditEntry(
            args.by, status, "fact", entry.fact_id, new_value=entry.fact.summary(),
            fact_id=entry.fact_id, transcript_id=entry.fact.transcript_id, quote=c.quote,
            quote_timestamp=c.timestamp, speaker=c.speaker,
            detail=f"{entry.review_type}: {entry.reason}" + (f" | note: {args.note}" if args.note else ""),
        ))
    finally:
        crm.close()
    queue.save()
    verb = "approved" if status == APPROVED else "rejected"
    tail = " (run `sync` to apply)" if status == APPROVED else ""
    print(f"{verb} {entry.fact_id[:8]}: {entry.fact.summary()}{tail}")
    return 0


def cmd_review_approve(args: argparse.Namespace, config: Config) -> int:
    return _decide(args, APPROVED)


def cmd_review_reject(args: argparse.Namespace, config: Config) -> int:
    return _decide(args, REJECTED)


def cmd_sync(args: argparse.Namespace, config: Config) -> int:
    ws = Workspace(Path(args.workdir))
    transcripts = ws.transcripts()
    if not transcripts:
        print("nothing to sync: run `ingest` and `extract` first", file=sys.stderr)
        return 1
    queue = ReviewQueue(ws.queue_path)
    crm = _crm(args)
    result = SyncResult()
    try:
        syncer = Syncer(crm, config, queue, {t.transcript_id: t.call_date for t in transcripts})
        for t in transcripts:
            gated = ws.load_facts(t.transcript_id)
            if not gated:
                print(f"skipping {t.transcript_id}: not extracted yet", file=sys.stderr)
                continue
            syncer.sync_transcript(t, gated, result)
        queue.save()
    finally:
        crm.close()
    print(f"applied {len(result.applied)} facts ({result.writes} CRM writes)")
    print(f"already applied (skipped, idempotent): {result.already_applied}")
    print(f"waiting for review (low confidence): {len(result.awaiting_review)}")
    print(f"held for review (destructive change): {len(result.queued_destructive)}")
    if result.superseded:
        print(f"superseded by a newer call (logged, not written): {result.superseded}")
    if result.rejected:
        print(f"rejected by reviewer (never applied): {result.rejected}")
    if result.awaiting_review or result.queued_destructive:
        print("run `review list` to see what needs a human")
    return 0


def cmd_show(args: argparse.Namespace, config: Config) -> int:
    crm = _crm(args)
    try:
        snap = crm.snapshot()
    finally:
        crm.close()
    for deal in snap["deals"]:
        print(f"DEAL  {deal['name']}  (account: {deal['account']})")
        for key in ("stage", "amount", "close_target", "competitors", "risks", "next_step"):
            print(f"  {key:<13}{deal[key] or '-'}")
    print("\nCONTACTS")
    for c in snap["contacts"]:
        print(f"  {c['name']:<18}{c['role'] or '-':<24}{c['email'] or '-'}")
    print("\nACTIVITIES")
    for a in snap["activities"]:
        due = f"  due {a['due_date']}" if a["due_date"] and a["kind"] == "task" else ""
        owner = f"{a['owner']}: " if a["owner"] else ""
        print(f"  [{a['kind']}] {owner}{_short(a['subject'], 70)}{due}")
    return 0


def cmd_audit(args: argparse.Namespace, config: Config) -> int:
    crm = _crm(args)
    try:
        rows = crm.audit_log(args.limit)
    finally:
        crm.close()
    for r in rows:
        change = f"{r['entity']}"
        if r["field"]:
            change += f".{r['field']}"
        if r["old_value"] or r["new_value"]:
            change += f" {r['old_value'] or '(empty)'!r} -> {r['new_value']!r}"
        print(f"{r['at']}  {r['action']:<10} {r['actor']:<18} {_short(change, 90)}")
        if r["quote"] and args.verbose:
            print(f"    cite: {r['transcript_id']} [{r['quote_timestamp']}] {r['speaker']}: \"{r['quote']}\"")
        if r["detail"] and args.verbose:
            print(f"    note: {r['detail']}")
    print(f"({len(rows)} audit rows)")
    return 0


def cmd_eval(args: argparse.Namespace, config: Config) -> int:
    report = run_eval(Path(args.fixtures), config, args.min_confidence)
    sys.stdout.write(render_report(report, verbose=args.verbose))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="meeting-crm",
        description="Turn sales-call transcripts into cited, reviewable, idempotent CRM updates",
    )
    parser.add_argument("--config", default="config.yaml", help="per-customer YAML config")
    parser.add_argument("--workdir", default=".meeting-crm", help="pipeline state directory")
    parser.add_argument("--db", default=None, help="SQLite CRM path (default: <workdir>/crm.db)")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("ingest", help="parse transcript files into utterances")
    p.add_argument("files", nargs="+", help="transcript .txt files")
    p.set_defaults(func=cmd_ingest)

    p = sub.add_parser("extract", help="extract facts and gate them by confidence")
    p.add_argument("transcripts", nargs="*", help="transcript ids (default: all ingested)")
    p.add_argument("-v", "--verbose", action="store_true", help="show citations and score reasons")
    p.set_defaults(func=cmd_extract)

    review = sub.add_parser("review", help="human-in-the-loop review queue")
    rsub = review.add_subparsers(dest="review_command", required=True)
    p = rsub.add_parser("list", help="show facts waiting for a human")
    p.add_argument("--all", action="store_true", help="include decided entries")
    p.set_defaults(func=cmd_review_list)
    for name, func in (("approve", cmd_review_approve), ("reject", cmd_review_reject)):
        p = rsub.add_parser(name, help=f"{name} a queued fact (id prefix is fine)")
        p.add_argument("fact_id")
        p.add_argument("--note", default="", help="reason, kept in the audit log")
        p.add_argument("--by", default=getpass.getuser(), help="reviewer name for the audit log")
        p.set_defaults(func=func)

    p = sub.add_parser("sync", help="apply approved facts to the CRM")
    p.set_defaults(func=cmd_sync)

    p = sub.add_parser("show", help="print current CRM state")
    p.set_defaults(func=cmd_show)

    p = sub.add_parser("audit", help="print the audit log")
    p.add_argument("--limit", type=int, default=None, help="only the last N rows")
    p.add_argument("-v", "--verbose", action="store_true", help="show citations and details")
    p.set_defaults(func=cmd_audit)

    p = sub.add_parser("eval", help="score extraction against labeled fixtures")
    p.add_argument("--fixtures", default="fixtures", help="dir with transcripts/ and labels/")
    p.add_argument("--min-confidence", type=float, default=0.0,
                   help="only score facts at or above this confidence")
    p.add_argument("-v", "--verbose", action="store_true", help="list every miss and false positive")
    p.set_defaults(func=cmd_eval)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        config = load_config(Path(args.config))
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2
    try:
        return int(args.func(args, config))
    except (FileNotFoundError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
