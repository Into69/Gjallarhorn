"""Sensor self-identification.

Resolves the MAC addresses of the host's own scanner adapters at boot
and whenever sensor settings change. The set is consulted in three
places:

  - Alert evaluation — a sensor MAC seen in its own scan should not
    fire alerts on itself, so it gets the same suppression treatment
    as a user-whitelisted device.
  - Report generation — same treatment, so PDF / Discord rollups
    don't include the operator's own gear.
  - Devices / WiFi-AP UI — surfaces a "Sensor" badge so the operator
    can tell at a glance that a row is their own adapter rather than
    a nearby device.

Linux-only paths via sysfs. On platforms without those files the
registry stays empty and every lookup returns False, which is the
safe default (treat unknown things as foreign devices)."""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)


# Strict MAC validator. We reject placeholder / sentinel values that
# would otherwise badge every nearby device:
#   00:00:00:00:00:00  — unconfigured / disabled adapter sysfs default
#   FF:FF:FF:FF:FF:FF  — L2 broadcast
# A 'sensor identity' that matched either would silently flag every
# row whose MAC happened to equal the sentinel — which is exactly
# the bug the user hit ("lots of devices have the sensor badge").
import re as _re
_MAC_RE = _re.compile(r"^[0-9A-F]{2}(?::[0-9A-F]{2}){5}$")
_MAC_BLACKLIST = {"00:00:00:00:00:00", "FF:FF:FF:FF:FF:FF"}


def _valid_sensor_mac(mac: Optional[str]) -> bool:
    """True for full-format MACs that aren't reserved sentinels."""
    if not mac or len(mac) != 17:
        return False
    if not _MAC_RE.match(mac):
        return False
    return mac not in _MAC_BLACKLIST


def _read_iface_mac(iface: Optional[str]) -> Optional[str]:
    """Read the hardware MAC of a Linux network interface from sysfs.
    Returns uppercase XX:XX:... or None. Tolerates the special
    'default' / 'auto' placeholders, missing sysfs entries, and the
    monitor-mode suffix some tools append (wlan1 → wlan1mon). Rejects
    placeholder MACs (all-zero, broadcast) so an unconfigured /
    disabled adapter doesn't get registered as the sensor's own."""
    if not iface or iface in ("default", "auto"):
        return None
    candidates = [iface]
    if iface.endswith("mon") and len(iface) > 3:
        candidates.append(iface[:-3])
    for name in candidates:
        try:
            mac = Path(f"/sys/class/net/{name}/address").read_text().strip().upper()
        except OSError:
            continue
        if _valid_sensor_mac(mac):
            return mac
    return None


def _read_bluetooth_mac(adapter: Optional[str]) -> Optional[str]:
    """Read the BR/EDR address of a BlueZ adapter from sysfs. Falls
    back to hci0 when no specific adapter is configured — matches
    bluetooth_classic_scanner's _resolve_adapter_path default.
    Rejects placeholder MACs for the same reason _read_iface_mac
    does."""
    name = adapter if adapter and adapter not in ("default", "auto") else "hci0"
    try:
        mac = Path(f"/sys/class/bluetooth/{name}/address").read_text().strip().upper()
    except OSError:
        return None
    return mac if _valid_sensor_mac(mac) else None


class SensorIdentity:
    """Runtime registry of the host's own scanner MACs."""

    def __init__(self) -> None:
        # MAC (uppercase) -> {"label": str, "iface": str}
        self._entries: dict[str, dict] = {}

    def refresh(self, settings) -> None:
        """Re-resolve sensor MACs from current adapter settings. Logs
        the new set on change so the operator can confirm what's being
        treated as self in the journal."""
        new: dict[str, dict] = {}
        wifi_iface = getattr(settings, "wifi_interface", None)
        probe_iface = getattr(settings, "probe_interface", None)
        bt_adapter = getattr(settings, "bluetooth_adapter", None)

        wifi_mac = _read_iface_mac(wifi_iface)
        if wifi_mac:
            new[wifi_mac] = {"label": "WiFi scanner",
                             "iface": wifi_iface}
        probe_mac = _read_iface_mac(probe_iface)
        if probe_mac and probe_mac not in new:
            new[probe_mac] = {"label": "Probe scanner",
                              "iface": probe_iface}
        elif probe_mac and probe_mac in new:
            # Same physical adapter doing both — broaden the label so the
            # UI badge reflects it.
            new[probe_mac]["label"] = "WiFi + probe scanner"
        bt_mac = _read_bluetooth_mac(bt_adapter)
        if bt_mac:
            new[bt_mac] = {"label": "Bluetooth scanner",
                           "iface": bt_adapter or "hci0"}

        if new != self._entries:
            summary = ", ".join(f"{m} ({v['label']})" for m, v in sorted(new.items())) or "(none)"
            log.info("sensor identity: %s", summary)
            self._entries = new

    def is_sensor(self, device_id: Optional[str]) -> bool:
        """True when the MAC (any case) matches one of our scanner
        adapters. Empty / None input returns False."""
        if not device_id:
            return False
        return device_id.upper() in self._entries

    def label_for(self, device_id: Optional[str]) -> Optional[str]:
        """Friendly label for a sensor MAC, or None if not ours."""
        if not device_id:
            return None
        entry = self._entries.get(device_id.upper())
        return entry["label"] if entry else None

    def entries(self) -> list[dict]:
        """Snapshot for API exposure. List of {mac, label, iface},
        sorted by MAC for stable output."""
        return [
            {"mac": m, "label": v["label"], "iface": v.get("iface")}
            for m, v in sorted(self._entries.items())
        ]


sensor_identity = SensorIdentity()
