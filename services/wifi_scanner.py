from __future__ import annotations

import asyncio
import logging
import re
import shutil
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
    """Return wireless interface names visible to `iw dev`."""
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
    names = re.findall(r"^\s*Interface\s+(\S+)", out.decode(errors="ignore"), re.MULTILINE)
    return names


async def scan_wifi(interface: str) -> list[WifiDevice]:
    """Run `iw dev <iface> scan` and parse all observable details."""
    if not interface:
        return []
    if not shutil.which("iw"):
        log.warning("`iw` not installed; cannot scan wifi")
        return []
    try:
        proc = await asyncio.create_subprocess_exec(
            "iw", "dev", interface, "scan",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        out, err = await proc.communicate()
    except Exception as e:
        log.warning("iw scan failed: %s", e)
        return []
    if proc.returncode != 0:
        log.warning("iw scan returned %s: %s", proc.returncode, err.decode(errors="ignore").strip())
        if not out:
            return []
    devices = list(_parse_iw_scan(out.decode(errors="ignore")))
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
