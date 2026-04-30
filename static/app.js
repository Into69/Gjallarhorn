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

    $("#fix-detail").textContent = JSON.stringify(fix, null, 2);

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

async function refreshLocationMarkers() {
  try {
    const { locations, active_id } = await api("/api/locations");
    for (const m of locationMarkers.values()) map.removeLayer(m);
    locationMarkers.clear();
    for (const loc of locations) {
      const c = L.circle([loc.lat, loc.lon], {
        radius: loc.radius_m,
        color: loc.id === active_id ? "#79e08c" : "#ffb86b",
        weight: 1, fillOpacity: 0.08,
      }).bindPopup(`<b>${loc.label || `Location ${loc.id}`}</b><br/>fixes: ${loc.fix_count}`);
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

// ---------- settings tab ----------
async function loadInterfaces() {
  const wifi = await api("/api/interfaces/wifi");
  const wsel = $("#set-wifi-iface");
  wsel.innerHTML = `<option value="">(none / auto)</option>`;
  for (const n of wifi.interfaces) {
    const o = document.createElement("option"); o.value = n; o.textContent = n;
    wsel.appendChild(o);
  }
  const bt = await api("/api/interfaces/bluetooth");
  const bsel = $("#set-bt-adapter");
  bsel.innerHTML = `<option value="">(default)</option>`;
  for (const n of bt.adapters) {
    const o = document.createElement("option"); o.value = n; o.textContent = n;
    bsel.appendChild(o);
  }
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
}

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
  setInterval(pollGps, 1500);
  setInterval(refreshLocationMarkers, 5000);
  setInterval(refreshOuiStatus, 10000);
})();
