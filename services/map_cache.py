"""Disk-cached OSM tile fetcher and Pillow-based map compositor.

Replaces the `staticmap` dependency. Tiles are fetched once via stdlib
`urllib`, cached under `tile-cache/{z}/{x}/{y}.png`, and reused by every
subsequent render. `render_map` composites the tiles needed for the
requested view and draws point markers on top.
"""
from __future__ import annotations

import asyncio
import io
import logging
import math
import shutil
import urllib.request
from pathlib import Path

from PIL import Image, ImageDraw

log = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent
CACHE_DIR = REPO_ROOT / "tile-cache"

TILE_SIZE = 256
USER_AGENT = "Gjallarhorn-Report/0.1 (https://github.com/Into69/Gjallarhorn)"
FETCH_TIMEOUT_S = 10.0

# Canonical tile-provider catalogue. The /api/map_providers endpoint
# returns the same shape (callers shouldn't have to duplicate the
# table). 'subdomains' is optional and only used when the URL pattern
# carries the '{s}' placeholder; we substitute the first character so
# the cached tile path is stable regardless of which subdomain a live
# Leaflet client happens to load.
MAP_PROVIDERS: dict[str, dict] = {
    "osm": {
        "name": "OpenStreetMap",
        "url": "https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png",
        "attribution": "© OpenStreetMap contributors",
        "max_zoom": 19,
        "subdomains": "abc",
    },
    "osm_topo": {
        "name": "OpenTopoMap",
        "url": "https://{s}.tile.opentopomap.org/{z}/{x}/{y}.png",
        "attribution": "© OpenStreetMap, SRTM | © OpenTopoMap (CC-BY-SA)",
        "max_zoom": 17,
        "subdomains": "abc",
    },
    "carto_positron": {
        "name": "Carto Positron (light)",
        "url": "https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}.png",
        "attribution": "© OpenStreetMap, © CARTO",
        "max_zoom": 19,
        "subdomains": "abcd",
    },
    "carto_dark": {
        "name": "Carto Dark Matter",
        "url": "https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}.png",
        "attribution": "© OpenStreetMap, © CARTO",
        "max_zoom": 19,
        "subdomains": "abcd",
    },
    "stamen_terrain": {
        "name": "Stamen Terrain",
        "url": "https://tiles.stadiamaps.com/tiles/stamen_terrain/{z}/{x}/{y}.png",
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


def _provider_url(provider: str) -> tuple[str, str]:
    """Resolve a provider key to (url_template_with_s_substituted, key_used).
    Falls back to OSM if the requested key is unknown."""
    p = MAP_PROVIDERS.get(provider) or MAP_PROVIDERS["osm"]
    url = p["url"]
    if "{s}" in url:
        sub = (p.get("subdomains") or "a")[0]
        url = url.replace("{s}", sub)
    return url, (provider if provider in MAP_PROVIDERS else "osm")


def _tile_path(provider: str, z: int, x: int, y: int) -> Path:
    # Per-provider sub-directory so switching providers doesn't pollute
    # cached tiles with a mismatched basemap.
    return CACHE_DIR / provider / str(z) / str(x) / f"{y}.png"


def _fetch_tile_blocking(url_template: str, z: int, x: int, y: int) -> bytes:
    url = url_template.format(z=z, x=x, y=y)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT_S) as resp:
        if resp.status >= 400:
            raise IOError(f"tile {z}/{x}/{y} returned HTTP {resp.status}")
        return resp.read()


async def _get_tile_bytes(provider: str, url_template: str,
                          z: int, x: int, y: int) -> bytes:
    """Return PNG bytes for tile (z,x,y), fetching + caching on miss."""
    p = _tile_path(provider, z, x, y)
    if p.exists():
        try:
            return p.read_bytes()
        except OSError:
            pass
    data = await asyncio.to_thread(_fetch_tile_blocking, url_template, z, x, y)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".png.tmp")
    tmp.write_bytes(data)
    tmp.replace(p)  # atomic on POSIX; close-enough on Windows
    return data


# ── projection ──────────────────────────────────────────────────────
def _latlon_to_world_px(lat: float, lon: float, z: int) -> tuple[float, float]:
    """Web-Mercator pixel coordinates at zoom z (whole-world raster)."""
    n = 2 ** z
    px_world = TILE_SIZE * n
    x = (lon + 180.0) / 360.0 * px_world
    lat_rad = math.radians(max(-85.05112878, min(85.05112878, lat)))
    y = (1.0 - math.log(math.tan(lat_rad) + 1.0 / math.cos(lat_rad)) / math.pi) / 2.0 * px_world
    return x, y


def _pick_zoom(
    points: list[tuple[float, float]], width_px: int, height_px: int,
    padding: int, max_zoom: int = 18, min_zoom: int = 2,
) -> int:
    if len(points) <= 1:
        return 16
    lats = [p[0] for p in points]
    lons = [p[1] for p in points]
    if max(lats) == min(lats) and max(lons) == min(lons):
        return 16
    for z in range(max_zoom, min_zoom - 1, -1):
        x0, y0 = _latlon_to_world_px(min(lats), min(lons), z)
        x1, y1 = _latlon_to_world_px(max(lats), max(lons), z)
        if (abs(x1 - x0) + 2 * padding <= width_px
                and abs(y1 - y0) + 2 * padding <= height_px):
            return z
    return min_zoom


# ── rendering ───────────────────────────────────────────────────────
async def render_map(
    points: list[tuple[float, float, str]],
    *,
    width_px: int = 900,
    height_px: int = 540,
    zoom: int | None = None,
    padding: int = 40,
    provider: str = "osm",
) -> bytes:
    """Compose a tile-based map for the given (lat, lon, color_hex)
    points. The basemap is picked from MAP_PROVIDERS using the supplied
    provider key (defaults to OSM) so the report's maps can mirror the
    operator's chosen Leaflet provider in the web UI.

    Returns PNG bytes. Tiles that fail to fetch are skipped — the result
    will have grey gaps where tiles are missing rather than raising."""
    if not points:
        raise ValueError("render_map requires at least one point")
    url_template, provider_key = _provider_url(provider)

    if zoom is None:
        zoom = _pick_zoom([(p[0], p[1]) for p in points], width_px, height_px, padding)

    if len(points) == 1:
        center_lat, center_lon = points[0][0], points[0][1]
    else:
        lats = [p[0] for p in points]
        lons = [p[1] for p in points]
        center_lat = (min(lats) + max(lats)) / 2.0
        center_lon = (min(lons) + max(lons)) / 2.0

    cx, cy = _latlon_to_world_px(center_lat, center_lon, zoom)
    left = cx - width_px / 2.0
    top = cy - height_px / 2.0

    tile_x0 = math.floor(left / TILE_SIZE)
    tile_y0 = math.floor(top / TILE_SIZE)
    tile_x1 = math.floor((left + width_px) / TILE_SIZE)
    tile_y1 = math.floor((top + height_px) / TILE_SIZE)

    canvas = Image.new("RGB", (width_px, height_px), (235, 235, 235))
    n = 2 ** zoom

    fetch_tasks = []
    coords: list[tuple[int, int]] = []
    for tx in range(tile_x0, tile_x1 + 1):
        for ty in range(tile_y0, tile_y1 + 1):
            if tx < 0 or tx >= n or ty < 0 or ty >= n:
                continue
            coords.append((tx, ty))
            fetch_tasks.append(_get_tile_bytes(provider_key, url_template, zoom, tx, ty))

    results = await asyncio.gather(*fetch_tasks, return_exceptions=True)
    for (tx, ty), data in zip(coords, results):
        if isinstance(data, Exception):
            log.warning("tile %d/%d/%d failed: %s", zoom, tx, ty, data)
            continue
        try:
            tile_img = Image.open(io.BytesIO(data)).convert("RGB")
        except Exception as e:
            log.warning("tile %d/%d/%d unreadable: %s", zoom, tx, ty, e)
            continue
        paste_x = int(round(tx * TILE_SIZE - left))
        paste_y = int(round(ty * TILE_SIZE - top))
        canvas.paste(tile_img, (paste_x, paste_y))

    draw = ImageDraw.Draw(canvas)
    for lat, lon, color in points:
        px, py = _latlon_to_world_px(lat, lon, zoom)
        x = int(round(px - left))
        y = int(round(py - top))
        # white halo + colored core for contrast against any basemap
        draw.ellipse([x - 9, y - 9, x + 9, y + 9], fill="white", outline="#444")
        draw.ellipse([x - 6, y - 6, x + 6, y + 6], fill=color)

    out = io.BytesIO()
    canvas.save(out, format="PNG", optimize=True)
    return out.getvalue()


# ── cache management ────────────────────────────────────────────────
def cache_status() -> dict:
    """Total cached tiles + on-disk size in bytes."""
    if not CACHE_DIR.exists():
        return {"count": 0, "bytes": 0, "dir": str(CACHE_DIR)}
    count = 0
    total = 0
    for p in CACHE_DIR.rglob("*.png"):
        try:
            total += p.stat().st_size
            count += 1
        except OSError:
            pass
    return {"count": count, "bytes": total, "dir": str(CACHE_DIR)}


def clear_cache() -> dict:
    """Delete every cached tile. Returns the count removed."""
    before = cache_status()
    if CACHE_DIR.exists():
        try:
            shutil.rmtree(CACHE_DIR)
        except OSError as e:
            log.warning("tile cache clear failed: %s", e)
    return {"removed": before["count"], "freed_bytes": before["bytes"]}
