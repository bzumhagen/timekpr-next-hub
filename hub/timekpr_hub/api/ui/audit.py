"""The Audit page: every admin action, newest first."""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession

from timekpr_hub.api.util import templates
from timekpr_hub.db.session import get_session
from timekpr_hub.services.audit import list_audit_events

router = APIRouter()

_AUDIT_PAGE_SIZE = 50


@router.get("/audit", response_class=HTMLResponse)
async def audit_page(
    request: Request, offset: int = 0, session: AsyncSession = Depends(get_session)
) -> HTMLResponse:
    """Every admin action, newest first -- the read side of
    services/audit.py's `record_audit_event`, which every mutating route in
    api/ui/ and api/admin/ already calls. Rendered once per request (no
    live poll, unlike the dashboard/devices pages): audit history doesn't
    change out from under the page the way live usage does, and `?offset=`
    pages back through it with plain links."""
    events = await list_audit_events(session, limit=_AUDIT_PAGE_SIZE + 1, offset=offset)
    has_more = len(events) > _AUDIT_PAGE_SIZE
    events = events[:_AUDIT_PAGE_SIZE]
    rows = [
        {
            "ts": e.ts.strftime("%Y-%m-%d %H:%M:%S UTC"),
            "actor": f"{e.actor_type} {e.actor_id}" if e.actor_id else e.actor_type,
            "action": e.action,
            "target": f"{e.target_type} {e.target_id}" if e.target_type else None,
            "before": json.dumps(e.before_json, indent=2, sort_keys=True) if e.before_json else None,
            "after": json.dumps(e.after_json, indent=2, sort_keys=True) if e.after_json else None,
            "ip": e.ip,
        }
        for e in events
    ]
    return templates.TemplateResponse(
        request,
        "audit.html",
        {
            "events": rows,
            "offset": offset,
            "page_size": _AUDIT_PAGE_SIZE,
            "has_more": has_more,
            "prev_offset": max(0, offset - _AUDIT_PAGE_SIZE),
        },
    )
