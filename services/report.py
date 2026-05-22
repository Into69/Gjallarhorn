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
    Image, KeepTogether, PageBreak, Paragraph, SimpleDocTemplate, Spacer,
    Table, TableStyle,
)
from reportlab.platypus.flowables import HRFlowable
from reportlab.pdfgen import canvas as _rl_canvas
import socket

import database as db
from services.map_cache import render_map as _render_map_png

log = logging.getLogger(__name__)

# How many follower / cross-location entries to include in the report.
# Keep this tight — the report's role is to surface signal, not to mirror
# the Devices tab. Long tail is still visible in the UI.
_MAX_COMMON_DEVICES = 20
# Per-cell caps — ReportLab can't split a single cell across pages, so a
# cell with too much wrapped text crashes the layout. Keep these tight.
_MAX_LOCS_PER_CELL = 12        # comma-separated location IDs per cell
_MAX_FREQ_PER_CELL = 10        # entries in the Recurrence "Per-location frequency" column


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
    target_inches: float = 6.5,
) -> Image | Paragraph:
    """Render an OSM map for the given points using the cached tile store.
    `target_inches` is the on-page width — defaults to the full body width
    of ~6.5", pass a smaller value (e.g. 4.0) for mini-maps that share a
    page with other content. Falls back to a text placeholder if the
    renderer raises — a missing map shouldn't break a whole report."""
    styles = _styles()
    if not points:
        return Paragraph("<i>No locations to plot.</i>", styles["caption"])
    try:
        png_bytes = await _render_map_png(
            points, width_px=width_px, height_px=height_px, zoom=zoom,
        )
        buf = io.BytesIO(png_bytes)
        target_w = target_inches * inch
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
    entries, with prefix matching. Also honors the runtime sensor-MAC
    registry so the host's own scanner adapters are excluded from
    reports regardless of whether the user added them to the whitelist."""
    from services.sensor_identity import sensor_identity
    norm = [(e["kind"], (e["device_id"] or "").lower()) for e in entries]
    def match(kind: str, device_id: str) -> bool:
        d = (device_id or "").lower()
        if sensor_identity.is_sensor(d):
            return True
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


# How many entries to surface per "top N" finding category in the
# summary. Three feels like the sweet spot — the single-winner version
# was too narrow ("strongest signal" hiding the runners-up), but a
# top-five would push the per-finding mini-maps into report bloat.
_FINDING_TOP_N = 3


def _loc_active_score(l: dict) -> int:
    """Combined unique-device count used to rank 'most active' locations."""
    return ((l.get("wifi_count") or 0)
            + (l.get("bt_count") or 0)
            + (l.get("wifi_client_count") or 0))


def _most_active_location(locations: list[dict]) -> dict | None:
    """Single-winner helper kept for callers that still want the
    headline location. Returns None when no location has any devices."""
    if not locations:
        return None
    ranked = sorted(locations, key=_loc_active_score, reverse=True)
    if not ranked or _loc_active_score(ranked[0]) == 0:
        return None
    return ranked[0]


def _rank_prefix(rank: int) -> str:
    """Inline-html lead-in for a ranked bullet inside a top-N group.
    Sub-heading already labels the category, so we just need to mark
    the operator's place in the ranking."""
    return f"<font color='#7a86a3'>#{rank}.</font>&nbsp;"


# Order each category's findings appear in the report. Bullets within
# a category share a sub-heading and stay together as a block.
_FINDING_CATEGORIES = [
    ("most_active",  "Most active locations"),
    ("strongest",    "Strongest signal"),
    ("most_traveled", "Most-traveled devices"),
    ("by_kind",      None),  # one-liner; no sub-heading
]


def _summary_findings(locations: list[dict], per_loc_devices: dict[int, list[dict]],
                      common: list[dict]) -> list[dict]:
    """Generate the noteworthy-pattern findings list. Each entry is a
    dict {"text": <inline-html bullet>, "map_points": [(lat,lon,hex)]
    | None, "map_zoom": int | None} so the renderer can drop a
    contextual mini-map immediately below the relevant sentence
    (rather than appending it once at the end of the section).

    For "most active location", "strongest signal", and "most-traveled
    device" we surface the top _FINDING_TOP_N entries — the single-
    winner version hid useful runners-up.

    Cheap derivations only — no extra DB calls."""
    out: list[dict] = []
    if not locations:
        return out

    # Index locations by id for quick coord lookups when building
    # the mini-maps below.
    loc_by_id: dict[int, dict] = {l["id"]: l for l in locations if l.get("id") is not None}

    # ── Most active locations (top N by combined unique-device count) ──
    ranked_locs = [l for l in sorted(locations, key=_loc_active_score, reverse=True)
                   if _loc_active_score(l) > 0]
    for rank, loc in enumerate(ranked_locs[:_FINDING_TOP_N], start=1):
        total = _loc_active_score(loc)
        label = loc.get("label") or f"Loc {loc['id']}"
        map_points = None
        if loc.get("lat") is not None and loc.get("lon") is not None:
            map_points = [(loc["lat"], loc["lon"], "#ff6b6b")]
        out.append({
            "category": "most_active",
            "text": f"{_rank_prefix(rank)}<b>{label}</b> — {total} unique devices.",
            "map_points": map_points,
            "map_zoom": 16,
        })

    # ── Strongest signal (top N distinct devices by best_rssi) ──
    flat: list[tuple[dict, int]] = []
    for loc_id, devs in per_loc_devices.items():
        for d in devs:
            r = d.get("best_rssi")
            if r is None:
                continue
            flat.append((d, loc_id))
    flat.sort(key=lambda pair: pair[0].get("best_rssi") or -999, reverse=True)
    # Dedupe by (kind, device_id) so a single device that's the strongest
    # at three locations doesn't take all three slots.
    seen_keys: set[tuple] = set()
    rank = 0
    for d, loc_id in flat:
        key = (d.get("kind"), (d.get("device_id") or "").lower())
        if key in seen_keys:
            continue
        seen_keys.add(key)
        rank += 1
        det = d.get("details") or {}
        name = det.get("ssid") or det.get("name") or d.get("device_id")
        loc = loc_by_id.get(loc_id)
        loc_label = (loc.get("label") if loc else None) or (
            f"Loc {loc_id}" if loc_id is not None else "(unknown)"
        )
        map_points = None
        if loc is not None and loc.get("lat") is not None and loc.get("lon") is not None:
            map_points = [(loc["lat"], loc["lon"], "#ff6b6b")]
        out.append({
            "category": "strongest",
            "text": (
                f"{_rank_prefix(rank)}<b>{d.get('best_rssi')} dBm</b> — "
                f"<font face='Courier'>{d.get('device_id')}</font>"
                f"{f' ({name})' if name and name != d.get('device_id') else ''}"
                f" at <i>{loc_label}</i>."
            ),
            "map_points": map_points,
            "map_zoom": 16,
        })
        if rank >= _FINDING_TOP_N:
            break

    # ── Most-traveled devices (top N from the common list) ──
    trav_rank = 0
    for dev in common:
        n = dev.get("n_locations") or 0
        if n < 2:
            break  # common stays ordered, so anything below 2 is the tail
        trav_rank += 1
        det = dev.get("details") or {}
        name = det.get("ssid") or det.get("name") or ""
        map_points: list[tuple[float, float, str]] = []
        for lid in (dev.get("locations") or []):
            loc = loc_by_id.get(int(lid)) if str(lid).isdigit() or isinstance(lid, int) else None
            if loc and loc.get("lat") is not None and loc.get("lon") is not None:
                map_points.append((loc["lat"], loc["lon"], "#ff6b6b"))
        out.append({
            "category": "most_traveled",
            "text": (
                f"{_rank_prefix(trav_rank)}"
                f"<font face='Courier'>{dev.get('device_id')}</font>"
                f"{f' ({name})' if name else ''} — seen at {n} locations."
            ),
            "map_points": map_points or None,
            "map_zoom": None,
        })
        if trav_rank >= _FINDING_TOP_N:
            break

    # ── Per-kind totals one-liner ──
    total_wifi = sum(int(l.get("wifi_count") or 0) for l in locations)
    total_bt = sum(int(l.get("bt_count") or 0) for l in locations)
    total_bt_classic = sum(int(l.get("bt_classic_count") or 0) for l in locations)
    total_cl = sum(int(l.get("wifi_client_count") or 0) for l in locations)
    if total_wifi or total_bt or total_bt_classic or total_cl:
        out.append({
            "category": "by_kind",
            "text": (
                f"<b>By kind:</b> {total_wifi} Wi-Fi APs, {total_bt} BLE devices, "
                f"{total_bt_classic} Bluetooth Classic, "
                f"{total_cl} Wi-Fi clients (passive probe captures)."
            ),
            "map_points": None,
            "map_zoom": None,
        })

    return out


def _top_followers(common: list[dict], *, top_n: int,
                   n_total_locations: int) -> list[dict]:
    """Pick the most-traveled devices from the common-devices list for
    the headline "Looks like you were followed by…" section. Sorted by
    fraction of total locations (then by total observations as the
    tiebreaker). Returns at most top_n entries; skipped entirely when
    nothing was observed at >= 2 locations."""
    if not common:
        return []
    out = sorted(
        common,
        key=lambda d: (
            -(d.get("n_locations") or 0),
            -(d.get("total_seen") or 0),
        ),
    )[:top_n]
    for d in out:
        n_locs = int(d.get("n_locations") or 0)
        d["_coverage_pct"] = (
            int(round(100 * n_locs / n_total_locations))
            if n_total_locations else 0
        )
    return out


async def _render_suspect(sus: dict, s: dict, *, total_locations: int,
                          loc_by_id: dict[int, dict] | None = None):
    """Build a KeepTogether block describing one "suspected follower"
    for the headline section. Pulls every available signal (kind,
    name, vendor, tracker class, RSSI, observation count, BLE-signature
    aliases) into a compact multi-line callout, then anchors a multi-
    marker mini-map underneath showing every location that suspect
    appeared at. The whole bundle is wrapped in KeepTogether so the
    map can't get split off from the callout it illustrates."""
    det = sus.get("details") or {}
    merged_count = sus.get("_merged_count") or 1
    members = sus.get("_members") or []
    # Pull the BLE address_type from the device's details so the label
    # splits "BLE (public)" / "BLE (random)" — matches what the Devices
    # tab and Discord embeds show.
    kind_label = db.kind_label(sus.get("kind", ""), det.get("address_type"), det)
    name = det.get("ssid") or det.get("name") or ""
    vendor = det.get("vendor") or ""
    n_locs = sus.get("n_locations") or 0
    total_seen = sus.get("total_seen") or 0
    max_rssi = sus.get("max_rssi")
    pct = sus.get("_coverage_pct") or 0
    locs = ", ".join(str(x) for x in (sus.get("locations") or [])[:_MAX_LOCS_PER_CELL])
    if (sus.get("locations") or []) and len(sus["locations"]) > _MAX_LOCS_PER_CELL:
        locs += f", +{len(sus['locations']) - _MAX_LOCS_PER_CELL} more"
    tracker = db.classify_tracker(sus.get("kind", ""), det) if hasattr(db, "classify_tracker") else None
    tracker_label = {
        "airtag": "AirTag / FindMy",
        "tile": "Tile",
        "samsung_smarttag": "Samsung SmartTag",
    }.get(tracker) if tracker else None

    # Headline line: device id + kind + (tracker class | name | vendor)
    bits = [f"<b><font face='Courier'>{_h(sus.get('device_id', ''))}</font></b>"]
    if merged_count > 1:
        bits.append(f"<font color='#52607a'>(+{merged_count - 1} merged BSSID)</font>")
    bits.append(f"<font color='#52607a'>· {kind_label}</font>")
    if name:
        bits.append(f"— {_h(name)}")
    if vendor:
        bits.append(f"<font color='#52607a'>({_h(vendor)})</font>")
    if tracker_label:
        bits.append(f"<b><font color='#cc2a2a'>⚠ {tracker_label}</font></b>")
    headline = " ".join(bits)

    # Stats line: coverage, total observations, RSSI
    stats_bits = [
        f"<b>{n_locs}</b> of {total_locations} locations ({pct}% coverage)",
        f"<b>{total_seen}</b> observations",
    ]
    if max_rssi is not None:
        stats_bits.append(f"best RSSI <b>{max_rssi} dBm</b>")
    stats = " · ".join(stats_bits)

    where_line = f"<font color='#52607a'>Seen at locations:</font> {_h(locs)}" if locs else ""

    aliases_line = ""
    if merged_count > 1 and members:
        shown = members[:6]
        more = len(members) - len(shown)
        alias_txt = ", ".join(f"<font face='Courier'>{_h(m)}</font>" for m in shown)
        if more > 0:
            alias_txt += f" (+{more} more)"
        aliases_line = f"<font color='#52607a'>Likely aliases:</font> {alias_txt}"

    parts = [headline, stats]
    if where_line:
        parts.append(where_line)
    if aliases_line:
        parts.append(aliases_line)
    body = "<br/>".join(parts)

    style = ParagraphStyle(
        "suspect", parent=s["body"],
        fontSize=10, leading=13,
        leftIndent=8, rightIndent=8,
        spaceBefore=6, spaceAfter=6,
        borderColor=colors.HexColor("#cca56a"),
        borderWidth=0.6, borderPadding=8,
        backColor=colors.HexColor("#fff6e8"),
    )
    chunk: list = [Paragraph(body, style)]
    # Per-suspect mini-map: every location the device was observed at,
    # auto-zoomed to fit. Plot only when we have a loc_by_id and at
    # least one resolvable lat/lon — silently skip otherwise so a
    # missing-coord follower still gets its callout.
    map_points: list[tuple[float, float, str]] = []
    if loc_by_id:
        for lid in (sus.get("locations") or []):
            try:
                loc = loc_by_id.get(int(lid))
            except (TypeError, ValueError):
                continue
            if loc and loc.get("lat") is not None and loc.get("lon") is not None:
                map_points.append((loc["lat"], loc["lon"], "#cc2a2a"))
    if map_points:
        chunk.append(Spacer(1, 4))
        chunk.append(await _render_map_image(
            map_points,
            width_px=600, height_px=320,
            zoom=None, target_inches=4.5,
        ))
        chunk.append(Spacer(1, 4))
    return KeepTogether(chunk)


def _h(s_in) -> str:
    """Escape for reportlab Paragraph HTML-ish markup."""
    return (str(s_in or "")
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;"))


def _section_rule() -> HRFlowable:
    """Thin horizontal rule between major report sections. Slightly
    indented so it doesn't kiss the page margins."""
    return HRFlowable(
        width="100%", thickness=0.4,
        color=colors.HexColor("#c8cdd5"),
        spaceBefore=10, spaceAfter=10,
    )


class _NumberedCanvas(_rl_canvas.Canvas):
    """Two-pass canvas that captures every page's state during the first
    build, then on save() iterates the captured pages and draws the
    'Gjallarhorn report — page N of M' footer with the now-known total
    page count. Standard ReportLab idiom for footers that need to
    reference the doc's total page count."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._saved_pages: list[dict] = []

    def showPage(self):
        self._saved_pages.append(dict(self.__dict__))
        self._startPage()

    def save(self):
        total = len(self._saved_pages)
        for state in self._saved_pages:
            self.__dict__.update(state)
            self._draw_footer(total)
            super().showPage()
        super().save()

    def _draw_footer(self, total: int) -> None:
        self.saveState()
        self.setFont("Helvetica", 8)
        self.setFillColor(colors.HexColor("#7a86a3"))
        page_w = self._pagesize[0]
        # Right-aligned page number, left-aligned product mark.
        self.drawString(
            0.5 * inch, 0.35 * inch,
            "Gjallarhorn — sensor report",
        )
        self.drawRightString(
            page_w - 0.5 * inch, 0.35 * inch,
            f"page {self._pageNumber} of {total}",
        )
        self.restoreState()


async def build_report_pdf(*, group_bssids: bool = True,
                           progress=None,
                           mission: dict | None = None) -> bytes:
    # Stage tracker — each `_step()` updates the caller's progress hook
    # AND emits an info log so the same trace lands in the Logs tab.
    # _STAGE_TOTAL is a moving target (we know roughly how many milestones
    # we'll hit; finer-grained ones bump it up).
    STAGE_TOTAL = 11  # one per _step() call below; keep in sync if adding/removing stages
    state = {"n": 0}
    def _step(label: str, *, weight: int = 1) -> None:
        state["n"] += weight
        log.info("report: %s (%d/%d)", label, state["n"], STAGE_TOTAL)
        if progress is not None:
            try:
                progress(label, state["n"], STAGE_TOTAL)
            except Exception:  # pragma: no cover — progress hook is opt-in
                pass

    _step("Loading locations")
    locations = await db.list_locations()
    locations = sorted(locations, key=lambda l: l["id"])

    _step("Loading common devices")
    common = await db.list_common_devices(min_locations=2, limit=_MAX_COMMON_DEVICES)

    _step("Loading whitelist")
    # Whitelist filtering — drop matching entries from per-location device
    # lists, recompute counts to match, and prune the common-devices table.
    # Combined (permanent + temporary) — temp entries also silence in reports.
    whitelist = await db.list_whitelist_combined()
    is_wl = _whitelist_matcher(whitelist)

    _step(f"Aggregating devices across {len(locations)} location(s)")
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
        loc["bt_classic_count"] = sum(1 for d in kept if d.get("kind") == "bluetooth_classic")
        loc["wifi_client_count"] = sum(1 for d in kept if d.get("kind") == "wifi_client")
        loc["total_observations"] = sum(int(d.get("seen_count") or 0) for d in kept)

    common = [c for c in common if not is_wl(c.get("kind", ""), c.get("device_id", ""))]
    if group_bssids:
        common = _group_common_by_ap_prefix(common)

    s = _styles()
    flow: list[Any] = []

    _step("Rendering summary")
    # ── Cover block ──
    # First-page banner identifying what the report covers: app title,
    # generation timestamp, sensor host name, optional mission scope,
    # and a totals strip. Designed to give the PDF a clear identity
    # when emailed or printed without needing to scan the body.
    flow.append(Paragraph("Gjallarhorn Sensor Report", s["title"]))
    try:
        host = socket.gethostname() or "(unknown host)"
    except Exception:
        host = "(unknown host)"
    flow.append(Paragraph(
        f"Generated {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} · "
        f"sensor host <font face='Courier'>{_h(host)}</font>",
        s["subtitle"],
    ))
    # ── Optional mission cover block ──
    # Threaded in from /api/missions/{id}/report/start. Renders the
    # mission name + window + per-kind / observation / alert deltas
    # right under the title so the report doubles as a mission summary.
    if mission:
        name = mission.get("name") or f"Mission #{mission.get('id', '?')}"
        flow.append(Paragraph(f"<b>Mission:</b> {name}", s["body"]))
        started = mission.get("started_at") or ""
        ended = mission.get("ended_at") or "(active)"
        flow.append(Paragraph(
            f"<i>Window:</i> {started} → {ended}", s["caption"],
        ))
        if mission.get("description"):
            flow.append(Paragraph(
                f"<i>{mission['description']}</i>", s["caption"],
            ))
        stats0 = mission.get("stats_start") or {}
        stats1 = mission.get("stats_end") or {}
        def _delta(key, sub=None):
            a = (stats0.get("devices", {}) if sub else stats0).get(sub or key)
            b = (stats1.get("devices", {}) if sub else stats1).get(sub or key)
            if a is None or b is None:
                return None
            return (b or 0) - (a or 0)
        diff_lines = []
        for label, key, sub in [
            ("Locations",        "locations", None),
            ("Devices total",    None,        "total"),
            ("Wi-Fi APs",        None,        "wifi"),
            ("BLE",              None,        "bluetooth"),
            ("Bluetooth Classic", None,       "bluetooth_classic"),
            ("Wi-Fi clients",    None,        "wifi_client"),
            ("Observations",     "observations", None),
            ("Alert events",     "alert_events", None),
        ]:
            v = _delta(key, sub)
            if v is None:
                continue
            sign = "+" if v >= 0 else ""
            diff_lines.append(f"{label}: {sign}{v}")
        if diff_lines:
            flow.append(Paragraph(
                "<b>Captured during this mission:</b> " + " · ".join(diff_lines),
                s["body"],
            ))
        flow.append(Spacer(1, 12))
    if whitelist:
        flow.append(Paragraph(
            f"<i>{len(whitelist)} whitelisted "
            f"{'entry' if len(whitelist) == 1 else 'entries'} excluded from this report.</i>",
            s["caption"],
        ))

    # ── Executive summary (first thing after the title) ──
    total_wifi = sum(int(l.get("wifi_count") or 0) for l in locations)
    total_bt = sum(int(l.get("bt_count") or 0) for l in locations)
    total_bt_classic = sum(int(l.get("bt_classic_count") or 0) for l in locations)
    total_clients = sum(int(l.get("wifi_client_count") or 0) for l in locations)
    total_obs = sum(int(l.get("total_observations") or 0) for l in locations)

    flow.append(Paragraph("Summary", s["h1"]))
    flow.append(Paragraph(_summary_blurb(locations, common), s["caption"]))

    flow.append(_table(
        ["Locations", "Wi-Fi APs", "BLE", "BT Classic", "Wi-Fi clients", "Observations", "Recurring"],
        [(
            str(len(locations)),
            str(total_wifi),
            str(total_bt),
            str(total_bt_classic),
            str(total_clients),
            str(total_obs),
            str(len(common)),
        )],
        col_widths=[1.0 * inch] * 7,
    ))
    flow.append(Spacer(1, 8))
    findings = _summary_findings(locations, per_loc_devices, common)
    # Bucket findings by category so we can drop a sub-heading once
    # per category instead of repeating the verb prefix in every
    # bullet. The categories list controls the on-page order.
    by_cat: dict[str, list[dict]] = {}
    for f in findings:
        by_cat.setdefault(f.get("category", "by_kind"), []).append(f)
    async def _build_finding_chunk(f: dict) -> list:
        # Bullet + its mini-map (if any) packaged so they stay on the
        # same page. Maps that span multiple markers (most-traveled)
        # get a slightly larger canvas — auto-zoom plus multiple
        # points needs the extra vertical room to be readable.
        chunk = [Paragraph(f"• {f['text']}", s["body"])]
        pts = f.get("map_points")
        if pts:
            wide = f.get("map_zoom") is None and len(pts) > 1
            chunk.append(Spacer(1, 4))
            chunk.append(await _render_map_image(
                pts,
                width_px=600 if wide else 560,
                height_px=380 if wide else 300,
                zoom=f.get("map_zoom"),
                target_inches=4.5 if wide else 4.0,
            ))
            chunk.append(Spacer(1, 6))
        return chunk

    for cat, heading in _FINDING_CATEGORIES:
        bucket = by_cat.get(cat) or []
        if not bucket:
            continue
        # Keep the sub-heading glued to the first bullet+map of its
        # category so the heading can't strand at the bottom of a
        # page with all its content pushed to the next. The remaining
        # items in the category get their own KeepTogether blocks
        # since each one needs to be independently page-fittable.
        first_chunk = await _build_finding_chunk(bucket[0])
        head_block: list = []
        if heading:
            head_block.append(Paragraph(heading, s["h2"]))
        flow.append(KeepTogether(head_block + first_chunk))
        for f in bucket[1:]:
            flow.append(KeepTogether(await _build_finding_chunk(f)))
        flow.append(Spacer(1, 8))
    flow.append(Spacer(1, 8))

    _step("Picking suspected followers")
    # ── "Looks like you were followed by…" callout ──
    # Headline-style section that calls out the most-traveled devices
    # *before* the data tables. Surfaces the answer to the question most
    # operators are actually asking when they generate this report.
    suspects = _top_followers(common, top_n=5, n_total_locations=len(locations))
    loc_by_id: dict[int, dict] = {
        l["id"]: l for l in locations if l.get("id") is not None
    }
    if suspects:
        flow.append(_section_rule())
        flow.append(Paragraph("Looks like you were followed by…", s["h1"]))
        flow.append(Paragraph(
            "Devices that appeared at the largest fraction of your sensor "
            "locations. BLE rotating-MAC siblings (devices sharing an "
            "advertising-data fingerprint) are listed under "
            "<b>likely aliases</b> so a tracker that cycles its address "
            "still reads as one suspect, not several.",
            s["caption"],
        ))
        for sus in suspects:
            flow.append(await _render_suspect(
                sus, s, total_locations=len(locations),
                loc_by_id=loc_by_id,
            ))
        flow.append(Spacer(1, 16))

    _step("Rendering overview map")
    # ── Overview map ──
    flow.append(_section_rule())
    flow.append(Paragraph("Overview", s["h1"]))
    points = [(l["lat"], l["lon"], "#ff6b6b") for l in locations if l.get("lat") is not None]
    flow.append(await _render_map_image(points))
    flow.append(Paragraph(
        f"{len(locations)} sensor location{'s' if len(locations) != 1 else ''} plotted.",
        s["caption"],
    ))

    _step(f"Computing follower companion-counts ({len(common)} candidates)")
    # ── Followers ── headline section: devices that have shown up at
    # multiple sensor locations, ranked by breadth and recency. The
    # "recent" column counts distinct locations within the last 24 hours,
    # which uses the same BLE-signature aggregation as the persistent_
    # companion alert rule — rotating private MACs collapse to one entry.
    flow.append(_section_rule())
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
            # Cap the location-id list so a follower seen at hundreds of
            # locations doesn't produce a single cell that's taller than
            # a page (ReportLab can't split one cell across pages).
            loc_ids = list(d.get("locations") or [])
            shown_locs = ", ".join(str(x) for x in loc_ids[:_MAX_LOCS_PER_CELL])
            if len(loc_ids) > _MAX_LOCS_PER_CELL:
                shown_locs += f", +{len(loc_ids) - _MAX_LOCS_PER_CELL} more"
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
                _cell(shown_locs),
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

    _step("Building recurrence breakdown")
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
        flow.append(_section_rule())
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
            for p in per_loc[:_MAX_FREQ_PER_CELL]:
                label = p.get("location_label") or f"#{p['location_id']}"
                # Trim long custom labels so one entry can't dominate the cell.
                if len(label) > 30:
                    label = label[:27] + "…"
                shown.append(f"{label} (#{p['location_id']}): {p['seen_count']}")
            tail = len(per_loc) - _MAX_FREQ_PER_CELL
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

    # Recent alert events + Alert rules sections were removed — the report
    # focuses on followers/recurrence; live alert state belongs in the UI.

    _step("Rendering per-location detail pages")
    # ── Per-location detail ──
    # One page per sensor location: tight-zoom mini-map of the bubble +
    # a top-N devices table drawn from per_loc_devices. Lets the operator
    # drill into 'what was at each location' after the cross-cutting
    # findings above. Locations with no captured devices and no coords
    # get skipped so the report doesn't pad out with empty placeholders.
    if locations:
        flow.append(PageBreak())
        flow.append(Paragraph("Per-location detail", s["h1"]))
        flow.append(Paragraph(
            "One page per sensor location, capped at the 15 strongest-signal "
            "devices per bubble. Use the Devices tab in the UI for the full "
            "list.",
            s["caption"],
        ))
        for li, loc in enumerate(locations):
            devs = per_loc_devices.get(loc["id"], []) or []
            has_coords = loc.get("lat") is not None and loc.get("lon") is not None
            if not devs and not has_coords:
                continue
            # Each location starts on a fresh page so the heading +
            # map + table read as one cohesive section. The very first
            # location uses the page-break that opened the section
            # above; subsequent locations get an explicit PageBreak.
            if li > 0:
                flow.append(PageBreak())
            label = loc.get("label") or f"Loc {loc['id']}"
            header_block: list = [
                Paragraph(
                    f"Location: <b>{_h(label)}</b> "
                    f"<font color='#7a86a3'>(#{loc['id']})</font>",
                    s["h2"],
                ),
            ]
            stats_bits: list[str] = []
            for label_, key in [
                ("Wi-Fi APs",         "wifi_count"),
                ("BLE",               "bt_count"),
                ("BT Classic",        "bt_classic_count"),
                ("Wi-Fi clients",     "wifi_client_count"),
                ("Total observations","total_observations"),
            ]:
                v = loc.get(key)
                if v:
                    stats_bits.append(f"<b>{v}</b> {label_}")
            if stats_bits:
                header_block.append(Paragraph(
                    " · ".join(stats_bits), s["caption"],
                ))
            if has_coords:
                header_block.append(Spacer(1, 4))
                header_block.append(await _render_map_image(
                    [(loc["lat"], loc["lon"], "#ff6b6b")],
                    width_px=560, height_px=300, zoom=16, target_inches=4.5,
                ))
            flow.append(KeepTogether(header_block))
            if not devs:
                flow.append(Paragraph(
                    "<i>No devices captured at this location yet.</i>",
                    s["caption"],
                ))
                continue
            # Top-N by best_rssi (closer to 0 = stronger). Cap at 15
            # so a busy cafe doesn't sprawl across multiple pages —
            # the Devices tab has the full list.
            top_devs = sorted(
                devs,
                key=lambda d: d.get("best_rssi") if d.get("best_rssi") is not None else -999,
                reverse=True,
            )[:15]
            rows: list[tuple] = []
            for d in top_devs:
                det = d.get("details") or {}
                name = det.get("ssid") or det.get("name") or ""
                vendor = det.get("vendor") or ""
                rows.append((
                    db.kind_label(d.get("kind", ""), det.get("address_type"), det),
                    _cell(d.get("device_id", ""), mono=True),
                    _cell(name),
                    _cell(vendor),
                    f"{d.get('best_rssi', '')} dBm" if d.get("best_rssi") is not None else "",
                    str(d.get("seen_count") or 0),
                ))
            flow.append(Spacer(1, 6))
            flow.append(_table(
                ["Kind", "Device ID", "Name / SSID", "Vendor", "Best RSSI", "Seen"],
                rows,
                col_widths=[1.10 * inch, 1.55 * inch, 1.45 * inch,
                             1.20 * inch, 0.80 * inch, 0.55 * inch],
            ))

    _step("Building PDF document")
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=letter,
        leftMargin=0.6 * inch, rightMargin=0.6 * inch,
        topMargin=0.6 * inch,
        # Slightly larger bottom margin so flowables don't crowd the
        # 'page N of M' footer the NumberedCanvas draws at y=0.35".
        bottomMargin=0.7 * inch,
        title="Gjallarhorn Sensor Report",
    )
    doc.build(flow, canvasmaker=_NumberedCanvas)
    return buf.getvalue()
