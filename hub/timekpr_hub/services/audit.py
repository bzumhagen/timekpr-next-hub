"""Audit log writes -- Phase 2 (CHECKLIST.md "alerts, audit_log tables
fully wired"). `AuditLog` has existed since Phase 1's initial schema but
nothing ever wrote to it (docs/best-practices-review.md) until now.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from timekpr_hub.db.models import AuditLog


async def record_audit_event(
    session: AsyncSession,
    *,
    actor_type: str,
    actor_id: str | None,
    action: str,
    target_type: str | None = None,
    target_id: str | None = None,
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
    ip: str | None = None,
) -> None:
    """Adds one row to the session -- does not commit. Every call site here
    already commits its own change in the same request/transaction, so the
    audit entry lands atomically with the change it describes rather than
    risking one succeeding without the other."""
    session.add(
        AuditLog(
            id=uuid.uuid4(),
            actor_type=actor_type,
            actor_id=actor_id,
            action=action,
            target_type=target_type,
            target_id=target_id,
            before_json=before,
            after_json=after,
            ip=ip,
        )
    )
