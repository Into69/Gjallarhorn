from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import aiosqlite

DB_PATH = Path(__file__).parent / "gjallarhorn.db"

# Default busy timeout for every aiosqlite connection. aiosqlite passes
# this to the underlying sqlite3 connection's `timeout=` kwarg, which
# governs how long a write blocks waiting for an exclusive lock before
# raising OperationalError("database is locked"). 10 s is plenty for the
# parallel scan loops + absence sweep + mission polling now running
# against the same file, even on Pi-class SD storage. Pair with the
# journal_mode=WAL pragma flipped in init_db() so readers don't have
# to fight writers in the first place.
_BUSY_TIMEOUT_S = 10.0


def _connect():
    """All aiosqlite connections route through here so the busy timeout
    is set consistently across every helper. Use exactly like
    `_connect()` was used before — the call site stays
    `async with _connect() as db: …`."""
    return aiosqlite.connect(DB_PATH, timeout=_BUSY_TIMEOUT_S)


SCHEMA = """
CREATE TABLE IF NOT EXISTS sensor_locations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    lat REAL NOT NULL,
    lon REAL NOT NULL,
    radius_m REAL NOT NULL,
    created_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    label TEXT,
    fix_count INTEGER NOT NULL DEFAULT 0,
    source TEXT NOT NULL DEFAULT 'auto'   -- 'auto' (clustered) | 'manual' (drawn geofence)
);

CREATE TABLE IF NOT EXISTS devices (
    location_id INTEGER NOT NULL,
    kind TEXT NOT NULL,                -- 'wifi' | 'bluetooth' | 'wifi_client'
    device_id TEXT NOT NULL,           -- BSSID or BT MAC
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    best_rssi INTEGER NOT NULL,
    last_rssi INTEGER NOT NULL,
    seen_count INTEGER NOT NULL DEFAULT 1,
    details_json TEXT NOT NULL,
    signature TEXT,                    -- BLE: stable adv-data fingerprint for cross-MAC linking; NULL otherwise
    PRIMARY KEY (location_id, kind, device_id),
    FOREIGN KEY (location_id) REFERENCES sensor_locations(id) ON DELETE CASCADE
);
-- idx_devices_signature is created in _migrate after the column is
-- guaranteed to exist (an existing DB upgraded from before this column
-- would error here on `no such column: signature`).

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
    latch INTEGER NOT NULL DEFAULT 1,       -- 1 = fire once per (rule,device) pair until cleared; 0 = fire every time conditions match
    include_whitelist INTEGER NOT NULL DEFAULT 0,  -- 1 = this rule sees whitelisted devices; 0 = whitelist suppresses it (default)
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
    cleared INTEGER NOT NULL DEFAULT 0,    -- 0 = latched (suppresses re-fire); 1 = acknowledged (allows re-fire); 2 = dismissed (hidden from feed, suppresses re-fire — set by per-row trash)
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

-- Session-scoped whitelist: matches the permanent table at runtime but is
-- wiped wholesale by "Delete all locations". Used by the baseline-scan
-- modal — every device the user doesn't promote to permanent lands here,
-- silencing it until the operator resets their location set.
CREATE TABLE IF NOT EXISTS device_temp_whitelist (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    device_id TEXT NOT NULL,
    note TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(kind, device_id)
);

CREATE TABLE IF NOT EXISTS missions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    description TEXT,
    started_at TEXT NOT NULL,
    ended_at TEXT,                          -- NULL while active
    stats_start_json TEXT NOT NULL,         -- snapshot of about_stats() at start
    stats_end_json TEXT,                    -- snapshot at end (filled by /end)
    notes TEXT                              -- operator-supplied free-text
);

CREATE INDEX IF NOT EXISTS idx_missions_active
    ON missions(ended_at) WHERE ended_at IS NULL;

-- Whitelisted devices' sightings get copied here before the parent
-- sensor_location is deleted, so the historic record survives. Same
-- shape as `devices` minus the location FK; combined on collisions
-- (sum seen_count, max best_rssi, min first_seen, max last_seen).
CREATE TABLE IF NOT EXISTS preserved_devices (
    kind TEXT NOT NULL,
    device_id TEXT NOT NULL,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    best_rssi INTEGER NOT NULL,
    last_rssi INTEGER NOT NULL,
    seen_count INTEGER NOT NULL,
    details_json TEXT NOT NULL,
    archived_from_location_id INTEGER,
    archived_at TEXT NOT NULL,
    PRIMARY KEY (kind, device_id)
);
"""


async def init_db() -> None:
    async with _connect() as db:
        # Switch to WAL once at boot so subsequent connections inherit it.
        # WAL is per-database-file, not per-connection — flipping it on
        # here persists for the lifetime of the file.
        #   journal_mode=WAL  — readers don't block writers, contention
        #                       between the scan loops + absence sweep +
        #                       mission polling drops dramatically.
        #   synchronous=NORMAL — safe with WAL, much faster than FULL on
        #                        Raspberry-Pi-class SD storage.
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("PRAGMA synchronous=NORMAL")
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
    if "latch" not in cols:
        # Default 1 preserves existing behaviour for pre-migration rules:
        # every fire is latched until the user clears it. Operators who
        # want the noisier "fire on every match" can opt in per-rule.
        await db.execute(
            "ALTER TABLE alert_rules ADD COLUMN latch INTEGER NOT NULL DEFAULT 1"
        )
    if "include_whitelist" not in cols:
        # Default 0 preserves the historical whitelist-as-mute behaviour:
        # whitelisted devices stay silent for every rule. Operators who
        # want a rule to fire on a whitelisted MAC (e.g. "tell me when
        # my own phone leaves home") opt in per-rule.
        await db.execute(
            "ALTER TABLE alert_rules ADD COLUMN include_whitelist INTEGER NOT NULL DEFAULT 0"
        )
    if "disabled_reason" not in cols:
        # Set by the broken-reference sweep when a rule's location_id
        # points at a now-deleted sensor_location. Non-NULL value blocks
        # re-enabling the rule until the operator fixes the reference
        # (PATCH endpoint clears it when location_id moves to a valid /
        # null value).
        await db.execute(
            "ALTER TABLE alert_rules ADD COLUMN disabled_reason TEXT"
        )

    async with db.execute("PRAGMA table_info(sensor_locations)") as cur:
        loc_cols = {row[1] for row in await cur.fetchall()}
    if "source" not in loc_cols:
        await db.execute(
            "ALTER TABLE sensor_locations ADD COLUMN source TEXT NOT NULL DEFAULT 'auto'"
        )

    # BLE-signature column for cross-MAC linking. Backfill from existing
    # rows on first migration so devices captured before this lands also
    # get fingerprinted.
    async with db.execute("PRAGMA table_info(devices)") as cur:
        dev_cols = {row[1] for row in await cur.fetchall()}
    if "signature" not in dev_cols:
        await db.execute("ALTER TABLE devices ADD COLUMN signature TEXT")
        async with db.execute(
            "SELECT location_id, kind, device_id, details_json FROM devices "
            "WHERE kind='bluetooth'"
        ) as cur:
            rows = await cur.fetchall()
        for loc_id, kind, did, raw in rows:
            try:
                d = json.loads(raw or "{}")
            except (TypeError, ValueError):
                continue
            sig = compute_ble_signature(kind, d)
            if sig:
                await db.execute(
                    "UPDATE devices SET signature=? "
                    "WHERE location_id=? AND kind=? AND device_id=?",
                    (sig, loc_id, kind, did),
                )
    # Always (re)create the index — IF NOT EXISTS makes it a no-op when
    # already present. Runs unconditionally so the index exists for both
    # fresh DBs (column created by SCHEMA) and migrated ones.
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_devices_signature ON devices(signature)"
    )

    # Latching: alert_events.cleared distinguishes acknowledged events
    # from active latches that should suppress re-fires until cleared.
    async with db.execute("PRAGMA table_info(alert_events)") as cur:
        ev_cols = {row[1] for row in await cur.fetchall()}
    if "cleared" not in ev_cols:
        await db.execute(
            "ALTER TABLE alert_events ADD COLUMN cleared INTEGER NOT NULL DEFAULT 0"
        )

    # Bluetooth case + cross-kind merge. The Classic scanner used to
    # store MACs lowercase while bleak's BLE rows are uppercase, so the
    # same physical device could land in two rows at one location. Two
    # passes:
    #   1. Collapse every (BLE, Classic) pair at the same location into
    #      the BLE row, folding Classic-only fields under
    #      details['classic'] and repointing observations / alert
    #      events at the survivor.
    #   2. Normalize remaining bluetooth* device_ids to uppercase across
    #      every table that stores raw MACs. Where a within-kind case
    #      duplicate exists (rare — bleak is consistent), merge the
    #      lowercase row into the uppercase one rather than letting the
    #      PRIMARY KEY collision abort the migration.
    async with db.execute(
        "SELECT location_id, lower(device_id) FROM devices "
        "WHERE kind IN ('bluetooth','bluetooth_classic') "
        "GROUP BY location_id, lower(device_id) "
        "HAVING COUNT(DISTINCT kind) > 1"
    ) as cur:
        cross_pairs = await cur.fetchall()
    for loc_id, mac_l in cross_pairs:
        mac = mac_l.upper()
        async with db.execute(
            "SELECT kind, device_id, details_json, first_seen, best_rssi, seen_count "
            "FROM devices WHERE location_id=? "
            "AND kind IN ('bluetooth','bluetooth_classic') "
            "AND lower(device_id)=?",
            (loc_id, mac_l),
        ) as cur2:
            rows = await cur2.fetchall()
        ble_row = next((r for r in rows if r[0] == "bluetooth"), None)
        classic_row = next((r for r in rows if r[0] == "bluetooth_classic"), None)
        if not (ble_row and classic_row):
            continue
        try:
            ble_details = json.loads(ble_row[2] or "{}")
        except (TypeError, ValueError):
            ble_details = {}
        try:
            classic_details = json.loads(classic_row[2] or "{}")
        except (TypeError, ValueError):
            classic_details = {}
        folded = {k: v for k, v in {
            "device_class": classic_details.get("device_class"),
            "device_class_label": classic_details.get("device_class_label"),
            "paired": classic_details.get("paired"),
            "connected": classic_details.get("connected"),
        }.items() if v is not None}
        if folded:
            prior = dict(ble_details.get("classic") or {})
            prior.update(folded)
            ble_details["classic"] = prior
        if not ble_details.get("name") and classic_details.get("name"):
            ble_details["name"] = classic_details["name"]
        if not ble_details.get("vendor") and classic_details.get("vendor"):
            ble_details["vendor"] = classic_details["vendor"]
        merged_first = min([s for s in (ble_row[3], classic_row[3]) if s])
        merged_best = max([s for s in (ble_row[4], classic_row[4]) if s is not None])
        merged_seen = (ble_row[5] or 0) + (classic_row[5] or 0)
        payload = json.dumps(ble_details, default=str)
        sig = compute_ble_signature("bluetooth", ble_details)
        await db.execute(
            "DELETE FROM devices WHERE location_id=? AND kind='bluetooth_classic' "
            "AND lower(device_id)=?",
            (loc_id, mac_l),
        )
        await db.execute(
            "UPDATE devices SET device_id=?, first_seen=?, best_rssi=?, "
            "seen_count=?, details_json=?, signature=? "
            "WHERE location_id=? AND kind='bluetooth' AND lower(device_id)=?",
            (mac, merged_first, merged_best, merged_seen, payload, sig,
             loc_id, mac_l),
        )
        await db.execute(
            "UPDATE observations SET kind='bluetooth', device_id=? "
            "WHERE location_id=? AND kind='bluetooth_classic' AND lower(device_id)=?",
            (mac, loc_id, mac_l),
        )
        await db.execute(
            "UPDATE alert_events SET device_kind='bluetooth', device_id=? "
            "WHERE location_id=? AND device_kind='bluetooth_classic' AND lower(device_id)=?",
            (mac, loc_id, mac_l),
        )

    # Uppercase the surviving rows. Handle the (rare) within-kind case
    # duplicate by absorbing the lowercase row into its uppercase twin.
    async with db.execute(
        "SELECT location_id, kind, device_id, details_json, first_seen, "
        "       best_rssi, seen_count "
        "FROM devices WHERE kind IN ('bluetooth','bluetooth_classic') "
        "AND device_id <> upper(device_id)"
    ) as cur:
        needs_upper = await cur.fetchall()
    for loc_id, k, did, raw, first_seen, best_rssi, seen_count in needs_upper:
        upper_id = did.upper()
        async with db.execute(
            "SELECT first_seen, best_rssi, seen_count, details_json "
            "FROM devices WHERE location_id=? AND kind=? AND device_id=?",
            (loc_id, k, upper_id),
        ) as cur2:
            twin = await cur2.fetchone()
        if twin is None:
            await db.execute(
                "UPDATE devices SET device_id=? "
                "WHERE location_id=? AND kind=? AND device_id=?",
                (upper_id, loc_id, k, did),
            )
        else:
            try:
                d_lower = json.loads(raw or "{}")
            except (TypeError, ValueError):
                d_lower = {}
            try:
                d_upper = json.loads(twin[3] or "{}")
            except (TypeError, ValueError):
                d_upper = {}
            merged_d = {**d_lower, **d_upper}
            merged_first2 = min([s for s in (first_seen, twin[0]) if s])
            merged_best2 = max([s for s in (best_rssi, twin[1]) if s is not None])
            merged_seen2 = (seen_count or 0) + (twin[2] or 0)
            payload2 = json.dumps(merged_d, default=str)
            sig2 = compute_ble_signature(k, merged_d)
            await db.execute(
                "UPDATE devices SET first_seen=?, best_rssi=?, seen_count=?, "
                "details_json=?, signature=? "
                "WHERE location_id=? AND kind=? AND device_id=?",
                (merged_first2, merged_best2, merged_seen2, payload2, sig2,
                 loc_id, k, upper_id),
            )
            await db.execute(
                "DELETE FROM devices WHERE location_id=? AND kind=? AND device_id=?",
                (loc_id, k, did),
            )

    # Related tables don't have a PRIMARY KEY on device_id, so the
    # straight uppercase UPDATE can't collide.
    for table, kind_col in [
        ("observations", "kind"),
        ("alert_events", "device_kind"),
        ("preserved_devices", "kind"),
        ("device_whitelist", "kind"),
        ("device_temp_whitelist", "kind"),
    ]:
        await db.execute(
            f"UPDATE {table} SET device_id = upper(device_id) "
            f"WHERE {kind_col} IN ('bluetooth','bluetooth_classic') "
            f"AND device_id <> upper(device_id)"
        )


# Manufacturer IDs for known commercial trackers. Integer values (Bluetooth
# SIG-assigned company IDs); JSON-serialized manufacturer_data keys come
# back as strings so we coerce on lookup.
_MFG_APPLE = 76      # 0x004C
_MFG_TILE = 1660     # 0x067C
_MFG_SAMSUNG = 117   # 0x0075


def kind_label(kind: str, address_type: Optional[str] = None,
               details: Optional[dict] = None) -> str:
    """Human-friendly label for a device kind. BLE devices get split by
    address_type since the scanner is BLE-only and the public/random
    distinction is the most useful breakdown the operator has — public
    addresses are usually fixed-MAC dual-mode peripherals (speakers,
    keyboards), random addresses are modern privacy-mode BLE (phones,
    AirTags). A dual-mode device whose Classic side has been merged
    in carries its CoD label under details['classic']. Mirrors
    formatKindLabel() in static/app.js."""
    if kind == "wifi":
        return "WiFi AP"
    if kind == "wifi_client":
        return "WiFi client (probe)"
    if kind == "bluetooth":
        at = (address_type or "").lower()
        base = "BLE (public)" if at == "public" else ("BLE (random)" if at == "random" else "BLE")
        classic = (details or {}).get("classic") if isinstance(details, dict) else None
        if classic:
            sub = classic.get("device_class_label") if isinstance(classic, dict) else None
            return f"{base} + Classic ({sub})" if sub else f"{base} + Classic"
        return base
    if kind == "bluetooth_classic":
        sub = (details or {}).get("device_class_label") if isinstance(details, dict) else None
        return f"Bluetooth Classic ({sub})" if sub else "Bluetooth Classic"
    return kind or ""


def classify_tracker(kind: str, details: dict) -> Optional[str]:
    """Identify known commercial trackers from BLE advertising data.

    Returns one of: 'airtag' (or third-party FindMy item), 'tile',
    'samsung_smarttag' — or None if the device doesn't match any of
    the patterns. Detection is based on protocol-level signals that
    survive the OUI/vendor lookup (which only tells us the chipset
    manufacturer)."""
    if kind != "bluetooth" or not isinstance(details, dict):
        return None

    svc_uuids = [str(u).lower() for u in (details.get("service_uuids") or [])]
    mfg = details.get("manufacturer_data") or {}
    if not isinstance(mfg, dict):
        mfg = {}

    def _has_mfg(target_id: int) -> Optional[str]:
        """Return the hex payload for `target_id`, or None."""
        for k, v in mfg.items():
            try:
                if int(k) == target_id:
                    return v if isinstance(v, str) else None
            except (TypeError, ValueError):
                continue
        return None

    # Tile — service UUID 0xFEED or company ID 0x067C
    if any(u.startswith("feed") or u.endswith("feed") or "0000feed" in u for u in svc_uuids):
        return "tile"
    if _has_mfg(_MFG_TILE) is not None:
        return "tile"

    # Samsung SmartTag — service UUID 0xFD5A (FindMyMobile)
    if any("fd5a" in u for u in svc_uuids):
        return "samsung_smarttag"

    # Apple FindMy / AirTag — manufacturer 0x004C, payload type byte 0x12
    apple_payload = _has_mfg(_MFG_APPLE)
    if apple_payload:
        # Hex string; the first byte is the Continuity message type. 0x12
        # = "FindMy" (AirTag, AirTag-compatible third-party items, "lost"
        # AirPods cases broadcasting in offline-finding mode).
        if apple_payload.lower().startswith("12"):
            return "airtag"

    return None


def compute_ble_signature(kind: str, details: dict) -> Optional[str]:
    """Stable identity hash for a BLE device, derived from durable bits of
    its advertising data. Used to link rotating private MAC addresses back
    to a single physical device (we don't have the device's IRK, so this
    is heuristic — but the durable parts of the adv payload survive MAC
    rotation for the vast majority of non-Apple BLE devices).

    Returns None for: non-BLE rows; public-address BLE (already stable);
    or BLE with too little fingerprintable data (single-source matches
    are too noisy to be useful)."""
    if kind != "bluetooth":
        return None
    if not isinstance(details, dict):
        return None
    # Public addresses don't rotate — no fingerprint needed.
    if details.get("address_type") == "public":
        return None

    parts: list[str] = []

    # Service UUIDs are usually stable across rotations — sort for hash stability.
    svc_uuids = details.get("service_uuids") or []
    if isinstance(svc_uuids, list) and svc_uuids:
        parts.append("svc:" + ",".join(sorted(str(u).lower() for u in svc_uuids)))

    # Manufacturer-data: the company-id KEYS are stable; values often rotate
    # (Apple Continuity counters, etc.), so we hash only the keys.
    mfg_data = details.get("manufacturer_data") or {}
    if isinstance(mfg_data, dict) and mfg_data:
        parts.append("mfg:" + ",".join(sorted(str(k) for k in mfg_data.keys())))

    # Service-data UUID keys are stable; values can rotate.
    svc_data = details.get("service_data") or {}
    if isinstance(svc_data, dict) and svc_data:
        parts.append("svd:" + ",".join(sorted(str(k).lower() for k in svc_data.keys())))

    # Appearance — 16-bit GAP code, stable per device class (e.g. 833 = HID
    # mouse). Useful as a tiebreaker, not strong on its own.
    appearance = details.get("appearance")
    if appearance is not None:
        parts.append(f"app:{appearance}")

    # Name — only when reasonably specific.
    name = (details.get("name") or "").strip()
    if name and len(name) >= 3:
        parts.append(f"nm:{name}")

    # Need at least 2 distinct fingerprint sources to avoid false-positive
    # clustering. A bare "mfg:0x004C" (Apple) by itself would lump every
    # Apple device on the planet into one signature.
    if len(parts) < 2:
        return None

    return hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()[:16]


async def get_setting(key: str) -> Optional[str]:
    async with _connect() as db:
        async with db.execute("SELECT value FROM settings_kv WHERE key=?", (key,)) as cur:
            row = await cur.fetchone()
            return row[0] if row else None


async def set_setting(key: str, value: str) -> None:
    async with _connect() as db:
        await db.execute(
            "INSERT INTO settings_kv(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
        await db.commit()


async def create_location(lat: float, lon: float, radius_m: float, label: str | None,
                          source: str = "auto") -> int:
    now = datetime.now().isoformat()
    # Manual (drawn) locations start with fix_count=0; they're geofences,
    # not auto-clusters that always have at least the opening fix.
    fix_count = 0 if source == "manual" else 1
    # Same 2-dp rounding LocationManager.effective_radius_m applies to
    # auto-cluster radii. Applied here defensively so manual-draw and
    # any future caller bypassing the location manager also lands at
    # consistent precision.
    radius_m = round(float(radius_m), 2)
    async with _connect() as db:
        cur = await db.execute(
            "INSERT INTO sensor_locations(lat,lon,radius_m,created_at,last_seen_at,"
            "label,fix_count,source) VALUES(?,?,?,?,?,?,?,?)",
            (lat, lon, radius_m, now, now, label, fix_count, source),
        )
        await db.commit()
        return cur.lastrowid


async def touch_location(location_id: int) -> None:
    now = datetime.now().isoformat()
    async with _connect() as db:
        await db.execute(
            "UPDATE sensor_locations SET last_seen_at=?, fix_count=fix_count+1 WHERE id=?",
            (now, location_id),
        )
        await db.commit()


async def list_location_centroids() -> list[dict]:
    """Lightweight: id/lat/lon/radius for every location. Used by the
    location manager to test 'am I inside an existing radius'."""
    async with _connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT id, lat, lon, radius_m, source FROM sensor_locations"
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]


async def list_locations() -> list[dict]:
    sql = """
        SELECT l.*,
            COALESCE(SUM(CASE WHEN d.kind = 'wifi'        THEN 1 ELSE 0 END), 0) AS wifi_count,
            COALESCE(SUM(CASE WHEN d.kind = 'bluetooth'   THEN 1 ELSE 0 END), 0) AS bt_count,
            COALESCE(SUM(CASE WHEN d.kind = 'wifi_client' THEN 1 ELSE 0 END), 0) AS wifi_client_count,
            COALESCE(SUM(d.seen_count), 0) AS total_observations
        FROM sensor_locations l
        LEFT JOIN devices d ON d.location_id = l.id
        GROUP BY l.id
        ORDER BY l.id DESC
    """
    async with _connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(sql) as cur:
            return [dict(r) for r in await cur.fetchall()]


def _match_whitelist(wl: list[tuple[str, str]], kind: str, device_id: str) -> bool:
    """Mirror of services.alert_service.is_whitelisted: a row matches when
    its (kind, target) pair has the same kind, and the device_id either
    equals or starts with the target (target acts as a prefix)."""
    d = (device_id or "").lower()
    return any(k == kind and t and (d == t or d.startswith(t)) for k, t in wl)


async def _preserve_whitelisted_devices(db, location_ids: list[int]) -> int:
    """Copy whitelisted devices in `location_ids` into preserved_devices,
    combining stats on collision. Caller owns the transaction. Honors
    BOTH the permanent and temporary whitelists — temp-whitelisted
    devices behave like permanent for the lifetime of their entry.
    Returns the number of device rows preserved."""
    if not location_ids:
        return 0
    async with db.execute(
        "SELECT kind, device_id FROM device_whitelist "
        "UNION SELECT kind, device_id FROM device_temp_whitelist"
    ) as cur:
        wl = [(r[0], (r[1] or "").lower()) for r in await cur.fetchall()]
    if not wl:
        return 0

    placeholders = ",".join("?" * len(location_ids))
    async with db.execute(
        f"SELECT location_id, kind, device_id, first_seen, last_seen, "
        f"best_rssi, last_rssi, seen_count, details_json "
        f"FROM devices WHERE location_id IN ({placeholders})",
        tuple(location_ids),
    ) as cur:
        rows = await cur.fetchall()

    now = datetime.now().isoformat()
    preserved = 0
    for loc_id, k, did, first_seen, last_seen, best_rssi, last_rssi, seen, details in rows:
        if not _match_whitelist(wl, k, did):
            continue
        async with db.execute(
            "SELECT first_seen, last_seen, best_rssi, last_rssi, seen_count, details_json "
            "FROM preserved_devices WHERE kind=? AND device_id=?",
            (k, did),
        ) as c:
            existing = await c.fetchone()
        if existing is None:
            await db.execute(
                "INSERT INTO preserved_devices(kind, device_id, first_seen, last_seen, "
                "best_rssi, last_rssi, seen_count, details_json, "
                "archived_from_location_id, archived_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (k, did, first_seen, last_seen, best_rssi, last_rssi, seen,
                 details, loc_id, now),
            )
        else:
            e_first, e_last, e_best, e_last_rssi, e_seen, e_details = existing
            merged_best = max(best_rssi, e_best)
            merged_first = min(first_seen, e_first)
            merged_last = max(last_seen, e_last)
            merged_last_rssi = last_rssi if last_seen >= e_last else e_last_rssi
            merged_details = details if last_seen >= e_last else e_details
            merged_seen = (seen or 0) + (e_seen or 0)
            await db.execute(
                "UPDATE preserved_devices SET first_seen=?, last_seen=?, "
                "best_rssi=?, last_rssi=?, seen_count=?, details_json=?, "
                "archived_from_location_id=?, archived_at=? "
                "WHERE kind=? AND device_id=?",
                (merged_first, merged_last, merged_best, merged_last_rssi,
                 merged_seen, merged_details, loc_id, now, k, did),
            )
        preserved += 1
    return preserved


async def delete_devices_at_location(location_id: int) -> dict:
    """Wipe every device and observation row tied to this location while
    keeping the sensor_location itself. Whitelisted devices get archived
    into preserved_devices first, same as delete_location, so their
    historical data survives the cleanup."""
    async with _connect() as db:
        async with db.execute(
            "SELECT 1 FROM sensor_locations WHERE id=?", (location_id,)
        ) as cur:
            if await cur.fetchone() is None:
                return {"devices": 0, "observations": 0, "preserved": 0,
                        "found": False}
        async with db.execute(
            "SELECT COUNT(*) FROM devices WHERE location_id=?", (location_id,)
        ) as cur:
            n_dev = (await cur.fetchone())[0]
        async with db.execute(
            "SELECT COUNT(*) FROM observations WHERE location_id=?", (location_id,)
        ) as cur:
            n_obs = (await cur.fetchone())[0]
        n_preserved = await _preserve_whitelisted_devices(db, [location_id])
        await db.execute("DELETE FROM observations WHERE location_id=?", (location_id,))
        await db.execute("DELETE FROM devices WHERE location_id=?", (location_id,))
        await db.commit()
    return {"devices": n_dev, "observations": n_obs,
            "preserved": n_preserved, "found": True}


async def delete_location(location_id: int) -> dict:
    """Delete one sensor location and its devices + observations. Whitelisted
    devices' sightings get copied to preserved_devices first so they survive
    the cascade. Returns the row counts removed (0 across the board if the
    location didn't exist)."""
    async with _connect() as db:
        async with db.execute(
            "SELECT 1 FROM sensor_locations WHERE id=?", (location_id,)
        ) as cur:
            if await cur.fetchone() is None:
                return {"locations": 0, "devices": 0, "observations": 0, "preserved": 0}
        async with db.execute(
            "SELECT COUNT(*) FROM devices WHERE location_id=?", (location_id,)
        ) as cur:
            n_dev = (await cur.fetchone())[0]
        async with db.execute(
            "SELECT COUNT(*) FROM observations WHERE location_id=?", (location_id,)
        ) as cur:
            n_obs = (await cur.fetchone())[0]
        n_preserved = await _preserve_whitelisted_devices(db, [location_id])
        await db.execute("DELETE FROM observations WHERE location_id=?", (location_id,))
        await db.execute("DELETE FROM devices WHERE location_id=?", (location_id,))
        await db.execute("DELETE FROM sensor_locations WHERE id=?", (location_id,))
        await db.commit()
    return {
        "locations": 1, "devices": n_dev, "observations": n_obs,
        "preserved": n_preserved,
    }


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in metres. Inlined here so database.py doesn't
    need to import services.location_manager (circular)."""
    R = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


# ── Merge-contained tunables ─────────────────────────────────────────────
# Fraction of the LOSER's area that must lie inside the WINNER's circle for
# the pair to qualify as "contained enough to merge". 0.5 = at least half
# the loser's bubble overlaps the winner. Stricter than the old "centroid
# inside radius" rule, which could trigger on as little as ~5% overlap when
# the loser was much larger than the winner.
MERGE_OVERLAP_THRESHOLD = 0.5
# When merging into an auto winner, cap how much the winner's radius can
# grow to engulf the loser. 1.10 = at most 10%. Manual winners never grow.
MERGE_GROW_CAP_FACTOR = 1.10


def _circle_overlap_area(r1: float, r2: float, d: float) -> float:
    """Lens-area of two circles with radii r1, r2 separated by centre
    distance d. 0 when disjoint; π·min(r1,r2)² when one fully contains
    the other."""
    if r1 <= 0 or r2 <= 0:
        return 0.0
    if d >= r1 + r2:
        return 0.0
    if d <= abs(r1 - r2):
        return math.pi * min(r1, r2) ** 2
    r1_sq, r2_sq, d_sq = r1 * r1, r2 * r2, d * d
    a1 = r1_sq * math.acos((d_sq + r1_sq - r2_sq) / (2 * d * r1))
    a2 = r2_sq * math.acos((d_sq + r2_sq - r1_sq) / (2 * d * r2))
    triangle = 0.5 * math.sqrt(
        (-d + r1 + r2) * (d + r1 - r2) * (d - r1 + r2) * (d + r1 + r2)
    )
    return a1 + a2 - triangle


def _loser_overlap_ratio(loser_radius: float, winner_radius: float, distance_m: float) -> float:
    """Fraction of the LOSER's area that lies inside the WINNER's circle.
    Returns a value in [0, 1]."""
    if loser_radius <= 0:
        return 0.0
    overlap = _circle_overlap_area(loser_radius, winner_radius, distance_m)
    return min(1.0, overlap / (math.pi * loser_radius ** 2))


async def find_contained_locations() -> list[dict]:
    """Return every (loser, winner) pair where at least
    MERGE_OVERLAP_THRESHOLD of the loser's circle area lies inside the
    winner's circle. Manual (drawn) locations are never losers: drawn
    geofences are user-authoritative and always survive. Pairs are sorted
    so manual winners absorb first, then smallest loser into largest
    winner."""
    locs = await list_locations()
    pairs: list[dict] = []
    for a in locs:
        if a.get("source") == "manual":
            continue  # manual locations never get merged away
        for b in locs:
            if a["id"] == b["id"]:
                continue
            d = _haversine_m(a["lat"], a["lon"], b["lat"], b["lon"])
            ratio = _loser_overlap_ratio(a["radius_m"], b["radius_m"], d)
            if ratio >= MERGE_OVERLAP_THRESHOLD:
                pairs.append({
                    "loser_id": a["id"],
                    "winner_id": b["id"],
                    "distance_m": d,
                    "loser_radius": a["radius_m"],
                    "winner_radius": b["radius_m"],
                    "loser_fix_count": a.get("fix_count", 0),
                    "winner_fix_count": b.get("fix_count", 0),
                    "loser_source": a.get("source", "auto"),
                    "winner_source": b.get("source", "auto"),
                    "overlap_ratio": ratio,
                })
    pairs.sort(key=lambda p: (
        # Prefer pairs whose winner is a drawn geofence — that way a manual
        # circle absorbs everything inside it before two auto-clusters try
        # to merge with each other.
        0 if p["winner_source"] == "manual" else 1,
        -p["overlap_ratio"],          # most-overlapping pair first
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

    async with _connect() as db:
        # Sanity: both rows must exist, otherwise return a no-op shape.
        async with db.execute(
            "SELECT id, lat, lon, radius_m, fix_count, last_seen_at, source "
            "FROM sensor_locations WHERE id IN (?,?)",
            (loser_id, winner_id),
        ) as cur:
            rows = {r[0]: r for r in await cur.fetchall()}
        if loser_id not in rows or winner_id not in rows:
            counts["noop"] = True
            return counts

        l_id, l_lat, l_lon, l_radius, l_fix_count, l_last_seen, l_source = rows[loser_id]
        w_id, w_lat, w_lon, w_radius, w_fix_count, w_last_seen, w_source = rows[winner_id]
        counts["winner_radius_before"] = w_radius
        # A manual location must never be a loser — protect against being
        # called directly with the wrong direction.
        if l_source == "manual":
            counts["noop"] = True
            counts["error"] = "manual locations cannot be merged away"
            return counts

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

        # 5. Update winner aggregates. For auto winners, expand radius to
        # engulf the loser if it wasn't already inside, but cap growth at
        # MERGE_GROW_CAP_FACTOR (10%) so a single merge can't blow up an
        # auto-cluster. The loser's outer rim may stick out of the post-
        # merge winner; that's the trade for keeping bubble sizes sane.
        # Manual winners keep the user-drawn radius — geofence size is
        # intentional. Same 2-dp rounding LocationManager.effective_radius_m
        # uses on creation, so merged radii match the precision of fresh
        # bubbles.
        if w_source == "manual":
            new_radius = w_radius
        else:
            dist = _haversine_m(w_lat, w_lon, l_lat, l_lon)
            needed = max(w_radius, dist + l_radius)
            cap = w_radius * MERGE_GROW_CAP_FACTOR
            new_radius = round(min(needed, cap), 2)
        new_fix_count = (w_fix_count or 0) + (l_fix_count or 0)
        new_last_seen = max(w_last_seen or "", l_last_seen or "") or w_last_seen
        await db.execute(
            "UPDATE sensor_locations "
            "SET radius_m=?, fix_count=?, last_seen_at=? WHERE id=?",
            (new_radius, new_fix_count, new_last_seen, winner_id),
        )
        counts["winner_radius_after"] = new_radius
        counts["winner_source"] = w_source

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


async def delete_auto_locations() -> dict:
    """Reset: delete every auto-clustered sensor location and its
    devices/observations while leaving drawn geofences (source='manual')
    in place. Whitelisted devices get archived into preserved_devices
    first, same as delete_all_locations does. Temp whitelist is left
    intact — Reset is a softer action than Delete all."""
    async with _connect() as db:
        async with db.execute(
            "SELECT id FROM sensor_locations WHERE source='auto'"
        ) as cur:
            ids = [r[0] for r in await cur.fetchall()]
        if not ids:
            return {"locations": 0, "devices": 0, "observations": 0, "preserved": 0}

        ph = ",".join("?" * len(ids))
        async with db.execute(
            f"SELECT COUNT(*) FROM devices WHERE location_id IN ({ph})", ids,
        ) as cur:
            n_dev = (await cur.fetchone())[0]
        async with db.execute(
            f"SELECT COUNT(*) FROM observations WHERE location_id IN ({ph})", ids,
        ) as cur:
            n_obs = (await cur.fetchone())[0]
        n_preserved = await _preserve_whitelisted_devices(db, ids)

        await db.execute(
            f"DELETE FROM observations WHERE location_id IN ({ph})", ids,
        )
        await db.execute(
            f"DELETE FROM devices WHERE location_id IN ({ph})", ids,
        )
        await db.execute(
            f"DELETE FROM sensor_locations WHERE id IN ({ph})", ids,
        )
        await db.commit()
    return {
        "locations": len(ids), "devices": n_dev, "observations": n_obs,
        "preserved": n_preserved,
    }


async def delete_all_locations() -> dict:
    """Delete every sensor location and all associated devices/observations.
    Whitelisted devices get archived into preserved_devices first. Also
    wipes the temporary whitelist — the baseline-scan temp entries are
    session-scoped to the current location set, so a full reset clears
    them too.
    Returns the row counts that were removed."""
    async with _connect() as db:
        async with db.execute("SELECT COUNT(*) FROM sensor_locations") as cur:
            n_loc = (await cur.fetchone())[0]
        async with db.execute("SELECT COUNT(*) FROM devices") as cur:
            n_dev = (await cur.fetchone())[0]
        async with db.execute("SELECT COUNT(*) FROM observations") as cur:
            n_obs = (await cur.fetchone())[0]
        async with db.execute("SELECT id FROM sensor_locations") as cur:
            all_ids = [r[0] for r in await cur.fetchall()]
        n_preserved = await _preserve_whitelisted_devices(db, all_ids)
        async with db.execute("SELECT COUNT(*) FROM device_temp_whitelist") as cur:
            n_temp = (await cur.fetchone())[0]
        await db.execute("DELETE FROM observations")
        await db.execute("DELETE FROM devices")
        await db.execute("DELETE FROM sensor_locations")
        await db.execute("DELETE FROM device_temp_whitelist")
        # Reset AUTOINCREMENT counters so new ids start from 1
        await db.execute(
            "DELETE FROM sqlite_sequence WHERE name IN ('sensor_locations','observations')"
        )
        await db.commit()
    return {
        "locations": n_loc, "devices": n_dev, "observations": n_obs,
        "preserved": n_preserved, "temp_whitelist_cleared": n_temp,
    }


async def update_location_label(location_id: int, label: str) -> None:
    async with _connect() as db:
        await db.execute(
            "UPDATE sensor_locations SET label=? WHERE id=?", (label, location_id)
        )
        await db.commit()


async def update_location_radius(location_id: int, radius_m: float) -> bool:
    """Resize a manual (drawn) geofence. Auto-cluster locations are skipped
    — their radius is governed by the clustering tunables; letting the user
    nudge it from the map UI would just be overwritten on the next fix.
    Returns True if a row was updated, False if the id was missing OR the
    row was auto-sourced."""
    radius_m = round(float(radius_m), 2)
    async with _connect() as db:
        cur = await db.execute(
            "UPDATE sensor_locations SET radius_m=? "
            "WHERE id=? AND source='manual'",
            (radius_m, location_id),
        )
        await db.commit()
        return (cur.rowcount or 0) > 0


def _classic_subset(d: dict) -> dict:
    """Pull just the Classic-only fields out of a Classic-shaped details
    dict. Used by upsert_bluetooth when folding Classic data into a BLE
    row's `classic` sub-dict."""
    if not isinstance(d, dict):
        return {}
    return {k: v for k, v in {
        "device_class": d.get("device_class"),
        "device_class_label": d.get("device_class_label"),
        "paired": d.get("paired"),
        "connected": d.get("connected"),
    }.items() if v is not None}


async def upsert_bluetooth(
    location_id: int,
    kind: str,
    device_id: str,
    rssi: int,
    details: dict,
) -> tuple[bool, str]:
    """Upsert a Bluetooth device row with BLE ↔ Classic merge.

    When both a BLE and a Classic sighting exist for the same MAC at the
    same location, they're collapsed into a single BLE row — Classic-only
    fields (device_class / device_class_label / paired / connected) move
    under details['classic'] so the major-class label and pair state
    still surface on the device card. MACs are normalized to uppercase
    on the way in, regardless of the case the scanner produced.

    Returns (is_new, effective_kind). effective_kind is the kind of the
    row that was actually written — 'bluetooth' whenever any BLE data
    is present (existing or incoming), else 'bluetooth_classic'."""
    address = (device_id or "").upper()
    address_l = address.lower()
    is_classic_input = (kind == "bluetooth_classic")
    now = datetime.now().isoformat()

    async with _connect() as db:
        async with db.execute(
            "SELECT kind, device_id, details_json, first_seen, best_rssi, seen_count "
            "FROM devices WHERE location_id=? AND "
            "kind IN ('bluetooth','bluetooth_classic') AND lower(device_id)=?",
            (location_id, address_l),
        ) as cur:
            rows = await cur.fetchall()

        ble_existing = next((r for r in rows if r[0] == "bluetooth"), None)
        classic_existing = next((r for r in rows if r[0] == "bluetooth_classic"), None)
        # BLE wins whenever BLE evidence exists — either a prior BLE row
        # or this upsert being BLE-flavored.
        target_kind = "bluetooth" if (ble_existing or not is_classic_input) else "bluetooth_classic"

        def _parse(row):
            try:
                return json.loads(row[2] or "{}")
            except (TypeError, ValueError):
                return {}

        target_row = ble_existing if target_kind == "bluetooth" else classic_existing
        merged = _parse(target_row) if target_row else {}

        if target_kind == "bluetooth_classic":
            # Pure Classic path — no BLE data anywhere. Overlay incoming
            # details on top of any prior classic details.
            merged = {**merged, **details}
        else:
            if is_classic_input:
                # Folding a Classic sighting into a BLE row. Classic
                # fields nest under "classic"; name/vendor fall back to
                # Classic only when BLE didn't already provide them.
                prior_classic = dict(merged.get("classic") or {})
                prior_classic.update(_classic_subset(details))
                merged["classic"] = prior_classic
                if not merged.get("name") and details.get("name"):
                    merged["name"] = details["name"]
                if not merged.get("vendor") and details.get("vendor"):
                    merged["vendor"] = details["vendor"]
            else:
                # BLE upsert. Top-level fields come from the scan; any
                # existing classic sub-dict survives.
                prior_classic = merged.get("classic")
                merged = {**merged, **details}
                if prior_classic and "classic" not in details:
                    merged["classic"] = prior_classic
                # First-time promotion: a Classic row exists but no BLE
                # row did. Pull Classic fields into the new BLE row's
                # classic sub-dict before we delete the Classic row.
                if classic_existing and not ble_existing:
                    folded = _classic_subset(_parse(classic_existing))
                    if folded:
                        prev = dict(merged.get("classic") or {})
                        prev.update(folded)
                        merged["classic"] = prev

        first_seen_candidates = [r[3] for r in rows if r[3]]
        first_seen_candidates.append(now)
        merged_first = min(first_seen_candidates)
        best_candidates = [r[4] for r in rows if r[4] is not None]
        best_candidates.append(rssi)
        merged_best = max(best_candidates)
        merged_seen = sum((r[5] or 0) for r in rows) + 1

        signature = compute_ble_signature(target_kind, merged)
        payload = json.dumps(merged, default=str)

        # When promoting to BLE, drop the Classic row and repoint its
        # historical observations + alert events so they hang off the
        # surviving (merged) row.
        if target_kind == "bluetooth" and classic_existing:
            await db.execute(
                "DELETE FROM devices WHERE location_id=? AND kind='bluetooth_classic' "
                "AND lower(device_id)=?",
                (location_id, address_l),
            )
            await db.execute(
                "UPDATE observations SET kind='bluetooth', device_id=? "
                "WHERE location_id=? AND kind='bluetooth_classic' AND lower(device_id)=?",
                (address, location_id, address_l),
            )
            await db.execute(
                "UPDATE alert_events SET device_kind='bluetooth', device_id=? "
                "WHERE location_id=? AND device_kind='bluetooth_classic' AND lower(device_id)=?",
                (address, location_id, address_l),
            )

        survivor = ble_existing if target_kind == "bluetooth" else classic_existing
        is_new = survivor is None
        if is_new:
            await db.execute(
                "INSERT INTO devices(location_id,kind,device_id,first_seen,last_seen,"
                "best_rssi,last_rssi,seen_count,details_json,signature) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                (location_id, target_kind, address, merged_first, now,
                 merged_best, rssi, merged_seen, payload, signature),
            )
        else:
            await db.execute(
                "UPDATE devices SET device_id=?, first_seen=?, last_seen=?, "
                "best_rssi=?, last_rssi=?, seen_count=?, details_json=?, signature=? "
                "WHERE location_id=? AND kind=? AND device_id=?",
                (address, merged_first, now, merged_best, rssi, merged_seen,
                 payload, signature, location_id, target_kind, survivor[1]),
            )
        await db.commit()

    return is_new, target_kind


async def upsert_device(
    location_id: int,
    kind: str,
    device_id: str,
    rssi: int,
    details: dict,
) -> bool:
    """Upsert a device row. Returns True if a new row was inserted, False if updated."""
    now = datetime.now().isoformat()
    payload = json.dumps(details, default=str)
    signature = compute_ble_signature(kind, details)
    async with _connect() as db:
        async with db.execute(
            "SELECT best_rssi, seen_count FROM devices WHERE location_id=? AND kind=? AND device_id=?",
            (location_id, kind, device_id),
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            await db.execute(
                "INSERT INTO devices(location_id,kind,device_id,first_seen,last_seen,"
                "best_rssi,last_rssi,seen_count,details_json,signature) "
                "VALUES(?,?,?,?,?,?,?,1,?,?)",
                (location_id, kind, device_id, now, now, rssi, rssi, payload, signature),
            )
            await db.commit()
            return True
        best = max(row[0], rssi)  # rssi is negative — higher is better
        await db.execute(
            "UPDATE devices SET last_seen=?, best_rssi=?, last_rssi=?, "
            "seen_count=seen_count+1, details_json=?, signature=? "
            "WHERE location_id=? AND kind=? AND device_id=?",
            (now, best, rssi, payload, signature, location_id, kind, device_id),
        )
        await db.commit()
        return False


async def get_device_details(location_id: int, kind: str, device_id: str) -> dict | None:
    """Return just the details_json (parsed) for one device, or None if absent.
    Cheaper than devices_at_location when you only need to merge with prior."""
    async with _connect() as db:
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


async def get_location_summary(location_id: int) -> dict | None:
    """Compact location info for the Discord enrichment. Returns label,
    lat, lon, radius_m, source — or None if the id is gone."""
    async with _connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT id, label, lat, lon, radius_m, source "
            "FROM sensor_locations WHERE id=?", (location_id,),
        ) as cur:
            row = await cur.fetchone()
    return dict(row) if row else None


async def get_device_summary(kind: str, device_id: str) -> dict | None:
    """Cross-location aggregate for one device. Used by alert dispatch
    to add temporal/lifetime context to Discord embeds."""
    device_id_l = (device_id or "").lower()
    async with _connect() as db:
        async with db.execute(
            "SELECT MIN(first_seen), MAX(last_seen), SUM(seen_count), "
            "MAX(signature), COUNT(DISTINCT location_id) "
            "FROM devices WHERE kind=? AND device_id=?",
            (kind, device_id_l),
        ) as cur:
            row = await cur.fetchone()
    if row is None or row[0] is None:
        return None
    return {
        "first_seen": row[0],
        "last_seen": row[1],
        "seen_count": int(row[2] or 0),
        "signature": row[3],
        "location_count": int(row[4] or 0),
    }


async def get_signature_siblings(signature: str,
                                 exclude_device_id: str = "") -> list[str]:
    """Every distinct MAC sharing a BLE adv-data signature, minus the
    excluded one. Used to surface rotating-MAC aliases in Discord."""
    if not signature:
        return []
    excl = (exclude_device_id or "").lower()
    async with _connect() as db:
        async with db.execute(
            "SELECT DISTINCT device_id FROM devices "
            "WHERE signature=? AND device_id <> ? ORDER BY device_id",
            (signature, excl),
        ) as cur:
            return [r[0] for r in await cur.fetchall()]


async def get_location_created_at(location_id: int) -> str | None:
    async with _connect() as db:
        async with db.execute(
            "SELECT created_at FROM sensor_locations WHERE id=?", (location_id,)
        ) as cur:
            row = await cur.fetchone()
            return row[0] if row else None


async def device_timeline(kind: str, device_id: str, *, max_points: int = 500) -> dict:
    """Per-device history: aggregate device-row stats per location, plus a
    sample of recent observations for the RSSI sparkline. `max_points`
    caps the observation sample so the response stays small even for a
    device with months of sightings."""
    device_id = (device_id or "").lower()
    out: dict = {
        "kind": kind, "device_id": device_id,
        "locations": [], "observations": [],
        "total_observations": 0, "first_seen": None, "last_seen": None,
        "details": {},
    }
    async with _connect() as db:
        db.row_factory = aiosqlite.Row
        # Per-location aggregates from the devices table (one row per loc).
        async with db.execute(
            """
            SELECT d.location_id, d.first_seen, d.last_seen, d.best_rssi,
                   d.last_rssi, d.seen_count, d.details_json,
                   l.label, l.lat AS loc_lat, l.lon AS loc_lon
            FROM devices d
            LEFT JOIN sensor_locations l ON l.id = d.location_id
            WHERE d.kind=? AND d.device_id=?
            ORDER BY d.last_seen DESC
            """,
            (kind, device_id),
        ) as cur:
            for r in await cur.fetchall():
                row = dict(r)
                try:
                    out["details"] = json.loads(row.pop("details_json") or "{}")
                except (TypeError, ValueError):
                    pass
                out["locations"].append({
                    "id": row["location_id"],
                    "label": row.get("label"),
                    "lat": row.get("loc_lat"),
                    "lon": row.get("loc_lon"),
                    "first_seen": row.get("first_seen"),
                    "last_seen": row.get("last_seen"),
                    "best_rssi": row.get("best_rssi"),
                    "last_rssi": row.get("last_rssi"),
                    "seen_count": row.get("seen_count"),
                })
        # Total observation count.
        async with db.execute(
            "SELECT COUNT(*), MIN(seen_at), MAX(seen_at) "
            "FROM observations WHERE kind=? AND device_id=?",
            (kind, device_id),
        ) as cur:
            n, first, last = await cur.fetchone()
        out["total_observations"] = int(n or 0)
        out["first_seen"] = first
        out["last_seen"] = last
        # Recent-observation sample for the sparkline. Newest first; the UI
        # reverses for left-to-right time order. Cheap because of the
        # idx_obs_device index.
        async with db.execute(
            """
            SELECT seen_at, rssi, location_id, lat, lon
            FROM observations
            WHERE kind=? AND device_id=?
            ORDER BY seen_at DESC LIMIT ?
            """,
            (kind, device_id, int(max_points)),
        ) as cur:
            out["observations"] = [dict(r) for r in await cur.fetchall()]
    out["observations"].reverse()  # oldest → newest
    return out


async def insert_observation(
    location_id: int,
    kind: str,
    device_id: str,
    rssi: int,
    lat: float | None,
    lon: float | None,
    raw: dict,
) -> None:
    now = datetime.now().isoformat()
    async with _connect() as db:
        await db.execute(
            "INSERT INTO observations(location_id,kind,device_id,rssi,lat,lon,seen_at,raw_json) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (location_id, kind, device_id, rssi, lat, lon, now, json.dumps(raw, default=str)),
        )
        await db.commit()


# ---------- alerts ----------
async def get_alert_rule(rule_id: int) -> dict | None:
    """Single alert_rule row (with parsed extra_conditions), or None
    if no row matches. Used by the PATCH endpoint to reason about the
    rule's *current* state before applying changes."""
    async with _connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM alert_rules WHERE id=?", (rule_id,),
        ) as cur:
            row = await cur.fetchone()
    if row is None:
        return None
    out = dict(row)
    raw = out.get("extra_conditions") or "[]"
    try:
        out["extra_conditions"] = json.loads(raw)
    except (TypeError, ValueError):
        out["extra_conditions"] = []
    return out


async def location_exists(loc_id: int) -> bool:
    """Quick existence check used by the rule PATCH endpoint to decide
    whether a location_id reference is currently valid."""
    async with _connect() as db:
        async with db.execute(
            "SELECT 1 FROM sensor_locations WHERE id=? LIMIT 1", (loc_id,),
        ) as cur:
            return await cur.fetchone() is not None


async def list_alert_rules() -> list[dict]:
    async with _connect() as db:
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
    latch: bool = True, include_whitelist: bool = False,
) -> int:
    now = datetime.now().isoformat()
    extra_json = json.dumps(extra_conditions or [])
    async with _connect() as db:
        cur = await db.execute(
            "INSERT INTO alert_rules(name,enabled,kind,match_type,match_value,"
            "location_id,notify_discord,audible,extra_conditions,latch,"
            "include_whitelist,created_at) "
            "VALUES(?,1,?,?,?,?,?,?,?,?,?,?)",
            (name, kind, match_type, match_value, location_id,
             1 if notify_discord else 0, 1 if audible else 0,
             extra_json, 1 if latch else 0,
             1 if include_whitelist else 0, now),
        )
        await db.commit()
        return cur.lastrowid


async def update_alert_rule(rule_id: int, fields: dict) -> None:
    if not fields:
        return
    cols = ", ".join(f"{k}=?" for k in fields.keys())
    args = list(fields.values()) + [rule_id]
    async with _connect() as db:
        await db.execute(f"UPDATE alert_rules SET {cols} WHERE id=?", args)
        await db.commit()


async def disable_rules_with_missing_location() -> list[dict]:
    """Scan every alert_rule whose location_id points at a specific
    location (not NULL = 'any', not -1 = 'active') and disable any rule
    whose target no longer exists. Stamps a human-readable reason so the
    UI can surface it and the PATCH endpoint can block re-enabling
    until the rule is fixed.

    Returns the list of rules that were just disabled (each shaped
    {id, name, location_id, disabled_reason}). Already-disabled rules
    that meet the criteria are still updated so disabled_reason stays
    current, but they're not returned (no behaviour change to flag)."""
    async with _connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT r.id, r.name, r.enabled, r.location_id "
            "FROM alert_rules r "
            "WHERE r.location_id IS NOT NULL AND r.location_id != -1 "
            "AND NOT EXISTS ("
            "  SELECT 1 FROM sensor_locations l WHERE l.id = r.location_id"
            ")"
        ) as cur:
            rows = [dict(r) for r in await cur.fetchall()]
        newly_disabled: list[dict] = []
        for r in rows:
            reason = f"location #{r['location_id']} no longer exists"
            was_enabled = bool(r["enabled"])
            await db.execute(
                "UPDATE alert_rules SET enabled=0, disabled_reason=? WHERE id=?",
                (reason, r["id"]),
            )
            if was_enabled:
                newly_disabled.append({
                    "id": r["id"], "name": r["name"],
                    "location_id": r["location_id"],
                    "disabled_reason": reason,
                })
        await db.commit()
    return newly_disabled


async def delete_alert_rule(rule_id: int) -> None:
    async with _connect() as db:
        await db.execute("DELETE FROM alert_events WHERE rule_id=?", (rule_id,))
        await db.execute("DELETE FROM alert_rules WHERE id=?", (rule_id,))
        await db.commit()


async def insert_alert_event(
    rule_id: int, location_id: int | None, device_kind: str, device_id: str,
    rssi: int | None, details: dict,
) -> int:
    now = datetime.now().isoformat()
    async with _connect() as db:
        cur = await db.execute(
            "INSERT INTO alert_events(rule_id,triggered_at,location_id,device_kind,"
            "device_id,rssi,details_json) VALUES(?,?,?,?,?,?,?)",
            (rule_id, now, location_id, device_kind, device_id, rssi, json.dumps(details, default=str)),
        )
        await db.commit()
        return cur.lastrowid


async def list_alert_events(limit: int = 100, since_id: int | None = None) -> list[dict]:
    # Filter out cleared=2 ('dismissed') rows so per-row trash truly
    # hides them from the live feed. They still exist in the DB to
    # back the latch across restart — list_latched_pairs picks them up.
    sql = (
        "SELECT e.*, r.name AS rule_name, r.match_type AS rule_match_type, "
        "       r.audible AS rule_audible, r.latch AS rule_latch "
        "FROM alert_events e LEFT JOIN alert_rules r ON r.id = e.rule_id "
        "WHERE e.cleared <> 2 "
    )
    args: list = []
    if since_id is not None:
        sql += "AND e.id > ? "
        args.append(since_id)
    sql += "ORDER BY e.id DESC LIMIT ?"
    args.append(limit)
    async with _connect() as db:
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
    async with _connect() as db:
        async with db.execute(sql, (kind, device_id, n_locations)) as cur:
            row = await cur.fetchone()
            return row[0] if row else 0


async def count_companion_locations(kind: str, device_id: str,
                                    window_hours: int) -> int:
    """Count distinct locations a device (or any of its BLE-signature
    siblings) was observed at within the last `window_hours`. The
    signature lookup means a phone rotating its private MAC every
    15 minutes still gets counted as one persistent companion rather
    than disappearing into the noise of new MACs.

    Used by the persistent_companion alert rule."""
    if window_hours < 1:
        return 0
    cutoff = (datetime.now() - timedelta(hours=window_hours)).isoformat()
    device_id = (device_id or "").lower()
    async with _connect() as db:
        # Look up the device's signature (BLE only — wifi rows have NULL).
        # If present, expand to every sibling MAC sharing that signature so
        # the count is across the physical device, not just one MAC.
        siblings = [device_id]
        if kind == "bluetooth":
            async with db.execute(
                "SELECT signature FROM devices WHERE kind=? AND device_id=? "
                "AND signature IS NOT NULL LIMIT 1",
                (kind, device_id),
            ) as cur:
                row = await cur.fetchone()
            sig = row[0] if row else None
            if sig:
                async with db.execute(
                    "SELECT DISTINCT device_id FROM devices WHERE signature=?",
                    (sig,),
                ) as cur:
                    siblings = [r[0] for r in await cur.fetchall()] or [device_id]
        placeholders = ",".join("?" * len(siblings))
        sql = (
            f"SELECT COUNT(DISTINCT location_id) FROM observations "
            f"WHERE kind=? AND seen_at >= ? AND device_id IN ({placeholders})"
        )
        async with db.execute(sql, [kind, cutoff, *siblings]) as cur:
            row = await cur.fetchone()
            return row[0] if row else 0


async def _ble_signature_siblings(db, kind: str, device_id_l: str) -> list[str]:
    """Helper: every MAC sharing this device's BLE adv-data signature,
    including itself. Returns just `[device_id_l]` for wifi / unsigned BLE.
    Caller owns the connection."""
    if kind != "bluetooth":
        return [device_id_l]
    async with db.execute(
        "SELECT signature FROM devices WHERE kind=? AND device_id=? "
        "AND signature IS NOT NULL LIMIT 1",
        (kind, device_id_l),
    ) as cur:
        row = await cur.fetchone()
    sig = row[0] if row else None
    if not sig:
        return [device_id_l]
    async with db.execute(
        "SELECT DISTINCT device_id FROM devices WHERE signature=?", (sig,),
    ) as cur:
        sibs = [r[0] for r in await cur.fetchall()]
    return sibs or [device_id_l]


async def first_sightings_at_locations(
    kind: str, device_id: str, location_ids: list[int], since_iso: str,
) -> dict[int, str]:
    """For each location id in `location_ids`, the earliest observation of
    (kind, device_id) at that location with seen_at >= since_iso. BLE
    rotating-MAC siblings are folded in so a private-MAC phone that
    rotates between arrivals still gets counted as one follower.

    Returns a dict of location_id → ISO timestamp of the first sighting
    within the window (locations with no matching observation are absent).
    Used by the co_arrival_transit rule."""
    if not location_ids:
        return {}
    device_id_l = (device_id or "").lower()
    async with _connect() as db:
        siblings = await _ble_signature_siblings(db, kind, device_id_l)
        sib_placeholders = ",".join("?" * len(siblings))
        loc_placeholders = ",".join("?" * len(location_ids))
        sql = (
            f"SELECT location_id, MIN(seen_at) FROM observations "
            f"WHERE kind=? AND seen_at >= ? "
            f"AND device_id IN ({sib_placeholders}) "
            f"AND location_id IN ({loc_placeholders}) "
            f"GROUP BY location_id"
        )
        args = [kind, since_iso, *siblings, *location_ids]
        async with db.execute(sql, args) as cur:
            return {int(r[0]): r[1] for r in await cur.fetchall()}


async def count_novel_locations(
    kind: str, device_id: str, *, window_hours: int, location_max_age_hours: int,
) -> int:
    """Distinct locations the device (or BLE siblings) appeared at within
    the last `window_hours`, restricted to sensor_locations whose
    `created_at` is itself within the last `location_max_age_hours`.

    "Novel" = the location is one you started visiting recently. A device
    that hits ≥N of these is following you through new ground rather than
    being part of the regular furniture of an established area.

    Used by the novel_location_chain rule."""
    if window_hours < 1 or location_max_age_hours < 1:
        return 0
    obs_cutoff = (datetime.now() - timedelta(hours=window_hours)).isoformat()
    loc_cutoff = (datetime.now() - timedelta(hours=location_max_age_hours)).isoformat()
    device_id_l = (device_id or "").lower()
    async with _connect() as db:
        siblings = await _ble_signature_siblings(db, kind, device_id_l)
        sib_placeholders = ",".join("?" * len(siblings))
        sql = (
            f"SELECT COUNT(DISTINCT o.location_id) FROM observations o "
            f"JOIN sensor_locations l ON l.id = o.location_id "
            f"WHERE o.kind=? AND o.seen_at >= ? AND l.created_at >= ? "
            f"AND o.device_id IN ({sib_placeholders})"
        )
        args = [kind, obs_cutoff, loc_cutoff, *siblings]
        async with db.execute(sql, args) as cur:
            row = await cur.fetchone()
            return int(row[0]) if row else 0


async def count_signature_macs(
    kind: str, device_id: str, *, window_hours: int,
) -> int:
    """How many distinct MACs share this device's BLE adv-data signature
    and were seen within the last `window_hours`. A non-BLE device or a
    BLE device with no signature always returns 1 (itself).

    A burst of new private-address MACs sharing one signature is the
    classic shape of a rotation-mode follower. Used by the
    mac_rotation_rate rule."""
    if window_hours < 1:
        return 1
    cutoff = (datetime.now() - timedelta(hours=window_hours)).isoformat()
    device_id_l = (device_id or "").lower()
    if kind != "bluetooth":
        return 1
    async with _connect() as db:
        async with db.execute(
            "SELECT signature FROM devices WHERE kind=? AND device_id=? "
            "AND signature IS NOT NULL LIMIT 1",
            (kind, device_id_l),
        ) as cur:
            row = await cur.fetchone()
        sig = row[0] if row else None
        if not sig:
            return 1
        async with db.execute(
            "SELECT COUNT(DISTINCT o.device_id) FROM observations o "
            "JOIN devices d ON d.kind = o.kind AND d.device_id = o.device_id "
            "WHERE o.kind='bluetooth' AND o.seen_at >= ? AND d.signature = ?",
            (cutoff, sig),
        ) as cur:
            row = await cur.fetchone()
            return int(row[0]) if row else 1


async def find_cross_kind_partner(
    kind: str, device_id: str, *, window_hours: int, min_overlap: int,
) -> dict | None:
    """Find the strongest cross-kind partner — a device of a different
    kind co-observed at ≥ `min_overlap` of this device's locations within
    the last `window_hours`. Useful for catching a follower that's running
    both a phone (BLE) and a WiFi-broadcasting accessory (e.g. a hotspot
    or laptop) — neither alone might trip the persistent_companion
    threshold, but together they confirm a single carrier.

    Returns `{"kind", "device_id", "overlap"}` for the best partner, or
    None when nothing meets the threshold. Used by cross_kind_co_travel.
    """
    if window_hours < 1 or min_overlap < 1:
        return None
    cutoff = (datetime.now() - timedelta(hours=window_hours)).isoformat()
    device_id_l = (device_id or "").lower()
    async with _connect() as db:
        siblings = await _ble_signature_siblings(db, kind, device_id_l)
        sib_placeholders = ",".join("?" * len(siblings))
        # Step 1: locations where THIS device (or BLE siblings) appeared
        # within the window.
        async with db.execute(
            f"SELECT DISTINCT location_id FROM observations "
            f"WHERE kind=? AND seen_at >= ? AND device_id IN ({sib_placeholders})",
            [kind, cutoff, *siblings],
        ) as cur:
            my_locs = [int(r[0]) for r in await cur.fetchall()]
        if len(my_locs) < min_overlap:
            return None
        loc_placeholders = ",".join("?" * len(my_locs))
        # Step 2: cross-kind devices observed at those same locations in
        # the same window, grouped by overlap count. Exclude the device
        # itself from the sibling set so a wifi/bluetooth alias of the
        # carrier never partners with itself.
        sql = (
            f"SELECT kind, device_id, COUNT(DISTINCT location_id) AS overlap "
            f"FROM observations "
            f"WHERE kind != ? AND seen_at >= ? "
            f"AND location_id IN ({loc_placeholders}) "
            f"GROUP BY kind, device_id "
            f"HAVING overlap >= ? "
            f"ORDER BY overlap DESC, device_id ASC "
            f"LIMIT 1"
        )
        args = [kind, cutoff, *my_locs, min_overlap]
        async with db.execute(sql, args) as cur:
            row = await cur.fetchone()
        if row is None:
            return None
        return {"kind": row[0], "device_id": row[1], "overlap": int(row[2])}


async def purge_old_data(
    *, observation_days: int = 0, device_days: int = 0,
) -> dict:
    """Drop observations older than `observation_days` and devices whose
    last_seen is older than `device_days`. Either threshold ≤ 0 disables
    that pass. Whitelisted devices' rows are kept regardless of the
    device-age threshold (they survive location deletion already, no point
    purging them here). Returns counts of rows removed."""
    counts = {"observations": 0, "devices": 0}
    if observation_days <= 0 and device_days <= 0:
        return counts
    async with _connect() as db:
        if observation_days > 0:
            cutoff = (datetime.now() - timedelta(days=observation_days)).isoformat()
            cur = await db.execute(
                "DELETE FROM observations WHERE seen_at < ?", (cutoff,),
            )
            counts["observations"] = cur.rowcount or 0
        if device_days > 0:
            cutoff = (datetime.now() - timedelta(days=device_days)).isoformat()
            # Skip whitelist matches — pull whitelist once, build a python
            # filter, run a single delete on the candidates that don't match.
            async with db.execute("SELECT kind, device_id FROM device_whitelist") as cur:
                wl = [(r[0], (r[1] or "").lower()) for r in await cur.fetchall()]
            async with db.execute(
                "SELECT location_id, kind, device_id FROM devices WHERE last_seen < ?",
                (cutoff,),
            ) as cur:
                stale = await cur.fetchall()
            removed = 0
            for loc_id, kind, did in stale:
                if _match_whitelist(wl, kind, did):
                    continue
                await db.execute(
                    "DELETE FROM devices WHERE location_id=? AND kind=? AND device_id=?",
                    (loc_id, kind, did),
                )
                removed += 1
            counts["devices"] = removed
        await db.commit()
    return counts


async def presence_snapshot() -> list[dict]:
    """For each sustained_presence rule, return the latest fire and the
    location it came from. Powers the Presence tab — one row per rule
    with a present/absent/unknown state derived from the most recent
    alert_event (cleared=2 dismissals included because dismissing the
    transition doesn't change the underlying state). Rules with
    location_id NULL fall through to the latest event's location, which
    is the closest we have to 'where the device is now' without
    re-walking every observation.

    Each entry:
      rule_id, rule_name, match_value, rule_location_id (None = any),
      rule_location_label, state ('present'|'absent'|'unknown'),
      last_location_id, last_location_label, last_at, last_device_id.
    """
    out: list[dict] = []
    async with _connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT id, name, match_value, location_id "
            "FROM alert_rules "
            "WHERE match_type='sustained_presence' AND enabled=1 "
            "ORDER BY name COLLATE NOCASE"
        ) as cur:
            rules = [dict(r) for r in await cur.fetchall()]
        async with db.execute(
            "SELECT id, label FROM sensor_locations"
        ) as cur:
            loc_label = {int(r[0]): r[1] for r in await cur.fetchall()}
        for r in rules:
            rule_id = int(r["id"])
            # Newest event for this rule, regardless of cleared state —
            # dismissing the transition row doesn't change the device's
            # current presence side.
            async with db.execute(
                "SELECT triggered_at, location_id, device_id, details_json "
                "FROM alert_events WHERE rule_id=? "
                "ORDER BY id DESC LIMIT 1",
                (rule_id,),
            ) as cur:
                ev = await cur.fetchone()
            state = "unknown"
            last_loc_id = None
            last_at = None
            last_device = None
            if ev:
                try:
                    det = json.loads(ev["details_json"] or "{}")
                except (TypeError, ValueError):
                    det = {}
                s = det.get("_presence_state")
                if s in ("present", "absent"):
                    state = s
                last_loc_id = ev["location_id"]
                last_at = ev["triggered_at"]
                last_device = ev["device_id"]
            rule_loc_id = r.get("location_id")
            out.append({
                "rule_id": rule_id,
                "rule_name": r["name"],
                "match_value": r.get("match_value"),
                "rule_location_id": rule_loc_id,
                "rule_location_label": loc_label.get(rule_loc_id) if rule_loc_id else None,
                "state": state,
                "last_location_id": last_loc_id,
                "last_location_label": loc_label.get(last_loc_id) if last_loc_id else None,
                "last_at": last_at,
                "last_device_id": last_device,
            })
    return out


async def list_latched_pairs() -> list[tuple[int, str]]:
    """Every (rule_id, device_id_lower) pair that's actively suppressing
    future fires. The cleared column has two suppressing values:
    cleared=0 (the organic latch that latch=1 rules acquire on fire)
    and cleared=2 (a per-row trash dismissal — applies to any rule
    type). For latch=0 (non-latching) rules, cleared=0 is just
    'this fired' and MUST NOT be treated as suppressing, otherwise
    after a restart load_latches() would silently mute every device
    a non-latching rule had ever matched.

    Latching rules: pair is suppressing when cleared IN (0, 2).
    Non-latching rules: pair is suppressing ONLY when cleared = 2."""
    async with _connect() as db:
        async with db.execute(
            "SELECT DISTINCT e.rule_id, lower(e.device_id) "
            "FROM alert_events e "
            "JOIN alert_rules r ON r.id = e.rule_id "
            "WHERE (r.latch = 1 AND e.cleared <> 1) "
            "   OR (r.latch = 0 AND e.cleared = 2)"
        ) as cur:
            return [(int(r[0]), r[1]) for r in await cur.fetchall()]


async def delete_alert_event(event_id: int) -> dict | None:
    """Per-row 'trash' for the live feed. Behavior depends on the row's
    prior cleared state so the user's mental model — 'this should stay
    gone' — holds across rule types and restarts:

      - cleared=0 (active latch): UPDATE cleared=2 so the row is hidden
        from the feed but the latch persists. Survives restart because
        list_latched_pairs includes cleared=2.
      - cleared=1 (already acknowledged): hard DELETE — the latch is
        already gone, the user is just tidying history.
      - cleared=2: idempotent no-op (the row's already dismissed).

    Returns the metadata the caller needs to update the in-memory latch
    set, or None if no row matched."""
    async with _connect() as db:
        async with db.execute(
            "SELECT rule_id, lower(device_id), cleared "
            "FROM alert_events WHERE id=?",
            (event_id,),
        ) as cur:
            row = await cur.fetchone()
        if not row:
            return None
        rule_id, device_id_l, prior_cleared = int(row[0]), row[1], int(row[2])
        if prior_cleared == 1:
            await db.execute("DELETE FROM alert_events WHERE id=?", (event_id,))
        else:
            await db.execute(
                "UPDATE alert_events SET cleared=2 WHERE id=?", (event_id,)
            )
        await db.commit()
    return {
        "rule_id": rule_id,
        "device_id_l": device_id_l,
        "prior_cleared": prior_cleared,
        # True when the post-state should keep the in-memory latch alive
        # (cleared=2 still suppresses; cleared=1 → DELETE means the row
        # was already non-suppressing, so the caller shouldn't add).
        "suppresses": prior_cleared != 1,
    }


async def clear_alert_pair(rule_id: int, device_id: str) -> int:
    """Mark every still-suppressing event for (rule_id, device_id_lower)
    as cleared=1. Hits both active latches (cleared=0) and dismissed
    rows (cleared=2) so the 🔓 button is a single 're-arm this device'
    action regardless of how the row got into a suppressing state.
    Caller is responsible for removing the matching latch from
    AlertService._latched."""
    async with _connect() as db:
        cur = await db.execute(
            "UPDATE alert_events SET cleared=1 "
            "WHERE rule_id=? AND lower(device_id)=? AND cleared <> 1",
            (rule_id, (device_id or "").lower()),
        )
        await db.commit()
        return cur.rowcount or 0


async def clear_all_latches() -> int:
    """Mark every still-suppressing event (cleared=0 or cleared=2) as
    cleared=1 without deleting any history. Returns the number of rows
    updated."""
    async with _connect() as db:
        cur = await db.execute(
            "UPDATE alert_events SET cleared=1 WHERE cleared <> 1"
        )
        await db.commit()
        return cur.rowcount or 0


async def list_recurring_device_locations(
    *, min_locations: int = 2, top_n: int = 10,
) -> list[dict]:
    """Per-location frequency breakdown for the top-N devices observed at
    ≥ min_locations distinct sensor locations. Picks devices ranked by
    total seen count, then returns one entry per device with a list of
    {location_id, location_label, seen_count, last_seen, best_rssi}
    sorted by seen_count DESC. Used by the report's "Recurrence
    breakdown" section to show *how often*, not just *where*."""
    sql = """
        WITH top_devs AS (
            SELECT kind, device_id,
                   SUM(seen_count) AS total,
                   COUNT(DISTINCT location_id) AS n_locs,
                   MAX(last_seen) AS last_seen,
                   MAX(best_rssi) AS best_rssi
            FROM devices
            GROUP BY kind, device_id
            HAVING n_locs >= ?
            ORDER BY total DESC, n_locs DESC
            LIMIT ?
        )
        SELECT t.kind, t.device_id, t.total, t.n_locs, t.last_seen, t.best_rssi,
               d.location_id, d.seen_count AS loc_seen, d.last_seen AS loc_last_seen,
               d.best_rssi AS loc_best_rssi, d.details_json,
               l.label AS location_label
        FROM top_devs t
        JOIN devices d ON d.kind = t.kind AND d.device_id = t.device_id
        LEFT JOIN sensor_locations l ON l.id = d.location_id
        ORDER BY t.total DESC, t.device_id, d.seen_count DESC
    """
    by_dev: dict[tuple[str, str], dict] = {}
    async with _connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(sql, (min_locations, top_n)) as cur:
            for row in await cur.fetchall():
                key = (row["kind"], row["device_id"])
                head = by_dev.get(key)
                if head is None:
                    try:
                        details = json.loads(row["details_json"] or "{}")
                    except (TypeError, ValueError):
                        details = {}
                    head = {
                        "kind": row["kind"],
                        "device_id": row["device_id"],
                        "total_seen": int(row["total"] or 0),
                        "n_locations": int(row["n_locs"] or 0),
                        "last_seen": row["last_seen"],
                        "best_rssi": row["best_rssi"],
                        "details": details,
                        "per_location": [],
                    }
                    by_dev[key] = head
                head["per_location"].append({
                    "location_id": row["location_id"],
                    "location_label": row["location_label"],
                    "seen_count": int(row["loc_seen"] or 0),
                    "last_seen": row["loc_last_seen"],
                    "best_rssi": row["loc_best_rssi"],
                })
    # Preserve the order from the SQL (already ordered by total DESC).
    return list(by_dev.values())


async def count_alert_events_for_rule_device(
    *, rule_id: int, device_kind: str, device_id: str,
) -> int:
    """How many times this (rule, device) pair has fired across history,
    including the row that *just* got inserted. Used by the Discord embed
    to distinguish "first hit" from "this is the 5th time"."""
    device_id_l = (device_id or "").lower()
    async with _connect() as db:
        async with db.execute(
            "SELECT COUNT(*) FROM alert_events "
            "WHERE rule_id=? AND device_kind=? AND lower(device_id)=?",
            (rule_id, device_kind, device_id_l),
        ) as cur:
            row = await cur.fetchone()
            return int(row[0]) if row else 0


async def bssids_for_ssid_at_location(
    location_id: int, ssid: str,
) -> set[str]:
    """Return the lowercased BSSIDs at this location whose AP advertises
    `ssid`. Used by the wifi_association alert rule so a client that's
    observed exchanging frames with one of those BSSIDs (via its
    `associated_bssids`) fires the rule even when it never sends a
    directed probe-req for the SSID. Empty when the SSID has no
    captured APs at the location or when ssid is blank."""
    if not ssid or location_id is None:
        return set()
    from services.wifi_scanner import normalize_ssid
    out: set[str] = set()
    async with _connect() as db:
        async with db.execute(
            "SELECT device_id, details_json FROM devices "
            "WHERE kind='wifi' AND location_id=?",
            (location_id,),
        ) as cur:
            rows = await cur.fetchall()
    for did, raw in rows:
        try:
            det = json.loads(raw or "{}")
        except (TypeError, ValueError):
            continue
        if normalize_ssid(det.get("ssid")) == ssid:
            out.add((did or "").lower())
    return out


async def stay_start_at_location(
    kind: str, device_id: str, location_id: int, max_gap_seconds: float,
) -> str | None:
    """ISO timestamp of when the device's most-recent 'stay' at this
    location began. A stay is a contiguous run of observations with no
    consecutive pair separated by more than ``max_gap_seconds``. Returns
    None when the device has no observations at this location.

    Used by the sustained_presence rule to ask "has this device been
    continuously visible here for at least N minutes?" — the rule checks
    whether (now - stay_start) >= N*60. Walking backward from the most
    recent sighting lets us short-circuit as soon as we find a gap, so
    long observation histories don't make the query expensive.
    """
    device_id_l = (device_id or "").lower()
    async with _connect() as db:
        async with db.execute(
            "SELECT seen_at FROM observations "
            "WHERE kind=? AND device_id=? AND location_id=? "
            "ORDER BY seen_at DESC LIMIT 5000",
            (kind, device_id_l, location_id),
        ) as cur:
            rows = await cur.fetchall()
    if not rows:
        return None
    stay_start = rows[0][0]
    prev_t: float | None = None
    for (seen_at,) in rows:
        try:
            t = datetime.fromisoformat(seen_at).timestamp()
        except ValueError:
            # Skip unparsable timestamps but don't reset the run — gap
            # detection only works on parseable rows; an unreadable row
            # in the middle would otherwise mask the gap on either side.
            continue
        if prev_t is not None and (prev_t - t) > max_gap_seconds:
            # Found a gap larger than the threshold — the current stay
            # starts at the row we processed in the previous iteration
            # (already captured in stay_start).
            break
        stay_start = seen_at
        prev_t = t
    return stay_start


async def last_observation_at_location(
    kind: str, device_id: str, location_id: int,
) -> str | None:
    """ISO timestamp of the device's most recent sighting at this location,
    or None if it's never been seen here. Used by sustained_presence to
    detect the absent transition (no sighting for > gap_minutes)."""
    device_id_l = (device_id or "").lower()
    async with _connect() as db:
        async with db.execute(
            "SELECT seen_at FROM observations "
            "WHERE kind=? AND device_id=? AND location_id=? "
            "ORDER BY seen_at DESC LIMIT 1",
            (kind, device_id_l, location_id),
        ) as cur:
            row = await cur.fetchone()
            return row[0] if row else None


def _alias_where(device_ids: list[str], kind: str | None) -> tuple[str, list]:
    """Build a parameterised WHERE fragment that matches observations
    whose device_id is exactly or prefix-matches any id in ``device_ids``
    (LIKE 'id%'). Optionally pinned to a kind. Returns (sql_fragment,
    params)."""
    ids = [d.lower() for d in device_ids if d]
    if not ids:
        return "0", []
    like_clauses = " OR ".join(["device_id LIKE ?"] * len(ids))
    where = f"({like_clauses})"
    params: list = [d + "%" for d in ids]
    if kind:
        where = f"kind=? AND {where}"
        params.insert(0, kind)
    return where, params


async def stay_start_multi(
    kind: str | None, device_ids: list[str],
    location_id: int, max_gap_seconds: float,
) -> str | None:
    """Like stay_start_at_location but the 'stay' spans a set of device
    IDs treated as one conceptual device (e.g. the wifi + bluetooth
    identities of the same phone). Any sighting from any listed id
    extends the stay; the stay only resets when every id has been silent
    for > ``max_gap_seconds``. IDs match exactly or as a prefix. Pass
    kind=None to span device kinds (the typical use)."""
    where, params = _alias_where(device_ids, kind)
    if not params:
        return None
    params.append(location_id)
    async with _connect() as db:
        async with db.execute(
            f"SELECT seen_at FROM observations "
            f"WHERE {where} AND location_id=? "
            f"ORDER BY seen_at DESC LIMIT 5000",
            params,
        ) as cur:
            rows = await cur.fetchall()
    if not rows:
        return None
    stay_start = rows[0][0]
    prev_t: float | None = None
    for (seen_at,) in rows:
        try:
            t = datetime.fromisoformat(seen_at).timestamp()
        except ValueError:
            continue
        if prev_t is not None and (prev_t - t) > max_gap_seconds:
            break
        stay_start = seen_at
        prev_t = t
    return stay_start


async def last_observation_multi(
    kind: str | None, device_ids: list[str], location_id: int,
) -> str | None:
    """ISO timestamp of the most-recent observation at this location for
    any device id in the supplied list (exact or prefix match)."""
    where, params = _alias_where(device_ids, kind)
    if not params:
        return None
    params.append(location_id)
    async with _connect() as db:
        async with db.execute(
            f"SELECT seen_at FROM observations "
            f"WHERE {where} AND location_id=? "
            f"ORDER BY seen_at DESC LIMIT 1",
            params,
        ) as cur:
            row = await cur.fetchone()
            return row[0] if row else None


async def list_presence_events() -> list[dict]:
    """Every recent sustained_presence transition, newest first, with
    state parsed out of the event's details_json. The caller (alert
    service) dedupes by either (rule, kind, device_id, location) for
    single-device rules or (rule, location) for alias-group rules
    depending on the rule's current match_value. Capped to 5000 rows so
    a noisy long-running rule can't bloat startup."""
    async with _connect() as db:
        async with db.execute(
            """
            SELECT e.rule_id, e.device_kind, lower(e.device_id), e.location_id,
                   e.details_json
            FROM alert_events e
            JOIN alert_rules r ON r.id = e.rule_id
            WHERE r.match_type = 'sustained_presence'
              AND e.location_id IS NOT NULL
            ORDER BY e.triggered_at DESC
            LIMIT 5000
            """
        ) as cur:
            rows = await cur.fetchall()
    out: list[dict] = []
    for r in rows:
        try:
            details = json.loads(r[4] or "{}")
        except (TypeError, ValueError):
            details = {}
        state = details.get("_presence_state")
        if state in ("present", "absent"):
            out.append({
                "rule_id": int(r[0]), "kind": r[1], "device_id_l": r[2],
                "location_id": int(r[3]), "state": state,
                "is_group": bool(details.get("_presence_group")),
            })
    return out


async def previous_observation_at_location(
    kind: str, device_id: str, location_id: int,
) -> str | None:
    """ISO timestamp of the second-most-recent observation of this device
    at this location — i.e. the sighting right before the one currently
    being processed. Used by the arrival_after_gap rule to measure the
    gap since the device's previous visit. Returns None when there's no
    prior observation (the device is brand new at this location)."""
    device_id_l = (device_id or "").lower()
    async with _connect() as db:
        async with db.execute(
            "SELECT seen_at FROM observations "
            "WHERE kind=? AND device_id=? AND location_id=? "
            "ORDER BY seen_at DESC LIMIT 1 OFFSET 1",
            (kind, device_id_l, location_id),
        ) as cur:
            row = await cur.fetchone()
            return row[0] if row else None


async def find_absent_devices_at_location(
    *, kind: str | None, location_id: int | None, min_age_minutes: int,
    max_age_minutes: int | None = None,
) -> list[dict]:
    """Devices whose last_seen at the matching location(s) is older than
    `min_age_minutes` and (optionally) newer than `max_age_minutes`.

    Used by the absence_gap alert loop — the orchestrator polls this on
    a steady cadence, and the upper bound stops the query returning
    every device ever seen (we don't want a rule that fires once when
    Alice leaves to also fire forever for everyone who's ever been
    here)."""
    if min_age_minutes < 1:
        return []
    now = datetime.now()
    upper = (now - timedelta(minutes=min_age_minutes)).isoformat()
    lower = (
        (now - timedelta(minutes=max_age_minutes)).isoformat()
        if max_age_minutes else None
    )
    conds = ["d.last_seen < ?"]
    args: list = [upper]
    if lower is not None:
        conds.append("d.last_seen >= ?")
        args.append(lower)
    if kind:
        conds.append("d.kind = ?")
        args.append(kind)
    if location_id is not None:
        conds.append("d.location_id = ?")
        args.append(location_id)
    sql = (
        "SELECT d.location_id, d.kind, d.device_id, d.last_seen, "
        "       d.last_rssi, d.details_json, l.label "
        "FROM devices d LEFT JOIN sensor_locations l ON l.id = d.location_id "
        f"WHERE {' AND '.join(conds)} "
        "ORDER BY d.last_seen DESC"
    )
    async with _connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(sql, tuple(args)) as cur:
            rows = [dict(r) for r in await cur.fetchall()]
    for r in rows:
        try:
            r["details"] = json.loads(r.pop("details_json") or "{}")
        except (TypeError, ValueError):
            r["details"] = {}
    return rows


async def about_stats() -> dict:
    """Cheap aggregate counts for the About tab. One round-trip, a handful
    of COUNT(*) queries — fine to call on every tab open."""
    out: dict = {
        "db_path": str(DB_PATH),
        "db_size_bytes": 0,
        "locations": 0,
        "devices": {"total": 0, "wifi": 0, "bluetooth": 0,
                    "bluetooth_classic": 0, "wifi_client": 0},
        "observations": 0,
        "alert_rules": 0,
        "alert_events": 0,
        "whitelist": 0,
    }
    try:
        if DB_PATH.exists():
            out["db_size_bytes"] = DB_PATH.stat().st_size
    except OSError:
        pass
    async with _connect() as db:
        async def _scalar(sql: str, args: tuple = ()) -> int:
            async with db.execute(sql, args) as cur:
                row = await cur.fetchone()
                return int(row[0]) if row and row[0] is not None else 0
        out["locations"] = await _scalar("SELECT COUNT(*) FROM sensor_locations")
        out["devices"]["total"] = await _scalar("SELECT COUNT(*) FROM devices")
        for k in ("wifi", "bluetooth", "bluetooth_classic", "wifi_client"):
            out["devices"][k] = await _scalar(
                "SELECT COUNT(*) FROM devices WHERE kind=?", (k,),
            )
        out["observations"] = await _scalar("SELECT COUNT(*) FROM observations")
        out["alert_rules"] = await _scalar("SELECT COUNT(*) FROM alert_rules")
        out["alert_events"] = await _scalar("SELECT COUNT(*) FROM alert_events")
        out["whitelist"] = await _scalar(
            "SELECT (SELECT COUNT(*) FROM device_whitelist) "
            "+ (SELECT COUNT(*) FROM device_temp_whitelist)",
        )
    return out


async def alert_event_counts_per_rule() -> list[dict]:
    """Per-rule fire counts and last-fired timestamps. Used by the report
    to show which alert rules are actually doing work."""
    sql = """
        SELECT r.id, r.name, r.kind, r.match_type, r.match_value,
               r.location_id, r.enabled,
               COALESCE(c.fires, 0) AS fires,
               c.last_fired
        FROM alert_rules r
        LEFT JOIN (
            SELECT rule_id, COUNT(*) AS fires, MAX(triggered_at) AS last_fired
            FROM alert_events GROUP BY rule_id
        ) c ON c.rule_id = r.id
        ORDER BY fires DESC, r.id ASC
    """
    async with _connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(sql) as cur:
            return [dict(r) for r in await cur.fetchall()]


async def clear_alert_events() -> int:
    async with _connect() as db:
        async with db.execute("SELECT COUNT(*) FROM alert_events") as cur:
            n = (await cur.fetchone())[0]
        await db.execute("DELETE FROM alert_events")
        await db.commit()
    return n


async def replace_oui_entries(rows: list[tuple[str, str, str, str | None]]) -> int:
    """Replace the OUI table contents in a single transaction. Returns inserted count."""
    async with _connect() as db:
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
    async with _connect() as db:
        async with db.execute(
            "SELECT prefix, registry, organization FROM oui_entries "
            "ORDER BY length(prefix) DESC"
        ) as cur:
            return [(r[0], r[1], r[2]) for r in await cur.fetchall()]


async def oui_count() -> int:
    async with _connect() as db:
        async with db.execute("SELECT COUNT(*) FROM oui_entries") as cur:
            row = await cur.fetchone()
            return row[0] if row else 0


async def oui_counts_by_registry() -> dict[str, int]:
    async with _connect() as db:
        async with db.execute(
            "SELECT registry, COUNT(*) FROM oui_entries GROUP BY registry"
        ) as cur:
            return {row[0]: row[1] for row in await cur.fetchall()}


async def create_mission(name: str, description: str | None = None) -> dict:
    """Open a new mission. Rejects (returns dict with error) if there's
    already an active mission — the active state is the row whose
    ended_at is NULL. Snapshots about_stats() at start so the End call
    can render a meaningful diff."""
    active = await active_mission()
    if active is not None:
        return {"error": "Another mission is already active", "active": active}
    stats = await about_stats()
    now = datetime.now().isoformat()
    async with _connect() as db:
        cur = await db.execute(
            "INSERT INTO missions(name, description, started_at, "
            "stats_start_json) VALUES(?,?,?,?)",
            (name.strip() or "Mission", (description or "").strip() or None,
             now, json.dumps(stats)),
        )
        await db.commit()
        mission_id = cur.lastrowid
    return await get_mission(mission_id)


async def end_mission(mission_id: int) -> dict | None:
    """Close out the active mission — snapshots about_stats() into
    stats_end_json and stamps ended_at. Returns the full mission row
    (with diff already inflated). Returns None if the mission doesn't
    exist or has already ended."""
    async with _connect() as db:
        async with db.execute(
            "SELECT ended_at FROM missions WHERE id=?", (mission_id,),
        ) as cur:
            row = await cur.fetchone()
        if row is None or row[0] is not None:
            return None
    stats = await about_stats()
    now = datetime.now().isoformat()
    async with _connect() as db:
        await db.execute(
            "UPDATE missions SET ended_at=?, stats_end_json=? WHERE id=?",
            (now, json.dumps(stats), mission_id),
        )
        await db.commit()
    return await get_mission(mission_id)


async def active_mission() -> dict | None:
    """Return the active mission (ended_at IS NULL) or None."""
    async with _connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM missions WHERE ended_at IS NULL "
            "ORDER BY id DESC LIMIT 1"
        ) as cur:
            row = await cur.fetchone()
    return _inflate_mission(dict(row)) if row else None


async def get_mission(mission_id: int) -> dict | None:
    async with _connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM missions WHERE id=?", (mission_id,),
        ) as cur:
            row = await cur.fetchone()
    return _inflate_mission(dict(row)) if row else None


async def list_missions(limit: int = 50) -> list[dict]:
    """Most recent missions first. Active mission (if any) appears at
    the top because its id is the latest."""
    async with _connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM missions ORDER BY id DESC LIMIT ?", (limit,),
        ) as cur:
            rows = [dict(r) for r in await cur.fetchall()]
    return [_inflate_mission(r) for r in rows]


async def delete_mission(mission_id: int) -> bool:
    """Remove a mission audit row. Does NOT delete the underlying
    data (locations, devices, observations) — the mission table is
    just bookkeeping over the existing data."""
    async with _connect() as db:
        cur = await db.execute(
            "DELETE FROM missions WHERE id=?", (mission_id,),
        )
        await db.commit()
        return (cur.rowcount or 0) > 0


def _inflate_mission(row: dict) -> dict:
    """Parse the JSON columns + compute a stats diff when both ends
    are populated. Keeps the API surface tidy so the UI doesn't
    re-parse the same blobs."""
    out = dict(row)
    for k in ("stats_start_json", "stats_end_json"):
        raw = out.pop(k, None)
        out[k.replace("_json", "")] = (
            json.loads(raw) if raw else None
        )
    start = out.get("stats_start") or {}
    end = out.get("stats_end") or {}
    if start and end:
        out["diff"] = _diff_stats(start, end)
    else:
        out["diff"] = None
    return out


def _diff_stats(start: dict, end: dict) -> dict:
    """Compute end - start across the about_stats() shape."""
    def _delta(a, b):
        try:
            return int((b or 0)) - int((a or 0))
        except (TypeError, ValueError):
            return None
    return {
        "locations":   _delta(start.get("locations"),   end.get("locations")),
        "observations": _delta(start.get("observations"), end.get("observations")),
        "alert_events": _delta(start.get("alert_events"), end.get("alert_events")),
        "devices": {
            "total": _delta(
                (start.get("devices") or {}).get("total"),
                (end.get("devices") or {}).get("total"),
            ),
            "wifi": _delta(
                (start.get("devices") or {}).get("wifi"),
                (end.get("devices") or {}).get("wifi"),
            ),
            "bluetooth": _delta(
                (start.get("devices") or {}).get("bluetooth"),
                (end.get("devices") or {}).get("bluetooth"),
            ),
            "bluetooth_classic": _delta(
                (start.get("devices") or {}).get("bluetooth_classic"),
                (end.get("devices") or {}).get("bluetooth_classic"),
            ),
            "wifi_client": _delta(
                (start.get("devices") or {}).get("wifi_client"),
                (end.get("devices") or {}).get("wifi_client"),
            ),
        },
    }


async def delete_devices_by_kind(kind: str) -> dict:
    """Wipe every device + observation of the given kind across all
    locations. Whitelisted devices follow the same preservation flow as
    location deletion — they're archived to preserved_devices first."""
    if kind not in ("wifi", "bluetooth", "bluetooth_classic", "wifi_client"):
        return {"error": "invalid kind", "devices": 0, "observations": 0}
    async with _connect() as db:
        # Pull the whitelist once for preservation matching.
        async with db.execute(
            "SELECT kind, device_id FROM device_whitelist "
            "UNION SELECT kind, device_id FROM device_temp_whitelist"
        ) as cur:
            wl = [(r[0], (r[1] or "").lower()) for r in await cur.fetchall()]
        async with db.execute(
            "SELECT location_id, kind, device_id, first_seen, last_seen, "
            "best_rssi, last_rssi, seen_count, details_json "
            "FROM devices WHERE kind=?", (kind,),
        ) as cur:
            dev_rows = await cur.fetchall()
        now = datetime.now().isoformat()
        preserved = 0
        for loc_id, k, did, first_seen, last_seen, best_rssi, last_rssi, seen, details in dev_rows:
            if not _match_whitelist(wl, k, did):
                continue
            async with db.execute(
                "SELECT first_seen, last_seen, best_rssi, last_rssi, seen_count, details_json "
                "FROM preserved_devices WHERE kind=? AND device_id=?",
                (k, did),
            ) as c:
                existing = await c.fetchone()
            if existing is None:
                await db.execute(
                    "INSERT INTO preserved_devices(kind, device_id, first_seen, last_seen, "
                    "best_rssi, last_rssi, seen_count, details_json, "
                    "archived_from_location_id, archived_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (k, did, first_seen, last_seen, best_rssi, last_rssi,
                     seen, details, loc_id, now),
                )
            else:
                e_first, e_last, e_best, e_last_rssi, e_seen, e_details = existing
                await db.execute(
                    "UPDATE preserved_devices SET first_seen=?, last_seen=?, "
                    "best_rssi=?, last_rssi=?, seen_count=?, details_json=?, "
                    "archived_from_location_id=?, archived_at=? "
                    "WHERE kind=? AND device_id=?",
                    (min(first_seen, e_first), max(last_seen, e_last),
                     max(best_rssi, e_best),
                     last_rssi if last_seen >= e_last else e_last_rssi,
                     (seen or 0) + (e_seen or 0),
                     details if last_seen >= e_last else e_details,
                     loc_id, now, k, did),
                )
            preserved += 1
        # Wipe observations + devices for the kind.
        cur = await db.execute("DELETE FROM observations WHERE kind=?", (kind,))
        obs_count = cur.rowcount or 0
        cur = await db.execute("DELETE FROM devices WHERE kind=?", (kind,))
        dev_count = cur.rowcount or 0
        await db.commit()
    return {
        "kind": kind, "devices": dev_count, "observations": obs_count,
        "preserved": preserved,
    }


async def integrity_check() -> dict:
    """Run SQLite's PRAGMA integrity_check. Returns 'ok' on a clean DB
    or a list of error strings otherwise. Cheap on a small DB; can take
    a moment on a large one."""
    async with _connect() as db:
        async with db.execute("PRAGMA integrity_check") as cur:
            rows = [r[0] for r in await cur.fetchall()]
    ok = len(rows) == 1 and rows[0].lower() == "ok"
    return {"ok": ok, "messages": rows}


def _classify_wifi_topology(bssids: list[dict]) -> dict:
    """Pick a topology label for an SSID group based on the 802.11 IEs we
    parsed off the BSSIDs. Order of certainty:

      1. mesh — at least one BSSID exposed an 802.11s Mesh ID or Mesh
         Configuration IE. Effectively zero false positives.
      2. ess  — multiple BSSIDs share an 802.11r Mobility Domain (MDID),
         which is the canonical signal for an 802.11r-roaming federated
         ESS deployment (commercial WiFi, eero-style mesh systems).
      3. multi_band — multiple BSSIDs from the same OUI on different
         bands (2.4 / 5 / 6 GHz). Strong indicator of a single physical
         radio advertising one SSID across its bands.
      4. multi_ap — generic "more than one BSSID" fallback when none of
         the above signals apply (different OUIs, missing IEs, etc.).
      5. single — just one BSSID.

    Returns a dict with `kind` (one of the above) plus collected
    evidence the UI can show in a tooltip."""
    if not bssids:
        return {"kind": "none"}
    if len(bssids) == 1:
        return {
            "kind": "single",
            "bands": [bssids[0].get("band")] if bssids[0].get("band") else [],
        }
    mesh_ids = [b.get("mesh_id") for b in bssids if b.get("is_mesh")]
    if any(b.get("is_mesh") for b in bssids):
        return {
            "kind": "mesh",
            "mesh_ids": sorted({m for m in mesh_ids if m}),
            "bands": sorted({b["band"] for b in bssids if b.get("band")}),
        }
    mdids = {b.get("mobility_domain") for b in bssids if b.get("mobility_domain")}
    if len(mdids) == 1 and next(iter(mdids)) is not None:
        return {
            "kind": "ess",
            "mobility_domain": next(iter(mdids)),
            "bands": sorted({b["band"] for b in bssids if b.get("band")}),
        }
    bands = sorted({b["band"] for b in bssids if b.get("band")})
    ouis = {b["bssid"][:8].lower() for b in bssids if b.get("bssid")}
    if len(bands) >= 2 and len(ouis) == 1:
        return {"kind": "multi_band", "bands": bands}
    return {"kind": "multi_ap", "bands": bands}


async def wifi_aps_grouped(location_id: int | None = None) -> list[dict]:
    """Pull every captured WiFi AP, group by SSID, and attach the list
    of wifi_client devices that have probed for that SSID. The
    "associated clients" link is best-effort — we don't capture 802.11
    association frames, only probe requests, so a client appears under
    an AP when its probed-SSID list contains the AP's SSID.

    Hidden APs (empty SSID) and clients that never probed for a named
    network (`ssids` empty) are bucketed under a single "(hidden)"
    group so they're not lost from the view. Filtering by location_id
    scopes both the APs and the client probes to that location.
    Sorted by best RSSI (strongest first).
    """
    sql_aps = (
        "SELECT location_id, device_id, last_seen, first_seen, "
        "       best_rssi, last_rssi, seen_count, details_json "
        "FROM devices WHERE kind='wifi'"
    )
    sql_clients = (
        "SELECT location_id, device_id, last_seen, first_seen, "
        "       best_rssi, last_rssi, seen_count, details_json "
        "FROM devices WHERE kind='wifi_client'"
    )
    args: tuple = ()
    if location_id is not None:
        sql_aps += " AND location_id=?"
        sql_clients += " AND location_id=?"
        args = (location_id,)

    async with _connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(sql_aps, args) as cur:
            ap_rows = [dict(r) for r in await cur.fetchall()]
        async with db.execute(sql_clients, args) as cur:
            client_rows = [dict(r) for r in await cur.fetchall()]

    # Normalize all-NUL or '\x00'-padded SSIDs so legacy rows captured
    # before the scanner started normalizing get bucketed under
    # (hidden) like the new ones.
    from services.wifi_scanner import normalize_ssid

    HIDDEN = "(hidden)"
    groups: dict[str, dict] = {}

    for r in ap_rows:
        try:
            det = json.loads(r.pop("details_json") or "{}")
        except (TypeError, ValueError):
            det = {}
        raw_ssid = normalize_ssid(det.get("ssid"))
        ssid = raw_ssid or HIDDEN
        g = groups.setdefault(ssid, {
            "ssid": raw_ssid,
            "is_hidden": not raw_ssid,
            "bssids": [],
            "clients": [],
            "client_count": 0,
            "bssid_count": 0,
            "best_rssi": None,
            "last_seen": None,
        })
        g["bssids"].append({
            "bssid": r["device_id"],
            "location_id": r["location_id"],
            "vendor": det.get("vendor"),
            "channel": det.get("channel"),
            "band": det.get("band"),
            "encryption": det.get("encryption"),
            "cipher": det.get("cipher"),
            "auth": det.get("auth"),
            "capabilities": det.get("capabilities"),
            "is_mesh": bool(det.get("is_mesh")),
            "mesh_id": det.get("mesh_id"),
            "mobility_domain": det.get("mobility_domain"),
            "best_rssi": r["best_rssi"],
            "last_rssi": r["last_rssi"],
            "seen_count": r["seen_count"],
            "first_seen": r["first_seen"],
            "last_seen": r["last_seen"],
        })
        # Roll the AP's best signal + last_seen into the group header.
        if g["best_rssi"] is None or (r["best_rssi"] is not None
                                     and r["best_rssi"] > g["best_rssi"]):
            g["best_rssi"] = r["best_rssi"]
        if g["last_seen"] is None or (r["last_seen"] or "") > g["last_seen"]:
            g["last_seen"] = r["last_seen"]

    # Map captured BSSIDs back to the SSID group they belong to. Used
    # below so a client observed exchanging frames with a known AP lands
    # in that AP's named-SSID group even if it never sent a directed
    # probe — modern clients lean almost entirely on wildcard probes,
    # so without this path every named SSID stays empty and only the
    # (hidden) bucket fills up.
    bssid_to_group_key: dict[str, str] = {}
    for ssid_key, g in groups.items():
        for b in g["bssids"]:
            bl = (b["bssid"] or "").lower()
            if bl:
                bssid_to_group_key[bl] = ssid_key

    # Walk clients, building two indexes in the same pass:
    #   1. group["clients"] — clients linked to each SSID group, via
    #      probed-SSID match OR via association evidence (a BSSID in
    #      the client's `associated_bssids` that we've captured as an
    #      AP for that SSID).
    #   2. bssid_to_attached — per-BSSID reverse index of clients with
    #      that BSSID in their `associated_bssids`. Attached to each
    #      BSSID below so the UI can flag rows with the ★ associated
    #      badge regardless of whether the client also probed.
    bssid_to_attached: dict[str, list[dict]] = {}
    for r in client_rows:
        try:
            det = json.loads(r.pop("details_json") or "{}")
        except (TypeError, ValueError):
            det = {}
        # Normalize each historical ssid so all-NUL / '\x00'-padded
        # entries collapse to "" and the client no longer claims to
        # have probed for a named hidden network.
        ssids = [
            normalize_ssid(s)
            for s in (det.get("ssids") or [])
            if isinstance(s, str)
        ]
        ssids = [s for s in ssids if s]
        assoc = [
            s.lower() for s in (det.get("associated_bssids") or [])
            if isinstance(s, str) and s
        ]
        entry = {
            "device_id": r["device_id"],
            "location_id": r["location_id"],
            "vendor": det.get("vendor"),
            "channels": det.get("channels") or [],
            "ssids": ssids,
            "associated_bssids": assoc,
            "randomized": bool(det.get("randomized")),
            "best_rssi": r["best_rssi"],
            "last_rssi": r["last_rssi"],
            "seen_count": r["seen_count"],
            "first_seen": r["first_seen"],
            "last_seen": r["last_seen"],
        }
        # Build the set of group keys this client belongs to:
        # - Each named SSID it probed for (creates a 'wanted' group if
        #   we've never seen an AP for it).
        # - Each captured-AP BSSID it's associated with (always lands
        #   in an existing group; assoc to a never-captured BSSID is
        #   recorded in bssid_to_attached but doesn't create a group).
        # - HIDDEN as a fallback when neither path names a network.
        probed_keys = [s for s in ssids if s]
        assoc_keys: list[str] = []
        for b in assoc:
            gk = bssid_to_group_key.get(b)
            if gk and gk not in assoc_keys:
                assoc_keys.append(gk)
        targets = probed_keys + [k for k in assoc_keys if k not in probed_keys]
        if not targets:
            targets = [HIDDEN]
        for target in targets:
            g = groups.get(target)
            if g is None:
                # Probed-only path: the client probed for a network we've
                # never seen the AP for. Keep an entry so the SSID still
                # appears, with an empty bssids list (a 'wanted but
                # unfound' row).
                g = groups.setdefault(target, {
                    "ssid": "" if target == HIDDEN else target,
                    "is_hidden": target == HIDDEN,
                    "bssids": [],
                    "clients": [],
                    "client_count": 0,
                    "bssid_count": 0,
                    "best_rssi": None,
                    "last_seen": None,
                })
            # Dedupe within group — a (location, device) pair should
            # only show once even if probed + assoc evidence converge.
            if not any(
                c["device_id"] == entry["device_id"]
                and c["location_id"] == entry["location_id"]
                for c in g["clients"]
            ):
                g["clients"].append(entry)
        # Reverse-index for the per-BSSID 'attached devices' list. We
        # dedupe by device_id so a client that hopped between locations
        # only shows once under a given BSSID.
        for b in assoc:
            attached_list = bssid_to_attached.setdefault(b, [])
            if not any(c["device_id"] == entry["device_id"] for c in attached_list):
                attached_list.append(entry)

    # Attach the reverse-index hits to each BSSID. 'attached_clients'
    # is best-effort evidence — populated whenever a client emits any
    # mgmt frame or data frame whose Addr3 names this BSSID.
    for g in groups.values():
        for b in g["bssids"]:
            attached = bssid_to_attached.get((b["bssid"] or "").lower(), [])
            # Sort by recency so the most-recently-seen device shows first.
            b["attached_clients"] = sorted(
                attached,
                key=lambda c: c.get("last_seen") or "",
                reverse=True,
            )
            b["attached_client_count"] = len(b["attached_clients"])

    # Sort BSSIDs by signal (strongest first), clients by recency.
    for g in groups.values():
        g["bssids"].sort(key=lambda b: (b.get("best_rssi") or -999), reverse=True)
        g["clients"].sort(key=lambda c: c.get("last_seen") or "", reverse=True)
        g["bssid_count"] = len(g["bssids"])
        g["client_count"] = len(g["clients"])
        g["topology"] = _classify_wifi_topology(g["bssids"])

    # Output ordering: groups with the strongest AP signal first, then
    # client-only "wanted" SSIDs by client count. Pure-wanted groups
    # drop to the bottom since they have no measured AP.
    return sorted(
        groups.values(),
        key=lambda g: (
            0 if g["bssid_count"] else 1,
            -(g["best_rssi"] if g["best_rssi"] is not None else -999),
            -g["client_count"],
            g["ssid"].lower(),
        ),
    )


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
    async with _connect() as db:
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
    async with _connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM device_whitelist ORDER BY kind, device_id"
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]


async def list_whitelist_with_devices() -> list[dict]:
    """Whitelist rows enriched with aggregate info about every device they
    match in the live `devices` and `preserved_devices` tables. The match
    rule mirrors AlertService.is_whitelisted: device_id == target (exact)
    OR device_id LIKE target||'%' (OUI / prefix). For each entry we
    return:
      - match_count        live devices matched
      - preserved_count    archived devices matched
      - location_count     distinct sensor_locations represented
      - best_rssi          strongest sighting across all matches
      - last_seen          most recent last_seen across matches
      - vendor / name      from the most recently-seen matching row
      - tracker_type       from the most recently-seen matching row
      - sample_device_id   one representative MAC (useful for prefix entries)
    """
    entries = await list_whitelist()
    if not entries:
        return entries
    async with _connect() as db:
        db.row_factory = aiosqlite.Row
        for e in entries:
            kind = e["kind"]
            target = (e["device_id"] or "").lower()
            if not target:
                continue
            like = f"{target}%"

            # Aggregates from live devices
            async with db.execute(
                """
                SELECT COUNT(*) AS n,
                       COUNT(DISTINCT location_id) AS loc_n,
                       MAX(best_rssi) AS best_rssi,
                       MAX(last_seen) AS last_seen
                FROM devices
                WHERE kind=? AND (device_id=? OR device_id LIKE ?)
                """,
                (kind, target, like),
            ) as cur:
                agg = dict(await cur.fetchone() or {})
            # Most-recent matching row for representative vendor/name
            async with db.execute(
                """
                SELECT device_id, details_json, last_seen
                FROM devices
                WHERE kind=? AND (device_id=? OR device_id LIKE ?)
                ORDER BY last_seen DESC LIMIT 1
                """,
                (kind, target, like),
            ) as cur:
                latest = await cur.fetchone()
            # Preserved-table count (whitelist entries archived from
            # deleted locations).
            async with db.execute(
                """
                SELECT COUNT(*) AS n,
                       MAX(last_seen) AS last_seen,
                       MAX(best_rssi) AS best_rssi
                FROM preserved_devices
                WHERE kind=? AND (device_id=? OR device_id LIKE ?)
                """,
                (kind, target, like),
            ) as cur:
                pres = dict(await cur.fetchone() or {})
            # Fall back to a preserved_devices latest row if no live match.
            if latest is None:
                async with db.execute(
                    """
                    SELECT device_id, details_json, last_seen
                    FROM preserved_devices
                    WHERE kind=? AND (device_id=? OR device_id LIKE ?)
                    ORDER BY last_seen DESC LIMIT 1
                    """,
                    (kind, target, like),
                ) as cur:
                    latest = await cur.fetchone()

            details = {}
            if latest is not None:
                try:
                    details = json.loads(latest["details_json"] or "{}")
                except (TypeError, ValueError):
                    details = {}

            e["match_count"] = int(agg.get("n") or 0)
            e["preserved_count"] = int(pres.get("n") or 0)
            e["location_count"] = int(agg.get("loc_n") or 0)
            # Pick the strongest RSSI across both tables (max of two MAXes).
            best = [r for r in (agg.get("best_rssi"), pres.get("best_rssi"))
                    if r is not None]
            e["best_rssi"] = max(best) if best else None
            last_seens = [r for r in (agg.get("last_seen"), pres.get("last_seen"))
                          if r]
            e["last_seen"] = max(last_seens) if last_seens else None
            e["vendor"] = details.get("vendor") or None
            e["name"] = details.get("ssid") or details.get("name") or None
            e["tracker_type"] = classify_tracker(kind, details)
            e["sample_device_id"] = latest["device_id"] if latest is not None else None
    return entries


async def add_whitelist(kind: str, device_id: str, note: str | None = None) -> int:
    """Insert a whitelist entry (or update its note if the (kind, device_id)
    pair already exists). Returns the row id."""
    now = datetime.now().isoformat()
    async with _connect() as db:
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


async def update_whitelist(entry_id: int, kind: str, device_id: str,
                           note: str | None) -> bool:
    """Update an existing whitelist row by id. Returns False if no row has
    that id; raises ValueError if the new (kind, device_id) collides with
    a different entry's UNIQUE constraint."""
    async with _connect() as db:
        async with db.execute(
            "SELECT 1 FROM device_whitelist WHERE id=?", (entry_id,)
        ) as cur:
            if await cur.fetchone() is None:
                return False
        async with db.execute(
            "SELECT id FROM device_whitelist WHERE kind=? AND device_id=? AND id <> ?",
            (kind, device_id.lower(), entry_id),
        ) as cur:
            if await cur.fetchone() is not None:
                raise ValueError(
                    f"another whitelist entry already has {kind}/{device_id}"
                )
        await db.execute(
            "UPDATE device_whitelist SET kind=?, device_id=?, note=? WHERE id=?",
            (kind, device_id.lower(), note, entry_id),
        )
        await db.commit()
        return True


async def list_temp_whitelist() -> list[dict]:
    """Session-scoped whitelist (baseline-scan auto-populated)."""
    async with _connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM device_temp_whitelist ORDER BY kind, device_id"
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]


async def add_temp_whitelist(kind: str, device_id: str,
                             note: str | None = None) -> int:
    """Upsert into the temporary whitelist. Same UNIQUE rule as the
    permanent table — repeated calls with the same (kind, device_id)
    update the note instead of duplicating."""
    now = datetime.now().isoformat()
    async with _connect() as db:
        await db.execute(
            "INSERT INTO device_temp_whitelist(kind, device_id, note, created_at) "
            "VALUES(?,?,?,?) "
            "ON CONFLICT(kind, device_id) DO UPDATE SET note=excluded.note",
            (kind, device_id.lower(), note, now),
        )
        await db.commit()
        async with db.execute(
            "SELECT id FROM device_temp_whitelist WHERE kind=? AND device_id=?",
            (kind, device_id.lower()),
        ) as cur:
            row = await cur.fetchone()
    return int(row[0]) if row else 0


async def delete_temp_whitelist_pair(kind: str, device_id: str) -> bool:
    async with _connect() as db:
        cur = await db.execute(
            "DELETE FROM device_temp_whitelist WHERE kind=? AND device_id=?",
            (kind, (device_id or "").lower()),
        )
        await db.commit()
        return cur.rowcount > 0


async def clear_temp_whitelist() -> int:
    """Wipe the temporary whitelist. Called from delete_all_locations."""
    async with _connect() as db:
        cur = await db.execute("DELETE FROM device_temp_whitelist")
        await db.commit()
        return cur.rowcount or 0


async def list_whitelist_combined() -> list[dict]:
    """Permanent + temporary whitelist rows merged. Used by alert and
    report matchers so temp entries silence devices the same as permanent
    ones. Each row carries a `source` field of 'permanent' or 'temp' so
    downstream code can render them differently if desired."""
    perm = await list_whitelist()
    for r in perm:
        r["source"] = "permanent"
    temp = await list_temp_whitelist()
    for r in temp:
        r["source"] = "temp"
    return perm + temp


async def delete_whitelist(entry_id: int) -> bool:
    async with _connect() as db:
        cur = await db.execute("DELETE FROM device_whitelist WHERE id=?", (entry_id,))
        await db.commit()
        return cur.rowcount > 0


async def clear_whitelist() -> int:
    """Drop every row from device_whitelist and return the deleted count.
    Backs the 'Clear all' icon in the Settings > whitelist panel; the
    temp / silenced list lives in its own table and isn't touched."""
    async with _connect() as db:
        cur = await db.execute("DELETE FROM device_whitelist")
        await db.commit()
        return cur.rowcount or 0


async def list_preserved_devices(kind: str | None = None) -> list[dict]:
    """Whitelisted devices archived from deleted locations. Shape mirrors
    devices_at_location for easy reuse on the Devices tab."""
    sql = "SELECT * FROM preserved_devices"
    args: tuple = ()
    if kind:
        sql += " WHERE kind=?"
        args = (kind,)
    sql += " ORDER BY last_seen DESC"
    async with _connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(sql, args) as cur:
            rows = [dict(r) for r in await cur.fetchall()]
    for r in rows:
        try:
            r["details"] = json.loads(r.pop("details_json"))
        except (TypeError, ValueError):
            r["details"] = {}
        r["tracker_type"] = classify_tracker(r.get("kind", ""), r["details"])
    return rows


async def clear_preserved_devices() -> int:
    async with _connect() as db:
        cur = await db.execute("DELETE FROM preserved_devices")
        await db.commit()
        return cur.rowcount or 0


# Temporal-correlation window: when MAC X stops being seen and MAC Y starts
# within this many seconds AND they share a BLE signature, the pair is a
# "high-confidence" rotation match. 20 minutes covers the typical 15-minute
# RPA rotation interval with slack for scan jitter.
TEMPORAL_LINK_WINDOW_S = 1200


def _parse_iso(s: str | None):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def _temporal_close(this_first, this_last, sib_first, sib_last,
                    window_s: float = TEMPORAL_LINK_WINDOW_S) -> bool:
    """True iff one device disappeared shortly before the other appeared
    (i.e., the lifetimes are adjacent rather than overlapping). Used to
    promote a signature-only link to high-confidence when the temporal
    pattern matches a rotation event."""
    if not (this_first and this_last and sib_first and sib_last):
        return False
    gap_after = (sib_first - this_last).total_seconds()
    gap_before = (this_first - sib_last).total_seconds()
    return (0 <= gap_after <= window_s) or (0 <= gap_before <= window_s)


async def devices_at_location(location_id: int, kind: str | None = None) -> list[dict]:
    """Per-location device rows with BLE rotating-MAC linkage info.

    For BLE rows that share a signature with other devices, this attaches
    `linked_count` (total signature-matching siblings) and
    `linked_device_ids` (their MACs). When at least one sibling's lifetime
    is adjacent to this row's (within TEMPORAL_LINK_WINDOW_S), that
    sibling is also added to `linked_recent_ids` and the link is treated
    as high-confidence — that's the "MAC X just rotated to MAC Y" signal.
    Wifi rows have signature NULL and stay unlinked."""
    sql = "SELECT * FROM devices WHERE location_id=?"
    args: tuple = (location_id,)
    if kind:
        sql += " AND kind=?"
        args = (location_id, kind)
    sql += " ORDER BY best_rssi DESC"
    async with _connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(sql, args) as cur:
            rows = [dict(r) for r in await cur.fetchall()]

        # Collect non-null signatures we need to look up siblings for.
        sigs = sorted({r["signature"] for r in rows if r.get("signature")})
        siblings_by_sig: dict[str, list[dict]] = {}
        if sigs:
            placeholders = ",".join("?" * len(sigs))
            async with db.execute(
                f"SELECT signature, device_id, first_seen, last_seen, best_rssi "
                f"FROM devices WHERE signature IN ({placeholders})",
                tuple(sigs),
            ) as cur:
                for sig, did, first_seen, last_seen, best_rssi in await cur.fetchall():
                    siblings_by_sig.setdefault(sig, []).append({
                        "device_id": did,
                        "first_seen": first_seen,
                        "last_seen": last_seen,
                        "best_rssi": best_rssi,
                    })

    for r in rows:
        r["details"] = json.loads(r.pop("details_json"))
        r["tracker_type"] = classify_tracker(r.get("kind", ""), r["details"])
        sig = r.get("signature")
        siblings = [s for s in siblings_by_sig.get(sig, [])
                    if s["device_id"] != r["device_id"]] if sig else []
        r["linked_device_ids"] = [s["device_id"] for s in siblings]
        r["linked_count"] = len(siblings)

        # Promote to high-confidence when a sibling's lifetime is adjacent
        # to this row's (rotation-style hand-off rather than overlap).
        this_first = _parse_iso(r.get("first_seen"))
        this_last = _parse_iso(r.get("last_seen"))
        recent: list[str] = []
        for s in siblings:
            if _temporal_close(this_first, this_last,
                               _parse_iso(s["first_seen"]),
                               _parse_iso(s["last_seen"])):
                recent.append(s["device_id"])
        r["linked_recent_ids"] = recent
        r["linked_recent_count"] = len(recent)
    return rows
