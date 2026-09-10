"""Hub-wide settings.

PLAN: "One timezone for the whole household, configured on the hub
(HUB_TZ=America/Denver)."
"""

from __future__ import annotations

from zoneinfo import ZoneInfo

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    hub_tz: str = "UTC"
    default_next_poll_ms: int = 20000

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.hub_tz)


settings = Settings()
