# Gjallarhorn

A FastAPI web UI that uses **gpsd** to track its location, scans nearby
**WiFi** and **Bluetooth (BLE)** devices, and clusters every sighting
into per-location lists. When the sensor moves more than a configured
distance from the active list's centroid, a new location/list is
automatically opened.

> Designed for Linux (Raspberry Pi, kismet-style sensor box, etc.).
> `gpsd`, `iw`, and BlueZ are Linux tools — the wifi/bluetooth scans
> won't work on Windows or macOS, but the web UI will still run.

## Features

- **Map tab** — live position, accuracy circle, all opened sensor
  locations as circles (active in green, others in orange).
- **Devices tab** — pick a location and view every WiFi AP / BLE
  device seen there, with full details (BSSID, SSID, channel, band,
  encryption suite, cipher, auth, capabilities, beacon interval, OUI;
  BT MAC, name, RSSI, TX power, manufacturer data, service UUIDs/data,
  appearance, address type).
- **Locations tab** — list all locations, rename them, force-open a
  new one at the current fix.
- **Settings tab** — map provider (OSM, OpenTopo, Carto Light/Dark,
  Stamen Terrain, Esri Satellite), wifi interface, bluetooth adapter,
  scan intervals, distance threshold for new lists, RSSI floor,
  gpsd host/port.

## Install

```bash
sudo apt install gpsd gpsd-clients iw bluez python3-venv
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

WiFi scanning with `iw dev <iface> scan` requires CAP_NET_ADMIN. The
two simplest options:

```bash
# A) just run the server as root (easy, blunt)
sudo .venv/bin/python gjallarhorn.py

# B) grant the capability to your python interpreter (per-venv)
sudo setcap cap_net_admin,cap_net_raw+eip $(readlink -f .venv/bin/python3)
```

Bluetooth (bleak / BlueZ) usually works without elevation if your user
is in the `bluetooth` group; otherwise run as root.

## Run

```bash
# make sure gpsd is up
sudo systemctl start gpsd

python gjallarhorn.py     # listens on http://0.0.0.0:5003
```

Open the UI, go to **Settings**, pick your wifi interface and bluetooth
adapter, hit **Save**. Scans start immediately on the configured
intervals.

## How clustering works

1. The GPS service polls gpsd at `gps_poll_interval_s`.
2. The location manager keeps an active sensor location (lat/lon
   centroid).
3. On every fix it computes haversine distance to the centroid. If
   `distance > new_location_distance_m`, it inserts a new
   `sensor_locations` row and that becomes the active list.
4. WiFi and BLE scan loops attribute every sighting to the currently
   active location id, so devices end up partitioned by where the
   sensor was.

Devices are stored once per `(location_id, kind, device_id)` with
`best_rssi`, `last_rssi`, `seen_count`, `first_seen`, `last_seen`,
plus a JSON blob of every detail captured. Each individual sighting
also goes into the `observations` table for time-series analysis.

## Layout

```
gjallarhorn.py           # FastAPI app + lifespan
config.py                # settings persistence wrapper
database.py              # aiosqlite schema + queries
models.py                # pydantic models
services/
  gps_service.py         # gpsd polling
  wifi_scanner.py        # `iw dev <iface> scan` parser
  bluetooth_scanner.py   # bleak BLE scanner
  location_manager.py    # haversine clustering
  scan_orchestrator.py   # background loops
static/
  index.html             # tabs: map / devices / locations / settings
  app.js                 # leaflet + REST polling
  style.css
```

## API quick reference

| Method | Path                                   | Purpose                       |
|--------|----------------------------------------|-------------------------------|
| GET    | `/api/gps`                             | current fix + active loc id   |
| GET    | `/api/settings`                        | read settings                 |
| PUT    | `/api/settings`                        | patch settings                |
| GET    | `/api/interfaces/wifi`                 | wifi interfaces from `iw`     |
| GET    | `/api/interfaces/bluetooth`            | BlueZ adapters                |
| GET    | `/api/map_providers`                   | tile provider catalog         |
| GET    | `/api/locations`                       | all locations + active id     |
| PATCH  | `/api/locations/{id}`                  | rename a location             |
| GET    | `/api/locations/{id}/devices?kind=...` | devices seen at a location    |
| POST   | `/api/locations/new`                   | force-open new location       |
