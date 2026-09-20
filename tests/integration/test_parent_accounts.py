"""Second (and later) parent accounts via invite links, plus password
change and removal -- `unauthenticated_client` throughout, since these
routes are exactly the ones that need a *real* persisted `Parent` row and
session cookie (the `client` fixture's fake in-memory parent has no row in
`parents` at all, so `count_parents`/FK-referencing inserts don't behave
the way a real session does)."""

from __future__ import annotations

import re

import pytest
from sqlalchemy import text

from tests.conftest import get_test_sessionmaker as _get_test_sessionmaker

pytestmark = pytest.mark.db


async def _setup_first_parent(client, email: str = "first@example.com", password: str = "hunter2hunter"):
    resp = await client.post("/setup", data={"email": email, "password": password}, follow_redirects=False)
    assert resp.status_code == 303


def _extract_invite_token(body: str) -> str:
    match = re.search(r"/invite/([\w-]+)", body)
    assert match is not None, body
    return match.group(1)


@pytest.mark.asyncio
async def test_invite_creates_a_second_working_account(unauthenticated_client):
    c = unauthenticated_client
    await _setup_first_parent(c)

    invite_resp = await c.post("/api/v1/parent-invites")
    assert invite_resp.status_code == 201
    token = invite_resp.json()["token"]

    # The invite page itself needs no auth at all.
    form_resp = await c.get(f"/invite/{token}", follow_redirects=False)
    assert form_resp.status_code == 200

    await c.post("/logout", follow_redirects=False)  # the invitee isn't the inviter
    redeem_resp = await c.post(
        f"/invite/{token}",
        data={"email": "second@example.com", "password": "correcthorse"},
        follow_redirects=False,
    )
    assert redeem_resp.status_code == 303  # logged in as the new parent immediately

    whoami = await c.get("/api/v1/parents")
    emails = {p["email"] for p in whoami.json()}
    assert emails == {"first@example.com", "second@example.com"}


@pytest.mark.asyncio
async def test_invite_is_single_use(unauthenticated_client):
    c = unauthenticated_client
    await _setup_first_parent(c)
    token = (await c.post("/api/v1/parent-invites")).json()["token"]

    first = await c.post(
        f"/invite/{token}", data={"email": "a@example.com", "password": "password1"}, follow_redirects=False
    )
    assert first.status_code == 303

    second = await c.post(f"/invite/{token}", data={"email": "b@example.com", "password": "password2"})
    assert second.status_code == 410


@pytest.mark.asyncio
async def test_last_remaining_parent_cannot_be_deleted(unauthenticated_client):
    c = unauthenticated_client
    await _setup_first_parent(c)

    parents = (await c.get("/api/v1/parents")).json()
    assert len(parents) == 1
    own_id = parents[0]["id"]

    resp = await c.delete(f"/api/v1/parents/{own_id}")
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_a_second_parent_can_be_deleted(unauthenticated_client):
    c = unauthenticated_client
    await _setup_first_parent(c)
    token = (await c.post("/api/v1/parent-invites")).json()["token"]
    await c.post(f"/invite/{token}", data={"email": "removable@example.com", "password": "removeme1"})

    parents = (await c.get("/api/v1/parents")).json()
    assert len(parents) == 2
    removable_id = next(p["id"] for p in parents if p["email"] == "removable@example.com")

    resp = await c.delete(f"/api/v1/parents/{removable_id}")
    assert resp.status_code == 200

    remaining = (await c.get("/api/v1/parents")).json()
    assert len(remaining) == 1


@pytest.mark.asyncio
async def test_change_password_requires_the_current_one(unauthenticated_client):
    c = unauthenticated_client
    await _setup_first_parent(c, password="originalpass")

    wrong = await c.post(
        "/api/v1/parent/password",
        json={"current_password": "not-it", "new_password": "newpassword1"},
    )
    assert wrong.status_code == 401

    right = await c.post(
        "/api/v1/parent/password",
        json={"current_password": "originalpass", "new_password": "newpassword1"},
    )
    assert right.status_code == 200

    await c.post("/logout", follow_redirects=False)
    relogin = await c.post(
        "/login", data={"email": "first@example.com", "password": "newpassword1"}, follow_redirects=False
    )
    assert relogin.status_code == 303


@pytest.mark.asyncio
async def test_password_change_invalidates_other_sessions(unauthenticated_client):
    """A session opened before the change must stop working; the one that
    made the change must not be logged out by its own request."""
    import httpx

    c = unauthenticated_client
    await _setup_first_parent(c, password="originalpass")

    # A second, independent session for the same account (e.g. another
    # browser) -- shares the app but not the cookie jar.
    other = httpx.AsyncClient(transport=c._transport, base_url=c.base_url)
    await other.post("/login", data={"email": "first@example.com", "password": "originalpass"})
    assert (await other.get("/api/v1/parents")).status_code == 200

    await c.post(
        "/api/v1/parent/password",
        json={"current_password": "originalpass", "new_password": "newpassword1"},
    )

    assert (await other.get("/api/v1/parents")).status_code == 401
    assert (await c.get("/api/v1/parents")).status_code == 200  # the changer stays logged in
    await other.aclose()


@pytest.mark.asyncio
async def test_parent_account_actions_are_audit_logged(unauthenticated_client):
    c = unauthenticated_client
    await _setup_first_parent(c)
    token = (await c.post("/api/v1/parent-invites")).json()["token"]
    await c.post(f"/invite/{token}", data={"email": "audited@example.com", "password": "auditedpw"})

    await c.post(
        "/api/v1/parent/password",
        json={"current_password": "auditedpw", "new_password": "auditedpw2"},
    )
    parents = (await c.get("/api/v1/parents")).json()
    target_id = next(p["id"] for p in parents if p["email"] == "first@example.com")
    await c.delete(f"/api/v1/parents/{target_id}")

    session_factory = _get_test_sessionmaker()
    async with session_factory() as session:
        actions = {r.action for r in (await session.execute(text("SELECT action FROM audit_log"))).all()}
    assert "parent.invite_created" in actions
    assert "parent.created" in actions
    assert "parent.password_changed" in actions
    assert "parent.deleted" in actions
