"""Hub-wide settings.

One timezone for the whole household, configured on the hub
(HUB_TZ=America/Denver).
"""

from __future__ import annotations

from zoneinfo import ZoneInfo

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # Local-dev-only default (throwaway credentials, matches deploy/.env.example's
    # dev compose) -- any real deployment sets DATABASE_URL explicitly (see
    # deploy/docker-compose.yml). Routed through here rather than read via a
    # raw `os.environ.get(...)` in db/session.py, so it's validated the same
    # way as every other setting.
    database_url: str = "postgresql+asyncpg://timekpr_hub:timekpr_hub@localhost:5432/timekpr_hub"
    hub_tz: str = "UTC"
    default_next_poll_ms: int = 20000

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.hub_tz)


settings = Settings()
