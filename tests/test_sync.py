"""Sync: idempotency, review gating, overwrite and closed-won protection, audit trail."""
from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from meeting_crm.config import Config
from meeting_crm.crm import SQLiteCRM
from meeting_crm.extract import Extractor
from meeting_crm.models import Transcript
from meeting_crm.review import APPROVED, DESTRUCTIVE, REJECTED, ReviewQueue, gate
from meeting_crm.sync import SyncResult, Syncer
from meeting_crm.workspace import GatedFact
from tests.conftest import MakeTranscript

CALL_1 = """
[00:00:15] Dana Whitfield: I'm Dana Whitfield, VP of Operations at Acme Logistics.
[00:01:44] Dana Whitfield: Our approved budget is $120,000 for this.
[00:02:16] Dana Whitfield: We need to go live by Q1.
[00:03:20] Sam Ortiz: I'll send over the security questionnaire by Friday.
[00:03:45] Sam Ortiz: Dana, can you share the route exports?
"""
CALL_2 = """
[00:01:02] Elena Ruiz: Our approved budget is $110,000 for year one.
[00:02:00] Elena Ruiz: We are also evaluating Routewise.
"""


class Harness:
    def __init__(self, tmp_path: Path, config: Config) -> None:
        self.config = config
        self.crm = SQLiteCRM(tmp_path / "crm.db")
        self.queue = ReviewQueue(tmp_path / "q.jsonl")
        self.transcripts: list[tuple[Transcript, list[GatedFact]]] = []

    def add(self, t: Transcript) -> list[GatedFact]:
        gated = [gate(f, self.config) for f in Extractor(self.config).extract(t)]
        self.transcripts.append((t, gated))
        return gated

    def sync(self) -> SyncResult:
        dates = {t.transcript_id: t.call_date for t, _ in self.transcripts}
        syncer = Syncer(self.crm, self.config, self.queue, dates)
        result = SyncResult()
        for t, gated in sorted(self.transcripts, key=lambda p: p[0].call_date):
            syncer.sync_transcript(t, gated, result)
        return result

    def deal(self) -> dict[str, str]:
        return self.crm.get_deal("1")

    def counts(self) -> dict[str, int]:
        return {table: self.crm.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in ("accounts", "deals", "contacts", "activities", "audit_log")}


@pytest.fixture
def h(tmp_path: Path, config: Config) -> Harness:
    return Harness(tmp_path, config)


def _fact_id(gated: list[GatedFact], kind: str) -> str:
    return next(g.fact.fact_id for g in gated if g.fact.kind == kind)


def test_sync_applies_high_confidence_facts(h: Harness, make_transcript: MakeTranscript) -> None:
    h.add(make_transcript(CALL_1))
    result = h.sync()
    deal = h.deal()
    assert (deal["amount"], deal["close_target"], deal["stage"]) == ("120000", "2027-Q1", "discovery")
    snap = h.crm.snapshot()
    assert [c["name"] for c in snap["contacts"]] == ["Dana Whitfield"]
    tasks = [a for a in snap["activities"] if a["kind"] == "task"]
    assert [t["owner"] for t in tasks] == ["Sam Ortiz"]  # the named request waits for review
    assert tasks[0]["due_date"] == "2026-09-11"
    assert len(result.awaiting_review) == 1


def test_resync_is_idempotent(h: Harness, make_transcript: MakeTranscript) -> None:
    h.add(make_transcript(CALL_1))
    first = h.sync()
    before = h.counts()
    second = h.sync()
    assert h.counts() == before  # no new rows anywhere, audit included
    assert second.applied == [] and second.writes == 0
    assert second.already_applied == len(first.applied)


def test_re_extracting_same_transcript_does_not_duplicate(h: Harness, make_transcript: MakeTranscript) -> None:
    h.add(make_transcript(CALL_1))
    h.sync()
    before = h.counts()
    h.transcripts.clear()
    h.add(make_transcript(CALL_1))  # same call, extracted again from scratch
    h.sync()
    assert h.counts() == before


def test_review_approve_then_reject_flow(h: Harness, make_transcript: MakeTranscript) -> None:
    gated = h.add(make_transcript(CALL_1))
    request_id = next(g.fact.fact_id for g in gated if g.gate == "review")
    h.sync()
    h.queue.enqueue(next(g.fact for g in gated if g.gate == "review"), "low_confidence", "low")
    h.queue.decide(request_id, APPROVED, "yash")
    result = h.sync()
    assert result.applied == [request_id]
    owners = {a["owner"] for a in h.crm.snapshot()["activities"] if a["kind"] == "task"}
    assert owners == {"Sam Ortiz", "Dana Whitfield"}
    actors = {r["actor"] for r in h.crm.audit_log() if r["fact_id"] == request_id}
    assert actors == {"approved:yash"}


def test_rejected_fact_is_never_applied(h: Harness, make_transcript: MakeTranscript) -> None:
    gated = h.add(make_transcript(CALL_1))
    fact = next(g.fact for g in gated if g.gate == "review")
    h.queue.enqueue(fact, "low_confidence", "low")
    h.queue.decide(fact.fact_id, REJECTED, "yash")
    result = h.sync()
    assert result.rejected == 1
    assert not h.crm.is_applied(fact.fact_id)
    assert h.crm.find_activity(fact.fact_id) is None


def test_overwrite_goes_to_review_then_applies_on_approval(h: Harness, make_transcript: MakeTranscript) -> None:
    h.add(make_transcript(CALL_1))
    h.sync()
    gated = h.add(make_transcript(CALL_2, call_id="test-2", date="2026-10-06"))
    result = h.sync()
    budget_id = _fact_id(gated, "budget")
    assert result.queued_destructive == [budget_id]
    assert h.deal()["amount"] == "120000"  # untouched
    assert h.deal()["competitors"] == "Routewise"  # appending to a list is not destructive
    entry = h.queue.get(budget_id)
    assert entry is not None and entry.review_type == DESTRUCTIVE
    assert "overwrites amount '120000' with '110000'" in entry.reason
    queued = [r for r in h.crm.audit_log() if r["action"] == "queued"]
    assert queued[0]["quote"] == "Our approved budget is $110,000 for year one."

    h.queue.decide(budget_id, APPROVED, "yash")
    h.sync()
    assert h.deal()["amount"] == "110000"
    applied = [r for r in h.crm.audit_log() if r["action"] == "applied" and r["field"] == "amount"]
    assert (applied[-1]["old_value"], applied[-1]["new_value"]) == ("120000", "110000")


def test_closed_won_deal_is_never_auto_modified(h: Harness, make_transcript: MakeTranscript) -> None:
    h.add(make_transcript(CALL_1))
    h.sync()
    h.crm.set_deal_field("1", "stage", "closed_won")
    gated = h.add(make_transcript(CALL_2, call_id="test-2", date="2026-10-06"))
    result = h.sync()
    competitor_id = _fact_id(gated, "competitor")
    assert competitor_id in result.queued_destructive  # even a harmless append is held
    entry = h.queue.get(competitor_id)
    assert entry is not None and "protected stage" in entry.reason
    assert h.deal()["competitors"] == "" and h.deal()["stage"] == "closed_won"


def test_stage_moving_backwards_needs_review(h: Harness, make_transcript: MakeTranscript) -> None:
    h.add(make_transcript("[00:00:05] Sam Ortiz: Let me walk through the pricing proposal."))
    h.sync()
    assert h.deal()["stage"] == "negotiation"  # forward moves auto-apply
    later = h.add(make_transcript("[00:00:05] Sam Ortiz: Tell me about how dispatch works.",
                                  call_id="test-2", date="2026-10-06"))
    result = h.sync()
    assert result.queued_destructive == [_fact_id(later, "stage")]
    assert h.deal()["stage"] == "negotiation"


def test_late_approval_of_older_fact_is_superseded(h: Harness, make_transcript: MakeTranscript) -> None:
    old = h.add(make_transcript(
        "[00:01:44] Dana Whitfield: We have roughly $120k budgeted, maybe more.", date="2026-09-08"))
    h.add(make_transcript(CALL_2, call_id="test-2", date="2026-10-06"))
    h.sync()
    assert h.deal()["amount"] == "110000"
    old_budget = next(g.fact for g in old if g.fact.kind == "budget")
    h.queue.enqueue(old_budget, "low_confidence", "low")
    h.queue.decide(old_budget.fact_id, APPROVED, "yash")
    result = h.sync()
    assert result.superseded == 1
    assert h.deal()["amount"] == "110000"  # the newer call wins
    assert any(r["action"] == "superseded" for r in h.crm.audit_log())


def test_failed_write_rolls_back_fact_and_audit(h: Harness, make_transcript: MakeTranscript,
                                                monkeypatch: pytest.MonkeyPatch) -> None:
    gated = h.add(make_transcript("[00:03:20] Sam Ortiz: I'll send the deck by Friday."))
    original = h.crm.create_activity

    def flaky(account_id: str, deal_id: str, kind: str, *rest: str) -> str:
        if kind == "task":
            raise RuntimeError("CRM is down")
        return original(account_id, deal_id, kind, *rest)

    monkeypatch.setattr(h.crm, "create_activity", flaky)
    with pytest.raises(RuntimeError):
        h.sync()
    fact_id = _fact_id(gated, "action_item")
    assert not h.crm.is_applied(fact_id)
    assert not [r for r in h.crm.audit_log() if r["fact_id"] == fact_id]
    monkeypatch.setattr(h.crm, "create_activity", original)
    assert fact_id in h.sync().applied  # retry after the outage succeeds exactly once


def test_field_map_must_match_crm(h: Harness, config: Config) -> None:
    bad = Config(**{**config.__dict__, "field_map": {"budget": "Amount__c"}})
    with pytest.raises(ValueError, match="Amount__c"):
        Syncer(h.crm, bad, h.queue, {"x": date(2026, 1, 1)})
