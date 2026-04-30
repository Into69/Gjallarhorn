"""OUI (Organizationally Unique Identifier) lookup.

Pulls the IEEE registries (MA-L, MA-M, MA-S) into SQLite and resolves
a MAC address (or BSSID) to a vendor name. Lookups are served from an
in-memory cache so the scanners don't hit the DB per device.

The IEEE serves CSV registries at:
  - https://standards-oui.ieee.org/oui/oui.csv          (MA-L, 24-bit)
  - https://standards-oui.ieee.org/oui28/mam.csv        (MA-M, 28-bit)
  - https://standards-oui.ieee.org/oui36/oui36.csv      (MA-S, 36-bit)

For lookup, the longest prefix that matches wins (MA-S beats MA-M beats MA-L)
because the longer registries carve out subspaces of shorter ones.
"""
from __future__ import annotations

import asyncio
import csv
import io
import logging
import urllib.error
import urllib.request
from datetime import datetime
from typing import Optional

import database as db

log = logging.getLogger(__name__)


REGISTRY_URLS: list[tuple[str, str, int]] = [
    # (registry, url, prefix_hex_len)
    ("MA-L", "https://standards-oui.ieee.org/oui/oui.csv", 6),
    ("MA-M", "https://standards-oui.ieee.org/oui28/mam.csv", 7),
    ("MA-S", "https://standards-oui.ieee.org/oui36/oui36.csv", 9),
]

USER_AGENT = "Gjallarhorn/0.1 (+https://example.invalid)"


class OUIService:
    def __init__(self) -> None:
        # cache keyed by prefix length -> {prefix: (registry, organization)}
        self._cache: dict[int, dict[str, tuple[str, str]]] = {}
        self._loaded = False
        self._lock = asyncio.Lock()
        self._last_updated: Optional[datetime] = None
        self._updating = False

    @property
    def updating(self) -> bool:
        return self._updating

    @property
    def last_updated(self) -> Optional[datetime]:
        return self._last_updated

    async def status(self) -> dict:
        per_registry = await db.oui_counts_by_registry()
        total = sum(per_registry.values())
        # DB file size in bytes (the whole sqlite file — not just the oui table,
        # but the OUI rows dominate it after an update)
        try:
            db_size = db.DB_PATH.stat().st_size
        except OSError:
            db_size = None
        return {
            "count": total,
            "per_registry": per_registry,
            "loaded": self._loaded,
            "updating": self._updating,
            "last_updated": self._last_updated.isoformat() if self._last_updated else None,
            "db_file_bytes": db_size,
            "db_file_path": str(db.DB_PATH),
        }

    async def ensure_loaded(self) -> None:
        if self._loaded:
            return
        async with self._lock:
            if self._loaded:
                return
            rows = await db.load_all_oui()
            self._cache = {6: {}, 7: {}, 9: {}}
            for prefix, registry, org in rows:
                self._cache.setdefault(len(prefix), {})[prefix] = (registry, org)
            self._loaded = True
            ts = await db.get_setting("oui_last_updated")
            if ts:
                try:
                    self._last_updated = datetime.fromisoformat(ts)
                except ValueError:
                    pass
            log.info("OUI cache loaded: %d entries", sum(len(v) for v in self._cache.values()))

    @staticmethod
    def _normalize(mac: str) -> str:
        return "".join(c for c in mac if c.isalnum()).upper()

    async def lookup(self, mac: str) -> Optional[str]:
        """Return vendor organization, or None if unknown."""
        await self.ensure_loaded()
        norm = self._normalize(mac)
        if len(norm) < 6:
            return None
        # Longest-prefix-first
        for L in (9, 7, 6):
            bucket = self._cache.get(L)
            if not bucket:
                continue
            entry = bucket.get(norm[:L])
            if entry:
                return entry[1]
        return None

    async def update_from_ieee(self) -> dict:
        """Download all three IEEE registries and replace the table. Returns a summary."""
        if self._updating:
            return {"ok": False, "error": "update already in progress"}
        self._updating = True
        try:
            rows: list[tuple[str, str, str, Optional[str]]] = []
            per_registry: dict[str, int] = {}
            for registry, url, prefix_len in REGISTRY_URLS:
                try:
                    body = await asyncio.to_thread(_http_get, url)
                except urllib.error.URLError as e:
                    log.warning("OUI fetch failed for %s: %s", registry, e)
                    per_registry[registry] = -1
                    continue
                parsed = list(_parse_csv(body, registry, prefix_len))
                per_registry[registry] = len(parsed)
                rows.extend(parsed)
                log.info("OUI: %s -> %d rows", registry, len(parsed))

            if not rows:
                return {"ok": False, "error": "no rows fetched", "per_registry": per_registry}

            inserted = await db.replace_oui_entries(rows)
            self._last_updated = datetime.utcnow()
            await db.set_setting("oui_last_updated", self._last_updated.isoformat())
            # rebuild cache
            self._loaded = False
            await self.ensure_loaded()
            return {
                "ok": True,
                "inserted": inserted,
                "per_registry": per_registry,
                "last_updated": self._last_updated.isoformat(),
            }
        finally:
            self._updating = False


def _http_get(url: str, timeout: float = 60.0) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = resp.read()
    # IEEE serves UTF-8 with occasional latin-1 stragglers; be permissive
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("latin-1", errors="replace")


def _parse_csv(body: str, registry: str, prefix_len: int):
    """IEEE CSV columns: Registry, Assignment, Organization Name, Organization Address."""
    reader = csv.reader(io.StringIO(body))
    header = next(reader, None)
    if not header:
        return
    # Locate columns by name (defensive against ordering changes)
    cols = {c.strip().lower(): i for i, c in enumerate(header)}
    i_assign = cols.get("assignment", 1)
    i_org = cols.get("organization name", 2)
    i_addr = cols.get("organization address", 3)
    for row in reader:
        if len(row) <= max(i_assign, i_org):
            continue
        prefix = "".join(c for c in row[i_assign] if c.isalnum()).upper()
        if len(prefix) != prefix_len:
            continue
        org = (row[i_org] or "").strip()
        addr = (row[i_addr] or "").strip() if len(row) > i_addr else None
        if not org:
            continue
        yield (prefix, registry, org, addr)


oui_service = OUIService()
