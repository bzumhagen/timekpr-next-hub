"""HTTP client for talking to the hub.

PLAN reference: "API" and "Auth and threat model". Uses httpx (sync client,
explicit timeouts) per PLAN's tech-stack recommendation -- no async needed
here since the agent has no GLib/asyncio loop to share time with.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import httpx

DEFAULT_TOKEN_PATH = Path("/var/lib/timekpr-hub-agent/device_token")
CONNECT_TIMEOUT_S = 5.0
READ_TIMEOUT_S = 10.0


class HubUnreachableError(RuntimeError):
    pass


class DeviceRevokedError(RuntimeError):
    """Raised on 401/403 -- PLAN pitfall: 'Never fail open on an auth error.'"""


@dataclass
class HubClientConfig:
    base_url: str
    token_path: Path = DEFAULT_TOKEN_PATH
    ca_cert: str | None = None  # PLAN: "--ca-cert" for self-signed deployments


class HubClient:
    def __init__(self, config: HubClientConfig) -> None:
        self._config = config
        self._token = self._load_token()
        self._client = httpx.Client(
            base_url=config.base_url,
            timeout=httpx.Timeout(connect=CONNECT_TIMEOUT_S, read=READ_TIMEOUT_S, write=READ_TIMEOUT_S, pool=READ_TIMEOUT_S),
            verify=config.ca_cert if config.ca_cert else True,
        )

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

    def enroll(self, *, enrollment_code: str, hostname: str, machine_id: str, os: str, tz: str,
               agent_version: str, local_users: list[str]) -> dict:
        resp = self._client.post(
            "/api/v1/enroll",
            json={
                "enrollment_code": enrollment_code,
                "hostname": hostname,
                "machine_id": machine_id,
                "os": os,
                "tz": tz,
                "agent_version": agent_version,
                "local_users": local_users,
            },
        )
        resp.raise_for_status()
        data = resp.json()
        self._token = data["device_token"]
        self._save_token(self._token)
        return data

    def sync(self, payload: dict) -> dict:
        """POST /sync. Raises DeviceRevokedError on 401/403 (PLAN: agent must
        treat this as immediate `closed` enforcement, never fail open) and
        HubUnreachableError on any network-level failure or 5xx (agent's
        offline-grace/cap/closed logic takes over)."""
        try:
            resp = self._client.post("/api/v1/sync", json=payload, headers=self._headers())
        except httpx.HTTPError as exc:
            raise HubUnreachableError(str(exc)) from exc

        if resp.status_code in (401, 403):
            raise DeviceRevokedError(f"device token rejected: {resp.status_code}")
        if resp.status_code >= 500:
            raise HubUnreachableError(f"hub returned {resp.status_code}")

        resp.raise_for_status()
        return resp.json()

    def post_events(self, events: list[dict]) -> None:
        """Fire-and-forget (PLAN: "batched, fire-and-forget"). Swallows
        failures -- events are diagnostic, never load-bearing for
        enforcement."""
        if not events:
            return
        try:
            self._client.post("/api/v1/events", json={"events": events}, headers=self._headers())
        except httpx.HTTPError:
            pass
