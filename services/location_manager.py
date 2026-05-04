from __future__ import annotations

import logging
import math
from datetime import datetime
from typing import Optional

from models import GPSFix, SensorLocation
import database as db

log = logging.getLogger(__name__)

# Safety cap for the dynamic radius — a sensor on a highway shouldn't
# generate kilometres-wide bubbles even if speed × t_s implies it.
DYNAMIC_RADIUS_MAX_M = 5000.0


def effective_radius_m(
    static_min_m: float, *, speed_mps: float | None,
    dynamic_enabled: bool, dynamic_t_s: float,
) -> float:
    """Resolve the radius for a new bubble. With dynamic adjustment off (or
    speed unknown / zero) we use the static minimum. When on, the radius
    grows linearly with speed: at constant speed v, the sensor reaches the
    bubble edge t_s seconds after the bubble opened."""
    if not dynamic_enabled or speed_mps is None or speed_mps <= 0:
        return round(static_min_m, 2)
    raw = min(static_min_m + speed_mps * max(0.0, dynamic_t_s), DYNAMIC_RADIUS_MAX_M)
    return round(raw, 2)


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    R = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


class LocationManager:
    """Owns the 'active sensor location'. When the GPS fix moves further than
    `new_location_distance_m` from the active location centroid, a new location
    row is opened and becomes active. New device sightings get tagged with the
    active location id, so each location ends up with its own list."""

    def __init__(self) -> None:
        self._active_id: Optional[int] = None
        self._active_lat: Optional[float] = None
        self._active_lon: Optional[float] = None

    @property
    def active_id(self) -> Optional[int]:
        return self._active_id

    async def update_with_fix(
        self, fix: GPSFix, *,
        static_threshold_m: float,
        label_template: str,
        dynamic_enabled: bool = False,
        dynamic_t_s: float = 60.0,
    ) -> Optional[int]:
        if fix.mode < 2 or fix.lat is None or fix.lon is None:
            return self._active_id

        # If the fix falls inside an existing location's radius, snap to that one
        # (re-using a previously mapped place instead of opening a duplicate).
        # If multiple radii overlap, the nearest centroid wins.
        matched = await self._find_containing_location(fix.lat, fix.lon)
        if matched is not None:
            if self._active_id != matched["id"]:
                log.info(
                    "Sensor entered existing location id=%s @ %.6f,%.6f; switching active",
                    matched["id"], matched["lat"], matched["lon"],
                )
            self._active_id = matched["id"]
            self._active_lat = matched["lat"]
            self._active_lon = matched["lon"]
            await db.touch_location(self._active_id)
            return self._active_id

        # Outside every existing radius — open a fresh location, sized for the
        # sensor's current motion if dynamic adjustment is on.
        radius = effective_radius_m(
            static_threshold_m, speed_mps=fix.speed,
            dynamic_enabled=dynamic_enabled, dynamic_t_s=dynamic_t_s,
        )
        return await self._open_new_location(fix.lat, fix.lon, radius, label_template)

    async def _find_containing_location(self, lat: float, lon: float) -> Optional[dict]:
        """Pick the location whose radius the fix falls inside. When several
        match, a drawn (manual) geofence is preferred over auto-clusters —
        user-drawn intent wins. Within the same source class, the nearest
        centroid wins."""
        matches: list[tuple[float, dict]] = []
        for loc in await db.list_location_centroids():
            d = haversine_m(loc["lat"], loc["lon"], lat, lon)
            if d <= loc["radius_m"]:
                matches.append((d, loc))
        if not matches:
            return None
        manual = [m for m in matches if m[1].get("source") == "manual"]
        pool = manual if manual else matches
        pool.sort(key=lambda x: x[0])
        return pool[0][1]

    async def _open_new_location(
        self, lat: float, lon: float, threshold_m: float, label_template: str
    ) -> int:
        new_id = await db.create_location(lat=lat, lon=lon, radius_m=threshold_m, label=None)
        label = label_template.format(id=new_id, lat=lat, lon=lon)
        await db.update_location_label(new_id, label)
        self._active_id = new_id
        self._active_lat = lat
        self._active_lon = lon
        log.info("Opened new sensor location id=%s @ %.6f,%.6f r=%.1fm",
                 new_id, lat, lon, threshold_m)
        return new_id


location_manager = LocationManager()
