"""Admin authentication: password hashing and session tokens.

Password + session cookie -- no second factor. Sessions mirror the
device-token pattern in `api/auth.py` deliberately, rather than inventing a
second scheme: a random token handed to the client, only its sha256 stored
server-side, revocable by deleting the row.
"""

from __future__ import annotations

import secrets
import uuid
from datetime import UTC, datetime, timedelta

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from fastapi.concurrency import run_in_threadpool
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from timekpr_hub.db.models import Admin, AdminInvite, AdminSession
from timekpr_hub.services.audit import record_audit_event
from timekpr_hub.services.tokens import hash_token

SESSION_COOKIE_NAME = "tkh_session"
SESSION_TTL = timedelta(days=30)
INVITE_TTL = timedelta(hours=24)

_hasher = PasswordHasher()


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return _hasher.verify(password_hash, password)
    except VerifyMismatchError:
        return False


async def create_session(
    session: AsyncSession, *, admin_id: uuid.UUID, ip: str | None, user_agent: str | None
) -> str:
    """Returns the raw cookie value -- only its hash is ever persisted, same
    as a device's bearer token (api/auth.py)."""
    raw_token = secrets.token_urlsafe(32)
    now = datetime.now(UTC)
    session.add(
        AdminSession(
            id=uuid.uuid4(),
            admin_id=admin_id,
            token_hash=hash_token(raw_token),
            expires_at=now + SESSION_TTL,
            ip=ip,
            user_agent=user_agent,
        )
    )
    return raw_token


async def get_admin_by_session_token(session: AsyncSession, raw_token: str) -> Admin | None:
    token_hash = hash_token(raw_token)
    result = await session.execute(select(AdminSession).where(AdminSession.token_hash == token_hash))
    admin_session = result.scalar_one_or_none()
    if admin_session is None:
        return None
    if admin_session.expires_at < datetime.now(UTC):
        return None
    admin_result = await session.execute(select(Admin).where(Admin.id == admin_session.admin_id))
    return admin_result.scalar_one_or_none()


async def delete_session(session: AsyncSession, raw_token: str) -> None:
    token_hash = hash_token(raw_token)
    result = await session.execute(select(AdminSession).where(AdminSession.token_hash == token_hash))
    admin_session = result.scalar_one_or_none()
    if admin_session is not None:
        await session.delete(admin_session)


async def any_admin_exists(session: AsyncSession) -> bool:
    result = await session.execute(select(Admin.id).limit(1))
    return result.first() is not None


async def count_admins(session: AsyncSession) -> int:
    result = await session.execute(select(func.count()).select_from(Admin))
    return int(result.scalar_one())


async def create_invite(session: AsyncSession, *, created_by_admin_id: uuid.UUID) -> str:
    """Returns the raw invite token for the URL -- mirrors
    api/enroll.py's enrollment codes: single-use, expiring, and minted by
    someone already authenticated (here, any existing admin)."""
    raw_token = secrets.token_urlsafe(24)
    now = datetime.now(UTC)
    session.add(
        AdminInvite(
            token=raw_token,
            expires_at=now + INVITE_TTL,
            created_by_admin_id=created_by_admin_id,
        )
    )
    return raw_token


class InviteError(RuntimeError):
    """Raised by `redeem_invite` with a message safe to show directly on
    the invite page -- unknown/used/expired, the same three outcomes
    api/enroll.py's enrollment codes have."""


async def redeem_invite(session: AsyncSession, *, token: str) -> None:
    """Atomically claims the invite (`used_at IS NULL AND not expired`, in
    one UPDATE) the same way api/enroll.py claims an enrollment code, so two
    concurrent submissions of the same invite link can't both succeed."""
    now = datetime.now(UTC)
    claim = await session.execute(
        update(AdminInvite)
        .where(
            AdminInvite.token == token,
            AdminInvite.used_at.is_(None),
            AdminInvite.expires_at >= now,
        )
        .values(used_at=now)
        .returning(AdminInvite.token)
    )
    if claim.scalar_one_or_none() is not None:
        return
    existing = await session.execute(select(AdminInvite).where(AdminInvite.token == token))
    row = existing.scalar_one_or_none()
    if row is None:
        raise InviteError("unknown or already-used invite link")
    if row.used_at is not None:
        raise InviteError("this invite link has already been used")
    raise InviteError("this invite link has expired")


def change_password(*, admin: Admin, new_password: str) -> None:
    """Re-hashes in place; call `delete_other_sessions` alongside this so a
    stolen or forgotten-open session elsewhere doesn't survive the change."""
    admin.password_hash = hash_password(new_password)


async def delete_other_sessions(session: AsyncSession, *, admin_id: uuid.UUID, keep_token: str) -> None:
    keep_hash = hash_token(keep_token)
    result = await session.execute(
        select(AdminSession).where(AdminSession.admin_id == admin_id, AdminSession.token_hash != keep_hash)
    )
    for row in result.scalars().all():
        await session.delete(row)


async def mint_admin_invite(session: AsyncSession, *, actor_admin_id: uuid.UUID, ip: str | None) -> str:
    """create_invite plus its audit event -- shared by the JSON admin API
    and the hub UI (api/admin/admins.py, api/ui/admins.py). Returns the raw
    token; building the invite URL is left to the caller, which has the
    request's own base_url."""
    token = await create_invite(session, created_by_admin_id=actor_admin_id)
    await record_audit_event(
        session, actor_type="admin", actor_id=str(actor_admin_id), action="admin.invite_created", ip=ip
    )
    return token


class LastAdminError(RuntimeError):
    """Raised by `delete_admin_account` instead of deleting the last
    remaining admin -- that would permanently lock the hub's own admin UI
    (there is no other way back in; /setup only ever fires once)."""


async def delete_admin_account(
    session: AsyncSession, *, target_admin_id: uuid.UUID, actor_admin_id: uuid.UUID, ip: str | None
) -> Admin | None:
    """Returns None (nothing to do) if target_admin_id doesn't exist;
    raises LastAdminError instead of ever deleting the last one, including
    an admin deleting themselves."""
    if await count_admins(session) <= 1:
        raise LastAdminError("cannot delete the last remaining admin account")
    result = await session.execute(select(Admin).where(Admin.id == target_admin_id))
    target = result.scalar_one_or_none()
    if target is None:
        return None
    await record_audit_event(
        session,
        actor_type="admin",
        actor_id=str(actor_admin_id),
        action="admin.deleted",
        target_type="admin",
        target_id=str(target.id),
        before={"email": target.email},
        ip=ip,
    )
    await session.delete(target)
    return target


class WrongPasswordError(RuntimeError):
    """Raised by `change_admin_own_password` when `current_password`
    doesn't match."""


async def change_admin_own_password(
    session: AsyncSession,
    *,
    admin: Admin,
    current_password: str,
    new_password: str,
    session_token: str,
    ip: str | None,
) -> None:
    """Verifies `current_password`, re-hashes in place, signs every other
    session for this admin out (a stolen or forgotten-open session
    elsewhere must not survive the change), and records the audit event.
    The hash/verify calls run off the event loop -- this is on the
    authenticated "change my own password" path, but still worth ~50-100ms
    of argon2 work not blocking other requests."""
    if not await run_in_threadpool(verify_password, current_password, admin.password_hash):
        raise WrongPasswordError("current password is incorrect")
    await run_in_threadpool(change_password, admin=admin, new_password=new_password)
    await delete_other_sessions(session, admin_id=admin.id, keep_token=session_token)
    await record_audit_event(
        session,
        actor_type="admin",
        actor_id=str(admin.id),
        action="admin.password_changed",
        target_type="admin",
        target_id=str(admin.id),
        ip=ip,
    )
