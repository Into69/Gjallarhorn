from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional

from config import settings_store
from services.location_manager import location_manager
from services.gps_service import GPSService
from services.wifi_scanner import scan_wifi, pick_wifi_interface
from services.bluetooth_scanner import scan_bluetooth
from services.bluetooth_classic_scanner import scan_bluetooth_classic
from services.alert_service import alert_service
from services.probe_scanner import probe_scanner, parse_channels
from services.oui import oui_service
import database as db

log = logging.getLogger(__name__)


class ScannerStats:
    """Per-scanner runtime stats surfaced on the map sidebar. Mirrors the
    shape of probe_scanner.status() so the same UI patterns apply."""
    def __init__(self) -> None:
        self.started_at: float = time.time()
        self.running: bool = False           # True only while a scan is mid-flight
        self.last_scan_at: Optional[float] = None
        self.last_scan_duration_s: Optional[float] = None
        self.last_scan_devices: int = 0
        self.total_devices_seen: int = 0     # cumulative since process start
        self.scan_count: int = 0
        self.last_error: Optional[str] = None
        self.interface: Optional[str] = None
        self.iface_meta: Optional[dict] = None  # SSID/adapter info, optional

    def begin(self, interface: Optional[str] = None) -> None:
        self.running = True
        if interface is not None:
            self.interface = interface

    def end(self, devices: int, *, duration_s: float) -> None:
        self.running = False
        self.last_scan_at = time.time()
        self.last_scan_duration_s = duration_s
        self.last_scan_devices = devices
        self.total_devices_seen += devices
        self.scan_count += 1
        self.last_error = None

    def fail(self, err: str) -> None:
        self.running = False
        self.last_error = err

    def status(self) -> dict:
        return {
            "started_at": self.started_at,
            "running": self.running,
            "last_scan_at": self.last_scan_at,
            "last_scan_duration_s": self.last_scan_duration_s,
            "last_scan_devices": self.last_scan_devices,
            "total_devices_seen": self.total_devices_seen,
            "scan_count": self.scan_count,
            "last_error": self.last_error,
            "interface": self.interface,
            "iface_meta": self.iface_meta,
        }


class ScanOrchestrator:
    """Runs the GPS poll, location clustering, and periodic wifi/bt scans."""

    def __init__(self, gps: GPSService) -> None:
        self.gps = gps
        self._tasks: list[asyncio.Task] = []
        self._stop = asyncio.Event()
        # Runtime pause flag. When true, the scan loops and the probe
        # callback stay alive and tick on schedule but skip their work
        # — no new device upserts, no alert evaluation, and the GPS
        # clustering doesn't open new locations. Not persisted: a fresh
        # process always starts unpaused.
        self._paused: bool = False
        # When False, scanners still run + alerts still evaluate, but
        # device upserts and observation rows are suppressed. Used by
        # the Mission tab's "Skip recording" toggle for situations
        # where the operator wants alerting but no DB churn.
        self._recording: bool = True
        self.wifi_stats = ScannerStats()
        self.bt_stats = ScannerStats()
        # Bluetooth Classic stats live alongside BLE so the map sidebar
        # can render both with the same widget. Stays "idle" until the
        # user enables the Classic scanner from Settings.
        self.bt_classic_stats = ScannerStats()
        # Event signalled by the BLE loop when its scan count crosses the
        # configured N threshold — wakes the Classic loop early when the
        # user selected the "after_ble_scans" trigger mode. The Classic
        # loop waits on whichever fires first: this event, the fallback
        # interval, or the global stop.
        self._classic_trigger = asyncio.Event()
        self._ble_scans_since_classic = 0

    @property
    def paused(self) -> bool:
        return self._paused

    def set_paused(self, paused: bool) -> None:
        if self._paused == paused:
            return
        self._paused = paused
        log.info("orchestrator %s", "PAUSED" if paused else "resumed")

    @property
    def recording(self) -> bool:
        return self._recording

    def set_recording(self, recording: bool) -> None:
        if self._recording == recording:
            return
        self._recording = recording
        log.info(
            "orchestrator recording %s",
            "ON (writing rows)" if recording else "OFF (alerts only, no row writes)",
        )

    async def start(self) -> None:
        self._stop.clear()
        self._tasks = [
            asyncio.create_task(self._gps_loop()),
            asyncio.create_task(self._wifi_loop()),
            asyncio.create_task(self._bt_loop()),
            asyncio.create_task(self._bt_classic_loop()),
            asyncio.create_task(self._probe_loop()),
            asyncio.create_task(self._purge_loop()),
            asyncio.create_task(self._absence_loop()),
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
                if not self._paused:
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
                self.wifi_stats.interface = iface
                loc_id = location_manager.active_id
                if not self._paused and iface and loc_id is not None:
                    self.wifi_stats.begin(iface)
                    t0 = time.time()
                    try:
                        devs = await scan_wifi(iface)
                    except Exception:
                        self.wifi_stats.fail("scan_wifi raised")
                        raise
                    fix = self.gps.fix
                    kept = 0
                    for d in devs:
                        if d.rssi < s.min_rssi:
                            continue
                        details = d.model_dump(mode="json")
                        # Recording=off: skip the row writes but still
                        # evaluate alerts so the operator hears about
                        # matches without growing the DB. is_new is
                        # False in this path since we don't write the
                        # device row that would otherwise mark it new.
                        if self._recording:
                            is_new = await db.upsert_device(
                                location_id=loc_id, kind="wifi", device_id=d.bssid,
                                rssi=d.rssi, details=details,
                            )
                            await db.insert_observation(
                                location_id=loc_id, kind="wifi", device_id=d.bssid,
                                rssi=d.rssi, lat=fix.lat, lon=fix.lon,
                                raw=details,
                            )
                        else:
                            is_new = False
                        await alert_service.evaluate(
                            device_kind="wifi", device_id=d.bssid, rssi=d.rssi,
                            location_id=loc_id, details=details, is_new=is_new,
                            speed_mps=fix.speed,
                        )
                        kept += 1
                    self.wifi_stats.end(kept, duration_s=time.time() - t0)
            except Exception as e:
                log.exception("wifi loop error: %s", e)
                self.wifi_stats.fail(f"{type(e).__name__}: {e}")
            if not await self._sleep((await settings_store.load()).wifi_scan_interval_s):
                return

    async def _bt_loop(self) -> None:
        while not self._stop.is_set():
            try:
                s = await settings_store.load()
                self.bt_stats.interface = s.bluetooth_adapter or None
                loc_id = location_manager.active_id
                if not self._paused and loc_id is not None:
                    self.bt_stats.begin(s.bluetooth_adapter or None)
                    t0 = time.time()
                    try:
                        devs = await scan_bluetooth(s.bluetooth_adapter, s.bluetooth_scan_duration_s)
                    except Exception:
                        self.bt_stats.fail("scan_bluetooth raised")
                        raise
                    fix = self.gps.fix
                    kept = 0
                    for d in devs:
                        if d.rssi < s.min_rssi:
                            continue
                        if s.hide_random_bt_addresses and d.address_type == "random":
                            continue
                        details = d.model_dump(mode="json")
                        if self._recording:
                            is_new = await db.upsert_device(
                                location_id=loc_id, kind="bluetooth", device_id=d.address,
                                rssi=d.rssi, details=details,
                            )
                            await db.insert_observation(
                                location_id=loc_id, kind="bluetooth", device_id=d.address,
                                rssi=d.rssi, lat=fix.lat, lon=fix.lon,
                                raw=details,
                            )
                        else:
                            is_new = False
                        await alert_service.evaluate(
                            device_kind="bluetooth", device_id=d.address, rssi=d.rssi,
                            location_id=loc_id, details=details, is_new=is_new,
                            speed_mps=fix.speed,
                        )
                        kept += 1
                    self.bt_stats.end(kept, duration_s=time.time() - t0)
                    # Optional piggy-back trigger: in "after_ble_scans" mode
                    # the Classic inquiry runs once every N completed BLE
                    # scans. Signalling via an Event keeps the two loops
                    # decoupled — the Classic loop still owns its own
                    # task / stats / error handling.
                    if (s.bluetooth_classic_enabled
                            and s.bluetooth_classic_trigger == "after_ble_scans"):
                        self._ble_scans_since_classic += 1
                        n = max(1, int(s.bluetooth_classic_every_n_ble_scans or 1))
                        if self._ble_scans_since_classic >= n:
                            self._ble_scans_since_classic = 0
                            self._classic_trigger.set()
            except Exception as e:
                log.exception("bt loop error: %s", e)
                self.bt_stats.fail(f"{type(e).__name__}: {e}")
            if not await self._sleep((await settings_store.load()).bluetooth_scan_interval_s):
                return

    async def _bt_classic_loop(self) -> None:
        """Bluetooth Classic (BR/EDR) inquiry on its own cadence. Stays
        idle until `bluetooth_classic_enabled` flips on — Classic
        discovery only finds devices in pairing/discoverable mode, so
        running it constantly is mostly wasted cycles. When enabled, we
        run a short inquiry on the configured interval and upsert every
        device the controller reports."""
        while not self._stop.is_set():
            try:
                s = await settings_store.load()
                self.bt_classic_stats.interface = (
                    s.bluetooth_adapter or None
                )
                loc_id = location_manager.active_id
                if (not self._paused and s.bluetooth_classic_enabled
                        and loc_id is not None):
                    self.bt_classic_stats.begin(s.bluetooth_adapter or None)
                    t0 = time.time()
                    try:
                        devs = await scan_bluetooth_classic(
                            s.bluetooth_adapter,
                            s.bluetooth_classic_scan_duration_s,
                        )
                    except Exception:
                        self.bt_classic_stats.fail("scan_bluetooth_classic raised")
                        raise
                    fix = self.gps.fix
                    kept = 0
                    for d in devs:
                        if d.rssi < s.min_rssi:
                            continue
                        details = d.model_dump(mode="json")
                        if self._recording:
                            is_new = await db.upsert_device(
                                location_id=loc_id, kind="bluetooth_classic",
                                device_id=d.address, rssi=d.rssi, details=details,
                            )
                            await db.insert_observation(
                                location_id=loc_id, kind="bluetooth_classic",
                                device_id=d.address, rssi=d.rssi,
                                lat=fix.lat, lon=fix.lon, raw=details,
                            )
                        else:
                            is_new = False
                        await alert_service.evaluate(
                            device_kind="bluetooth_classic", device_id=d.address,
                            rssi=d.rssi, location_id=loc_id, details=details,
                            is_new=is_new, speed_mps=fix.speed,
                        )
                        kept += 1
                    self.bt_classic_stats.end(kept, duration_s=time.time() - t0)
            except Exception as e:
                log.exception("bt classic loop error: %s", e)
                self.bt_classic_stats.fail(f"{type(e).__name__}: {e}")
            # Wait until either:
            #   • the global stop fires (exit cleanly)
            #   • the configured fallback interval elapses
            #   • the BLE loop signals _classic_trigger (after-N-scans mode)
            # In "interval" mode the trigger never fires, so this collapses
            # to the existing interval sleep. In "after_ble_scans" mode the
            # interval acts as a safety cap — we fall back to running at the
            # interval if the BLE loop hasn't ticked enough scans in time.
            s = await settings_store.load()
            interval = s.bluetooth_classic_scan_interval_s
            stop_wait = asyncio.create_task(self._stop.wait())
            trig_wait = asyncio.create_task(self._classic_trigger.wait())
            try:
                done, _ = await asyncio.wait(
                    {stop_wait, trig_wait},
                    timeout=interval,
                    return_when=asyncio.FIRST_COMPLETED,
                )
            finally:
                for t in (stop_wait, trig_wait):
                    if not t.done():
                        t.cancel()
            self._classic_trigger.clear()
            if self._stop.is_set():
                return

    async def _purge_loop(self) -> None:
        """Periodically drop rows past the configured retention windows.
        Runs once at startup (after a small delay so init_db is settled)
        and then daily. No-op when both retention knobs are 0."""
        # Stagger the first run so it doesn't compete with startup.
        if not await self._sleep(60.0):
            return
        while not self._stop.is_set():
            try:
                s = await settings_store.load()
                obs_d = int(s.observation_retention_days or 0)
                dev_d = int(s.device_retention_days or 0)
                if obs_d > 0 or dev_d > 0:
                    counts = await db.purge_old_data(
                        observation_days=obs_d, device_days=dev_d,
                    )
                    if counts.get("observations") or counts.get("devices"):
                        log.info(
                            "auto-purge dropped %d observations + %d devices "
                            "(retention obs=%dd dev=%dd)",
                            counts.get("observations", 0),
                            counts.get("devices", 0), obs_d, dev_d,
                        )
            except Exception as e:
                log.exception("purge loop error: %s", e)
            # 24h between sweeps. Cancellation-aware sleep so stop() returns fast.
            if not await self._sleep(24 * 3600):
                return

    async def _absence_loop(self) -> None:
        """Drive the absence_gap rule type. Absence isn't observable from
        a single sighting — it's defined by the *lack* of one — so a
        sighting-driven evaluate() can't catch it. This loop polls every
        ~30s and asks alert_service to check every enabled absence_gap
        rule against the current `devices` table. Cheap: bounded by the
        number of absence rules × matching devices, and the DB query is
        indexed on last_seen."""
        # Stagger the first run so it doesn't compete with startup.
        if not await self._sleep(20.0):
            return
        while not self._stop.is_set():
            try:
                if not self._paused:
                    await alert_service.check_absence_rules()
                    # sustained_presence flip-flops also need a non-sighting
                    # tick to fire the 'absent' transition — same cadence,
                    # same gating (paused = quiet, exception = isolated).
                    await alert_service.check_presence_transitions()
            except Exception as e:
                log.exception("absence loop error: %s", e)
            if not await self._sleep(30.0):
                return

    async def _probe_loop(self) -> None:
        """Watches probe-scanner settings; starts/stops/switches the
        capture (tshark or scapy backend, optional auto-monitor and
        channel hopping) as needed. Doesn't poll for probes itself —
        the scanner pushes them via the callback below."""
        while not self._stop.is_set():
            try:
                s = await settings_store.load()
                want_iface = (s.probe_interface or "").strip() or None
                want_backend = s.probe_backend
                want_auto = s.probe_auto_monitor
                want_channels = parse_channels(s.probe_channels)
                want_hop_ms = int(getattr(s, "probe_channel_hop_ms", 100) or 100)
                if want_iface:
                    cur = (
                        probe_scanner.interface,
                        probe_scanner.backend,
                        probe_scanner.auto_monitor,
                        probe_scanner.channels,
                        getattr(probe_scanner, "_hop_ms", 100),
                    ) if probe_scanner.running else (None, None, None, [], 0)
                    target = (want_iface, want_backend, want_auto, want_channels, want_hop_ms)
                    if cur != target:
                        await probe_scanner.start(
                            want_iface, self._on_probe,
                            backend=want_backend,
                            auto_monitor=want_auto,
                            channels=want_channels,
                            hop_ms=want_hop_ms,
                        )
                elif probe_scanner.running:
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
        if self._paused:
            return
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
        frame_type = probe.get("frame_type") or "probe"
        new_bssid = (probe.get("bssid") or "").strip().lower()

        # Merge with any existing details so the SSID list and channels
        # accumulate across observations of the same client. associated_bssids
        # is the AP MAC(s) the client has sent association / reassociation
        # requests to — strong evidence of an actual connection, where the
        # probed-SSIDs list is only "wanted to find this network".
        prior = await db.get_device_details(loc_id, "wifi_client", mac) or {}
        ssids = list(prior.get("ssids") or [])
        if new_ssid and new_ssid not in ssids:
            ssids.append(new_ssid)
        channels = list(prior.get("channels") or [])
        if channel is not None and channel not in channels:
            channels.append(channel)
        associated_bssids = list(prior.get("associated_bssids") or [])
        if (
            frame_type in ("assoc", "reassoc")
            and new_bssid
            and new_bssid not in associated_bssids
        ):
            associated_bssids.append(new_bssid)
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
            "associated_bssids": associated_bssids,
            "randomized": probe.get("randomized", False),
        }

        fix = self.gps.fix
        if self._recording:
            is_new = await db.upsert_device(
                location_id=loc_id, kind="wifi_client", device_id=mac,
                rssi=rssi, details=details,
            )
            await db.insert_observation(
                location_id=loc_id, kind="wifi_client", device_id=mac,
                rssi=rssi, lat=fix.lat, lon=fix.lon,
                raw={
                    **details,
                    "ssid": new_ssid, "channel": channel,
                    "frame_type": frame_type,
                    "bssid": new_bssid or None,
                },
            )
        else:
            is_new = False
        # `_last_ssid` is the SSID captured in *this* probe (not the
        # accumulated list). Used by the wifi_association rule to fire
        # only when a client probes for a specific network on this scan,
        # without treating every subsequent sighting as a re-match just
        # because the SSID is still in the device's history.
        eval_details = {**details, "_last_ssid": new_ssid}
        await alert_service.evaluate(
            device_kind="wifi_client", device_id=mac, rssi=rssi,
            location_id=loc_id, details=eval_details, is_new=is_new,
            speed_mps=fix.speed,
        )


orchestrator: Optional[ScanOrchestrator] = None
