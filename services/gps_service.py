"""Streaming gpsd client.

Talks to gpsd the same way `cgps` does: opens a TCP connection to
2947, sends ``?WATCH={"enable":true,"json":true}``, and reads
newline-delimited JSON messages as they arrive (TPV for time /
position / velocity, SKY for satellites). The latest fix is exposed
via :pyattr:`GPSService.fix`.

Pure asyncio — no thread, no `gpsd-py3` dependency. Reconnects
automatically with bounded backoff if gpsd goes away.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime
from typing import Optional

from models import GPSFix

log = logging.getLogger(__name__)


WATCH_CMD = b'?WATCH={"enable":true,"json":true}\n'


class GPSService:
    def __init__(self, host: str = "127.0.0.1", port: int = 2947, poll_s: float = 1.0):
        self.host = host
        self.port = port
        self.poll_s = poll_s  # retained for API compatibility; used as min backoff
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
        backoff = max(self.poll_s, 1.0)
        while not self._stop.is_set():
            try:
                await self._stream_once()
                backoff = max(self.poll_s, 1.0)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self._connected = False
                log.debug("gpsd stream ended: %s", e)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=backoff)
            except asyncio.TimeoutError:
                pass
            backoff = min(backoff * 1.5, 10.0)

    async def _stream_once(self) -> None:
        reader, writer = await asyncio.open_connection(self.host, self.port)
        try:
            writer.write(WATCH_CMD)
            await writer.drain()
            self._connected = True
            log.info("gpsd connected to %s:%d (streaming)", self.host, self.port)
            while not self._stop.is_set():
                line = await reader.readline()
                if not line:
                    raise ConnectionError("gpsd closed connection")
                try:
                    msg = json.loads(line.decode("utf-8", errors="replace"))
                except json.JSONDecodeError:
                    continue
                self._handle(msg)
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass
            self._connected = False

    def _handle(self, msg: dict) -> None:
        cls = msg.get("class")
        if cls == "TPV":
            self._handle_tpv(msg)
        elif cls == "SKY":
            self._handle_sky(msg)
        # VERSION / DEVICE / DEVICES / WATCH ignored — informational only

    def _handle_tpv(self, msg: dict) -> None:
        f = self._fix.model_copy()
        mode = msg.get("mode")
        if mode is not None:
            f.mode = mode
        if "lat" in msg:
            f.lat = msg["lat"]
        if "lon" in msg:
            f.lon = msg["lon"]
        # gpsd >= 3.20 prefers altHAE/altMSL over the deprecated 'alt'
        alt = msg.get("altHAE")
        if alt is None:
            alt = msg.get("altMSL")
        if alt is None:
            alt = msg.get("alt")
        if alt is not None:
            f.alt = alt
        for k in ("speed", "track", "climb"):
            if k in msg:
                setattr(f, k, msg[k])
        # Horizontal error: prefer eph (95% confidence), fall back to max(epx, epy)
        eph = msg.get("eph")
        if eph is None:
            epx, epy = msg.get("epx"), msg.get("epy")
            if epx is not None and epy is not None:
                eph = max(epx, epy)
        if eph is not None:
            f.error_h = eph
        if "epv" in msg:
            f.error_v = msg["epv"]
        t = msg.get("time")
        if isinstance(t, str):
            try:
                f.time = datetime.fromisoformat(t.replace("Z", "+00:00"))
            except ValueError:
                pass
        self._fix = f

    def _handle_sky(self, msg: dict) -> None:
        f = self._fix.model_copy()
        sats = msg.get("satellites") or []
        # Prefer the per-sat array when present — it's the most authoritative
        # source (we can see exactly which sats are 'used'). Fall back to
        # gpsd 3.20+ summary fields nSat/uSat. Older gpsd builds with no
        # satellite info at all leave the prior counts intact via model_copy
        # rather than wiping them.
        if sats:
            f.sats_visible = len(sats)
            f.sats_used = sum(1 for s in sats if s.get("used"))
        elif "nSat" in msg or "uSat" in msg:
            if "nSat" in msg:
                f.sats_visible = msg["nSat"]
            if "uSat" in msg:
                f.sats_used = msg["uSat"]
        else:
            log.debug("SKY without satellite counts (will rely on prior values): %s", msg)
        self._fix = f
