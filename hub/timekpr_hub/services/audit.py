"""Audit log: an append-only record of every admin action, written here
and read back by api/admin/devices.py's GET /audit (the hub UI's Audit
page)."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
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


async def list_audit_events(
    session: AsyncSession,
    *,
    limit: int = 50,
    offset: int = 0,
    actor_id: str | None = None,
    target_type: str | None = None,
    target_id: str | None = None,
) -> list[AuditLog]:
    """Newest first, optionally narrowed to one actor and/or one target
    (e.g. one device or one user) -- the hub is household-scale, so plain
    OFFSET pagination is simple and correct rather than needing a keyset
    cursor."""
    stmt = select(AuditLog).order_by(AuditLog.ts.desc()).limit(limit).offset(offset)
    if actor_id is not None:
        stmt = stmt.where(AuditLog.actor_id == actor_id)
    if target_type is not None:
        stmt = stmt.where(AuditLog.target_type == target_type)
    if target_id is not None:
        stmt = stmt.where(AuditLog.target_id == target_id)
    result = await session.execute(stmt)
    return list(result.scalars().all())
