from __future__ import annotations

import json
import math
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
    notify_discord INTEGER NOT NULL DEFAULT 0,
    audible INTEGER NOT NULL DEFAULT 0,
    extra_conditions TEXT NOT NULL DEFAULT '[]',  -- JSON list of {match_type,match_value}; AND-combined with the primary match
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

CREATE TABLE IF NOT EXISTS device_whitelist (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,             -- 'wifi' | 'bluetooth' | 'wifi_client'
    device_id TEXT NOT NULL,        -- exact id, or a prefix (matches like the device_id rule)
    note TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(kind, device_id)
);
"""


async def init_db() -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript(SCHEMA)
        await _migrate(db)
        await db.commit()


async def _migrate(db: aiosqlite.Connection) -> None:
    """Idempotent ALTER TABLEs for columns added after a DB was first created.
    `CREATE TABLE IF NOT EXISTS` won't add new columns to an existing table."""
    async with db.execute("PRAGMA table_info(alert_rules)") as cur:
        cols = {row[1] for row in await cur.fetchall()}
    if "notify_discord" not in cols:
        await db.execute(
            "ALTER TABLE alert_rules ADD COLUMN notify_discord INTEGER NOT NULL DEFAULT 0"
        )
    if "audible" not in cols:
        await db.execute(
            "ALTER TABLE alert_rules ADD COLUMN audible INTEGER NOT NULL DEFAULT 0"
        )
    if "extra_conditions" not in cols:
        await db.execute(
            "ALTER TABLE alert_rules ADD COLUMN extra_conditions TEXT NOT NULL DEFAULT '[]'"
        )


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


async def delete_location(location_id: int) -> dict:
    """Delete one sensor location and its devices + observations.
    Returns the row counts removed (0 across the board if the location
    didn't exist)."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT 1 FROM sensor_locations WHERE id=?", (location_id,)
        ) as cur:
            if await cur.fetchone() is None:
                return {"locations": 0, "devices": 0, "observations": 0}
        async with db.execute(
            "SELECT COUNT(*) FROM devices WHERE location_id=?", (location_id,)
        ) as cur:
            n_dev = (await cur.fetchone())[0]
        async with db.execute(
            "SELECT COUNT(*) FROM observations WHERE location_id=?", (location_id,)
        ) as cur:
            n_obs = (await cur.fetchone())[0]
        await db.execute("DELETE FROM observations WHERE location_id=?", (location_id,))
        await db.execute("DELETE FROM devices WHERE location_id=?", (location_id,))
        await db.execute("DELETE FROM sensor_locations WHERE id=?", (location_id,))
        await db.commit()
    return {"locations": 1, "devices": n_dev, "observations": n_obs}


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in metres. Inlined here so database.py doesn't
    need to import services.location_manager (circular)."""
    R = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


async def find_contained_locations() -> list[dict]:
    """Return every (loser, winner) pair where the loser's centroid sits
    inside the winner's radius — the same containment rule LocationManager
    uses to attribute new fixes. Pairs are sorted so the smallest loser
    inside the largest winner is merged first; that keeps tiny clusters
    from "winning" against the bubble that contains them."""
    locs = await list_locations()
    pairs: list[dict] = []
    for a in locs:
        for b in locs:
            if a["id"] == b["id"]:
                continue
            d = _haversine_m(a["lat"], a["lon"], b["lat"], b["lon"])
            if d <= b["radius_m"]:
                pairs.append({
                    "loser_id": a["id"],
                    "winner_id": b["id"],
                    "distance_m": d,
                    "loser_radius": a["radius_m"],
                    "winner_radius": b["radius_m"],
                    "loser_fix_count": a.get("fix_count", 0),
                    "winner_fix_count": b.get("fix_count", 0),
                })
    pairs.sort(key=lambda p: (
        p["loser_radius"],            # smallest loser first
        -p["winner_radius"],          # largest winner first
        -p["winner_fix_count"],       # most-trafficked winner
        p["winner_id"],               # oldest winner (lowest id) breaks ties
    ))
    return pairs


async def merge_locations(loser_id: int, winner_id: int) -> dict:
    """Move every device/observation/alert tied to `loser_id` onto
    `winner_id`, combining colliding device rows by (kind, device_id),
    expand the winner's radius to cover the loser if needed, then delete
    the loser row. Returns counts for the UI."""
    if loser_id == winner_id:
        return {"loser_id": loser_id, "winner_id": winner_id, "noop": True}

    counts = {
        "loser_id": loser_id,
        "winner_id": winner_id,
        "devices_moved": 0,
        "devices_combined": 0,
        "observations_moved": 0,
        "alert_events_moved": 0,
        "alert_rules_repointed": 0,
        "winner_radius_before": None,
        "winner_radius_after": None,
    }

    async with aiosqlite.connect(DB_PATH) as db:
        # Sanity: both rows must exist, otherwise return a no-op shape.
        async with db.execute(
            "SELECT id, lat, lon, radius_m, fix_count, last_seen_at "
            "FROM sensor_locations WHERE id IN (?,?)",
            (loser_id, winner_id),
        ) as cur:
            rows = {r[0]: r for r in await cur.fetchall()}
        if loser_id not in rows or winner_id not in rows:
            counts["noop"] = True
            return counts

        l_id, l_lat, l_lon, l_radius, l_fix_count, l_last_seen = rows[loser_id]
        w_id, w_lat, w_lon, w_radius, w_fix_count, w_last_seen = rows[winner_id]
        counts["winner_radius_before"] = w_radius

        # 1. Observations have no unique constraint — single UPDATE moves them.
        cur = await db.execute(
            "UPDATE observations SET location_id=? WHERE location_id=?",
            (winner_id, loser_id),
        )
        counts["observations_moved"] = cur.rowcount or 0

        # 2. Devices: combine on collision because PK is (location_id, kind, device_id).
        async with db.execute(
            "SELECT kind, device_id, first_seen, last_seen, best_rssi, "
            "last_rssi, seen_count, details_json FROM devices WHERE location_id=?",
            (loser_id,),
        ) as cur:
            loser_devs = await cur.fetchall()
        for k, did, l_first, l_last, l_best, l_last_rssi, l_seen, l_details in loser_devs:
            async with db.execute(
                "SELECT first_seen, last_seen, best_rssi, last_rssi, seen_count, details_json "
                "FROM devices WHERE location_id=? AND kind=? AND device_id=?",
                (winner_id, k, did),
            ) as c:
                row = await c.fetchone()
            if row is None:
                # No collision — just rewrite the FK.
                await db.execute(
                    "UPDATE devices SET location_id=? "
                    "WHERE location_id=? AND kind=? AND device_id=?",
                    (winner_id, loser_id, k, did),
                )
                counts["devices_moved"] += 1
            else:
                w_first, w_last, w_best, w_last_rssi_, w_seen, w_details = row
                # RSSI is negative — "best" is the max (least-negative).
                merged_best = max(l_best, w_best)
                merged_first = min(l_first, w_first)
                merged_last = max(l_last, w_last)
                merged_last_rssi = l_last_rssi if l_last >= w_last else w_last_rssi_
                merged_details = w_details if w_last >= l_last else l_details
                merged_seen = (l_seen or 0) + (w_seen or 0)
                await db.execute(
                    "UPDATE devices SET first_seen=?, last_seen=?, best_rssi=?, "
                    "last_rssi=?, seen_count=?, details_json=? "
                    "WHERE location_id=? AND kind=? AND device_id=?",
                    (merged_first, merged_last, merged_best, merged_last_rssi,
                     merged_seen, merged_details, winner_id, k, did),
                )
                await db.execute(
                    "DELETE FROM devices WHERE location_id=? AND kind=? AND device_id=?",
                    (loser_id, k, did),
                )
                counts["devices_combined"] += 1

        # 3. Alert events tied to the loser get repointed (informational FK).
        cur = await db.execute(
            "UPDATE alert_events SET location_id=? WHERE location_id=?",
            (winner_id, loser_id),
        )
        counts["alert_events_moved"] = cur.rowcount or 0

        # 4. Alert rules pinned to the loser keep firing at the winner.
        cur = await db.execute(
            "UPDATE alert_rules SET location_id=? WHERE location_id=?",
            (winner_id, loser_id),
        )
        counts["alert_rules_repointed"] = cur.rowcount or 0

        # 5. Update winner aggregates: expand radius to engulf the loser if
        # the loser's full circle wasn't already inside, and roll up totals.
        dist = _haversine_m(w_lat, w_lon, l_lat, l_lon)
        new_radius = max(w_radius, dist + l_radius)
        new_fix_count = (w_fix_count or 0) + (l_fix_count or 0)
        new_last_seen = max(w_last_seen or "", l_last_seen or "") or w_last_seen
        await db.execute(
            "UPDATE sensor_locations "
            "SET radius_m=?, fix_count=?, last_seen_at=? WHERE id=?",
            (new_radius, new_fix_count, new_last_seen, winner_id),
        )
        counts["winner_radius_after"] = new_radius

        # 6. Drop the loser row (CASCADE on devices is moot now — they're gone).
        await db.execute("DELETE FROM sensor_locations WHERE id=?", (loser_id,))
        await db.commit()

    return counts


async def auto_merge_contained(*, max_iterations: int = 1000) -> dict:
    """Iteratively merge every location whose centroid sits inside another
    location's radius. Re-evaluates after each merge so transitive chains
    (A inside B inside C) collapse cleanly into the outermost survivor."""
    merged: list[dict] = []
    for _ in range(max_iterations):
        pairs = await find_contained_locations()
        if not pairs:
            break
        p = pairs[0]
        merged.append(await merge_locations(p["loser_id"], p["winner_id"]))
    return {
        "merged": len(merged),
        "details": merged,
        "loser_ids": [m["loser_id"] for m in merged],
    }


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


async def get_device_details(location_id: int, kind: str, device_id: str) -> dict | None:
    """Return just the details_json (parsed) for one device, or None if absent.
    Cheaper than devices_at_location when you only need to merge with prior."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT details_json FROM devices WHERE location_id=? AND kind=? AND device_id=?",
            (location_id, kind, device_id),
        ) as cur:
            row = await cur.fetchone()
    if not row:
        return None
    try:
        return json.loads(row[0] or "{}")
    except (TypeError, ValueError):
        return {}


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
            rows = [dict(r) for r in await cur.fetchall()]
    # Inflate the extra_conditions JSON so callers (alert_service, API)
    # can use it as a list directly without parsing each time.
    for r in rows:
        raw = r.get("extra_conditions") or "[]"
        try:
            r["extra_conditions"] = json.loads(raw)
        except (TypeError, ValueError):
            r["extra_conditions"] = []
    return rows


async def create_alert_rule(
    name: str, kind: str | None, match_type: str, match_value: str,
    location_id: int | None, notify_discord: bool = False,
    audible: bool = False, extra_conditions: list | None = None,
) -> int:
    now = datetime.utcnow().isoformat()
    extra_json = json.dumps(extra_conditions or [])
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "INSERT INTO alert_rules(name,enabled,kind,match_type,match_value,"
            "location_id,notify_discord,audible,extra_conditions,created_at) "
            "VALUES(?,1,?,?,?,?,?,?,?,?)",
            (name, kind, match_type, match_value, location_id,
             1 if notify_discord else 0, 1 if audible else 0,
             extra_json, now),
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
        "SELECT e.*, r.name AS rule_name, r.match_type AS rule_match_type, "
        "       r.audible AS rule_audible "
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


async def list_common_devices(min_locations: int = 2, limit: int = 50) -> list[dict]:
    """Devices seen at >= min_locations distinct locations.

    For each kind/device_id, returns the location list, total observations
    across all locations, the best RSSI ever recorded, and a representative
    name/vendor (taken from the most-recently-seen row)."""
    sql = """
        WITH ranked AS (
            SELECT kind, device_id, location_id, last_seen, best_rssi, seen_count, details_json,
                   ROW_NUMBER() OVER (
                       PARTITION BY kind, device_id ORDER BY last_seen DESC
                   ) AS rn
            FROM devices
        ),
        agg AS (
            SELECT kind, device_id,
                   COUNT(DISTINCT location_id) AS n_locations,
                   SUM(seen_count)             AS total_seen,
                   MAX(best_rssi)              AS max_rssi,
                   GROUP_CONCAT(location_id)   AS location_ids
            FROM devices
            GROUP BY kind, device_id
            HAVING COUNT(DISTINCT location_id) >= ?
        )
        SELECT a.*, r.details_json AS latest_details
        FROM agg a
        JOIN ranked r ON r.kind = a.kind AND r.device_id = a.device_id AND r.rn = 1
        ORDER BY a.n_locations DESC, a.total_seen DESC
        LIMIT ?
    """
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(sql, (min_locations, limit)) as cur:
            rows = [dict(r) for r in await cur.fetchall()]
    for r in rows:
        try:
            r["details"] = json.loads(r.pop("latest_details") or "{}")
        except (TypeError, ValueError):
            r["details"] = {}
        ids = (r.get("location_ids") or "")
        r["locations"] = sorted({int(x) for x in ids.split(",") if x})
        r.pop("location_ids", None)
    return rows


# ---------- whitelist ----------
async def list_whitelist() -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM device_whitelist ORDER BY kind, device_id"
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]


async def add_whitelist(kind: str, device_id: str, note: str | None = None) -> int:
    """Insert a whitelist entry (or update its note if the (kind, device_id)
    pair already exists). Returns the row id."""
    now = datetime.utcnow().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO device_whitelist(kind, device_id, note, created_at) "
            "VALUES(?,?,?,?) "
            "ON CONFLICT(kind, device_id) DO UPDATE SET note=excluded.note",
            (kind, device_id.lower(), note, now),
        )
        await db.commit()
        async with db.execute(
            "SELECT id FROM device_whitelist WHERE kind=? AND device_id=?",
            (kind, device_id.lower()),
        ) as cur:
            row = await cur.fetchone()
    return int(row[0]) if row else 0


async def delete_whitelist(entry_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("DELETE FROM device_whitelist WHERE id=?", (entry_id,))
        await db.commit()
        return cur.rowcount > 0


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
