"""Hub-UI routes: the dashboard, the per-user policy editor, and the
usage-statistics view.

Deliberately thin: reuses the same service functions as the JSON admin API
(`api/admin/`) rather than duplicating logic, and renders server-side HTML
fragments/pages -- no client-side JS beyond `_base.html`'s small form/poll
helper, the dashboard's inline ticker, and the policy editor's own
"check/clear a whole day" convenience buttons.

Every route here (and every /api/v1/* admin route) requires an
authenticated admin session -- see `get_current_admin_ui` in
`api/admin_auth.py`, applied router-level in `app.py`.

Split by area: dashboard.py (index, the per-user summary cards, quick
grants), overrides.py (day-limit/day-hours overrides, the chore gate),
policy.py (the full policy editor), users.py (per-user settings, rename,
delete, usage stats), devices.py, admins.py, audit.py.
"""

from __future__ import annotations

from fastapi import APIRouter

from timekpr_hub.api.ui import admins, audit, dashboard, devices, overrides, policy, users

router = APIRouter()
router.include_router(dashboard.router)
router.include_router(overrides.router)
router.include_router(policy.router)
router.include_router(users.router)
router.include_router(devices.router)
router.include_router(admins.router)
router.include_router(audit.router)
