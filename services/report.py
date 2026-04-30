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

# How many rows of per-location devices and common-device entries to show.
_TOP_DEVICES_PER_LOCATION = 12
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


def _truncate(s: str, n: int) -> str:
    s = s or ""
    return s if len(s) <= n else s[: n - 1] + "…"


def _device_row(d: dict) -> tuple[str, str, str, str, str, str]:
    det = d.get("details") or {}
    name = det.get("ssid") or det.get("name") or ""
    vendor = det.get("vendor") or ""
    return (
        d.get("kind", ""),
        d.get("device_id", ""),
        _truncate(name, 24),
        _truncate(vendor, 22),
        f"{d.get('best_rssi', '')} dBm",
        str(d.get("seen_count", "")),
    )


def _table(headers: list[str], rows: list[tuple], col_widths: list[float] | None = None) -> Table:
    data = [headers] + [list(r) for r in rows]
    t = Table(data, colWidths=col_widths, repeatRows=1)
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1f4060")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 8.5),
        ("BOTTOMPADDING", (0, 0), (-1, 0), 5),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f3f5f9")]),
        ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#c8cdd5")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
    ]))
    return t


_KIND_COLORS = {"wifi": "#1f78b4", "bluetooth": "#8b5cf6"}


async def build_report_pdf() -> bytes:
    locations = await db.list_locations()
    locations = sorted(locations, key=lambda l: l["id"])
    common = await db.list_common_devices(min_locations=2, limit=_MAX_COMMON_DEVICES)

    # Per-location device pulls in parallel-ish (sequentially since aiosqlite
    # serializes anyway, but the volume is small).
    per_loc_devices: dict[int, list[dict]] = {}
    for loc in locations:
        per_loc_devices[loc["id"]] = await db.devices_at_location(loc["id"])

    s = _styles()
    flow: list[Any] = []

    # ── Cover ──
    flow.append(Paragraph("Gjallarhorn Sensor Report", s["title"]))
    flow.append(Paragraph(
        f"Generated {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}", s["subtitle"],
    ))

    total_wifi = sum(int(l.get("wifi_count") or 0) for l in locations)
    total_bt = sum(int(l.get("bt_count") or 0) for l in locations)
    total_obs = sum(int(l.get("total_observations") or 0) for l in locations)
    flow.append(_table(
        ["Locations", "Wi-Fi devices", "Bluetooth devices", "Total observations", "Common devices"],
        [(str(len(locations)), str(total_wifi), str(total_bt), str(total_obs), str(len(common)))],
    ))
    flow.append(Spacer(1, 16))

    # ── Overview map ──
    flow.append(Paragraph("Overview", s["h1"]))
    points = [(l["lat"], l["lon"], "#ff6b6b") for l in locations if l.get("lat") is not None]
    flow.append(await _render_map_image(points))
    flow.append(Paragraph(
        f"{len(locations)} sensor location{'s' if len(locations) != 1 else ''} plotted.",
        s["caption"],
    ))

    # ── Cross-location comparison ──
    if locations:
        flow.append(Paragraph("Per-location device counts", s["h1"]))
        comp_rows = [
            (
                str(l["id"]),
                _truncate(l.get("label") or "", 28),
                str(l.get("wifi_count") or 0),
                str(l.get("bt_count") or 0),
                str(l.get("total_observations") or 0),
                _fmt_time(l.get("created_at")),
                _fmt_time(l.get("last_seen_at")),
            )
            for l in locations
        ]
        flow.append(_table(
            ["ID", "Label", "Wi-Fi", "BT", "Obs.", "Created", "Last seen"],
            comp_rows,
            col_widths=[0.4 * inch, 2.0 * inch, 0.6 * inch, 0.5 * inch, 0.7 * inch, 1.2 * inch, 1.2 * inch],
        ))

    # ── Common devices ──
    if common:
        flow.append(PageBreak())
        flow.append(Paragraph("Common devices (seen at ≥ 2 locations)", s["h1"]))
        flow.append(Paragraph(
            "Devices that recurred across multiple sensor locations. Higher "
            "<b>locs</b> counts suggest mobile devices traveling with the sensor "
            "(e.g. the operator's own phone) or fixed infrastructure visible from "
            "multiple sites.",
            s["caption"],
        ))
        rows = []
        for d in common:
            det = d.get("details") or {}
            name = det.get("ssid") or det.get("name") or ""
            vendor = det.get("vendor") or ""
            locs = ", ".join(str(x) for x in (d.get("locations") or [])[:8])
            if d.get("locations") and len(d["locations"]) > 8:
                locs += f", +{len(d['locations']) - 8}"
            rows.append((
                d.get("kind", ""),
                d.get("device_id", ""),
                _truncate(name, 22),
                _truncate(vendor, 20),
                str(d.get("n_locations", "")),
                locs,
                f"{d.get('max_rssi', '')} dBm",
                str(d.get("total_seen", "")),
            ))
        flow.append(_table(
            ["Kind", "Device ID", "Name / SSID", "Vendor", "#locs", "Locations", "Best RSSI", "Total seen"],
            rows,
            col_widths=[0.5 * inch, 1.1 * inch, 1.1 * inch, 1.0 * inch, 0.45 * inch, 1.0 * inch, 0.7 * inch, 0.65 * inch],
        ))

    # ── Per-location detail ──
    for loc in locations:
        flow.append(PageBreak())
        title = f"Location #{loc['id']}"
        if loc.get("label"):
            title += f" — {loc['label']}"
        flow.append(Paragraph(title, s["h1"]))

        meta_rows = [
            ("Coordinates", f"{loc['lat']:.6f}, {loc['lon']:.6f}"),
            ("Radius", f"{loc.get('radius_m', '—')} m"),
            ("Fixes", str(loc.get("fix_count", "—"))),
            ("Wi-Fi devices", str(loc.get("wifi_count") or 0)),
            ("Bluetooth devices", str(loc.get("bt_count") or 0)),
            ("Total observations", str(loc.get("total_observations") or 0)),
            ("Created", _fmt_time(loc.get("created_at"))),
            ("Last seen", _fmt_time(loc.get("last_seen_at"))),
        ]
        meta_table = Table(meta_rows, colWidths=[1.6 * inch, 4.9 * inch])
        meta_table.setStyle(TableStyle([
            ("FONTSIZE", (0, 0), (-1, -1), 9),
            ("TEXTCOLOR", (0, 0), (0, -1), colors.HexColor("#52607a")),
            ("FONTNAME", (0, 0), (0, -1), "Helvetica-Bold"),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
            ("TOPPADDING", (0, 0), (-1, -1), 2),
        ]))
        flow.append(meta_table)
        flow.append(Spacer(1, 8))

        # Mini-map centered on this site
        flow.append(await _render_map_image(
            [(loc["lat"], loc["lon"], "#ff6b6b")],
            width_px=820, height_px=440, zoom=16,
        ))

        # Top devices at this location
        devs = per_loc_devices.get(loc["id"]) or []
        flow.append(Paragraph(
            f"Top {min(len(devs), _TOP_DEVICES_PER_LOCATION)} devices by best RSSI",
            s["h2"],
        ))
        if not devs:
            flow.append(Paragraph("<i>No devices recorded at this location yet.</i>", s["caption"]))
        else:
            top = devs[:_TOP_DEVICES_PER_LOCATION]
            flow.append(_table(
                ["Kind", "Device ID", "Name / SSID", "Vendor", "Best RSSI", "Seen"],
                [_device_row(d) for d in top],
                col_widths=[0.6 * inch, 1.4 * inch, 1.7 * inch, 1.5 * inch, 0.8 * inch, 0.5 * inch],
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
