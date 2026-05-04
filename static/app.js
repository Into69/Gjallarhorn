// ---------- helpers ----------
const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...opts,
  });
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  return res.json();
}

// ---------- tabs ----------
$$(".tab-btn").forEach((btn) => {
  btn.addEventListener("click", () => {
    $$(".tab-btn").forEach((b) => b.classList.toggle("active", b === btn));
    const id = btn.dataset.tab;
    $$(".tab").forEach((t) => t.classList.toggle("active", t.id === `tab-${id}`));
    if (id === "map" && map) setTimeout(() => map.invalidateSize(), 50);
    if (id === "devices") refreshDevices();
    if (id === "locations") refreshLocations();
    if (id === "alerts") refreshAlerts();
    if (id === "logs") refreshLogs();
  });
});

// ---------- map ----------
let map, tileLayer, sensorMarker, accuracyCircle;
const locationMarkers = new Map();
const providersCache = {};

async function initMap() {
  const { providers } = await api("/api/map_providers");
  Object.assign(providersCache, providers);
  const sel = $("#set-map-provider");
  sel.innerHTML = "";
  for (const [k, v] of Object.entries(providers)) {
    const opt = document.createElement("option");
    opt.value = k; opt.textContent = v.name;
    sel.appendChild(opt);
  }
  map = L.map("map", { zoomControl: true }).setView([0, 0], 2);
  await applyMapProvider("osm");
}

async function applyMapProvider(key) {
  const p = providersCache[key];
  if (!p) return;
  if (tileLayer) map.removeLayer(tileLayer);
  const opts = { attribution: p.attribution, maxZoom: p.max_zoom || 19 };
  if (p.subdomains) opts.subdomains = p.subdomains;
  tileLayer = L.tileLayer(p.url, opts);
  tileLayer.addTo(map);
}

// ---------- live status / GPS poll ----------
const CARDINAL = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
                  "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"];
function cardinal(deg) {
  if (deg == null || isNaN(deg)) return "";
  return CARDINAL[Math.round(((deg % 360 + 360) % 360) / 22.5) % 16];
}

function renderFixCard(fix) {
  const modeEl = $("#fix-mode");
  if (fix.mode >= 3) { modeEl.textContent = "3D Fix"; modeEl.className = "fix-mode fix-3d"; }
  else if (fix.mode === 2) { modeEl.textContent = "2D Fix"; modeEl.className = "fix-mode fix-2d"; }
  else { modeEl.textContent = "No Fix"; modeEl.className = "fix-mode no-fix"; }

  const used = fix.sats_used ?? null, vis = fix.sats_visible ?? null;
  $("#fix-sats").textContent = (used != null && vis != null) ? `${used}/${vis} sats`
                              : (vis != null ? `${vis} visible` : "— sats");

  $("#fix-lat").textContent = fix.lat != null ? `${fix.lat.toFixed(6)}°` : "—";
  $("#fix-lon").textContent = fix.lon != null ? `${fix.lon.toFixed(6)}°` : "—";

  $("#fix-alt").innerHTML = fix.alt != null
    ? `${fix.alt.toFixed(1)} <span class="unit">m</span>`
    : `<span class="unit">—</span>`;

  if (fix.speed != null) {
    const kmh = (fix.speed * 3.6).toFixed(1);
    $("#fix-speed").innerHTML =
      `${fix.speed.toFixed(2)} <span class="unit">m/s</span><span class="sub">${kmh} km/h</span>`;
  } else {
    $("#fix-speed").innerHTML = `<span class="unit">—</span>`;
  }

  if (fix.track != null) {
    $("#fix-heading").innerHTML =
      `${Math.round(fix.track)}° <span class="unit">${cardinal(fix.track)}</span>`;
  } else {
    $("#fix-heading").innerHTML = `<span class="unit">—</span>`;
  }

  if (fix.error_h != null) {
    const sub = fix.error_v != null ? `<span class="sub">±${fix.error_v.toFixed(1)} m vert</span>` : "";
    $("#fix-accuracy").innerHTML =
      `±${fix.error_h.toFixed(1)} <span class="unit">m</span>${sub}`;
  } else {
    $("#fix-accuracy").innerHTML = `<span class="unit">—</span>`;
  }

  const pct = (used != null && vis) ? (used / vis) * 100 : 0;
  $("#fix-sat-fill").style.width = `${Math.max(0, Math.min(100, pct))}%`;
  $("#fix-sat-detail").textContent = (used != null && vis != null)
    ? `${used} used / ${vis} visible` : "—";

  if (fix.time) {
    try { $("#fix-updated").textContent = new Date(fix.time).toLocaleTimeString(); }
    catch { $("#fix-updated").textContent = String(fix.time); }
  } else {
    $("#fix-updated").textContent = "—";
  }
}

async function pollGps() {
  try {
    const data = await api("/api/gps");
    const fix = data.fix;
    const gpsPill = $("#gps-status");
    if (data.connected && fix.mode >= 2) {
      const used = fix.sats_used, vis = fix.sats_visible;
      let satStr;
      if (used != null && vis != null) satStr = `${used}/${vis} sats`;
      else if (vis != null) satStr = `${vis} visible`;
      else satStr = "no sat info";
      gpsPill.textContent = `GPS: ${fix.mode}D fix · ${satStr}`;
      gpsPill.className = "pill ok";
    } else if (data.connected) {
      const vis = fix.sats_visible;
      gpsPill.textContent = `GPS: searching (${vis != null ? vis + " visible" : "no sat info"})`;
      gpsPill.className = "pill warn";
    } else {
      gpsPill.textContent = "GPS: gpsd unreachable";
      gpsPill.className = "pill err";
    }
    $("#loc-status").textContent = `Loc: ${data.active_location_id ?? "—"}`;
    $("#loc-status").className = "pill " + (data.active_location_id ? "ok" : "");

    renderFixCard(fix);
    $("#fix-active-loc").textContent = data.active_location_id ?? "—";

    if (fix.lat != null && fix.lon != null) {
      const ll = [fix.lat, fix.lon];
      const firstFix = !sensorMarker;
      if (firstFix) {
        sensorMarker = L.circleMarker(ll, { radius: 8, color: "#5cd1ff", fillColor: "#5cd1ff", fillOpacity: 0.8 }).addTo(map);
        // One-shot center on first fix only when no toggle will do it for us.
        if (!mapToggles.trackSensor && !mapToggles.smartTrack && !mapToggles.autoZoom) {
          map.setView(ll, 17);
        }
      } else {
        sensorMarker.setLatLng(ll);
      }
      if (fix.error_h) {
        if (!accuracyCircle) {
          accuracyCircle = L.circle(ll, { radius: fix.error_h, color: "#5cd1ff", fillOpacity: 0.05, weight: 1 }).addTo(map);
        } else {
          accuracyCircle.setLatLng(ll); accuracyCircle.setRadius(fix.error_h);
        }
      }
      applyMapView(ll);
    }
  } catch (e) {
    $("#gps-status").textContent = "GPS: error";
    $("#gps-status").className = "pill err";
  }
}

function locationTooltipHtml(loc, isActive) {
  const label = escapeHtml(loc.label || `Location ${loc.id}`);
  const activeBadge = isActive ? `<span class="gj-tip-badge active">ACTIVE</span>` : "";
  const drawnBadge = loc.source === "manual" ? `<span class="gj-tip-badge drawn">DRAWN</span>` : "";
  return `
    <div class="gj-tip-card">
      <div class="gj-tip-header">
        <span class="gj-tip-id">#${loc.id}</span>
        <span class="gj-tip-label">${label}</span>
        ${drawnBadge}${activeBadge}
      </div>
      <div class="gj-tip-coords">${loc.lat.toFixed(5)}, ${loc.lon.toFixed(5)}</div>
      <div class="gj-tip-stats">
        <div class="gj-tip-stat">
          <span class="gj-tip-stat-label">WiFi</span>
          <span class="gj-tip-stat-value wifi">${loc.wifi_count ?? 0}</span>
        </div>
        <div class="gj-tip-stat">
          <span class="gj-tip-stat-label">Bluetooth</span>
          <span class="gj-tip-stat-value bt">${loc.bt_count ?? 0}</span>
        </div>
        <div class="gj-tip-stat">
          <span class="gj-tip-stat-label">WiFi clients</span>
          <span class="gj-tip-stat-value client">${loc.wifi_client_count ?? 0}</span>
        </div>
        <div class="gj-tip-stat">
          <span class="gj-tip-stat-label">Fixes</span>
          <span class="gj-tip-stat-value">${loc.fix_count ?? 0}</span>
        </div>
        <div class="gj-tip-stat">
          <span class="gj-tip-stat-label">Radius</span>
          <span class="gj-tip-stat-value">${Math.round(loc.radius_m)}<span class="gj-tip-unit">m</span></span>
        </div>
      </div>
    </div>`;
}

async function refreshLocationMarkers() {
  try {
    const { locations, active_id } = await api("/api/locations");
    for (const m of locationMarkers.values()) map.removeLayer(m);
    locationMarkers.clear();
    for (const loc of locations) {
      const isActive = loc.id === active_id;
      const isManual = loc.source === "manual";
      // Drawn geofences are styled distinctly (dashed accent stroke) so a
      // glance at the map tells you which circles you placed yourself vs.
      // which ones the auto-clusterer made.
      const c = L.circle([loc.lat, loc.lon], {
        radius: loc.radius_m,
        color: isActive ? "#79e08c" : (isManual ? "#5cd1ff" : "#ffb86b"),
        weight: isManual ? 2 : 1.5,
        dashArray: isManual ? "6,4" : null,
        fillOpacity: isActive ? 0.12 : (isManual ? 0.05 : 0.06),
      }).bindTooltip(locationTooltipHtml(loc, isActive), {
        className: "gj-tip",
        direction: "top",
        offset: [0, -4],
        opacity: 1,
        sticky: true,
      });
      c.addTo(map);
      locationMarkers.set(loc.id, c);
    }
  } catch (e) { /* ignore */ }
}

// ---------- devices tab ----------
const PRESERVED_SENTINEL = "__preserved__";

async function loadLocationOptions() {
  const { locations, active_id } = await api("/api/locations");
  const sel = $("#dev-location");
  const previous = sel.value;
  sel.innerHTML = "";
  for (const loc of locations) {
    const o = document.createElement("option");
    o.value = loc.id;
    o.textContent = `${loc.label || `Loc ${loc.id}`}${loc.id === active_id ? " (active)" : ""}`;
    sel.appendChild(o);
  }
  // Pseudo-location for whitelisted devices archived from deleted locations.
  // Only shown when there's something in it, so it doesn't clutter the
  // dropdown for fresh installs.
  try {
    const pres = await api("/api/preserved-devices");
    if ((pres.devices || []).length) {
      const opt = document.createElement("option");
      opt.value = PRESERVED_SENTINEL;
      opt.textContent = `★ Preserved (whitelist) — ${pres.devices.length}`;
      sel.appendChild(opt);
    }
  } catch { /* preserved endpoint may not be available; ignore */ }
  // Preserve the user's prior selection if it still exists; otherwise
  // default to the active location (only really used on first load,
  // since this function gets re-called from refreshDevices on change).
  const ids = new Set(locations.map(l => String(l.id)));
  if (previous && (ids.has(previous) || previous === PRESERVED_SENTINEL)) {
    sel.value = previous;
  } else if (active_id != null) {
    sel.value = String(active_id);
  }
}

async function refreshDevices() {
  await loadLocationOptions();
  const id = $("#dev-location").value;
  if (!id) return;
  const kind = $("#dev-kind").value;
  const q = kind ? `?kind=${kind}` : "";
  const { devices } = id === PRESERVED_SENTINEL
    ? await api(`/api/preserved-devices${q}`)
    : await api(`/api/locations/${id}/devices${q}`);
  const tbody = $("#dev-table tbody");
  tbody.innerHTML = "";

  // Optionally collapse wifi BSSIDs that share the same first 5 octets
  // (multi-BSSID radios on the same physical AP). Other kinds pass through.
  const groupBssid = $("#dev-group-bssid")?.checked;
  let rows = groupBssid ? groupWifiByApPrefix(devices) : devices;

  // Time-range filter: drop rows whose last_seen is older than the selected
  // window. Comparing ISO strings lexically only works because the format is
  // fixed-width, so compare via Date instead.
  const sinceSec = parseInt($("#dev-since")?.value || "0", 10);
  if (sinceSec > 0) {
    const cutoff = Date.now() - sinceSec * 1000;
    rows = rows.filter(d => {
      const t = d.last_seen ? new Date(d.last_seen + "Z").getTime() : 0;
      // Backend stores UTC ISO without tz suffix; appending "Z" forces UTC.
      // Fallback to raw parse if that fails.
      return (Number.isFinite(t) && t > 0 ? t : Date.parse(d.last_seen || "")) >= cutoff;
    });
  }

  // Free-text search across MAC, SSID/name, and vendor — case-insensitive.
  const q_search = ($("#dev-search")?.value || "").trim().toLowerCase();
  if (q_search) {
    rows = rows.filter(d => {
      const det = d.details || {};
      const haystack = [
        d.device_id, det.ssid, det.name, det.vendor,
        ...(d._merged_ssids || []),
      ].filter(Boolean).join(" ").toLowerCase();
      return haystack.includes(q_search);
    });
  }

  for (const d of rows) {
    tbody.appendChild(renderDeviceRow(d));
  }
  const total = (groupBssid ? groupWifiByApPrefix(devices) : devices).length;
  const countEl = $("#dev-count");
  if (countEl) {
    countEl.textContent = (sinceSec || q_search)
      ? `${rows.length} of ${total}`
      : `${total} device${total === 1 ? "" : "s"}`;
  }
}

function renderDeviceRow(d) {
  const det = d.details || {};
  // For wifi_client probes, the device doesn't have a single SSID — it has
  // a list of networks it's been searching for. Show those instead.
  let nameOrSsid;
  if (d.kind === "wifi_client" && Array.isArray(det.ssids)) {
    const named = det.ssids.filter(Boolean);
    nameOrSsid = named.length
      ? named.slice(0, 3).join(", ") + (named.length > 3 ? `, +${named.length - 3}` : "")
      : "(wildcard)";
  } else if (d._merged_ssids) {
    // Grouped row: show all distinct SSIDs that the merged BSSIDs broadcast.
    nameOrSsid = d._merged_ssids.join(", ");
  } else {
    nameOrSsid = det.ssid ?? det.name ?? "";
  }
  const tr = document.createElement("tr");
  // Mark merged rows visually so it's obvious they represent multiple BSSIDs.
  if (d._merged_count > 1) tr.classList.add("merged-ap");
  const wl = isWhitelisted(d.kind, d.device_id);
  if (wl) tr.classList.add("whitelisted");
  // BLE link badge: when this row's signature matches one or more other
  // rows (rotating private MACs sharing a stable adv-data fingerprint),
  // surface the count beside the device id with the alias list as the
  // tooltip. When at least one sibling's lifetime is temporally adjacent
  // (one disappeared as the other appeared, within ~20 min), the badge
  // gets a high-confidence flag — that's the "MAC X just rotated to
  // MAC Y" signal.
  const linkedCount = d.linked_count || 0;
  const linkedIds = d.linked_device_ids || [];
  const recentCount = d.linked_recent_count || 0;
  const recentIds = d.linked_recent_ids || [];
  let linkBadge = "";
  if (linkedCount > 0) {
    const aliasSummary = linkedIds.slice(0, 8).join(", ") +
      (linkedIds.length > 8 ? `, +${linkedIds.length - 8}` : "");
    const tooltip = recentCount > 0
      ? `${recentCount} of ${linkedCount} are temporally adjacent (likely same device, just rotated MAC). Aliases: ${aliasSummary}`
      : `Likely the same physical device as: ${aliasSummary}`;
    const cls = recentCount > 0 ? "link-tag link-tag-strong" : "link-tag";
    const text = recentCount > 0
      ? `⚡ +${recentCount}/${linkedCount}`
      : `🔗 +${linkedCount}`;
    linkBadge = ` <span class="${cls}" title="${escapeAttr(tooltip)}">${text}</span>`;
  }
  const idCell = d._merged_count > 1
    ? `<span class="mono">${escapeHtml(d.device_id)}</span> <span class="merged-tag">+${d._merged_count - 1}</span>${linkBadge}`
    : `<span class="mono">${escapeHtml(d.device_id)}</span>${linkBadge}`;
  const wlBtn = wl
    ? `<button type="button" class="icon-btn dev-wl active" data-kind="${escapeAttr(d.kind)}" data-id="${escapeAttr(d.device_id)}" title="Whitelisted — click to remove from whitelist" aria-label="Remove from whitelist">★</button>`
    : `<button type="button" class="icon-btn dev-wl" data-kind="${escapeAttr(d.kind)}" data-id="${escapeAttr(d.device_id)}" title="Whitelist this device (silences alerts and excludes from reports)" aria-label="Add to whitelist">☆</button>`;

  // Build the JSON shown in the expandable details cell. Promote linked
  // aliases to a top-level field so they're easy to spot, and split into
  // high-confidence (temporally adjacent) and the full set.
  const detailsPayload = {};
  if (d._merged_count > 1) detailsPayload.members = d._members;
  if (linkedCount > 0) {
    if (recentCount > 0) detailsPayload.linked_aliases_high_confidence = recentIds;
    detailsPayload.linked_aliases = linkedIds;
    if (d.signature) detailsPayload.signature = d.signature;
  }
  detailsPayload.details = det;
  const summaryText = d._merged_count > 1
    ? "members + JSON"
    : (linkedCount > 0 ? `linked aliases + JSON` : "JSON");

  tr.innerHTML = `
    <td>${escapeHtml(d.kind)}</td>
    <td>${idCell}</td>
    <td>${escapeHtml(nameOrSsid)}</td>
    <td>${escapeHtml(det.vendor || "")}</td>
    <td>${d.best_rssi}</td>
    <td>${d.last_rssi ?? ""}</td>
    <td>${d.seen_count}</td>
    <td class="mono">${formatTime(d.first_seen)}</td>
    <td class="mono">${formatTime(d.last_seen)}</td>
    <td>${wlBtn}</td>
    <td><details><summary>${summaryText}</summary><pre>${escapeHtml(JSON.stringify(detailsPayload, null, 2))}</pre></details></td>
  `;
  // Wire the whitelist button — done here so each row keeps its own
  // event listener bound to the right (kind, id) pair.
  tr.querySelector(".dev-wl").addEventListener("click", (ev) => {
    ev.stopPropagation();
    quickWhitelistToggle(d.kind, d.device_id);
  });
  return tr;
}

function groupWifiByApPrefix(devices) {
  const groups = new Map();   // prefix -> aggregated row
  const out = [];
  for (const d of devices) {
    if (d.kind !== "wifi" || !d.device_id || d.device_id.length < 17) {
      out.push(d);
      continue;
    }
    const prefix = d.device_id.slice(0, 14).toLowerCase();   // "aa:bb:cc:dd:ee"
    let g = groups.get(prefix);
    if (!g) {
      // Seed with a shallow clone so we can mutate aggregate fields freely.
      g = {
        ...d,
        details: { ...(d.details || {}) },
        _members: [d.device_id],
        _merged_count: 1,
        _merged_ssids: [],
        _merged_vendors: [],
      };
      // Track SSIDs/vendors as deduped lists from the start.
      const det0 = d.details || {};
      if (det0.ssid) g._merged_ssids.push(det0.ssid);
      if (det0.vendor) g._merged_vendors.push(det0.vendor);
      groups.set(prefix, g);
      out.push(g);
      continue;
    }
    // Merge a new BSSID into the existing group.
    g._members.push(d.device_id);
    g._merged_count++;
    g.seen_count = (g.seen_count || 0) + (d.seen_count || 0);
    if (d.best_rssi != null && (g.best_rssi == null || d.best_rssi > g.best_rssi)) {
      g.best_rssi = d.best_rssi;
    }
    // last_rssi / last_seen come from the most recently-seen member.
    if (d.last_seen && (!g.last_seen || d.last_seen > g.last_seen)) {
      g.last_seen = d.last_seen;
      g.last_rssi = d.last_rssi;
    }
    if (d.first_seen && (!g.first_seen || d.first_seen < g.first_seen)) {
      g.first_seen = d.first_seen;
    }
    // Use the lowest BSSID as the canonical id so the grouping is stable.
    if (d.device_id < g.device_id) g.device_id = d.device_id;
    const det = d.details || {};
    if (det.ssid && !g._merged_ssids.includes(det.ssid)) g._merged_ssids.push(det.ssid);
    if (det.vendor && !g._merged_vendors.includes(det.vendor)) g._merged_vendors.push(det.vendor);
    if (!g.details.vendor && det.vendor) g.details.vendor = det.vendor;
  }
  return out;
}

$("#dev-refresh").addEventListener("click", refreshDevices);
$("#dev-location").addEventListener("change", refreshDevices);
$("#dev-kind").addEventListener("change", refreshDevices);
$("#dev-group-bssid").addEventListener("change", refreshDevices);
// Search and time-range filter are local to the rendered set, so refilter
// without re-fetching. Debounce the search input to keep typing snappy.
let _devSearchTimer = null;
$("#dev-search")?.addEventListener("input", () => {
  clearTimeout(_devSearchTimer);
  _devSearchTimer = setTimeout(refreshDevices, 150);
});
$("#dev-since")?.addEventListener("change", refreshDevices);

// Quick jump from the Devices tab to the whitelist editor in Settings.
$("#dev-manage-whitelist")?.addEventListener("click", () => {
  const settingsBtn = $$(".tab-btn").find(b => b.dataset.tab === "settings");
  if (settingsBtn) settingsBtn.click();
  // The Settings tab needs a tick to become display:block before scrollIntoView
  // can compute a real offset.
  setTimeout(() => {
    const panel = $("#whitelist-panel");
    if (panel) {
      panel.scrollIntoView({ behavior: "smooth", block: "start" });
      panel.querySelector("input[name='device_id']")?.focus();
    }
  }, 50);
});

// ---------- locations tab ----------
// Inline SVG icons — small currentColor glyphs so they pick up button text colour.
const ICON_FLOPPY = `<svg viewBox="0 0 16 16" width="14" height="14" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M2 2h9l3 3v9a1 1 0 0 1-1 1H3a1 1 0 0 1-1-1V3a1 1 0 0 1 1-1Z"/><path d="M4 2v4h7V2"/><path d="M5 10h6v4H5z"/></svg>`;
const ICON_TRASH  = `<svg viewBox="0 0 16 16" width="14" height="14" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M3 4h10"/><path d="M5 4V2.5A.5.5 0 0 1 5.5 2h5a.5.5 0 0 1 .5.5V4"/><path d="M4 4l1 9.5a1 1 0 0 0 1 .9h4a1 1 0 0 0 1-.9L12 4"/><path d="M6.5 7v5M9.5 7v5"/></svg>`;
const ICON_PAUSE  = `<svg viewBox="0 0 16 16" width="16" height="16" aria-hidden="true" fill="currentColor"><rect x="3.5" y="2" width="3" height="12" rx="0.5"/><rect x="9.5" y="2" width="3" height="12" rx="0.5"/></svg>`;
const ICON_PLAY   = `<svg viewBox="0 0 16 16" width="16" height="16" aria-hidden="true" fill="currentColor"><path d="M3.5 2.5v11a.5.5 0 0 0 .77.42l8.5-5.5a.5.5 0 0 0 0-.84l-8.5-5.5A.5.5 0 0 0 3.5 2.5z"/></svg>`;
// Sensor tracking — crosshair / target.
const ICON_CROSSHAIR = `<svg viewBox="0 0 16 16" width="16" height="16" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"><circle cx="8" cy="8" r="6.5"/><circle cx="8" cy="8" r="2" fill="currentColor"/><line x1="8" y1="0.5" x2="8" y2="2.5"/><line x1="8" y1="13.5" x2="8" y2="15.5"/><line x1="0.5" y1="8" x2="2.5" y2="8"/><line x1="13.5" y1="8" x2="15.5" y2="8"/></svg>`;
// Smart tracking — navigation arrow (think "follow but smart").
const ICON_NAV = `<svg viewBox="0 0 16 16" width="16" height="16" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linejoin="round"><path d="M14 2 L2 7 L7 9 L9 14 Z" fill="currentColor" fill-opacity="0.4"/></svg>`;
// Auto-zoom — four corner brackets ("fit to view").
const ICON_FIT = `<svg viewBox="0 0 16 16" width="16" height="16" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M2 5.5V2.5h3"/><path d="M14 5.5V2.5h-3"/><path d="M2 10.5v3h3"/><path d="M14 10.5v3h-3"/></svg>`;
// Dashed circle = "draw a geofence". Reads as a marquee / selection ring.
const ICON_DRAW = `<svg viewBox="0 0 16 16" width="16" height="16" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><circle cx="8" cy="8" r="6" stroke-dasharray="2,2"/><circle cx="8" cy="8" r="1.6" fill="currentColor"/></svg>`;

async function refreshLocations() {
  const { locations, active_id } = await api("/api/locations");
  const tbody = $("#loc-table tbody");
  tbody.innerHTML = "";
  for (const loc of locations) {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td>${loc.id}${loc.id === active_id ? " ●" : ""}</td>
      <td><input class="loc-label" data-id="${loc.id}" value="${escapeAttr(loc.label || "")}" /></td>
      <td class="mono">${loc.lat.toFixed(6)}</td>
      <td class="mono">${loc.lon.toFixed(6)}</td>
      <td>${loc.radius_m}</td>
      <td>${loc.fix_count}</td>
      <td class="mono">${formatTime(loc.created_at)}</td>
      <td class="mono">${formatTime(loc.last_seen_at)}</td>
      <td class="row-actions">
        <button type="button" class="icon-btn save-label" data-id="${loc.id}" title="Save label changes" aria-label="Save label">${ICON_FLOPPY}</button>
        <button type="button" class="icon-btn danger delete-loc" data-id="${loc.id}" title="Delete this location and all of its devices/observations" aria-label="Delete location">${ICON_TRASH}</button>
      </td>
    `;
    tbody.appendChild(tr);
  }
  $$(".save-label").forEach((b) =>
    b.addEventListener("click", async () => {
      const id = b.dataset.id;
      const input = $(`.loc-label[data-id="${id}"]`);
      await api(`/api/locations/${id}`, { method: "PATCH", body: JSON.stringify({ label: input.value }) });
      refreshLocations();
    })
  );
  $$(".delete-loc").forEach((b) =>
    b.addEventListener("click", async () => {
      const id = b.dataset.id;
      if (!confirm(`Delete location #${id}?\n\nThis permanently removes its devices and observations. This cannot be undone.`)) return;
      try {
        await api(`/api/locations/${id}`, { method: "DELETE" });
        await refreshLocations();
        await refreshLocationMarkers();
        await loadLocationOptions();
      } catch (e) {
        alert("Delete failed: " + e.message);
      }
    })
  );
}

$("#loc-refresh").addEventListener("click", refreshLocations);
$("#loc-new").addEventListener("click", async () => {
  try { await api("/api/locations/new", { method: "POST" }); refreshLocations(); }
  catch (e) { alert(e.message); }
});

$("#loc-merge").addEventListener("click", async () => {
  const btn = $("#loc-merge");
  const orig = btn.textContent;
  btn.disabled = true; btn.textContent = "Checking…";
  try {
    const preview = await api("/api/locations/contained");
    const pairs = preview.pairs || [];
    if (pairs.length === 0) {
      alert("No contained locations found — nothing to merge.");
      return;
    }
    const summary = pairs.slice(0, 10)
      .map(p => {
        const pct = p.overlap_ratio != null ? `${Math.round(p.overlap_ratio * 100)}% overlap` : `${p.distance_m.toFixed(1)}m apart`;
        return `  • #${p.loser_id} (r=${p.loser_radius}m) into #${p.winner_id} (r=${p.winner_radius}m, ${pct})`;
      })
      .join("\n");
    const more = pairs.length > 10 ? `\n  …and ${pairs.length - 10} more` : "";
    if (!confirm(
      `Found ${pairs.length} containment pair(s). Merging will combine each loser's ` +
      `devices and observations into its container, expand the container's radius if ` +
      `needed, and delete the loser. Chains (A inside B inside C) collapse to the outermost.\n\n` +
      `${summary}${more}\n\nProceed?`
    )) return;
    btn.textContent = "Merging…";
    const r = await api("/api/locations/merge_contained", { method: "POST" });
    const totals = (r.details || []).reduce((acc, d) => ({
      devices_moved: acc.devices_moved + (d.devices_moved || 0),
      devices_combined: acc.devices_combined + (d.devices_combined || 0),
      observations_moved: acc.observations_moved + (d.observations_moved || 0),
    }), { devices_moved: 0, devices_combined: 0, observations_moved: 0 });
    alert(
      `Merged ${r.merged} location(s).\n\n` +
      `Devices reattributed: ${totals.devices_moved}\n` +
      `Devices combined (collisions): ${totals.devices_combined}\n` +
      `Observations moved: ${totals.observations_moved}`
    );
    await refreshLocations();
    await refreshLocationMarkers();
    await loadLocationOptions();
  } catch (e) {
    alert("Merge failed: " + e.message);
  } finally {
    btn.disabled = false; btn.textContent = orig;
  }
});

$("#loc-report").addEventListener("click", async () => {
  const btn = $("#loc-report");
  const orig = btn.textContent;
  btn.disabled = true; btn.textContent = "Generating…";
  try {
    // Mirror the Devices tab's "Group multi-BSSID APs" checkbox so the PDF
    // device tables match what the user sees on the Devices tab.
    const groupBssids = $("#dev-group-bssid")?.checked ? 1 : 0;
    const res = await fetch(`/api/locations/report.pdf?group_bssids=${groupBssids}`, { method: "GET" });
    if (!res.ok) {
      const t = await res.text();
      throw new Error(t || `HTTP ${res.status}`);
    }
    const blob = await res.blob();
    const url = URL.createObjectURL(blob);
    const cd = res.headers.get("Content-Disposition") || "";
    const m = cd.match(/filename="([^"]+)"/);
    const a = document.createElement("a");
    a.href = url;
    a.download = m ? m[1] : "gjallarhorn-report.pdf";
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 5000);
  } catch (e) {
    alert("Report generation failed: " + e.message);
  } finally {
    btn.disabled = false; btn.textContent = orig;
  }
});
$("#loc-delete-all").addEventListener("click", async () => {
  const ok = confirm(
    "Delete ALL locations?\n\n" +
    "This permanently removes every sensor location AND every device " +
    "and observation tied to them. The active location will be re-opened " +
    "from the next GPS fix.\n\n" +
    "This cannot be undone."
  );
  if (!ok) return;
  const btn = $("#loc-delete-all");
  btn.disabled = true; btn.textContent = "Deleting…";
  try {
    const res = await api("/api/locations", { method: "DELETE" });
    const d = res.deleted || {};
    alert(`Deleted ${d.locations || 0} locations, ${d.devices || 0} devices, ${d.observations || 0} observations.`);
    await refreshLocations();
    await refreshLocationMarkers();
    await loadLocationOptions();
  } catch (e) {
    alert("Delete failed: " + e.message);
  } finally {
    btn.disabled = false; btn.textContent = "Delete all";
  }
});

// ---------- settings tab ----------
function fmtKV(label, value) {
  if (value === null || value === undefined || value === "") return "";
  return `<dt>${escapeHtml(label)}</dt><dd>${escapeHtml(String(value))}</dd>`;
}

function renderWifiCard(iface) {
  const connected = !!iface.ssid;
  const badges = [];
  if (connected) badges.push(`<span class="badge warn">connected → ${escapeHtml(iface.ssid)}</span>`);
  if (iface.type) badges.push(`<span class="badge">${escapeHtml(iface.type)}</span>`);
  if (iface.band) badges.push(`<span class="badge">${escapeHtml(iface.band)}</span>`);
  return `
    <div class="iface-card ${connected ? "connected" : ""}">
      <h4>${escapeHtml(iface.name)} ${badges.join(" ")}</h4>
      <dl>
        ${fmtKV("MAC", iface.mac)}
        ${fmtKV("Type", iface.type)}
        ${fmtKV("SSID", iface.ssid || "(not associated)")}
        ${fmtKV("Channel", iface.channel)}
        ${fmtKV("Frequency", iface.frequency_mhz ? iface.frequency_mhz + " MHz" : null)}
        ${fmtKV("Width", iface.width)}
        ${fmtKV("TX power", iface.txpower_dbm != null ? iface.txpower_dbm + " dBm" : null)}
      </dl>
    </div>`;
}

function renderBtCard(adapter) {
  const badges = [];
  if (adapter.powered === true) badges.push(`<span class="badge ok">powered</span>`);
  if (adapter.powered === false) badges.push(`<span class="badge warn">off</span>`);
  if (adapter.discovering) badges.push(`<span class="badge">discovering</span>`);
  if (adapter.discoverable) badges.push(`<span class="badge">discoverable</span>`);
  return `
    <div class="iface-card">
      <h4>${escapeHtml(adapter.name)} ${badges.join(" ")}</h4>
      <dl>
        ${fmtKV("Address", adapter.address)}
        ${fmtKV("Alias", adapter.alias)}
        ${fmtKV("Powered", adapter.powered)}
        ${fmtKV("Discoverable", adapter.discoverable)}
        ${fmtKV("Pairable", adapter.pairable)}
        ${fmtKV("Discovering", adapter.discovering)}
        ${fmtKV("Class", adapter.class != null ? "0x" + Number(adapter.class).toString(16) : null)}
      </dl>
    </div>`;
}

async function loadInterfaces() {
  // WiFi
  const wifi = await api("/api/interfaces/wifi");
  const wsel = $("#set-wifi-iface");
  wsel.innerHTML = `
    <option value="">(none — disabled)</option>
    <option value="auto">(auto — pick first available)</option>
  `;
  for (const iface of wifi.interfaces) {
    const o = document.createElement("option");
    o.value = iface.name;
    const tag = iface.ssid ? ` — connected to ${iface.ssid}` : "";
    o.textContent = `${iface.name}${tag}`;
    if (iface.ssid) {
      o.disabled = true;
      o.title = "Disabled: interface is associated with an access point. Disconnect it before using for scans.";
    }
    wsel.appendChild(o);
  }
  $("#wifi-iface-info").innerHTML = wifi.interfaces.length
    ? wifi.interfaces.map(renderWifiCard).join("")
    : `<div class="muted">No WiFi interfaces detected (requires Linux + <code>iw</code>).</div>`;

  // Probe scanner — same interface list, but associated interfaces stay
  // selectable (the auto-monitor path will yank them into monitor mode and
  // drop the existing association on purpose). Each option's current iw
  // type is surfaced in the label so monitor-ready interfaces are obvious;
  // they're also sorted to the top.
  const psel = $("#set-probe-iface");
  if (psel) {
    const prev = psel.value;
    psel.innerHTML = `<option value="">(none — disabled)</option>`;
    const rankIface = (i) => {
      if (i.type === "monitor") return 0;
      if (!i.ssid) return 1;
      return 2;
    };
    const ranked = [...wifi.interfaces].sort((a, b) => rankIface(a) - rankIface(b));
    for (const iface of ranked) {
      const o = document.createElement("option");
      o.value = iface.name;
      let tag;
      if (iface.type === "monitor") {
        tag = " — monitor mode ✓";
        o.title = "Already in monitor mode — probe scanner can capture immediately.";
      } else if (iface.ssid) {
        tag = ` — ${iface.type || "managed"}, connected to ${iface.ssid} (auto-monitor will disconnect)`;
        o.title = "Selecting this interface with auto-monitor on will disconnect it from its current AP.";
      } else {
        tag = ` — ${iface.type || "managed"}`;
        o.title = "Not in monitor mode. Enable auto-monitor, or set monitor mode manually before starting the scanner.";
      }
      o.textContent = `${iface.name}${tag}`;
      psel.appendChild(o);
    }
    if (prev) psel.value = prev;  // preserve user's saved selection
  }

  // Bluetooth
  const bt = await api("/api/interfaces/bluetooth");
  const bsel = $("#set-bt-adapter");
  bsel.innerHTML = `<option value="">(default)</option>`;
  for (const a of bt.adapters) {
    const o = document.createElement("option");
    o.value = a.name;
    const addr = a.address ? ` · ${a.address}` : "";
    const power = a.powered === false ? " · off" : "";
    o.textContent = `${a.name}${addr}${power}`;
    if (a.powered === false) {
      o.disabled = true;
      o.title = "Disabled: adapter is not powered on.";
    }
    bsel.appendChild(o);
  }
  $("#bt-adapter-info").innerHTML = bt.adapters.length
    ? bt.adapters.map(renderBtCard).join("")
    : `<div class="muted">No Bluetooth adapters detected.</div>`;
}

async function loadSettings() {
  const s = await api("/api/settings");
  for (const [k, v] of Object.entries(s)) {
    const el = document.querySelector(`[name="${k}"]`);
    if (!el) continue;
    if (el.type === "checkbox") el.checked = !!v;
    else el.value = v ?? "";
  }
  await applyMapProvider(s.map_provider);
  applyLocDynamicEnabled();
  // Kick off the probe channel checkbox grid for the saved interface (if any).
  refreshProbeChannelsForIface((s.probe_interface || "").trim() || null);
}

function applyLocDynamicEnabled() {
  const cb = $("#set-loc-dynamic");
  const t = $("#set-loc-dynamic-t");
  if (!cb || !t) return;
  t.disabled = !cb.checked;
  t.style.opacity = cb.checked ? "1" : "0.5";
}
$("#set-loc-dynamic")?.addEventListener("change", applyLocDynamicEnabled);

$("#settings-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const fd = new FormData(e.target);
  const payload = {};
  for (const el of e.target.elements) {
    if (!el.name) continue;
    if (el.type === "checkbox") payload[el.name] = el.checked;
    else if (el.type === "number") payload[el.name] = el.value === "" ? null : Number(el.value);
    else payload[el.name] = el.value || null;
  }
  $("#save-status").textContent = "saving…";
  try {
    const updated = await api("/api/settings", { method: "PUT", body: JSON.stringify(payload) });
    $("#save-status").textContent = "saved";
    await applyMapProvider(updated.map_provider);
    setTimeout(() => ($("#save-status").textContent = ""), 1500);
  } catch (err) {
    $("#save-status").textContent = "error: " + err.message;
  }
});

// ── probe scanner channel picker ──────────────────────
let _probeChannelsCache = null;       // most recent fetched channel list
let _probeChannelsIface = null;       // iface those channels were fetched for

function _parseChannelList(s) {
  // Tolerant of whitespace and bad entries; mirrors services/probe_scanner.parse_channels.
  return new Set(
    (s || "").split(",")
      .map(p => parseInt(p.trim(), 10))
      .filter(n => Number.isInteger(n) && n >= 1 && n <= 196)
  );
}

function _serializeChannels(set) {
  return [...set].sort((a, b) => a - b).join(",");
}

function renderProbeChannels(channels, selected) {
  const grid = $("#probe-channels-grid");
  if (!grid) return;
  if (!channels || !channels.length) {
    grid.className = "muted";
    grid.textContent = _probeChannelsIface
      ? `No supported channels reported for ${_probeChannelsIface} (iw not available, or interface offline?).`
      : "Pick an interface above to see its supported channels.";
    updateProbeChannelsSummary(selected);
    return;
  }
  grid.className = "";
  grid.innerHTML = "";
  // Group by band; render each band as its own grid.
  const bands = {};
  for (const c of channels) {
    (bands[c.band] = bands[c.band] || []).push(c);
  }
  // Stable order: 2.4 → 5 → 6 → other
  const ORDER = ["2.4GHz", "5GHz", "6GHz"];
  const bandKeys = Object.keys(bands).sort((a, b) => {
    const ai = ORDER.indexOf(a), bi = ORDER.indexOf(b);
    return (ai === -1 ? 99 : ai) - (bi === -1 ? 99 : bi);
  });
  for (const band of bandKeys) {
    const wrap = document.createElement("div");
    wrap.className = "probe-channels-band";
    wrap.innerHTML = `<div class="probe-channels-band-title">${escapeHtml(band)}</div>`;
    const inner = document.createElement("div");
    inner.className = "probe-channels-grid";
    for (const c of bands[band]) {
      const checked = selected.has(c.channel);
      const flags = [];
      if (c.no_ir)    flags.push("no-IR");
      if (c.disabled) flags.push("disabled");
      const flagStr = flags.length ? ` (${flags.join(", ")})` : "";
      const label = document.createElement("label");
      label.title = `${c.freq_mhz} MHz${flagStr}`;
      if (c.disabled) label.classList.add("disabled");
      if (c.no_ir)    label.classList.add("no-ir");
      label.innerHTML = `
        <input type="checkbox" value="${c.channel}" ${checked ? "checked" : ""} />
        <span>${c.channel}</span>
      `;
      label.querySelector("input").addEventListener("change", () => {
        const cur = _parseChannelList($("#probe-channels-value").value);
        const ch = c.channel;
        if (label.querySelector("input").checked) cur.add(ch); else cur.delete(ch);
        $("#probe-channels-value").value = _serializeChannels(cur);
        updateProbeChannelsSummary(cur);
      });
      inner.appendChild(label);
    }
    wrap.appendChild(inner);
    grid.appendChild(wrap);
  }
  updateProbeChannelsSummary(selected);
}

function updateProbeChannelsSummary(selected) {
  const out = $("#probe-channels-summary");
  if (!out) return;
  if (!selected || !selected.size) {
    out.textContent = "none selected — hopping disabled";
    return;
  }
  const list = _serializeChannels(selected);
  out.textContent = `${selected.size} selected: ${list}`;
}

async function refreshProbeChannelsForIface(iface) {
  const selected = _parseChannelList($("#probe-channels-value").value);
  if (!iface) {
    _probeChannelsCache = null;
    _probeChannelsIface = null;
    renderProbeChannels(null, selected);
    return;
  }
  try {
    const r = await api(`/api/interfaces/wifi/${encodeURIComponent(iface)}/channels`);
    _probeChannelsCache = r.channels || [];
    _probeChannelsIface = iface;
    renderProbeChannels(_probeChannelsCache, selected);
  } catch (e) {
    _probeChannelsCache = [];
    _probeChannelsIface = iface;
    renderProbeChannels([], selected);
  }
}

$("#set-probe-iface")?.addEventListener("change", (e) => {
  refreshProbeChannelsForIface(e.target.value || null);
});
$("#probe-channels-defaults")?.addEventListener("click", () => {
  $("#probe-channels-value").value = "1,6,11";
  // Re-render to reflect the new state in the checkboxes.
  renderProbeChannels(_probeChannelsCache, _parseChannelList("1,6,11"));
});
$("#probe-channels-clear")?.addEventListener("click", () => {
  $("#probe-channels-value").value = "";
  renderProbeChannels(_probeChannelsCache, new Set());
});

// Cached for the per-second relative-time ticker, and for rate computation
// across successive /api/probe/status polls.
let _probeLastStatus = null;
let _probePrevCount = null;
let _probePrevAt = null;
let _probeRateText = "—";

async function refreshProbeStatus() {
  try {
    const s = await api("/api/probe/status");
    _probeLastStatus = s;

    // Compute rate (probes/min) from the delta since the previous poll.
    const now = Date.now() / 1000;
    const count = s.probe_count || 0;
    if (_probePrevCount !== null && _probePrevAt !== null && s.running) {
      const dt = now - _probePrevAt;
      const dn = count - _probePrevCount;
      if (dt > 0 && dn >= 0) {
        const perMin = (dn / dt) * 60;
        if (perMin >= 100) _probeRateText = `${perMin.toFixed(0)}/min`;
        else if (perMin >= 10) _probeRateText = `${perMin.toFixed(1)}/min`;
        else if (perMin > 0) _probeRateText = `${perMin.toFixed(2)}/min`;
        else _probeRateText = "0/min";
      }
    } else if (!s.running) {
      _probeRateText = "—";
    }
    _probePrevCount = count;
    _probePrevAt = now;

    // ---- Settings-tab panel (existing) ----
    const stateEl = $("#probe-state");
    if (stateEl) {
      if (s.running) {
        const tag = s.auto_monitor ? " · auto-monitor" : "";
        stateEl.innerHTML = `<span class="pill ok">running on ${escapeHtml(s.interface || "")}${tag}</span>`;
      } else if (s.last_error) {
        stateEl.innerHTML = `<span class="pill err">${escapeHtml(s.last_error)}</span>`;
      } else {
        stateEl.innerHTML = `<span class="pill">disabled</span>`;
      }
      // Live stats (channel, probe count, last-probe time, rate, uptime)
      // live on the Map sidebar card now — no need to duplicate them in
      // the Settings dl. Tshark/scapy availability is configuration info
      // and stays here.
      const tsharkEl = $("#probe-tshark");
      if (tsharkEl) {
        tsharkEl.innerHTML = s.tshark_available
          ? `<span class="pill ok">available</span>`
          : `<span class="pill warn">missing — apt install tshark</span>`;
      }
      const scapyEl = $("#probe-scapy");
      if (scapyEl) {
        scapyEl.innerHTML = s.scapy_available
          ? `<span class="pill ok">available</span>`
          : `<span class="pill warn">missing — pip install scapy</span>`;
      }
    }

    // ---- Map-sidebar card ----
    updateProbeMapCard(s);
  } catch (e) {
    const stateEl = $("#probe-state");
    if (stateEl) stateEl.textContent = "error: " + e.message;
    const mapErr = $("#probe-map-error");
    const mapState = $("#probe-map-state");
    if (mapState) {
      mapState.className = "probe-state-pill error";
      mapState.textContent = "error";
    }
    if (mapErr) {
      mapErr.hidden = false;
      mapErr.textContent = "status fetch failed: " + e.message;
    }
  }
}

function updateProbeMapCard(s) {
  const stateEl = $("#probe-map-state");
  if (!stateEl) return;

  // Status pill
  if (s.running) {
    stateEl.className = "probe-state-pill running";
    stateEl.textContent = "running";
  } else if (s.last_error) {
    stateEl.className = "probe-state-pill error";
    stateEl.textContent = "error";
  } else {
    stateEl.className = "probe-state-pill stopped";
    stateEl.textContent = "stopped";
  }

  // Meta line: iface · backend · auto-monitor (or hint when not configured)
  const metaEl = $("#probe-map-meta");
  if (metaEl) {
    if (s.running) {
      const parts = [];
      if (s.interface) parts.push(s.interface);
      if (s.backend) parts.push(s.backend);
      if (s.auto_monitor) parts.push("auto-monitor");
      metaEl.textContent = parts.join(" · ") || "—";
    } else if (s.interface) {
      metaEl.textContent = `${s.interface} · ${s.backend || ""} · idle`;
    } else {
      metaEl.textContent = "Not configured — set interface in Settings";
    }
  }

  // Error banner: scanner-level error when stopped, or a channel-set
  // failure while running (channel-hop iw calls being denied is a common
  // silent-failure mode — surfacing it here saves a trip to the Logs tab).
  const errEl = $("#probe-map-error");
  if (errEl) {
    let msg = null;
    if (s.last_error && !s.running) msg = s.last_error;
    else if (s.running && s.last_channel_error) msg = `channel-set failing: ${s.last_channel_error}`;
    if (msg) {
      errEl.hidden = false;
      errEl.textContent = msg;
    } else {
      errEl.hidden = true;
    }
  }

  // Channel: prefer the live current_channel; if unknown but a hop list is
  // configured and we're running, show "cycling" so the card matches what
  // the Settings tab reports.
  const ch = s.current_channel;
  const hop = s.channels || [];
  const chEl = $("#probe-map-channel");
  if (chEl) {
    if (ch != null) chEl.textContent = String(ch);
    else if (hop.length && s.running) chEl.textContent = "cycling…";
    else chEl.textContent = "—";
  }

  // Probes captured
  const countEl = $("#probe-map-count");
  if (countEl) countEl.textContent = (s.probe_count || 0).toLocaleString();

  // Rate (cached, recomputed on each poll)
  const rateEl = $("#probe-map-rate");
  if (rateEl) rateEl.textContent = _probeRateText;

  // Channel-hop summary in footer (uses `hop` from earlier in the function)
  const hopEl = $("#probe-map-hop");
  if (hopEl) {
    if (hop.length) hopEl.textContent = `Hopping ${hop.join(",")}`;
    else hopEl.textContent = "No channel hop";
  }

  // Relative-time fields are rendered immediately, then re-rendered every
  // second by tickProbeRelativeTimes() so they don't go stale between polls.
  tickProbeRelativeTimes();
}

function tickProbeRelativeTimes() {
  const s = _probeLastStatus;
  const lastEl = $("#probe-map-last");
  const upEl = $("#probe-map-uptime");
  if (!lastEl || !upEl) return;
  if (!s) {
    lastEl.textContent = "—";
    upEl.textContent = "—";
    return;
  }
  const now = Date.now() / 1000;
  lastEl.textContent = s.last_probe_at ? formatProbeAgo(now - s.last_probe_at) : "never";
  upEl.textContent = (s.started_at && s.running) ? formatProbeDuration(now - s.started_at) : "—";
}

function formatProbeAgo(seconds) {
  if (!isFinite(seconds) || seconds < 0) return "—";
  if (seconds < 1) return "now";
  return formatProbeDuration(seconds) + " ago";
}

function formatProbeDuration(seconds) {
  if (!isFinite(seconds) || seconds < 0) return "—";
  const s = Math.floor(seconds);
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ${s % 60}s`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}h ${m % 60}m`;
  const d = Math.floor(h / 24);
  return `${d}d ${h % 24}h`;
}

async function refreshTileCache() {
  try {
    const s = await api("/api/tilecache/status");
    $("#tc-count").textContent = s.count.toLocaleString();
    $("#tc-size").textContent = formatBytes(s.bytes);
    $("#tc-dir").textContent = s.dir;
  } catch (e) {
    $("#tc-status").textContent = "error: " + e.message;
  }
}

$("#tc-refresh").addEventListener("click", refreshTileCache);

$("#tc-clear").addEventListener("click", async () => {
  if (!confirm("Delete every cached map tile?\n\nReports generated next will re-fetch tiles from OpenStreetMap on demand.")) return;
  const btn = $("#tc-clear");
  btn.disabled = true;
  $("#tc-status").textContent = "clearing…";
  try {
    const r = await api("/api/tilecache/clear", { method: "POST" });
    $("#tc-status").textContent = `removed ${r.removed} tile${r.removed === 1 ? "" : "s"} (${formatBytes(r.freed_bytes)})`;
    await refreshTileCache();
    setTimeout(() => ($("#tc-status").textContent = ""), 4000);
  } catch (e) {
    $("#tc-status").textContent = "error: " + e.message;
  } finally {
    btn.disabled = false;
  }
});

$("#purge-now")?.addEventListener("click", async () => {
  const status = $("#purge-status");
  const btn = $("#purge-now");
  // Use whatever the form currently shows, even if not yet saved — that
  // way the user can experiment with thresholds before committing.
  const obs = parseInt(document.querySelector("[name=observation_retention_days]")?.value || "0", 10);
  const dev = parseInt(document.querySelector("[name=device_retention_days]")?.value || "0", 10);
  if ((!obs || obs <= 0) && (!dev || dev <= 0)) {
    status.textContent = "both thresholds are 0 — nothing to purge";
    return;
  }
  if (!confirm(
    `Purge rows with the current thresholds?\n\n` +
    `Observations older than: ${obs > 0 ? obs + " days" : "(disabled)"}\n` +
    `Devices last seen >: ${dev > 0 ? dev + " days" : "(disabled)"}\n\n` +
    `This cannot be undone.`
  )) return;
  btn.disabled = true;
  status.textContent = "purging…";
  try {
    const r = await api("/api/maintenance/purge", {
      method: "POST",
      body: JSON.stringify({ observation_days: obs, device_days: dev }),
    });
    const removed = r.removed || {};
    status.textContent =
      `removed ${removed.observations || 0} observations, ${removed.devices || 0} devices`;
    setTimeout(() => (status.textContent = ""), 6000);
    await refreshLocations?.();
  } catch (e) {
    status.textContent = "error: " + e.message;
  } finally {
    btn.disabled = false;
  }
});

$("#discord-test").addEventListener("click", async () => {
  const status = $("#discord-test-status");
  status.textContent = "sending…";
  try {
    await api("/api/settings/notifications/discord/test", { method: "POST" });
    status.textContent = "sent — check Discord";
  } catch (err) {
    status.textContent = "failed: " + err.message;
  }
  setTimeout(() => (status.textContent = ""), 4000);
});

// ---------- alerts ----------
const MATCH_TYPE_LABEL = {
  device_id: "device id",
  name_contains: "name contains",
  vendor_contains: "vendor contains",
  rssi_above: "RSSI ≥",
  new_device: "new device (after Ns)",
  cross_location: "cross-location M/N",
};
let alertsLastSeenId = 0;

// Rules cache for double-click-to-edit; populated on every refresh.
let rulesById = {};

async function refreshAlertRules() {
  const { rules } = await api("/api/alerts/rules");
  rulesById = Object.fromEntries(rules.map(r => [String(r.id), r]));
  const tbody = $("#rules-table tbody");
  tbody.innerHTML = "";
  for (const r of rules) {
    const tr = document.createElement("tr");
    tr.dataset.id = r.id;
    tr.title = "Double-click to edit";
    const extras = Array.isArray(r.extra_conditions) ? r.extra_conditions : [];
    const extraTip = extras.length
      ? extras.map(c => `${MATCH_TYPE_LABEL[c.match_type] || c.match_type} = ${c.match_value}`).join(" AND ")
      : "";
    const extraBadge = extras.length
      ? ` <span class="rule-extra-badge" title="${escapeAttr("AND " + extraTip)}">+${extras.length} AND</span>`
      : "";
    tr.innerHTML = `
      <td><input type="checkbox" class="rule-toggle" data-id="${r.id}" ${r.enabled ? "checked" : ""}></td>
      <td>${escapeHtml(r.name)}</td>
      <td>${escapeHtml(r.kind || "any")}</td>
      <td>${escapeHtml(MATCH_TYPE_LABEL[r.match_type] || r.match_type)}${extraBadge}</td>
      <td class="mono">${escapeHtml(r.match_value)}</td>
      <td>${r.location_id ?? "any"}</td>
      <td><input type="checkbox" class="rule-discord" data-id="${r.id}" ${r.notify_discord ? "checked" : ""}></td>
      <td><input type="checkbox" class="rule-audible" data-id="${r.id}" ${r.audible ? "checked" : ""}></td>
      <td class="mono">${formatTime(r.created_at)}</td>
      <td><button class="danger rule-delete" data-id="${r.id}">Delete</button></td>
    `;
    tr.addEventListener("dblclick", (ev) => {
      // Don't hijack double-clicks on inline controls
      if (ev.target.closest("input, button")) return;
      enterEditRuleMode(rulesById[r.id]);
    });
    tbody.appendChild(tr);
  }
  $$(".rule-toggle").forEach(cb =>
    cb.addEventListener("change", async () => {
      await api(`/api/alerts/rules/${cb.dataset.id}`, {
        method: "PATCH", body: JSON.stringify({ enabled: cb.checked }),
      });
    })
  );
  $$(".rule-discord").forEach(cb =>
    cb.addEventListener("change", async () => {
      await api(`/api/alerts/rules/${cb.dataset.id}`, {
        method: "PATCH", body: JSON.stringify({ notify_discord: cb.checked }),
      });
    })
  );
  $$(".rule-audible").forEach(cb =>
    cb.addEventListener("change", async () => {
      await api(`/api/alerts/rules/${cb.dataset.id}`, {
        method: "PATCH", body: JSON.stringify({ audible: cb.checked }),
      });
      // Resume the audio context on this user gesture so future alarms
      // aren't blocked by browser autoplay policy.
      if (cb.checked) primeAudio();
    })
  );
  $$(".rule-delete").forEach(b =>
    b.addEventListener("click", async () => {
      if (!confirm("Delete this rule and its alert history?")) return;
      await api(`/api/alerts/rules/${b.dataset.id}`, { method: "DELETE" });
      refreshAlertRules();
    })
  );
  // Populate the location dropdown in the form
  try {
    const locs = await api("/api/locations");
    const sel = $("#rule-location");
    sel.innerHTML = `<option value="">any</option>`;
    for (const loc of locs.locations || []) {
      const o = document.createElement("option");
      o.value = loc.id; o.textContent = `${loc.id} · ${loc.label || ""}`.trim();
      sel.appendChild(o);
    }
  } catch {}
}

// Last-fetched events kept in memory so filter/sort can re-render without
// hitting the server again.
let alertsCache = [];

async function refreshAlertEvents({ silent = false } = {}) {
  const { events } = await api(`/api/alerts/events?limit=500`);
  alertsCache = events || [];
  syncAlertsRuleFilter();
  renderFilteredAlerts();
  if (alertsCache.length && alertsCache[0].id > alertsLastSeenId && !silent) {
    // New events arrived since last check
  }
  if (alertsCache.length) alertsLastSeenId = alertsCache[0].id;
}

function syncAlertsRuleFilter() {
  // Populate the rule dropdown from whatever rule_ids appear in the cache.
  const sel = $("#alerts-rule");
  if (!sel) return;
  const previous = sel.value;
  const seen = new Map();
  for (const e of alertsCache) {
    if (e.rule_id != null && !seen.has(e.rule_id)) {
      seen.set(e.rule_id, e.rule_name || `rule ${e.rule_id}`);
    }
  }
  sel.innerHTML = `<option value="">any rule</option>` +
    [...seen.entries()]
      .sort((a, b) => String(a[1]).localeCompare(String(b[1])))
      .map(([id, name]) => `<option value="${id}">${escapeHtml(name)}</option>`)
      .join("");
  // Preserve the user's selection if that rule is still represented.
  if (previous && seen.has(Number(previous))) sel.value = previous;
}

function applyAlertFilters(events) {
  const q = ($("#alerts-search")?.value || "").trim().toLowerCase();
  const kind = $("#alerts-kind")?.value || "";
  const rule = $("#alerts-rule")?.value || "";
  const sort = $("#alerts-sort")?.value || "newest";

  let out = events.filter((e) => {
    if (kind && e.device_kind !== kind) return false;
    if (rule && String(e.rule_id) !== rule) return false;
    if (q) {
      const det = e.details || {};
      const hay = [
        e.device_id, e.rule_name, det.ssid, det.name, det.vendor,
      ].filter(Boolean).join(" ").toLowerCase();
      if (!hay.includes(q)) return false;
    }
    return true;
  });

  out.sort((a, b) => {
    if (sort === "oldest") return a.id - b.id;
    if (sort === "rssi_strong") return (b.rssi ?? -999) - (a.rssi ?? -999);
    if (sort === "rssi_weak")   return (a.rssi ?? 999)  - (b.rssi ?? 999);
    return b.id - a.id; // newest
  });
  return out;
}

function renderFilteredAlerts() {
  const list = $("#alerts-list");
  const counter = $("#alerts-count");
  const total = alertsCache.length;
  if (!total) {
    list.innerHTML = `<div class="muted">No alerts yet.</div>`;
    if (counter) counter.textContent = "";
    setBadge(0);
    return;
  }
  const shown = applyAlertFilters(alertsCache);
  if (counter) {
    counter.textContent = shown.length === total
      ? `${total} event${total === 1 ? "" : "s"}`
      : `${shown.length} of ${total}`;
  }
  if (!shown.length) {
    list.innerHTML = `<div class="muted">No alerts match the current filters.</div>`;
    return;
  }
  list.innerHTML = shown.map(renderAlertEvent).join("");
}

// Debounce search to keep large feeds responsive while typing.
let _alertsSearchTimer = null;
function onAlertsFilterChange(immediate = false) {
  if (immediate) {
    if (_alertsSearchTimer) clearTimeout(_alertsSearchTimer);
    renderFilteredAlerts();
    return;
  }
  if (_alertsSearchTimer) clearTimeout(_alertsSearchTimer);
  _alertsSearchTimer = setTimeout(renderFilteredAlerts, 120);
}

$("#alerts-search").addEventListener("input", () => onAlertsFilterChange());
$("#alerts-kind").addEventListener("change", () => onAlertsFilterChange(true));
$("#alerts-rule").addEventListener("change", () => onAlertsFilterChange(true));
$("#alerts-sort").addEventListener("change", () => onAlertsFilterChange(true));
$("#alerts-filter-reset").addEventListener("click", () => {
  $("#alerts-search").value = "";
  $("#alerts-kind").value = "";
  $("#alerts-rule").value = "";
  $("#alerts-sort").value = "newest";
  onAlertsFilterChange(true);
});

function renderAlertEvent(e) {
  const det = e.details || {};
  const label = det.ssid || det.name || "";
  const vendor = det.vendor ? ` · ${escapeHtml(det.vendor)}` : "";
  const where = e.location_id != null ? `loc #${e.location_id}` : "no loc";
  return `
    <div class="alert-item kind-${escapeHtml(e.device_kind)}">
      <div>
        <span class="alert-rule">${escapeHtml(e.rule_name || "rule " + e.rule_id)}</span>
        <span class="muted"> matched </span>
        <span class="alert-device">${escapeHtml(e.device_id)}</span>
        ${label ? `<span class="muted"> · </span><span>${escapeHtml(label)}</span>` : ""}
        ${vendor}
      </div>
      <div class="alert-meta">
        <span class="alert-rssi">${e.rssi != null ? e.rssi + " dBm" : ""}</span>
        · ${escapeHtml(where)}
        · ${formatTime(e.triggered_at)}
      </div>
    </div>
  `;
}

function setBadge(n) {
  const b = $("#alerts-badge");
  if (!b) return;
  if (n > 0) { b.textContent = n; b.hidden = false; }
  else { b.hidden = true; }
}

// Tracks the id of the most recent alert we've already popped on the map.
// Distinct from alertsLastSeenId (which represents "last viewed in feed")
// so popups fire even when the user isn't on the alerts tab.
let alertsLastPoppedId = 0;
// `${rule_id}|${device_id}` pairs we've already shown on the map this session.
// A "repeat" of an existing pair (same rule firing again on the same device
// after the cooldown) is suppressed so the operator only sees genuinely new
// matches, not the same ping recurring every minute.
const _alertPoppedPairs = new Set();
function _alertPairKey(e) { return `${e.rule_id}|${e.device_id}`; }

const ALERT_TOAST_TTL_MS = 8000;
const ALERT_TOAST_MAX = 5;

// ── audible alarm ────────────────────────────────────────────────
let _audioCtx = null;
function getAudioCtx() {
  if (_audioCtx) return _audioCtx;
  try {
    _audioCtx = new (window.AudioContext || window.webkitAudioContext)();
  } catch { return null; }
  return _audioCtx;
}
// Browsers block AudioContext.start until a user gesture. Call this from
// any click handler to "prime" the context so subsequent alarms can play
// even when the user isn't actively interacting.
function primeAudio() {
  const ctx = getAudioCtx();
  if (ctx && ctx.state === "suspended") ctx.resume().catch(() => {});
}
document.addEventListener("click", primeAudio, { once: true, capture: true });

function playAlarm() {
  const ctx = getAudioCtx();
  if (!ctx) return;
  if (ctx.state === "suspended") {
    // Best-effort resume; if it fails the alarm is silently skipped this turn.
    ctx.resume().catch(() => {});
    if (ctx.state === "suspended") return;
  }
  const now = ctx.currentTime;
  // Three-tone alarm: high-low-high, ~600ms total
  const tones = [
    { f: 880, t: 0.00, dur: 0.18 },
    { f: 660, t: 0.20, dur: 0.18 },
    { f: 880, t: 0.40, dur: 0.22 },
  ];
  for (const tone of tones) {
    const osc = ctx.createOscillator();
    const gain = ctx.createGain();
    osc.type = "square";
    osc.frequency.value = tone.f;
    gain.gain.setValueAtTime(0.0001, now + tone.t);
    gain.gain.exponentialRampToValueAtTime(0.18, now + tone.t + 0.005);
    gain.gain.exponentialRampToValueAtTime(0.0001, now + tone.t + tone.dur);
    osc.connect(gain).connect(ctx.destination);
    osc.start(now + tone.t);
    osc.stop(now + tone.t + tone.dur + 0.02);
  }
}

function showAlertOnMap(e) {
  // Render as a floating toast in the map's corner — no longer pinned to
  // any location bubble, so the popup shows even if the marker isn't on
  // screen and doesn't drag the user's eye away from what they were looking at.
  const stack = $("#alert-toasts");
  if (!stack) return;
  const det = e.details || {};
  const label = det.ssid || det.name || "";
  const vendor = det.vendor || "";

  const toast = document.createElement("div");
  toast.className = `alert-toast kind-${escapeAttr(e.device_kind)}`;
  toast.innerHTML = `
    <button class="alert-toast-close" aria-label="dismiss">×</button>
    <div class="alert-popup">
      <div class="alert-popup-rule">⚡ ${escapeHtml(e.rule_name || "rule " + e.rule_id)}</div>
      <div><span class="mono">${escapeHtml(e.device_id)}</span></div>
      ${label ? `<div>${escapeHtml(label)}</div>` : ""}
      ${vendor ? `<div class="muted">${escapeHtml(vendor)}</div>` : ""}
      <div class="alert-popup-meta">
        ${e.rssi != null ? `${e.rssi} dBm · ` : ""}${e.location_id != null ? `loc #${e.location_id} · ` : ""}${escapeHtml(formatTime(e.triggered_at))}
      </div>
    </div>
  `;

  const dismiss = () => {
    if (toast._dismissed) return;
    toast._dismissed = true;
    clearTimeout(toast._timer);
    toast.classList.add("dismissing");
    setTimeout(() => toast.remove(), 200);
  };
  toast.querySelector(".alert-toast-close").addEventListener("click", dismiss);
  toast._timer = setTimeout(dismiss, ALERT_TOAST_TTL_MS);

  stack.appendChild(toast);

  // Cap the stack so a burst of alerts can't push the screen full.
  while (stack.children.length > ALERT_TOAST_MAX) {
    const oldest = stack.firstElementChild;
    if (oldest) {
      clearTimeout(oldest._timer);
      oldest.remove();
    }
  }

  // Audible alarm if the rule asked for one. The flag is joined onto the
  // event by the events query (rule_audible) so we don't depend on the
  // rules cache being loaded.
  if (e.rule_audible) playAlarm();
}

async function pollAlertsBadge() {
  try {
    const { events } = await api(`/api/alerts/events?limit=10`);
    if (!events.length) { setBadge(0); return; }
    const newest = events[0].id;

    // Popup any events newer than the last one we popped, skipping pairs
    // (rule_id, device_id) we've already shown so a repeating alert
    // doesn't pop again every minute.
    //
    // On the very first poll (alertsLastPoppedId === 0) we don't pop
    // anything — and we also seed `_alertPoppedPairs` with the recent
    // events' pairs so subsequent firings of those same pairs are
    // treated as repeats too, not "new since I opened the page".
    if (alertsLastPoppedId === 0) {
      for (const e of events) _alertPoppedPairs.add(_alertPairKey(e));
    } else {
      const fresh = events.filter(e => e.id > alertsLastPoppedId);
      // Show oldest-first so the newest is the one left visible.
      for (const e of fresh.slice().reverse()) {
        const key = _alertPairKey(e);
        if (_alertPoppedPairs.has(key)) continue;
        _alertPoppedPairs.add(key);
        showAlertOnMap(e);
      }
    }
    alertsLastPoppedId = newest;

    const unseen = events.filter(e => e.id > alertsLastSeenId).length;
    setBadge(unseen);
    if ($("#tab-alerts").classList.contains("active")) {
      // Tab is open — auto-refresh feed and clear badge
      await refreshAlertEvents();
      setBadge(0);
    }
  } catch {}
}

async function refreshAlerts() {
  await refreshAlertRules();
  await refreshAlertEvents();
  setBadge(0);
}

// ── compound rule conditions ──────────────────────────
// Only the simple value-based types are valid as additional conditions —
// stateful types (new_device, cross_location) only make sense as the
// primary match.
const COMPOUND_TYPES = [
  ["device_id",       "device id (exact / prefix)"],
  ["name_contains",   "name / SSID contains"],
  ["vendor_contains", "vendor contains"],
  ["rssi_above",      "RSSI ≥ dBm"],
];

function buildExtraConditionRow(initial = { match_type: "device_id", match_value: "" }) {
  const row = document.createElement("div");
  row.className = "rule-extra-row";
  const opts = COMPOUND_TYPES
    .map(([v, label]) => `<option value="${v}"${v === initial.match_type ? " selected" : ""}>${escapeHtml(label)}</option>`)
    .join("");
  row.innerHTML = `
    <select class="rule-extra-type">${opts}</select>
    <input type="text" class="rule-extra-value mono"
           value="${escapeAttr(initial.match_value || "")}"
           placeholder="${escapeAttr(MATCH_TYPE_PLACEHOLDERS[initial.match_type] || "")}" />
    <button type="button" class="icon-btn danger rule-extra-remove" title="Remove this condition" aria-label="Remove condition">×</button>
  `;
  // Update placeholder when the type changes.
  row.querySelector(".rule-extra-type").addEventListener("change", (e) => {
    row.querySelector(".rule-extra-value").placeholder =
      MATCH_TYPE_PLACEHOLDERS[e.target.value] || "";
  });
  row.querySelector(".rule-extra-remove").addEventListener("click", () => row.remove());
  return row;
}

function readExtraConditions() {
  const out = [];
  for (const row of $$("#rule-extra-list .rule-extra-row")) {
    const mt = row.querySelector(".rule-extra-type").value;
    const mv = (row.querySelector(".rule-extra-value").value || "").trim();
    if (!mv) continue;  // silently drop blank rows
    out.push({ match_type: mt, match_value: mv });
  }
  return out;
}

function setExtraConditions(list) {
  const container = $("#rule-extra-list");
  if (!container) return;
  container.innerHTML = "";
  for (const c of (list || [])) container.appendChild(buildExtraConditionRow(c));
}

$("#rule-add-extra")?.addEventListener("click", () => {
  $("#rule-extra-list").appendChild(buildExtraConditionRow());
});

function enterEditRuleMode(rule) {
  if (!rule) return;
  const form = $("#rule-form");
  form.dataset.editingId = String(rule.id);
  form.elements["name"].value = rule.name || "";
  form.elements["kind"].value = rule.kind || "";
  form.elements["match_type"].value = rule.match_type || "device_id";
  form.elements["match_value"].value = rule.match_value || "";
  form.elements["location_id"].value = rule.location_id != null ? String(rule.location_id) : "";
  form.elements["notify_discord"].checked = !!rule.notify_discord;
  form.elements["audible"].checked = !!rule.audible;
  setExtraConditions(rule.extra_conditions || []);
  // Update placeholder for the new match_type without clobbering the value.
  $("#rule-match-value").placeholder = MATCH_TYPE_PLACEHOLDERS[rule.match_type] || "";
  $("#rule-form-title").textContent = `Edit rule #${rule.id}`;
  $("#rule-form-submit").textContent = "Update rule";
  $("#rule-form-cancel").hidden = false;
  $("#rule-form-status").textContent = "";
  form.scrollIntoView({ behavior: "smooth", block: "nearest" });
  form.elements["name"].focus();
}

function exitEditRuleMode() {
  const form = $("#rule-form");
  form.dataset.editingId = "";
  form.reset();
  applyMatchTypeUI($("#rule-match-type").value);
  setExtraConditions([]);
  $("#rule-form-title").textContent = "New rule";
  $("#rule-form-submit").textContent = "Add rule";
  $("#rule-form-cancel").hidden = true;
}

$("#rule-form-cancel").addEventListener("click", exitEditRuleMode);

$("#rule-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const form = e.target;
  const editingId = form.dataset.editingId || "";
  const fd = new FormData(form);
  const payload = {
    name: fd.get("name"),
    kind: fd.get("kind") || null,
    match_type: fd.get("match_type"),
    match_value: fd.get("match_value"),
    location_id: fd.get("location_id") || null,
    notify_discord: fd.get("notify_discord") === "on",
    audible: fd.get("audible") === "on",
    extra_conditions: readExtraConditions(),
  };
  $("#rule-form-status").textContent = "saving…";
  try {
    if (editingId) {
      await api(`/api/alerts/rules/${editingId}`, { method: "PATCH", body: JSON.stringify(payload) });
      $("#rule-form-status").textContent = "updated";
    } else {
      await api("/api/alerts/rules", { method: "POST", body: JSON.stringify(payload) });
      $("#rule-form-status").textContent = "added";
    }
    exitEditRuleMode();
    setTimeout(() => $("#rule-form-status").textContent = "", 1200);
    await refreshAlertRules();
  } catch (err) {
    $("#rule-form-status").textContent = "error: " + err.message;
  }
});

$("#alerts-clear").addEventListener("click", async () => {
  if (!confirm("Clear the entire alert feed? Rules will stay.")) return;
  await api("/api/alerts/events", { method: "DELETE" });
  await refreshAlertEvents();
  setBadge(0);
});

const MATCH_TYPE_PLACEHOLDERS = {
  device_id: "aa:bb:cc:dd:ee:ff or aa:bb:cc",
  name_contains: "Apple, Pixel, MyNet…",
  vendor_contains: "Samsung, Cisco…",
  rssi_above: "-60",
  new_device: "300 (seconds the location must be established first; 0 = arm immediately)",
  cross_location: "5/2 — appears in at least 2 of the last 5 locations",
};
const MATCH_TYPE_DEFAULTS = {
  rssi_above: "-60",
  new_device: "300",
  cross_location: "5/2",
};

function applyMatchTypeUI(matchType) {
  const v = $("#rule-match-value");
  v.placeholder = MATCH_TYPE_PLACEHOLDERS[matchType] || "";
  // Always swap the value to the new type's default (or clear it for free-text
  // types) so a stale value from the previous type can't accidentally submit.
  v.value = MATCH_TYPE_DEFAULTS[matchType] || "";
}
$("#rule-match-type").addEventListener("change", (e) => applyMatchTypeUI(e.target.value));
applyMatchTypeUI($("#rule-match-type").value);

// ---------- OUI ----------
function formatBytes(n) {
  if (n == null) return "—";
  const u = ["B", "KiB", "MiB", "GiB"];
  let i = 0; let v = n;
  while (v >= 1024 && i < u.length - 1) { v /= 1024; i++; }
  return `${v.toFixed(v < 10 && i > 0 ? 1 : 0)} ${u[i]}`;
}

async function refreshOuiStatus() {
  try {
    const s = await api("/api/oui/status");
    const pill = $("#oui-status");
    if (s.updating) {
      pill.textContent = "OUI: updating…"; pill.className = "pill warn";
    } else if (s.count > 0) {
      const when = s.last_updated ? ` · updated ${formatTime(s.last_updated)}` : "";
      pill.textContent = `OUI: ${s.count.toLocaleString()} entries${when}`;
      pill.className = "pill ok";
    } else {
      pill.textContent = "OUI: empty (click Update)";
      pill.className = "pill warn";
    }
    const per = s.per_registry || {};
    $("#oui-total").textContent = (s.count || 0).toLocaleString();
    $("#oui-mal").textContent = (per["MA-L"] || 0).toLocaleString();
    $("#oui-mam").textContent = (per["MA-M"] || 0).toLocaleString();
    $("#oui-mas").textContent = (per["MA-S"] || 0).toLocaleString();
    $("#oui-updated").textContent = s.last_updated ? formatTime(s.last_updated) : "never";
    $("#oui-file").textContent = `${s.db_file_path || ""} (${formatBytes(s.db_file_bytes)})`;
  } catch (e) {
    $("#oui-status").textContent = "OUI: error";
    $("#oui-status").className = "pill err";
  }
}

$("#oui-update").addEventListener("click", async () => {
  const btn = $("#oui-update");
  btn.disabled = true; btn.textContent = "Downloading…";
  $("#oui-status").textContent = "OUI: updating…";
  $("#oui-status").className = "pill warn";
  try {
    const res = await api("/api/oui/update", { method: "POST" });
    const per = res.per_registry || {};
    const detail = Object.entries(per).map(([k, v]) => `${k}:${v}`).join(" ");
    alert(`OUI updated: ${res.inserted} entries (${detail})`);
  } catch (e) {
    alert("OUI update failed: " + e.message);
  } finally {
    btn.disabled = false; btn.textContent = "Update from IEEE";
    refreshOuiStatus();
  }
});

$("#oui-test").addEventListener("click", async () => {
  const mac = $("#oui-test-mac").value.trim();
  if (!mac) return;
  try {
    const res = await api(`/api/oui/lookup?mac=${encodeURIComponent(mac)}`);
    $("#oui-test-result").textContent = res.vendor ? `→ ${res.vendor}` : "→ unknown";
  } catch (e) {
    $("#oui-test-result").textContent = "error: " + e.message;
  }
});

// ---------- whitelist ----------
let _whitelistCache = [];

function buildWhitelistMatcher(entries) {
  // Mirror of services/alert_service.is_whitelisted — used to render the
  // Devices tab toggle without needing a server round-trip per row.
  const norm = entries.map(e => [e.kind, (e.device_id || "").toLowerCase()]);
  return (kind, deviceId) => {
    const d = (deviceId || "").toLowerCase();
    return norm.some(([k, target]) =>
      k === kind && target && (d === target || d.startsWith(target))
    );
  };
}
let isWhitelisted = (_k, _d) => false;

async function refreshWhitelist() {
  const tbody = $("#wl-table tbody");
  if (!tbody) return;
  try {
    const r = await api("/api/whitelist");
    _whitelistCache = r.entries || [];
    isWhitelisted = buildWhitelistMatcher(_whitelistCache);
    tbody.innerHTML = "";
    for (const e of _whitelistCache) tbody.appendChild(_makeWhitelistRow(e));
  } catch (e) {
    tbody.innerHTML = `<tr><td colspan="5" class="muted">error: ${escapeHtml(e.message)}</td></tr>`;
  }
}

function _makeWhitelistRow(e) {
  const tr = document.createElement("tr");
  tr.dataset.id = e.id;
  tr.dataset.kind = e.kind;
  tr.dataset.deviceId = e.device_id || "";
  tr.dataset.note = e.note || "";
  tr.dataset.createdAt = e.created_at || "";
  tr.title = "Double-click to edit";
  tr.innerHTML = `
    <td>${escapeHtml(e.kind)}</td>
    <td class="mono">${escapeHtml(e.device_id)}</td>
    <td>${escapeHtml(e.note || "")}</td>
    <td class="mono">${formatTime(e.created_at)}</td>
    <td><button type="button" class="icon-btn danger wl-delete" data-id="${e.id}" title="Remove from whitelist" aria-label="Remove">×</button></td>
  `;
  tr.querySelector(".wl-delete").addEventListener("click", async (ev) => {
    ev.stopPropagation();
    if (!confirm(`Remove ${e.kind} ${e.device_id} from the whitelist?`)) return;
    await api(`/api/whitelist/${e.id}`, { method: "DELETE" });
    await refreshWhitelist();
    await refreshDevices();
  });
  tr.addEventListener("dblclick", (ev) => {
    if (tr.classList.contains("editing")) return;
    if (ev.target.closest(".icon-btn")) return;  // don't trigger from buttons
    _enterWhitelistEdit(tr);
  });
  return tr;
}

function _enterWhitelistEdit(tr) {
  tr.classList.add("editing");
  const { id, kind, deviceId, note, createdAt } = tr.dataset;
  const opt = (k) => `<option value="${k}"${k === kind ? " selected" : ""}>${k}</option>`;
  tr.innerHTML = `
    <td>
      <select class="wl-edit-kind">
        ${opt("wifi")}${opt("bluetooth")}${opt("wifi_client")}
      </select>
    </td>
    <td><input type="text" class="wl-edit-device-id mono" value="${escapeAttr(deviceId)}" /></td>
    <td><input type="text" class="wl-edit-note" value="${escapeAttr(note)}" placeholder="note" /></td>
    <td class="mono">${escapeHtml(formatTime(createdAt))}</td>
    <td class="row-actions">
      <button type="button" class="icon-btn wl-save" title="Save (Enter)" aria-label="Save">✓</button>
      <button type="button" class="icon-btn wl-cancel" title="Cancel (Esc)" aria-label="Cancel">✗</button>
    </td>
  `;
  const idInput = tr.querySelector(".wl-edit-device-id");
  idInput.focus();
  idInput.select();
  tr.querySelectorAll("select, input").forEach(el => {
    el.addEventListener("keydown", (ev) => {
      if (ev.key === "Enter") { ev.preventDefault(); _saveWhitelistEdit(tr); }
      else if (ev.key === "Escape") { ev.preventDefault(); refreshWhitelist(); }
    });
  });
  tr.querySelector(".wl-save").addEventListener("click", () => _saveWhitelistEdit(tr));
  tr.querySelector(".wl-cancel").addEventListener("click", () => refreshWhitelist());
}

async function _saveWhitelistEdit(tr) {
  const id = tr.dataset.id;
  const payload = {
    kind: tr.querySelector(".wl-edit-kind").value,
    device_id: tr.querySelector(".wl-edit-device-id").value.trim(),
    note: tr.querySelector(".wl-edit-note").value.trim() || null,
  };
  if (!payload.device_id) {
    alert("device id is required");
    tr.querySelector(".wl-edit-device-id")?.focus();
    return;
  }
  try {
    await api(`/api/whitelist/${id}`, {
      method: "PATCH",
      body: JSON.stringify(payload),
    });
  } catch (e) {
    alert("Save failed: " + e.message);
    return;
  }
  await refreshWhitelist();
  // The matcher may have changed — re-render the Devices tab so the
  // ★/☆ stars and the row-dimming match the new whitelist.
  await refreshDevices();
}

$("#wl-form")?.addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const form = ev.target;
  const fd = new FormData(form);
  const payload = {
    kind: fd.get("kind"),
    device_id: (fd.get("device_id") || "").toString().trim(),
    note: (fd.get("note") || "").toString().trim() || null,
  };
  $("#wl-form-status").textContent = "saving…";
  try {
    await api("/api/whitelist", { method: "POST", body: JSON.stringify(payload) });
    form.reset();
    $("#wl-form-status").textContent = "added";
    setTimeout(() => $("#wl-form-status").textContent = "", 1500);
    await refreshWhitelist();
  } catch (err) {
    $("#wl-form-status").textContent = "error: " + err.message;
  }
});

async function quickWhitelistToggle(kind, deviceId) {
  // Find an existing entry that exactly matches; if found, delete it.
  // Otherwise add a new entry.
  const exact = _whitelistCache.find(
    e => e.kind === kind && (e.device_id || "").toLowerCase() === deviceId.toLowerCase()
  );
  if (exact) {
    if (!confirm(`Remove ${kind} ${deviceId} from the whitelist?`)) return;
    await api(`/api/whitelist/${exact.id}`, { method: "DELETE" });
  } else {
    await api("/api/whitelist", { method: "POST", body: JSON.stringify({
      kind, device_id: deviceId, note: null,
    }) });
  }
  await refreshWhitelist();
  await refreshDevices();
}

// ---------- updates ----------
let lastUpdateStatus = null;

function renderUpdateStatus(s) {
  lastUpdateStatus = s;
  if (!s.ok) {
    $("#upd-state").textContent = s.error || "unavailable";
    $("#upd-apply").disabled = true;
    return;
  }
  const cur = s.current;
  $("#upd-current").innerHTML = cur
    ? `<span class="mono">${escapeHtml(cur.short)}</span> · ${escapeHtml(cur.subject)} <span class="muted">(${formatTime(cur.date)})</span>`
    : "—";
  $("#upd-branch").textContent = s.branch || "—";
  $("#upd-remote").innerHTML = s.remote_url
    ? `<span class="mono">${escapeHtml(s.remote_url)}</span>`
    : "—";

  let stateText = "up to date";
  let stateCls = "";
  if (s.fetch_error) { stateText = "fetch failed: " + s.fetch_error; stateCls = "warn"; }
  else if (s.behind > 0 && s.ahead > 0) { stateText = `diverged (behind ${s.behind}, ahead ${s.ahead}) — fast-forward not possible`; stateCls = "warn"; }
  else if (s.behind > 0) { stateText = `${s.behind} commit${s.behind === 1 ? "" : "s"} behind origin`; stateCls = "warn"; }
  else if (s.ahead > 0) { stateText = `${s.ahead} commit${s.ahead === 1 ? "" : "s"} ahead of origin`; }
  $("#upd-state").innerHTML = stateCls
    ? `<span class="pill ${stateCls}">${escapeHtml(stateText)}</span>`
    : escapeHtml(stateText);

  // Target preview
  const showTarget = s.behind > 0 && s.target;
  $("#upd-target").hidden = !showTarget;
  if (showTarget) {
    $("#upd-target-info").textContent =
      `${s.target.short}  ${s.target.subject}\n` +
      `${s.target.author} · ${formatTime(s.target.date)}`;
  }
  // Dedicated requirements warning, only when there's actually an update pending
  $("#upd-reqs").hidden = !(showTarget && s.requirements_changed);

  // Dirty warning
  $("#upd-dirty").hidden = !s.dirty;
  if (s.dirty) {
    $("#upd-dirty-files").textContent = (s.dirty_files || []).join("\n") || "(unknown)";
  }

  // Apply button is enabled only when we can actually do a fast-forward
  $("#upd-apply").disabled = !(s.behind > 0 && s.ahead === 0 && !s.dirty && s.upstream);
}

async function loadUpdateStatus() {
  try {
    const s = await api("/api/system/update/status");
    renderUpdateStatus(s);
  } catch (e) {
    $("#upd-state").textContent = "error: " + e.message;
  }
}

$("#upd-check").addEventListener("click", async () => {
  const btn = $("#upd-check");
  btn.disabled = true; btn.textContent = "Checking…";
  $("#upd-status").textContent = "fetching origin…";
  try {
    const s = await api("/api/system/update/check", { method: "POST" });
    renderUpdateStatus(s);
    $("#upd-status").textContent = s.fetch_error ? "" : "checked";
    setTimeout(() => ($("#upd-status").textContent = ""), 1500);
  } catch (e) {
    $("#upd-status").textContent = "error: " + e.message;
  } finally {
    btn.disabled = false; btn.textContent = "Check for updates";
  }
});

$("#upd-apply").addEventListener("click", async () => {
  const btn = $("#upd-apply");
  const restoreBtn = () => { btn.disabled = false; btn.textContent = "Download & restart"; };

  // Re-fetch status so the requirements flag reflects what we're actually
  // about to pull, not whatever was visible when the panel last loaded.
  btn.disabled = true; btn.textContent = "Checking…";
  $("#upd-status").textContent = "checking…";
  let s;
  try {
    s = await api("/api/system/update/check", { method: "POST" });
    renderUpdateStatus(s);
  } catch (e) {
    $("#upd-status").textContent = "error: " + e.message;
    restoreBtn();
    return;
  }
  if (!s.ok || s.behind === 0) {
    $("#upd-status").textContent = s.behind === 0 ? "already up to date" : "";
    restoreBtn();
    return;
  }

  let msg;
  if (s.requirements_changed) {
    msg =
      "Dependency change detected.\n\n" +
      "requirements.txt is changing in this update. After the restart you " +
      "must run:\n\n" +
      "    pip install -r requirements.txt\n\n" +
      "If you skip this, the app may fail to start because of missing or " +
      "outdated dependencies.\n\n" +
      "Continue with the pull and restart anyway?";
  } else {
    msg =
      "Pull the latest commits from origin and restart the app?\n\n" +
      "In-flight requests will fail briefly during the restart.";
  }
  if (!confirm(msg)) {
    $("#upd-status").textContent = "cancelled";
    setTimeout(() => ($("#upd-status").textContent = ""), 1500);
    restoreBtn();
    return;
  }

  btn.textContent = "Updating…";
  $("#upd-status").textContent = "pulling…";
  try {
    const r = await api("/api/system/update/apply", {
      method: "POST",
      body: JSON.stringify({
        restart: true,
        acknowledge_requirements_change: !!s.requirements_changed,
      }),
    });
    if (r.updated) {
      $("#upd-status").textContent = r.restarting
        ? (s.requirements_changed
            ? "updated — restarting (run pip install after)…"
            : "updated — restarting…")
        : "updated";
      if (r.restarting) waitForRestart();
    } else {
      $("#upd-status").textContent = "already up to date";
      await loadUpdateStatus();
      restoreBtn();
    }
  } catch (e) {
    $("#upd-status").textContent = "error: " + e.message;
    restoreBtn();
  }
});

$("#upd-restart").addEventListener("click", async () => {
  if (!confirm("Restart the app now? In-flight requests will fail briefly.")) return;
  $("#upd-status").textContent = "restarting…";
  try {
    await api("/api/system/restart", { method: "POST" });
    waitForRestart();
  } catch (e) {
    $("#upd-status").textContent = "error: " + e.message;
  }
});

async function waitForRestart() {
  // Poll until the new process answers, then refresh.
  const deadline = Date.now() + 30000;
  await new Promise(r => setTimeout(r, 1500));
  while (Date.now() < deadline) {
    try {
      await fetch("/api/system/update/status", { cache: "no-store" });
      $("#upd-status").textContent = "back online — reloading…";
      setTimeout(() => location.reload(), 600);
      return;
    } catch {
      await new Promise(r => setTimeout(r, 1000));
    }
  }
  $("#upd-status").textContent = "timed out waiting for restart — reload manually";
}

// ---------- pause toggle (map tab floating control) ----------
let _pausedState = false;

function renderPauseButton(paused) {
  _pausedState = paused;
  const btn = $("#map-pause");
  const icon = $("#map-pause-icon");
  if (!btn || !icon) return;
  if (paused) {
    btn.classList.add("paused");
    btn.title = "PAUSED — scanning, alerts, and new locations are suspended. Click to resume.";
    btn.setAttribute("aria-label", "Resume scanning");
    icon.innerHTML = ICON_PLAY;
  } else {
    btn.classList.remove("paused");
    btn.title = "Pause scanning, alerts, and new-location creation";
    btn.setAttribute("aria-label", "Pause scanning");
    icon.innerHTML = ICON_PAUSE;
  }
}

async function refreshPauseStatus() {
  try {
    const r = await api("/api/system/pause");
    renderPauseButton(!!r.paused);
  } catch { /* leave whatever was last shown */ }
}

$("#map-pause").addEventListener("click", async () => {
  // Optimistic: flip the icon immediately so it feels responsive, then
  // confirm against the server's response.
  const next = !_pausedState;
  renderPauseButton(next);
  try {
    const r = await api("/api/system/pause", {
      method: "POST", body: JSON.stringify({ paused: next }),
    });
    renderPauseButton(!!r.paused);
  } catch (e) {
    // Roll back the optimistic flip on failure.
    renderPauseButton(!next);
    alert("Pause toggle failed: " + e.message);
  }
});

// ---------- map view toggles ----------
// Per-browser preferences (no server state). All three default to off.
const mapToggles = {
  trackSensor: localStorage.getItem("mapTrackSensor") === "1",
  smartTrack:  localStorage.getItem("mapSmartTrack")  === "1",
  autoZoom:    localStorage.getItem("mapAutoZoom")    === "1",
};

function persistMapToggles() {
  localStorage.setItem("mapTrackSensor", mapToggles.trackSensor ? "1" : "0");
  localStorage.setItem("mapSmartTrack",  mapToggles.smartTrack  ? "1" : "0");
  localStorage.setItem("mapAutoZoom",    mapToggles.autoZoom    ? "1" : "0");
}

function renderMapToggles() {
  const set = (id, on) => $(id)?.classList.toggle("active", on);
  set("#map-track-sensor", mapToggles.trackSensor);
  set("#map-smart-track",  mapToggles.smartTrack);
  set("#map-autozoom",     mapToggles.autoZoom);
}

function setupMapToggleIcons() {
  const inject = (id, html) => { const el = $(id); if (el) el.innerHTML = html; };
  inject("#map-track-sensor-icon", ICON_CROSSHAIR);
  inject("#map-smart-track-icon",  ICON_NAV);
  inject("#map-autozoom-icon",     ICON_FIT);
  inject("#map-draw-icon",         ICON_DRAW);
  renderMapToggles();
}

function bindMapToggle(btnId, key) {
  $(btnId)?.addEventListener("click", () => {
    mapToggles[key] = !mapToggles[key];
    persistMapToggles();
    renderMapToggles();
    // Apply immediately so the click feels responsive — don't wait for the
    // next GPS poll.
    if (sensorMarker) applyMapView(sensorMarker.getLatLng());
  });
}
bindMapToggle("#map-track-sensor", "trackSensor");
bindMapToggle("#map-smart-track",  "smartTrack");
bindMapToggle("#map-autozoom",     "autoZoom");

// ── draw geofence ───────────────────────────────────
// Click the button → enter draw mode (cursor crosshair, dragging disabled).
// Click+drag on the map: center placed on mousedown, radius grows with the
// haversine to the current pointer position. Mouseup finalizes and prompts
// for a label. ESC cancels. The temp circle is removed on exit; the real
// one re-renders via refreshLocationMarkers after the POST succeeds.
let drawState = null;  // { centerLatLng, tempCircle } when active

function exitDrawMode({ commit = false } = {}) {
  if (!drawState) return null;
  const { tempCircle, centerLatLng, mouseHandlers } = drawState;
  if (mouseHandlers) {
    map.off("mousedown", mouseHandlers.down);
    map.off("mousemove", mouseHandlers.move);
    map.off("mouseup",   mouseHandlers.up);
  }
  if (tempCircle) {
    if (commit) {
      // Caller pulls radius before we remove the circle.
    } else {
      map.removeLayer(tempCircle);
    }
  }
  map.dragging.enable();
  map.getContainer().classList.remove("drawing");
  $("#map-draw")?.classList.remove("active");
  drawState = null;
  return { tempCircle, centerLatLng };
}

async function finalizeDraw(centerLatLng, tempCircle) {
  const radius = tempCircle.getRadius();
  if (radius < 1) {
    map.removeLayer(tempCircle);
    return;
  }
  const defaultLabel = `Geofence @ ${centerLatLng.lat.toFixed(4)},${centerLatLng.lng.toFixed(4)}`;
  const label = prompt(
    `Geofence radius: ${Math.round(radius)} m\n\nLabel for this drawn location:`,
    defaultLabel,
  );
  // Always remove the temp; the real one comes back via refreshLocationMarkers
  // (or stays gone if the user cancelled).
  map.removeLayer(tempCircle);
  if (label === null) return;  // cancelled
  try {
    await api("/api/locations/draw", {
      method: "POST",
      body: JSON.stringify({
        lat: centerLatLng.lat,
        lon: centerLatLng.lng,
        radius_m: Math.round(radius * 10) / 10,
        label: label.trim() || null,
      }),
    });
    await refreshLocationMarkers();
    await loadLocationOptions();
  } catch (e) {
    alert("Could not save geofence: " + e.message);
  }
}

function startDrawMode() {
  if (drawState) return;
  map.dragging.disable();
  map.getContainer().classList.add("drawing");
  $("#map-draw")?.classList.add("active");

  let center = null;
  let temp = null;

  const handlers = {
    down: (e) => {
      // Start a fresh drag. If the user clicks once and then again
      // somewhere else without moving the mouse, treat the second click as
      // a new center too.
      center = e.latlng;
      if (temp) map.removeLayer(temp);
      temp = L.circle(center, {
        radius: 0,
        color: "#5cd1ff",
        weight: 2,
        dashArray: "6,4",
        fillOpacity: 0.05,
      }).addTo(map);
      drawState.centerLatLng = center;
      drawState.tempCircle = temp;
    },
    move: (e) => {
      if (!center || !temp) return;
      const r = center.distanceTo(e.latlng);  // metres, leaflet's haversine
      temp.setRadius(r);
    },
    up: async (e) => {
      if (!center || !temp) return;
      // Use the final pointer position rather than the last mousemove —
      // accounts for the case where the user lifts the button without
      // moving (radius would be 0 → handled in finalizeDraw).
      const r = center.distanceTo(e.latlng);
      temp.setRadius(r);
      // Snapshot before exiting (which would otherwise drop refs).
      const c = drawState.centerLatLng;
      const t = drawState.tempCircle;
      exitDrawMode({ commit: true });
      await finalizeDraw(c, t);
    },
  };

  drawState = { centerLatLng: null, tempCircle: null, mouseHandlers: handlers };
  map.on("mousedown", handlers.down);
  map.on("mousemove", handlers.move);
  map.on("mouseup",   handlers.up);
}

$("#map-draw")?.addEventListener("click", () => {
  if (drawState) {
    // Cancelling — discard any in-progress draw.
    exitDrawMode({ commit: false });
  } else {
    startDrawMode();
  }
});

document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && drawState) exitDrawMode({ commit: false });
});

function applyMapView(latlng) {
  // Called every GPS poll (and on toggle clicks). All branches are no-ops
  // when nothing's enabled, so the user keeps full manual control.
  if (!latlng) return;
  const ll = latlng.lat !== undefined ? latlng : L.latLng(latlng[0], latlng[1]);

  // Comfort zone for "smart" mode: inner 50% of the current viewport.
  const inComfortZone = () => {
    const b = map.getBounds();
    return b.pad(-0.25).contains(ll);
  };

  if (mapToggles.autoZoom) {
    // Smart + auto-zoom: only re-fit when sensor's drifted out of view.
    if (mapToggles.smartTrack && inComfortZone()) return;
    const bounds = L.latLngBounds([ll]);
    for (const m of locationMarkers.values()) {
      try { bounds.extend(m.getLatLng()); } catch {}
    }
    if (bounds.isValid()) {
      map.fitBounds(bounds, { padding: [40, 40], maxZoom: 18, animate: true });
    }
    return;
  }

  if (mapToggles.smartTrack) {
    if (!inComfortZone()) map.panTo(ll, { animate: true });
    return;
  }

  if (mapToggles.trackSensor) {
    map.panTo(ll, { animate: true });
  }
}

// ---------- logs ----------
let logsLastSeenId = 0;
let logsRendered = [];      // mirror of what the DOM shows; capped at 1000
const LOGS_DOM_CAP = 1000;
let _logsTimer = null;

async function refreshLogs({ reset = false } = {}) {
  const list = $("#log-list");
  if (!list) return;
  if (reset) {
    logsLastSeenId = 0;
    logsRendered = [];
    list.innerHTML = "";
  }
  const level = $("#log-level").value || "INFO";
  const url = `/api/logs?since_id=${logsLastSeenId}&level=${encodeURIComponent(level)}&limit=500`;
  let res;
  try {
    res = await api(url);
  } catch (e) {
    if (!list.children.length) list.innerHTML = `<div class="muted">error: ${escapeHtml(e.message)}</div>`;
    return;
  }
  const entries = res.entries || [];
  if (!entries.length && !logsRendered.length) {
    list.innerHTML = `<div class="muted">No log entries yet.</div>`;
    updateLogCount(res.stats);
    return;
  }
  if (entries.length) {
    if (!logsRendered.length) list.innerHTML = "";
    const q = ($("#log-search").value || "").trim().toLowerCase();
    const frag = document.createDocumentFragment();
    for (const e of entries) {
      logsLastSeenId = Math.max(logsLastSeenId, e.id);
      logsRendered.push(e);
      if (q && !logEntryMatches(e, q)) continue;
      frag.appendChild(renderLogRow(e));
    }
    list.appendChild(frag);
    // Trim DOM + mirror so memory stays bounded over long sessions.
    while (list.children.length > LOGS_DOM_CAP) list.firstElementChild.remove();
    if (logsRendered.length > LOGS_DOM_CAP) {
      logsRendered = logsRendered.slice(-LOGS_DOM_CAP);
    }
    if ($("#log-autoscroll").checked) {
      list.scrollTop = list.scrollHeight;
    }
  }
  updateLogCount(res.stats);
}

function renderLogRow(e) {
  const tr = document.createElement("div");
  tr.className = `log-row lvl-${e.level || "INFO"}`;
  const ts = new Date((e.ts || 0) * 1000);
  const tsStr = ts.toLocaleTimeString(undefined, { hour12: false }) +
    "." + String(ts.getMilliseconds()).padStart(3, "0");
  tr.innerHTML = `
    <span class="ts">${escapeHtml(tsStr)}</span>
    <span class="level">${escapeHtml(e.level || "")}</span>
    <span class="logger" title="${escapeAttr(e.logger || "")}">${escapeHtml(e.logger || "")}</span>
    <span class="msg">${escapeHtml(e.message || "")}</span>
  `;
  return tr;
}

function logEntryMatches(e, q) {
  return ((e.logger || "").toLowerCase().includes(q)
       || (e.message || "").toLowerCase().includes(q)
       || (e.level || "").toLowerCase().includes(q));
}

function updateLogCount(stats) {
  if (!stats) return;
  $("#log-count").textContent = `${stats.count} / ${stats.capacity} buffered`;
}

function applyLogsFilter() {
  // Level/search change re-fetches from scratch since the filter is server-
  // side for level and we want the level filter to drop already-rendered rows.
  refreshLogs({ reset: true });
}

let _logSearchTimer = null;
$("#log-level").addEventListener("change", applyLogsFilter);
$("#log-search").addEventListener("input", () => {
  if (_logSearchTimer) clearTimeout(_logSearchTimer);
  _logSearchTimer = setTimeout(() => {
    // Search is client-side: just re-render the mirror with the new query.
    const list = $("#log-list");
    const q = ($("#log-search").value || "").trim().toLowerCase();
    list.innerHTML = "";
    const frag = document.createDocumentFragment();
    for (const e of logsRendered) {
      if (q && !logEntryMatches(e, q)) continue;
      frag.appendChild(renderLogRow(e));
    }
    list.appendChild(frag);
    if ($("#log-autoscroll").checked) list.scrollTop = list.scrollHeight;
  }, 120);
});
$("#log-clear").addEventListener("click", async () => {
  if (!confirm("Clear the in-memory log buffer? Records already on disk (journal/syslog) are not affected.")) return;
  await api("/api/logs", { method: "DELETE" });
  await refreshLogs({ reset: true });
});

function startLogsPolling() {
  if (_logsTimer) return;
  _logsTimer = setInterval(() => {
    if ($("#log-pause").checked) return;
    if (!$("#tab-logs").classList.contains("active")) return;
    refreshLogs();
  }, 2000);
}

// ---------- utils ----------
function escapeHtml(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}
function escapeAttr(s) { return escapeHtml(s).replace(/"/g, "&quot;"); }
function formatTime(iso) {
  if (!iso) return "";
  try { return new Date(iso).toLocaleString(); } catch { return iso; }
}

// ---------- boot ----------
(async function main() {
  await initMap();
  await loadInterfaces();
  await loadSettings();
  pollGps();
  refreshLocationMarkers();
  refreshOuiStatus();
  loadUpdateStatus();
  refreshTileCache();
  refreshProbeStatus();
  setInterval(refreshProbeStatus, 3000);
  setInterval(tickProbeRelativeTimes, 1000);
  refreshPauseStatus();
  setInterval(refreshPauseStatus, 15000);
  setupMapToggleIcons();
  refreshWhitelist();
  startLogsPolling();
  setInterval(pollGps, 1500);
  setInterval(refreshLocationMarkers, 5000);
  setInterval(refreshOuiStatus, 10000);
  setInterval(pollAlertsBadge, 4000);
})();
