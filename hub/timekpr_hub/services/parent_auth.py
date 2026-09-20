"""Parent authentication: password hashing and session tokens.

Password + session cookie -- no second factor. Sessions mirror the
device-token pattern in `api/auth.py` deliberately, rather than inventing a
second scheme: a random token handed to the client, only its sha256 stored
server-side, revocable by deleting the row.
"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from datetime import UTC, datetime, timedelta

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from timekpr_hub.db.models import Parent, ParentInvite, ParentSession

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


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


async def create_session(
    session: AsyncSession, *, parent_id: uuid.UUID, ip: str | None, user_agent: str | None
) -> str:
    """Returns the raw cookie value -- only its hash is ever persisted, same
    as a device's bearer token (api/auth.py)."""
    raw_token = secrets.token_urlsafe(32)
    now = datetime.now(UTC)
    session.add(
        ParentSession(
            id=uuid.uuid4(),
            parent_id=parent_id,
            token_hash=_hash_token(raw_token),
            expires_at=now + SESSION_TTL,
            ip=ip,
            user_agent=user_agent,
        )
    )
    return raw_token


async def get_parent_by_session_token(session: AsyncSession, raw_token: str) -> Parent | None:
    token_hash = _hash_token(raw_token)
    result = await session.execute(select(ParentSession).where(ParentSession.token_hash == token_hash))
    parent_session = result.scalar_one_or_none()
    if parent_session is None:
        return None
    if parent_session.expires_at < datetime.now(UTC):
        return None
    parent_result = await session.execute(select(Parent).where(Parent.id == parent_session.parent_id))
    return parent_result.scalar_one_or_none()


async def delete_session(session: AsyncSession, raw_token: str) -> None:
    token_hash = _hash_token(raw_token)
    result = await session.execute(select(ParentSession).where(ParentSession.token_hash == token_hash))
    parent_session = result.scalar_one_or_none()
    if parent_session is not None:
        await session.delete(parent_session)


async def any_parent_exists(session: AsyncSession) -> bool:
    result = await session.execute(select(Parent.id).limit(1))
    return result.first() is not None


async def count_parents(session: AsyncSession) -> int:
    result = await session.execute(select(func.count()).select_from(Parent))
    return int(result.scalar_one())


async def create_invite(session: AsyncSession, *, created_by_parent_id: uuid.UUID) -> str:
    """Returns the raw invite token for the URL -- mirrors
    api/enroll.py's enrollment codes: single-use, expiring, and minted by
    someone already authenticated (here, any existing parent)."""
    raw_token = secrets.token_urlsafe(24)
    now = datetime.now(UTC)
    session.add(
        ParentInvite(
            token=raw_token,
            expires_at=now + INVITE_TTL,
            created_by_parent_id=created_by_parent_id,
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
        update(ParentInvite)
        .where(
            ParentInvite.token == token,
            ParentInvite.used_at.is_(None),
            ParentInvite.expires_at >= now,
        )
        .values(used_at=now)
        .returning(ParentInvite.token)
    )
    if claim.scalar_one_or_none() is not None:
        return
    existing = await session.execute(select(ParentInvite).where(ParentInvite.token == token))
    row = existing.scalar_one_or_none()
    if row is None:
        raise InviteError("unknown or already-used invite link")
    if row.used_at is not None:
        raise InviteError("this invite link has already been used")
    raise InviteError("this invite link has expired")


def change_password(*, parent: Parent, new_password: str) -> None:
    """Re-hashes in place; call `delete_other_sessions` alongside this so a
    stolen or forgotten-open session elsewhere doesn't survive the change."""
    parent.password_hash = hash_password(new_password)


async def delete_other_sessions(session: AsyncSession, *, parent_id: uuid.UUID, keep_token: str) -> None:
    keep_hash = _hash_token(keep_token)
    result = await session.execute(
        select(ParentSession).where(
            ParentSession.parent_id == parent_id, ParentSession.token_hash != keep_hash
        )
    )
    for row in result.scalars().all():
        await session.delete(row)
