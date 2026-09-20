"""Admin-facing API: the JSON endpoints behind the hub UI.

Every route here is gated on a logged-in admin session -- see
`api/admin_auth.py`, which wires the dependency in `app.py`.

Split by area: users.py (grants, the policy editor, per-date overrides,
the chore gate, per-user settings), devices.py (enrollment codes, the
audit log, device revoke/observe/enforce/delete), admins.py (admin
accounts).
"""

from __future__ import annotations

from fastapi import APIRouter

from timekpr_hub.api.admin import admins, devices, users

router = APIRouter()
router.include_router(users.router)
router.include_router(devices.router)
router.include_router(admins.router)
