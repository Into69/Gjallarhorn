"""HackRF-driven BLE advertising scanner.

Wraps the ``btle_rx`` tool from JiaoXianjun/BTLE
(https://github.com/JiaoXianjun/BTLE) as an asyncio subprocess and
dispatches parsed advertising packets through an async callback into
the orchestrator. Complementary to the bleak-based BLE scanner — both
can run simultaneously, and the dual-mode merge in db.upsert_bluetooth
collapses the same MAC seen by both paths into one row.

Why an SDR-based scanner alongside bleak:
  - btle_rx sees the raw 802.15.4 PHY, including advertisements bleak's
    BlueZ stack filters out (legacy modes, scan-response-only devices,
    weak / fragmented packets at distance).
  - RSSI is the absolute dBm the radio measures, not the controller's
    massaged value.
  - It can sit on one ad channel while the OS adapter scans elsewhere.

Channel-hopping limits:
  - HackRF is half-duplex and tunes to a single frequency at a time.
  - btle_rx is a single-channel tool; we channel-hop by spawning /
    killing the process per channel. Process startup is ~50-100 ms
    on a Pi, so practical dwell is ~500 ms / channel minimum.

The scanner is silently disabled when ``btle_rx`` isn't on PATH so
the rest of Gjallarhorn still runs on hosts without the SDR toolchain
installed — same shape as the BLE scanner's missing-bleak fallback."""
from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import time
from pathlib import Path
from typing import Awaitable, Callable, Optional

log = logging.getLogger(__name__)

# Default dwell per advertising channel. 1000 ms is comfortable: BLE
# advertisers cycle through 37→38→39 every adv_interval (default ~20 ms
# but can be up to 10 s for low-power tags), so a 1 s window catches
# every advertiser at least once for the typical interval range.
_DEFAULT_HOP_MS = 1000
# BLE advertising channel indexes — these are PHY channel numbers, not
# the L2CAP "data" channels (0-36) BLE uses for connections.
_AD_CHANNELS = (37, 38, 39)
# How long to wait for btle_rx to exit cleanly before SIGKILL on hop.
_PROC_TERMINATE_TIMEOUT_S = 1.5
# Backoff before the loop tries to spawn again after a fatal error.
_RESTART_BACKOFF_S = 5.0

HackRFBleCallback = Callable[[dict], Awaitable[None]]


# Standard install locations to probe when the binary isn't on PATH.
# `sudo make install` from JiaoXianjun/BTLE drops btle_rx into
# /usr/local/bin, which is not on PATH for processes spawned by
# systemd's default service environment (PATH is stripped to
# /usr/bin:/bin). Falling back to these explicit paths makes the
# detection work even when the operator runs gjallarhorn under
# systemd without a custom PATH= line in the unit file.
_BTLE_RX_CANDIDATES = (
    "/usr/local/bin/btle_rx",
    "/usr/bin/btle_rx",
    "/opt/btle/btle_rx",
    str(Path.home() / "BTLE/host/build/btle_rx"),
)
_HACKRF_INFO_CANDIDATES = (
    "/usr/local/bin/hackrf_info",
    "/usr/bin/hackrf_info",
)


def _resolve_binary(name: str, candidates: tuple) -> Optional[str]:
    """Find an executable by name. Checks $PATH first via shutil.which,
    then falls through to the candidate list for the systemd-stripped-
    PATH case. Returns the first hit (or None)."""
    hit = shutil.which(name)
    if hit:
        return hit
    for p in candidates:
        if os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    return None


def btle_rx_path() -> Optional[str]:
    """Resolved path to the btle_rx binary, or None if not found.
    Use this when spawning the subprocess — passing an absolute path
    survives PATH variance between interactive shells and service
    managers."""
    return _resolve_binary("btle_rx", _BTLE_RX_CANDIDATES)


def hackrf_info_path() -> Optional[str]:
    return _resolve_binary("hackrf_info", _HACKRF_INFO_CANDIDATES)


def btle_rx_available() -> bool:
    """True iff the btle_rx binary is resolvable, either via PATH or
    via the standard /usr/local/bin install location."""
    return btle_rx_path() is not None


def hackrf_info_available() -> bool:
    return hackrf_info_path() is not None


class HackRFBleScanner:
    """One HackRF / one btle_rx instance per process. Switching serial
    or config calls stop() then start() again, same shape as
    ProbeScanner."""

    def __init__(self) -> None:
        self._task: Optional[asyncio.Task] = None
        self._proc: Optional[asyncio.subprocess.Process] = None
        self._stop = asyncio.Event()
        self._cb: Optional[HackRFBleCallback] = None
        # Config (filled by start())
        self._serial: Optional[str] = None
        self._gain: int = 40
        self._min_rssi: int = -90
        self._hop_ms: int = _DEFAULT_HOP_MS
        self._channels: tuple[int, ...] = _AD_CHANNELS
        # Status
        self._running = False
        self._last_error: Optional[str] = None
        self._last_packet_at: Optional[float] = None
        self._packet_count: int = 0
        self._started_at: Optional[float] = None
        self._current_channel: Optional[int] = None
        # Per-session set so a one-off hardware error on a channel
        # doesn't kill the whole hop loop. Cleared on each start() so
        # the operator can fix the issue and re-test without editing
        # settings.
        self._disabled_channels: set[int] = set()

    # ── status surface ──────────────────────────────────────────────
    @property
    def running(self) -> bool:
        return self._running

    @property
    def last_error(self) -> Optional[str]:
        return self._last_error

    @property
    def last_packet_at(self) -> Optional[float]:
        return self._last_packet_at

    @property
    def packet_count(self) -> int:
        return self._packet_count

    @property
    def started_at(self) -> Optional[float]:
        return self._started_at

    @property
    def current_channel(self) -> Optional[int]:
        return self._current_channel

    @property
    def disabled_channels(self) -> set[int]:
        return set(self._disabled_channels)

    def stats(self) -> dict:
        """Snapshot for the /api status surface + Mission tile."""
        bin_path = btle_rx_path()
        return {
            "running": self._running,
            "binary_available": bin_path is not None,
            "binary_path": bin_path,
            "serial": self._serial,
            "gain": self._gain,
            "min_rssi": self._min_rssi,
            "hop_ms": self._hop_ms,
            "channels": list(self._channels),
            "current_channel": self._current_channel,
            "disabled_channels": sorted(self._disabled_channels),
            "packet_count": self._packet_count,
            "last_packet_at": self._last_packet_at,
            "started_at": self._started_at,
            "last_error": self._last_error,
        }

    # ── lifecycle ───────────────────────────────────────────────────
    async def start(
        self,
        *,
        callback: HackRFBleCallback,
        serial: Optional[str] = None,
        gain: int = 40,
        min_rssi: int = -90,
        hop_ms: int = _DEFAULT_HOP_MS,
        channels: Optional[list[int]] = None,
    ) -> None:
        """Begin the channel-hop loop. Silently no-ops when btle_rx
        isn't on PATH — the operator can install it and toggle the
        Settings switch to retry without restarting the app."""
        if self._task and not self._task.done():
            return
        if not btle_rx_available():
            self._last_error = (
                "btle_rx binary not found on PATH — install "
                "JiaoXianjun/BTLE first (see README)"
            )
            return
        self._cb = callback
        self._serial = (serial or "").strip() or None
        self._gain = max(0, min(62, int(gain)))   # HackRF VGA: 0–62 in 2 dB steps
        self._min_rssi = int(min_rssi)
        self._hop_ms = max(200, int(hop_ms))      # below 200 ms the spawn cost dominates
        self._channels = tuple(
            int(c) for c in (channels or _AD_CHANNELS)
            if int(c) in _AD_CHANNELS
        ) or _AD_CHANNELS
        self._stop.clear()
        self._last_error = None
        self._packet_count = 0
        self._started_at = time.time()
        self._disabled_channels.clear()
        self._task = asyncio.create_task(self._run_loop(), name="hackrf-ble-scanner")

    async def stop(self) -> None:
        """Tear down the loop and any live btle_rx process."""
        self._stop.set()
        await self._kill_proc()
        if self._task:
            try:
                await asyncio.wait_for(self._task, timeout=3.0)
            except asyncio.TimeoutError:
                self._task.cancel()
                try:
                    await self._task
                except (asyncio.CancelledError, Exception):
                    pass
            self._task = None
        self._running = False
        self._current_channel = None

    async def _kill_proc(self) -> None:
        if not self._proc:
            return
        try:
            if self._proc.returncode is None:
                self._proc.terminate()
                try:
                    await asyncio.wait_for(self._proc.wait(),
                                           timeout=_PROC_TERMINATE_TIMEOUT_S)
                except asyncio.TimeoutError:
                    self._proc.kill()
                    await self._proc.wait()
        except Exception as e:
            log.debug("hackrf scanner: subprocess teardown error: %s", e)
        finally:
            self._proc = None

    # ── main loop ───────────────────────────────────────────────────
    async def _run_loop(self) -> None:
        """Outer channel-hop loop. For each enabled channel: spawn
        btle_rx, drain stdout for hop_ms, kill the process, advance."""
        self._running = True
        try:
            while not self._stop.is_set():
                live = [c for c in self._channels
                        if c not in self._disabled_channels]
                if not live:
                    # Every channel got disabled — pause briefly then
                    # try the original set again so a transient hardware
                    # error doesn't permanently silence the scanner.
                    log.warning("hackrf scanner: all channels disabled; "
                                "retrying full set in %ds", _RESTART_BACKOFF_S)
                    self._disabled_channels.clear()
                    try:
                        await asyncio.wait_for(self._stop.wait(),
                                               timeout=_RESTART_BACKOFF_S)
                    except asyncio.TimeoutError:
                        pass
                    continue
                for ch in live:
                    if self._stop.is_set():
                        break
                    try:
                        await self._scan_channel(ch)
                    except Exception as e:
                        log.warning("hackrf scanner: channel %d failed: %s", ch, e)
                        self._last_error = f"channel {ch}: {e}"
                        self._disabled_channels.add(ch)
        finally:
            self._running = False
            self._current_channel = None
            await self._kill_proc()

    async def _scan_channel(self, ch: int) -> None:
        """Spawn btle_rx pinned to one advertising channel; drain its
        output for hop_ms milliseconds; tear down."""
        # Always use the resolved absolute path so the spawn works
        # under systemd's stripped PATH (only /usr/bin:/bin by default;
        # btle_rx typically lives in /usr/local/bin).
        binary = btle_rx_path()
        if not binary:
            self._last_error = "btle_rx not resolvable on PATH or in /usr/local/bin"
            self._stop.set()
            return
        cmd: list[str] = [binary, "-c", str(ch), "-g", str(self._gain)]
        # btle_rx's `-s` selects a HackRF by serial when more than one
        # is plugged in. Skipped when only one is attached.
        if self._serial:
            cmd.extend(["-s", self._serial])
        try:
            self._proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError:
            # Binary disappeared mid-run — log + halt the loop so we
            # don't busy-spin on respawn attempts.
            self._last_error = "btle_rx binary not found on PATH"
            self._stop.set()
            return
        self._current_channel = ch
        # Drain stderr concurrently so its buffer doesn't fill (and so
        # we can surface a hardware error message if the spawn was
        # accepted but the device is busy).
        stderr_task = asyncio.create_task(self._drain_stderr(self._proc))
        deadline = time.time() + (self._hop_ms / 1000.0)
        try:
            assert self._proc.stdout is not None
            while not self._stop.is_set():
                remaining = deadline - time.time()
                if remaining <= 0:
                    break
                try:
                    line = await asyncio.wait_for(
                        self._proc.stdout.readline(),
                        timeout=remaining,
                    )
                except asyncio.TimeoutError:
                    break
                if not line:
                    # Process exited on its own (e.g. HackRF unplugged).
                    rc = self._proc.returncode
                    if rc is not None and rc != 0:
                        self._last_error = (
                            f"btle_rx exited with code {rc} on channel {ch}"
                        )
                    break
                await self._handle_line(
                    line.decode("utf-8", errors="replace").rstrip(), ch,
                )
        finally:
            stderr_task.cancel()
            try:
                await stderr_task
            except (asyncio.CancelledError, Exception):
                pass
            await self._kill_proc()
            self._current_channel = None

    async def _drain_stderr(self, proc: asyncio.subprocess.Process) -> None:
        if proc.stderr is None:
            return
        while not self._stop.is_set():
            line = await proc.stderr.readline()
            if not line:
                return
            text = line.decode("utf-8", errors="replace").rstrip()
            if not text:
                continue
            # btle_rx is chatty on init ("hackrf_open returned 0", board
            # info dumps); only log lines that look like errors.
            lower = text.lower()
            if "error" in lower or "fail" in lower or "not found" in lower:
                self._last_error = text
                log.warning("hackrf scanner [%s]: %s",
                            "btle_rx-stderr", text)

    # ── output parser ───────────────────────────────────────────────
    # btle_rx prints one decoded packet per line. The exact column set
    # depends on which build, but the fields we care about appear in
    # readable form:
    #   "AdvA: aa:bb:cc:dd:ee:ff"   (advertiser MAC)
    #   "RSSI: -47"                 (dBm)
    #   "PDU_Type: ADV_IND"         (advertising mode)
    #   "AdvData: 02 01 06 ..."     (hex octets of the advertising payload)
    _RE_ADVA = re.compile(r"AdvA[:\s=]+([0-9A-Fa-f]{2}(?::[0-9A-Fa-f]{2}){5})")
    _RE_RSSI = re.compile(r"RSSI[:\s=]+(-?\d+)")
    _RE_PDU = re.compile(r"PDU[_\s]Type[:\s=]+([A-Za-z_]+)")
    _RE_ADVDATA = re.compile(r"Adv(?:Data|_Data)[:\s=]+([0-9A-Fa-f\s]+?)(?:\s{2,}|$)")

    async def _handle_line(self, line: str, ch: int) -> None:
        m_mac = self._RE_ADVA.search(line)
        if not m_mac:
            return
        mac = m_mac.group(1).upper()
        m_rssi = self._RE_RSSI.search(line)
        rssi = int(m_rssi.group(1)) if m_rssi else -100
        if rssi < self._min_rssi:
            return
        m_pdu = self._RE_PDU.search(line)
        pdu_type = m_pdu.group(1) if m_pdu else None
        m_data = self._RE_ADVDATA.search(line)
        adv_data = (m_data.group(1).replace(" ", "") if m_data else "")
        self._packet_count += 1
        self._last_packet_at = time.time()
        if self._cb is None:
            return
        try:
            await self._cb({
                "mac": mac,
                "rssi": rssi,
                "pdu_type": pdu_type,
                "adv_data_hex": adv_data,
                "channel": ch,
            })
        except Exception as e:
            log.warning("hackrf scanner: dispatch error for %s: %s", mac, e)


# Module-level singleton so the orchestrator can wire one capture
# without juggling object ownership.
hackrf_ble_scanner = HackRFBleScanner()


# ── BLE advertising-data parser ────────────────────────────────────
# AdvData is a sequence of length-tagged AD structures (BLE Core Spec
# v5.3 Vol 3 Part C §11). Each structure: 1 byte length (of type +
# data), 1 byte AD type, N bytes data. We decode the AD types most
# useful to the rest of Gjallarhorn — anything else surfaces as raw
# hex under the unknown_types dict so the operator can inspect it
# without us having to teach the parser every assigned-numbers entry.
_AD_TYPE_FLAGS = 0x01
_AD_TYPE_INC_UUID16 = 0x02
_AD_TYPE_COMPLETE_UUID16 = 0x03
_AD_TYPE_INC_UUID32 = 0x04
_AD_TYPE_COMPLETE_UUID32 = 0x05
_AD_TYPE_INC_UUID128 = 0x06
_AD_TYPE_COMPLETE_UUID128 = 0x07
_AD_TYPE_SHORT_NAME = 0x08
_AD_TYPE_COMPLETE_NAME = 0x09
_AD_TYPE_TX_POWER = 0x0A
_AD_TYPE_SERVICE_DATA_16 = 0x16
_AD_TYPE_SERVICE_DATA_32 = 0x20
_AD_TYPE_SERVICE_DATA_128 = 0x21
_AD_TYPE_MFG = 0xFF


def parse_ble_adv_data(hex_str: str) -> dict:
    """Decode an AdvData hex blob into the same dict shape the bleak
    scanner produces (manufacturer_data: dict[int, hex_str],
    service_uuids: list[str], service_data: dict[str, hex_str], name,
    tx_power, flags). Anything we don't decode lands under
    'unknown_types' as type → hex so it's not silently dropped."""
    out: dict = {
        "manufacturer_data": {},
        "service_uuids": [],
        "service_data": {},
        "name": None,
        "tx_power": None,
        "flags": None,
        "unknown_types": {},
    }
    if not hex_str:
        return out
    try:
        raw = bytes.fromhex(hex_str)
    except ValueError:
        return out
    i = 0
    n = len(raw)
    while i < n:
        length = raw[i]
        if length == 0 or i + length >= n + 1:
            break
        if i + 1 >= n:
            break
        ad_type = raw[i + 1]
        data = raw[i + 2:i + 1 + length]
        i += 1 + length
        if ad_type == _AD_TYPE_FLAGS and len(data) >= 1:
            out["flags"] = data[0]
        elif ad_type in (_AD_TYPE_INC_UUID16, _AD_TYPE_COMPLETE_UUID16):
            for j in range(0, len(data) - 1, 2):
                u = int.from_bytes(data[j:j + 2], "little")
                out["service_uuids"].append(f"0000{u:04x}-0000-1000-8000-00805f9b34fb")
        elif ad_type in (_AD_TYPE_INC_UUID32, _AD_TYPE_COMPLETE_UUID32):
            for j in range(0, len(data) - 3, 4):
                u = int.from_bytes(data[j:j + 4], "little")
                out["service_uuids"].append(f"{u:08x}-0000-1000-8000-00805f9b34fb")
        elif ad_type in (_AD_TYPE_INC_UUID128, _AD_TYPE_COMPLETE_UUID128):
            for j in range(0, len(data) - 15, 16):
                # 128-bit UUIDs are little-endian on the wire.
                b = data[j:j + 16][::-1]
                out["service_uuids"].append(
                    f"{b[0]:02x}{b[1]:02x}{b[2]:02x}{b[3]:02x}-"
                    f"{b[4]:02x}{b[5]:02x}-"
                    f"{b[6]:02x}{b[7]:02x}-"
                    f"{b[8]:02x}{b[9]:02x}-"
                    f"{b[10]:02x}{b[11]:02x}{b[12]:02x}{b[13]:02x}{b[14]:02x}{b[15]:02x}"
                )
        elif ad_type in (_AD_TYPE_SHORT_NAME, _AD_TYPE_COMPLETE_NAME):
            try:
                out["name"] = data.decode("utf-8", errors="replace").rstrip("\x00")
            except Exception:
                pass
        elif ad_type == _AD_TYPE_TX_POWER and len(data) >= 1:
            # Signed 8-bit dBm.
            out["tx_power"] = int.from_bytes(data[:1], "big", signed=True)
        elif ad_type == _AD_TYPE_SERVICE_DATA_16 and len(data) >= 2:
            u = int.from_bytes(data[:2], "little")
            uuid = f"0000{u:04x}-0000-1000-8000-00805f9b34fb"
            out["service_data"][uuid] = data[2:].hex()
        elif ad_type == _AD_TYPE_MFG and len(data) >= 2:
            company_id = int.from_bytes(data[:2], "little")
            # Match bleak's shape: dict[int, hex_str_of_remaining_bytes].
            out["manufacturer_data"][company_id] = data[2:].hex()
        else:
            out["unknown_types"][f"0x{ad_type:02x}"] = data.hex()
    return out
