"""Passive WiFi probe-request scanner.

Spawns `tshark` on a monitor-mode interface and streams probe-request
frames as they're captured. Each parsed probe is dispatched via an
async callback the orchestrator wires up to do location tagging,
device upsert, and alert evaluation.

The interface must already be in monitor mode before the scanner
starts — we deliberately don't manage that ourselves because misuse
can take down a user's wireless connectivity. A typical setup is:

    sudo ip link set wlan1 down
    sudo iw dev wlan1 set type monitor
    sudo ip link set wlan1 up

Or via airmon-ng:

    sudo airmon-ng start wlan1   # creates wlan1mon

Tshark needs CAP_NET_RAW + CAP_NET_ADMIN (or root) on the capture
interface, the same caps the existing wifi_scanner needs.
"""
from __future__ import annotations

import asyncio
import logging
import shutil
import time
from typing import Awaitable, Callable, Optional

log = logging.getLogger(__name__)

# Backoff between auto-restarts of the tshark subprocess if it dies.
_RESTART_BACKOFF_S = 5.0

ProbeCallback = Callable[[dict], Awaitable[None]]


class ProbeScanner:
    """Manages the tshark subprocess and dispatches parsed probes.

    Single-instance: one capture per process. Switching interfaces calls
    stop() then start() again."""

    def __init__(self) -> None:
        self._task: Optional[asyncio.Task] = None
        self._proc: Optional[asyncio.subprocess.Process] = None
        self._stop = asyncio.Event()
        self._iface: Optional[str] = None
        self._on_probe: Optional[ProbeCallback] = None
        # Status surface
        self._running = False
        self._last_error: Optional[str] = None
        self._last_probe_at: Optional[float] = None
        self._probe_count: int = 0
        self._started_at: Optional[float] = None

    @property
    def running(self) -> bool:
        return self._running

    @property
    def interface(self) -> Optional[str]:
        return self._iface

    def status(self) -> dict:
        return {
            "running": self._running,
            "interface": self._iface,
            "tshark_available": shutil.which("tshark") is not None,
            "last_error": self._last_error,
            "last_probe_at": self._last_probe_at,
            "probe_count": self._probe_count,
            "started_at": self._started_at,
        }

    async def start(self, interface: str, on_probe: ProbeCallback) -> None:
        """Start (or restart) the scanner on the given interface."""
        if self._task is not None and self._iface == interface:
            self._on_probe = on_probe
            return  # already running on this interface
        await self.stop()
        self._iface = interface
        self._on_probe = on_probe
        self._stop.clear()
        self._last_error = None
        self._probe_count = 0
        self._started_at = time.time()
        self._task = asyncio.create_task(self._run_forever())

    async def stop(self) -> None:
        if self._task is None:
            return
        self._stop.set()
        if self._proc is not None and self._proc.returncode is None:
            try:
                self._proc.terminate()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(self._proc.wait(), timeout=3.0)
            except asyncio.TimeoutError:
                try:
                    self._proc.kill()
                except ProcessLookupError:
                    pass
                await self._proc.wait()
        try:
            await asyncio.wait_for(self._task, timeout=5.0)
        except asyncio.TimeoutError:
            pass
        finally:
            self._task = None
            self._proc = None
            self._running = False

    async def _run_forever(self) -> None:
        if shutil.which("tshark") is None:
            self._last_error = "tshark not found in PATH (install: apt install tshark)"
            log.error("probe scanner: %s", self._last_error)
            return
        while not self._stop.is_set():
            try:
                await self._run_once()
            except Exception as e:
                self._last_error = f"{type(e).__name__}: {e}"
                log.exception("probe scanner crashed: %s", e)
            self._running = False
            if self._stop.is_set():
                return
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=_RESTART_BACKOFF_S)
                return  # stop() was signalled while we were waiting
            except asyncio.TimeoutError:
                pass  # backoff complete, loop and restart

    async def _run_once(self) -> None:
        cmd = [
            "tshark",
            "-i", self._iface or "",
            "-l",                 # line-buffered output
            "-T", "fields",
            "-E", "separator=|",
            "-e", "wlan.sa",
            "-e", "wlan_radio.signal_dbm",
            "-e", "wlan.ssid",
            "-e", "wlan_radio.channel",
            "-f", "type mgt subtype probe-req",
            "-Q",                 # suppress packet-count summary
        ]
        log.info("probe scanner: starting %s", " ".join(cmd))
        self._proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        self._running = True
        self._last_error = None

        # Drain stderr concurrently so its buffer doesn't fill, and so we can
        # surface tshark's complaints (bad iface, permission denied, etc.).
        stderr_task = asyncio.create_task(self._drain_stderr())
        try:
            assert self._proc.stdout is not None
            while not self._stop.is_set():
                line = await self._proc.stdout.readline()
                if not line:
                    break
                await self._handle_line(line.decode("utf-8", errors="replace").rstrip())
        finally:
            stderr_task.cancel()
            try:
                await stderr_task
            except (asyncio.CancelledError, Exception):
                pass

    async def _drain_stderr(self) -> None:
        if self._proc is None or self._proc.stderr is None:
            return
        while True:
            line = await self._proc.stderr.readline()
            if not line:
                return
            text = line.decode("utf-8", errors="replace").rstrip()
            if not text:
                continue
            # Filter the routine "Capturing on …" / drop-stats noise
            low = text.lower()
            if "capturing on" in low or "packets dropped" in low:
                continue
            self._last_error = text
            log.warning("tshark stderr: %s", text)

    async def _handle_line(self, line: str) -> None:
        if not line or self._on_probe is None:
            return
        parts = line.split("|")
        while len(parts) < 4:
            parts.append("")
        mac = parts[0].strip().lower()
        rssi_s = parts[1].strip()
        ssid = parts[2]                 # raw — may contain whitespace
        channel_s = parts[3].strip()
        if len(mac) != 17:
            return                      # malformed
        try:
            rssi = int(rssi_s) if rssi_s else None
        except ValueError:
            rssi = None
        try:
            channel = int(channel_s) if channel_s else None
        except ValueError:
            channel = None

        probe = {
            "mac": mac,
            "rssi": rssi,
            "ssid": ssid,               # may be empty (wildcard probe)
            "channel": channel,
            "randomized": _is_randomized_mac(mac),
        }
        self._last_probe_at = time.time()
        self._probe_count += 1
        try:
            await self._on_probe(probe)
        except Exception as e:
            log.exception("probe callback failed: %s", e)


def _is_randomized_mac(mac: str) -> bool:
    """Locally-administered bit set on the first byte indicates a
    privacy-randomized MAC (per IEEE 802 — bit 1 of the first octet)."""
    try:
        first = int(mac.split(":")[0], 16)
    except (ValueError, IndexError):
        return False
    return bool(first & 0b00000010)


probe_scanner = ProbeScanner()
