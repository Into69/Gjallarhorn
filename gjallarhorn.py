from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException
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
log = logging.getLogger("gjallarhorn")

ROOT = Path(__file__).parent
STATIC_DIR = ROOT / "static"

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


# ---------- Interfaces / adapters ----------
@app.get("/api/interfaces/wifi")
async def api_wifi_interfaces():
    """Rich info per wireless interface, incl. associated SSID if any."""
    return {"interfaces": await list_wifi_interface_info()}


@app.get("/api/interfaces/wifi/names")
async def api_wifi_interface_names():
    return {"interfaces": await list_wifi_interfaces()}


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
async def api_label_location(loc_id: int, payload: dict):
    label = payload.get("label", "").strip()
    if not label:
        raise HTTPException(400, "label required")
    await db.update_location_label(loc_id, label)
    return {"ok": True}


@app.get("/api/locations/{loc_id}/devices")
async def api_location_devices(loc_id: int, kind: Optional[str] = None):
    return {"devices": await db.devices_at_location(loc_id, kind)}


@app.delete("/api/locations")
async def api_delete_all_locations():
    """Wipe every location and all associated devices and observations."""
    counts = await db.delete_all_locations()
    # Reset the active-location pointer so the next GPS fix opens a fresh one.
    location_manager._active_id = None  # type: ignore[attr-defined]
    location_manager._active_lat = None  # type: ignore[attr-defined]
    location_manager._active_lon = None  # type: ignore[attr-defined]
    log.info("Deleted all locations: %s", counts)
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
    "new_device", "cross_location",
}
ALLOWED_KINDS = {None, "wifi", "bluetooth"}


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
    rule_id = await db.create_alert_rule(name, kind, match_type, match_value, location_id)
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
    n = await db.clear_alert_events()
    return {"deleted": n}


# ---------- Manual control ----------
@app.post("/api/locations/new")
async def api_force_new_location():
    """Force-open a new location at the current fix (debug helper)."""
    if gps is None or gps.fix.lat is None:
        raise HTTPException(503, "no GPS fix")
    s = await settings_store.load()
    new_id = await db.create_location(gps.fix.lat, gps.fix.lon, s.new_location_distance_m, None)
    label = s.location_label_template.format(id=new_id, lat=gps.fix.lat, lon=gps.fix.lon)
    await db.update_location_label(new_id, label)
    location_manager._active_id = new_id  # type: ignore[attr-defined]
    location_manager._active_lat = gps.fix.lat  # type: ignore[attr-defined]
    location_manager._active_lon = gps.fix.lon  # type: ignore[attr-defined]
    return {"id": new_id}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("gjallarhorn:app", host="0.0.0.0", port=5003, reload=False,
                log_level="warning", access_log=False)
