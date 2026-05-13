"""Alert evaluation.

Each scan hit (wifi or bluetooth, after the orchestrator has tagged it
with the current sensor location) is run through every enabled rule.
A match emits one alert_events row, with a per-(rule, device) cooldown
to keep the feed sane while a strong target sits next to the sensor.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any

import database as db
from config import settings_store
from services.location_manager import location_manager

log = logging.getLogger(__name__)


class AlertService:
    def __init__(self) -> None:
        self._rules: list[dict] = []
        self._rules_loaded = False
        # Latched (rule_id, device_id_lower) pairs — once an alert fires
        # for a pair, it's added here and won't fire again until cleared
        # via clear_alert_pair (per pair) or clear_all_latches (mass).
        # Persisted to the alert_events.cleared column so latches survive
        # process restarts.
        self._latched: set[tuple[int, str]] = set()
        # location_id -> created_at epoch seconds (cached; created_at never changes)
        self._location_created: dict[int, float] = {}
        # Cached whitelist as (kind, lowercased target). Targets match either
        # exactly or as a prefix (so "aa:bb:cc" whitelists the whole OUI).
        self._whitelist: list[tuple[str, str]] = []

    async def load_rules(self) -> None:
        self._rules = await db.list_alert_rules()
        self._rules_loaded = True

    async def load_whitelist(self) -> None:
        # Permanent + temporary whitelists are merged for matching — the
        # temp set is auto-populated by the baseline-scan flow and lives
        # until "Delete all locations" wipes it.
        rows = await db.list_whitelist_combined()
        self._whitelist = [(r["kind"], (r["device_id"] or "").lower()) for r in rows]

    async def load_latches(self) -> None:
        self._latched = set(await db.list_latched_pairs())

    async def reload(self) -> None:
        """Call after any rule or whitelist mutation so the next scan
        uses fresh state."""
        await self.load_rules()
        await self.load_whitelist()
        await self.load_latches()

    async def unlatch(self, rule_id: int, device_id: str) -> int:
        """Clear the latch for one (rule, device) pair so future matches
        fire again. Returns the number of event rows marked cleared."""
        n = await db.clear_alert_pair(rule_id, device_id)
        self._latched.discard((rule_id, (device_id or "").lower()))
        return n

    async def unlatch_all(self) -> int:
        """Clear every active latch without deleting alert history."""
        n = await db.clear_all_latches()
        self._latched.clear()
        return n

    def is_whitelisted(self, kind: str, device_id_l: str) -> bool:
        for k, target in self._whitelist:
            if k != kind or not target:
                continue
            if device_id_l == target or device_id_l.startswith(target):
                return True
        return False

    async def evaluate(
        self,
        device_kind: str,           # 'wifi' | 'bluetooth'
        device_id: str,             # BSSID or MAC, lowercase
        rssi: int,
        location_id: int | None,
        details: dict,
        is_new: bool = False,
    ) -> list[int]:
        """Run all enabled rules against a single device sighting. Returns
        a list of newly-inserted alert_events ids (after latch filtering)."""
        if not self._rules_loaded:
            await self.load_rules()
            await self.load_whitelist()
            await self.load_latches()
        # Whitelist gate: silently skip every rule for whitelisted devices.
        if self.is_whitelisted(device_kind, (device_id or "").lower()):
            return []
        if not self._rules:
            return []

        emitted: list[int] = []
        device_id_l = (device_id or "").lower()
        ssid_or_name = (details.get("ssid") or details.get("name") or "")
        vendor = (details.get("vendor") or "")
        location_age_s: float | None = None  # lazily fetched

        for rule in self._rules:
            if not rule.get("enabled"):
                continue
            kind_filter = rule.get("kind")
            if kind_filter and kind_filter != device_kind:
                continue
            loc_filter = rule.get("location_id")
            if loc_filter == -1:
                # "Active location" sentinel — resolve to whatever loc the
                # sensor is currently parked at. If no active loc (GPS lost,
                # or sensor moving between bubbles), the rule has no scope
                # and is silently skipped.
                active = location_manager.active_id
                if active is None or active != location_id:
                    continue
            elif loc_filter is not None and loc_filter != location_id:
                continue

            mt = rule.get("match_type")
            if mt == "new_device":
                if not is_new:
                    continue
                # match_value is the establishment-time threshold in seconds.
                # 0 means fire immediately; >0 means location must have existed
                # for at least that many seconds before new-device alerts arm.
                try:
                    establishment_s = max(0, int((rule.get("match_value") or "0").strip()))
                except ValueError:
                    establishment_s = 0
                if establishment_s > 0:
                    if location_age_s is None:
                        location_age_s = await self._location_age_seconds(location_id)
                    if location_age_s < establishment_s:
                        continue
            elif mt == "cross_location":
                # match_value: "N/M" — fire when device appears in >= M of the last N locations.
                n_locs, min_m = _parse_cross_location_value(rule.get("match_value") or "")
                if n_locs is None:
                    continue
                count = await db.count_device_in_recent_locations(device_kind, device_id_l, n_locs)
                if count < min_m:
                    continue
                # Stash the count so it lands in the alert's stored details for context.
                details = {**details, "_cross_location_count": count, "_cross_location_n": n_locs}
            elif mt == "persistent_companion":
                # match_value: "M/H" — device (or BLE-signature siblings) seen at
                # ≥ M distinct locations within the last H hours. Strong signal
                # of a follower / "stalker tag" rather than coincidental overlap.
                min_locs, window_h = _parse_companion_value(rule.get("match_value") or "")
                if min_locs is None:
                    continue
                count = await db.count_companion_locations(device_kind, device_id_l, window_h)
                if count < min_locs:
                    continue
                details = {
                    **details,
                    "_companion_count": count,
                    "_companion_window_h": window_h,
                }
            elif not _matches(rule, device_id_l, ssid_or_name, vendor, rssi):
                continue

            # Compound rule: every extra condition must also match. Conditions
            # are passed straight through to _matches since they share the
            # match_type/match_value shape; types here are restricted at the
            # API to the simple value-based four (no stateful types).
            extras = rule.get("extra_conditions") or []
            if extras and not all(
                _matches(c, device_id_l, ssid_or_name, vendor, rssi)
                for c in extras
            ):
                continue

            # Latch: a (rule, device) pair only fires once per latch
            # cycle. Persists in alert_events.cleared so latches survive
            # restarts. The user clears via /api/alerts/clear or the
            # per-row button in the live feed.
            key = (rule["id"], device_id_l)
            if key in self._latched:
                continue
            self._latched.add(key)

            event_id = await db.insert_alert_event(
                rule_id=rule["id"], location_id=location_id,
                device_kind=device_kind, device_id=device_id, rssi=rssi,
                details=details,
            )
            log.info(
                "ALERT '%s' (rule %d) %s/%s rssi=%d", rule["name"], rule["id"],
                device_kind, device_id, rssi,
            )
            emitted.append(event_id)

            # Fire-and-forget Discord notification, if both the rule opts in
            # and the webhook is configured (checked inside _dispatch_discord).
            if rule.get("notify_discord"):
                asyncio.create_task(_dispatch_discord(
                    rule=rule, device_kind=device_kind, device_id=device_id,
                    rssi=rssi, location_id=location_id, details=details,
                ))

        return emitted

    async def _location_age_seconds(self, location_id: int | None) -> float:
        """Seconds since the given location's row was created. Cached per id."""
        if location_id is None:
            return 0.0
        cached = self._location_created.get(location_id)
        if cached is None:
            ts = await db.get_location_created_at(location_id)
            if ts is None:
                return 0.0
            try:
                cached = datetime.fromisoformat(ts).timestamp()
            except ValueError:
                return 0.0
            self._location_created[location_id] = cached
        return max(0.0, time.time() - cached)


# ── Discord webhook ───────────────────────────────────────────────
_KIND_COLOR = {
    "wifi": 0x5cd1ff,        # cyan
    "bluetooth": 0xb8a3ff,   # purple
}
_DEFAULT_COLOR = 0xff6b6b    # red

# Cache failures briefly so a misconfigured webhook doesn't hammer Discord.
_webhook_failure_until: float = 0.0
_WEBHOOK_BACKOFF_S = 60.0


async def _dispatch_discord(
    rule: dict, device_kind: str, device_id: str, rssi: int,
    location_id: int | None, details: dict,
) -> None:
    """Post a themed embed to the configured Discord webhook. No-op if
    unset. Pulls cross-location aggregates, BLE signature siblings, and
    location metadata so the embed has everything an operator needs to
    decide whether to act on the alert without opening the UI."""
    global _webhook_failure_until
    try:
        s = await settings_store.load()
        url = (s.discord_webhook_url or "").strip()
        if not url:
            return
        if time.monotonic() < _webhook_failure_until:
            return

        # Enrichment — best-effort. Missing data falls back to the basic
        # embed; no individual lookup is allowed to break the dispatch.
        try:
            loc_info = (await db.get_location_summary(location_id)
                         if location_id is not None else None)
        except Exception:
            loc_info = None
        try:
            dev_info = await db.get_device_summary(device_kind, device_id)
        except Exception:
            dev_info = None
        siblings: list[str] = []
        if dev_info and dev_info.get("signature"):
            try:
                siblings = await db.get_signature_siblings(
                    dev_info["signature"], exclude_device_id=device_id,
                )
            except Exception:
                siblings = []
        tracker_type = db.classify_tracker(device_kind, details)
        # 24-hour companion-location count — strong signal for "following".
        # Uses the same BLE-signature aggregation as the persistent_companion
        # rule, so rotating-MAC siblings collapse to one count.
        try:
            recent_24h = await db.count_companion_locations(
                device_kind, device_id, window_hours=24,
            )
        except Exception:
            recent_24h = 0
        # How many alert_events have already fired on this (rule, device)
        # pair across the system's history? Lets the operator see at a
        # glance whether this is "first time" or "this keeps happening".
        try:
            prior_fires = await db.count_alert_events_for_rule_device(
                rule_id=rule["id"], device_kind=device_kind, device_id=device_id,
            )
        except Exception:
            prior_fires = 0
        context = {
            "location": loc_info,
            "device_summary": dev_info,
            "linked_aliases": siblings,
            "tracker_type": tracker_type,
            "recent_locations_24h": recent_24h,
            "prior_fires": prior_fires,
        }

        payload = build_discord_payload(
            rule=rule, device_kind=device_kind, device_id=device_id, rssi=rssi,
            location_id=location_id, details=details, username=s.discord_username,
            context=context,
        )
        await asyncio.to_thread(_post_webhook_sync, url, payload)
    except Exception as e:
        _webhook_failure_until = time.monotonic() + _WEBHOOK_BACKOFF_S
        log.warning("discord webhook failed (suppressing for %ds): %s",
                    int(_WEBHOOK_BACKOFF_S), e)


def _post_webhook_sync(url: str, payload: dict) -> None:
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=body,
        headers={
            "Content-Type": "application/json",
            "User-Agent": "Gjallarhorn/0.1",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        # Discord returns 204 No Content on success
        if resp.status >= 400:
            raise urllib.error.HTTPError(
                url, resp.status, f"webhook returned {resp.status}", resp.headers, None
            )


def build_discord_payload(
    rule: dict, device_kind: str, device_id: str, rssi: int,
    location_id: int | None, details: dict, username: str = "Gjallarhorn",
    context: dict | None = None,
) -> dict:
    """Compose a Discord embed for one alert event. `context` holds optional
    enrichments from the dispatch path (location summary, device aggregates,
    BLE signature siblings, tracker classification) — all best-effort, the
    embed degrades gracefully when any are missing."""
    context = context or {}
    color = _KIND_COLOR.get(device_kind, _DEFAULT_COLOR)
    match_type = rule.get("match_type") or "?"
    match_value = (rule.get("match_value") or "").strip()

    name_or_ssid = details.get("ssid") or details.get("name") or ""
    vendor = details.get("vendor") or ""
    tracker_type = context.get("tracker_type")

    fields: list[dict] = [
        {"name": "Device", "value": f"`{device_id}`", "inline": True},
        {"name": "RSSI",   "value": f"{rssi} dBm",   "inline": True},
        {"name": "Kind",   "value": _KIND_LABELS.get(device_kind, device_kind),
         "inline": True},
    ]
    if name_or_ssid:
        fields.append({"name": "Name / SSID",
                       "value": str(name_or_ssid)[:1000], "inline": True})
    if vendor:
        fields.append({"name": "Vendor", "value": str(vendor), "inline": True})
    if tracker_type:
        fields.append({"name": "Tracker class",
                       "value": _TRACKER_LABELS.get(tracker_type, tracker_type),
                       "inline": True})

    # Per-kind technical context.
    if device_kind == "wifi":
        ch = details.get("channel")
        band = details.get("band")
        if ch or band:
            fields.append({"name": "Channel",
                           "value": f"{ch or '?'}{f' ({band})' if band else ''}",
                           "inline": True})
        freq = details.get("frequency_mhz")
        if freq:
            fields.append({"name": "Frequency",
                           "value": f"{freq} MHz", "inline": True})
        enc = details.get("encryption")
        cipher = details.get("cipher")
        if enc or cipher:
            fields.append({"name": "Encryption",
                           "value": f"{enc or '?'}{f' / {cipher}' if cipher else ''}",
                           "inline": True})
        auth = details.get("auth")
        if auth:
            fields.append({"name": "Auth", "value": str(auth)[:100],
                           "inline": True})
        caps = details.get("capabilities")
        if caps:
            shown = str(caps)
            if len(shown) > 60:
                shown = shown[:57] + "…"
            fields.append({"name": "Capabilities",
                           "value": shown, "inline": True})
        beacon = details.get("beacon_interval_ms")
        if beacon:
            fields.append({"name": "Beacon",
                           "value": f"{beacon} ms", "inline": True})
    elif device_kind == "bluetooth":
        addr_type = details.get("address_type")
        if addr_type:
            fields.append({"name": "Address type",
                           "value": str(addr_type), "inline": True})
        tx_power = details.get("tx_power")
        if tx_power is not None:
            fields.append({"name": "TX power",
                           "value": f"{tx_power} dBm", "inline": True})
        connectable = details.get("is_connectable")
        if connectable is not None:
            fields.append({"name": "Connectable",
                           "value": "yes" if connectable else "no",
                           "inline": True})
        mfg = details.get("manufacturer_data") or {}
        if isinstance(mfg, dict) and mfg:
            ids = ", ".join(_format_company_id(k) for k in sorted(mfg.keys(),
                            key=lambda x: int(x)))
            fields.append({"name": "Manufacturer",
                           "value": ids[:1000], "inline": True})
            # Preview the first manufacturer-data payload (16 hex bytes max)
            # so operators can eyeball the protocol-level fingerprint.
            try:
                first_key = sorted(mfg.keys(), key=lambda x: int(x))[0]
                first_val = str(mfg[first_key] or "")
                if first_val:
                    preview = first_val[:32]
                    if len(first_val) > 32:
                        preview += "…"
                    fields.append({"name": "Mfg payload",
                                   "value": f"`{preview}`", "inline": True})
            except (ValueError, IndexError):
                pass
        svcs = details.get("service_uuids") or []
        if isinstance(svcs, list) and svcs:
            shown = ", ".join(str(u) for u in svcs[:3])
            if len(svcs) > 3:
                shown += f" (+{len(svcs) - 3})"
            fields.append({"name": "Services",
                           "value": shown[:1000], "inline": True})
        appearance = details.get("appearance")
        if appearance is not None:
            fields.append({"name": "Appearance",
                           "value": str(appearance), "inline": True})
    elif device_kind == "wifi_client":
        ssids = details.get("ssids") or []
        if isinstance(ssids, list) and ssids:
            named = [s for s in ssids if s]
            shown = ", ".join(named[:3]) or "(wildcard)"
            if len(named) > 3:
                shown += f" (+{len(named) - 3} more)"
            fields.append({"name": "Probed SSIDs",
                           "value": shown[:1000], "inline": True})
        ch = details.get("channel")
        if ch is not None:
            fields.append({"name": "Channel",
                           "value": str(ch), "inline": True})
        if details.get("randomized"):
            fields.append({"name": "MAC type",
                           "value": "randomized (privacy)", "inline": True})

    # Location enrichment — label and coordinates with an OSM link.
    loc = context.get("location")
    embed_url: str | None = None
    if loc:
        loc_label = loc.get("label") or f"#{loc['id']}"
        loc_value = loc_label
        if str(loc.get("id")) and (loc.get("label") or "").strip():
            loc_value = f"{loc_label} (#{loc['id']})"
        fields.append({"name": "Location", "value": loc_value, "inline": True})
        if loc.get("lat") is not None and loc.get("lon") is not None:
            lat = float(loc["lat"]); lon = float(loc["lon"])
            embed_url = f"https://www.openstreetmap.org/?mlat={lat}&mlon={lon}#map=18/{lat}/{lon}"
            fields.append({
                "name": "Coordinates",
                "value": f"[{lat:.5f}, {lon:.5f}]({embed_url})",
                "inline": True,
            })
    elif location_id is not None:
        fields.append({"name": "Location",
                       "value": f"#{location_id}", "inline": True})

    # Device-lifetime aggregates from db.get_device_summary.
    dev = context.get("device_summary")
    if dev:
        if dev.get("first_seen"):
            fields.append({"name": "First seen",
                           "value": _short_time(dev["first_seen"]),
                           "inline": True})
        if dev.get("last_seen"):
            fields.append({"name": "Last seen",
                           "value": _short_time(dev["last_seen"]),
                           "inline": True})
        if dev.get("seen_count"):
            fields.append({"name": "Total observations",
                           "value": str(dev["seen_count"]),
                           "inline": True})
        if dev.get("location_count"):
            fields.append({"name": "Locations (all-time)",
                           "value": str(dev["location_count"]),
                           "inline": True})

    # 24h companion-locations: how broadly the device (and its rotating-MAC
    # siblings) has covered in the last day. Surfaces "this is following me
    # right now" vs "matched a stale rule".
    recent_24h = context.get("recent_locations_24h")
    if recent_24h is not None and recent_24h >= 2:
        fields.append({
            "name": "Recent (24h)",
            "value": f"{recent_24h} locations",
            "inline": True,
        })

    # Latch history — how many times this exact (rule, device) pair has
    # ever fired. Distinguishes "first time" from "this keeps happening".
    prior_fires = context.get("prior_fires") or 0
    if prior_fires >= 1:
        fields.append({
            "name": "Latch history",
            "value": f"{prior_fires} fire{'s' if prior_fires != 1 else ''} "
                     f"on this pair",
            "inline": True,
        })

    # BLE rotating-MAC siblings — when the alert fires on a private MAC,
    # show what other addresses the same physical device has used so the
    # operator can correlate.
    siblings = context.get("linked_aliases") or []
    if siblings:
        shown_aliases = ", ".join(f"`{m}`" for m in siblings[:6])
        if len(siblings) > 6:
            shown_aliases += f" (+{len(siblings) - 6} more)"
        fields.append({
            "name": f"Linked aliases ({len(siblings)})",
            "value": shown_aliases[:1000],
            "inline": False,
        })

    # Stateful match-type extras (existing behavior, preserved).
    if match_type == "cross_location":
        n = details.get("_cross_location_n")
        c = details.get("_cross_location_count")
        if n is not None and c is not None:
            fields.append({
                "name": "Cross-location",
                "value": f"in {c} of last {n}", "inline": True,
            })
    if match_type == "persistent_companion":
        c = details.get("_companion_count")
        h = details.get("_companion_window_h")
        if c is not None and h is not None:
            fields.append({
                "name": "Persistent companion",
                "value": f"{c} locations in last {h}h", "inline": True,
            })

    # Description: include match_value so operators see *what* triggered,
    # the rule's scope, and any tracker classification up front.
    desc_parts = [f"`{match_type}` matched on **{_KIND_LABELS.get(device_kind, device_kind)}**"]
    if match_value:
        desc_parts.append(f"value: `{match_value}`")
    rule_loc = rule.get("location_id")
    if rule_loc is None:
        scope = "any location"
    elif rule_loc == -1:
        scope = "active location"
    else:
        scope = f"location #{rule_loc}"
    desc_parts.append(f"scope: {scope}")
    if rule.get("extra_conditions"):
        n_extra = len(rule["extra_conditions"])
        desc_parts.append(
            f"+{n_extra} compound condition{'s' if n_extra != 1 else ''}"
        )
    if tracker_type:
        desc_parts.append(f"⚠ classified as **{_TRACKER_LABELS.get(tracker_type, tracker_type)}**")
    description = " · ".join(desc_parts)

    embed: dict = {
        "title": f"⚡ {rule.get('name', 'Alert')}",
        "description": description,
        "color": color,
        "fields": fields[:25],   # Discord caps embed.fields at 25
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "footer": {"text": f"rule #{rule.get('id', '?')} · {match_type}"},
    }
    if embed_url:
        embed["url"] = embed_url
    return {"username": username, "embeds": [embed]}


_KIND_LABELS = {
    "wifi": "WiFi AP",
    "bluetooth": "Bluetooth",
    "wifi_client": "WiFi client (probe)",
}
_TRACKER_LABELS = {
    "airtag": "AirTag / FindMy",
    "tile": "Tile",
    "samsung_smarttag": "Samsung SmartTag",
}
# Bluetooth SIG company IDs we mention by name in alert embeds. Anything
# not in this set is rendered as `0xNNNN`. Keeps embeds readable without
# pulling in the full SIG list.
_BT_COMPANY_NAMES = {
    6: "Microsoft",
    76: "Apple",
    117: "Samsung",
    224: "Google",
    301: "Nordic Semi",
    420: "Bose",
    343: "Garmin",
    1660: "Tile",
    1118: "Anker",
    2257: "Fitbit",
}


def _format_company_id(raw_key) -> str:
    try:
        i = int(raw_key)
    except (TypeError, ValueError):
        return str(raw_key)
    name = _BT_COMPANY_NAMES.get(i)
    return f"{name} (0x{i:04X})" if name else f"0x{i:04X}"


def _short_time(iso: str) -> str:
    """Render an ISO timestamp as a Discord-friendly short form. Naive
    times (the local-time strings the rest of the app emits) round-trip
    fine; if the parser fails just pass the string through."""
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return str(iso)
    return dt.strftime("%Y-%m-%d %H:%M")


def _parse_companion_value(value: str) -> tuple[int | None, int]:
    """Parse 'M/H' into (min_locations, window_hours). M defaults to 2,
    H defaults to 4. Returns (None, 0) if unparsable. Both ≥ 1 required."""
    parts = [p.strip() for p in (value or "").split("/")]
    try:
        m = int(parts[0]) if parts and parts[0] else 2
        h = int(parts[1]) if len(parts) > 1 and parts[1] else 4
    except (ValueError, IndexError):
        return None, 0
    if m < 2 or h < 1:
        return None, 0
    return m, h


def _parse_cross_location_value(value: str) -> tuple[int | None, int]:
    """Parse 'N/M' into (n_locations, min_matches). Returns (None, 0) if invalid.

    M defaults to 2 if only N is given. Both must be >= 2 and M <= N.
    """
    parts = [p.strip() for p in (value or "").split("/")]
    try:
        n = int(parts[0])
        m = int(parts[1]) if len(parts) > 1 and parts[1] else 2
    except (ValueError, IndexError):
        return None, 0
    if n < 2 or m < 2 or m > n:
        return None, 0
    return n, m


def _matches(rule: dict, device_id_l: str, name_or_ssid: str, vendor: str, rssi: int) -> bool:
    mt = rule.get("match_type")
    mv = (rule.get("match_value") or "").strip()
    if not mv:
        return False

    if mt == "device_id":
        # Exact full id, or a prefix (e.g. "aa:bb:cc" matches the OUI).
        target = mv.lower()
        return device_id_l == target or device_id_l.startswith(target)

    if mt == "name_contains":
        return mv.lower() in (name_or_ssid or "").lower()

    if mt == "vendor_contains":
        return mv.lower() in (vendor or "").lower()

    if mt == "rssi_above":
        try:
            threshold = int(mv)
        except ValueError:
            return False
        # rssi is dBm (negative). "above" = stronger = closer to 0.
        return rssi >= threshold

    return False


alert_service = AlertService()
