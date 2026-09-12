"""HubClient tests, against a real local HTTP server (`http.server` on a
loopback socket in a background thread) rather than mocking
`urllib.request.urlopen` -- exercises the actual request/response wire
format the stdlib client sends and parses, not just that it calls the
right function. See hubclient.py's module docstring for why this is
stdlib-only (`urllib.request`) rather than httpx.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from timekpr_hub_agent.hubclient import (
    DeviceRevokedError,
    EnrollError,
    HubClient,
    HubClientConfig,
    HubUnreachableError,
)


class _Handler(BaseHTTPRequestHandler):
    # Set per-test on the class before starting the server.
    responses: dict[str, tuple[int, dict]] = {}
    # Path -> {bearer token: (status, payload)} -- takes priority over
    # `responses` when set, for tests that need the response to depend on
    # which token was actually sent (e.g. simulating a rotated token).
    token_gated_responses: dict[str, dict[str, tuple[int, dict]]] = {}
    last_request: dict | None = None
    request_log: list[dict] = []

    def do_POST(self) -> None:  # noqa: N802 -- BaseHTTPRequestHandler's own naming
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length) if length else b""
        request = {
            "path": self.path,
            "headers": dict(self.headers),
            "body": json.loads(body) if body else None,
        }
        type(self).last_request = request
        type(self).request_log.append(request)

        gated = self.token_gated_responses.get(self.path)
        if gated is not None:
            auth = self.headers.get("Authorization", "")
            token = auth.removeprefix("Bearer ")
            status, payload = gated.get(token, (401, {"detail": "invalid device token"}))
        else:
            status, payload = self.responses.get(self.path, (404, {"detail": "no route configured"}))
        data = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args) -> None:  # silence per-request stderr logging
        pass


@pytest.fixture
def server():
    _Handler.responses = {}
    _Handler.token_gated_responses = {}
    _Handler.last_request = None
    _Handler.request_log = []
    httpd = HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield httpd, _Handler
    finally:
        httpd.shutdown()
        thread.join(timeout=5)


def _client(server, tmp_path) -> HubClient:
    httpd, _ = server
    port = httpd.server_address[1]
    return HubClient(HubClientConfig(base_url=f"http://127.0.0.1:{port}", token_path=tmp_path / "token"))


def test_enroll_success_saves_token(server, tmp_path):
    httpd, handler = server
    handler.responses["/api/v1/enroll"] = (
        201,
        {
            "device_id": "d1",
            "device_token": "tkh_abc",
            "hub_time": "2026-01-01T00:00:00+00:00",
            "hub_tz": "UTC",
        },
    )
    client = _client(server, tmp_path)

    data = client.enroll(
        enrollment_code="CODE1",
        hostname="h",
        machine_id="m",
        os="linux",
        tz="UTC",
        agent_version="0.1.0",
        local_users=["alice"],
    )

    assert data["device_token"] == "tkh_abc"
    assert (tmp_path / "token").read_text() == "tkh_abc"
    assert (tmp_path / "token").stat().st_mode & 0o777 == 0o600
    assert handler.last_request["body"]["enrollment_code"] == "CODE1"


@pytest.mark.parametrize(
    "status,fragment",
    [(404, "unknown"), (409, "already used"), (410, "expired")],
)
def test_enroll_failure_statuses_raise_friendly_errors(server, tmp_path, status, fragment):
    _, handler = server
    handler.responses["/api/v1/enroll"] = (status, {"detail": "whatever"})
    client = _client(server, tmp_path)

    with pytest.raises(EnrollError, match=fragment):
        client.enroll(
            enrollment_code="CODE1",
            hostname="h",
            machine_id="m",
            os="linux",
            tz="UTC",
            agent_version="0.1.0",
            local_users=["alice"],
        )


def test_enroll_unreachable_hub_raises_enroll_error(tmp_path):
    # Nothing listening on this port.
    client = HubClient(HubClientConfig(base_url="http://127.0.0.1:1", token_path=tmp_path / "token"))
    with pytest.raises(EnrollError, match="can't reach the hub"):
        client.enroll(
            enrollment_code="CODE1",
            hostname="h",
            machine_id="m",
            os="linux",
            tz="UTC",
            agent_version="0.1.0",
            local_users=["alice"],
        )


def test_enroll_with_a_schemeless_base_url_raises_enroll_error_not_a_traceback(tmp_path):
    """A bare `urllib.request.Request` raises `ValueError: unknown url type`
    for a scheme-less URL (e.g. "192.168.1.5:8000") -- neither URLError nor
    OSError, so it used to escape every handler as a raw traceback instead
    of the friendly EnrollError every other failure mode gets. The CLI
    layer normalizes this away in practice (config.normalize_hub_url), but
    this is the defensive second line in _post itself."""
    client = HubClient(HubClientConfig(base_url="192.168.1.5:1", token_path=tmp_path / "token"))
    with pytest.raises(EnrollError):
        client.enroll(
            enrollment_code="CODE1",
            hostname="h",
            machine_id="m",
            os="linux",
            tz="UTC",
            agent_version="0.1.0",
            local_users=["alice"],
        )


def test_sync_sends_bearer_token_and_returns_body(server, tmp_path):
    _, handler = server
    (tmp_path / "token").write_text("tkh_existing")
    handler.responses["/api/v1/sync"] = (200, {"hub_time": "x", "users": []})
    client = _client(server, tmp_path)

    data = client.sync({"agent_time": "x", "users": []})

    assert data["users"] == []
    assert handler.last_request["headers"]["Authorization"] == "Bearer tkh_existing"


@pytest.mark.parametrize("status", [401, 403])
def test_sync_revoked_token_raises_device_revoked(server, tmp_path, status):
    _, handler = server
    (tmp_path / "token").write_text("tkh_existing")
    handler.responses["/api/v1/sync"] = (status, {"detail": "revoked"})
    client = _client(server, tmp_path)

    with pytest.raises(DeviceRevokedError):
        client.sync({"agent_time": "x", "users": []})


def test_sync_5xx_raises_hub_unreachable(server, tmp_path):
    _, handler = server
    (tmp_path / "token").write_text("tkh_existing")
    handler.responses["/api/v1/sync"] = (503, {"detail": "db down"})
    client = _client(server, tmp_path)

    with pytest.raises(HubUnreachableError):
        client.sync({"agent_time": "x", "users": []})


def test_sync_unreachable_hub_raises_hub_unreachable(tmp_path):
    (tmp_path / "token").write_text("tkh_existing")
    client = HubClient(HubClientConfig(base_url="http://127.0.0.1:1", token_path=tmp_path / "token"))
    with pytest.raises(HubUnreachableError):
        client.sync({"agent_time": "x", "users": []})


def test_sync_reloads_a_rotated_token_and_retries_instead_of_treating_it_as_revoked(server, tmp_path):
    """A long-running `run` process's HubClient loads its token once, at
    __init__ -- but a re-enroll on the same machine rewrites the token
    file on disk out from under it. Previously every subsequent /sync 401'd
    forever (indistinguishable from a genuine revoke) until something
    restarted the service (docs/best-practices-review.md-style live
    finding: `status`, a fresh process, succeeded while the long-running
    `run` service kept getting rejected on the very same tick). `sync` now
    notices the on-disk token differs from what it's holding and retries
    once with the fresh one before giving up."""
    _, handler = server
    token_path = tmp_path / "token"
    token_path.write_text("tkh_old")
    client = _client(server, tmp_path)  # loads "tkh_old" into memory here

    # Simulate a re-enroll: the file now holds a new token: the hub only
    # accepts THAT one.
    token_path.write_text("tkh_new")
    handler.token_gated_responses["/api/v1/sync"] = {
        "tkh_new": (200, {"hub_time": "x", "users": []}),
    }

    data = client.sync({"agent_time": "x", "users": []})

    assert data["users"] == []
    # Exactly two requests: the stale-token 401, then the retry that
    # succeeded with the reloaded token.
    assert len(handler.request_log) == 2
    assert handler.request_log[0]["headers"]["Authorization"] == "Bearer tkh_old"
    assert handler.request_log[1]["headers"]["Authorization"] == "Bearer tkh_new"


def test_sync_still_raises_device_revoked_when_the_token_on_disk_is_unchanged(server, tmp_path):
    """The reload-and-retry above must not paper over a genuine revoke: if
    the token file was never rewritten (no re-enroll happened), retrying
    with "the same token that just failed" would be pointless -- confirm
    it still raises DeviceRevokedError, and confirm it did NOT bother
    retrying (only one request went out)."""
    _, handler = server
    token_path = tmp_path / "token"
    token_path.write_text("tkh_revoked")
    client = _client(server, tmp_path)
    handler.responses["/api/v1/sync"] = (403, {"detail": "device token revoked"})

    with pytest.raises(DeviceRevokedError):
        client.sync({"agent_time": "x", "users": []})

    assert len(handler.request_log) == 1
