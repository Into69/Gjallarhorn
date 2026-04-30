"""Alert evaluation.

Each scan hit (wifi or bluetooth, after the orchestrator has tagged it
with the current sensor location) is run through every enabled rule.
A match emits one alert_events row, with a per-(rule, device) cooldown
to keep the feed sane while a strong target sits next to the sensor.
"""
from __future__ import annotations

import logging
import time
from typing import Any

import database as db

log = logging.getLogger(__name__)


# Cooldown between repeated alerts for the same (rule_id, device_id) pair
COOLDOWN_S = 60.0


class AlertService:
    def __init__(self) -> None:
        self._rules: list[dict] = []
        self._rules_loaded = False
        # (rule_id, device_id) -> last fired monotonic timestamp
        self._cooldown: dict[tuple[int, str], float] = {}

    async def load_rules(self) -> None:
        self._rules = await db.list_alert_rules()
        self._rules_loaded = True

    async def reload(self) -> None:
        """Call after any rule mutation so the next scan uses fresh rules."""
        await self.load_rules()

    async def evaluate(
        self,
        device_kind: str,           # 'wifi' | 'bluetooth'
        device_id: str,             # BSSID or MAC, lowercase
        rssi: int,
        location_id: int | None,
        details: dict,
    ) -> list[int]:
        """Run all enabled rules against a single device sighting. Returns
        a list of newly-inserted alert_events ids (after cooldown filtering)."""
        if not self._rules_loaded:
            await self.load_rules()
        if not self._rules:
            return []

        emitted: list[int] = []
        now = time.monotonic()
        device_id_l = (device_id or "").lower()
        ssid_or_name = (details.get("ssid") or details.get("name") or "")
        vendor = (details.get("vendor") or "")

        for rule in self._rules:
            if not rule.get("enabled"):
                continue
            kind_filter = rule.get("kind")
            if kind_filter and kind_filter != device_kind:
                continue
            loc_filter = rule.get("location_id")
            if loc_filter is not None and loc_filter != location_id:
                continue
            if not _matches(rule, device_id_l, ssid_or_name, vendor, rssi):
                continue

            key = (rule["id"], device_id_l)
            last = self._cooldown.get(key, 0.0)
            if now - last < COOLDOWN_S:
                continue
            self._cooldown[key] = now

            event_id = await db.insert_alert_event(
                rule_id=rule["id"], location_id=location_id,
                device_kind=device_kind, device_id=device_id, rssi=rssi,
                details=details,
            )
            log.info(
                "ALERT '%s' (rule %d) %s/%s rssi=%d", rule["name"], rule["id"],
                device_kind, device_id, rssi,
            )
            emitted.append(event_id)

        return emitted


def _matches(rule: dict, device_id_l: str, name_or_ssid: str, vendor: str, rssi: int) -> bool:
    mt = rule.get("match_type")
    mv = (rule.get("match_value") or "").strip()
    if not mv:
        return False

    if mt == "device_id":
        # Exact full id, or a prefix (e.g. "aa:bb:cc" matches the OUI).
        target = mv.lower()
        return device_id_l == target or device_id_l.startswith(target)

    if mt == "name_contains":
        return mv.lower() in (name_or_ssid or "").lower()

    if mt == "vendor_contains":
        return mv.lower() in (vendor or "").lower()

    if mt == "rssi_above":
        try:
            threshold = int(mv)
        except ValueError:
            return False
        # rssi is dBm (negative). "above" = stronger = closer to 0.
        return rssi >= threshold

    return False


alert_service = AlertService()
