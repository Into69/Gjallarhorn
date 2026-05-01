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
    wifi_interface: Optional[str] = None  # e.g. wlan0; null = auto-pick
    bluetooth_adapter: Optional[str] = None  # e.g. hci0; null = system default

    # scan cadence
    wifi_scan_interval_s: int = 15
    bluetooth_scan_interval_s: int = 15
    bluetooth_scan_duration_s: int = 8
    gps_poll_interval_s: float = 1.0

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

    # notifications
    discord_webhook_url: Optional[str] = None
    discord_username: str = "Gjallarhorn"
