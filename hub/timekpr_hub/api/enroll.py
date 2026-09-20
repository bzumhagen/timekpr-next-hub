"""POST /enroll -- device enrollment via a one-time code.

No device auth on this endpoint (there's no token yet); the enrollment code
itself is the credential, single-use and short-lived.
"""

from __future__ import annotations

import secrets
import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from timekpr_hub_core.models import EnrollRequest, EnrollResponse

from timekpr_hub.db.models import Device, EnrollmentCode
from timekpr_hub.db.session import get_session
from timekpr_hub.services.enrollment import provision_user_alias
from timekpr_hub.services.policy import policy_to_payload
from timekpr_hub.services.tokens import hash_token
from timekpr_hub.settings import settings

router = APIRouter()

TOKEN_PREFIX = "tkh_"


@router.post("/enroll", response_model=EnrollResponse, status_code=status.HTTP_201_CREATED)
async def enroll(req: EnrollRequest, session: AsyncSession = Depends(get_session)) -> EnrollResponse:
    now = datetime.now(UTC)

    # Atomically claim the code (used_at IS NULL AND not expired, in the
    # same UPDATE) rather than a plain SELECT followed by a later UPDATE --
    # two concurrent enrolls with the same code could otherwise both pass
    # the used_at is None check before either commits.
    # Zero rows back means unknown/used/
    # expired; a follow-up SELECT (safe now -- nothing left to race) picks
    # which for the error.
    claim = await session.execute(
        update(EnrollmentCode)
        .where(
            EnrollmentCode.code == req.enrollment_code,
            EnrollmentCode.used_at.is_(None),
            EnrollmentCode.expires_at >= now,
        )
        .values(used_at=now)
        .returning(EnrollmentCode.code)
    )
    claimed_code = claim.scalar_one_or_none()
    if claimed_code is None:
        existing_code = await session.execute(
            select(EnrollmentCode).where(EnrollmentCode.code == req.enrollment_code)
        )
        existing_row = existing_code.scalar_one_or_none()
        if existing_row is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown enrollment code")
        if existing_row.used_at is not None:
            raise HTTPException(status.HTTP_409_CONFLICT, "enrollment code already used")
        raise HTTPException(status.HTTP_410_GONE, "enrollment code expired")

    raw_token = TOKEN_PREFIX + secrets.token_urlsafe(32)

    # Rebind onto an existing device with the same machine_id, rather than
    # forking a second history for the same physical machine -- the
    # original bug report: uninstalling and reinstalling the agent (or
    # `pacman -U` over an existing install) re-enrolled as a brand-new
    # device every time, silently orphaning that machine's past
    # usage_counters/activity_intervals under the old device row. Only a
    # non-revoked device counts as "the same live machine"; an admin who
    # explicitly revoked a device gets a genuinely new row on the next
    # enroll (the partial unique index on devices.machine_id only covers
    # status <> 'revoked', so this SELECT and that index agree).
    existing_device_result = await session.execute(
        select(Device).where(Device.machine_id == req.machine_id, Device.status != "revoked")
    )
    existing_device = existing_device_result.scalar_one_or_none()
    rebound = existing_device is not None
    previously_enrolled_at = existing_device.enrolled_at if existing_device else None

    if existing_device is not None:
        device = existing_device
        device.token_hash = hash_token(raw_token)
        device.name = req.hostname
        device.agent_version = req.agent_version
        device.status = "active"
        # enrolled_at is deliberately left untouched -- it's this device's
        # original enrollment date, not this rebind's.
    else:
        device = Device(
            id=uuid.uuid4(),
            name=req.hostname,
            machine_id=req.machine_id,
            token_hash=hash_token(raw_token),
            # An admin-minted enrollment code is itself the approval -- there's
            # no separate authentication on either endpoint for a second
            # "approve" step to actually gate anything. A 'pending' status
            # would mean a brand-new device could already sync anyway
            # (auth.py only rejects 'revoked'), so this also removes a step
            # that added friction without adding security.
            status="active",
            enforcement="enforce",
            agent_version=req.agent_version,
            enrolled_at=now,
        )
        session.add(device)
    await session.flush()

    new_users: list[str] = []
    policies: dict[str, object] = {}

    # Provision or merge into an existing canonical user per reported local
    # username: a fresh username gets a new User row, a username matching
    # one already known to the hub (e.g. this account also exists on
    # another device) just gets a new alias pointing at it. The same
    # provisioning a local user added to an already-enrolled device's
    # managed list goes through later, at api/sync.py's unmapped-user branch.
    for local_username in req.local_users:
        user, policy, is_new = await provision_user_alias(
            session,
            device_id=device.id,
            local_username=local_username,
            local_policy_snapshot=req.local_policies.get(local_username),
        )
        if is_new:
            new_users.append(local_username)
        policies[local_username] = policy_to_payload(policy)

    await session.execute(
        update(EnrollmentCode).where(EnrollmentCode.code == claimed_code).values(used_by_device_id=device.id)
    )

    await session.commit()

    return EnrollResponse(
        device_id=str(device.id),
        device_token=raw_token,
        hub_time=now.isoformat(),
        hub_tz=settings.hub_tz,
        next_poll_ms=settings.default_next_poll_ms,
        new_users=new_users,
        policies=policies,
        rebound=rebound,
        previously_enrolled_at=previously_enrolled_at.isoformat() if previously_enrolled_at else None,
    )
