"""SQLite reference CRM: accounts, contacts, deals, activities, plus an audit table.

Uniqueness constraints back up the idempotency logic in sync.py: even a buggy
caller cannot create a second contact with the same name on an account or a
second activity from the same source.
"""
from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from meeting_crm.crm.base import AuditEntry, Contact, CRMAdapter

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE COLLATE NOCASE,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS contacts (
    id INTEGER PRIMARY KEY,
    account_id INTEGER NOT NULL REFERENCES accounts(id),
    name TEXT NOT NULL COLLATE NOCASE,
    role TEXT NOT NULL DEFAULT '',
    email TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (account_id, name)
);
CREATE TABLE IF NOT EXISTS deals (
    id INTEGER PRIMARY KEY,
    account_id INTEGER NOT NULL REFERENCES accounts(id),
    name TEXT NOT NULL COLLATE NOCASE,
    stage TEXT NOT NULL DEFAULT '',
    amount TEXT NOT NULL DEFAULT '',
    close_target TEXT NOT NULL DEFAULT '',
    competitors TEXT NOT NULL DEFAULT '',
    risks TEXT NOT NULL DEFAULT '',
    next_step TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (account_id, name)
);
CREATE TABLE IF NOT EXISTS activities (
    id INTEGER PRIMARY KEY,
    account_id INTEGER NOT NULL REFERENCES accounts(id),
    deal_id INTEGER REFERENCES deals(id),
    kind TEXT NOT NULL,
    subject TEXT NOT NULL,
    owner TEXT NOT NULL DEFAULT '',
    due_date TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'open',
    source_key TEXT NOT NULL UNIQUE,
    citation TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS applied_facts (
    fact_id TEXT PRIMARY KEY,
    transcript_id TEXT NOT NULL,
    applied_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY,
    at TEXT NOT NULL,
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    entity TEXT NOT NULL,
    entity_id TEXT NOT NULL DEFAULT '',
    field TEXT NOT NULL DEFAULT '',
    old_value TEXT NOT NULL DEFAULT '',
    new_value TEXT NOT NULL DEFAULT '',
    fact_id TEXT NOT NULL DEFAULT '',
    transcript_id TEXT NOT NULL DEFAULT '',
    quote TEXT NOT NULL DEFAULT '',
    quote_timestamp TEXT NOT NULL DEFAULT '',
    speaker TEXT NOT NULL DEFAULT '',
    detail TEXT NOT NULL DEFAULT ''
);
"""

DEAL_FIELDS = frozenset({"stage", "amount", "close_target", "competitors", "risks", "next_step"})
CONTACT_FIELDS = frozenset({"role", "email"})


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class SQLiteCRM(CRMAdapter):
    def __init__(self, path: Path | str) -> None:
        if isinstance(path, Path):
            path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path), isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(SCHEMA)
        self._depth = 0

    @contextmanager
    def _tx(self) -> Iterator[None]:
        if self._depth:
            self._depth += 1
            try:
                yield
            finally:
                self._depth -= 1
            return
        self.conn.execute("BEGIN IMMEDIATE")
        self._depth = 1
        try:
            yield
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        else:
            self.conn.execute("COMMIT")
        finally:
            self._depth = 0

    def transaction(self) -> AbstractContextManager[None]:
        return self._tx()

    def ensure_account(self, name: str) -> tuple[str, bool]:
        row = self.conn.execute("SELECT id FROM accounts WHERE name = ?", (name,)).fetchone()
        if row:
            return str(row["id"]), False
        cur = self.conn.execute(
            "INSERT INTO accounts (name, created_at) VALUES (?, ?)", (name, _now())
        )
        return str(cur.lastrowid), True

    def ensure_deal(self, account_id: str, name: str, initial_stage: str) -> tuple[str, bool]:
        row = self.conn.execute(
            "SELECT id FROM deals WHERE account_id = ? AND name = ?", (int(account_id), name)
        ).fetchone()
        if row:
            return str(row["id"]), False
        now = _now()
        cur = self.conn.execute(
            "INSERT INTO deals (account_id, name, stage, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
            (int(account_id), name, initial_stage, now, now),
        )
        return str(cur.lastrowid), True

    def deal_fields(self) -> frozenset[str]:
        return DEAL_FIELDS

    def get_deal(self, deal_id: str) -> dict[str, str]:
        row = self.conn.execute("SELECT * FROM deals WHERE id = ?", (int(deal_id),)).fetchone()
        if row is None:
            raise KeyError(f"deal {deal_id} not found")
        return {k: str(row[k]) for k in row.keys()}

    def set_deal_field(self, deal_id: str, field: str, value: str) -> None:
        if field not in DEAL_FIELDS:  # whitelist: field names are interpolated into SQL
            raise ValueError(f"unknown deal field {field!r}")
        self.conn.execute(
            f"UPDATE deals SET {field} = ?, updated_at = ? WHERE id = ?", (value, _now(), int(deal_id))
        )

    def find_contact(self, account_id: str, name: str) -> Contact | None:
        row = self.conn.execute(
            "SELECT id, name, role, email FROM contacts WHERE account_id = ? AND name = ?",
            (int(account_id), name),
        ).fetchone()
        if row is None:
            return None
        return Contact(str(row["id"]), row["name"], row["role"], row["email"])

    def create_contact(self, account_id: str, name: str, role: str, email: str) -> str:
        now = _now()
        cur = self.conn.execute(
            "INSERT INTO contacts (account_id, name, role, email, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (int(account_id), name, role, email, now, now),
        )
        return str(cur.lastrowid)

    def set_contact_field(self, contact_id: str, field: str, value: str) -> None:
        if field not in CONTACT_FIELDS:
            raise ValueError(f"unknown contact field {field!r}")
        self.conn.execute(
            f"UPDATE contacts SET {field} = ?, updated_at = ? WHERE id = ?",
            (value, _now(), int(contact_id)),
        )

    def find_activity(self, source_key: str) -> str | None:
        row = self.conn.execute(
            "SELECT id FROM activities WHERE source_key = ?", (source_key,)
        ).fetchone()
        return str(row["id"]) if row else None

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
    ) -> str:
        cur = self.conn.execute(
            "INSERT INTO activities (account_id, deal_id, kind, subject, owner, due_date, "
            "source_key, citation, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (int(account_id), int(deal_id), kind, subject, owner, due_date, source_key, citation, _now()),
        )
        return str(cur.lastrowid)

    def is_applied(self, fact_id: str) -> bool:
        row = self.conn.execute("SELECT 1 FROM applied_facts WHERE fact_id = ?", (fact_id,)).fetchone()
        return row is not None

    def mark_applied(self, fact_id: str, transcript_id: str) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO applied_facts (fact_id, transcript_id, applied_at) VALUES (?, ?, ?)",
            (fact_id, transcript_id, _now()),
        )

    def record_audit(self, entry: AuditEntry) -> None:
        self.conn.execute(
            "INSERT INTO audit_log (at, actor, action, entity, entity_id, field, old_value, new_value, "
            "fact_id, transcript_id, quote, quote_timestamp, speaker, detail) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                _now(), entry.actor, entry.action, entry.entity, entry.entity_id, entry.field,
                entry.old_value, entry.new_value, entry.fact_id, entry.transcript_id, entry.quote,
                entry.quote_timestamp, entry.speaker, entry.detail,
            ),
        )

    def last_write_source(self, entity: str, entity_id: str, field: str) -> str | None:
        row = self.conn.execute(
            "SELECT transcript_id FROM audit_log WHERE action = 'applied' AND entity = ? "
            "AND entity_id = ? AND field = ? ORDER BY id DESC LIMIT 1",
            (entity, entity_id, field),
        ).fetchone()
        return str(row["transcript_id"]) if row else None

    def audit_log(self, limit: int | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM audit_log ORDER BY id"
        rows = self.conn.execute(sql).fetchall()
        if limit is not None:
            rows = rows[-limit:]
        return [dict(r) for r in rows]

    def snapshot(self) -> dict[str, list[dict[str, Any]]]:
        def rows(sql: str) -> list[dict[str, Any]]:
            return [dict(r) for r in self.conn.execute(sql).fetchall()]

        return {
            "accounts": rows("SELECT id, name FROM accounts ORDER BY id"),
            "contacts": rows(
                "SELECT c.id, a.name AS account, c.name, c.role, c.email FROM contacts c "
                "JOIN accounts a ON a.id = c.account_id ORDER BY c.id"
            ),
            "deals": rows(
                "SELECT d.id, a.name AS account, d.name, d.stage, d.amount, d.close_target, "
                "d.competitors, d.risks, d.next_step FROM deals d "
                "JOIN accounts a ON a.id = d.account_id ORDER BY d.id"
            ),
            "activities": rows(
                "SELECT id, kind, subject, owner, due_date, status, source_key FROM activities ORDER BY id"
            ),
        }

    def close(self) -> None:
        self.conn.close()
