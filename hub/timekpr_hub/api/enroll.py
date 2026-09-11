"""POST /enroll -- PLAN "API": device enrollment via a one-time code.

No device auth on this endpoint (there's no token yet); the enrollment code
itself is the credential, single-use and short-lived.
"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from timekpr_hub_core.models import EnrollRequest, EnrollResponse

from timekpr_hub.db.models import Device, EnrollmentCode, User, UserAlias
from timekpr_hub.db.session import get_session
from timekpr_hub.services.policy import create_initial_policy, get_current_policy, policy_to_payload
from timekpr_hub.settings import settings

router = APIRouter()

TOKEN_PREFIX = "tkh_"


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


@router.post("/enroll", response_model=EnrollResponse, status_code=status.HTTP_201_CREATED)
async def enroll(req: EnrollRequest, session: AsyncSession = Depends(get_session)) -> EnrollResponse:
    now = datetime.now(UTC)

    result = await session.execute(select(EnrollmentCode).where(EnrollmentCode.code == req.enrollment_code))
    code_row = result.scalar_one_or_none()
    if code_row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown enrollment code")
    if code_row.used_at is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "enrollment code already used")
    if code_row.expires_at < now:
        raise HTTPException(status.HTTP_410_GONE, "enrollment code expired")

    raw_token = TOKEN_PREFIX + secrets.token_urlsafe(32)
    device = Device(
        id=uuid.uuid4(),
        name=req.hostname,
        hostname=req.hostname,
        machine_id=req.machine_id,
        token_hash=_hash_token(raw_token),
        token_prefix=raw_token[:12],
        # A parent-minted enrollment code is itself the approval -- there's
        # no separate authentication on either endpoint yet for a second
        # "approve" step to actually gate anything (docs/best-practices-
        # review.md), and it becomes properly meaningful once parent auth
        # lands: minting a code will require being logged in. The old
        # 'pending' default meant a brand-new device could already sync
        # (auth.py only rejected 'revoked'), so this also removes a step
        # that added friction without adding security. `approve_device`
        # (parent.py) and the UI button are kept for any device enrolled
        # before this change, or a future opt-in "require approval" mode.
        status="active",
        enforcement="enforce",
        agent_version=req.agent_version,
        os_info=req.os,
        tz=req.tz,
        enrolled_at=now,
    )
    session.add(device)
    await session.flush()

    new_users: list[str] = []
    policies: dict[str, object] = {}

    # Provision or merge into an existing canonical user per reported local
    # username (PLAN "Enrollment"): a fresh username gets a new User row, a
    # username matching one already known to the hub (e.g. this account also
    # exists on another device) just gets a new alias pointing at it.
    for local_username in req.local_users:
        # Savepoint so a unique-constraint race against a concurrent enroll
        # of the same brand-new username (two devices, first sync each)
        # falls back to "someone else just created it" instead of aborting
        # the whole enrollment.
        is_new = False
        try:
            async with session.begin_nested():
                user = User(id=uuid.uuid4(), canonical_username=local_username, display_name=local_username)
                session.add(user)
                await session.flush()
            is_new = True
        except IntegrityError:
            existing = await session.execute(select(User).where(User.canonical_username == local_username))
            user = existing.scalar_one()

        session.add(
            UserAlias(id=uuid.uuid4(), user_id=user.id, device_id=device.id, local_username=local_username)
        )

        if is_new:
            new_users.append(local_username)
            # Seed the initial policy from this device's own configured
            # limits when it reported one (Phase 5a), rather than always
            # falling back to the hub's 1h/day placeholder -- a brand-new
            # user whose only device already has, say, a 2h/day limit
            # configured shouldn't suddenly show 1h/day in the hub UI.
            snapshot = req.local_policies.get(local_username)
            policy = await create_initial_policy(
                session,
                user.id,
                daily_limits_s=snapshot.daily_limits_s if snapshot else None,
                weekly_limit_s=snapshot.weekly_limit_s if snapshot else None,
                monthly_limit_s=snapshot.monthly_limit_s if snapshot else None,
                allowed_weekdays=snapshot.allowed_weekdays if snapshot else None,
            )
            user.current_policy_id = policy.id
        else:
            existing_policy = await get_current_policy(session, user)
            if existing_policy is None:
                existing_policy = await create_initial_policy(session, user.id)
                user.current_policy_id = existing_policy.id
            policy = existing_policy

        policies[local_username] = policy_to_payload(policy)

    code_row.used_at = now
    code_row.used_by_device_id = device.id

    await session.commit()

    return EnrollResponse(
        device_id=str(device.id),
        device_token=raw_token,
        hub_time=now.isoformat(),
        hub_tz=settings.hub_tz,
        next_poll_ms=settings.default_next_poll_ms,
        new_users=new_users,
        policies=policies,
    )
