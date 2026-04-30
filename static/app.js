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
      gpsPill.textContent = `GPS: ${fix.mode}D fix · ${fix.sats_used ?? "?"}/${fix.sats_visible ?? "?"} sats`;
      gpsPill.className = "pill ok";
    } else if (data.connected) {
      gpsPill.textContent = `GPS: searching (${fix.sats_visible ?? "?"} visible)`;
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
      if (!sensorMarker) {
        sensorMarker = L.circleMarker(ll, { radius: 8, color: "#5cd1ff", fillColor: "#5cd1ff", fillOpacity: 0.8 }).addTo(map);
        map.setView(ll, 17);
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
    }
  } catch (e) {
    $("#gps-status").textContent = "GPS: error";
    $("#gps-status").className = "pill err";
  }
}

function locationTooltipHtml(loc, isActive) {
  const label = escapeHtml(loc.label || `Location ${loc.id}`);
  const activeBadge = isActive ? `<span class="gj-tip-badge active">ACTIVE</span>` : "";
  return `
    <div class="gj-tip-card">
      <div class="gj-tip-header">
        <span class="gj-tip-id">#${loc.id}</span>
        <span class="gj-tip-label">${label}</span>
        ${activeBadge}
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
      const c = L.circle([loc.lat, loc.lon], {
        radius: loc.radius_m,
        color: isActive ? "#79e08c" : "#ffb86b",
        weight: 1.5,
        fillOpacity: isActive ? 0.12 : 0.06,
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
async function loadLocationOptions() {
  const { locations, active_id } = await api("/api/locations");
  const sel = $("#dev-location");
  sel.innerHTML = "";
  for (const loc of locations) {
    const o = document.createElement("option");
    o.value = loc.id;
    o.textContent = `${loc.label || `Loc ${loc.id}`}${loc.id === active_id ? " (active)" : ""}`;
    sel.appendChild(o);
  }
  if (active_id) sel.value = active_id;
}

async function refreshDevices() {
  await loadLocationOptions();
  const id = $("#dev-location").value;
  if (!id) return;
  const kind = $("#dev-kind").value;
  const q = kind ? `?kind=${kind}` : "";
  const { devices } = await api(`/api/locations/${id}/devices${q}`);
  const tbody = $("#dev-table tbody");
  tbody.innerHTML = "";
  for (const d of devices) {
    const det = d.details || {};
    const nameOrSsid = det.ssid ?? det.name ?? "";
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td>${d.kind}</td>
      <td class="mono">${d.device_id}</td>
      <td>${escapeHtml(nameOrSsid)}</td>
      <td>${escapeHtml(det.vendor || "")}</td>
      <td>${d.best_rssi}</td>
      <td>${d.last_rssi}</td>
      <td>${d.seen_count}</td>
      <td class="mono">${formatTime(d.first_seen)}</td>
      <td class="mono">${formatTime(d.last_seen)}</td>
      <td><details><summary>JSON</summary><pre>${escapeHtml(JSON.stringify(det, null, 2))}</pre></details></td>
    `;
    tbody.appendChild(tr);
  }
}

$("#dev-refresh").addEventListener("click", refreshDevices);
$("#dev-location").addEventListener("change", refreshDevices);
$("#dev-kind").addEventListener("change", refreshDevices);

// ---------- locations tab ----------
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
      <td><button class="secondary save-label" data-id="${loc.id}">Save</button></td>
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
}

$("#loc-refresh").addEventListener("click", refreshLocations);
$("#loc-new").addEventListener("click", async () => {
  try { await api("/api/locations/new", { method: "POST" }); refreshLocations(); }
  catch (e) { alert(e.message); }
});

$("#loc-report").addEventListener("click", async () => {
  const btn = $("#loc-report");
  const orig = btn.textContent;
  btn.disabled = true; btn.textContent = "Generating…";
  try {
    const res = await fetch("/api/locations/report.pdf", { method: "GET" });
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
  wsel.innerHTML = `<option value="">(none / auto)</option>`;
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
    tr.innerHTML = `
      <td><input type="checkbox" class="rule-toggle" data-id="${r.id}" ${r.enabled ? "checked" : ""}></td>
      <td>${escapeHtml(r.name)}</td>
      <td>${escapeHtml(r.kind || "any")}</td>
      <td>${escapeHtml(MATCH_TYPE_LABEL[r.match_type] || r.match_type)}</td>
      <td class="mono">${escapeHtml(r.match_value)}</td>
      <td>${r.location_id ?? "any"}</td>
      <td><input type="checkbox" class="rule-discord" data-id="${r.id}" ${r.notify_discord ? "checked" : ""}></td>
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
let _alertPopupTimer = null;

function showAlertOnMap(e) {
  if (e.location_id == null) return;
  const marker = locationMarkers.get(e.location_id);
  if (!marker) return; // marker not loaded yet — skip rather than guess
  const latlng = marker.getLatLng();
  const det = e.details || {};
  const label = det.ssid || det.name || "";
  const vendor = det.vendor || "";
  const html = `
    <div class="alert-popup kind-${escapeAttr(e.device_kind)}">
      <div class="alert-popup-rule">⚡ ${escapeHtml(e.rule_name || "rule " + e.rule_id)}</div>
      <div><span class="mono">${escapeHtml(e.device_id)}</span></div>
      ${label ? `<div>${escapeHtml(label)}</div>` : ""}
      ${vendor ? `<div class="muted">${escapeHtml(vendor)}</div>` : ""}
      <div class="alert-popup-meta">
        ${e.rssi != null ? `${e.rssi} dBm · ` : ""}${escapeHtml(formatTime(e.triggered_at))}
      </div>
    </div>
  `;
  const popup = L.popup({
    autoClose: false,
    closeOnClick: false,
    className: "gj-alert-popup",
    offset: [0, -4],
  }).setLatLng(latlng).setContent(html).openOn(map);

  // Auto-dismiss after a few seconds. A subsequent alert popup will already
  // have replaced this one (Leaflet's openOn closes prior popups).
  if (_alertPopupTimer) clearTimeout(_alertPopupTimer);
  _alertPopupTimer = setTimeout(() => {
    if (map.hasLayer(popup)) map.closePopup(popup);
  }, 8000);
}

async function pollAlertsBadge() {
  try {
    const { events } = await api(`/api/alerts/events?limit=10`);
    if (!events.length) { setBadge(0); return; }
    const newest = events[0].id;

    // Popup any events newer than the last one we popped. Skip on first
    // poll (alertsLastPoppedId === 0) so existing alerts from the DB
    // don't all pop up at once on page load.
    if (alertsLastPoppedId > 0) {
      const fresh = events.filter(e => e.id > alertsLastPoppedId);
      // Show oldest-first so the newest is the one left visible.
      for (const e of fresh.slice().reverse()) showAlertOnMap(e);
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
  setInterval(pollGps, 1500);
  setInterval(refreshLocationMarkers, 5000);
  setInterval(refreshOuiStatus, 10000);
  setInterval(pollAlertsBadge, 4000);
})();
