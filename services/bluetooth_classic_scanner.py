"""Bluetooth Classic (BR/EDR) inquiry via BlueZ DBus.

Classic discovery is different from BLE — devices only respond to an
inquiry when they're in discoverable / pairing mode, so the population is
sparse (paired headphones, keyboards, etc. stay silent). Running a Classic
scan briefly switches the controller out of LE mode, which can stall the
BLE scanner; the orchestrator schedules them on different intervals to
limit overlap.

Linux/BlueZ only — on platforms without BlueZ this module's scan returns
an empty list, the same way the BLE scanner does when bleak is absent.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Optional

from models import BluetoothClassicDevice
from services.oui import oui_service

log = logging.getLogger(__name__)


# Bluetooth Class of Device major-class table (CoD bits 8–12). Decoded
# into a human-readable label that surfaces on the Devices tab.
# Spec: https://www.bluetooth.com/specifications/assigned-numbers/baseband/
_COD_MAJOR_CLASS = {
    0x00: "Miscellaneous",
    0x01: "Computer",
    0x02: "Phone",
    0x03: "LAN/Network access point",
    0x04: "Audio/Video",
    0x05: "Peripheral",        # keyboard / mouse / joystick
    0x06: "Imaging",            # printer / scanner / camera
    0x07: "Wearable",
    0x08: "Toy",
    0x09: "Health",
}


def decode_cod(cod: Optional[int]) -> Optional[str]:
    """Resolve the CoD integer to its major-class label. None when no CoD
    was reported (a device that returns inquiry but no EIR record)."""
    if cod is None:
        return None
    try:
        major = (int(cod) >> 8) & 0x1F
    except (TypeError, ValueError):
        return None
    return _COD_MAJOR_CLASS.get(major, f"Unknown (major=0x{major:02X})")


async def scan_bluetooth_classic(
    adapter: Optional[str], duration_s: float = 10.0,
) -> list[BluetoothClassicDevice]:
    """Active BR/EDR inquiry on the given BlueZ adapter. Runs Discovery
    with a Transport=bredr filter for `duration_s` seconds, then reads
    every BR/EDR-flagged device the manager knows about and returns them
    as BluetoothClassicDevice rows. Devices already paired but currently
    discoverable show up too.

    On a system without BlueZ (no bleak/dbus, non-Linux) this returns an
    empty list and logs a debug message — same shape as the BLE scanner's
    missing-bleak fallback."""
    try:
        from bleak.backends.bluezdbus.manager import get_global_bluez_manager
    except ImportError:
        log.debug("bleak/BlueZ unavailable; cannot scan bluetooth classic")
        return []

    try:
        from dbus_fast import Variant  # type: ignore
    except ImportError:
        try:
            from dbus_next import Variant  # type: ignore
        except ImportError:
            log.debug("dbus_fast/dbus_next unavailable; cannot scan classic")
            return []

    try:
        mgr = await get_global_bluez_manager()
    except Exception as e:
        log.debug("BlueZ manager unavailable: %s", e)
        return []

    adapter_path = _resolve_adapter_path(mgr, adapter)
    if not adapter_path:
        log.warning("No BlueZ adapter found for classic scan (asked for %r)", adapter)
        return []

    bus = getattr(mgr, "_bus", None)
    if bus is None:
        log.debug("BlueZ manager has no underlying bus; cannot scan classic")
        return []

    try:
        adapter_introspection = await bus.introspect("org.bluez", adapter_path)
        proxy = bus.get_proxy_object("org.bluez", adapter_path, adapter_introspection)
        iface = proxy.get_interface("org.bluez.Adapter1")
    except Exception as e:
        log.warning("classic scan: failed to bind Adapter1 on %s: %s", adapter_path, e)
        return []

    # SetDiscoveryFilter takes a{sv} — Transport="bredr" makes BlueZ run
    # only BR/EDR inquiry, leaving the LE radio free. We could ask for
    # "auto" (both) but that'd compete more aggressively with the BLE loop.
    filters = {"Transport": Variant("s", "bredr"), "DuplicateData": Variant("b", False)}
    try:
        await iface.call_set_discovery_filter(filters)
    except Exception as e:
        # Some BlueZ versions reject SetDiscoveryFilter when discovery is
        # already running on another transport. Log and continue with the
        # default filter — we'll still get BR/EDR devices, just also LE.
        log.debug("classic scan: SetDiscoveryFilter failed (%s) — continuing", e)

    try:
        await iface.call_start_discovery()
    except Exception as e:
        log.warning("classic scan: StartDiscovery failed: %s", e)
        return []

    try:
        await asyncio.sleep(max(1.0, duration_s))
    finally:
        try:
            await iface.call_stop_discovery()
        except Exception as e:
            log.debug("classic scan: StopDiscovery failed: %s", e)

    devices = await _harvest_devices(bus, mgr, adapter_path)

    now = datetime.now()
    out: list[BluetoothClassicDevice] = []
    for addr, props in devices.items():
        cod = props.get("Class")
        d = BluetoothClassicDevice(
            address=addr,
            name=props.get("Name") or props.get("Alias"),
            rssi=int(props.get("RSSI") or -100),
            device_class=int(cod) if cod is not None else None,
            device_class_label=decode_cod(cod),
            paired=props.get("Paired"),
            connected=props.get("Connected"),
            last_seen=now,
            seen_count=1,
        )
        out.append(d)

    for d in out:
        try:
            d.vendor = await oui_service.lookup(d.address)
        except Exception:
            pass
    return out


def _resolve_adapter_path(mgr, want: Optional[str]) -> Optional[str]:
    """Pick the BlueZ adapter object path matching `want` (e.g. 'hci0').
    Falls back to the first adapter if `want` is None / 'default'.

    bleak's `_adapters` cache shape varies by version — older builds
    use a `dict[path, props]`, newer ones a `set[path]`, and some
    intermediates a `list[path]`. Treat any iterable of path strings
    as the source of truth, with .keys() only when present."""
    adapters = getattr(mgr, "_adapters", None)
    if not adapters:
        return None
    if hasattr(adapters, "keys"):
        paths = list(adapters.keys())
    else:
        paths = list(adapters)
    if not paths:
        return None
    if not want or want == "default":
        return paths[0]
    for path in paths:
        if not isinstance(path, str):
            continue
        name = path.rstrip("/").split("/")[-1]
        if name == want:
            return path
    return None


def _unwrap_variant(v):
    """Unbox a dbus_fast / dbus_next Variant into its native Python
    value. Pass-through for anything that's already a primitive (some
    bleak versions hand back unwrapped values)."""
    return getattr(v, "value", v)


async def _harvest_devices(bus, mgr, adapter_path: str) -> dict[str, dict]:
    """Pull every BR/EDR-flagged device under this adapter via BlueZ's
    ObjectManager. Doing one GetManagedObjects round-trip is cheaper
    than poking around bleak's internal caches and immune to the
    dict/set/list shape variance across bleak versions. Filters by
    adapter prefix and drops AddressType='random' rows that leaked in
    if the SetDiscoveryFilter call was rejected by an older BlueZ."""
    try:
        intro = await bus.introspect("org.bluez", "/")
        proxy = bus.get_proxy_object("org.bluez", "/", intro)
        om = proxy.get_interface("org.freedesktop.DBus.ObjectManager")
        managed = await om.call_get_managed_objects()
    except Exception as e:
        log.debug("classic scan: GetManagedObjects failed (%s)", e)
        # Fall back to whatever bleak has cached locally — better than
        # nothing on systems where the ObjectManager call breaks.
        return _harvest_devices_from_mgr(mgr, adapter_path)

    out: dict[str, dict] = {}
    for path, ifaces in managed.items():
        if not isinstance(path, str) or not path.startswith(adapter_path + "/"):
            continue
        dev = ifaces.get("org.bluez.Device1") if isinstance(ifaces, dict) else None
        if not dev:
            continue
        get = lambda k: _unwrap_variant(dev.get(k))  # noqa: E731
        addr = get("Address")
        if not addr:
            continue
        addr_type = (get("AddressType") or "").lower()
        if addr_type and addr_type != "public":
            continue
        out[addr.lower()] = {
            "Address": addr,
            "Name": get("Name"),
            "Alias": get("Alias"),
            "RSSI": get("RSSI"),
            "Class": get("Class"),
            "Paired": get("Paired"),
            "Connected": get("Connected"),
        }
    return out


def _harvest_devices_from_mgr(mgr, adapter_path: str) -> dict[str, dict]:
    """Legacy harvest path. Reads bleak's internal device cache when the
    ObjectManager call isn't available. Tolerates dict/set/list shapes
    for the cache; sets/lists yield path strings only (no properties)
    so the resulting rows are mostly empty — better than crashing."""
    devices_attr = (
        getattr(mgr, "_devices", None) or getattr(mgr, "devices", None) or {}
    )
    out: dict[str, dict] = {}
    if hasattr(devices_attr, "items"):
        iterator = devices_attr.items()
    else:
        iterator = ((p, None) for p in devices_attr)
    for path, props in iterator:
        if not isinstance(path, str) or not path.startswith(adapter_path + "/"):
            continue
        def _get(key: str, attr: Optional[str] = None, _p=props):
            if isinstance(_p, dict):
                return _p.get(key)
            if _p is None:
                return None
            return getattr(_p, attr or key.lower(), None)

        addr = _get("Address", "address")
        if not addr:
            continue
        addr_type = (_get("AddressType", "address_type") or "").lower()
        if addr_type and addr_type != "public":
            continue
        out[addr.lower()] = {
            "Address": addr,
            "Name": _get("Name", "name"),
            "Alias": _get("Alias", "alias"),
            "RSSI": _get("RSSI", "rssi"),
            "Class": _get("Class"),
            "Paired": _get("Paired", "paired"),
            "Connected": _get("Connected", "connected"),
        }
    return out
