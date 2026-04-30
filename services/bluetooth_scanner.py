from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Optional

from models import BluetoothDevice
from services.oui import oui_service

log = logging.getLogger(__name__)


async def list_bluetooth_adapters() -> list[str]:
    """List BT adapters. On Linux uses BlueZ via DBus; falls back to a single 'default' entry."""
    try:
        from bleak.backends.bluezdbus.manager import get_global_bluez_manager
        mgr = await get_global_bluez_manager()
        adapters = list(mgr._adapters.keys())  # type: ignore[attr-defined]
        return [a.split("/")[-1] for a in adapters] or ["default"]
    except Exception:
        return ["default"]


async def scan_bluetooth(adapter: Optional[str], duration_s: float = 8.0) -> list[BluetoothDevice]:
    """Active BLE scan. Captures device name, RSSI, manufacturer data, services, etc."""
    try:
        from bleak import BleakScanner
    except ImportError:
        log.warning("bleak not installed; cannot scan bluetooth")
        return []

    found: dict[str, BluetoothDevice] = {}
    now_factory = datetime.utcnow

    def cb(device, adv):
        try:
            mfg = {k: v.hex() for k, v in (adv.manufacturer_data or {}).items()}
            svc_data = {k: v.hex() for k, v in (adv.service_data or {}).items()}
            uuids = list(adv.service_uuids or [])
            existing = found.get(device.address)
            seen_count = (existing.seen_count + 1) if existing else 1
            found[device.address] = BluetoothDevice(
                address=device.address,
                name=adv.local_name or device.name,
                rssi=adv.rssi if adv.rssi is not None else -100,
                tx_power=adv.tx_power,
                address_type=getattr(device, "address_type", None),
                appearance=getattr(adv, "appearance", None),
                manufacturer_data=mfg,
                service_uuids=uuids,
                service_data=svc_data,
                is_connectable=getattr(adv, "connectable", None),
                last_seen=now_factory(),
                seen_count=seen_count,
            )
        except Exception as e:
            log.debug("bt callback error: %s", e)

    kwargs = {}
    if adapter and adapter != "default":
        kwargs["adapter"] = adapter

    try:
        scanner = BleakScanner(detection_callback=cb, **kwargs)
        await scanner.start()
        try:
            await asyncio.sleep(duration_s)
        finally:
            await scanner.stop()
    except Exception as e:
        log.warning("bluetooth scan failed: %s", e)
        return []

    devices = list(found.values())
    for d in devices:
        d.vendor = await oui_service.lookup(d.address)
    return devices
