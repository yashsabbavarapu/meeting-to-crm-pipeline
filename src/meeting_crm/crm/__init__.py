"""CRM adapters. ``CRMAdapter`` is the interface; ``SQLiteCRM`` is the reference implementation."""
from meeting_crm.crm.base import AuditEntry, Contact, CRMAdapter
from meeting_crm.crm.sqlite import SQLiteCRM

__all__ = ["AuditEntry", "CRMAdapter", "Contact", "SQLiteCRM"]
