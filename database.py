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

CREATE TABLE IF NOT EXISTS alert_rules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    kind TEXT,                              -- 'wifi' | 'bluetooth' | NULL = both
    match_type TEXT NOT NULL,               -- device_id | name_contains | vendor_contains | rssi_above
    match_value TEXT NOT NULL,
    location_id INTEGER,                    -- NULL = any location
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS alert_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    rule_id INTEGER NOT NULL,
    triggered_at TEXT NOT NULL,
    location_id INTEGER,
    device_kind TEXT NOT NULL,
    device_id TEXT NOT NULL,
    rssi INTEGER,
    details_json TEXT NOT NULL,
    FOREIGN KEY (rule_id) REFERENCES alert_rules(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_alert_events_time ON alert_events(triggered_at DESC);
CREATE INDEX IF NOT EXISTS idx_alert_events_rule ON alert_events(rule_id);
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


async def list_location_centroids() -> list[dict]:
    """Lightweight: id/lat/lon/radius for every location. Used by the
    location manager to test 'am I inside an existing radius'."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT id, lat, lon, radius_m FROM sensor_locations"
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]


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
) -> bool:
    """Upsert a device row. Returns True if a new row was inserted, False if updated."""
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
            await db.commit()
            return True
        best = max(row[0], rssi)  # rssi is negative — higher is better
        await db.execute(
            "UPDATE devices SET last_seen=?, best_rssi=?, last_rssi=?, "
            "seen_count=seen_count+1, details_json=? "
            "WHERE location_id=? AND kind=? AND device_id=?",
            (now, best, rssi, payload, location_id, kind, device_id),
        )
        await db.commit()
        return False


async def get_location_created_at(location_id: int) -> str | None:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT created_at FROM sensor_locations WHERE id=?", (location_id,)
        ) as cur:
            row = await cur.fetchone()
            return row[0] if row else None


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


# ---------- alerts ----------
async def list_alert_rules() -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM alert_rules ORDER BY id DESC"
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]


async def create_alert_rule(
    name: str, kind: str | None, match_type: str, match_value: str,
    location_id: int | None,
) -> int:
    now = datetime.utcnow().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "INSERT INTO alert_rules(name,enabled,kind,match_type,match_value,location_id,created_at) "
            "VALUES(?,1,?,?,?,?,?)",
            (name, kind, match_type, match_value, location_id, now),
        )
        await db.commit()
        return cur.lastrowid


async def update_alert_rule(rule_id: int, fields: dict) -> None:
    if not fields:
        return
    cols = ", ".join(f"{k}=?" for k in fields.keys())
    args = list(fields.values()) + [rule_id]
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(f"UPDATE alert_rules SET {cols} WHERE id=?", args)
        await db.commit()


async def delete_alert_rule(rule_id: int) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM alert_events WHERE rule_id=?", (rule_id,))
        await db.execute("DELETE FROM alert_rules WHERE id=?", (rule_id,))
        await db.commit()


async def insert_alert_event(
    rule_id: int, location_id: int | None, device_kind: str, device_id: str,
    rssi: int | None, details: dict,
) -> int:
    now = datetime.utcnow().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "INSERT INTO alert_events(rule_id,triggered_at,location_id,device_kind,"
            "device_id,rssi,details_json) VALUES(?,?,?,?,?,?,?)",
            (rule_id, now, location_id, device_kind, device_id, rssi, json.dumps(details, default=str)),
        )
        await db.commit()
        return cur.lastrowid


async def list_alert_events(limit: int = 100, since_id: int | None = None) -> list[dict]:
    sql = (
        "SELECT e.*, r.name AS rule_name, r.match_type AS rule_match_type "
        "FROM alert_events e LEFT JOIN alert_rules r ON r.id = e.rule_id "
    )
    args: list = []
    if since_id is not None:
        sql += "WHERE e.id > ? "
        args.append(since_id)
    sql += "ORDER BY e.id DESC LIMIT ?"
    args.append(limit)
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(sql, tuple(args)) as cur:
            rows = [dict(r) for r in await cur.fetchall()]
    for r in rows:
        try:
            r["details"] = json.loads(r.pop("details_json"))
        except (TypeError, ValueError):
            r["details"] = {}
    return rows


async def count_device_in_recent_locations(kind: str, device_id: str, n_locations: int) -> int:
    """How many of the most recent N locations have this device in their devices row.

    Used by the cross_location alert rule.
    """
    if n_locations < 1:
        n_locations = 1
    sql = """
        SELECT COUNT(DISTINCT location_id) FROM devices
        WHERE kind=? AND device_id=?
          AND location_id IN (SELECT id FROM sensor_locations ORDER BY id DESC LIMIT ?)
    """
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(sql, (kind, device_id, n_locations)) as cur:
            row = await cur.fetchone()
            return row[0] if row else 0


async def clear_alert_events() -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT COUNT(*) FROM alert_events") as cur:
            n = (await cur.fetchone())[0]
        await db.execute("DELETE FROM alert_events")
        await db.commit()
    return n


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
