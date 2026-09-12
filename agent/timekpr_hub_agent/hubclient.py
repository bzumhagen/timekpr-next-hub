"""HTTP client for talking to the hub.

PLAN reference: "API" and "Auth and threat model". Built on `urllib.request`
rather than a third-party HTTP library: the agent only ever makes simple
JSON POSTs (no streaming, no connection pooling, no async), and stdlib-only
means the agent has zero non-stdlib dependencies of its own beyond
timekpr-next itself (CHECKLIST.md "Multi-distro support") -- one less thing
every future distro's packaging has to provide. No async needed either way,
since the agent has no GLib/asyncio loop to share time with.
"""

from __future__ import annotations

import json
import ssl
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

DEFAULT_TOKEN_PATH = Path("/var/lib/timekpr-hub-agent/device_token")
TIMEOUT_S = 10.0
"""A single timeout covering the whole request (connect + read)"""


class HubUnreachableError(RuntimeError):
    pass


class DeviceRevokedError(RuntimeError):
    """Raised on 401/403 -- PLAN pitfall: 'Never fail open on an auth error.'"""


class EnrollError(RuntimeError):
    """Raised by `HubClient.enroll` with a message meant to be printed
    directly to a parent running `enroll` at a terminal -- no status code,
    no traceback, just what went wrong and what to do about it."""


@dataclass
class HubClientConfig:
    base_url: str
    token_path: Path = DEFAULT_TOKEN_PATH
    ca_cert: str | None = None  # PLAN: "--ca-cert" for self-signed deployments


class HubClient:
    def __init__(self, config: HubClientConfig) -> None:
        self._config = config
        self._token = self._load_token()
        # None means "use urllib's own default HTTPS verification" (the
        # system trust store) -- only built explicitly when a custom CA is
        # given. Passing a context at all is harmless for a plain `http://`
        # base_url; urllib only consults it for `https://` requests.
        self._ssl_context = ssl.create_default_context(cafile=config.ca_cert) if config.ca_cert else None

    def _load_token(self) -> str | None:
        if self._config.token_path.exists():
            return self._config.token_path.read_text().strip()
        return None

    def _save_token(self, token: str) -> None:
        self._config.token_path.parent.mkdir(parents=True, exist_ok=True)
        self._config.token_path.write_text(token)
        self._config.token_path.chmod(0o600)

    def _headers(self) -> dict:
        if not self._token:
            raise RuntimeError("device not enrolled -- no token on disk")
        return {"Authorization": f"Bearer {self._token}"}

    def _post(self, path: str, payload: dict, headers: dict | None = None) -> tuple[int, dict]:
        """POST JSON, returning `(status_code, parsed_json_body)` for *any*
        HTTP response, 4xx/5xx included -- callers inspect the status the
        same way they would a normal httpx response, rather than one
        exception type per status code. Only a genuine network-level
        failure (DNS, connection refused, timeout, TLS handshake) raises,
        as `urllib.error.URLError` (`ConnectionError`/`socket.timeout` are
        both `OSError` subtypes it can wrap, but are caught the same way by
        callers below either way)."""
        url = self._config.base_url.rstrip("/") + path
        try:
            req = urllib.request.Request(
                url,
                data=json.dumps(payload).encode("utf-8"),
                method="POST",
                headers={"Content-Type": "application/json", **(headers or {})},
            )
            with urllib.request.urlopen(req, timeout=TIMEOUT_S, context=self._ssl_context) as resp:
                body = resp.read()
                return resp.status, (json.loads(body) if body else {})
        except ValueError as exc:
            # A scheme-less or otherwise malformed base_url (e.g.
            # "192.168.1.5:8000") makes urllib.request.Request itself raise
            # "ValueError: unknown url type" -- neither URLError nor
            # OSError, so it used to escape every handler below (and every
            # caller's) as a raw traceback instead of a friendly
            # HubUnreachableError/EnrollError. config.normalize_hub_url
            # prevents this at the CLI layer already; this is the defensive
            # second line in case base_url reaches here some other way (a
            # hand-edited agent.env, a future caller).
            raise urllib.error.URLError(str(exc)) from exc
        except urllib.error.HTTPError as exc:
            # Still a completed HTTP exchange (the hub responded, just with
            # an error status) -- urllib raises this instead of returning it
            # as a normal response, but it carries the same information.
            body = exc.read()
            try:
                parsed = json.loads(body) if body else {}
            except json.JSONDecodeError:
                parsed = {}
            return exc.code, parsed

    def enroll(
        self,
        *,
        enrollment_code: str,
        hostname: str,
        machine_id: str,
        os: str,
        tz: str,
        agent_version: str,
        local_users: list[str],
        local_policies: dict[str, dict] | None = None,
    ) -> dict:
        try:
            status, data = self._post(
                "/api/v1/enroll",
                {
                    "enrollment_code": enrollment_code,
                    "hostname": hostname,
                    "machine_id": machine_id,
                    "os": os,
                    "tz": tz,
                    "agent_version": agent_version,
                    "local_users": local_users,
                    "local_policies": local_policies or {},
                },
            )
        except (urllib.error.URLError, OSError) as exc:
            raise EnrollError(
                f"can't reach the hub at {self._config.base_url} "
                "(check --hub-url, and --ca-cert if it uses a self-signed certificate)"
            ) from exc

        # Friendly messages for the enrollment-code failure modes a parent
        # will actually hit -- a bare traceback for "code expired" is not
        # something to hand a parent at a terminal.
        if status == 404:
            raise EnrollError("unknown enrollment code -- generate a new one in the hub UI")
        if status == 409:
            raise EnrollError("enrollment code already used -- generate a new one in the hub UI")
        if status == 410:
            raise EnrollError("enrollment code expired -- generate a new one in the hub UI")
        if status >= 400:
            raise EnrollError(f"hub returned {status}: {data}")

        self._token = data["device_token"]
        self._save_token(self._token)
        return data

    def sync(self, payload: dict) -> dict:
        """POST /sync. Raises DeviceRevokedError on 401/403 (PLAN: agent must
        treat this as immediate `closed` enforcement, never fail open) and
        HubUnreachableError on any network-level failure or 5xx (agent's
        offline-grace/cap/closed logic takes over)."""
        try:
            status, data = self._post("/api/v1/sync", payload, headers=self._headers())
        except (urllib.error.URLError, OSError) as exc:
            raise HubUnreachableError(str(exc)) from exc

        if status in (401, 403):
            raise DeviceRevokedError(f"device token rejected: {status}")
        if status >= 500:
            raise HubUnreachableError(f"hub returned {status}")
        if status >= 400:
            raise HubUnreachableError(f"hub returned {status}: {data}")

        return data
