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
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from timekpr_hub.db.models import Parent, ParentSession

SESSION_COOKIE_NAME = "tkh_session"
SESSION_TTL = timedelta(days=30)

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
