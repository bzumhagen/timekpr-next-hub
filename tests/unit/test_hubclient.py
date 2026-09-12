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
    last_request: dict | None = None

    def do_POST(self) -> None:  # noqa: N802 -- BaseHTTPRequestHandler's own naming
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length) if length else b""
        type(self).last_request = {
            "path": self.path,
            "headers": dict(self.headers),
            "body": json.loads(body) if body else None,
        }
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
    _Handler.last_request = None
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


def test_post_events_swallows_network_failure(tmp_path):
    (tmp_path / "token").write_text("tkh_existing")
    client = HubClient(HubClientConfig(base_url="http://127.0.0.1:1", token_path=tmp_path / "token"))
    client.post_events([{"kind": "agent_started"}])  # must not raise


def test_post_events_noop_on_empty_list(tmp_path):
    (tmp_path / "token").write_text("tkh_existing")
    client = HubClient(HubClientConfig(base_url="http://127.0.0.1:1", token_path=tmp_path / "token"))
    client.post_events([])  # must not even attempt a request
