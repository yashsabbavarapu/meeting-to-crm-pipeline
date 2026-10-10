"""Confidence gating and the JSONL review queue."""
from __future__ import annotations

from pathlib import Path

import pytest

from meeting_crm.config import Config
from meeting_crm.models import Citation, Fact
from meeting_crm.review import (
    APPROVED, DESTRUCTIVE, LOW_CONFIDENCE, PENDING, REJECTED, QueueError, ReviewQueue, gate,
)


def _fact(fact_id: str = "abc123def456", kind: str = "budget", confidence: float = 0.6) -> Fact:
    return Fact(fact_id, "t1", kind, {"amount": "120000"}, confidence,
                [Citation("00:01:44", "Dana Whitfield", "We have roughly $120k budgeted.")],
                "budget_amount", ["test"])


def test_gate_uses_per_kind_thresholds(config: Config) -> None:
    assert gate(_fact(confidence=0.79), config).gate == "review"  # budget threshold is 0.80
    assert gate(_fact(confidence=0.80), config).gate == "auto"
    assert gate(_fact(kind="contact", confidence=0.70), config).gate == "auto"  # contact is 0.70
    assert gate(_fact(kind="risk", confidence=0.74), config).threshold == 0.75


def test_enqueue_is_idempotent_and_persists(tmp_path: Path) -> None:
    path = tmp_path / "q.jsonl"
    q = ReviewQueue(path)
    assert q.enqueue(_fact(), LOW_CONFIDENCE, "low")
    assert not q.enqueue(_fact(), LOW_CONFIDENCE, "low")
    q.save()
    reloaded = ReviewQueue(path)
    assert len(reloaded.pending()) == 1
    entry = reloaded.pending()[0]
    assert entry.fact.citations[0].quote == "We have roughly $120k budgeted."
    assert path.read_text(encoding="utf-8").count("\n") == 1


def test_approve_and_reject_by_prefix(tmp_path: Path) -> None:
    q = ReviewQueue(tmp_path / "q.jsonl")
    q.enqueue(_fact("aaa111"), LOW_CONFIDENCE, "low")
    q.enqueue(_fact("aab222"), LOW_CONFIDENCE, "low")
    with pytest.raises(QueueError, match="ambiguous"):
        q.decide("aa", APPROVED, "yash")
    with pytest.raises(QueueError, match="no queued fact"):
        q.decide("zzz", APPROVED, "yash")
    approved = q.decide("aaa", APPROVED, "yash", "confirmed by email")
    rejected = q.decide("aab", REJECTED, "yash")
    assert (approved.status, approved.decided_by, approved.note) == (APPROVED, "yash", "confirmed by email")
    assert rejected.status == REJECTED
    assert q.pending() == []
    with pytest.raises(QueueError, match="already approved"):
        q.decide("aaa", REJECTED, "yash")


def test_decisions_survive_re_extraction(tmp_path: Path) -> None:
    q = ReviewQueue(tmp_path / "q.jsonl")
    q.enqueue(_fact(), LOW_CONFIDENCE, "low")
    q.decide("abc", REJECTED, "yash")
    assert not q.enqueue(_fact(), LOW_CONFIDENCE, "low")  # a re-run never resurrects it
    assert not q.enqueue(_fact(), DESTRUCTIVE, "overwrite")
    assert q.get("abc123def456") is not None
    assert q.get("abc123def456").status == REJECTED  # type: ignore[union-attr]


def test_low_confidence_approval_does_not_authorize_overwrite(tmp_path: Path) -> None:
    q = ReviewQueue(tmp_path / "q.jsonl")
    q.enqueue(_fact(), LOW_CONFIDENCE, "low")
    q.decide("abc", APPROVED, "yash")
    assert q.enqueue(_fact(), DESTRUCTIVE, "overwrites amount")
    entry = q.get("abc123def456")
    assert entry is not None
    assert (entry.status, entry.review_type, entry.decided_by) == (PENDING, DESTRUCTIVE, "")
