from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import sys
from datetime import datetime
from typing import Iterable

from models import WifiDevice
from services.oui import oui_service

log = logging.getLogger(__name__)


def _channel_from_freq(freq_mhz: int) -> int | None:
    if 2412 <= freq_mhz <= 2484:
        if freq_mhz == 2484:
            return 14
        return (freq_mhz - 2407) // 5
    if 5160 <= freq_mhz <= 5885:
        return (freq_mhz - 5000) // 5
    if 5955 <= freq_mhz <= 7115:
        return (freq_mhz - 5950) // 5
    return None


def _band_from_freq(freq_mhz: int) -> str:
    if freq_mhz < 3000:
        return "2.4GHz"
    if freq_mhz < 5950:
        return "5GHz"
    return "6GHz"


async def list_wifi_interfaces() -> list[str]:
    """Return only the interface names (compat shim)."""
    return [info["name"] for info in await list_wifi_interface_info()]


async def list_interface_channels(iface: str) -> list[dict]:
    """Return the channels supported by `iface`, parsed out of `iw phy info`.
    Each entry: {channel, freq_mhz, band, disabled, no_ir}. Empty list when
    iw isn't installed or the interface isn't found."""
    if not iface or not shutil.which("iw"):
        return []
    # Find the wiphy index for this interface (line: "wiphy N").
    try:
        proc = await asyncio.create_subprocess_exec(
            "iw", "dev", iface, "info",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        out, _ = await proc.communicate()
    except Exception as e:
        log.warning("iw dev %s info failed: %s", iface, e)
        return []
    m = re.search(r"^\s*wiphy\s+(\d+)", out.decode(errors="ignore"), re.MULTILINE)
    if not m:
        return []
    phy = f"phy{m.group(1)}"
    try:
        proc = await asyncio.create_subprocess_exec(
            "iw", "phy", phy, "info",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        out, _ = await proc.communicate()
    except Exception as e:
        log.warning("iw phy %s info failed: %s", phy, e)
        return []
    return _parse_iw_phy_channels(out.decode(errors="ignore"))


def _parse_iw_phy_channels(text: str) -> list[dict]:
    """Pull every channel entry out of an `iw phy info` blob. Lines look like
    `* 2412 MHz [1] (20.0 dBm)` or `* 5320 MHz [64] (disabled)`. Same channel
    listed under multiple bands is deduped."""
    channels: list[dict] = []
    seen: set[int] = set()
    for raw in text.splitlines():
        m = re.match(r"\s*\*\s+(\d+)\s+MHz\s+\[(\d+)\](.*)", raw)
        if not m:
            continue
        freq = int(m.group(1))
        ch = int(m.group(2))
        if ch in seen:
            continue
        seen.add(ch)
        rest = m.group(3) or ""
        channels.append({
            "channel": ch,
            "freq_mhz": freq,
            "band": _band_from_freq(freq),
            "disabled": "(disabled)" in rest,
            "no_ir": "(no IR)" in rest,
        })
    channels.sort(key=lambda c: c["freq_mhz"])
    return channels


async def pick_wifi_interface() -> str | None:
    """Auto-pick a wireless interface suitable for scanning. Prefers
    interfaces not currently associated with an AP, since associated
    interfaces typically can't scan without disrupting the connection.
    Returns None when no candidate is available."""
    infos = await list_wifi_interface_info()
    if not infos:
        return None
    unassociated = [i for i in infos if not i.get("ssid")]
    chosen = unassociated[0] if unassociated else infos[0]
    return chosen.get("name")


async def list_wifi_interface_info() -> list[dict]:
    """Return rich info per wireless interface from `iw dev`.

    Each entry: name, type, mac, ssid (None if not associated),
    channel, frequency_mhz, band, width, txpower_dbm.
    """
    if not shutil.which("iw"):
        return []
    try:
        proc = await asyncio.create_subprocess_exec(
            "iw", "dev", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        out, _ = await proc.communicate()
    except Exception as e:
        log.warning("iw dev failed: %s", e)
        return []
    return _parse_iw_dev(out.decode(errors="ignore"))


def _parse_iw_dev(text: str) -> list[dict]:
    interfaces: list[dict] = []
    cur: dict | None = None
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("Interface "):
            if cur:
                interfaces.append(cur)
            cur = {"name": line.split(None, 1)[1]}
        elif cur is None:
            continue
        elif line.startswith("addr "):
            cur["mac"] = line.split(None, 1)[1].lower()
        elif line.startswith("ssid "):
            cur["ssid"] = line.split(None, 1)[1]
        elif line.startswith("type "):
            cur["type"] = line.split(None, 1)[1]
        elif line.startswith("channel "):
            m = re.match(r"channel\s+(\d+)\s*\((\d+)\s*MHz\)", line)
            if m:
                cur["channel"] = int(m.group(1))
                cur["frequency_mhz"] = int(m.group(2))
                cur["band"] = _band_from_freq(int(m.group(2)))
            mw = re.search(r"width:\s*(\S+\s*\S*)", line)
            if mw:
                cur["width"] = mw.group(1).rstrip(",")
        elif line.startswith("txpower "):
            m = re.match(r"txpower\s+([\d.]+)", line)
            if m:
                cur["txpower_dbm"] = float(m.group(1))
    if cur:
        interfaces.append(cur)
    return interfaces


_warned_iw_perm = False
_warned_no_backend = False


async def scan_wifi(interface: str) -> list[WifiDevice]:
    """Scan for nearby APs.

    Tries `iw dev <iface> scan` first (full detail). If that fails with a
    permission error, falls back to `nmcli` — NetworkManager already has
    the privileges to scan and exposes results unprivileged.
    """
    global _warned_iw_perm, _warned_no_backend
    if not interface:
        return []

    devices: list[WifiDevice] = []
    iw_err: str | None = None
    if shutil.which("iw"):
        devices, iw_err = await _scan_with_iw(interface)
        if devices:
            return await _enrich_with_vendor(devices)

    if iw_err and "not permitted" in iw_err.lower():
        if not _warned_iw_perm:
            py = os.path.realpath(sys.executable)
            log.warning(
                "iw scan denied (CAP_NET_ADMIN required). "
                "Either run as root, or grant the cap to the running interpreter: "
                "sudo setcap cap_net_admin,cap_net_raw+eip %s. "
                "Falling back to nmcli where available.",
                py,
            )
            _warned_iw_perm = True
    elif iw_err:
        log.debug("iw scan: %s", iw_err)

    if shutil.which("nmcli"):
        devices = await _scan_with_nmcli(interface)
        if devices:
            return await _enrich_with_vendor(devices)

    if not _warned_no_backend:
        log.warning(
            "No usable wifi backend (iw blocked or missing, nmcli not installed). "
            "Install network-manager or fix `iw` privileges to enable scanning."
        )
        _warned_no_backend = True
    return []


async def _scan_with_iw(interface: str) -> tuple[list[WifiDevice], str | None]:
    try:
        proc = await asyncio.create_subprocess_exec(
            "iw", "dev", interface, "scan",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        out, err = await proc.communicate()
    except Exception as e:
        return [], str(e)
    err_text = err.decode(errors="ignore").strip()
    if proc.returncode != 0 and not out:
        return [], err_text or f"iw exited {proc.returncode}"
    return list(_parse_iw_scan(out.decode(errors="ignore"))), err_text or None


async def _scan_with_nmcli(interface: str) -> list[WifiDevice]:
    """Fallback wifi scan via NetworkManager. Lower detail than iw but unprivileged."""
    fields = "BSSID,SSID,CHAN,FREQ,SIGNAL,SECURITY"
    try:
        proc = await asyncio.create_subprocess_exec(
            "nmcli", "-t", "-f", fields, "device", "wifi", "list",
            "ifname", interface, "--rescan", "auto",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        out, err = await proc.communicate()
    except Exception as e:
        log.warning("nmcli scan failed: %s", e)
        return []
    if proc.returncode != 0:
        log.warning("nmcli returned %s: %s", proc.returncode, err.decode(errors="ignore").strip())
        return []

    now = datetime.utcnow()
    devices: list[WifiDevice] = []
    for line in out.decode(errors="ignore").splitlines():
        if not line.strip():
            continue
        parts = _split_nmcli_terse(line)
        if len(parts) < 6:
            continue
        bssid_raw, ssid, chan, freq, signal, sec = parts[:6]
        bssid = bssid_raw.lower()
        if not re.match(r"^[0-9a-f:]{17}$", bssid):
            continue
        try:
            sig_pct = int(signal)
        except ValueError:
            sig_pct = 0
        # NetworkManager reports SIGNAL as a 0-100 percentage; map to dBm
        # via the same approximation NM uses internally (-100 dBm @ 0%, -50 dBm @ 100%).
        rssi = -100 + sig_pct // 2
        try:
            freq_mhz: int | None = int(freq)
        except ValueError:
            freq_mhz = None
        try:
            channel: int | None = int(chan)
        except ValueError:
            channel = None
        sec_norm = (sec or "").upper().strip() or "OPEN"
        if "WPA3" in sec_norm or "SAE" in sec_norm:
            enc = "WPA3"
        elif "WPA2" in sec_norm:
            enc = "WPA2"
        elif "WPA" in sec_norm:
            enc = "WPA"
        elif "WEP" in sec_norm:
            enc = "WEP"
        else:
            enc = "OPEN"
        devices.append(WifiDevice(
            bssid=bssid,
            ssid=ssid or "<hidden>",
            rssi=rssi,
            frequency_mhz=freq_mhz,
            channel=channel,
            band=_band_from_freq(freq_mhz) if freq_mhz else None,
            encryption=enc,
            cipher=None,
            auth=sec or None,
            vendor_oui=bssid[:8].upper().replace(":", "-"),
            capabilities=None,
            beacon_interval_ms=None,
            last_seen=now,
        ))
    return devices


def _split_nmcli_terse(line: str) -> list[str]:
    """Split nmcli -t output, treating ``\\:`` as a literal colon inside fields."""
    parts: list[str] = []
    cur: list[str] = []
    i = 0
    while i < len(line):
        c = line[i]
        if c == "\\" and i + 1 < len(line):
            cur.append(line[i + 1])
            i += 2
        elif c == ":":
            parts.append("".join(cur))
            cur = []
            i += 1
        else:
            cur.append(c)
            i += 1
    parts.append("".join(cur))
    return parts


async def _enrich_with_vendor(devices: list[WifiDevice]) -> list[WifiDevice]:
    for d in devices:
        d.vendor = await oui_service.lookup(d.bssid)
    return devices


def _parse_iw_scan(text: str) -> Iterable[WifiDevice]:
    blocks = re.split(r"^BSS\s+", text, flags=re.MULTILINE)[1:]
    now = datetime.utcnow()
    for blk in blocks:
        bssid = blk.split("(", 1)[0].strip().split()[0].lower()
        if not re.match(r"^[0-9a-f:]{17}$", bssid):
            continue

        def first(pat: str, flags: int = 0) -> str | None:
            m = re.search(pat, blk, flags)
            return m.group(1).strip() if m else None

        ssid = first(r"^\s*SSID:\s*(.*)$", re.MULTILINE)
        if ssid == "":
            ssid = "<hidden>"
        rssi_s = first(r"signal:\s*(-?\d+\.?\d*)\s*dBm")
        rssi = int(float(rssi_s)) if rssi_s else -100
        freq_s = first(r"freq:\s*(\d+)")
        freq = int(freq_s) if freq_s else None
        chan = _channel_from_freq(freq) if freq else None
        band = _band_from_freq(freq) if freq else None

        capa = first(r"capability:\s*(.*)$", re.MULTILINE)
        beacon = first(r"beacon interval:\s*(\d+)")

        # encryption
        enc, cipher, auth = "OPEN", None, None
        if re.search(r"WPA3", blk, re.IGNORECASE) or re.search(r"SAE", blk):
            enc = "WPA3"
        elif re.search(r"RSN:", blk):
            enc = "WPA2"
        elif re.search(r"WPA:", blk):
            enc = "WPA"
        elif re.search(r"Privacy", blk):
            enc = "WEP"
        cipher = first(r"Pairwise ciphers:\s*(.*)$", re.MULTILINE)
        auth = first(r"Authentication suites:\s*(.*)$", re.MULTILINE)

        yield WifiDevice(
            bssid=bssid,
            ssid=ssid,
            rssi=rssi,
            frequency_mhz=freq,
            channel=chan,
            band=band,
            encryption=enc,
            cipher=cipher,
            auth=auth,
            vendor_oui=bssid[:8].upper().replace(":", "-") if bssid else None,
            capabilities=capa,
            beacon_interval_ms=int(beacon) if beacon else None,
            last_seen=now,
        )
