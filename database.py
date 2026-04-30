from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Optional

import aiosqlite

DB_PATH = Path(__file__).parent / "gjallarhorn.db"


SCHEMA = """
CREATE TABLE IF NOT EXISTS sensor_locations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    lat REAL NOT NULL,
    lon REAL NOT NULL,
    radius_m REAL NOT NULL,
    created_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    label TEXT,
    fix_count INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS devices (
    location_id INTEGER NOT NULL,
    kind TEXT NOT NULL,                -- 'wifi' | 'bluetooth'
    device_id TEXT NOT NULL,           -- BSSID or BT MAC
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    best_rssi INTEGER NOT NULL,
    last_rssi INTEGER NOT NULL,
    seen_count INTEGER NOT NULL DEFAULT 1,
    details_json TEXT NOT NULL,
    PRIMARY KEY (location_id, kind, device_id),
    FOREIGN KEY (location_id) REFERENCES sensor_locations(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    location_id INTEGER NOT NULL,
    kind TEXT NOT NULL,
    device_id TEXT NOT NULL,
    rssi INTEGER NOT NULL,
    lat REAL,
    lon REAL,
    seen_at TEXT NOT NULL,
    raw_json TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_obs_location ON observations(location_id);
CREATE INDEX IF NOT EXISTS idx_obs_device ON observations(kind, device_id);

CREATE TABLE IF NOT EXISTS settings_kv (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS oui_entries (
    prefix TEXT PRIMARY KEY,           -- uppercase hex, no separators (6/7/9 chars)
    registry TEXT NOT NULL,            -- MA-L | MA-M | MA-S | IAB
    organization TEXT NOT NULL,
    address TEXT
);

CREATE INDEX IF NOT EXISTS idx_oui_len ON oui_entries(length(prefix));
"""


async def init_db() -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript(SCHEMA)
        await db.commit()


async def get_setting(key: str) -> Optional[str]:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT value FROM settings_kv WHERE key=?", (key,)) as cur:
            row = await cur.fetchone()
            return row[0] if row else None


async def set_setting(key: str, value: str) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO settings_kv(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
        await db.commit()


async def create_location(lat: float, lon: float, radius_m: float, label: str | None) -> int:
    now = datetime.utcnow().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "INSERT INTO sensor_locations(lat,lon,radius_m,created_at,last_seen_at,label,fix_count) "
            "VALUES(?,?,?,?,?,?,1)",
            (lat, lon, radius_m, now, now, label),
        )
        await db.commit()
        return cur.lastrowid


async def touch_location(location_id: int) -> None:
    now = datetime.utcnow().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE sensor_locations SET last_seen_at=?, fix_count=fix_count+1 WHERE id=?",
            (now, location_id),
        )
        await db.commit()


async def list_locations() -> list[dict]:
    sql = """
        SELECT l.*,
            COALESCE(SUM(CASE WHEN d.kind = 'wifi'      THEN 1 ELSE 0 END), 0) AS wifi_count,
            COALESCE(SUM(CASE WHEN d.kind = 'bluetooth' THEN 1 ELSE 0 END), 0) AS bt_count,
            COALESCE(SUM(d.seen_count), 0) AS total_observations
        FROM sensor_locations l
        LEFT JOIN devices d ON d.location_id = l.id
        GROUP BY l.id
        ORDER BY l.id DESC
    """
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(sql) as cur:
            return [dict(r) for r in await cur.fetchall()]


async def delete_all_locations() -> dict:
    """Delete every sensor location and all associated devices/observations.
    Returns the row counts that were removed."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT COUNT(*) FROM sensor_locations") as cur:
            n_loc = (await cur.fetchone())[0]
        async with db.execute("SELECT COUNT(*) FROM devices") as cur:
            n_dev = (await cur.fetchone())[0]
        async with db.execute("SELECT COUNT(*) FROM observations") as cur:
            n_obs = (await cur.fetchone())[0]
        await db.execute("DELETE FROM observations")
        await db.execute("DELETE FROM devices")
        await db.execute("DELETE FROM sensor_locations")
        # Reset AUTOINCREMENT counters so new ids start from 1
        await db.execute(
            "DELETE FROM sqlite_sequence WHERE name IN ('sensor_locations','observations')"
        )
        await db.commit()
    return {"locations": n_loc, "devices": n_dev, "observations": n_obs}


async def update_location_label(location_id: int, label: str) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE sensor_locations SET label=? WHERE id=?", (label, location_id)
        )
        await db.commit()


async def upsert_device(
    location_id: int,
    kind: str,
    device_id: str,
    rssi: int,
    details: dict,
) -> None:
    now = datetime.utcnow().isoformat()
    payload = json.dumps(details, default=str)
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT best_rssi, seen_count FROM devices WHERE location_id=? AND kind=? AND device_id=?",
            (location_id, kind, device_id),
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            await db.execute(
                "INSERT INTO devices(location_id,kind,device_id,first_seen,last_seen,"
                "best_rssi,last_rssi,seen_count,details_json) VALUES(?,?,?,?,?,?,?,1,?)",
                (location_id, kind, device_id, now, now, rssi, rssi, payload),
            )
        else:
            best = max(row[0], rssi)  # rssi is negative — higher is better
            await db.execute(
                "UPDATE devices SET last_seen=?, best_rssi=?, last_rssi=?, "
                "seen_count=seen_count+1, details_json=? "
                "WHERE location_id=? AND kind=? AND device_id=?",
                (now, best, rssi, payload, location_id, kind, device_id),
            )
        await db.commit()


async def insert_observation(
    location_id: int,
    kind: str,
    device_id: str,
    rssi: int,
    lat: float | None,
    lon: float | None,
    raw: dict,
) -> None:
    now = datetime.utcnow().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO observations(location_id,kind,device_id,rssi,lat,lon,seen_at,raw_json) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (location_id, kind, device_id, rssi, lat, lon, now, json.dumps(raw, default=str)),
        )
        await db.commit()


async def replace_oui_entries(rows: list[tuple[str, str, str, str | None]]) -> int:
    """Replace the OUI table contents in a single transaction. Returns inserted count."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM oui_entries")
        await db.executemany(
            "INSERT OR REPLACE INTO oui_entries(prefix,registry,organization,address) VALUES(?,?,?,?)",
            rows,
        )
        await db.commit()
        async with db.execute("SELECT COUNT(*) FROM oui_entries") as cur:
            n = (await cur.fetchone())[0]
    return n


async def load_all_oui() -> list[tuple[str, str, str]]:
    """Return (prefix, registry, organization) for every row, ordered longest-prefix-first."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT prefix, registry, organization FROM oui_entries "
            "ORDER BY length(prefix) DESC"
        ) as cur:
            return [(r[0], r[1], r[2]) for r in await cur.fetchall()]


async def oui_count() -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT COUNT(*) FROM oui_entries") as cur:
            row = await cur.fetchone()
            return row[0] if row else 0


async def oui_counts_by_registry() -> dict[str, int]:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT registry, COUNT(*) FROM oui_entries GROUP BY registry"
        ) as cur:
            return {row[0]: row[1] for row in await cur.fetchall()}


async def devices_at_location(location_id: int, kind: str | None = None) -> list[dict]:
    sql = "SELECT * FROM devices WHERE location_id=?"
    args: tuple = (location_id,)
    if kind:
        sql += " AND kind=?"
        args = (location_id, kind)
    sql += " ORDER BY best_rssi DESC"
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(sql, args) as cur:
            rows = [dict(r) for r in await cur.fetchall()]
    for r in rows:
        r["details"] = json.loads(r.pop("details_json"))
    return rows
