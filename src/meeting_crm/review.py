"""Confidence gating and the human-in-the-loop review queue (a JSONL file).

Two things send a fact to a human:
- ``low_confidence``: its rule-based score is below the per-kind threshold (decided at extract).
- ``destructive``: applying it would overwrite existing CRM data or touch a protected
  deal (decided at sync, because only then is current CRM state known).

Approval is scoped to the reason it was given for. A low-confidence approval does
not authorize an overwrite: if the approved fact turns out to be destructive at
sync time, it goes back to pending with the new reason.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from meeting_crm.config import Config
from meeting_crm.models import Fact
from meeting_crm.workspace import GatedFact, atomic_write

PENDING, APPROVED, REJECTED, SUPERSEDED = "pending", "approved", "rejected", "superseded"
LOW_CONFIDENCE, DESTRUCTIVE = "low_confidence", "destructive"


def gate(fact: Fact, config: Config) -> GatedFact:
    threshold = config.threshold_for(fact.kind)
    return GatedFact(fact, "auto" if fact.confidence >= threshold else "review", threshold)


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class QueueError(LookupError):
    pass


@dataclass
class QueueEntry:
    fact: Fact
    status: str
    review_type: str
    reason: str
    queued_at: str
    decided_at: str = ""
    decided_by: str = ""
    note: str = ""

    @property
    def fact_id(self) -> str:
        return self.fact.fact_id

    def to_dict(self) -> dict[str, Any]:
        return {
            "fact_id": self.fact_id,
            "transcript_id": self.fact.transcript_id,
            "status": self.status,
            "review_type": self.review_type,
            "reason": self.reason,
            "queued_at": self.queued_at,
            "decided_at": self.decided_at,
            "decided_by": self.decided_by,
            "note": self.note,
            "fact": self.fact.to_dict(),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> QueueEntry:
        return cls(
            fact=Fact.from_dict(data["fact"]),
            status=str(data["status"]),
            review_type=str(data["review_type"]),
            reason=str(data["reason"]),
            queued_at=str(data["queued_at"]),
            decided_at=str(data.get("decided_at", "")),
            decided_by=str(data.get("decided_by", "")),
            note=str(data.get("note", "")),
        )


class ReviewQueue:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.entries: dict[str, QueueEntry] = {}
        if path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    entry = QueueEntry.from_dict(json.loads(line))
                    self.entries[entry.fact_id] = entry

    def save(self) -> None:
        atomic_write(self.path, "".join(json.dumps(e.to_dict()) + "\n" for e in self.entries.values()))

    def get(self, fact_id: str) -> QueueEntry | None:
        return self.entries.get(fact_id)

    def enqueue(self, fact: Fact, review_type: str, reason: str) -> bool:
        """Add a fact for review. Idempotent: returns False if nothing changed.

        A fact already decided for the same review type keeps its decision, so
        re-running extract never resurrects something a human already handled.
        """
        existing = self.entries.get(fact.fact_id)
        if existing is None:
            self.entries[fact.fact_id] = QueueEntry(fact, PENDING, review_type, reason, now_iso())
            return True
        if existing.status == REJECTED:
            return False
        if review_type == DESTRUCTIVE and existing.review_type == LOW_CONFIDENCE:
            # Escalate: approving a low-confidence fact never authorizes an overwrite.
            existing.review_type = DESTRUCTIVE
            existing.reason = reason
            existing.status = PENDING
            existing.decided_at = existing.decided_by = existing.note = ""
            return True
        if existing.status == PENDING:
            existing.fact = fact  # refresh the evidence in case rules changed
        return False

    def resolve(self, prefix: str) -> QueueEntry:
        matches = [e for fid, e in self.entries.items() if fid.startswith(prefix)]
        if not matches:
            raise QueueError(f"no queued fact matches {prefix!r}")
        if len(matches) > 1:
            raise QueueError(f"{prefix!r} is ambiguous ({len(matches)} matches), use more characters")
        return matches[0]

    def decide(self, prefix: str, status: str, actor: str, note: str = "") -> QueueEntry:
        if status not in (APPROVED, REJECTED):
            raise ValueError(f"invalid decision {status!r}")
        entry = self.resolve(prefix)
        if entry.status != PENDING:
            raise QueueError(f"{entry.fact_id} is already {entry.status}")
        entry.status = status
        entry.decided_at = now_iso()
        entry.decided_by = actor
        entry.note = note
        return entry

    def supersede(self, fact_id: str, reason: str) -> None:
        """Close a pending entry because a newer call made it moot. Never deletes history."""
        entry = self.entries.get(fact_id)
        if entry is not None and entry.status == PENDING:
            entry.status = SUPERSEDED
            entry.decided_at = now_iso()
            entry.decided_by = "pipeline"
            entry.note = reason

    def pending(self) -> list[QueueEntry]:
        return [e for e in self.entries.values() if e.status == PENDING]

    def all(self) -> list[QueueEntry]:
        return list(self.entries.values())
