from __future__ import annotations

import json

from models import AppSettings
from database import get_setting, set_setting

_SETTINGS_KEY = "app_settings"


class SettingsStore:
    def __init__(self) -> None:
        self._cached: AppSettings | None = None

    async def load(self) -> AppSettings:
        if self._cached is not None:
            return self._cached
        raw = await get_setting(_SETTINGS_KEY)
        if raw is None:
            self._cached = AppSettings()
            await self.save(self._cached)
        else:
            self._cached = AppSettings.model_validate_json(raw)
        return self._cached

    async def save(self, settings: AppSettings) -> AppSettings:
        await set_setting(_SETTINGS_KEY, settings.model_dump_json())
        self._cached = settings
        return settings

    async def patch(self, partial: dict) -> AppSettings:
        current = await self.load()
        merged = current.model_copy(update={k: v for k, v in partial.items() if v is not None})
        return await self.save(merged)


settings_store = SettingsStore()
