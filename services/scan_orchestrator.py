from __future__ import annotations

import asyncio
import logging
from typing import Optional

from config import settings_store
from services.location_manager import location_manager
from services.gps_service import GPSService
from services.wifi_scanner import scan_wifi
from services.bluetooth_scanner import scan_bluetooth
from services.alert_service import alert_service
import database as db

log = logging.getLogger(__name__)


class ScanOrchestrator:
    """Runs the GPS poll, location clustering, and periodic wifi/bt scans."""

    def __init__(self, gps: GPSService) -> None:
        self.gps = gps
        self._tasks: list[asyncio.Task] = []
        self._stop = asyncio.Event()

    async def start(self) -> None:
        self._stop.clear()
        self._tasks = [
            asyncio.create_task(self._gps_loop()),
            asyncio.create_task(self._wifi_loop()),
            asyncio.create_task(self._bt_loop()),
        ]

    async def stop(self) -> None:
        self._stop.set()
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()

    async def _sleep(self, seconds: float) -> bool:
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)
            return False
        except asyncio.TimeoutError:
            return True

    async def _gps_loop(self) -> None:
        while not self._stop.is_set():
            try:
                s = await settings_store.load()
                fix = self.gps.fix
                await location_manager.update_with_fix(
                    fix, s.new_location_distance_m, s.location_label_template
                )
            except Exception as e:
                log.exception("gps loop error: %s", e)
            if not await self._sleep((await settings_store.load()).gps_poll_interval_s):
                return

    async def _wifi_loop(self) -> None:
        while not self._stop.is_set():
            try:
                s = await settings_store.load()
                iface = s.wifi_interface
                loc_id = location_manager.active_id
                if iface and loc_id is not None:
                    devs = await scan_wifi(iface)
                    fix = self.gps.fix
                    for d in devs:
                        if d.rssi < s.min_rssi:
                            continue
                        details = d.model_dump(mode="json")
                        is_new = await db.upsert_device(
                            location_id=loc_id, kind="wifi", device_id=d.bssid,
                            rssi=d.rssi, details=details,
                        )
                        await db.insert_observation(
                            location_id=loc_id, kind="wifi", device_id=d.bssid,
                            rssi=d.rssi, lat=fix.lat, lon=fix.lon,
                            raw=details,
                        )
                        await alert_service.evaluate(
                            device_kind="wifi", device_id=d.bssid, rssi=d.rssi,
                            location_id=loc_id, details=details, is_new=is_new,
                        )
            except Exception as e:
                log.exception("wifi loop error: %s", e)
            if not await self._sleep((await settings_store.load()).wifi_scan_interval_s):
                return

    async def _bt_loop(self) -> None:
        while not self._stop.is_set():
            try:
                s = await settings_store.load()
                loc_id = location_manager.active_id
                if loc_id is not None:
                    devs = await scan_bluetooth(s.bluetooth_adapter, s.bluetooth_scan_duration_s)
                    fix = self.gps.fix
                    for d in devs:
                        if d.rssi < s.min_rssi:
                            continue
                        if s.hide_random_bt_addresses and d.address_type == "random":
                            continue
                        details = d.model_dump(mode="json")
                        is_new = await db.upsert_device(
                            location_id=loc_id, kind="bluetooth", device_id=d.address,
                            rssi=d.rssi, details=details,
                        )
                        await db.insert_observation(
                            location_id=loc_id, kind="bluetooth", device_id=d.address,
                            rssi=d.rssi, lat=fix.lat, lon=fix.lon,
                            raw=details,
                        )
                        await alert_service.evaluate(
                            device_kind="bluetooth", device_id=d.address, rssi=d.rssi,
                            location_id=loc_id, details=details, is_new=is_new,
                        )
            except Exception as e:
                log.exception("bt loop error: %s", e)
            if not await self._sleep((await settings_store.load()).bluetooth_scan_interval_s):
                return


orchestrator: Optional[ScanOrchestrator] = None
