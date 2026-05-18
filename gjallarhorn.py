from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

import database as db
from config import settings_store
from models import AppSettings
from services.gps_service import GPSService
from services.wifi_scanner import list_wifi_interfaces, list_wifi_interface_info
from services.bluetooth_scanner import list_bluetooth_adapters, list_bluetooth_adapter_info
from services.scan_orchestrator import ScanOrchestrator
from services.location_manager import location_manager
from services.oui import oui_service
from services.alert_service import alert_service

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
for _noisy in ("uvicorn", "uvicorn.error", "uvicorn.access", "fastapi"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)

# In-memory log capture for the Logs tab. Installed before any module-level
# logging happens so we don't lose the first few "service starting" lines.
from services.log_buffer import install as _install_log_buffer  # noqa: E402
_install_log_buffer()

log = logging.getLogger("gjallarhorn")

ROOT = Path(__file__).parent
STATIC_DIR = ROOT / "static"

# Process-start timestamp for the About tab's "Uptime" field. Captured at
# import time so even API calls that arrive before the lifespan startup
# completes get a sensible value.
APP_STARTED_AT = time.time()

gps: Optional[GPSService] = None
orchestrator: Optional[ScanOrchestrator] = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global gps, orchestrator
    await db.init_db()
    s = await settings_store.load()
    gps = GPSService(host=s.gpsd_host, port=s.gpsd_port, poll_s=s.gps_poll_interval_s)
    await gps.start()
    await oui_service.ensure_loaded()
    await alert_service.load_rules()
    await alert_service.load_whitelist()
    await alert_service.load_latches()
    await alert_service.load_presence_state()
    orchestrator = ScanOrchestrator(gps)
    await orchestrator.start()
    log.info("Gjallarhorn started")
    try:
        yield
    finally:
        if orchestrator:
            await orchestrator.stop()
        if gps:
            await gps.stop()
        log.info("Gjallarhorn stopped")


app = FastAPI(title="Gjallarhorn", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


# ---------- GPS ----------
@app.get("/api/gps")
async def api_gps():
    if gps is None:
        raise HTTPException(503, "GPS not initialized")
    return {
        "connected": gps.connected,
        "fix": gps.fix.model_dump(mode="json"),
        "active_location_id": location_manager.active_id,
    }


# ---------- Settings ----------
@app.get("/api/settings", response_model=AppSettings)
async def api_get_settings() -> AppSettings:
    return await settings_store.load()


@app.put("/api/settings", response_model=AppSettings)
async def api_put_settings(payload: dict) -> AppSettings:
    new = await settings_store.patch(payload)
    if gps is not None:
        gps.host = new.gpsd_host
        gps.port = new.gpsd_port
        gps.poll_s = new.gps_poll_interval_s
    return new


@app.post("/api/settings/notifications/discord/test")
async def api_test_discord_webhook():
    """Post a synthetic embed to the configured webhook so the user can verify it works."""
    from services.alert_service import build_discord_payload, _post_webhook_sync
    import asyncio

    s = await settings_store.load()
    url = (s.discord_webhook_url or "").strip()
    if not url:
        raise HTTPException(400, "No Discord webhook URL configured")

    payload = build_discord_payload(
        rule={"id": 0, "name": "Test alert", "match_type": "device_id"},
        device_kind="wifi", device_id="aa:bb:cc:dd:ee:ff", rssi=-42,
        location_id=None, details={"ssid": "TestNetwork", "vendor": "Acme Inc."},
        username=s.discord_username,
    )
    try:
        await asyncio.to_thread(_post_webhook_sync, url, payload)
    except Exception as e:
        raise HTTPException(502, f"Webhook delivery failed: {e}")
    return {"ok": True}


# ---------- Logs ----------
_LEVEL_NAME_TO_NO = {
    "DEBUG": logging.DEBUG,
    "INFO": logging.INFO,
    "WARNING": logging.WARNING,
    "ERROR": logging.ERROR,
    "CRITICAL": logging.CRITICAL,
}


@app.get("/api/logs")
async def api_logs(since_id: int = 0, level: str = "INFO", limit: int = 500):
    from services.log_buffer import log_buffer
    level_no = _LEVEL_NAME_TO_NO.get(level.upper(), logging.INFO)
    return {
        "entries": log_buffer.get(since_id=since_id, min_level_no=level_no, limit=limit),
        "stats": log_buffer.stats(),
    }


@app.delete("/api/logs")
async def api_logs_clear():
    from services.log_buffer import log_buffer
    return {"cleared": log_buffer.clear()}


# ---------- Device whitelist ----------
@app.get("/api/whitelist")
async def api_list_whitelist():
    """Whitelist entries enriched with aggregate info from matching device
    rows so the Settings UI can show vendor/name/last-seen/etc. The slim
    list_whitelist (without the joins) is what alert_service and report
    use for their hot-path matchers."""
    return {"entries": await db.list_whitelist_with_devices()}


@app.post("/api/whitelist")
async def api_add_whitelist(payload: dict):
    """Add (or upsert) a whitelist entry. Body: {kind, device_id, note?}.
    The device_id is matched as either an exact value or a prefix at
    evaluation time, so 'aa:bb:cc' silences a whole OUI."""
    kind = payload.get("kind")
    if kind not in ALLOWED_KINDS or kind is None:
        raise HTTPException(400, "kind must be wifi, bluetooth, or wifi_client")
    device_id = (payload.get("device_id") or "").strip()
    if not device_id:
        raise HTTPException(400, "device_id required")
    note = payload.get("note")
    if note is not None:
        note = str(note).strip() or None
    entry_id = await db.add_whitelist(kind, device_id, note)
    await alert_service.reload()
    return {"id": entry_id}


@app.patch("/api/whitelist/{entry_id}")
async def api_update_whitelist(entry_id: int, payload: dict):
    """Edit an existing whitelist row in place. Body shape matches POST:
    {kind, device_id, note?}. 409 if (kind, device_id) collides with a
    different existing entry."""
    kind = payload.get("kind")
    if kind not in ALLOWED_KINDS or kind is None:
        raise HTTPException(400, "kind must be wifi, bluetooth, or wifi_client")
    device_id = (payload.get("device_id") or "").strip()
    if not device_id:
        raise HTTPException(400, "device_id required")
    note = payload.get("note")
    if note is not None:
        note = str(note).strip() or None
    try:
        ok = await db.update_whitelist(entry_id, kind, device_id, note)
    except ValueError as e:
        raise HTTPException(409, str(e))
    if not ok:
        raise HTTPException(404, "entry not found")
    await alert_service.reload()
    return {"ok": True}


@app.delete("/api/whitelist/{entry_id}")
async def api_delete_whitelist(entry_id: int):
    ok = await db.delete_whitelist(entry_id)
    if not ok:
        raise HTTPException(404, "entry not found")
    await alert_service.reload()
    return {"ok": True}


@app.delete("/api/whitelist")
async def api_clear_whitelist():
    """Drop every entry from the permanent whitelist. The temp /
    silenced list is untouched (it has its own DELETE endpoint). Used
    by the Settings panel's bulk-delete icon."""
    n = await db.clear_whitelist()
    if n:
        await alert_service.reload()
        log.info("Cleared whitelist: %d entries removed", n)
    return {"ok": True, "removed": n}


@app.get("/api/preserved-devices")
async def api_list_preserved_devices(kind: Optional[str] = None):
    """Whitelisted device sightings archived from deleted locations."""
    return {"devices": await db.list_preserved_devices(kind)}


@app.get("/api/devices/{kind}/{device_id}/timeline")
async def api_device_timeline(kind: str, device_id: str):
    """Per-device history: location summary, recent observations sample,
    and aggregate stats. Powers the Devices-tab timeline modal."""
    if kind not in ALLOWED_KINDS:
        raise HTTPException(400, "unknown kind")
    return await db.device_timeline(kind, device_id)


@app.delete("/api/preserved-devices")
async def api_clear_preserved_devices():
    n = await db.clear_preserved_devices()
    return {"ok": True, "cleared": n}


# ---------- Probe scanner ----------
@app.get("/api/probe/status")
async def api_probe_status():
    from services.probe_scanner import probe_scanner
    return probe_scanner.status()


@app.get("/api/scanners/status")
async def api_scanners_status():
    """Wifi + bluetooth scan-loop runtime stats — last scan time, duration,
    device counts, errors. Surfaced on the map sidebar so users can see at
    a glance whether the scanners are working."""
    s = await settings_store.load()
    out = {
        "paused": orchestrator.paused if orchestrator else False,
        "wifi": {
            **(orchestrator.wifi_stats.status() if orchestrator else {}),
            "scan_interval_s": s.wifi_scan_interval_s,
            "configured_iface": s.wifi_interface,
        },
        "bluetooth": {
            **(orchestrator.bt_stats.status() if orchestrator else {}),
            "scan_interval_s": s.bluetooth_scan_interval_s,
            "scan_duration_s": s.bluetooth_scan_duration_s,
            "configured_adapter": s.bluetooth_adapter,
            # Bluetooth Classic (BR/EDR) is a separate scanner that runs
            # against the same adapter. Surfaced under the BLE card on the
            # map so operators can see at a glance which transport just
            # scanned — both modes are unified into one Bluetooth panel.
            "classic_enabled": s.bluetooth_classic_enabled,
            "classic_running": (
                orchestrator.bt_classic_stats.running if orchestrator else False
            ),
            "classic_scan_interval_s": s.bluetooth_classic_scan_interval_s,
            "classic_scan_duration_s": s.bluetooth_classic_scan_duration_s,
            "classic_trigger": s.bluetooth_classic_trigger,
            "classic_every_n_ble_scans": s.bluetooth_classic_every_n_ble_scans,
            "classic_last_scan_at": (
                orchestrator.bt_classic_stats.last_scan_at if orchestrator else None
            ),
            "classic_last_scan_devices": (
                orchestrator.bt_classic_stats.last_scan_devices if orchestrator else 0
            ),
            "classic_total_devices_seen": (
                orchestrator.bt_classic_stats.total_devices_seen if orchestrator else 0
            ),
        },
    }
    return out


# ---------- Pause / resume ----------
@app.get("/api/system/pause")
async def api_pause_status():
    return {"paused": orchestrator.paused if orchestrator else False}


@app.post("/api/system/pause")
async def api_set_pause(payload: dict | None = None):
    """Toggle or set the pause flag. Body may contain {paused: bool};
    if absent, toggles the current state."""
    if orchestrator is None:
        raise HTTPException(503, "orchestrator not running")
    if payload is None or "paused" not in payload:
        target = not orchestrator.paused
    else:
        target = bool(payload.get("paused"))
    orchestrator.set_paused(target)
    return {"paused": orchestrator.paused}


@app.get("/api/system/recording")
async def api_recording_status():
    """Returns whether the orchestrator is currently writing device /
    observation rows. False means scans + alert evaluation are still
    running, but no new rows land in the DB — the Mission tab's
    'Skip recording' toggle controls this."""
    return {"recording": orchestrator.recording if orchestrator else True}


@app.post("/api/system/recording")
async def api_set_recording(payload: dict | None = None):
    """Toggle or set the recording flag. Body: {recording: bool}; absent
    toggles. Off = orchestrator skips upsert_device / insert_observation
    while still calling alert_service.evaluate — alert rules that are
    purely value-based fire fine; stateful rules (cross_location,
    persistent_companion, arrival_after_gap, absence_gap) freeze on
    whatever DB state existed when recording was paused."""
    if orchestrator is None:
        raise HTTPException(503, "orchestrator not running")
    if payload is None or "recording" not in payload:
        target = not orchestrator.recording
    else:
        target = bool(payload.get("recording"))
    orchestrator.set_recording(target)
    return {"recording": orchestrator.recording}


# ---------- About ----------
@app.get("/api/about")
async def api_about():
    """Self-description for the About tab: build info (git branch / commit),
    runtime uptime, aggregate DB counts. The git block reuses the updater
    service's status probe so we don't duplicate the rev-parse pipeline."""
    from services import updater
    try:
        upd = await updater.get_status(do_fetch=False)
    except Exception as e:
        upd = {"ok": False, "error": str(e)}
    build: dict = {
        "branch": upd.get("branch") if upd.get("ok") else None,
        "current": upd.get("current") if upd.get("ok") else None,
        "remote_url": upd.get("remote_url") if upd.get("ok") else None,
        "dirty": bool(upd.get("dirty")) if upd.get("ok") else False,
    }
    stats = await db.about_stats()
    return {
        "app": "Gjallarhorn",
        "tagline": "Field sensor for detecting devices that follow you.",
        "platform": {
            "python": __import__("platform").python_version(),
            "system": __import__("platform").system(),
            "release": __import__("platform").release(),
        },
        "runtime": {
            "started_at_unix": APP_STARTED_AT,
            "uptime_seconds": max(0.0, time.time() - APP_STARTED_AT),
            "server_time": datetime.now().isoformat(timespec="seconds"),
        },
        "build": build,
        "stats": stats,
    }


# ---------- Self-update ----------
@app.get("/api/system/update/status")
async def api_update_status():
    from services import updater
    return await updater.get_status(do_fetch=False)


@app.post("/api/system/update/check")
async def api_update_check():
    from services import updater
    return await updater.get_status(do_fetch=True)


@app.post("/api/system/update/apply")
async def api_update_apply(payload: dict | None = None):
    """Fast-forward pull from origin and (by default) restart the process.

    If the pull would change `requirements.txt`, the caller must include
    `"acknowledge_requirements_change": true` in the payload — otherwise
    we return 412 so a UI can re-prompt rather than silently breaking
    deps."""
    from services import updater
    payload = payload or {}
    restart = bool(payload.get("restart", True))
    ack = bool(payload.get("acknowledge_requirements_change", False))
    try:
        result = await updater.apply_update(acknowledge_requirements_change=ack)
    except updater.RequirementsChangedError as e:
        raise HTTPException(
            status_code=412,
            detail={
                "error": str(e),
                "requirements_changed": True,
                "needs_acknowledgement": True,
            },
        )
    except updater.GitError as e:
        raise HTTPException(409, str(e))
    if result.get("updated") and restart:
        updater.schedule_restart()
        result["restarting"] = True
    return result


@app.post("/api/system/restart")
async def api_system_restart():
    from services import updater
    updater.schedule_restart()
    return {"ok": True, "restarting": True}


# ---------- Interfaces / adapters ----------
@app.get("/api/interfaces/wifi")
async def api_wifi_interfaces():
    """Rich info per wireless interface, incl. associated SSID if any."""
    return {"interfaces": await list_wifi_interface_info()}


@app.get("/api/interfaces/wifi/names")
async def api_wifi_interface_names():
    return {"interfaces": await list_wifi_interfaces()}


@app.get("/api/interfaces/wifi/{iface}/channels")
async def api_wifi_interface_channels(iface: str):
    """Channels supported by a wireless interface, derived from `iw phy info`."""
    from services.wifi_scanner import list_interface_channels
    return {"channels": await list_interface_channels(iface)}


@app.get("/api/interfaces/bluetooth")
async def api_bt_adapters():
    """Rich info per BlueZ adapter (address, powered, discovering, etc.)."""
    return {"adapters": await list_bluetooth_adapter_info()}


@app.get("/api/interfaces/bluetooth/names")
async def api_bt_adapter_names():
    return {"adapters": await list_bluetooth_adapters()}


# ---------- Map providers ----------
@app.get("/api/map_providers")
async def api_map_providers():
    return {
        "providers": {
            "osm": {
                "name": "OpenStreetMap",
                "url": "https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png",
                "attribution": "© OpenStreetMap contributors",
                "max_zoom": 19,
            },
            "osm_topo": {
                "name": "OpenTopoMap",
                "url": "https://{s}.tile.opentopomap.org/{z}/{x}/{y}.png",
                "attribution": "© OpenStreetMap, SRTM | © OpenTopoMap (CC-BY-SA)",
                "max_zoom": 17,
            },
            "carto_positron": {
                "name": "Carto Positron (light)",
                "url": "https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}.png",
                "attribution": "© OpenStreetMap, © CARTO",
                "max_zoom": 19,
            },
            "carto_dark": {
                "name": "Carto Dark Matter",
                "url": "https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}.png",
                "attribution": "© OpenStreetMap, © CARTO",
                "max_zoom": 19,
            },
            "stamen_terrain": {
                "name": "Stamen Terrain",
                "url": "https://tiles.stadiamaps.com/tiles/stamen_terrain/{z}/{x}/{y}{r}.png",
                "attribution": "© Stadia Maps, © Stamen Design, © OSM",
                "max_zoom": 18,
            },
            "esri_satellite": {
                "name": "Esri Satellite",
                "url": "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
                "attribution": "Tiles © Esri",
                "max_zoom": 19,
            },
            "google_roadmap": {
                "name": "Google Roadmap",
                "url": "https://mt{s}.google.com/vt/lyrs=m&x={x}&y={y}&z={z}",
                "attribution": "Map data © Google",
                "max_zoom": 20,
                "subdomains": "0123",
            },
            "google_satellite": {
                "name": "Google Satellite",
                "url": "https://mt{s}.google.com/vt/lyrs=s&x={x}&y={y}&z={z}",
                "attribution": "Imagery © Google",
                "max_zoom": 20,
                "subdomains": "0123",
            },
            "google_hybrid": {
                "name": "Google Hybrid",
                "url": "https://mt{s}.google.com/vt/lyrs=y&x={x}&y={y}&z={z}",
                "attribution": "Imagery © Google",
                "max_zoom": 20,
                "subdomains": "0123",
            },
            "google_terrain": {
                "name": "Google Terrain",
                "url": "https://mt{s}.google.com/vt/lyrs=p&x={x}&y={y}&z={z}",
                "attribution": "Map data © Google",
                "max_zoom": 20,
                "subdomains": "0123",
            },
        }
    }


# ---------- Locations + devices ----------
@app.get("/api/locations")
async def api_locations():
    return {"locations": await db.list_locations(), "active_id": location_manager.active_id}


@app.patch("/api/locations/{loc_id}")
async def api_patch_location(loc_id: int, payload: dict):
    """Update a location's label and/or its radius. Both fields are
    optional but at least one is required. Radius edits are only honored
    for drawn (manual) geofences — auto-clustered locations have their
    radius governed by the clustering tunables and would just be
    overwritten on the next fix."""
    did_something = False
    if "label" in payload:
        label = (payload.get("label") or "").strip()
        if not label:
            raise HTTPException(400, "label cannot be empty")
        await db.update_location_label(loc_id, label)
        did_something = True
    if "radius_m" in payload:
        raw = payload.get("radius_m")
        if raw is None:
            raise HTTPException(400, "radius_m must be a number")
        try:
            radius_m = float(raw)
        except (TypeError, ValueError):
            raise HTTPException(400, "radius_m must be a number")
        if not (1 <= radius_m <= 100000):
            raise HTTPException(400, "radius_m must be between 1 and 100000")
        ok = await db.update_location_radius(loc_id, radius_m)
        if not ok:
            raise HTTPException(
                400, "radius_m can only be changed on drawn (manual) geofences",
            )
        did_something = True
    if not did_something:
        raise HTTPException(400, "label or radius_m required")
    return {"ok": True}


@app.get("/api/locations/{loc_id}/devices")
async def api_location_devices(loc_id: int, kind: Optional[str] = None):
    return {"devices": await db.devices_at_location(loc_id, kind)}


@app.get("/api/wifi/aps")
async def api_wifi_aps(location_id: Optional[int] = None):
    """WiFi APs (kind='wifi') grouped by SSID, with the wifi_client
    probes that named each SSID attached as 'associated' clients.

    The 'associated' link is best-effort — we don't capture 802.11
    association frames, only probe requests, so a client lands under
    an AP whenever its probed-SSID list contains the AP's SSID. Useful
    enough to answer "which devices have been looking for this network"
    in one place. Optional location_id filter narrows both APs and
    probes to that bubble."""
    return {"aps": await db.wifi_aps_grouped(location_id=location_id)}


@app.delete("/api/locations/{loc_id}/devices")
async def api_delete_location_devices(loc_id: int):
    """Wipe every device + observation at this location while keeping
    the location row itself. Whitelisted devices are archived to
    preserved_devices first."""
    counts = await db.delete_devices_at_location(loc_id)
    if not counts.get("found"):
        raise HTTPException(404, "location not found")
    log.info("Cleared devices at location %d: %s", loc_id, counts)
    return {"ok": True, "deleted": counts}


async def _disable_broken_rules_after_location_delete() -> list[dict]:
    """Sweep alert rules for now-dangling location_id references, disable
    them, and reload the alert service so the orchestrator stops trying
    to evaluate them. Logs each rule it touches so the operator can find
    out from the log what got disabled without inspecting the table."""
    disabled = await db.disable_rules_with_missing_location()
    for r in disabled:
        log.warning(
            "Alert rule #%s '%s' disabled: %s",
            r["id"], r["name"], r["disabled_reason"],
        )
    if disabled:
        await alert_service.reload()
    return disabled


@app.delete("/api/locations/{loc_id}")
async def api_delete_location(loc_id: int):
    """Delete one location and its associated devices/observations.
    If it's the active location, clear the location_manager pointer so
    the next GPS fix opens a fresh one."""
    counts = await db.delete_location(loc_id)
    if counts.get("locations", 0) == 0:
        raise HTTPException(404, "location not found")
    if location_manager.active_id == loc_id:
        location_manager._active_id = None  # type: ignore[attr-defined]
        location_manager._active_lat = None  # type: ignore[attr-defined]
        location_manager._active_lon = None  # type: ignore[attr-defined]
    log.info("Deleted location %d: %s", loc_id, counts)
    disabled = await _disable_broken_rules_after_location_delete()
    if disabled:
        counts["rules_disabled"] = disabled
    return {"ok": True, "deleted": counts}


@app.post("/api/locations/draw")
async def api_draw_location(payload: dict):
    """Create a user-drawn geofence (source='manual'). Body:
    {lat, lon, radius_m, label?}. Manual locations are protected from
    being merged away and keep their drawn radius even when other
    locations get folded into them."""
    raw_lat, raw_lon, raw_r = payload.get("lat"), payload.get("lon"), payload.get("radius_m")
    if raw_lat is None or raw_lon is None or raw_r is None:
        raise HTTPException(400, "lat, lon, radius_m are required")
    try:
        lat = float(raw_lat)
        lon = float(raw_lon)
        radius_m = float(raw_r)
    except (TypeError, ValueError):
        raise HTTPException(400, "lat, lon, radius_m must all be numbers")
    if not (-90 <= lat <= 90) or not (-180 <= lon <= 180):
        raise HTTPException(400, "lat/lon out of range")
    if not (1 <= radius_m <= 100000):
        raise HTTPException(400, "radius_m must be between 1 and 100000")
    label = (payload.get("label") or "").strip() or None
    new_id = await db.create_location(
        lat=lat, lon=lon, radius_m=radius_m, label=label, source="manual",
    )
    if label is None:
        # Fall back to the same template as auto-created locations so the
        # entry is at least labelled in the table.
        s = await settings_store.load()
        await db.update_location_label(
            new_id, s.location_label_template.format(id=new_id, lat=lat, lon=lon),
        )
    log.info("Drew manual geofence id=%s @ %.6f,%.6f r=%.1fm",
             new_id, lat, lon, radius_m)
    return {"ok": True, "id": new_id}


@app.get("/api/locations/contained")
async def api_locations_contained_preview():
    """List the (loser, winner) pairs that auto_merge_contained would merge.
    Pure preview — no mutation."""
    return {"pairs": await db.find_contained_locations()}


@app.post("/api/locations/merge_contained")
async def api_merge_contained_locations():
    """Merge every location whose centroid is inside another location's
    radius into its container. Iterative — chains collapse to the
    outermost survivor. The active-location pointer follows the merge."""
    result = await db.auto_merge_contained()
    if location_manager.active_id in result.get("loser_ids", []):
        # The active loc was merged away — clear so the next GPS fix
        # snaps onto whichever surviving location now contains it.
        location_manager._active_id = None  # type: ignore[attr-defined]
        location_manager._active_lat = None  # type: ignore[attr-defined]
        location_manager._active_lon = None  # type: ignore[attr-defined]
    if result["merged"]:
        log.info("Merged %d contained locations: %s",
                 result["merged"], result["loser_ids"])
    disabled = await _disable_broken_rules_after_location_delete()
    if disabled:
        result["rules_disabled"] = disabled
    return {"ok": True, **result}


@app.post("/api/locations/{loser_id}/merge_into/{winner_id}")
async def api_merge_into(loser_id: int, winner_id: int):
    """Merge `loser_id` into `winner_id`. Used by the map's per-bubble
    "Merge into…" action — bypasses the contained-pair finder so the user
    can fold any two locations together, not just overlapping ones."""
    if loser_id == winner_id:
        raise HTTPException(400, "loser and winner must be different")
    result = await db.merge_locations(loser_id, winner_id)
    if result.get("error"):
        raise HTTPException(400, result["error"])
    if result.get("noop"):
        raise HTTPException(404, "one or both locations no longer exist")
    if location_manager.active_id == loser_id:
        # The active loc was merged away — clear so the next GPS fix snaps
        # onto whichever surviving location now contains it (same handling
        # the auto-merge endpoints do).
        location_manager._active_id = None  # type: ignore[attr-defined]
        location_manager._active_lat = None  # type: ignore[attr-defined]
        location_manager._active_lon = None  # type: ignore[attr-defined]
    log.info("Manual merge: location %d into %d", loser_id, winner_id)
    disabled = await _disable_broken_rules_after_location_delete()
    if disabled:
        result["rules_disabled"] = disabled
    return {"ok": True, **result}


@app.post("/api/locations/merge_contained/step")
async def api_merge_contained_step():
    """One iteration of the merge_contained loop — merges a single pair
    if any qualifies, otherwise returns done=True. Designed for UIs that
    want to show progress per merge instead of waiting on the bulk
    endpoint to return."""
    pairs = await db.find_contained_locations()
    if not pairs:
        return {"ok": True, "done": True, "merged": None, "remaining": 0}
    p = pairs[0]
    merged = await db.merge_locations(p["loser_id"], p["winner_id"])
    if location_manager.active_id == p["loser_id"]:
        location_manager._active_id = None  # type: ignore[attr-defined]
        location_manager._active_lat = None  # type: ignore[attr-defined]
        location_manager._active_lon = None  # type: ignore[attr-defined]
    log.info("Merged location %d into %d (step)", p["loser_id"], p["winner_id"])
    disabled = await _disable_broken_rules_after_location_delete()
    if disabled:
        merged["rules_disabled"] = disabled
    # `remaining` is the count *before* this step's merge, minus 1 for the
    # pair we just collapsed. The next find_contained_locations may reveal
    # more or fewer than that — UIs should treat it as a hint, not a total.
    return {
        "ok": True, "done": False, "merged": merged,
        "remaining": max(0, len(pairs) - 1),
    }


@app.post("/api/maintenance/purge")
async def api_purge_old_data(payload: dict | None = None):
    """Manually run the retention purge using the configured (or
    overridden) thresholds. Body may contain {observation_days, device_days}
    to override the saved settings for this one call (e.g. dry-run-ish,
    user-initiated cleanup)."""
    s = await settings_store.load()
    payload = payload or {}
    obs_d = int(payload.get("observation_days", s.observation_retention_days or 0))
    dev_d = int(payload.get("device_days", s.device_retention_days or 0))
    counts = await db.purge_old_data(
        observation_days=obs_d, device_days=dev_d,
    )
    log.info("manual purge: obs_d=%d dev_d=%d removed=%s", obs_d, dev_d, counts)
    return {"ok": True, "removed": counts,
            "observation_days": obs_d, "device_days": dev_d}


# ---------- Missions ----------
@app.get("/api/missions")
async def api_list_missions(limit: int = 50):
    return {"missions": await db.list_missions(limit=limit)}


@app.get("/api/missions/active")
async def api_active_mission():
    return {"mission": await db.active_mission()}


@app.post("/api/missions")
async def api_start_mission(payload: dict | None = None):
    """Open a new mission. Rejects if another is already active."""
    payload = payload or {}
    name = (payload.get("name") or "").strip() or "Mission"
    description = payload.get("description")
    out = await db.create_mission(name, description)
    if isinstance(out, dict) and out.get("error"):
        raise HTTPException(409, out["error"])
    log.info("Mission started: id=%s name=%s", out.get("id"), out.get("name"))
    return {"mission": out}


@app.post("/api/missions/{mission_id}/end")
async def api_end_mission(mission_id: int):
    out = await db.end_mission(mission_id)
    if out is None:
        raise HTTPException(404, "mission not found or already ended")
    log.info("Mission ended: id=%s", mission_id)
    return {"mission": out}


@app.get("/api/missions/{mission_id}")
async def api_get_mission(mission_id: int):
    out = await db.get_mission(mission_id)
    if out is None:
        raise HTTPException(404, "mission not found")
    return {"mission": out}


@app.delete("/api/missions/{mission_id}")
async def api_delete_mission(mission_id: int):
    """Delete a mission audit row. Does NOT touch the captured data."""
    ok = await db.delete_mission(mission_id)
    if not ok:
        raise HTTPException(404, "mission not found")
    return {"ok": True}


# ---------- Maintenance: per-kind delete, integrity, DB backup ----------
@app.post("/api/maintenance/delete-kind")
async def api_delete_kind(payload: dict):
    """Wipe every device + observation of one kind. Whitelisted devices
    follow the same preservation path as location deletion (archived to
    preserved_devices first)."""
    kind = (payload or {}).get("kind")
    if kind not in ("wifi", "bluetooth", "bluetooth_classic", "wifi_client"):
        raise HTTPException(400, "kind must be wifi / bluetooth / bluetooth_classic / wifi_client")
    out = await db.delete_devices_by_kind(kind)
    log.info("Per-kind delete: %s -> %s", kind, out)
    return out


@app.post("/api/maintenance/integrity")
async def api_integrity_check():
    return await db.integrity_check()


@app.get("/api/maintenance/db/export")
async def api_db_export():
    """Stream the live SQLite file as a download. Runs
    `PRAGMA wal_checkpoint(TRUNCATE)` first so every committed write
    sitting in the -wal sidecar gets folded into the main file before
    we hand the bytes to the client — otherwise the exported .db can
    miss recent rows that haven't checkpointed yet."""
    if not db.DB_PATH.exists():
        raise HTTPException(404, "database file not found")
    # TRUNCATE mode also resets the WAL file to zero bytes after the
    # checkpoint, which is harmless under load (a fresh WAL is created
    # on the next write) and keeps the backup snapshot self-contained
    # without needing to grab the .db-wal alongside.
    try:
        async with db._connect() as conn:
            await conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            await conn.commit()
    except Exception as e:
        # Don't fail the export if checkpoint can't run (e.g. another
        # connection is holding a read lock); the backup still works,
        # it just may lag the WAL tip slightly. Log so the operator
        # knows.
        log.warning("db export: wal_checkpoint failed (%s) — exporting anyway", e)
    return FileResponse(
        path=str(db.DB_PATH),
        filename=f"gjallarhorn-{datetime.now().strftime('%Y%m%d-%H%M%S')}.db",
        media_type="application/octet-stream",
    )


@app.post("/api/maintenance/db/import")
async def api_db_import(file: UploadFile = File(...)):
    """Replace the live SQLite file with an uploaded one. Backs up the
    current file alongside (with `.bak-TIMESTAMP` suffix) before
    overwriting, and stops the scan orchestrator while the swap
    happens so no writes race against the replace. Caller must restart
    the process for the new DB to take effect — the in-memory
    connections in alert_service / settings_store cache schema state
    from boot."""
    raw = await file.read()
    if not raw or raw[:16] != b"SQLite format 3\x00":
        raise HTTPException(400, "uploaded file is not a SQLite database (magic header missing)")
    backup_path = db.DB_PATH.with_suffix(
        db.DB_PATH.suffix + f".bak-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    )
    if orchestrator is not None:
        try:
            await orchestrator.stop()
        except Exception as e:
            log.warning("orchestrator.stop() failed during db import: %s", e)
    try:
        if db.DB_PATH.exists():
            db.DB_PATH.replace(backup_path)
        db.DB_PATH.write_bytes(raw)
        log.warning(
            "DB replaced via /import — previous file at %s. Restart the "
            "process to reload the new DB.", backup_path,
        )
    finally:
        # Restart orchestrator so the app keeps scanning; alerts/settings
        # caches are stale until process restart but the worst case is
        # one orphaned scan against the new file.
        if orchestrator is not None:
            try:
                await orchestrator.start()
            except Exception as e:
                log.warning("orchestrator.start() failed after db import: %s", e)
    return {
        "ok": True,
        "size_bytes": len(raw),
        "backup_path": str(backup_path),
        "restart_recommended": True,
    }


@app.post("/api/maintenance/vacuum")
async def api_vacuum_db():
    """Reclaim free pages and rewrite the SQLite file. Manual trigger
    after a big delete (Reset / Delete all / Purge) to shrink the file
    on disk — SQLite keeps the freed pages allocated otherwise. Cheap on
    a small DB, can take a moment on a large one."""
    import aiosqlite
    size_before = db.DB_PATH.stat().st_size if db.DB_PATH.exists() else 0
    async with aiosqlite.connect(db.DB_PATH) as conn:
        await conn.execute("VACUUM")
        await conn.commit()
    size_after = db.DB_PATH.stat().st_size if db.DB_PATH.exists() else 0
    log.info("manual VACUUM: %d → %d bytes (saved %d)",
             size_before, size_after, size_before - size_after)
    return {
        "ok": True,
        "size_before_bytes": size_before,
        "size_after_bytes": size_after,
        "saved_bytes": max(0, size_before - size_after),
    }


@app.get("/api/tilecache/status")
async def api_tilecache_status():
    from services.map_cache import cache_status
    return cache_status()


@app.post("/api/tilecache/clear")
async def api_tilecache_clear():
    from services.map_cache import clear_cache
    return clear_cache()


@app.get("/api/locations/report.pdf")
async def api_locations_report(group_bssids: bool = True):
    """Generate a downloadable PDF summary of all sensor locations.
    `group_bssids` mirrors the Devices tab's "Group multi-BSSID APs"
    checkbox — when true, wifi BSSIDs sharing the same first 5 octets
    are folded into a single row per physical AP. Synchronous; UI
    callers that want progress should use the /report/start +
    /report/status + /report/result.pdf flow below."""
    from fastapi.responses import Response
    from services.report import build_report_pdf

    pdf = await build_report_pdf(group_bssids=group_bssids)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return Response(
        content=pdf,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="gjallarhorn-report-{stamp}.pdf"'},
    )


# ── Async report generation with progress ──────────────────────────────
# Module-level state since only one report can run at a time. The status
# endpoint is polled by the Locations-tab UI to drive a progress bar; the
# bytes are stashed here for the browser to fetch via /result.pdf.
_report_state: dict = {
    "running": False,
    "stage_n": 0,
    "stage_total": 0,
    "stage_label": "",
    "started_at": None,
    "finished_at": None,
    "result": None,
    "filename": None,
    "error": None,
}


async def _run_report_job(group_bssids: bool, mission: dict | None = None) -> None:
    """Background task: builds the PDF, streaming progress into _report_state.
    Optional mission dict is threaded through to the renderer so the
    Mission tab's "Mission report" button can prepend a cover page
    summarising the mission's stats diff."""
    from services.report import build_report_pdf

    def progress(label: str, n: int, total: int) -> None:
        _report_state["stage_label"] = label
        _report_state["stage_n"] = n
        _report_state["stage_total"] = total

    try:
        pdf = await build_report_pdf(
            group_bssids=group_bssids, progress=progress, mission=mission,
        )
        _report_state["result"] = pdf
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        if mission and mission.get("name"):
            safe = "".join(c if c.isalnum() or c in "-_." else "-"
                           for c in mission["name"])[:40]
            _report_state["filename"] = f"gjallarhorn-mission-{safe}-{stamp}.pdf"
        else:
            _report_state["filename"] = f"gjallarhorn-report-{stamp}.pdf"
        _report_state["stage_label"] = "Done"
    except Exception as e:
        log.exception("report generation failed")
        _report_state["error"] = f"{type(e).__name__}: {e}"
    finally:
        _report_state["running"] = False
        _report_state["finished_at"] = time.time()


@app.post("/api/missions/{mission_id}/report/start")
async def api_mission_report_start(mission_id: int, group_bssids: bool = True):
    """Kick off a report build scoped to a mission. Same status / result
    endpoints as the generic /api/locations/report/* flow, but the PDF
    gets a cover page with the mission's name + stats diff prepended."""
    mission = await db.get_mission(mission_id)
    if mission is None:
        raise HTTPException(404, "mission not found")
    if _report_state["running"]:
        raise HTTPException(409, "a report is already being generated")
    _report_state.update({
        "running": True,
        "stage_n": 0, "stage_total": 0, "stage_label": "Starting…",
        "started_at": time.time(), "finished_at": None,
        "result": None, "filename": None, "error": None,
    })
    log.info("report: start mission=%d (%s)", mission_id, mission.get("name"))
    asyncio.create_task(_run_report_job(group_bssids, mission=mission))
    return {"ok": True}


@app.post("/api/locations/report/start")
async def api_report_start(group_bssids: bool = True):
    """Kick off a background report build. Returns immediately. Poll
    /api/locations/report/status to follow progress, then fetch
    /api/locations/report/result.pdf when done."""
    if _report_state["running"]:
        raise HTTPException(409, "a report is already being generated")
    _report_state.update({
        "running": True,
        "stage_n": 0,
        "stage_total": 0,
        "stage_label": "Starting…",
        "started_at": time.time(),
        "finished_at": None,
        "result": None,
        "filename": None,
        "error": None,
    })
    log.info("report: start (group_bssids=%s)", group_bssids)
    asyncio.create_task(_run_report_job(group_bssids))
    return {"ok": True}


@app.get("/api/locations/report/status")
async def api_report_status():
    """Snapshot of the current/last report job. The PDF bytes are not
    returned here — fetch /report/result.pdf when status.ready=True."""
    return {
        "running": _report_state["running"],
        "stage_n": _report_state["stage_n"],
        "stage_total": _report_state["stage_total"],
        "stage_label": _report_state["stage_label"],
        "started_at": _report_state["started_at"],
        "finished_at": _report_state["finished_at"],
        "ready": _report_state["result"] is not None and not _report_state["running"],
        "error": _report_state["error"],
        "filename": _report_state["filename"],
    }


@app.get("/api/locations/report/result.pdf")
async def api_report_result():
    """Stream back the PDF from the last completed background job. 404
    until /report/start has produced output; 500 if the job errored."""
    from fastapi.responses import Response
    if _report_state["error"]:
        raise HTTPException(500, _report_state["error"])
    if _report_state["result"] is None:
        raise HTTPException(404, "no report ready — call /report/start first")
    if _report_state["running"]:
        raise HTTPException(409, "report still being generated")
    pdf = _report_state["result"]
    fname = _report_state["filename"] or "gjallarhorn-report.pdf"
    return Response(
        content=pdf,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


@app.post("/api/locations/reset")
async def api_reset_locations():
    """Delete every auto-clustered location while keeping drawn geofences.
    Whitelisted devices' history is archived to preserved_devices first.
    Active-location pointer is cleared if the active loc was wiped. Also
    clears the alert feed and any active latches — a reset is a fresh
    page for the operator."""
    counts = await db.delete_auto_locations()
    # If the active loc was an auto cluster (probably was), it's now gone.
    # Cheapest check: look up the active id; clear if not found.
    if location_manager.active_id is not None:
        info = await db.get_location_summary(location_manager.active_id)
        if info is None:
            location_manager._active_id = None  # type: ignore[attr-defined]
            location_manager._active_lat = None  # type: ignore[attr-defined]
            location_manager._active_lon = None  # type: ignore[attr-defined]
    counts["alerts_cleared"] = await db.clear_alert_events()
    alert_service._latched.clear()  # type: ignore[attr-defined]
    log.info("reset locations (auto only): %s", counts)
    disabled = await _disable_broken_rules_after_location_delete()
    if disabled:
        counts["rules_disabled"] = disabled
    return {"ok": True, "deleted": counts}


@app.delete("/api/locations")
async def api_delete_all_locations():
    """Wipe every location and all associated devices and observations.
    Also clears the alert feed and any active latches — the entire
    sensor state is being reset, so historical alerts no longer have
    context."""
    counts = await db.delete_all_locations()
    # Reset the active-location pointer so the next GPS fix opens a fresh one.
    location_manager._active_id = None  # type: ignore[attr-defined]
    location_manager._active_lat = None  # type: ignore[attr-defined]
    location_manager._active_lon = None  # type: ignore[attr-defined]
    counts["alerts_cleared"] = await db.clear_alert_events()
    alert_service._latched.clear()  # type: ignore[attr-defined]
    log.info("Deleted all locations: %s", counts)
    disabled = await _disable_broken_rules_after_location_delete()
    if disabled:
        counts["rules_disabled"] = disabled
    return {"ok": True, "deleted": counts}


# ---------- OUI database ----------
@app.get("/api/oui/status")
async def api_oui_status():
    return await oui_service.status()


@app.post("/api/oui/update")
async def api_oui_update():
    """Download MA-L + MA-M + MA-S registries from IEEE and rebuild the local DB."""
    result = await oui_service.update_from_ieee()
    if not result.get("ok"):
        raise HTTPException(502, result.get("error", "update failed"))
    return result


@app.get("/api/oui/lookup")
async def api_oui_lookup(mac: str):
    return {"mac": mac, "vendor": await oui_service.lookup(mac)}


# ---------- Alerts ----------
ALLOWED_MATCH_TYPES = {
    "device_id", "name_contains", "vendor_contains", "rssi_above",
    "new_device", "cross_location", "persistent_companion",
    "co_arrival_transit", "travel_time_companion", "approach_vector",
    "novel_location_chain", "mac_rotation_rate", "cross_kind_co_travel",
    "arrival_after_gap", "absence_gap", "sustained_presence",
    "wifi_association",
}
# Compound (AND) conditions only support the simple value-based types — the
# stateful ones (new_device, cross_location) only make sense as the primary
# match.
COMPOUND_MATCH_TYPES = {
    "device_id", "name_contains", "vendor_contains", "rssi_above",
}
ALLOWED_KINDS = {None, "wifi", "bluetooth", "wifi_client", "bluetooth_classic"}


def _validate_presence_value(value: str) -> None:
    """Reject sustained_presence match_values that the rule evaluator
    would silently skip. Format is 'N' or 'N/G' with an optional
    '@id1,id2,...' suffix; both N and G must parse as integers >= 1.
    A typo like '10m' or '10 min' would otherwise return None from
    _parse_presence_value and the rule would sit inert."""
    s = (value or "").strip()
    # Strip alias suffix before parsing the duration spec.
    if "@" in s:
        s = s.split("@", 1)[0]
    parts = [p.strip() for p in s.split("/")]
    try:
        n = int(parts[0]) if parts and parts[0] else 0
        g = int(parts[1]) if len(parts) > 1 and parts[1] else 5
    except (ValueError, IndexError):
        raise HTTPException(
            400,
            "sustained_presence match_value must be 'N' or 'N/G' "
            "(integers, minutes), optionally followed by "
            "'@id1,id2,...' aliases — got: " + (value or "")
        )
    if n < 1 or g < 1:
        raise HTTPException(
            400, "sustained_presence requires N >= 1 and G >= 1 minute"
        )


def _validate_extra_conditions(raw) -> list[dict]:
    """Normalise + validate the extra_conditions list for create/update.
    Raises HTTPException on bad input."""
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise HTTPException(400, "extra_conditions must be a list")
    out: list[dict] = []
    for i, c in enumerate(raw):
        if not isinstance(c, dict):
            raise HTTPException(400, f"extra_conditions[{i}] must be an object")
        mt = c.get("match_type")
        if mt not in COMPOUND_MATCH_TYPES:
            raise HTTPException(
                400,
                f"extra_conditions[{i}].match_type must be one of "
                f"{sorted(COMPOUND_MATCH_TYPES)}",
            )
        mv = (c.get("match_value") or "").strip()
        if not mv:
            raise HTTPException(400, f"extra_conditions[{i}].match_value required")
        out.append({"match_type": mt, "match_value": mv})
    return out


@app.get("/api/alerts/rules")
async def api_list_rules():
    return {"rules": await db.list_alert_rules()}


@app.post("/api/alerts/rules")
async def api_create_rule(payload: dict):
    name = (payload.get("name") or "").strip()
    if not name:
        raise HTTPException(400, "name required")
    match_type = payload.get("match_type")
    if match_type not in ALLOWED_MATCH_TYPES:
        raise HTTPException(400, f"match_type must be one of {sorted(ALLOWED_MATCH_TYPES)}")
    match_value = (payload.get("match_value") or "").strip()
    if not match_value:
        raise HTTPException(400, "match_value required")
    if match_type == "wifi_association":
        # 'client@ssid' — both sides optional but '@' alone (nothing on
        # either side) is rejected since it would mean "any client probes
        # for any SSID", i.e. fire on every probe-request tick.
        if "@" not in match_value:
            raise HTTPException(400, "wifi_association match_value must be 'client@ssid'")
        c, s = match_value.split("@", 1)
        if not c.strip() and not s.strip():
            raise HTTPException(400, "wifi_association requires a client and/or SSID — both can't be blank")
    if match_type == "sustained_presence":
        # 'N' or 'N/G' with an optional '@id1,id2,...' alias suffix.
        # Reject typo'd forms like '10 min' here rather than letting the
        # parser silently return None at evaluation time (which makes the
        # rule appear created-but-inert).
        _validate_presence_value(match_value)
    kind = payload.get("kind") or None
    if kind not in ALLOWED_KINDS:
        raise HTTPException(400, "kind must be wifi, bluetooth, or null")
    location_id = payload.get("location_id")
    if location_id in ("", None):
        location_id = None
    else:
        try:
            location_id = int(location_id)
        except (TypeError, ValueError):
            raise HTTPException(400, "location_id must be an integer")
        # Reject creation against a missing location so a rule can't
        # spawn pre-broken. -1 (active-location sentinel) and NULL stay
        # exempt — those are valid even with no concrete location.
        if location_id != -1 and not await db.location_exists(location_id):
            raise HTTPException(
                400, f"location #{location_id} does not exist",
            )
    notify_discord = bool(payload.get("notify_discord"))
    audible = bool(payload.get("audible"))
    extra_conditions = _validate_extra_conditions(payload.get("extra_conditions"))
    # Latch defaults to True when the field is missing so existing API
    # clients keep the historical "fire once until cleared" behaviour.
    latch = bool(payload.get("latch", True))
    # include_whitelist defaults False — same legacy behaviour, where the
    # whitelist mutes every rule unless the rule explicitly opts in.
    include_whitelist = bool(payload.get("include_whitelist", False))
    rule_id = await db.create_alert_rule(
        name, kind, match_type, match_value, location_id,
        notify_discord, audible, extra_conditions,
        latch=latch, include_whitelist=include_whitelist,
    )
    await alert_service.reload()
    return {"id": rule_id}


@app.patch("/api/alerts/rules/{rule_id}")
async def api_update_rule(rule_id: int, payload: dict):
    fields: dict = {}
    if "enabled" in payload:
        fields["enabled"] = 1 if payload["enabled"] else 0
    if "name" in payload:
        name = (payload["name"] or "").strip()
        if not name:
            raise HTTPException(400, "name cannot be empty")
        fields["name"] = name
    if "match_type" in payload:
        if payload["match_type"] not in ALLOWED_MATCH_TYPES:
            raise HTTPException(400, "invalid match_type")
        fields["match_type"] = payload["match_type"]
    if "match_value" in payload:
        v = (payload["match_value"] or "").strip()
        if not v:
            raise HTTPException(400, "match_value cannot be empty")
        # If the caller is updating to wifi_association (in this same
        # payload, or unchanged from the stored row), enforce the
        # 'client@ssid' shape so a malformed value can't sneak past the
        # create-time check.
        mt = fields.get("match_type") or payload.get("match_type")
        if mt == "wifi_association":
            if "@" not in v:
                raise HTTPException(400, "wifi_association match_value must be 'client@ssid'")
            c, s = v.split("@", 1)
            if not c.strip() and not s.strip():
                raise HTTPException(400, "wifi_association requires a client and/or SSID")
        if mt == "sustained_presence":
            _validate_presence_value(v)
        fields["match_value"] = v
    if "kind" in payload:
        k = payload["kind"] or None
        if k not in ALLOWED_KINDS:
            raise HTTPException(400, "invalid kind")
        fields["kind"] = k
    if "location_id" in payload:
        v = payload["location_id"]
        if v in ("", None):
            fields["location_id"] = None
        else:
            try:
                fields["location_id"] = int(v)
            except (TypeError, ValueError):
                raise HTTPException(400, "location_id must be int or null")
    if "notify_discord" in payload:
        fields["notify_discord"] = 1 if payload["notify_discord"] else 0
    if "latch" in payload:
        # Switching latch off mid-life doesn't auto-clear existing latches —
        # the user can hit Unlatch in the live feed if they want a clean
        # slate. Same in reverse: switching back to latched doesn't wipe
        # history, it just gates the next fire.
        fields["latch"] = 1 if payload["latch"] else 0
    if "include_whitelist" in payload:
        fields["include_whitelist"] = 1 if payload["include_whitelist"] else 0
    if "audible" in payload:
        fields["audible"] = 1 if payload["audible"] else 0
    if "extra_conditions" in payload:
        import json as _json
        fields["extra_conditions"] = _json.dumps(
            _validate_extra_conditions(payload["extra_conditions"])
        )

    # Broken-reference enforcement. After computing the resulting
    # location_id (from this patch or the stored row), reject re-enables
    # of a rule that still points at a missing location, and clear the
    # disabled_reason when the rule's reference is now valid (the
    # operator just fixed it). This is what makes a rule "stuck disabled"
    # until the operator either re-targets it or pulls it back to a
    # non-specific scope.
    current = await db.get_alert_rule(rule_id)
    if current is None:
        raise HTTPException(404, "rule not found")
    effective_loc = fields["location_id"] if "location_id" in fields else current.get("location_id")
    loc_valid = (
        effective_loc is None
        or effective_loc == -1
        or await db.location_exists(int(effective_loc))
    )
    if not loc_valid:
        reason = f"location #{effective_loc} no longer exists"
        fields["disabled_reason"] = reason
        fields["enabled"] = 0
        if "enabled" in payload and payload["enabled"]:
            raise HTTPException(
                400,
                f"cannot enable rule: {reason} — change the location filter first",
            )
    elif current.get("disabled_reason"):
        # Reference is back in a valid shape — clear the broken-reason
        # mark. enabled stays at whatever the patch (or DB) set; the
        # operator still has to flip it on explicitly.
        fields["disabled_reason"] = None

    await db.update_alert_rule(rule_id, fields)
    await alert_service.reload()
    return {"ok": True}


@app.delete("/api/alerts/rules/{rule_id}")
async def api_delete_rule(rule_id: int):
    await db.delete_alert_rule(rule_id)
    await alert_service.reload()
    return {"ok": True}


@app.get("/api/alerts/events")
async def api_list_events(limit: int = 100, since_id: Optional[int] = None):
    return {"events": await db.list_alert_events(limit=limit, since_id=since_id)}


@app.delete("/api/alerts/events")
async def api_clear_events():
    """Delete every alert_events row. Side effect: every active latch is
    cleared too, since a deleted row can't keep its latch alive."""
    n = await db.clear_alert_events()
    alert_service._latched.clear()  # type: ignore[attr-defined]
    return {"deleted": n}


@app.delete("/api/alerts/events/{event_id}")
async def api_delete_event(event_id: int):
    """Delete a single alert_events row. Used by the live-feed per-row
    trash icon. If this was the last un-cleared event for its (rule,
    device) pair, also remove the in-memory latch — otherwise the
    pair would stay 'latched' against an event row that no longer
    exists, blocking future fires."""
    result = await db.delete_alert_event(event_id)
    if result is None:
        raise HTTPException(404, "alert event not found")
    if not result["was_cleared"] and result["uncleared_remaining"] == 0:
        alert_service._latched.discard(
            (result["rule_id"], result["device_id_l"])
        )
    return {"ok": True}


@app.post("/api/alerts/clear")
async def api_clear_latch(payload: dict):
    """Clear a single latched (rule, device) pair so future matches fire
    again. Body: {rule_id, device_id}. Marks every alert_events row for
    the pair as cleared (history preserved) and removes the in-memory
    latch."""
    raw_rule_id = payload.get("rule_id")
    if raw_rule_id is None:
        raise HTTPException(400, "rule_id required")
    try:
        rule_id = int(raw_rule_id)
    except (TypeError, ValueError):
        raise HTTPException(400, "rule_id must be an integer")
    device_id = (payload.get("device_id") or "").strip()
    if not device_id:
        raise HTTPException(400, "device_id required")
    n = await alert_service.unlatch(rule_id, device_id)
    return {"ok": True, "cleared": n}


@app.post("/api/alerts/clear-all")
async def api_clear_all_latches():
    """Clear every active latch in one shot — alert history is kept,
    just marked acknowledged."""
    n = await alert_service.unlatch_all()
    return {"ok": True, "cleared": n}


# ── Baseline scan ──────────────────────────────────────────────────────
# One-shot "what's around me right now" scan that aggregates a few wifi
# scan iterations + an extended bluetooth scan into a temporary device
# list. Operators run this once they're somewhere safe to capture every
# nearby device as known-good, then selectively promote entries to the
# permanent whitelist.
_baseline_state: dict = {
    "running": False,
    "stage_n": 0,
    "stage_total": 0,
    "stage_label": "",
    "device_count": 0,
    "started_at": None,
    "finished_at": None,
    "result": None,       # list[dict] when ready
    "error": None,
    "rssi_floor": None,
}


async def _run_baseline_scan(rssi_floor: int, wifi_iterations: int = 3,
                             bt_duration_s: float = 15.0) -> None:
    """Background task: bursts the wifi/BT scanners harder than the
    standard cadence and merges everything into _baseline_state['result'].
    Caller picks the rssi_floor; usually the user's configured min_rssi
    or a tighter override."""
    from services.wifi_scanner import scan_wifi, pick_wifi_interface
    from services.bluetooth_scanner import scan_bluetooth

    settings = await settings_store.load()
    iface = settings.wifi_interface
    if iface == "auto":
        iface = await pick_wifi_interface()

    # Stage count: 1 init + N wifi iterations + 1 BT scan + 1 finalize.
    total = 2 + (wifi_iterations if iface else 0)
    _baseline_state["stage_total"] = total
    _baseline_state["stage_n"] = 0

    def _step(label: str) -> None:
        _baseline_state["stage_n"] = min(total, _baseline_state["stage_n"] + 1)
        _baseline_state["stage_label"] = label
        log.info("baseline: %s (%d/%d)",
                 label, _baseline_state["stage_n"], total)

    found: dict[tuple[str, str], dict] = {}

    def _keep(kind: str, device_id: str, rssi: int, name: str, vendor: str,
              details: dict) -> None:
        device_id = (device_id or "").lower()
        if rssi < rssi_floor:
            return
        key = (kind, device_id)
        prev = found.get(key)
        if prev is None or rssi > prev["rssi"]:
            tracker = db.classify_tracker(kind, details)
            found[key] = {
                "kind": kind, "device_id": device_id, "rssi": rssi,
                "name": name or "", "vendor": vendor or "",
                "tracker_type": tracker, "details": details,
            }
        _baseline_state["device_count"] = len(found)

    _step("Initializing")

    try:
        # ── WiFi bursts ──────────────────────────────────────────────
        if iface:
            for i in range(wifi_iterations):
                _step(f"WiFi scan {i + 1} of {wifi_iterations} ({iface})")
                try:
                    devs = await scan_wifi(iface)
                except Exception as e:
                    log.warning("baseline wifi iter %d failed: %s", i + 1, e)
                    continue
                for d in devs:
                    _keep(
                        "wifi", d.bssid, d.rssi,
                        d.ssid or "", d.vendor or "",
                        d.model_dump(mode="json"),
                    )
                if i < wifi_iterations - 1:
                    await asyncio.sleep(1.5)

        # ── Bluetooth extended scan ──────────────────────────────────
        _step(f"Bluetooth scan ({int(bt_duration_s)}s)")
        try:
            devs = await scan_bluetooth(settings.bluetooth_adapter, bt_duration_s)
        except Exception as e:
            log.warning("baseline bt scan failed: %s", e)
            devs = []
        for d in devs:
            if settings.hide_random_bt_addresses and d.address_type == "random":
                continue
            _keep(
                "bluetooth", d.address, d.rssi,
                d.name or "", d.vendor or "",
                d.model_dump(mode="json"),
            )

        _step("Finalizing")
        # Drop devices that are already on the whitelist — no point making
        # the user re-promote things they've already approved. Match rule
        # mirrors AlertService.is_whitelisted (exact id or OUI prefix).
        wl_rows = await db.list_whitelist()
        wl = [(r["kind"], (r["device_id"] or "").lower()) for r in wl_rows]
        kept = [
            d for d in found.values()
            if not db._match_whitelist(wl, d["kind"], d["device_id"])
        ]
        # Sort strongest first so the UI's default order is "closest".
        _baseline_state["result"] = sorted(
            kept, key=lambda x: -int(x.get("rssi") or -999),
        )
        _baseline_state["device_count"] = len(kept)
    except Exception as e:
        log.exception("baseline scan failed")
        _baseline_state["error"] = f"{type(e).__name__}: {e}"
    finally:
        _baseline_state["running"] = False
        _baseline_state["finished_at"] = time.time()
        _baseline_state["stage_label"] = (
            "Done" if _baseline_state["error"] is None else "Failed"
        )


@app.post("/api/scan/baseline/start")
async def api_baseline_start(rssi_floor: Optional[int] = None):
    """Kick off a baseline scan. `rssi_floor` defaults to the configured
    min_rssi; pass a stricter value (e.g. -65) to limit the capture to
    very close devices only."""
    if _baseline_state["running"]:
        raise HTTPException(409, "a baseline scan is already running")
    settings = await settings_store.load()
    floor = rssi_floor if rssi_floor is not None else settings.min_rssi
    _baseline_state.update({
        "running": True,
        "stage_n": 0,
        "stage_total": 0,
        "stage_label": "Queued",
        "device_count": 0,
        "started_at": time.time(),
        "finished_at": None,
        "result": None,
        "error": None,
        "rssi_floor": floor,
    })
    log.info("baseline: start (rssi_floor=%d dBm)", floor)
    asyncio.create_task(_run_baseline_scan(rssi_floor=floor))
    return {"ok": True}


@app.get("/api/scan/baseline/status")
async def api_baseline_status():
    """Snapshot of the in-flight or last baseline-scan job."""
    return {
        "running": _baseline_state["running"],
        "stage_n": _baseline_state["stage_n"],
        "stage_total": _baseline_state["stage_total"],
        "stage_label": _baseline_state["stage_label"],
        "device_count": _baseline_state["device_count"],
        "started_at": _baseline_state["started_at"],
        "finished_at": _baseline_state["finished_at"],
        "ready": _baseline_state["result"] is not None and not _baseline_state["running"],
        "error": _baseline_state["error"],
        "rssi_floor": _baseline_state["rssi_floor"],
    }


@app.get("/api/scan/baseline/result")
async def api_baseline_result():
    """Captured device list from the last completed scan."""
    if _baseline_state["error"]:
        raise HTTPException(500, _baseline_state["error"])
    if _baseline_state["result"] is None:
        raise HTTPException(404, "no baseline result — start a scan first")
    if _baseline_state["running"]:
        raise HTTPException(409, "baseline scan still running")
    return {"devices": _baseline_state["result"]}


@app.get("/api/whitelist/temp")
async def api_list_temp_whitelist():
    """Inspect the temporary whitelist (session-scoped, baseline-populated)."""
    return {"entries": await db.list_temp_whitelist()}


@app.post("/api/whitelist/temp/promote")
async def api_promote_temp_whitelist(payload: dict):
    """Move one (kind, device_id) from the temp list to the permanent
    whitelist. Body: {kind, device_id, note?}."""
    kind = payload.get("kind")
    if kind not in ALLOWED_KINDS or kind is None:
        raise HTTPException(400, "kind must be wifi, bluetooth, or wifi_client")
    device_id = (payload.get("device_id") or "").strip().lower()
    if not device_id:
        raise HTTPException(400, "device_id required")
    note = (payload.get("note") or "").strip() or "promoted from temp"
    await db.add_whitelist(kind, device_id, note)
    await db.delete_temp_whitelist_pair(kind, device_id)
    await alert_service.reload()
    return {"ok": True}


@app.delete("/api/whitelist/temp")
async def api_clear_temp_whitelist(payload: dict | None = None):
    """Without a body: clear the entire temp whitelist.
    With {kind, device_id}: drop one entry (un-silences the device)."""
    payload = payload or {}
    kind = payload.get("kind")
    device_id = (payload.get("device_id") or "").strip().lower()
    if kind and device_id:
        ok = await db.delete_temp_whitelist_pair(kind, device_id)
        await alert_service.reload()
        return {"ok": True, "removed": int(ok)}
    n = await db.clear_temp_whitelist()
    await alert_service.reload()
    return {"ok": True, "removed": n}


@app.post("/api/scan/baseline/promote")
async def api_baseline_promote(payload: dict):
    """Resolve a baseline-scan capture. Body: {entries: [{kind, device_id,
    note?}, ...]} — the selected entries get added to the permanent
    whitelist; everything else in _baseline_state['result'] but not in
    the selection lands in the temporary whitelist, silencing those
    devices until the operator hits "Delete all locations". Returns
    counts of each."""
    entries = payload.get("entries") or []
    if not isinstance(entries, list):
        raise HTTPException(400, "entries must be a list")

    # Index the user's explicit selection so we can subtract it from the
    # cached baseline result to find the "leave silenced but don't keep"
    # set.
    picked: set[tuple[str, str]] = set()
    added_perm = 0
    for e in entries:
        kind = e.get("kind")
        device_id = (e.get("device_id") or "").strip().lower()
        note = (e.get("note") or "baseline scan").strip() or "baseline scan"
        if kind not in ALLOWED_KINDS or kind is None or not device_id:
            continue
        picked.add((kind, device_id))
        try:
            await db.add_whitelist(kind, device_id, note)
            # If the same device was previously parked in the temp list
            # (from an earlier baseline that didn't promote it), drop the
            # temp row — it's been upgraded.
            await db.delete_temp_whitelist_pair(kind, device_id)
            added_perm += 1
        except Exception as ex:
            log.warning("baseline promote failed for %s/%s: %s",
                        kind, device_id, ex)

    # Everything the user *saw* in the modal but didn't promote goes into
    # the temp whitelist. The result list is what was shown.
    added_temp = 0
    for d in (_baseline_state.get("result") or []):
        kind = d.get("kind")
        device_id = (d.get("device_id") or "").lower()
        if not kind or not device_id:
            continue
        if (kind, device_id) in picked:
            continue
        try:
            await db.add_temp_whitelist(kind, device_id, "baseline (silenced)")
            added_temp += 1
        except Exception as ex:
            log.warning("baseline temp-whitelist failed for %s/%s: %s",
                        kind, device_id, ex)

    if added_perm or added_temp:
        await alert_service.reload()
    log.info("baseline promote: %d → permanent, %d → temporary",
             added_perm, added_temp)
    return {"ok": True, "added": added_perm, "silenced": added_temp}


# ---------- Manual control ----------
@app.post("/api/locations/new")
async def api_force_new_location():
    """Force-open a new location at the current fix (debug helper)."""
    if gps is None:
        raise HTTPException(503, "no GPS fix")
    lat = gps.fix.lat
    lon = gps.fix.lon
    if lat is None or lon is None:
        raise HTTPException(503, "no GPS fix")
    s = await settings_store.load()
    from services.location_manager import effective_radius_m
    radius = effective_radius_m(
        s.new_location_distance_m, speed_mps=gps.fix.speed,
        dynamic_enabled=s.new_location_dynamic, dynamic_t_s=s.new_location_dynamic_t_s,
    )
    new_id = await db.create_location(lat, lon, radius, None)
    label = s.location_label_template.format(id=new_id, lat=lat, lon=lon)
    await db.update_location_label(new_id, label)
    location_manager._active_id = new_id  # type: ignore[attr-defined]
    location_manager._active_lat = lat  # type: ignore[attr-defined]
    location_manager._active_lon = lon  # type: ignore[attr-defined]
    return {"id": new_id}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("gjallarhorn:app", host="0.0.0.0", port=5003, reload=False,
                log_level="warning", access_log=False)
