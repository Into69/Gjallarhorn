"""PDF location report generator.

Lays out a multi-page report covering every sensor location: an overview
map of all sites, per-location detail pages with a mini-map and top
device list, a side-by-side device-count comparison, and a 'common
devices' table for hardware seen at multiple locations.
"""
from __future__ import annotations

import io
import logging
from datetime import datetime
from typing import Any

from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import (
    Image, PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle,
)

import database as db
from services.map_cache import render_map as _render_map_png

log = logging.getLogger(__name__)

# How many follower / cross-location entries to include in the report.
_MAX_COMMON_DEVICES = 60


def _styles() -> dict[str, ParagraphStyle]:
    base = getSampleStyleSheet()
    out: dict[str, ParagraphStyle] = {}
    out["title"] = ParagraphStyle(
        "title", parent=base["Title"], fontSize=22, leading=26, spaceAfter=4,
    )
    out["subtitle"] = ParagraphStyle(
        "subtitle", parent=base["Normal"], fontSize=10, textColor=colors.grey,
        spaceAfter=20,
    )
    out["h1"] = ParagraphStyle(
        "h1", parent=base["Heading1"], fontSize=16, leading=20, spaceBefore=8,
        spaceAfter=8, textColor=colors.HexColor("#1f4060"),
    )
    out["h2"] = ParagraphStyle(
        "h2", parent=base["Heading2"], fontSize=13, leading=16, spaceBefore=6,
        spaceAfter=4, textColor=colors.HexColor("#2a4d70"),
    )
    out["body"] = base["BodyText"]
    out["mono"] = ParagraphStyle(
        "mono", parent=base["Code"], fontSize=8, leading=10,
    )
    out["caption"] = ParagraphStyle(
        "caption", parent=base["Normal"], fontSize=9, textColor=colors.grey,
        spaceAfter=8,
    )
    return out


async def _render_map_image(
    points: list[tuple[float, float, str]],   # (lat, lon, color_hex)
    *,
    width_px: int = 900,
    height_px: int = 540,
    zoom: int | None = None,
) -> Image | Paragraph:
    """Render an OSM map for the given points using the cached tile store.
    Falls back to a text placeholder if the renderer raises — a missing
    map shouldn't break a whole report."""
    styles = _styles()
    if not points:
        return Paragraph("<i>No locations to plot.</i>", styles["caption"])
    try:
        png_bytes = await _render_map_png(
            points, width_px=width_px, height_px=height_px, zoom=zoom,
        )
        buf = io.BytesIO(png_bytes)
        # scale to ~6.5 inches wide, preserving aspect
        target_w = 6.5 * inch
        scale = target_w / width_px
        return Image(buf, width=target_w, height=height_px * scale)
    except Exception as e:
        log.warning("map render failed: %s", e)
        return Paragraph(
            f"<i>Map unavailable ({type(e).__name__}: {e}).</i>",
            styles["caption"],
        )


def _fmt_time(iso: str | None) -> str:
    if not iso:
        return "—"
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return iso


# Reusable cell paragraph styles. Using Paragraph (rather than raw strings)
# inside Table cells lets text wrap to multiple lines so nothing gets clipped
# when content exceeds the column width. wordWrap="CJK" ensures even long
# unbroken tokens (BSSIDs, run-on SSIDs) wrap at the cell edge.
_CELL_STYLE = ParagraphStyle(
    "rcell", fontName="Helvetica", fontSize=8, leading=10, wordWrap="CJK",
)
_CELL_MONO_STYLE = ParagraphStyle(
    "rcell_mono", fontName="Courier", fontSize=7.5, leading=9.5, wordWrap="CJK",
)


def _cell(text: Any, *, mono: bool = False) -> Paragraph:
    """Wrap a value in a Paragraph so the table cell auto-grows vertically."""
    s = "" if text is None else str(text)
    s = s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return Paragraph(s, _CELL_MONO_STYLE if mono else _CELL_STYLE)


def _group_wifi_by_ap_prefix(devices: list[dict]) -> list[dict]:
    """Mirror of the JS groupWifiByApPrefix used on the Devices tab. Aggregates
    wifi BSSIDs that share the same first 5 octets (same physical AP advertising
    on multiple bands/SSIDs) into a single row. Non-wifi rows pass through.
    Adds _merged_count, _members, _merged_ssids, _merged_vendors fields."""
    groups: dict[str, dict] = {}
    out: list[dict] = []
    for d in devices:
        kind = d.get("kind")
        device_id = d.get("device_id") or ""
        if kind != "wifi" or len(device_id) < 17:
            out.append(d)
            continue
        prefix = device_id[:14].lower()  # "aa:bb:cc:dd:ee"
        g = groups.get(prefix)
        if g is None:
            g = {
                **d,
                "details": dict(d.get("details") or {}),
                "_members": [device_id],
                "_merged_count": 1,
                "_merged_ssids": [],
                "_merged_vendors": [],
            }
            det0 = d.get("details") or {}
            if det0.get("ssid"):
                g["_merged_ssids"].append(det0["ssid"])
            if det0.get("vendor"):
                g["_merged_vendors"].append(det0["vendor"])
            groups[prefix] = g
            out.append(g)
            continue
        g["_members"].append(device_id)
        g["_merged_count"] += 1
        g["seen_count"] = (g.get("seen_count") or 0) + (d.get("seen_count") or 0)
        if d.get("best_rssi") is not None and (
            g.get("best_rssi") is None or d["best_rssi"] > g["best_rssi"]
        ):
            g["best_rssi"] = d["best_rssi"]
        if d.get("last_seen") and (not g.get("last_seen") or d["last_seen"] > g["last_seen"]):
            g["last_seen"] = d["last_seen"]
            g["last_rssi"] = d.get("last_rssi")
        if d.get("first_seen") and (not g.get("first_seen") or d["first_seen"] < g["first_seen"]):
            g["first_seen"] = d["first_seen"]
        if device_id < g["device_id"]:
            g["device_id"] = device_id
        det = d.get("details") or {}
        if det.get("ssid") and det["ssid"] not in g["_merged_ssids"]:
            g["_merged_ssids"].append(det["ssid"])
        if det.get("vendor") and det["vendor"] not in g["_merged_vendors"]:
            g["_merged_vendors"].append(det["vendor"])
        if not g["details"].get("vendor") and det.get("vendor"):
            g["details"]["vendor"] = det["vendor"]
    return out


def _group_common_by_ap_prefix(common: list[dict]) -> list[dict]:
    """Same prefix grouping for the common-devices list. Locations sets and
    cross-location counts are unioned/summed so the table reflects the
    physical AP rather than its individual BSSIDs."""
    groups: dict[str, dict] = {}
    out: list[dict] = []
    for d in common:
        kind = d.get("kind")
        device_id = d.get("device_id") or ""
        if kind != "wifi" or len(device_id) < 17:
            out.append(d)
            continue
        prefix = device_id[:14].lower()
        g = groups.get(prefix)
        if g is None:
            g = {
                **d,
                "details": dict(d.get("details") or {}),
                "locations": list(d.get("locations") or []),
                "_members": [device_id],
                "_merged_count": 1,
            }
            groups[prefix] = g
            out.append(g)
            continue
        g["_members"].append(device_id)
        g["_merged_count"] += 1
        merged_locs = set(g.get("locations") or [])
        for L in d.get("locations") or []:
            merged_locs.add(L)
        g["locations"] = sorted(merged_locs)
        g["n_locations"] = len(g["locations"])
        g["total_seen"] = (g.get("total_seen") or 0) + (d.get("total_seen") or 0)
        if d.get("max_rssi") is not None and (
            g.get("max_rssi") is None or d["max_rssi"] > g["max_rssi"]
        ):
            g["max_rssi"] = d["max_rssi"]
        if device_id < g["device_id"]:
            g["device_id"] = device_id
    # n_locations may shift after merging, so re-sort by coverage then volume.
    out.sort(key=lambda d: (-(d.get("n_locations") or 0), -(d.get("total_seen") or 0)))
    return out


def _table(headers: list[str], rows: list[tuple], col_widths: list[float] | None = None) -> Table:
    data = [headers] + [list(r) for r in rows]
    t = Table(data, colWidths=col_widths, repeatRows=1)
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1f4060")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, 0), 9),
        # Plain-string data cells inherit this size; Paragraph cells use
        # their own ParagraphStyle and ignore it.
        ("FONTSIZE", (0, 1), (-1, -1), 8.5),
        ("BOTTOMPADDING", (0, 0), (-1, 0), 5),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f3f5f9")]),
        ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#c8cdd5")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 1), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 1), (-1, -1), 3),
    ]))
    return t


_KIND_COLORS = {"wifi": "#1f78b4", "bluetooth": "#8b5cf6"}


def _whitelist_matcher(entries: list[dict]):
    """Build a fast (kind, device_id_lower) -> bool matcher from whitelist
    entries, with prefix matching."""
    norm = [(e["kind"], (e["device_id"] or "").lower()) for e in entries]
    def match(kind: str, device_id: str) -> bool:
        d = (device_id or "").lower()
        for k, target in norm:
            if k != kind or not target:
                continue
            if d == target or d.startswith(target):
                return True
        return False
    return match


def _summary_blurb(locations: list[dict], common: list[dict]) -> str:
    """One-paragraph framing for the summary section. Uses just the
    cheap aggregates so this stays fast even on huge data sets."""
    if not locations:
        return "No sensor locations recorded yet."
    creates = [l.get("created_at") for l in locations if l.get("created_at")]
    last_seens = [l.get("last_seen_at") for l in locations if l.get("last_seen_at")]
    span = ""
    if creates and last_seens:
        first_ts = min(creates)
        last_ts = max(last_seens)
        span = f" Coverage spans {_fmt_time(first_ts)} → {_fmt_time(last_ts)}."
    n_recurring = len(common)
    follow_phrase = (
        f"{n_recurring} device{'s' if n_recurring != 1 else ''} appeared at "
        f"two or more locations." if n_recurring else
        "No devices were observed at multiple locations."
    )
    return (
        f"This report covers {len(locations)} sensor location"
        f"{'s' if len(locations) != 1 else ''}.{span} {follow_phrase}"
    )


def _summary_findings(locations: list[dict], per_loc_devices: dict[int, list[dict]],
                      common: list[dict]) -> list[str]:
    """Generate up to ~5 bullet-points highlighting noteworthy patterns.
    Cheap derivations only — no extra DB calls."""
    out: list[str] = []
    if not locations:
        return out

    # Most active location (highest device count or fix_count)
    by_devices = sorted(
        locations,
        key=lambda l: (
            (l.get("wifi_count") or 0)
            + (l.get("bt_count") or 0)
            + (l.get("wifi_client_count") or 0)
        ),
        reverse=True,
    )
    top_loc = by_devices[0]
    top_loc_total = (
        (top_loc.get("wifi_count") or 0)
        + (top_loc.get("bt_count") or 0)
        + (top_loc.get("wifi_client_count") or 0)
    )
    if top_loc_total > 0:
        label = top_loc.get("label") or f"Loc {top_loc['id']}"
        out.append(
            f"<b>Most active location:</b> {label} — {top_loc_total} unique devices."
        )

    # Strongest signal across the whole dataset
    strongest = None
    for devs in per_loc_devices.values():
        for d in devs:
            r = d.get("best_rssi")
            if r is None:
                continue
            if strongest is None or r > strongest["best_rssi"]:
                strongest = d
    if strongest is not None:
        det = strongest.get("details") or {}
        name = det.get("ssid") or det.get("name") or strongest.get("device_id")
        out.append(
            f"<b>Strongest signal:</b> {strongest.get('best_rssi')} dBm — "
            f"<font face='Courier'>{strongest.get('device_id')}</font>"
            f"{f' ({name})' if name and name != strongest.get('device_id') else ''}."
        )

    # Top recurring device
    if common:
        head = common[0]
        det = head.get("details") or {}
        name = det.get("ssid") or det.get("name") or ""
        n = head.get("n_locations") or 0
        if n >= 2:
            out.append(
                f"<b>Most-traveled device:</b> "
                f"<font face='Courier'>{head.get('device_id')}</font>"
                f"{f' ({name})' if name else ''} — seen at {n} locations."
            )

    # Per-kind totals as a one-liner
    total_wifi = sum(int(l.get("wifi_count") or 0) for l in locations)
    total_bt = sum(int(l.get("bt_count") or 0) for l in locations)
    total_cl = sum(int(l.get("wifi_client_count") or 0) for l in locations)
    if total_wifi or total_bt or total_cl:
        out.append(
            f"<b>By kind:</b> {total_wifi} Wi-Fi APs, {total_bt} Bluetooth devices, "
            f"{total_cl} Wi-Fi clients (passive probe captures)."
        )

    return out


async def build_report_pdf(*, group_bssids: bool = True) -> bytes:
    locations = await db.list_locations()
    locations = sorted(locations, key=lambda l: l["id"])
    common = await db.list_common_devices(min_locations=2, limit=_MAX_COMMON_DEVICES)

    # Whitelist filtering — drop matching entries from per-location device
    # lists, recompute counts to match, and prune the common-devices table.
    whitelist = await db.list_whitelist()
    is_wl = _whitelist_matcher(whitelist)

    # Per-location device pulls in parallel-ish (sequentially since aiosqlite
    # serializes anyway, but the volume is small).
    per_loc_devices: dict[int, list[dict]] = {}
    for loc in locations:
        devs = await db.devices_at_location(loc["id"])
        kept = [
            d for d in devs if not is_wl(d.get("kind", ""), d.get("device_id", ""))
        ]
        # Mirror the Devices tab's "Group multi-BSSID APs" checkbox when
        # asked. Grouping happens AFTER whitelist filtering so a whitelisted
        # member doesn't pull its physical AP off the list.
        if group_bssids:
            kept = _group_wifi_by_ap_prefix(kept)
        per_loc_devices[loc["id"]] = kept
        # Recompute the per-location counts so the comparison table reflects
        # the post-whitelist (and post-grouping, when enabled) totals — the
        # SQL aggregate from list_locations is unaware of either.
        loc["wifi_count"] = sum(1 for d in kept if d.get("kind") == "wifi")
        loc["bt_count"] = sum(1 for d in kept if d.get("kind") == "bluetooth")
        loc["wifi_client_count"] = sum(1 for d in kept if d.get("kind") == "wifi_client")
        loc["total_observations"] = sum(int(d.get("seen_count") or 0) for d in kept)

    common = [c for c in common if not is_wl(c.get("kind", ""), c.get("device_id", ""))]
    if group_bssids:
        common = _group_common_by_ap_prefix(common)

    s = _styles()
    flow: list[Any] = []

    # ── Title ──
    flow.append(Paragraph("Gjallarhorn Sensor Report", s["title"]))
    flow.append(Paragraph(
        f"Generated {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}", s["subtitle"],
    ))
    if whitelist:
        flow.append(Paragraph(
            f"<i>{len(whitelist)} whitelisted "
            f"{'entry' if len(whitelist) == 1 else 'entries'} excluded from this report.</i>",
            s["caption"],
        ))

    # ── Executive summary (first thing after the title) ──
    total_wifi = sum(int(l.get("wifi_count") or 0) for l in locations)
    total_bt = sum(int(l.get("bt_count") or 0) for l in locations)
    total_clients = sum(int(l.get("wifi_client_count") or 0) for l in locations)
    total_obs = sum(int(l.get("total_observations") or 0) for l in locations)

    flow.append(Paragraph("Summary", s["h1"]))
    flow.append(Paragraph(_summary_blurb(locations, common), s["caption"]))

    flow.append(_table(
        ["Locations", "Wi-Fi APs", "Bluetooth", "Wi-Fi clients", "Observations", "Recurring"],
        [(
            str(len(locations)),
            str(total_wifi),
            str(total_bt),
            str(total_clients),
            str(total_obs),
            str(len(common)),
        )],
        col_widths=[1.18 * inch] * 6,
    ))
    flow.append(Spacer(1, 8))
    findings = _summary_findings(locations, per_loc_devices, common)
    if findings:
        for line in findings:
            flow.append(Paragraph(f"• {line}", s["body"]))
    flow.append(Spacer(1, 16))

    # ── Overview map ──
    flow.append(Paragraph("Overview", s["h1"]))
    points = [(l["lat"], l["lon"], "#ff6b6b") for l in locations if l.get("lat") is not None]
    flow.append(await _render_map_image(points))
    flow.append(Paragraph(
        f"{len(locations)} sensor location{'s' if len(locations) != 1 else ''} plotted.",
        s["caption"],
    ))

    # ── Followers ── headline section: devices that have shown up at
    # multiple sensor locations, ranked by breadth and recency. The
    # "recent" column counts distinct locations within the last 24 hours,
    # which uses the same BLE-signature aggregation as the persistent_
    # companion alert rule — rotating private MACs collapse to one entry.
    flow.append(Spacer(1, 14))
    flow.append(Paragraph("Followers", s["h1"]))
    flow.append(Paragraph(
        "Devices observed at multiple sensor locations — ordered by total "
        "location count, then total observations. Higher counts suggest a "
        "device travelling with the sensor (operator's phone, watch) or "
        "ubiquitous infrastructure (carrier kit, common IoT). The "
        "<b>recent locs</b> column re-counts within the last 24 hours and "
        "treats BLE rotating MACs (devices sharing an adv-data fingerprint) "
        "as a single physical device, so a tracker that cycles its address "
        "still shows its true reach.",
        s["caption"],
    ))
    if not common:
        flow.append(Paragraph(
            "<i>No devices have been observed at two or more locations yet.</i>",
            s["caption"],
        ))
    else:
        # Recent (last-24h) location count per row, BLE-signature aware.
        # 24h matches the default persistent_companion alert window. We do
        # this for the ranked list (capped to keep the queries small).
        recent_loc_counts: dict[tuple, int] = {}
        for d in common[:_MAX_COMMON_DEVICES]:
            try:
                recent_loc_counts[(d["kind"], d["device_id"])] = (
                    await db.count_companion_locations(
                        d["kind"], d["device_id"], window_hours=24,
                    )
                )
            except Exception:  # pragma: no cover — query is best-effort
                recent_loc_counts[(d["kind"], d["device_id"])] = 0

        rows = []
        for i, d in enumerate(common, start=1):
            det = d.get("details") or {}
            merged_count = d.get("_merged_count") or 1
            name = det.get("ssid") or det.get("name") or ""
            vendor = det.get("vendor") or ""
            locs = ", ".join(str(x) for x in (d.get("locations") or []))
            device_id = d.get("device_id", "")
            if merged_count > 1:
                device_id = f"{device_id} (+{merged_count - 1})"
            recent = recent_loc_counts.get((d["kind"], d["device_id"]), 0)
            rows.append((
                str(i),
                d.get("kind", ""),
                _cell(device_id, mono=True),
                _cell(name),
                _cell(vendor),
                str(d.get("n_locations", "")),
                str(recent),
                _cell(locs),
                f"{d.get('max_rssi', '')} dBm",
                str(d.get("total_seen", "")),
            ))
        flow.append(_table(
            ["#", "Kind", "Device ID", "Name / SSID", "Vendor",
             "Locs", "Recent", "Where", "Best RSSI", "Total seen"],
            rows,
            col_widths=[0.30 * inch, 0.55 * inch, 1.20 * inch, 1.05 * inch,
                         0.95 * inch, 0.40 * inch, 0.55 * inch, 0.95 * inch,
                         0.70 * inch, 0.70 * inch],
        ))

    # ── Recurrence breakdown ── for the top recurring devices, show how
    # many times they were observed at each location (not just *where*,
    # but *how often*). Skipped entirely when nothing's been observed at
    # multiple locations.
    recurrence = await db.list_recurring_device_locations(
        min_locations=2, top_n=10,
    )
    # Whitelist filter — drop entries the user has explicitly excluded.
    recurrence = [
        r for r in recurrence
        if not is_wl(r.get("kind", ""), r.get("device_id", ""))
    ]
    if recurrence:
        flow.append(Spacer(1, 14))
        flow.append(Paragraph("Recurrence breakdown", s["h1"]))
        flow.append(Paragraph(
            "Top 10 devices observed at multiple locations, ranked by total "
            "observations. The breakdown column shows how many times each "
            "device was logged <i>at each location</i> — a device with one "
            "very heavy location and a thin scatter elsewhere reads "
            "differently from one that's evenly distributed.",
            s["caption"],
        ))
        rows = []
        for i, d in enumerate(recurrence, start=1):
            det = d.get("details") or {}
            name = det.get("ssid") or det.get("name") or ""
            vendor = det.get("vendor") or ""
            # Format the per-location frequency: "Home (#1): 22, Cafe (#2): 18"
            # Cap at 8 entries to keep the cell readable; tail count appended.
            per_loc = d.get("per_location") or []
            shown = []
            for p in per_loc[:8]:
                label = p.get("location_label") or f"#{p['location_id']}"
                shown.append(f"{label} (#{p['location_id']}): {p['seen_count']}")
            tail = len(per_loc) - 8
            breakdown = ", ".join(shown)
            if tail > 0:
                breakdown += f", +{tail} more"
            rows.append((
                str(i),
                d.get("kind", ""),
                _cell(d.get("device_id", ""), mono=True),
                _cell(name),
                _cell(vendor),
                str(d.get("n_locations", "")),
                str(d.get("total_seen", "")),
                _cell(breakdown),
            ))
        flow.append(_table(
            ["#", "Kind", "Device ID", "Name / SSID", "Vendor",
             "Locs", "Total", "Per-location frequency"],
            rows,
            col_widths=[0.30 * inch, 0.55 * inch, 1.20 * inch, 1.05 * inch,
                         0.85 * inch, 0.40 * inch, 0.55 * inch, 2.45 * inch],
        ))

    # ── Recent alert events ──
    flow.append(PageBreak())
    flow.append(Paragraph("Recent alert events", s["h1"]))
    events = await db.list_alert_events(limit=100)
    # Drop events for whitelisted devices — the rule fired before the user
    # whitelisted the device, but they're not actionable history any more.
    events = [
        e for e in events
        if not is_wl(e.get("device_kind", ""), e.get("device_id", ""))
    ]
    if not events:
        flow.append(Paragraph(
            "<i>No alert events recorded.</i>", s["caption"],
        ))
    else:
        flow.append(Paragraph(
            f"{len(events)} most recent matches across all enabled rules. "
            "Whitelisted devices' historical alerts are excluded.",
            s["caption"],
        ))
        ev_rows = []
        for e in events:
            det = e.get("details") or {}
            name = det.get("ssid") or det.get("name") or ""
            ev_rows.append((
                _cell(e.get("rule_name") or f"rule {e.get('rule_id')}"),
                e.get("device_kind", ""),
                _cell(e.get("device_id", ""), mono=True),
                _cell(name),
                f"{e['rssi']} dBm" if e.get("rssi") is not None else "",
                str(e.get("location_id") or "—"),
                _fmt_time(e.get("triggered_at")),
            ))
        flow.append(_table(
            ["Rule", "Kind", "Device ID", "Name / SSID", "RSSI", "Loc", "When"],
            ev_rows,
            col_widths=[1.30 * inch, 0.55 * inch, 1.30 * inch, 1.40 * inch,
                         0.70 * inch, 0.45 * inch, 1.40 * inch],
        ))

    # ── Active alert rules ──
    flow.append(Spacer(1, 14))
    flow.append(Paragraph("Alert rules", s["h1"]))
    rules = await db.alert_event_counts_per_rule()
    if not rules:
        flow.append(Paragraph(
            "<i>No alert rules configured.</i>", s["caption"],
        ))
    else:
        flow.append(Paragraph(
            "Configured rules with their lifetime fire counts. Rules that "
            "haven't fired may be over-specific, scoped to a location the "
            "sensor hasn't visited, or simply waiting for the right device.",
            s["caption"],
        ))
        rule_rows = []
        for r in rules:
            loc = r.get("location_id")
            loc_label = (
                "any" if loc is None
                else "active" if loc == -1
                else f"#{loc}"
            )
            rule_rows.append((
                "✓" if r.get("enabled") else "—",
                _cell(r.get("name") or ""),
                r.get("kind") or "any",
                r.get("match_type") or "",
                _cell(r.get("match_value") or "", mono=True),
                loc_label,
                str(r.get("fires") or 0),
                _fmt_time(r.get("last_fired")),
            ))
        flow.append(_table(
            ["On", "Name", "Kind", "Match", "Value", "Loc", "Fires", "Last fired"],
            rule_rows,
            col_widths=[0.30 * inch, 1.55 * inch, 0.55 * inch, 1.05 * inch,
                         1.30 * inch, 0.50 * inch, 0.50 * inch, 1.35 * inch],
        ))

    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=letter,
        leftMargin=0.6 * inch, rightMargin=0.6 * inch,
        topMargin=0.6 * inch, bottomMargin=0.6 * inch,
        title="Gjallarhorn Sensor Report",
    )
    doc.build(flow)
    return buf.getvalue()
