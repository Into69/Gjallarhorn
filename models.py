from __future__ import annotations

from datetime import datetime
from typing import Optional, Literal

from pydantic import BaseModel, Field


class GPSFix(BaseModel):
    mode: int = 0  # 0=no fix, 1=no fix, 2=2D, 3=3D
    lat: Optional[float] = None
    lon: Optional[float] = None
    alt: Optional[float] = None
    speed: Optional[float] = None
    track: Optional[float] = None
    climb: Optional[float] = None
    error_h: Optional[float] = None  # horizontal error in meters
    error_v: Optional[float] = None
    sats_visible: Optional[int] = None
    sats_used: Optional[int] = None
    time: Optional[datetime] = None


class SensorLocation(BaseModel):
    id: int
    lat: float
    lon: float
    radius_m: float
    created_at: datetime
    last_seen_at: datetime
    label: Optional[str] = None
    fix_count: int = 0


class WifiDevice(BaseModel):
    bssid: str
    ssid: Optional[str] = None
    rssi: int
    frequency_mhz: Optional[int] = None
    channel: Optional[int] = None
    band: Optional[str] = None  # 2.4GHz / 5GHz / 6GHz
    encryption: Optional[str] = None  # OPEN/WEP/WPA/WPA2/WPA3
    cipher: Optional[str] = None
    auth: Optional[str] = None
    vendor_oui: Optional[str] = None
    vendor: Optional[str] = None
    capabilities: Optional[str] = None
    beacon_interval_ms: Optional[int] = None
    last_seen: datetime
    seen_count: int = 1


class BluetoothDevice(BaseModel):
    address: str
    name: Optional[str] = None
    rssi: int
    tx_power: Optional[int] = None
    address_type: Optional[str] = None  # public / random
    vendor: Optional[str] = None
    appearance: Optional[int] = None
    manufacturer_data: dict[int, str] = Field(default_factory=dict)
    service_uuids: list[str] = Field(default_factory=list)
    service_data: dict[str, str] = Field(default_factory=dict)
    is_connectable: Optional[bool] = None
    last_seen: datetime
    seen_count: int = 1


class BluetoothClassicDevice(BaseModel):
    """One BR/EDR sighting from a BlueZ inquiry. Classic devices broadcast
    only when in discoverable / pairing mode, so the population is sparse
    compared to BLE — but when one does show up we get the CoD (Class of
    Device) bitfield, which decodes into a useful peripheral category
    (audio / phone / peripheral / etc.) that BLE doesn't expose."""
    address: str
    name: Optional[str] = None
    rssi: int
    vendor: Optional[str] = None
    # 24-bit Bluetooth Class of Device; BlueZ exposes it as a single int.
    # Decoded into the major-device-class string in `device_class_label`.
    device_class: Optional[int] = None
    device_class_label: Optional[str] = None
    paired: Optional[bool] = None
    connected: Optional[bool] = None
    last_seen: datetime
    seen_count: int = 1


class DeviceObservation(BaseModel):
    """A single sighting tied to a sensor location."""
    location_id: int
    device_kind: Literal["wifi", "bluetooth"]
    device_id: str  # BSSID for wifi, MAC for BT
    rssi: int
    lat: Optional[float] = None
    lon: Optional[float] = None
    seen_at: datetime
    raw: dict = Field(default_factory=dict)


class AppSettings(BaseModel):
    # map
    map_provider: Literal[
        "osm", "osm_topo", "carto_positron", "carto_dark", "stamen_terrain", "esri_satellite",
        "google_roadmap", "google_satellite", "google_hybrid", "google_terrain"
    ] = "osm"

    # interfaces
    # wifi_interface: null = disabled (no scanning); "auto" = pick first
    # non-associated wireless interface at scan time; otherwise the literal
    # interface name (e.g. "wlan0").
    wifi_interface: Optional[str] = None
    bluetooth_adapter: Optional[str] = None  # e.g. hci0; null = system default

    # scan cadence
    wifi_scan_interval_s: int = 15
    bluetooth_scan_interval_s: int = 15
    bluetooth_scan_duration_s: int = 8
    gps_poll_interval_s: float = 1.0

    # Bluetooth Classic (BR/EDR) inquiry — separate loop because Classic
    # discovery only finds devices in discoverable / pairing mode and
    # requires switching the controller out of LE mode, which can briefly
    # stall the BLE scan. Off by default; longer interval than BLE since
    # the device population is sparse and short bursts work better than
    # the constant 8s scan the BLE loop runs.
    bluetooth_classic_enabled: bool = False
    bluetooth_classic_scan_interval_s: int = 60
    bluetooth_classic_scan_duration_s: int = 10

    # location clustering
    new_location_distance_m: float = 25.0  # if sensor moves more than this, open a new list
    # When enabled, radius grows with current GPS speed so a moving sensor opens
    # bigger bubbles. Effective radius = new_location_distance_m + speed * t_s,
    # clamped to a safety cap. With t=60s, a sensor at constant 10 m/s lands
    # near the edge of its bubble exactly 60s after the bubble was opened.
    new_location_dynamic: bool = False
    new_location_dynamic_t_s: float = 60.0
    location_label_template: str = "Loc {id} @ {lat:.5f},{lon:.5f}"

    # filtering
    min_rssi: int = -95
    hide_random_bt_addresses: bool = False

    # gpsd
    gpsd_host: str = "127.0.0.1"
    gpsd_port: int = 2947

    # probe-request scanner (passive WiFi client detection)
    probe_interface: Optional[str] = None      # e.g. wlan1mon; null = disabled
    probe_backend: Literal["tshark", "scapy"] = "tshark"
    probe_skip_randomized: bool = True         # drop locally-administered MACs
    probe_min_rssi: int = -90                  # noisier than AP scans, higher floor
    # Auto-configure the interface — runs `ip link` + `iw dev set type monitor`
    # on start and restores the prior mode on stop. Requires CAP_NET_ADMIN on
    # the `iw` binary (and `tshark` for the tshark backend) — file caps don't
    # propagate from python to subprocesses. setup.sh sets these. Disabled by
    # default since it disrupts any existing connectivity on the chosen iface.
    probe_auto_monitor: bool = False
    # Comma-separated channel list to cycle through while capturing. Without
    # hopping, the radio sits on whatever channel the driver happened to
    # land on and misses probes on every other channel. Default cycles the
    # 2.4 GHz non-overlapping channels (1, 6, 11). Empty disables hopping.
    probe_channels: str = "1,6,11"

    # notifications
    discord_webhook_url: Optional[str] = None
    discord_username: str = "Gjallarhorn"

    # retention — auto-purge old rows so the DB doesn't grow unbounded.
    # 0 = keep forever for either knob.
    observation_retention_days: int = 30
    device_retention_days: int = 0
