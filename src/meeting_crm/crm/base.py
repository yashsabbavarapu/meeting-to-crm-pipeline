"""The CRM adapter interface. Sync logic talks only to this, never to a concrete CRM.

A Salesforce or HubSpot adapter implements the same methods against their APIs;
``SQLiteCRM`` is the reference implementation used for the demo and tests.
Adapters are deliberately dumb: read state, write a field, record an audit row.
All policy (gating, overwrite protection, idempotency decisions) lives in sync.py
so it behaves identically across CRMs.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class AuditEntry:
    actor: str
    action: str  # created | applied | noop | superseded | queued | approved | rejected
    entity: str  # account | deal | contact | activity | fact
    entity_id: str = ""
    field: str = ""
    old_value: str = ""
    new_value: str = ""
    fact_id: str = ""
    transcript_id: str = ""
    quote: str = ""
    quote_timestamp: str = ""
    speaker: str = ""
    detail: str = ""


@dataclass(frozen=True)
class Contact:
    contact_id: str
    name: str
    role: str
    email: str


class CRMAdapter(ABC):
    @abstractmethod
    def transaction(self) -> AbstractContextManager[None]:
        """Group writes so a fact's changes and its audit rows land together or not at all."""

    @abstractmethod
    def ensure_account(self, name: str) -> tuple[str, bool]:
        """Return (account_id, created)."""

    @abstractmethod
    def ensure_deal(self, account_id: str, name: str, initial_stage: str) -> tuple[str, bool]:
        """Return (deal_id, created)."""

    @abstractmethod
    def deal_fields(self) -> frozenset[str]:
        """Writable deal fields. field_map targets must be in this set."""

    @abstractmethod
    def get_deal(self, deal_id: str) -> dict[str, str]:
        """Current deal field values, '' for empty."""

    @abstractmethod
    def set_deal_field(self, deal_id: str, field: str, value: str) -> None: ...

    @abstractmethod
    def find_contact(self, account_id: str, name: str) -> Contact | None: ...

    @abstractmethod
    def create_contact(self, account_id: str, name: str, role: str, email: str) -> str: ...

    @abstractmethod
    def set_contact_field(self, contact_id: str, field: str, value: str) -> None: ...

    @abstractmethod
    def find_activity(self, source_key: str) -> str | None:
        """Activities are keyed by their source (fact id or meeting id) for idempotency."""

    @abstractmethod
    def create_activity(
        self,
        account_id: str,
        deal_id: str,
        kind: str,
        subject: str,
        owner: str,
        due_date: str,
        source_key: str,
        citation: str,
    ) -> str: ...

    @abstractmethod
    def is_applied(self, fact_id: str) -> bool: ...

    @abstractmethod
    def mark_applied(self, fact_id: str, transcript_id: str) -> None: ...

    @abstractmethod
    def record_audit(self, entry: AuditEntry) -> None: ...

    @abstractmethod
    def last_write_source(self, entity: str, entity_id: str, field: str) -> str | None:
        """Transcript id behind the most recent applied write to a field (field provenance)."""

    @abstractmethod
    def audit_log(self, limit: int | None = None) -> list[dict[str, Any]]: ...

    @abstractmethod
    def snapshot(self) -> dict[str, list[dict[str, Any]]]:
        """Full CRM state for display: accounts, contacts, deals, activities."""

    @abstractmethod
    def close(self) -> None: ...
