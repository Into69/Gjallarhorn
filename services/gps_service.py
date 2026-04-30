from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Optional

from models import GPSFix

log = logging.getLogger(__name__)


class GPSService:
    """Async wrapper around gpsd. Falls back to a mock fix when gpsd is unreachable.

    The gpsd-py3 lib is sync, so we drive it from a thread and expose the latest
    fix via an asyncio.Event-protected attribute.
    """

    def __init__(self, host: str = "127.0.0.1", port: int = 2947, poll_s: float = 1.0):
        self.host = host
        self.port = port
        self.poll_s = poll_s
        self._fix = GPSFix()
        self._connected = False
        self._task: Optional[asyncio.Task] = None
        self._stop = asyncio.Event()

    @property
    def fix(self) -> GPSFix:
        return self._fix

    @property
    def connected(self) -> bool:
        return self._connected

    async def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            await asyncio.wait([self._task], timeout=2)

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                fix = await asyncio.to_thread(self._poll_once)
                if fix is not None:
                    self._fix = fix
                    self._connected = True
            except Exception as e:
                self._connected = False
                log.debug("gpsd poll failed: %s", e)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.poll_s)
            except asyncio.TimeoutError:
                pass

    def _poll_once(self) -> GPSFix | None:
        try:
            import gpsd  # gpsd-py3
        except ImportError:
            return None
        try:
            gpsd.connect(host=self.host, port=self.port)
            packet = gpsd.get_current()
        except Exception as e:
            log.debug("gpsd unreachable: %s", e)
            return None

        mode = getattr(packet, "mode", 0)
        fix = GPSFix(mode=mode)
        if mode >= 2:
            try:
                fix.lat, fix.lon = packet.position()
            except Exception:
                pass
        if mode >= 3:
            try:
                fix.alt = packet.altitude()
            except Exception:
                pass
        try:
            fix.speed = packet.speed()
        except Exception:
            pass
        try:
            fix.track = packet.movement().get("track")
        except Exception:
            pass
        try:
            err = getattr(packet, "error", {}) or {}
            fix.error_h = err.get("x")
            fix.error_v = err.get("v")
        except Exception:
            pass
        try:
            fix.sats_visible = getattr(packet, "sats", None)
            fix.sats_used = getattr(packet, "sats_valid", None)
        except Exception:
            pass
        try:
            t = packet.get_time()
            fix.time = t if isinstance(t, datetime) else None
        except Exception:
            pass
        return fix
