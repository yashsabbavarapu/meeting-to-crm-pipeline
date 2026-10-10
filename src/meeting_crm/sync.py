"""Apply gated facts to a CRM through the adapter interface.

Guarantees, in order of importance:
1. Nothing destructive happens without a human. Overwriting a non-empty field
   with a different value, moving a deal backwards, or touching a deal in a
   protected stage (closed_won, closed_lost) sends the fact to the review queue.
2. Idempotent. A fact is applied at most once (tracked by fact id), and each
   write is also a no-op when the CRM already holds the value.
3. Every proposed change is audited with its outcome and its transcript citation.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date

from meeting_crm.config import Config
from meeting_crm.crm.base import AuditEntry, CRMAdapter
from meeting_crm.models import Fact, Transcript
from meeting_crm.review import APPROVED, DESTRUCTIVE, REJECTED, ReviewQueue
from meeting_crm.workspace import GatedFact

LIST_KINDS = frozenset({"competitor", "risk"})


@dataclass
class Change:
    entity: str
    entity_id: str
    field: str
    old: str
    new: str
    apply: Callable[[], str] | None  # returns the entity id it wrote; None means no-op
    destructive: str = ""  # non-empty reason when this change needs human approval
    superseded: str = ""  # non-empty when a newer call already wrote this field


@dataclass
class SyncResult:
    applied: list[str] = field(default_factory=list)
    already_applied: int = 0
    awaiting_review: list[str] = field(default_factory=list)
    queued_destructive: list[str] = field(default_factory=list)
    rejected: int = 0
    superseded: int = 0
    writes: int = 0


def _split(value: str) -> list[str]:
    return [v for v in value.split(";") if v]


class Syncer:
    def __init__(
        self,
        crm: CRMAdapter,
        config: Config,
        queue: ReviewQueue,
        call_dates: dict[str, date] | None = None,
    ) -> None:
        self.crm = crm
        self.config = config
        self.queue = queue
        self.call_dates = call_dates or {}
        missing = {v for v in config.field_map.values()} - crm.deal_fields()
        if missing:
            raise ValueError(f"field_map targets not supported by this CRM: {sorted(missing)}")

    # Planning -----------------------------------------------------------

    def _newer_source(self, entity: str, entity_id: str, field_name: str, fact: Fact) -> str:
        """Name the newer call that already wrote this field, or '' if the fact is current.

        Late approvals are common (a reviewer clears last week's queue today). Without
        this check, approving an old fact would silently regress a newer CRM value.
        """
        source = self.crm.last_write_source(entity, entity_id, field_name)
        if not source or source == fact.transcript_id:
            return ""
        written, ours = self.call_dates.get(source), self.call_dates.get(fact.transcript_id)
        if written and ours and written > ours:
            return f"{field_name} was set by a newer call ({source}, {written.isoformat()})"
        return ""

    def _deal_change(self, deal_id: str, deal: dict[str, str], fact: Fact) -> list[Change]:
        target = self.config.field_map.get(fact.kind)
        if target is None:
            return []
        old = deal.get(target, "")
        if fact.kind in LIST_KINDS:
            item = fact.fields["name" if fact.kind == "competitor" else "category"]
            items = _split(old)
            if item.lower() in (i.lower() for i in items):
                return [Change("deal", deal_id, target, old, old, None)]
            new = ";".join(items + [item])  # multi-select picklist style, like Salesforce
        elif fact.kind == "budget":
            new = fact.fields["amount"]
        elif fact.kind == "timeline":
            new = fact.fields["target"]
        elif fact.kind == "next_step":
            date_part = fact.fields.get("date", "")
            new = f"{date_part}: {fact.fields['description']}" if date_part else fact.fields["description"]
        elif fact.kind == "stage":
            new = fact.fields["stage"]
        else:
            return []

        if old == new:
            return [Change("deal", deal_id, target, old, new, None)]

        reason = ""
        current_stage = deal.get("stage", "")
        if current_stage in self.config.protected_stages:
            reason = f"deal is {current_stage}, a protected stage"
        elif fact.kind == "stage":
            if self.config.stage_index(new) < self.config.stage_index(old):
                reason = f"moves the deal backwards from {old} to {new}"
        elif (
            old
            and fact.kind not in LIST_KINDS
            and self.config.review_overwrites
            and target not in self.config.freely_updated_fields
        ):
            reason = f"overwrites {target} {old!r} with {new!r}"

        def apply() -> str:
            self.crm.set_deal_field(deal_id, target, new)
            return deal_id

        stale = self._newer_source("deal", deal_id, target, fact) if old else ""
        return [Change("deal", deal_id, target, old, new, apply, reason, stale)]

    def _contact_changes(self, account_id: str, fact: Fact) -> list[Change]:
        name = fact.fields["name"]
        role, email = fact.fields.get("role", ""), fact.fields.get("email", "")
        existing = self.crm.find_contact(account_id, name)
        if existing is None:
            def create() -> str:
                return self.crm.create_contact(account_id, name, role, email)

            return [Change("contact", "", "name", "", name, create)]
        changes: list[Change] = []
        for fname, new in (("role", role), ("email", email)):
            old = getattr(existing, fname)
            if not new or old.lower() == new.lower():
                changes.append(Change("contact", existing.contact_id, fname, old, old, None))
                continue

            def update(cid: str = existing.contact_id, f: str = fname, v: str = new) -> str:
                self.crm.set_contact_field(cid, f, v)
                return cid

            reason = f"overwrites contact {fname} {old!r} with {new!r}" if old else ""
            stale = self._newer_source("contact", existing.contact_id, fname, fact) if old else ""
            changes.append(Change("contact", existing.contact_id, fname, old, new, update, reason, stale))
        return changes

    def _activity_change(self, account_id: str, deal_id: str, fact: Fact) -> list[Change]:
        if self.crm.find_activity(fact.fact_id):
            return [Change("activity", "", "task", "", "", None)]
        owner, task, due = fact.fields["owner"], fact.fields["task"], fact.fields.get("due", "")
        citation = f"{fact.transcript_id} {fact.citations[0].render()}"

        def create() -> str:
            return self.crm.create_activity(
                account_id, deal_id, "task", task, owner, due, fact.fact_id, citation
            )

        return [Change("activity", "", "task", "", f"{owner}: {task}", create)]

    def plan(self, account_id: str, deal_id: str, fact: Fact) -> list[Change]:
        if fact.kind == "contact":
            return self._contact_changes(account_id, fact)
        if fact.kind == "action_item":
            return self._activity_change(account_id, deal_id, fact)
        return self._deal_change(deal_id, self.crm.get_deal(deal_id), fact)

    # Execution ----------------------------------------------------------

    def _audit(self, action: str, actor: str, change: Change, fact: Fact, detail: str = "") -> None:
        c = fact.citations[0]
        self.crm.record_audit(AuditEntry(
            actor=actor, action=action, entity=change.entity, entity_id=change.entity_id,
            field=change.field, old_value=change.old, new_value=change.new, fact_id=fact.fact_id,
            transcript_id=fact.transcript_id, quote=c.quote, quote_timestamp=c.timestamp,
            speaker=c.speaker, detail=detail,
        ))

    def _ensure_containers(self, t: Transcript) -> tuple[str, str]:
        account_id, created = self.crm.ensure_account(t.account)
        if created:
            self.crm.record_audit(AuditEntry("pipeline", "created", "account", account_id,
                                             new_value=t.account, transcript_id=t.transcript_id))
        deal_id, created = self.crm.ensure_deal(account_id, t.deal, self.config.initial_stage)
        if created:
            self.crm.record_audit(AuditEntry("pipeline", "created", "deal", deal_id,
                                             new_value=t.deal, transcript_id=t.transcript_id))
        meeting_key = f"meeting:{t.transcript_id}"
        if not self.crm.find_activity(meeting_key):
            activity_id = self.crm.create_activity(
                account_id, deal_id, "meeting", t.title, "", t.call_date.isoformat(), meeting_key,
                t.source,
            )
            self.crm.record_audit(AuditEntry("pipeline", "created", "activity", activity_id,
                                             new_value=t.title, transcript_id=t.transcript_id))
        return account_id, deal_id

    def sync_transcript(self, t: Transcript, gated: list[GatedFact], result: SyncResult) -> None:
        with self.crm.transaction():
            account_id, deal_id = self._ensure_containers(t)
        for g in gated:
            fact = g.fact
            if self.crm.is_applied(fact.fact_id):
                result.already_applied += 1
                continue
            entry = self.queue.get(fact.fact_id)
            if entry is not None and entry.status == REJECTED:
                result.rejected += 1
                continue
            approved = entry is not None and entry.status == APPROVED
            if g.gate == "review" and not approved:
                result.awaiting_review.append(fact.fact_id)
                continue
            override = approved and entry is not None and entry.review_type == DESTRUCTIVE
            actor = f"approved:{entry.decided_by}" if approved and entry is not None else "pipeline"

            with self.crm.transaction():
                changes = self.plan(account_id, deal_id, fact)
                for c in changes:
                    if c.superseded and c.apply is not None:
                        self._audit("superseded", actor, c, fact, c.superseded)
                        c.apply = None
                if changes and all(c.superseded for c in changes):
                    self.queue.supersede(fact.fact_id, changes[0].superseded)
                    self.crm.mark_applied(fact.fact_id, fact.transcript_id)
                    result.superseded += 1
                    continue
                blocked = [c for c in changes if c.destructive and c.apply is not None]
                if blocked and not override:
                    reason = "; ".join(c.destructive for c in blocked)
                    if self.queue.enqueue(fact, DESTRUCTIVE, reason):
                        for c in blocked:
                            self._audit("queued", "pipeline", c, fact, reason)
                    result.queued_destructive.append(fact.fact_id)
                    continue
                for c in changes:
                    if c.superseded:
                        continue
                    if c.apply is None:
                        self._audit("noop", actor, c, fact, "CRM already holds this value")
                        continue
                    c.entity_id = c.apply()
                    self._audit("applied", actor, c, fact, c.destructive)
                    result.writes += 1
                self.crm.mark_applied(fact.fact_id, fact.transcript_id)
                result.applied.append(fact.fact_id)
