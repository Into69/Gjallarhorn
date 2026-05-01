from __future__ import annotations

import asyncio
import logging
from typing import Optional

from config import settings_store
from services.location_manager import location_manager
from services.gps_service import GPSService
from services.wifi_scanner import scan_wifi, pick_wifi_interface
from services.bluetooth_scanner import scan_bluetooth
from services.alert_service import alert_service
from services.probe_scanner import probe_scanner
from services.oui import oui_service
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
            asyncio.create_task(self._probe_loop()),
        ]

    async def stop(self) -> None:
        self._stop.set()
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        await probe_scanner.stop()

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
                    fix,
                    static_threshold_m=s.new_location_distance_m,
                    label_template=s.location_label_template,
                    dynamic_enabled=s.new_location_dynamic,
                    dynamic_t_s=s.new_location_dynamic_t_s,
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
                # "auto" → resolve to the first non-associated wireless interface;
                # falls through as None if no candidate exists, which is the same
                # as "none" — silently skip the scan this tick.
                if iface == "auto":
                    iface = await pick_wifi_interface()
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

    async def _probe_loop(self) -> None:
        """Watches probe-scanner settings; starts/stops/switches the
        capture (tshark or scapy backend) as needed. Doesn't poll for
        probes itself — the scanner pushes them via the callback below."""
        while not self._stop.is_set():
            try:
                s = await settings_store.load()
                want_iface = (s.probe_interface or "").strip() or None
                want_backend = s.probe_backend
                cur_iface = probe_scanner.interface if probe_scanner.running else None
                cur_backend = probe_scanner.backend if probe_scanner.running else None
                if want_iface and (want_iface != cur_iface or want_backend != cur_backend):
                    await probe_scanner.start(
                        want_iface, self._on_probe, backend=want_backend,
                    )
                elif not want_iface and probe_scanner.running:
                    await probe_scanner.stop()
            except Exception as e:
                log.exception("probe loop error: %s", e)
            if not await self._sleep(5.0):
                return

    async def _on_probe(self, probe: dict) -> None:
        """Handle a single probe-request observation. Tags with the active
        location, applies kind/RSSI/randomization filters, then upserts
        into devices and runs alert evaluation. Probed SSIDs accumulate
        in the device's details so we can see which networks a client is
        chasing."""
        s = await settings_store.load()
        loc_id = location_manager.active_id
        if loc_id is None:
            return
        rssi = probe.get("rssi")
        if rssi is None or rssi < s.probe_min_rssi:
            return
        if probe.get("randomized") and s.probe_skip_randomized:
            return

        mac = probe["mac"]
        new_ssid = (probe.get("ssid") or "").strip()
        channel = probe.get("channel")

        # Merge with any existing details so the SSID list and channels
        # accumulate across observations of the same client.
        prior = await db.get_device_details(loc_id, "wifi_client", mac) or {}
        ssids = list(prior.get("ssids") or [])
        if new_ssid and new_ssid not in ssids:
            ssids.append(new_ssid)
        channels = list(prior.get("channels") or [])
        if channel is not None and channel not in channels:
            channels.append(channel)
        vendor = prior.get("vendor")
        if not vendor:
            try:
                vendor = await oui_service.lookup(mac)
            except Exception:
                vendor = None
        details = {
            "vendor": vendor,
            "ssids": ssids,
            "channels": channels,
            "randomized": probe.get("randomized", False),
        }

        fix = self.gps.fix
        is_new = await db.upsert_device(
            location_id=loc_id, kind="wifi_client", device_id=mac,
            rssi=rssi, details=details,
        )
        await db.insert_observation(
            location_id=loc_id, kind="wifi_client", device_id=mac,
            rssi=rssi, lat=fix.lat, lon=fix.lon,
            raw={**details, "ssid": new_ssid, "channel": channel},
        )
        await alert_service.evaluate(
            device_kind="wifi_client", device_id=mac, rssi=rssi,
            location_id=loc_id, details=details, is_new=is_new,
        )


orchestrator: Optional[ScanOrchestrator] = None
