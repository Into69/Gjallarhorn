"""Passive WiFi probe-request scanner.

Captures 802.11 probe-request frames from a monitor-mode interface
and dispatches each parsed probe through an async callback. Two
interchangeable backends:

  - ``tshark`` (default): spawns the system tshark binary as a
    subprocess. Pros — kernel-level BPF filter, low CPU, ubiquitous.
    Cons — system package install required (``apt install tshark``).

  - ``scapy``: uses the scapy Python library's AsyncSniffer in a
    background thread. Pros — pure-Python, no system install. Cons —
    higher CPU; the scapy install pulls a few transitive deps.

The interface must already be in monitor mode before the scanner
starts — we deliberately don't manage that ourselves because misuse
can take down a user's wireless connectivity. A typical setup:

    sudo ip link set wlan1 down
    sudo iw dev wlan1 set type monitor
    sudo ip link set wlan1 up

Both backends need CAP_NET_RAW + CAP_NET_ADMIN (or root) on the
capture interface, the same caps the existing wifi_scanner needs.
"""
from __future__ import annotations

import asyncio
import logging
import shutil
import time
from typing import Awaitable, Callable, Optional

from services.wifi_scanner import normalize_ssid

log = logging.getLogger(__name__)

# Backoff between auto-restarts of the capture if it dies.
_RESTART_BACKOFF_S = 5.0
# Per-(client, bssid) dedup window for data-frame dispatch. One event
# per pair per minute is plenty to keep the orchestrator's associated_bssids
# list current — every additional frame would just re-confirm the same
# linkage.
_DATA_DEDUP_WINDOW_S = 60.0

ProbeCallback = Callable[[dict], Awaitable[None]]

VALID_BACKENDS = ("tshark", "scapy")


def scapy_available() -> bool:
    try:
        import scapy  # noqa: F401
        return True
    except Exception:
        return False


class ProbeScanner:
    """Manages the active capture and dispatches parsed probes.

    Single-instance: one capture per process. Switching interface or
    backend calls stop() then start() again."""

    def __init__(self) -> None:
        self._task: Optional[asyncio.Task] = None
        self._proc: Optional[asyncio.subprocess.Process] = None
        self._stop = asyncio.Event()
        self._iface: Optional[str] = None
        self._backend: str = "tshark"
        self._auto_monitor: bool = False
        self._channels: list[int] = []
        # Default dwell matches probe_channel_hop_ms's default; the
        # orchestrator overrides via start(..., hop_ms=...).
        self._hop_ms: int = 100
        self._on_probe: Optional[ProbeCallback] = None
        # Bookkeeping for restoring the interface on stop.
        self._restore_type: Optional[str] = None
        # Status surface
        self._running = False
        self._last_error: Optional[str] = None
        self._last_probe_at: Optional[float] = None
        self._probe_count: int = 0
        self._started_at: Optional[float] = None
        self._current_channel: Optional[int] = None
        self._last_channel_error: Optional[str] = None
        self._channel_set_count: int = 0
        # Channels we've already warned about so we don't spam logs.
        self._warned_channels: set[int] = set()
        # Channels that `iw set channel` has rejected this session (e.g.
        # the kernel reports 'Unknown channel' on a 6 GHz channel for a
        # 2.4-GHz-only adapter). Disabled channels are skipped on every
        # subsequent hop so the loop doesn't waste a dwell tick on them.
        # Cleared on each start() so the operator can fix the underlying
        # cause and re-test without editing settings.
        self._disabled_channels: set[int] = set()
        # Scanner-level dedup for data-frame dispatch. Each data frame
        # tells us 'client X is talking to BSSID Y' — running the
        # orchestrator callback on every frame would drown the loop in
        # tens of thousands of identical-conclusion events per second.
        # Cap to one dispatch per (client, bssid) per
        # _DATA_DEDUP_WINDOW_S seconds and prune entries older than the
        # window on each insert so the map can't grow unbounded.
        self._recent_data_dispatch: dict[tuple[str, str], float] = {}

    @property
    def running(self) -> bool:
        return self._running

    @property
    def interface(self) -> Optional[str]:
        return self._iface

    @property
    def backend(self) -> str:
        return self._backend

    @property
    def auto_monitor(self) -> bool:
        return self._auto_monitor

    @property
    def channels(self) -> list[int]:
        return list(self._channels)

    def status(self) -> dict:
        return {
            "running": self._running,
            "interface": self._iface,
            "backend": self._backend,
            "tshark_available": shutil.which("tshark") is not None,
            "scapy_available": scapy_available(),
            "auto_monitor": self._auto_monitor,
            "channels": list(self._channels),
            "hop_ms": self._hop_ms,
            "disabled_channels": sorted(self._disabled_channels),
            "current_channel": self._current_channel,
            "last_error": self._last_error,
            "last_channel_error": self._last_channel_error,
            "channel_set_count": self._channel_set_count,
            "last_probe_at": self._last_probe_at,
            "probe_count": self._probe_count,
            "started_at": self._started_at,
        }

    async def start(
        self, interface: str, on_probe: ProbeCallback,
        *, backend: str = "tshark",
        auto_monitor: bool = False,
        channels: Optional[list[int]] = None,
        hop_ms: int = 100,
    ) -> None:
        """Start (or restart) the scanner on the given interface + backend."""
        if backend not in VALID_BACKENDS:
            raise ValueError(f"backend must be one of {VALID_BACKENDS}")
        channels = list(channels or [])
        # Clamp to a sane range. Below ~50 ms most drivers can't finish
        # the channel-set ioctl and re-tune the radio before we're on
        # to the next hop, so captures get nothing. Above 2000 ms it's
        # not really 'hopping' anymore.
        hop_ms = max(50, min(2000, int(hop_ms or 100)))
        if (self._task is not None and self._iface == interface
                and self._backend == backend
                and self._auto_monitor == auto_monitor
                and self._channels == channels
                and self._hop_ms == hop_ms):
            self._on_probe = on_probe
            return  # already running with the same config
        await self.stop()
        self._iface = interface
        self._backend = backend
        self._auto_monitor = auto_monitor
        self._channels = channels
        self._hop_ms = hop_ms
        self._on_probe = on_probe
        self._stop.clear()
        self._last_error = None
        self._probe_count = 0
        self._current_channel = None
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
        # Pre-flight: backend availability check
        if self._backend == "tshark" and shutil.which("tshark") is None:
            self._last_error = "tshark not found in PATH (apt install tshark)"
            log.error("probe scanner: %s", self._last_error)
            return
        if self._backend == "scapy" and not scapy_available():
            self._last_error = "scapy not installed (pip install scapy)"
            log.error("probe scanner: %s", self._last_error)
            return

        # Auto-prep monitor mode if asked. Records the prior type so we can
        # restore it on stop. If setup fails we bail rather than guessing.
        self._restore_type = None
        if self._auto_monitor and self._iface:
            prior, err = await _setup_monitor_mode(self._iface)
            if err:
                self._last_error = f"monitor mode setup failed: {err}"
                log.error("probe scanner: %s", self._last_error)
                return
            self._restore_type = prior or "managed"
            log.info("probe scanner: %s set to monitor (was %s)", self._iface, self._restore_type)

        # Channel hopper runs in parallel for as long as the scanner does.
        hop_task: Optional[asyncio.Task] = None
        if self._channels and self._iface:
            hop_task = asyncio.create_task(self._channel_hop())

        try:
            while not self._stop.is_set():
                try:
                    if self._backend == "tshark":
                        await self._run_tshark()
                    elif self._backend == "scapy":
                        await self._run_scapy()
                    else:
                        self._last_error = f"unknown backend: {self._backend}"
                        return
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
        finally:
            if hop_task is not None:
                hop_task.cancel()
                try:
                    await hop_task
                except (asyncio.CancelledError, Exception):
                    pass
            if self._restore_type and self._iface:
                log.info("probe scanner: restoring %s to %s", self._iface, self._restore_type)
                await _restore_interface(self._iface, self._restore_type)
                self._restore_type = None
            self._current_channel = None

    async def _channel_hop(self) -> None:
        """Cycle the interface through self._channels until stop is signalled.

        Channels the kernel rejects (`iw set channel` returns non-zero,
        typically 'Unknown channel' / EINVAL on adapters that don't
        support that band) are added to _disabled_channels and skipped
        on every subsequent iteration this session, so the loop doesn't
        burn its dwell budget retrying a hop the radio will never honour.
        """
        if not self._channels or not self._iface:
            return
        self._warned_channels.clear()
        self._disabled_channels.clear()
        self._last_channel_error = None
        i = 0
        while not self._stop.is_set():
            # Filter out anything the kernel has already rejected. Done
            # fresh each iteration so a 'set channel' that succeeded
            # earlier can clear its slot.
            active = [c for c in self._channels if c not in self._disabled_channels]
            if not active:
                log.error(
                    "probe scanner: every configured channel rejected by the kernel "
                    "on %s — exiting hop loop. Disable bad channels in Settings, or "
                    "restart the scanner to retry.",
                    self._iface,
                )
                self._last_channel_error = "all configured channels rejected by the kernel"
                self._current_channel = None
                return
            ch = active[i % len(active)]
            rc, _, err = await _run_cmd(["iw", "dev", self._iface, "set", "channel", str(ch)])
            if rc == 0:
                self._current_channel = ch
                self._channel_set_count += 1
                self._last_channel_error = None
            else:
                msg = (err.strip() or f"rc={rc}")
                self._last_channel_error = f"ch{ch}: {msg}"
                # Disable the channel for this session so the loop
                # doesn't keep trying it. Warn once so the operator can
                # see why their selection just shrunk.
                self._disabled_channels.add(ch)
                if ch not in self._warned_channels:
                    self._warned_channels.add(ch)
                    log.warning(
                        "probe scanner: iw set channel %s failed on %s: %s — "
                        "channel disabled for this session (%d of %d still active)",
                        ch, self._iface, msg,
                        len(self._channels) - len(self._disabled_channels),
                        len(self._channels),
                    )
            i += 1
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._hop_ms / 1000.0)
                return
            except asyncio.TimeoutError:
                pass

    # ── tshark backend ─────────────────────────────────────────────
    async def _run_tshark(self) -> None:
        # Capture every 802.11 frame that names a (client, BSSID) pair —
        # not just probe/assoc-requests. Matches Kismet's approach
        # (phy_80211.process_client): the BSSID is in the frame header,
        # so any management or data frame between a client and an AP
        # firms up the client-to-AP link without us having to guess.
        #
        # Filter (libpcap BPF):
        #   - Management subtypes that involve a specific client:
        #     probe-req / assoc-req+resp / reassoc-req+resp / auth /
        #     deauth / disassoc. Probe-resp + beacon are AP-side
        #     broadcasts — the active wifi_scanner already captures APs
        #     so we skip those here.
        #   - Every data frame (type 2). Direction bits (to-ds / from-ds)
        #     plus addr3 give us the client + BSSID; dedup'd at dispatch
        #     so the orchestrator only sees one event per (client, AP)
        #     per _DATA_DEDUP_WINDOW_S.
        bpf = (
            "(type mgt and ("
            "subtype probe-req or "
            "subtype assoc-req or subtype assoc-resp or "
            "subtype reassoc-req or subtype reassoc-resp or "
            "subtype auth or subtype deauth or subtype disassoc"
            ")) or type data"
        )
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
            "-e", "wlan.fc.type_subtype",
            "-e", "wlan.bssid",
            "-e", "wlan.da",
            "-e", "wlan.fc.tods",
            "-e", "wlan.fc.fromds",
            "-f", bpf,
            "-Q",                 # suppress packet-count summary
        ]
        log.info("probe scanner [tshark]: starting %s", " ".join(cmd))
        self._proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        self._running = True
        self._last_error = None

        # Drain stderr concurrently so its buffer doesn't fill, and so we can
        # surface tshark's complaints (bad iface, permission denied, etc.).
        stderr_task = asyncio.create_task(self._drain_tshark_stderr())
        try:
            assert self._proc.stdout is not None
            while not self._stop.is_set():
                line = await self._proc.stdout.readline()
                if not line:
                    break
                await self._handle_tshark_line(line.decode("utf-8", errors="replace").rstrip())
        finally:
            stderr_task.cancel()
            try:
                await stderr_task
            except (asyncio.CancelledError, Exception):
                pass

    async def _drain_tshark_stderr(self) -> None:
        if self._proc is None or self._proc.stderr is None:
            return
        while True:
            line = await self._proc.stderr.readline()
            if not line:
                return
            text = line.decode("utf-8", errors="replace").rstrip()
            if not text:
                continue
            low = text.lower()
            # tshark's normal "Capturing on …" / drop-stats summary.
            if "capturing on" in low or "packets dropped" in low:
                continue
            # Wireshark extcap / SDR (UHD) libraries log a banner at startup
            # at [INFO]/[DEBUG]/[NOTE] level — benign, just noise. Keep them
            # at debug so the Logs tab can still show them if needed.
            if low.startswith(("[info]", "[debug]", "[note]")):
                log.debug("tshark stderr: %s", text)
                continue
            self._last_error = text
            log.warning("tshark stderr: %s", text)

    async def _handle_tshark_line(self, line: str) -> None:
        if not line:
            return
        parts = line.split("|")
        while len(parts) < 9:
            parts.append("")
        sa = parts[0].strip().lower()
        rssi_s = parts[1].strip()
        ssid = parts[2]
        channel_s = parts[3].strip()
        subtype_s = parts[4].strip()
        bssid = parts[5].strip().lower()
        da = parts[6].strip().lower()
        tods = parts[7].strip()
        fromds = parts[8].strip()
        try:
            rssi = int(rssi_s) if rssi_s else None
        except ValueError:
            rssi = None
        try:
            channel = int(channel_s) if channel_s else None
        except ValueError:
            channel = None
        frame_type = _frame_type_from_subtype(subtype_s)
        if frame_type is None:
            return
        # For data frames, direction bits decide which side is the
        # client. The orchestrator only cares about the client MAC, so
        # flip sa / da based on to-ds / from-ds. Adhoc / WDS frames are
        # skipped — they don't map cleanly to a client-↔-AP association.
        mac = sa
        if frame_type == "data":
            to_ds = tods in ("1", "True", "true")
            from_ds = fromds in ("1", "True", "true")
            if to_ds and not from_ds:
                mac = sa            # client → AP
            elif from_ds and not to_ds:
                mac = da            # AP → client; the destination is the client
            else:
                return              # adhoc / WDS — skip
        if len(mac) != 17:
            return
        # Scanner-level dedup for data frames: dispatch a given
        # (client, bssid) pair at most once per window so the
        # orchestrator's per-frame cost (db reads, alert evaluate)
        # stays bounded. Management frames are rare enough that the
        # dedup is intentionally limited to data.
        if frame_type == "data" and bssid:
            if not self._allow_data_dispatch(mac, bssid):
                return
        await self._dispatch(_build_frame(
            mac=mac, rssi=rssi, ssid=ssid, channel=channel,
            frame_type=frame_type, bssid=bssid,
        ))

    def _allow_data_dispatch(self, mac: str, bssid: str) -> bool:
        """Return True if this (client, bssid) pair hasn't been
        dispatched within _DATA_DEDUP_WINDOW_S. Also prunes any
        stale entries so the dict can't grow without bound."""
        now = time.time()
        key = (mac, bssid)
        cutoff = now - _DATA_DEDUP_WINDOW_S
        # Cheap prune — only run on every Nth call to avoid scanning
        # the whole dict on every data frame.
        if len(self._recent_data_dispatch) > 256:
            stale = [k for k, t in self._recent_data_dispatch.items() if t < cutoff]
            for k in stale:
                self._recent_data_dispatch.pop(k, None)
        prev = self._recent_data_dispatch.get(key)
        if prev is not None and prev >= cutoff:
            return False
        self._recent_data_dispatch[key] = now
        return True

    # ── scapy backend ──────────────────────────────────────────────
    async def _run_scapy(self) -> None:
        try:
            from scapy.all import AsyncSniffer
            from scapy.layers.dot11 import (
                Dot11, Dot11Elt,
                Dot11ProbeReq,
                Dot11AssoReq, Dot11AssoResp,
                Dot11ReassoReq, Dot11ReassoResp,
                Dot11Auth, Dot11Deauth, Dot11Disas,
            )
        except Exception as e:
            self._last_error = f"scapy import failed: {e}"
            log.error("probe scanner [scapy]: %s", self._last_error)
            return

        loop = asyncio.get_running_loop()

        def packet_handler(pkt) -> None:
            try:
                # Pick the most specific management layer first, then
                # fall back to a generic data-frame check.
                if pkt.haslayer(Dot11ProbeReq):
                    frame_type = "probe"
                elif pkt.haslayer(Dot11AssoReq):
                    frame_type = "assoc"
                elif pkt.haslayer(Dot11AssoResp):
                    frame_type = "assoc-resp"
                elif pkt.haslayer(Dot11ReassoReq):
                    frame_type = "reassoc"
                elif pkt.haslayer(Dot11ReassoResp):
                    frame_type = "reassoc-resp"
                elif pkt.haslayer(Dot11Auth):
                    frame_type = "auth"
                elif pkt.haslayer(Dot11Deauth):
                    frame_type = "deauth"
                elif pkt.haslayer(Dot11Disas):
                    frame_type = "disassoc"
                elif pkt.haslayer(Dot11) and getattr(pkt[Dot11], "type", None) == 2:
                    frame_type = "data"
                else:
                    return
                d11 = pkt[Dot11]
                fc = getattr(d11, "FCfield", 0) or 0
                to_ds = bool(fc & 0x1)
                from_ds = bool(fc & 0x2)
                # Direction-aware addressing — mirrors Kismet's
                # distrib_to / distrib_from / distrib_inter logic.
                # For data frames we need addr1 (RA) when the AP is
                # transmitting *to* the client.
                if frame_type == "data":
                    if to_ds and not from_ds:
                        mac = (pkt.addr2 or "").lower()        # client → AP
                        bssid = (pkt.addr1 or "").lower()
                    elif from_ds and not to_ds:
                        mac = (pkt.addr1 or "").lower()        # AP → client
                        bssid = (pkt.addr2 or "").lower()
                    else:
                        return    # adhoc / WDS — skip
                else:
                    mac = (pkt.addr2 or "").lower()
                    # addr1 is the receiver; for management it's
                    # typically the AP (or broadcast for probes).
                    bssid = (pkt.addr1 or "").lower() if frame_type != "probe" else ""
                if len(mac) != 17:
                    return
                ssid = ""
                # Walk the Information Elements; ID 0 is the SSID.
                # Data frames don't carry IEs so we just skip the walk
                # for them.
                if frame_type != "data":
                    elt = pkt.getlayer(Dot11Elt)
                    while elt is not None:
                        if getattr(elt, "ID", None) == 0:
                            try:
                                ssid = (elt.info or b"").decode("utf-8", errors="replace")
                            except Exception:
                                ssid = ""
                            break
                        elt = elt.payload.getlayer(Dot11Elt) if hasattr(elt, "payload") else None
                rssi = getattr(pkt, "dBm_AntSignal", None)
                if rssi is not None:
                    try:
                        rssi = int(rssi)
                    except (TypeError, ValueError):
                        rssi = None
                channel = None
                ch_freq = getattr(pkt, "ChannelFrequency", None) or getattr(pkt, "Channel", None)
                if ch_freq:
                    try:
                        channel = _freq_to_channel(int(ch_freq))
                    except (TypeError, ValueError):
                        channel = None
                if frame_type == "data" and bssid and not self._allow_data_dispatch(mac, bssid):
                    return
                frame = _build_frame(
                    mac=mac, rssi=rssi, ssid=ssid, channel=channel,
                    frame_type=frame_type, bssid=bssid,
                )
                # AsyncSniffer's prn runs in its own thread — bridge to the asyncio loop.
                asyncio.run_coroutine_threadsafe(self._dispatch(frame), loop)
            except Exception as e:
                log.warning("scapy packet parse failed: %s", e)

        log.info("probe scanner [scapy]: starting AsyncSniffer on %s", self._iface)
        sniffer = AsyncSniffer(
            iface=self._iface,
            prn=packet_handler,
            store=False,
            monitor=True,
            # Same BPF as the tshark path — every client-side mgmt
            # subtype plus all data frames.
            filter=(
                "(type mgt and ("
                "subtype probereq or "
                "subtype assocreq or subtype assocresp or "
                "subtype reassocreq or subtype reassocresp or "
                "subtype auth or subtype deauth or subtype disassoc"
                ")) or type data"
            ),
        )
        try:
            sniffer.start()
        except Exception as e:
            self._last_error = f"scapy sniffer failed to start: {e}"
            log.error("probe scanner [scapy]: %s", self._last_error)
            return

        self._running = True
        self._last_error = None
        try:
            await self._stop.wait()
        finally:
            try:
                sniffer.stop()
            except Exception as e:
                log.warning("scapy stop failed: %s", e)

    # ── shared dispatch ────────────────────────────────────────────
    async def _dispatch(self, probe: dict) -> None:
        if self._on_probe is None:
            return
        self._last_probe_at = time.time()
        self._probe_count += 1
        try:
            await self._on_probe(probe)
        except Exception as e:
            log.exception("probe callback failed: %s", e)


def _build_frame(
    *, mac: str, rssi: Optional[int], ssid: str, channel: Optional[int],
    frame_type: str, bssid: str,
) -> dict:
    """Build the dispatch dict for any wifi-client frame the scanner
    cares about. frame_type is one of probe / assoc / reassoc;
    callers branch on it. bssid is meaningful only for assoc/reassoc;
    probes broadcast to ff:ff:ff:ff:ff:ff which we treat as None.
    Any all-NUL or '\\x00'-escape-only SSID — the way some APs and
    clients encode 'no SSID' instead of a zero-length IE — gets
    normalized to "" so the downstream pipeline treats it as a
    wildcard / hidden probe."""
    if bssid in ("", "ff:ff:ff:ff:ff:ff"):
        bssid = ""
    return {
        "mac": mac,
        "rssi": rssi,
        "ssid": normalize_ssid(ssid),
        "channel": channel,
        "frame_type": frame_type,
        "bssid": bssid,
        "randomized": _is_randomized_mac(mac),
    }


def _frame_type_from_subtype(subtype_s: str) -> Optional[str]:
    """Map tshark's wlan.fc.type_subtype hex string to a frame_type
    label. Covers the management subtypes the BPF filter admits, plus
    'data' for every type-2 subtype (with or without QoS / null
    variants). Unknown subtypes return None and the caller drops them."""
    s = (subtype_s or "").strip().lower()
    if not s:
        return None
    try:
        v = int(s, 16) if s.startswith("0x") else int(s)
    except ValueError:
        return None
    # Management subtypes (type bits = 0 → high nibble of the byte is 0).
    if v == 0x00:
        return "assoc"          # association request
    if v == 0x01:
        return "assoc-resp"     # association response
    if v == 0x02:
        return "reassoc"        # reassociation request
    if v == 0x03:
        return "reassoc-resp"   # reassociation response
    if v == 0x04:
        return "probe"          # probe request
    if v == 0x0a:
        return "disassoc"       # disassociation
    if v == 0x0b:
        return "auth"           # authentication
    if v == 0x0c:
        return "deauth"         # deauthentication
    # Data frames span 0x20..0x2f (subtypes 0..15 of type=2). The
    # high nibble (>>4) is the type field — we only care that it's 2
    # (data), since direction + addr3 give us the (client, BSSID) pair
    # regardless of subtype.
    if (v >> 4) == 0x2:
        return "data"
    return None


# Backwards-compatible alias for any external caller (none in-repo)
# that still expects the old probe-only builder shape.
def _build_probe(mac: str, rssi: Optional[int], ssid: str, channel: Optional[int]) -> dict:
    return _build_frame(
        mac=mac, rssi=rssi, ssid=ssid, channel=channel,
        frame_type="probe", bssid="",
    )


def _is_randomized_mac(mac: str) -> bool:
    """Locally-administered bit set on the first byte indicates a
    privacy-randomized MAC (per IEEE 802 — bit 1 of the first octet)."""
    try:
        first = int(mac.split(":")[0], 16)
    except (ValueError, IndexError):
        return False
    return bool(first & 0b00000010)


async def _run_cmd(cmd: list[str]) -> tuple[int, str, str]:
    """Run a subprocess, capture stdout+stderr. Returns (rc, out, err)."""
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    out_b, err_b = await proc.communicate()
    return (
        proc.returncode or 0,
        out_b.decode("utf-8", errors="replace"),
        err_b.decode("utf-8", errors="replace"),
    )


async def _get_interface_type(iface: str) -> Optional[str]:
    """Read the current type ('managed', 'monitor', etc.) from `iw dev info`."""
    rc, out, _ = await _run_cmd(["iw", "dev", iface, "info"])
    if rc != 0:
        return None
    for line in out.splitlines():
        s = line.strip()
        if s.startswith("type "):
            return s.split(None, 1)[1].strip()
    return None


async def _setup_monitor_mode(iface: str) -> tuple[Optional[str], str]:
    """Put `iface` into monitor mode and bring it up. Returns
    (prior_type, error). On success error is "". prior_type may be None
    if we couldn't introspect — in that case we won't try to restore."""
    prior = await _get_interface_type(iface)
    if prior == "monitor":
        # Already there — leave the prior type as None so we won't undo
        # a setup the user did manually before enabling auto-mode.
        return None, ""
    for cmd in (
        ["ip", "link", "set", iface, "down"],
        ["iw", "dev", iface, "set", "type", "monitor"],
        ["ip", "link", "set", iface, "up"],
    ):
        rc, out, err = await _run_cmd(cmd)
        if rc != 0:
            msg = (err.strip() or out.strip() or f"rc={rc}")
            return prior, f"{' '.join(cmd)}: {msg}"
    return prior, ""


async def _restore_interface(iface: str, target_type: str) -> None:
    """Best-effort restore of `iface` to `target_type` (typically 'managed').
    Failures are logged but don't raise — we don't want stop() to stall."""
    for cmd in (
        ["ip", "link", "set", iface, "down"],
        ["iw", "dev", iface, "set", "type", target_type],
        ["ip", "link", "set", iface, "up"],
    ):
        rc, _, err = await _run_cmd(cmd)
        if rc != 0:
            log.warning("interface restore step failed: %s: %s",
                        " ".join(cmd), err.strip())


def parse_channels(spec: str) -> list[int]:
    """Parse a comma-separated channel list into [int]. Drops invalid entries."""
    out: list[int] = []
    for piece in (spec or "").split(","):
        p = piece.strip()
        if not p:
            continue
        try:
            n = int(p)
        except ValueError:
            continue
        if 1 <= n <= 196:  # covers 2.4 GHz, 5 GHz, and 6 GHz channel numbers
            out.append(n)
    return out


def _freq_to_channel(freq_mhz: int) -> Optional[int]:
    """Convert a frequency in MHz to its 802.11 channel number.
    Covers 2.4 GHz (1-14) and 5 GHz (32-177)."""
    if freq_mhz == 2484:
        return 14
    if 2412 <= freq_mhz <= 2472:
        return (freq_mhz - 2407) // 5
    if 5160 <= freq_mhz <= 5885:
        return (freq_mhz - 5000) // 5
    return None


probe_scanner = ProbeScanner()
