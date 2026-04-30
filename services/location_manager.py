from __future__ import annotations

import logging
import math
from datetime import datetime
from typing import Optional

from models import GPSFix, SensorLocation
import database as db

log = logging.getLogger(__name__)


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

    async def update_with_fix(self, fix: GPSFix, threshold_m: float, label_template: str) -> Optional[int]:
        if fix.mode < 2 or fix.lat is None or fix.lon is None:
            return self._active_id

        if self._active_id is None:
            new_id = await db.create_location(
                lat=fix.lat, lon=fix.lon, radius_m=threshold_m, label=None
            )
            label = label_template.format(id=new_id, lat=fix.lat, lon=fix.lon)
            await db.update_location_label(new_id, label)
            self._active_id = new_id
            self._active_lat = fix.lat
            self._active_lon = fix.lon
            log.info("Opened first sensor location id=%s @ %.6f,%.6f", new_id, fix.lat, fix.lon)
            return new_id

        d = haversine_m(self._active_lat, self._active_lon, fix.lat, fix.lon)
        if d > threshold_m:
            new_id = await db.create_location(
                lat=fix.lat, lon=fix.lon, radius_m=threshold_m, label=None
            )
            label = label_template.format(id=new_id, lat=fix.lat, lon=fix.lon)
            await db.update_location_label(new_id, label)
            self._active_id = new_id
            self._active_lat = fix.lat
            self._active_lon = fix.lon
            log.info("Sensor moved %.1fm > %.1fm; opened location id=%s", d, threshold_m, new_id)
        else:
            await db.touch_location(self._active_id)

        return self._active_id


location_manager = LocationManager()
