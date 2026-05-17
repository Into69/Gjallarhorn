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
// Per-tab auto-poll: stays parked while the tab is hidden so a long
// session doesn't burn CPU on background refreshes. The Mission tab
// (live ticker, uptime, lifecycle) is the one that visibly benefits
// from polling — most other tabs are user-driven.
let _missionPollTimer = null;
function startMissionPoll() {
  stopMissionPoll();
  // 5s feels live without hammering /api/about + /api/missions.
  _missionPollTimer = setInterval(() => {
    if (document.hidden) return;
    refreshMission().catch(() => {});
  }, 5000);
}
function stopMissionPoll() {
  if (_missionPollTimer) {
    clearInterval(_missionPollTimer);
    _missionPollTimer = null;
  }
}
// Pause/resume the poll when the browser tab itself goes
// background/foreground — Chrome throttles intervals heavily on
// hidden tabs, but stopping explicitly avoids surprise spikes when
// the tab comes back.
document.addEventListener("visibilitychange", () => {
  const onMission = document.querySelector("#tab-mission.active");
  if (!onMission) return;
  if (document.hidden) stopMissionPoll();
  else { refreshMission().catch(() => {}); startMissionPoll(); }
});

$$(".tab-btn").forEach((btn) => {
  btn.addEventListener("click", () => {
    $$(".tab-btn").forEach((b) => b.classList.toggle("active", b === btn));
    const id = btn.dataset.tab;
    $$(".tab").forEach((t) => t.classList.toggle("active", t.id === `tab-${id}`));
    if (id === "map" && map) setTimeout(() => map.invalidateSize(), 50);
    if (id === "mission") { refreshMission(); startMissionPoll(); }
    else { stopMissionPoll(); }
    if (id === "devices") refreshDevices();
    if (id === "wifi-aps") refreshWifiAps();
    if (id === "locations") refreshLocations();
    if (id === "alerts") refreshAlerts();
    if (id === "logs") refreshLogs();
    if (id === "about") refreshAbout();
  });
});

// ---------- settings tab — sidebar nav ----------
// The settings tab now has a left-side nav with one section visible at a
// time. activateSettingsSection() toggles the active nav button + section
// panel, hides the form Save bar when the active section is outside the
// form (whitelist / silenced / updates / oui), and persists the last
// pick in localStorage so refreshing keeps you where you were.
function activateSettingsSection(name) {
  const navBtns = $$(".settings-nav-item");
  let matched = false;
  for (const b of navBtns) {
    const active = b.dataset.section === name && !b.hidden;
    b.classList.toggle("active", active);
    if (active) matched = true;
  }
  // Fall back to 'map' if the requested section doesn't exist or is hidden
  // (e.g. 'silenced' when there are no silenced entries).
  if (!matched) name = "map";
  for (const sec of $$(".settings-section")) {
    sec.classList.toggle("active", sec.dataset.section === name);
  }
  // Mirror the active section on .settings-content so CSS can hide the
  // Save bar when an auxiliary panel is showing.
  const content = document.querySelector(".settings-content");
  if (content) content.dataset.activeSection = name;
  try { localStorage.setItem("settingsActiveSection", name); } catch {}
}
$$(".settings-nav-item").forEach(b => {
  b.addEventListener("click", () => activateSettingsSection(b.dataset.section));
});
// Initial selection — restore from localStorage if it points at a still-
// visible section, otherwise default to 'map'.
(function restoreSettingsSection() {
  let saved = null;
  try { saved = localStorage.getItem("settingsActiveSection"); } catch {}
  activateSettingsSection(saved || "map");
})();

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
  // First-load tile gap: Leaflet reads container size at init time, but
  // the CSS grid that gives the map its final width (sidebar 320px) and
  // the `zoom` counter-sizing on body settle a frame or two later, so
  // tiles past the initially-measured width never get requested. Two
  // safety nets:
  //   1. Defer one invalidateSize past the next paint to catch the
  //      initial layout settle.
  //   2. Observe future resizes so font-scale changes or window resizes
  //      keep the map fully covered.
  const mapEl = document.getElementById("map");
  const kickInvalidate = () => { try { map.invalidateSize(); } catch {} };
  requestAnimationFrame(() => requestAnimationFrame(kickInvalidate));
  setTimeout(kickInvalidate, 250);
  if (mapEl && "ResizeObserver" in window) {
    new ResizeObserver(kickInvalidate).observe(mapEl);
  }
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

// Per-bubble popup shown on click. Hosts the "Merge into…" entry-point —
// click puts the map into merge-pick mode where the next bubble click
// chooses the target. Drawn geofences cannot be losers (the server-side
// merge_locations refuses them), so disable the button on those.
function locationPopupHtml(loc) {
  const label = escapeHtml(loc.label || `Location ${loc.id}`);
  const isManual = loc.source === "manual";
  const note = isManual
    ? `<div class="loc-popup-note">Drawn geofence — can absorb other locations, but can't be merged away itself.</div>`
    : "";
  const mergeDisabled = isManual ? ' disabled title="Drawn geofences cannot be merged away"' : "";
  // Only manual geofences expose the resize action — auto-cluster radii
  // are governed by the clustering tunables and would just be overwritten
  // on the next fix, so resizing them from the map would be misleading.
  const resizeBtn = isManual
    ? `<button type="button" class="loc-popup-resize">Adjust size…</button>`
    : "";
  return `
    <div class="loc-popup">
      <div class="loc-popup-title">#${loc.id} · ${label}</div>
      <div class="loc-popup-coords mono">${loc.lat.toFixed(5)}, ${loc.lon.toFixed(5)} · r=${Math.round(loc.radius_m)} m</div>
      ${note}
      <div class="loc-popup-actions">
        <button type="button" class="loc-popup-merge"${mergeDisabled}>Merge into…</button>
        ${resizeBtn}
      </div>
    </div>`;
}

// Resize-pick state. Set when the user picks "Adjust size…" on a drawn
// geofence's popup; while active, the geofence's circle is the only live
// element on the map — every mousemove redraws it at the new radius, and
// the next map click commits. Esc / Cancel reverts.
let resizeState = null;

function showResizeBanner(loc, radius) {
  const banner = $("#resize-banner");
  const text = banner?.querySelector(".merge-banner-text");
  if (!banner || !text) return;
  const label = loc.label || `Location ${loc.id}`;
  text.textContent =
    `Resizing #${loc.id} (${label}) — move the mouse to set radius, `
    + `click to commit. Esc to cancel.`;
  updateResizeBannerRadius(radius);
  banner.hidden = false;
}

function updateResizeBannerRadius(radius_m) {
  const el = $("#resize-banner-radius");
  if (el) el.textContent = `${Math.round(radius_m)} m`;
}

function hideResizeBanner() {
  const banner = $("#resize-banner");
  if (banner) banner.hidden = true;
}

async function enterResizeMode(loc) {
  if (loc.source !== "manual") return;
  if (resizeState) return;
  if (mergeState) exitMergeMode();   // mutually exclusive modes
  const center = L.latLng(loc.lat, loc.lon);
  // Reserve the state slot before re-rendering — refreshLocationMarkers
  // checks resizeState to skip binding a popup on the source marker and
  // on every other marker too (a click on another bubble during resize
  // should not pop up its info card).
  resizeState = {
    locId: loc.id,
    sourceLoc: loc,
    originalRadius: loc.radius_m,
    lastRadius: loc.radius_m,
    center,
    handlers: null,
  };
  await refreshLocationMarkers();
  const marker = locationMarkers.get(loc.id);
  if (!marker) { resizeState = null; return; }
  const handlers = {
    move: (e) => {
      if (!resizeState) return;
      const r = Math.max(1, center.distanceTo(e.latlng));
      marker.setRadius(r);
      resizeState.lastRadius = r;
      updateResizeBannerRadius(r);
    },
    click: async (e) => {
      if (!resizeState) return;
      const r = Math.max(1, center.distanceTo(e.latlng));
      await commitResize(loc.id, r);
    },
    keydown: (e) => {
      if (e.key === "Escape") exitResizeMode({ commit: false });
    },
  };
  resizeState.handlers = handlers;
  // The same cursor styling the draw flow uses — signals "the map is in a
  // capture mode right now".
  map.getContainer().classList.add("drawing");
  map.dragging.disable();
  showResizeBanner(loc, loc.radius_m);
  map.on("mousemove", handlers.move);
  map.on("click", handlers.click);
  document.addEventListener("keydown", handlers.keydown);
}

function exitResizeMode({ commit = false } = {}) {
  if (!resizeState) return;
  const { handlers } = resizeState;
  if (handlers) {
    map.off("mousemove", handlers.move);
    map.off("click", handlers.click);
    document.removeEventListener("keydown", handlers.keydown);
  }
  map.getContainer().classList.remove("drawing");
  map.dragging.enable();
  hideResizeBanner();
  resizeState = null;
  // Always re-render: on cancel this snaps the radius back to the DB value
  // (originalRadius); on commit it rebinds the popup with the new size. The
  // commit caller awaits this same path after the PATCH succeeds.
  if (!commit) refreshLocationMarkers();
}

async function commitResize(locId, radius_m) {
  try {
    await api(`/api/locations/${locId}`, {
      method: "PATCH",
      body: JSON.stringify({ radius_m: Math.round(radius_m * 10) / 10 }),
    });
    exitResizeMode({ commit: true });
    await refreshLocationMarkers();
    if (document.querySelector("#tab-locations.active")) {
      try { await refreshLocations(); } catch {}
    }
  } catch (e) {
    alert("Resize failed: " + (e.message || e));
    exitResizeMode({ commit: false });
  }
}

// Merge-pick state. Set when the user picks a source location from a
// popup's "Merge into…" button; the next bubble click chooses the target.
// Null in normal mode.
let mergeState = null;

function showMergeBanner(loc) {
  const banner = $("#merge-banner");
  const text = banner?.querySelector(".merge-banner-text");
  if (!banner || !text) return;
  const label = loc.label || `Location ${loc.id}`;
  text.textContent = `Merging #${loc.id} (${label}) — click any other bubble to choose the target. Esc to cancel.`;
  banner.hidden = false;
}

function hideMergeBanner() {
  const banner = $("#merge-banner");
  if (banner) banner.hidden = true;
}

function enterMergeMode(loc) {
  mergeState = { sourceId: loc.id, sourceLoc: loc };
  // Re-render markers so the source bubble is highlighted and popups are
  // suppressed (bubble click now picks a target instead of opening info).
  showMergeBanner(loc);
  refreshLocationMarkers();
}

function exitMergeMode() {
  if (!mergeState) return;
  mergeState = null;
  hideMergeBanner();
  refreshLocationMarkers();
}

async function pickMergeTarget(targetLoc) {
  if (!mergeState) return;
  const src = mergeState.sourceLoc;
  if (targetLoc.id === src.id) return;  // can't merge into self — ignore
  const srcLabel = src.label || `Location ${src.id}`;
  const tgtLabel = targetLoc.label || `Location ${targetLoc.id}`;
  const ok = confirm(
    `Merge #${src.id} (${srcLabel}) into #${targetLoc.id} (${tgtLabel})?\n\n`
    + `Devices, observations, and alert history move to #${targetLoc.id}. `
    + `#${src.id} is deleted. Whitelisted devices are preserved.`
  );
  if (!ok) return;
  try {
    await api(`/api/locations/${src.id}/merge_into/${targetLoc.id}`, { method: "POST" });
    exitMergeMode();
    await refreshLocationMarkers();
    // Devices tab dropdown caches location ids; refresh so the merged-away
    // id falls off without forcing the user to switch tabs.
    try { await loadLocationOptions(); } catch {}
    // If the Locations tab is currently visible, keep it in sync too.
    if (document.querySelector("#tab-locations.active")) {
      try { await refreshLocations(); } catch {}
    }
  } catch (e) {
    alert("Merge failed: " + (e.message || e));
  }
}

async function refreshLocationMarkers() {
  try {
    const { locations, active_id } = await api("/api/locations");
    for (const m of locationMarkers.values()) map.removeLayer(m);
    locationMarkers.clear();
    for (const loc of locations) {
      const isActive = loc.id === active_id;
      const isManual = loc.source === "manual";
      const isMergeSource = mergeState && mergeState.sourceId === loc.id;
      const isResizeSource = resizeState && resizeState.locId === loc.id;
      // Drawn geofences are styled distinctly (dashed accent stroke) so a
      // glance at the map tells you which circles you placed yourself vs.
      // which ones the auto-clusterer made. The two interactive modes get
      // their own emphasis: red while merging-from this bubble, cyan
      // (solid stroke) while live-resizing it. Resize switches the dash
      // off so the moving edge reads as solid as it grows / shrinks.
      const color = isResizeSource ? "#5cd1ff"
        : isMergeSource ? "#ff6b6b"
        : isActive ? "#79e08c"
        : isManual ? "#5cd1ff"
        : "#ffb86b";
      const c = L.circle([loc.lat, loc.lon], {
        radius: loc.radius_m,
        color,
        weight: (isMergeSource || isResizeSource) ? 3 : (isManual ? 2 : 1.5),
        dashArray: (isManual && !isResizeSource) ? "6,4" : null,
        fillOpacity: isResizeSource ? 0.12
          : isMergeSource ? 0.18
          : isActive ? 0.12
          : isManual ? 0.05
          : 0.06,
      }).bindTooltip(locationTooltipHtml(loc, isActive), {
        className: "gj-tip",
        direction: "top",
        offset: [0, -4],
        opacity: 1,
        sticky: true,
      });
      // Suppress the info popup while merge-picking or resizing — a
      // bubble click in those modes is a gesture (target selection /
      // commit), not an "open info" intent.
      if (!mergeState && !resizeState) {
        c.bindPopup(locationPopupHtml(loc), {
          className: "loc-popup-wrap",
          autoClose: true, closeButton: true,
        });
        c.on("popupopen", (ev) => {
          const el = ev.popup.getElement();
          if (!el) return;
          const mergeBtn = el.querySelector(".loc-popup-merge");
          if (mergeBtn) {
            mergeBtn.addEventListener("click", () => {
              c.closePopup();
              enterMergeMode(loc);
            });
          }
          const resizeBtn = el.querySelector(".loc-popup-resize");
          if (resizeBtn) {
            resizeBtn.addEventListener("click", () => {
              c.closePopup();
              enterResizeMode(loc);
            });
          }
        });
      }
      c.on("click", () => {
        if (mergeState) pickMergeTarget(loc);
        // In resize mode the map.click handler does the work; this
        // marker.click fires alongside it but should not re-trigger anything.
      });
      c.addTo(map);
      locationMarkers.set(loc.id, c);
    }
  } catch (e) { /* ignore */ }
}

document.addEventListener("keydown", (e) => {
  if (e.key !== "Escape") return;
  // Resize first — both modes shouldn't be active at once, but if they
  // somehow are, prefer the more-recent (resize) mode for Escape.
  if (resizeState) exitResizeMode({ commit: false });
  else if (mergeState) exitMergeMode();
});
$("#merge-banner-cancel")?.addEventListener("click", () => exitMergeMode());
$("#resize-banner-cancel")?.addEventListener("click", () => exitResizeMode({ commit: false }));

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

// Toggles the thin progress bar above the devices table. Reference-counted
// so overlapping debounce + fetch paths don't tug the bar's visibility
// against each other — it only hides when every show() has paired with a
// hide().
let _devProgressCount = 0;
function showDevProgress() {
  _devProgressCount++;
  const el = document.getElementById("dev-progress");
  if (el) el.hidden = false;
}
function hideDevProgress() {
  _devProgressCount = Math.max(0, _devProgressCount - 1);
  if (_devProgressCount === 0) {
    const el = document.getElementById("dev-progress");
    if (el) el.hidden = true;
  }
}

async function refreshDevices() {
  showDevProgress();
  try {
    await _refreshDevicesInner();
  } finally {
    hideDevProgress();
  }
}

async function _refreshDevicesInner() {
  await loadLocationOptions();
  const id = $("#dev-location").value;
  if (!id) return;
  // The Kind filter accepts compound 'bluetooth:public' / 'bluetooth:random'
  // pseudo-kinds in addition to the real DB kinds. The server side only
  // knows wifi/bluetooth/wifi_client, so we send the base kind and
  // post-filter rows by address_type in JS.
  const rawKind = $("#dev-kind").value;
  let kind = rawKind;
  let bleAddrFilter = null;
  if (rawKind && rawKind.startsWith("bluetooth:")) {
    bleAddrFilter = rawKind.split(":")[1];
    kind = "bluetooth";
  }
  const q = kind ? `?kind=${kind}` : "";
  let { devices } = id === PRESERVED_SENTINEL
    ? await api(`/api/preserved-devices${q}`)
    : await api(`/api/locations/${id}/devices${q}`);
  if (bleAddrFilter) {
    devices = devices.filter(d => {
      const at = (d.details && d.details.address_type) || "";
      return at === bleAddrFilter;
    });
  }
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
      // Backend writes naive ISO timestamps in *local* time. Parse them
      // verbatim — no "Z" suffix, no tz adjustment — so the displayed
      // window matches the wall clock the user is reading.
      const t = d.last_seen ? Date.parse(d.last_seen) : 0;
      return (Number.isFinite(t) && t > 0) ? t >= cutoff : false;
    });
  }

  // Free-text search across MAC, SSID/name, vendor, and tracker class —
  // case-insensitive. Searching "airtag" or "tile" matches classified
  // trackers.
  const q_search = ($("#dev-search")?.value || "").trim().toLowerCase();
  if (q_search) {
    rows = rows.filter(d => {
      const det = d.details || {};
      const haystack = [
        d.device_id, det.ssid, det.name, det.vendor,
        d.tracker_type,
        ...(d._merged_ssids || []),
      ].filter(Boolean).join(" ").toLowerCase();
      return haystack.includes(q_search);
    });
  }

  // Min RSSI: keep rows whose best_rssi is at or above the threshold (RSSI
  // is negative — "above" = stronger = closer to 0).
  const minRssiRaw = ($("#dev-min-rssi")?.value || "").trim();
  const minRssi = minRssiRaw === "" ? null : parseInt(minRssiRaw, 10);
  if (Number.isFinite(minRssi)) {
    rows = rows.filter(d => d.best_rssi != null && d.best_rssi >= minRssi);
  }

  const hideWl = $("#dev-hide-wl")?.checked;
  if (hideWl) {
    rows = rows.filter(d => !isWhitelisted(d.kind, d.device_id));
  }

  const trackersOnly = $("#dev-trackers-only")?.checked;
  if (trackersOnly) {
    rows = rows.filter(d => !!d.tracker_type);
  }

  const linkedOnly = $("#dev-linked-only")?.checked;
  if (linkedOnly) {
    rows = rows.filter(d => (d.linked_count || 0) > 0);
  }

  for (const d of rows) {
    tbody.appendChild(renderDeviceRow(d));
  }
  const total = (groupBssid ? groupWifiByApPrefix(devices) : devices).length;
  const filtersActive = sinceSec > 0 || q_search
    || Number.isFinite(minRssi) || hideWl || trackersOnly || linkedOnly;
  // Empty-state row when nothing renders — distinguishes "no devices
  // captured here yet" from "filters excluded everything we have". The
  // tbody colspan needs to cover every column header in #dev-table.
  if (!rows.length) {
    const thead = $("#dev-table thead tr");
    const cols = thead ? thead.children.length : 11;
    const tr = document.createElement("tr");
    tr.className = "dev-empty-row";
    const td = document.createElement("td");
    td.colSpan = cols;
    if (total === 0) {
      td.innerHTML = `
        <div class="dev-empty">
          <div class="dev-empty-title">No devices captured at this location yet.</div>
          <div class="dev-empty-hint">As wifi / bluetooth / probe scans run they'll appear here.</div>
        </div>`;
    } else {
      td.innerHTML = `
        <div class="dev-empty">
          <div class="dev-empty-title">No devices match the current filters.</div>
          <div class="dev-empty-hint">${total} device${total === 1 ? "" : "s"} are hidden — clear a filter to see them.</div>
        </div>`;
    }
    tr.appendChild(td);
    tbody.appendChild(tr);
  }
  const countEl = $("#dev-count");
  if (countEl) {
    countEl.textContent = filtersActive
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
  // Known-tracker classification (AirTag, Tile, Samsung SmartTag) — surfaced
  // as a red-bordered badge so they pop in the device list.
  const trackerLabel = {
    airtag: "AirTag/FindMy",
    tile: "Tile",
    samsung_smarttag: "Samsung SmartTag",
  }[d.tracker_type];
  const trackerBadge = trackerLabel
    ? ` <span class="tracker-tag" title="Identified by BLE adv-data pattern">${escapeHtml(trackerLabel)}</span>`
    : "";

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
    ? `<span class="mono">${escapeHtml(d.device_id)}</span> <span class="merged-tag">+${d._merged_count - 1}</span>${trackerBadge}${linkBadge}`
    : `<span class="mono">${escapeHtml(d.device_id)}</span>${trackerBadge}${linkBadge}`;
  const wlBtn = wl
    ? `<button type="button" class="icon-btn dev-wl active" data-kind="${escapeAttr(d.kind)}" data-id="${escapeAttr(d.device_id)}" title="Whitelisted — click to remove from whitelist" aria-label="Remove from whitelist">★</button>`
    : `<button type="button" class="icon-btn dev-wl" data-kind="${escapeAttr(d.kind)}" data-id="${escapeAttr(d.device_id)}" title="Whitelist this device (silences alerts and excludes from reports)" aria-label="Add to whitelist">☆</button>`;
  const timelineBtn = `<button type="button" class="icon-btn dev-timeline" data-kind="${escapeAttr(d.kind)}" data-id="${escapeAttr(d.device_id)}" title="Per-device timeline (RSSI history, locations seen at)" aria-label="Show timeline">⏱</button>`;

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
    <td>${escapeHtml(formatKindLabel(d.kind, det))}</td>
    <td>${idCell}</td>
    <td>${escapeHtml(nameOrSsid)}</td>
    <td>${escapeHtml(det.vendor || "")}</td>
    <td>${d.best_rssi}</td>
    <td>${d.last_rssi ?? ""}</td>
    <td>${d.seen_count}</td>
    <td class="mono">${formatTime(d.first_seen)}</td>
    <td class="mono">${formatTime(d.last_seen)}</td>
    <td class="row-actions">${timelineBtn}${wlBtn}</td>
    <td><details><summary>${summaryText}</summary><pre>${escapeHtml(JSON.stringify(detailsPayload, null, 2))}</pre></details></td>
  `;
  // Wire the whitelist button — done here so each row keeps its own
  // event listener bound to the right (kind, id) pair.
  tr.querySelector(".dev-wl").addEventListener("click", (ev) => {
    ev.stopPropagation();
    quickWhitelistToggle(d.kind, d.device_id);
  });
  tr.querySelector(".dev-timeline").addEventListener("click", (ev) => {
    ev.stopPropagation();
    showDeviceTimeline(d.kind, d.device_id);
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
// Search, RSSI, time-range, and toggle filters are local to the rendered
// set, so refilter without re-fetching. Debounce text/number inputs.
let _devSearchTimer = null;
let _devDebouncePending = false;
const _devDebounce = () => {
  // Show the progress bar immediately so typing feels responsive, even
  // before the debounce window elapses and the actual fetch fires.
  if (!_devDebouncePending) {
    _devDebouncePending = true;
    showDevProgress();
  }
  clearTimeout(_devSearchTimer);
  _devSearchTimer = setTimeout(async () => {
    try {
      await refreshDevices();
    } finally {
      _devDebouncePending = false;
      hideDevProgress();
    }
  }, 150);
};
$("#dev-search")?.addEventListener("input", _devDebounce);
$("#dev-min-rssi")?.addEventListener("input", _devDebounce);
$("#dev-since")?.addEventListener("change", refreshDevices);
$("#dev-hide-wl")?.addEventListener("change", refreshDevices);
$("#dev-trackers-only")?.addEventListener("change", refreshDevices);
$("#dev-linked-only")?.addEventListener("change", refreshDevices);

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
// Eraser — clear devices at a location while keeping the location.
const ICON_ERASER = `<svg viewBox="0 0 16 16" width="14" height="14" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M2 12 9 5 13 9 6 16 H2 Z"/><path d="M9 5 11 3 14 6 12 8"/><path d="M2 16 H10"/></svg>`;
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
// Beacon/radar — "baseline scan". Three arcs radiating from a center dot.
const ICON_BASELINE = `<svg viewBox="0 0 16 16" width="16" height="16" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round"><circle cx="8" cy="8" r="1.4" fill="currentColor" stroke="none"/><path d="M5 8a3 3 0 0 1 6 0"/><path d="M3 8a5 5 0 0 1 10 0"/><path d="M1 8a7 7 0 0 1 14 0"/></svg>`;

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
        <button type="button" class="icon-btn clear-loc-devices" data-id="${loc.id}" title="Clear devices at this location (keeps the location row; whitelisted devices are preserved)" aria-label="Clear devices">${ICON_ERASER}</button>
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
  $$(".clear-loc-devices").forEach((b) =>
    b.addEventListener("click", async () => {
      const id = b.dataset.id;
      if (!confirm(
        `Clear all devices at location #${id}?\n\n` +
        `Every device row and observation tied to this location will be removed. ` +
        `Whitelisted devices' history is preserved (visible under the Devices tab's ` +
        `"Preserved (whitelist)" pseudo-location). The location itself stays.`
      )) return;
      try {
        const r = await api(`/api/locations/${id}/devices`, { method: "DELETE" });
        const d = r.deleted || {};
        alert(
          `Cleared ${d.devices || 0} devices and ${d.observations || 0} observations.` +
          (d.preserved ? `\n${d.preserved} whitelisted device row(s) archived.` : "")
        );
        await refreshLocations();
        await refreshDevices();   // Devices tab might be looking at this loc
      } catch (e) {
        alert("Clear failed: " + e.message);
      }
    })
  );
}

$("#loc-refresh").addEventListener("click", refreshLocations);
$("#loc-new").addEventListener("click", async () => {
  try { await api("/api/locations/new", { method: "POST" }); refreshLocations(); }
  catch (e) { alert(e.message); }
});

function showMergeProgress(done, total) {
  const prog = $("#loc-merge-progress");
  const label = $("#loc-merge-progress-label");
  if (!prog || !label) return;
  // Total can grow mid-merge (a winner's expanded radius may newly contain
  // other locations on the next iteration), so guard against done > max.
  const max = Math.max(1, total, done);
  prog.max = max;
  prog.value = done;
  prog.hidden = false;
  label.textContent = `${done} / ${max}`;
  label.hidden = false;
}

function hideMergeProgress() {
  const prog = $("#loc-merge-progress");
  const label = $("#loc-merge-progress-label");
  if (prog) prog.hidden = true;
  if (label) label.hidden = true;
}

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
    const totals = { devices_moved: 0, devices_combined: 0, observations_moved: 0 };
    let merged = 0;
    let total = pairs.length;
    showMergeProgress(0, total);

    // Step the merge loop one pair at a time so the progress bar can
    // advance with each transaction. Hard cap matches the backend's
    // auto_merge_contained safety bound.
    for (let i = 0; i < 1000; i++) {
      const r = await api("/api/locations/merge_contained/step", { method: "POST" });
      if (r.done) break;
      merged++;
      const m = r.merged || {};
      totals.devices_moved += m.devices_moved || 0;
      totals.devices_combined += m.devices_combined || 0;
      totals.observations_moved += m.observations_moved || 0;
      // `remaining` is a hint from the server; if it surfaced more pairs
      // (chain growth from radius expansion), grow the bar's max.
      const projected = merged + (r.remaining || 0);
      if (projected > total) total = projected;
      showMergeProgress(merged, total);
    }
    hideMergeProgress();

    alert(
      `Merged ${merged} location(s).\n\n` +
      `Devices reattributed: ${totals.devices_moved}\n` +
      `Devices combined (collisions): ${totals.devices_combined}\n` +
      `Observations moved: ${totals.observations_moved}`
    );
    await refreshLocations();
    await refreshLocationMarkers();
    await loadLocationOptions();
  } catch (e) {
    hideMergeProgress();
    alert("Merge failed: " + e.message);
  } finally {
    btn.disabled = false; btn.textContent = orig;
  }
});

function showReportProgress(done, total, label) {
  const prog = $("#loc-report-progress");
  const labelEl = $("#loc-report-progress-label");
  if (!prog || !labelEl) return;
  const max = Math.max(1, total, done);
  prog.max = max;
  prog.value = done;
  prog.hidden = false;
  labelEl.textContent = label
    ? `${label} (${done}/${max})`
    : `${done}/${max}`;
  labelEl.hidden = false;
}

function hideReportProgress() {
  const prog = $("#loc-report-progress");
  const label = $("#loc-report-progress-label");
  if (prog) prog.hidden = true;
  if (label) label.hidden = true;
}

$("#loc-report").addEventListener("click", async () => {
  const btn = $("#loc-report");
  const orig = btn.textContent;
  btn.disabled = true; btn.textContent = "Starting…";
  showReportProgress(0, 1, "queued");
  try {
    // Mirror the Devices tab's "Group multi-BSSID APs" checkbox so the PDF
    // device tables match what the user sees on the Devices tab.
    const groupBssids = $("#dev-group-bssid")?.checked ? 1 : 0;
    await api(`/api/locations/report/start?group_bssids=${groupBssids}`, {
      method: "POST",
    });
    btn.textContent = "Generating…";

    // Poll status; bail on error or once ready=true.
    let ready = false;
    let last_err = null;
    for (let i = 0; i < 600; i++) {  // 600 * 500ms = 5 min cap
      await new Promise(r => setTimeout(r, 500));
      let st;
      try {
        st = await api("/api/locations/report/status");
      } catch (e) {
        last_err = e;
        continue;  // transient — retry
      }
      if (st.error) {
        throw new Error(st.error);
      }
      showReportProgress(st.stage_n || 0, st.stage_total || 1, st.stage_label || "");
      if (st.ready) { ready = true; break; }
      if (!st.running && st.error) throw new Error(st.error);
    }
    if (!ready) throw last_err || new Error("report timed out");

    // Fetch the bytes and trigger the browser's save-as.
    const res = await fetch("/api/locations/report/result.pdf");
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
    hideReportProgress();
    btn.disabled = false; btn.textContent = orig;
  }
});
// Legacy listener — the Reset button moved to the Mission tab so this
// element may not exist on a fresh load. Optional-chain prevents a
// startup crash; the click handler below still runs if the element is
// re-introduced somewhere later.
$("#loc-reset")?.addEventListener("click", async () => {
  const ok = confirm(
    "Reset auto-clustered locations?\n\n" +
    "Wipes every auto-clustered sensor location and the devices/" +
    "observations attached to them. Drawn geofences are kept, and " +
    "whitelisted devices' history is archived to the preserved list. " +
    "The temporary whitelist is left intact.\n\n" +
    "This cannot be undone."
  );
  if (!ok) return;
  const btn = $("#loc-reset");
  btn.disabled = true; btn.textContent = "Resetting…";
  try {
    const res = await api("/api/locations/reset", { method: "POST" });
    const d = res.deleted || {};
    alert(
      `Reset complete:\n` +
      `  ${d.locations || 0} auto location(s) removed\n` +
      `  ${d.devices || 0} device row(s) cleared\n` +
      `  ${d.observations || 0} observation(s) cleared` +
      (d.preserved ? `\n  ${d.preserved} whitelisted device row(s) archived` : "") +
      (d.alerts_cleared ? `\n  ${d.alerts_cleared} alert event(s) cleared` : "")
    );
    await refreshLocations();
    await refreshLocationMarkers();
    await loadLocationOptions();
    await refreshDevices();
  } catch (e) {
    alert("Reset failed: " + e.message);
  } finally {
    btn.disabled = false; btn.textContent = "Reset";
  }
});

// Legacy listener — same as above, the Delete-all button moved to the
// Mission tab. Optional-chain so the script doesn't blow up on load.
$("#loc-delete-all")?.addEventListener("click", async () => {
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
    const bits = [
      `${d.locations || 0} location(s)`,
      `${d.devices || 0} device(s)`,
      `${d.observations || 0} observation(s)`,
    ];
    if (d.preserved) bits.push(`${d.preserved} whitelist row(s) archived`);
    if (d.temp_whitelist_cleared) bits.push(`${d.temp_whitelist_cleared} temp whitelist entr${d.temp_whitelist_cleared === 1 ? "y" : "ies"} cleared`);
    if (d.alerts_cleared) bits.push(`${d.alerts_cleared} alert event(s) cleared`);
    alert("Deleted:\n  • " + bits.join("\n  • "));
    await refreshLocations();
    await refreshLocationMarkers();
    await loadLocationOptions();
    await refreshTempWhitelist();
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
  applyBtcTriggerMode();
  applyFontScale(s.font_scale || "default");
  // Kick off the probe channel checkbox grid for the saved interface (if any).
  refreshProbeChannelsForIface((s.probe_interface || "").trim() || null);
}

// Set the body[data-font-scale] attribute that drives the global zoom
// CSS rules. "default" is the no-zoom path, so we clear the attribute
// instead of setting it — keeps the DOM clean in the common case.
//
// Changing the attribute flips the body's CSS width/height (via the
// `100vh / var(--ui-zoom)` counter-sizing) and reflows the grid, which
// resizes the map cell. Leaflet's tile grid was sized for the previous
// dimensions, so it needs an explicit invalidateSize after the reflow
// settles or the right-hand tiles never get requested. Two animation
// frames is enough for the layout to land before we measure.
function applyFontScale(value) {
  const valid = new Set(["x-small", "small", "default", "large", "x-large"]);
  const v = valid.has(value) ? value : "default";
  if (v === "default") document.body.removeAttribute("data-font-scale");
  else document.body.setAttribute("data-font-scale", v);
  if (typeof map !== "undefined" && map) {
    const kick = () => { try { map.invalidateSize(); } catch {} };
    // Two animation frames covers the layout-settle path; a longer
    // setTimeout catches the slow case where the body's counter-sized
    // dimensions haven't propagated to the grid track by the time the
    // raf chain runs.
    requestAnimationFrame(() => requestAnimationFrame(kick));
    setTimeout(kick, 250);
  }
}
$("#set-font-scale")?.addEventListener("change", (e) => applyFontScale(e.target.value));

function applyLocDynamicEnabled() {
  const cb = $("#set-loc-dynamic");
  const t = $("#set-loc-dynamic-t");
  if (!cb || !t) return;
  t.disabled = !cb.checked;
  t.style.opacity = cb.checked ? "1" : "0.5";
}
$("#set-loc-dynamic")?.addEventListener("change", applyLocDynamicEnabled);

// Bluetooth Classic trigger mode — show the "every N BLE scans" field
// only when that mode is selected. The scan-interval input stays visible
// in both modes (it's a safety-net fallback in after_ble_scans mode).
function applyBtcTriggerMode() {
  const sel = $("#set-btc-trigger");
  const wrap = $("#set-btc-every-n-wrap");
  if (!sel || !wrap) return;
  wrap.hidden = sel.value !== "after_ble_scans";
}
$("#set-btc-trigger")?.addEventListener("change", applyBtcTriggerMode);

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
  if (lastEl && upEl) {
    if (!s) {
      lastEl.textContent = "—";
      upEl.textContent = "—";
    } else {
      const now = Date.now() / 1000;
      lastEl.textContent = s.last_probe_at ? formatProbeAgo(now - s.last_probe_at) : "never";
      upEl.textContent = (s.started_at && s.running) ? formatProbeDuration(now - s.started_at) : "—";
    }
  }
  // Same per-second tick reuses last fetched scanner stats so "Last scan"
  // ages without waiting for the next 3-second poll.
  tickScannerRelativeTimes();
}

let _scannersLastStatus = null;

async function refreshScannerStatus() {
  let s;
  try {
    s = await api("/api/scanners/status");
  } catch (e) {
    return;
  }
  _scannersLastStatus = s;
  updateScannerCard("wifi", s.wifi || {}, s.paused);
  updateScannerCard("bt", s.bluetooth || {}, s.paused);
  tickScannerRelativeTimes();
}

function updateScannerCard(prefix, stats, paused) {
  const id = (suffix) => `#${prefix}-map-${suffix}`;
  const stateEl = $(id("state"));
  if (!stateEl) return;

  // The Bluetooth card has a secondary transport (BLE vs BR/EDR) the
  // wifi card doesn't, so the state-pill text includes which transport
  // is mid-scan and a small badge to the left advertises the active
  // mode at a glance.
  const isBT = prefix === "bt";
  const classicEnabled = isBT && !!stats.classic_enabled;
  const classicRunning = isBT && !!stats.classic_running;
  const bleRunning = !!stats.running;
  const liveTransport = isBT
    ? (classicRunning ? " · BR/EDR" : (bleRunning ? " · BLE" : ""))
    : "";

  if (bleRunning || classicRunning) {
    stateEl.className = "probe-state-pill running";
    stateEl.textContent = "scanning" + liveTransport;
  } else if (paused) {
    stateEl.className = "probe-state-pill stopped";
    stateEl.textContent = "paused";
  } else if (stats.last_error) {
    stateEl.className = "probe-state-pill error";
    stateEl.textContent = "error";
  } else if (stats.scan_count) {
    stateEl.className = "probe-state-pill stopped";
    stateEl.textContent = "idle";
  } else {
    stateEl.className = "probe-state-pill stopped";
    stateEl.textContent = "waiting";
  }

  // Transport badge (Bluetooth card only). Reflects what's configured to
  // run, not just what's currently scanning — gives operators a stable
  // indicator of mode even between scan ticks.
  if (isBT) {
    const transportEl = $(id("transport"));
    if (transportEl) {
      if (classicRunning) {
        transportEl.className = "bt-transport-pill classic";
        transportEl.textContent = "BR/EDR";
      } else if (classicEnabled) {
        transportEl.className = "bt-transport-pill dual";
        transportEl.textContent = "BLE + BR/EDR";
      } else {
        transportEl.className = "bt-transport-pill ble";
        transportEl.textContent = "BLE";
      }
    }
  }

  const metaEl = $(id("meta"));
  if (metaEl) {
    const parts = [];
    if (stats.interface) parts.push(stats.interface);
    else if (stats.configured_iface) parts.push(`${stats.configured_iface} (configured)`);
    else if (stats.configured_adapter) parts.push(`${stats.configured_adapter} (configured)`);
    else parts.push(prefix === "wifi" ? "no interface configured" : "default adapter");
    if (stats.scan_duration_s) parts.push(`BLE scans ${stats.scan_duration_s}s`);
    if (isBT && classicEnabled) {
      // Spell out the Classic cadence so operators can tell from the
      // card whether BR/EDR is on a fixed interval or piggy-backed on
      // BLE scans.
      const dur = stats.classic_scan_duration_s;
      const trig = stats.classic_trigger === "after_ble_scans"
        ? `every ${stats.classic_every_n_ble_scans || "?"} BLE scans`
        : `every ${stats.classic_scan_interval_s || "?"}s`;
      parts.push(`BR/EDR ${dur ? dur + "s, " : ""}${trig}`);
    }
    metaEl.textContent = parts.join(" · ");
  }

  const errEl = $(id("error"));
  if (errEl) {
    if (stats.last_error && !stats.running) {
      errEl.hidden = false;
      errEl.textContent = stats.last_error;
    } else {
      errEl.hidden = true;
    }
  }

  const lastCountEl = $(id("last-count"));
  if (lastCountEl) lastCountEl.textContent = (stats.last_scan_devices ?? "—").toString();

  const totalEl = $(id("total"));
  if (totalEl) totalEl.textContent = (stats.total_devices_seen ?? 0).toLocaleString();

  const durEl = $(id("duration"));
  if (durEl) {
    durEl.textContent = stats.last_scan_duration_s != null
      ? `${stats.last_scan_duration_s.toFixed(1)}s`
      : "—";
  }

  const intvEl = $(id("interval"));
  if (intvEl) intvEl.textContent = stats.scan_interval_s ? `${stats.scan_interval_s}s` : "—";
}

function tickScannerRelativeTimes() {
  const s = _scannersLastStatus;
  if (!s) return;
  const now = Date.now() / 1000;
  for (const [prefix, stats] of [["wifi", s.wifi], ["bt", s.bluetooth]]) {
    if (!stats) continue;
    const lastEl = $(`#${prefix}-map-last`);
    const upEl = $(`#${prefix}-map-uptime`);
    if (lastEl) {
      lastEl.textContent = stats.last_scan_at
        ? formatProbeAgo(now - stats.last_scan_at)
        : "never";
    }
    if (upEl) {
      upEl.textContent = stats.started_at
        ? `up ${formatProbeDuration(now - stats.started_at)}`
        : "—";
    }
  }
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

// ---------- wifi APs tab ----------
// Groups WiFi captures (kind='wifi') by SSID and folds the wifi_client
// probes that named each SSID under it as "associated" clients. Caches
// the last fetched payload so search/hide-orphans filters can rerender
// without round-tripping the server.
let _wapCache = [];
let _wapExpandedSsids = new Set();   // remembers which groups the user opened

async function refreshWifiAps() {
  const list = $("#wap-list");
  const progress = $("#wap-progress");
  if (progress) progress.hidden = false;
  try {
    // Mirror the Devices-tab location dropdown so APs and probes are
    // always scoped to one bubble — matches "where was this AP found"
    // semantics rather than aggregating across every visit ever.
    await populateWapLocations();
    const locVal = $("#wap-location").value;
    if (!locVal) {
      _wapCache = [];
      if (list) {
        list.innerHTML = `<div class="muted">No locations yet — capture a fix or draw a geofence to start scoping WiFi APs.</div>`;
      }
      const counter = $("#wap-count");
      if (counter) counter.textContent = "";
      return;
    }
    const data = await api(`/api/wifi/aps?location_id=${encodeURIComponent(locVal)}`);
    _wapCache = data.aps || [];
    renderWifiAps();
  } catch (e) {
    if (list) list.innerHTML = `<div class="muted">Could not load WiFi APs: ${escapeHtml(e.message || String(e))}</div>`;
  } finally {
    if (progress) progress.hidden = true;
  }
}

async function populateWapLocations() {
  const sel = $("#wap-location");
  if (!sel) return;
  const prev = sel.value;
  try {
    const { locations, active_id } = await api("/api/locations");
    sel.innerHTML = "";
    for (const loc of locations || []) {
      const o = document.createElement("option");
      o.value = loc.id;
      o.textContent = `${loc.label || `Loc ${loc.id}`}${loc.id === active_id ? " (active)" : ""}`;
      sel.appendChild(o);
    }
    // Default to whichever location is currently selected; otherwise the
    // active location (matches the Devices-tab default); otherwise the
    // first one in the list so the user has somewhere to land.
    const ids = new Set((locations || []).map(l => String(l.id)));
    if (prev && ids.has(prev)) {
      sel.value = prev;
    } else if (active_id != null && ids.has(String(active_id))) {
      sel.value = String(active_id);
    } else if (sel.options.length) {
      sel.value = sel.options[0].value;
    }
  } catch { /* keep whatever's there */ }
}

function renderWifiAps() {
  const list = $("#wap-list");
  const counter = $("#wap-count");
  if (!list) return;
  const q = ($("#wap-search")?.value || "").trim().toLowerCase();
  const hideOrphans = !!$("#wap-hide-orphans")?.checked;

  const filtered = _wapCache.filter(g => {
    if (hideOrphans && !g.bssid_count) return false;
    if (!q) return true;
    if ((g.ssid || "").toLowerCase().includes(q)) return true;
    if (g.bssids.some(b =>
      (b.bssid || "").toLowerCase().includes(q) ||
      (b.vendor || "").toLowerCase().includes(q)
    )) return true;
    if (g.clients.some(c =>
      (c.device_id || "").toLowerCase().includes(q) ||
      (c.vendor || "").toLowerCase().includes(q)
    )) return true;
    return false;
  });

  if (counter) {
    // Surface the visible / probe-only split so it's obvious at a glance
    // how many "wanted but unseen" SSIDs are in view alongside the
    // captured APs. The split reflects the filtered set, not the
    // global cache.
    const visibleCount = filtered.filter(g => g.bssid_count > 0).length;
    const wantedCount = filtered.length - visibleCount;
    const total = _wapCache.length;
    const scope = filtered.length === total
      ? `${total} SSID${total === 1 ? "" : "s"}`
      : `${filtered.length} of ${total}`;
    counter.textContent = `${scope} · ${visibleCount} visible · ${wantedCount} probe-only`;
  }
  if (!filtered.length) {
    list.innerHTML = `<div class="muted">${
      _wapCache.length ? "No SSIDs match the current filters." : "No WiFi captures yet."
    }</div>`;
    return;
  }
  list.innerHTML = filtered.map(renderWapGroup).join("");
  // Wire the open/close persistence on each <details>.
  for (const det of list.querySelectorAll("details.wap-group")) {
    const ssid = det.dataset.ssid || "";
    det.addEventListener("toggle", () => {
      if (det.open) _wapExpandedSsids.add(ssid);
      else _wapExpandedSsids.delete(ssid);
    });
  }
}

function renderWapGroup(g) {
  const ssidLabel = g.is_hidden ? "(hidden / wildcard)" : g.ssid;
  const opened = _wapExpandedSsids.has(g.ssid) ? " open" : "";
  const encs = new Set();
  for (const b of g.bssids) if (b.encryption) encs.add(b.encryption);
  const encBadge = encs.size
    ? `<span class="wap-badge enc">${escapeHtml([...encs].join(" / "))}</span>`
    : (g.bssid_count
        ? `<span class="wap-badge open">open</span>`
        : "");
  // Topology badge — driven by the server-side _classify_wifi_topology
  // call which inspects the 802.11 IEs we parsed off each BSSID:
  //   mesh        — Mesh ID / Mesh Configuration IE present (true 802.11s)
  //   ess         — shared 802.11r Mobility Domain (federated roaming)
  //   multi_band  — same OUI across ≥2 bands (single radio, multiple bands)
  //   multi_ap    — ≥2 BSSIDs but none of the above signals (generic)
  //   single      — just one BSSID; no badge
  const topo = g.topology || {};
  const tBands = (topo.bands || []).filter(Boolean);
  let meshBadge = "";
  if (topo.kind === "mesh") {
    const ids = (topo.mesh_ids || []).filter(Boolean);
    const tip = "802.11s mesh — Mesh ID / Mesh Configuration IE seen"
      + (ids.length ? ` (${ids.join(", ")})` : "")
      + (tBands.length ? ` · ${tBands.join(" + ")}` : "");
    meshBadge = `<span class="wap-badge mesh" title="${escapeAttr(tip)}">mesh · ${g.bssid_count} radios</span>`;
  } else if (topo.kind === "ess") {
    const tip = "Federated ESS — shared 802.11r Mobility Domain"
      + (topo.mobility_domain ? ` (MDID ${topo.mobility_domain})` : "")
      + (tBands.length ? ` · ${tBands.join(" + ")}` : "");
    meshBadge = `<span class="wap-badge ess" title="${escapeAttr(tip)}">ESS · ${g.bssid_count} APs</span>`;
  } else if (topo.kind === "multi_band") {
    const tip = "Multi-band radio — one device advertising the SSID across "
      + (tBands.length ? tBands.join(" + ") : "multiple bands");
    meshBadge = `<span class="wap-badge multiband" title="${escapeAttr(tip)}">multi-band · ${g.bssid_count} radios</span>`;
  } else if (topo.kind === "multi_ap") {
    const tip = "Multiple BSSIDs share this SSID, but no mesh / ESS IE was captured. "
      + "Likely multiple APs federated under one SSID, or scan output missing IEs.";
    meshBadge = `<span class="wap-badge multiap" title="${escapeAttr(tip)}">multi-AP · ${g.bssid_count} radios</span>`;
  }
  // Status dot left of the SSID name — visible when at least one AP
  // was captured for this SSID, "wanted" when only probe requests
  // have named it (clients hunting for a network that hasn't been
  // observed). Uses a colored dot rather than emoji so the icon
  // matches the rest of the muted/accent palette.
  const visible = g.bssid_count > 0;
  const statusDot = visible
    ? `<span class="wap-state visible" title="Visible — ${g.bssid_count} AP${g.bssid_count === 1 ? "" : "s"} captured for this SSID" aria-label="visible"></span>`
    : `<span class="wap-state wanted" title="Wanted — clients have probed for this SSID but no AP has been captured" aria-label="probe-only"></span>`;
  const bestRssi = g.best_rssi != null ? `${g.best_rssi} dBm` : "—";
  const bssidRows = g.bssids.length
    ? g.bssids.map(b => `
        <tr>
          <td class="mono">${escapeHtml(b.bssid)}</td>
          <td>${escapeHtml(b.vendor || "")}</td>
          <td>${b.channel != null ? escapeHtml(String(b.channel)) : ""}${b.band ? ` <span class="muted">${escapeHtml(b.band)}</span>` : ""}</td>
          <td>${escapeHtml(b.encryption || "open")}</td>
          <td>${b.best_rssi != null ? b.best_rssi + " dBm" : ""}</td>
          <td>${b.seen_count ?? ""}</td>
          <td class="mono">${escapeHtml(formatTime(b.last_seen))}</td>
          <td>${b.location_id != null ? `#${b.location_id}` : ""}</td>
        </tr>`).join("")
    : `<tr><td colspan="8" class="muted">No AP captured for this SSID — clients have been probing for it.</td></tr>`;

  const clientRows = g.clients.length
    ? g.clients.map(c => {
        const probed = (c.ssids || []).filter(s => s).slice(0, 6).join(", ");
        const more = (c.ssids || []).filter(s => s).length > 6 ? ` (+${(c.ssids || []).filter(s => s).length - 6})` : "";
        return `
        <tr>
          <td class="mono">${escapeHtml(c.device_id)}${c.randomized ? ' <span class="wap-badge rand">rand</span>' : ""}</td>
          <td>${escapeHtml(c.vendor || "")}</td>
          <td>${(c.channels || []).join(", ")}</td>
          <td>${c.best_rssi != null ? c.best_rssi + " dBm" : ""}</td>
          <td>${c.seen_count ?? ""}</td>
          <td class="mono">${escapeHtml(formatTime(c.last_seen))}</td>
          <td>${escapeHtml(probed)}${more ? `<span class="muted">${escapeHtml(more)}</span>` : ""}</td>
        </tr>`;
      }).join("")
    : `<tr><td colspan="7" class="muted">No clients have been observed probing for this network.</td></tr>`;

  return `
    <details class="wap-group"${opened} data-ssid="${escapeAttr(g.ssid)}">
      <summary>
        ${statusDot}
        <span class="wap-ssid">${escapeHtml(ssidLabel)}</span>
        ${encBadge}
        ${meshBadge}
        <span class="wap-stats">
          <span class="wap-stat" title="Access points (BSSIDs) advertising this SSID">${g.bssid_count} AP${g.bssid_count === 1 ? "" : "s"}</span>
          <span class="wap-stat" title="WiFi clients seen probing for this SSID">${g.client_count} client${g.client_count === 1 ? "" : "s"}</span>
          <span class="wap-stat" title="Strongest RSSI across all BSSIDs for this SSID">${bestRssi}</span>
        </span>
      </summary>
      <div class="wap-body">
        <h4 class="wap-h">Access points</h4>
        <table class="wap-table">
          <thead><tr>
            <th>BSSID</th><th>Vendor</th><th>Channel</th><th>Encryption</th>
            <th>Best RSSI</th><th>Seen</th><th>Last seen</th><th>Loc</th>
          </tr></thead>
          <tbody>${bssidRows}</tbody>
        </table>
        <h4 class="wap-h">Clients seen probing for this network</h4>
        <table class="wap-table">
          <thead><tr>
            <th>MAC</th><th>Vendor</th><th>Channels</th><th>Best RSSI</th>
            <th>Probes</th><th>Last seen</th><th>Other SSIDs probed</th>
          </tr></thead>
          <tbody>${clientRows}</tbody>
        </table>
      </div>
    </details>`;
}

$("#wap-refresh")?.addEventListener("click", refreshWifiAps);
$("#wap-location")?.addEventListener("change", refreshWifiAps);
$("#wap-search")?.addEventListener("input", () => {
  clearTimeout(_wapSearchTimer);
  _wapSearchTimer = setTimeout(renderWifiAps, 120);
});
$("#wap-hide-orphans")?.addEventListener("change", renderWifiAps);
$("#wap-expand-all")?.addEventListener("click", () => {
  for (const det of document.querySelectorAll("#wap-list details.wap-group")) {
    det.open = true;
    if (det.dataset.ssid != null) _wapExpandedSsids.add(det.dataset.ssid);
  }
});
$("#wap-collapse-all")?.addEventListener("click", () => {
  for (const det of document.querySelectorAll("#wap-list details.wap-group")) {
    det.open = false;
  }
  _wapExpandedSsids.clear();
});
let _wapSearchTimer = null;

// ---------- alerts ----------
const MATCH_TYPE_LABEL = {
  device_id: "device id",
  name_contains: "name contains",
  vendor_contains: "vendor contains",
  rssi_above: "RSSI ≥",
  new_device: "new device (after Ns)",
  cross_location: "cross-location M/N",
  persistent_companion: "persistent companion M/H",
  co_arrival_transit: "co-arrival M/N/Ws",
  travel_time_companion: "travel-time T/V",
  approach_vector: "approach D/T",
  novel_location_chain: "novel-locations N/H",
  mac_rotation_rate: "MAC rotation K/H",
  cross_kind_co_travel: "cross-kind co-travel M/H",
  arrival_after_gap: "arrival after N min gap",
  absence_gap: "absence ≥ N min",
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
      <td>${r.location_id == null ? "any" : r.location_id === -1 ? "active" : r.location_id}</td>
      <td><input type="checkbox" class="rule-discord" data-id="${r.id}" ${r.notify_discord ? "checked" : ""}></td>
      <td><input type="checkbox" class="rule-audible" data-id="${r.id}" ${r.audible ? "checked" : ""}></td>
      <td><input type="checkbox" class="rule-latch" data-id="${r.id}" ${(r.latch ?? 1) ? "checked" : ""}></td>
      <td><input type="checkbox" class="rule-include-wl" data-id="${r.id}" ${r.include_whitelist ? "checked" : ""}></td>
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
  $$(".rule-latch").forEach(cb =>
    cb.addEventListener("change", async () => {
      await api(`/api/alerts/rules/${cb.dataset.id}`, {
        method: "PATCH", body: JSON.stringify({ latch: cb.checked }),
      });
    })
  );
  $$(".rule-include-wl").forEach(cb =>
    cb.addEventListener("change", async () => {
      await api(`/api/alerts/rules/${cb.dataset.id}`, {
        method: "PATCH", body: JSON.stringify({ include_whitelist: cb.checked }),
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
  // Populate the location dropdown in the form. Two special options:
  //   ""  any location
  //   -1  active location (resolves to the current active loc at fire time)
  try {
    const locs = await api("/api/locations");
    const sel = $("#rule-location");
    const prev = sel.value;
    const activeId = locs.active_id;
    const activeLabel = activeId != null
      ? `active location (currently #${activeId})`
      : "active location (no active location)";
    sel.innerHTML =
      `<option value="">any</option>` +
      `<option value="-1">${escapeHtml(activeLabel)}</option>`;
    for (const loc of locs.locations || []) {
      const o = document.createElement("option");
      o.value = loc.id; o.textContent = `${loc.id} · ${loc.label || ""}`.trim();
      sel.appendChild(o);
    }
    if (prev) sel.value = prev;  // preserve mid-edit selection
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
  // Whitelist toggle: alerts only fire on non-whitelisted devices, so the
  // button typically reads ☆. Once whitelisted (here or on the Devices tab)
  // we render the active ★ — historical events stay in the feed for
  // reference but are clearly marked as actioned.
  const wl = isWhitelisted(e.device_kind, e.device_id);
  const wlBtn = wl
    ? `<button type="button" class="icon-btn alert-wl active" disabled title="Whitelisted — future alerts on this device are suppressed" aria-label="Already whitelisted">★</button>`
    : `<button type="button" class="icon-btn alert-wl" data-kind="${escapeAttr(e.device_kind)}" data-id="${escapeAttr(e.device_id)}" title="Whitelist this device — silences future alerts and excludes it from PDF reports" aria-label="Whitelist device">☆</button>`;
  // Latch state only applies when the rule itself latches. Rules with
  // latch=0 fire on every match — the alert_events row gets cleared=0
  // because nothing flipped it, but there's no actual latch to hold or
  // release. Hide the badge and unlatch button entirely for those.
  // rule_latch defaults to 1 for legacy events that pre-date the column.
  // absence_gap is a special case: the rule type always latches
  // server-side (fire once per absence period, regardless of the rule's
  // latch flag) since 30s-poll spam would be useless. Mirror that here
  // so the badge shows up even when the rule was saved with latch=0.
  const ruleLatches = (e.rule_latch ?? 1) !== 0
    || e.rule_match_type === "absence_gap";
  const latched = ruleLatches
    && (e.cleared === 0 || e.cleared === false || e.cleared == null);
  let latchBadge = "";
  let latchBtn = "";
  if (ruleLatches) {
    latchBadge = latched
      ? `<span class="latch-tag" title="Latched — this rule won't fire again on this device until cleared">🔒 latched</span>`
      : `<span class="latch-tag cleared" title="Acknowledged">cleared</span>`;
    if (latched) {
      latchBtn = `<button type="button" class="icon-btn alert-clear" data-rule="${escapeAttr(e.rule_id)}" data-id="${escapeAttr(e.device_id)}" title="Clear the latch so future matches fire again" aria-label="Clear latch">🔓</button>`;
    }
  }
  return `
    <div class="alert-item kind-${escapeHtml(e.device_kind)} ${ruleLatches && !latched ? "alert-cleared" : ""}">
      <div>
        <span class="alert-rule">${escapeHtml(e.rule_name || "rule " + e.rule_id)}</span>
        <span class="muted"> matched </span>
        <span class="alert-device">${escapeHtml(e.device_id)}</span>
        ${label ? `<span class="muted"> · </span><span>${escapeHtml(label)}</span>` : ""}
        ${vendor}
        ${latchBadge}
      </div>
      <div class="alert-meta">
        <span class="alert-rssi">${e.rssi != null ? e.rssi + " dBm" : ""}</span>
        · ${escapeHtml(where)}
        · ${formatTime(e.triggered_at)}
        ${latchBtn}${wlBtn}
      </div>
    </div>
  `;
}

// Single delegated handler — the alert list is re-rendered on filter
// changes and on every poll, so per-row listeners would churn. Handles
// both the whitelist (★) and unlatch (🔓) buttons.
$("#alerts-list")?.addEventListener("click", async (ev) => {
  const wlBtn = ev.target.closest(".alert-wl");
  if (wlBtn && !wlBtn.disabled) {
    const kind = wlBtn.dataset.kind;
    const deviceId = wlBtn.dataset.id;
    if (!kind || !deviceId) return;
    if (!confirm(
      `Whitelist ${kind} ${deviceId}?\n\n` +
      `This silences all future alerts for this device and excludes it from PDF reports.`
    )) return;
    wlBtn.disabled = true;
    try {
      await api("/api/whitelist", {
        method: "POST",
        body: JSON.stringify({ kind, device_id: deviceId, note: "from alert" }),
      });
      await refreshWhitelist();
      renderFilteredAlerts();
      await refreshDevices();
    } catch (err) {
      alert("Whitelist failed: " + err.message);
      wlBtn.disabled = false;
    }
    return;
  }
  const clrBtn = ev.target.closest(".alert-clear");
  if (clrBtn) {
    const ruleId = parseInt(clrBtn.dataset.rule, 10);
    const deviceId = clrBtn.dataset.id;
    if (!Number.isFinite(ruleId) || !deviceId) return;
    clrBtn.disabled = true;
    try {
      await api("/api/alerts/clear", {
        method: "POST",
        body: JSON.stringify({ rule_id: ruleId, device_id: deviceId }),
      });
      // Reflect locally so re-render flips the badge without a round-trip
      // to /api/alerts/events first.
      for (const e of alertsCache) {
        if (e.rule_id === ruleId &&
            (e.device_id || "").toLowerCase() === deviceId.toLowerCase()) {
          e.cleared = 1;
        }
      }
      renderFilteredAlerts();
    } catch (err) {
      alert("Unlatch failed: " + err.message);
      clrBtn.disabled = false;
    }
  }
});

$("#alerts-unlatch-all")?.addEventListener("click", async () => {
  if (!confirm("Clear every active alarm latch?\n\nHistory is kept; rules can fire again on those devices.")) return;
  try {
    await api("/api/alerts/clear-all", { method: "POST" });
    for (const e of alertsCache) e.cleared = 1;
    renderFilteredAlerts();
  } catch (err) {
    alert("Unlatch failed: " + err.message);
  }
});

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
// `${rule_id}|${device_id}` pairs we recently popped, with the timestamp
// they were popped. Used to swallow bursts where the same alert fires
// every poll for a non-latching rule — without this an aggressive
// "fire on every match" rule would flood the screen. Latching rules
// can't re-fire server-side anyway, so this only matters for
// non-latching ones. 5-minute window keeps genuine re-arrivals
// visible while still cutting per-tick spam.
const _alertPoppedPairs = new Map();
const _ALERT_POP_REPEAT_MS = 5 * 60 * 1000;
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
  // Mission tab's "Mute audible alarms" toggle gates this — set
  // from setupMissionToggles() and read here without an additional
  // localStorage round-trip on every fire.
  if (window._missionMuteAlarms) return;
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
    const now = Date.now();
    if (alertsLastPoppedId === 0) {
      // First poll — seed dedup with what's already in the feed so we
      // don't burst-pop the entire alert history on page load.
      for (const e of events) _alertPoppedPairs.set(_alertPairKey(e), now);
    } else {
      const fresh = events.filter(e => e.id > alertsLastPoppedId);
      // Show oldest-first so the newest ends up on top of the stack.
      for (const e of fresh.slice().reverse()) {
        const key = _alertPairKey(e);
        const lastPopped = _alertPoppedPairs.get(key);
        if (lastPopped && (now - lastPopped) < _ALERT_POP_REPEAT_MS) continue;
        _alertPoppedPairs.set(key, now);
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
  // Latch defaults to ON for legacy rules (the DB column was added with
  // a `DEFAULT 1` migration) so an undefined value should read as latched.
  form.elements["latch"].checked = rule.latch == null ? true : !!rule.latch;
  // include_whitelist defaults OFF — historical behaviour was that the
  // whitelist muted every rule, and legacy rules carry that semantic.
  form.elements["include_whitelist"].checked = !!rule.include_whitelist;
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
    latch: fd.get("latch") === "on",
    include_whitelist: fd.get("include_whitelist") === "on",
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
  persistent_companion: "3/4 — seen at ≥3 distinct locations within the last 4 hours (BLE rotating MACs counted together)",
  co_arrival_transit: "2/5/120 — co-arrives within 120s of you at 2 of the last 5 location transitions",
  travel_time_companion: "60/2 — sighted for ≥60s while GPS speed ≥2 m/s (riding with you)",
  approach_vector: "8/30 — RSSI improved by ≥8 dB over 30s while you were stationary (closing in)",
  novel_location_chain: "2/24 — shows up at ≥2 places you only started visiting in the last 24h",
  mac_rotation_rate: "3/4 — ≥3 distinct MACs sharing one BLE signature in the last 4h (BLE only)",
  cross_kind_co_travel: "2/24 — pairs with a device of the other kind at ≥2 of your last-24h locations",
  arrival_after_gap: "30 — fire when this device shows up after ≥30 min away (0 = every sighting)",
  absence_gap: "30 — fire when this device hasn't been seen at the location for ≥30 min",
};
const MATCH_TYPE_DEFAULTS = {
  rssi_above: "-60",
  new_device: "300",
  cross_location: "5/2",
  persistent_companion: "3/4",
  co_arrival_transit: "2/5/120",
  travel_time_companion: "60/2",
  approach_vector: "8/30",
  novel_location_chain: "2/24",
  mac_rotation_rate: "3/4",
  cross_kind_co_travel: "2/24",
  arrival_after_gap: "30",
  absence_gap: "30",
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

async function refreshTempWhitelist() {
  const panel = $("#wl-temp-panel");
  const tbody = $("#wl-temp-table tbody");
  const navBtn = $("#nav-silenced");
  if (!panel || !tbody) return;
  try {
    const r = await api("/api/whitelist/temp");
    const entries = r.entries || [];
    if (!entries.length) {
      panel.hidden = true;
      if (navBtn) navBtn.hidden = true;
      tbody.innerHTML = "";
      // If the silenced section was active, fall back to whitelist.
      if (navBtn && navBtn.classList.contains("active")) {
        activateSettingsSection("whitelist");
      }
      return;
    }
    panel.hidden = false;
    if (navBtn) navBtn.hidden = false;
    tbody.innerHTML = entries.map(e => `
      <tr>
        <td>${escapeHtml(formatKindLabel(e.kind, null))}</td>
        <td class="mono">${escapeHtml(e.device_id)}</td>
        <td>${escapeHtml(e.note || "")}</td>
        <td class="mono">${escapeHtml(formatTime(e.created_at))}</td>
        <td class="row-actions">
          <button type="button" class="icon-btn wl-temp-promote" data-kind="${escapeAttr(e.kind)}" data-id="${escapeAttr(e.device_id)}" title="Promote to permanent whitelist" aria-label="Promote">★</button>
          <button type="button" class="icon-btn danger wl-temp-remove" data-kind="${escapeAttr(e.kind)}" data-id="${escapeAttr(e.device_id)}" title="Un-silence (future matches will fire alerts again)" aria-label="Remove">×</button>
        </td>
      </tr>
    `).join("");
    $$(".wl-temp-promote").forEach(b => b.addEventListener("click", async () => {
      await api("/api/whitelist/temp/promote", {
        method: "POST",
        body: JSON.stringify({
          kind: b.dataset.kind,
          device_id: b.dataset.id,
          note: "promoted from baseline",
        }),
      });
      await refreshWhitelist();
      await refreshTempWhitelist();
    }));
    $$(".wl-temp-remove").forEach(b => b.addEventListener("click", async () => {
      if (!confirm(`Un-silence ${b.dataset.kind} ${b.dataset.id}? Future alerts will fire again on this device.`)) return;
      await api("/api/whitelist/temp", {
        method: "DELETE",
        body: JSON.stringify({ kind: b.dataset.kind, device_id: b.dataset.id }),
      });
      await refreshTempWhitelist();
      await refreshDevices();
    }));
  } catch (e) {
    tbody.innerHTML = `<tr><td colspan="5" class="muted">error: ${escapeHtml(e.message)}</td></tr>`;
  }
}

$("#wl-temp-clear")?.addEventListener("click", async () => {
  if (!confirm("Clear every temporarily-silenced device?\n\nFuture matches will trigger alerts on them again until you re-baseline or whitelist.")) return;
  try {
    await api("/api/whitelist/temp", { method: "DELETE" });
    await refreshTempWhitelist();
    await refreshDevices();
  } catch (e) {
    alert("Clear failed: " + e.message);
  }
});

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
    tbody.innerHTML = `<tr><td colspan="11" class="muted">error: ${escapeHtml(e.message)}</td></tr>`;
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
  // Match summary — "12" for exact, "12 (+3 archived)" when there are
  // also preserved_devices rows hidden from the main view.
  const matches = e.match_count || 0;
  const archived = e.preserved_count || 0;
  const matchCell = matches === 0 && archived === 0
    ? `<span class="muted">no match yet</span>`
    : matches.toString() + (archived ? ` <span class="muted">(+${archived} archived)</span>` : "");
  const trackerBadge = e.tracker_type
    ? ` <span class="tracker-tag">${escapeHtml(e.tracker_type)}</span>`
    : "";
  // Compose the displayed device id: for OUI prefixes, also show the most
  // recent matching MAC so the user knows what's actually been hit.
  const idCell = e.sample_device_id && e.sample_device_id !== e.device_id
    ? `<span class="mono">${escapeHtml(e.device_id)}</span>${trackerBadge}`
      + ` <span class="muted">→ ${escapeHtml(e.sample_device_id)}</span>`
    : `<span class="mono">${escapeHtml(e.device_id)}</span>${trackerBadge}`;
  tr.innerHTML = `
    <td>${escapeHtml(formatKindLabel(e.kind, null))}</td>
    <td>${idCell}</td>
    <td>${escapeHtml(e.note || "")}</td>
    <td>${matchCell}</td>
    <td>${e.location_count ?? 0}</td>
    <td>${escapeHtml(e.vendor || "")}</td>
    <td>${escapeHtml(e.name || "")}</td>
    <td>${e.best_rssi != null ? e.best_rssi + " dBm" : ""}</td>
    <td class="mono">${escapeHtml(formatTime(e.last_seen))}</td>
    <td class="mono">${escapeHtml(formatTime(e.created_at))}</td>
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
    <td colspan="6" class="muted" style="text-align:center;">— editing —</td>
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
  inject("#map-baseline-icon",     ICON_BASELINE);
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

// ── Baseline scan ──────────────────────────────────────────────────
// Walks the user through a baseline of "everything nearby": bursts
// the WiFi/BT scanners harder than normal, then opens a modal letting
// the user promote individual entries to the permanent whitelist.
$("#map-baseline")?.addEventListener("click", async () => {
  if (!confirm(
    "Run a baseline scan?\n\n" +
    "Will run multiple WiFi scans and an extended Bluetooth sweep to " +
    "capture every nearby device. Use this somewhere safe (home, office) " +
    "where everything visible should count as known-good.\n\n" +
    "Takes ~30 seconds. Won't interrupt the ongoing background scans."
  )) return;

  // Drive both the progress phase and the result phase from one modal.
  openModal("Baseline scan", `
    <div class="baseline-progress">
      <p class="muted" id="baseline-stage">Starting…</p>
      <progress id="baseline-bar" value="0" max="1"></progress>
      <p class="muted"><span id="baseline-count">0</span> device(s) captured so far</p>
    </div>
  `);

  try {
    await api("/api/scan/baseline/start", { method: "POST" });
  } catch (e) {
    $("#modal-body").innerHTML = `<div class="muted">Failed to start: ${escapeHtml(e.message)}</div>`;
    return;
  }

  // Poll until ready or error. 250ms cadence keeps the bar lively.
  let ready = false;
  let lastErr = null;
  for (let i = 0; i < 600; i++) {
    await new Promise(r => setTimeout(r, 250));
    let st;
    try { st = await api("/api/scan/baseline/status"); }
    catch (e) { lastErr = e; continue; }
    if (st.error) {
      $("#modal-body").innerHTML = `<div class="muted">Scan failed: ${escapeHtml(st.error)}</div>`;
      return;
    }
    const bar = $("#baseline-bar");
    const stage = $("#baseline-stage");
    const count = $("#baseline-count");
    if (bar) {
      bar.max = Math.max(1, st.stage_total || 1);
      bar.value = st.stage_n || 0;
    }
    if (stage) {
      stage.textContent = st.stage_label || "Working…";
    }
    if (count) {
      count.textContent = String(st.device_count || 0);
    }
    if (st.ready) { ready = true; break; }
  }
  if (!ready) {
    $("#modal-body").innerHTML = `<div class="muted">Scan timed out${lastErr ? ": " + escapeHtml(lastErr.message) : ""}</div>`;
    return;
  }

  // Fetch and render the captured device list with checkboxes.
  let data;
  try {
    data = await api("/api/scan/baseline/result");
  } catch (e) {
    $("#modal-body").innerHTML = `<div class="muted">Couldn't fetch result: ${escapeHtml(e.message)}</div>`;
    return;
  }
  renderBaselineResult(data.devices || []);
});

function renderBaselineResult(devices) {
  $("#modal-title").textContent =
    `Baseline scan — ${devices.length} device${devices.length === 1 ? "" : "s"}`;
  if (!devices.length) {
    $("#modal-body").innerHTML = `<div class="muted">No devices captured above the configured RSSI floor.</div>`;
    return;
  }
  const rows = devices.map((d, i) => {
    const trackerBadge = d.tracker_type
      ? ` <span class="tracker-tag">${escapeHtml(d.tracker_type)}</span>`
      : "";
    // Baseline scan rows put address_type at the top of `d` (it's the
    // BluetoothDevice model coming straight from the scanner), not inside
    // a nested `.details` like the Devices-tab rows do. Passing `d` itself
    // works because formatKindLabel just reads `.address_type` off whatever
    // it gets handed.
    return `
      <tr>
        <td><input type="checkbox" class="baseline-pick" data-i="${i}" checked /></td>
        <td>${escapeHtml(formatKindLabel(d.kind, d))}</td>
        <td class="mono">${escapeHtml(d.device_id)}${trackerBadge}</td>
        <td>${escapeHtml(d.name || "")}</td>
        <td>${escapeHtml(d.vendor || "")}</td>
        <td>${d.rssi != null ? d.rssi + " dBm" : ""}</td>
      </tr>`;
  }).join("");
  $("#modal-body").innerHTML = `
    <p class="muted">
      Sorted by signal strength. <b>Checked</b> entries go to your permanent
      whitelist. <b>Unchecked</b> entries are silenced in the temporary
      whitelist — they won't trigger alerts but get wiped if you ever press
      "Delete all locations" on the Locations tab.
    </p>
    <div class="baseline-toolbar">
      <button type="button" id="baseline-select-all" class="secondary">Select all</button>
      <button type="button" id="baseline-select-none" class="secondary">Select none</button>
      <button type="button" id="baseline-only-trackers" class="secondary" title="Trackers only — AirTag, Tile, Samsung SmartTag">Only trackers</button>
      <span class="toolbar-spacer"></span>
      <button type="button" id="baseline-promote">Apply</button>
    </div>
    <div class="baseline-list">
      <table>
        <thead>
          <tr>
            <th>✓</th><th>Kind</th><th>Device ID</th>
            <th>Name / SSID</th><th>Vendor</th><th>RSSI</th>
          </tr>
        </thead>
        <tbody>${rows}</tbody>
      </table>
    </div>
  `;
  // Cache for the promote handler — checkboxes carry the index back to the
  // original device list.
  _baselineCache = devices;
  $("#baseline-select-all")?.addEventListener("click",
    () => $$(".baseline-pick").forEach(cb => cb.checked = true));
  $("#baseline-select-none")?.addEventListener("click",
    () => $$(".baseline-pick").forEach(cb => cb.checked = false));
  $("#baseline-only-trackers")?.addEventListener("click", () => {
    $$(".baseline-pick").forEach(cb => {
      const idx = Number(cb.dataset.i);
      cb.checked = !!_baselineCache[idx]?.tracker_type;
    });
  });
  $("#baseline-promote")?.addEventListener("click", baselinePromoteSelected);
}

let _baselineCache = [];

async function baselinePromoteSelected() {
  const picked = $$(".baseline-pick")
    .filter(cb => cb.checked)
    .map(cb => _baselineCache[Number(cb.dataset.i)])
    .filter(Boolean)
    .map(d => ({ kind: d.kind, device_id: d.device_id, note: "baseline scan" }));
  const btn = $("#baseline-promote");
  btn.disabled = true;
  try {
    const r = await api("/api/scan/baseline/promote", {
      method: "POST",
      body: JSON.stringify({ entries: picked }),
    });
    closeModal();
    const bits = [];
    if (r.added) bits.push(`${r.added} added to permanent whitelist`);
    if (r.silenced) bits.push(`${r.silenced} silenced (temporary)`);
    alert(bits.length ? bits.join(" · ") : "Nothing to apply.");
    await refreshWhitelist();
    await refreshTempWhitelist();
    await refreshDevices();
  } catch (e) {
    alert("Whitelist apply failed: " + e.message);
    btn.disabled = false;
  }
}

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

// ---------- modal ----------
function openModal(title, html) {
  const overlay = $("#modal-overlay");
  if (!overlay) return;
  $("#modal-title").textContent = title;
  $("#modal-body").innerHTML = html;
  overlay.hidden = false;
  // Focus the close button so Escape and tab navigation work immediately.
  $("#modal-close").focus();
}

function closeModal() {
  const overlay = $("#modal-overlay");
  if (overlay) overlay.hidden = true;
}

$("#modal-close")?.addEventListener("click", closeModal);
$("#modal-overlay")?.addEventListener("click", (ev) => {
  if (ev.target.id === "modal-overlay") closeModal();
});
document.addEventListener("keydown", (ev) => {
  if (ev.key === "Escape" && !$("#modal-overlay")?.hidden) closeModal();
});

// ---------- per-device timeline ----------
async function showDeviceTimeline(kind, deviceId) {
  openModal(`${kind} ${deviceId}`, `<div class="muted">Loading timeline…</div>`);
  let data;
  try {
    data = await api(`/api/devices/${encodeURIComponent(kind)}/${encodeURIComponent(deviceId)}/timeline`);
  } catch (e) {
    $("#modal-body").innerHTML = `<div class="muted">Error: ${escapeHtml(e.message)}</div>`;
    return;
  }
  $("#modal-body").innerHTML = renderDeviceTimeline(data);
}

function renderDeviceTimeline(data) {
  const det = data.details || {};
  const name = det.ssid || det.name || "";
  const vendor = det.vendor || "";
  const sparkline = renderRssiSparkline(data.observations || []);
  const locsRows = (data.locations || []).map(l => `
    <tr>
      <td>${l.id}</td>
      <td>${escapeHtml(l.label || "")}</td>
      <td>${l.best_rssi ?? ""}</td>
      <td>${l.last_rssi ?? ""}</td>
      <td>${l.seen_count ?? 0}</td>
      <td class="mono">${escapeHtml(formatTime(l.first_seen))}</td>
      <td class="mono">${escapeHtml(formatTime(l.last_seen))}</td>
    </tr>`).join("");
  return `
    <div class="timeline-meta">
      ${name ? `<div><strong>Name / SSID:</strong> ${escapeHtml(name)}</div>` : ""}
      ${vendor ? `<div><strong>Vendor:</strong> ${escapeHtml(vendor)}</div>` : ""}
      <div><strong>Total observations:</strong> ${data.total_observations ?? 0}</div>
      <div><strong>First seen:</strong> ${escapeHtml(formatTime(data.first_seen))}</div>
      <div><strong>Last seen:</strong> ${escapeHtml(formatTime(data.last_seen))}</div>
    </div>
    <h4 class="section-h">RSSI over time (last ${(data.observations || []).length} observations)</h4>
    ${sparkline}
    <h4 class="section-h">Locations seen at (${(data.locations || []).length})</h4>
    ${locsRows
      ? `<table class="timeline-locs"><thead><tr><th>ID</th><th>Label</th><th>Best</th><th>Last</th><th>Seen</th><th>First</th><th>Last seen</th></tr></thead><tbody>${locsRows}</tbody></table>`
      : `<div class="muted">No location attributions.</div>`}
  `;
}

function renderRssiSparkline(observations) {
  // Each observation is {seen_at, rssi}. Map seen_at → x linearly across
  // [0, W], rssi → y across [-100, -20] inverted. Output a single SVG
  // <polyline>. RSSI is dBm (negative); stronger = higher (closer to 0)
  // so we invert before scaling.
  if (!observations.length) {
    return `<div class="muted">No observations recorded.</div>`;
  }
  const W = 600, H = 120, PAD = 6;
  // Backend writes naive local-time ISO; parse as-is so the sparkline
  // axis labels match the wall clock.
  const times = observations.map(o => Date.parse(o.seen_at));
  const t0 = Math.min(...times), t1 = Math.max(...times);
  const dt = Math.max(1, t1 - t0);
  const RSSI_MIN = -100, RSSI_MAX = -20;  // typical band
  const yScale = (rssi) => {
    const r = Math.max(RSSI_MIN, Math.min(RSSI_MAX, rssi));
    return PAD + (RSSI_MAX - r) / (RSSI_MAX - RSSI_MIN) * (H - 2 * PAD);
  };
  const xScale = (t) => PAD + (t - t0) / dt * (W - 2 * PAD);
  const points = observations.map((o, i) => {
    const t = times[i] || t0;
    return `${xScale(t).toFixed(1)},${yScale(o.rssi).toFixed(1)}`;
  }).join(" ");
  // Reference lines at -50, -70, -90
  const refLines = [-50, -70, -90].map(r => {
    const y = yScale(r).toFixed(1);
    return `<line x1="${PAD}" y1="${y}" x2="${W - PAD}" y2="${y}" stroke="#2b313c" stroke-dasharray="2,3" stroke-width="1"/>
            <text x="${W - PAD - 2}" y="${(parseFloat(y) - 2).toFixed(1)}" fill="#52607a" font-size="9" text-anchor="end">${r} dBm</text>`;
  }).join("");
  const tFirst = new Date(t0).toLocaleString();
  const tLast = new Date(t1).toLocaleString();
  return `
    <div class="sparkline-wrap">
      <svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" class="sparkline">
        ${refLines}
        <polyline fill="none" stroke="var(--accent)" stroke-width="1.4" points="${points}" />
      </svg>
      <div class="sparkline-axis">
        <span>${escapeHtml(tFirst)}</span>
        <span>${escapeHtml(tLast)}</span>
      </div>
    </div>`;
}

// Human-friendly Kind label for table rows. The DB stores the kind as
// 'wifi' | 'bluetooth' | 'wifi_client' (kept stable for filtering and
// alert rules), but the Devices tab renders a richer string — BLE devices
// get split by address_type since the scanner is BLE-only and the
// public/random distinction is the most useful breakdown the user has.
function formatKindLabel(kind, details) {
  if (kind === "wifi") return "WiFi";
  if (kind === "wifi_client") return "WiFi client";
  if (kind === "bluetooth") {
    const at = (details && details.address_type) || "";
    if (at === "public") return "BLE (public)";
    if (at === "random") return "BLE (random)";
    return "BLE";
  }
  if (kind === "bluetooth_classic") {
    // Append the CoD major-class when the inquiry reported one — e.g.
    // "Bluetooth Classic (Audio/Video)" — so the table tells the user
    // what it is at a glance.
    const sub = details && details.device_class_label;
    return sub ? `Bluetooth Classic (${sub})` : "Bluetooth Classic";
  }
  return kind || "";
}

// ---------- mission tab ----------
// Polls /api/about for live DB stats + runtime info, lays them out in
// the Mission control panel, and wires the bulk-action buttons that
// used to live on the Locations tab. Confirm dialogs match the prior
// scope copy so nothing surprises the operator.
async function refreshMission() {
  try {
    const a = await api("/api/about");
    const setText = (id, v) => { const el = document.getElementById(id); if (el) el.textContent = v ?? "—"; };
    const s = a.stats || {};
    const d = s.devices || {};
    setText("mission-stat-uptime", formatUptime(a.runtime?.uptime_seconds));
    setText("mission-stat-active-loc", "—");
    setText("mission-stat-time", a.runtime?.server_time || "—");
    setText("mission-stat-locations", (s.locations ?? 0).toLocaleString());
    setText("mission-stat-devices",   (d.total ?? 0).toLocaleString());
    setText("mission-stat-wifi",      (d.wifi ?? 0).toLocaleString());
    setText("mission-stat-ble",       (d.bluetooth ?? 0).toLocaleString());
    setText("mission-stat-btc",       (d.bluetooth_classic ?? 0).toLocaleString());
    setText("mission-stat-clients",   (d.wifi_client ?? 0).toLocaleString());
    setText("mission-stat-obs",       (s.observations ?? 0).toLocaleString());
    setText("mission-stat-rules",     (s.alert_rules ?? 0).toLocaleString());
    setText("mission-stat-events",    (s.alert_events ?? 0).toLocaleString());
    setText("mission-stat-wl",        (s.whitelist ?? 0).toLocaleString());
    setText("mission-stat-dbsize",    formatBytes(s.db_size_bytes ?? 0));
    setText("mission-stat-dbpath",    s.db_path || "—");
  } catch (e) {
    const el = document.getElementById("mission-stat-state");
    if (el) el.textContent = "error";
  }
  // Pause state + per-scanner state tiles. The single "Scanning" tile
  // got split into four — Wi-Fi / BLE / Bluetooth Classic / Probe — so
  // the operator can see at a glance which scanners are mid-flight,
  // idle, paused, or disabled. Each tile takes one of:
  //   "scanning" — running this tick
  //   "idle"     — enabled but between scans
  //   "paused"   — orchestrator-wide pause
  //   "error"    — last attempt raised
  //   "disabled" — not configured / not enabled
  try {
    const [paused, gps, scanners, probe] = await Promise.all([
      api("/api/system/pause").catch(() => ({})),
      api("/api/gps").catch(() => ({})),
      api("/api/scanners/status").catch(() => ({})),
      api("/api/probe/status").catch(() => ({})),
    ]);
    const isPaused = !!paused.paused;
    const btn = document.getElementById("mission-pause");
    if (btn) btn.textContent = isPaused ? "Resume scanning" : "Pause scanning";
    const loc = document.getElementById("mission-stat-active-loc");
    if (loc) loc.textContent = gps.active_location_id != null
      ? `#${gps.active_location_id}` : "—";
    _renderScannerStateTile(
      "mission-stat-wifi-state", _classifyScannerState({
        paused: isPaused,
        running: scanners?.wifi?.running,
        last_error: scanners?.wifi?.last_error,
        enabled: !!scanners?.wifi?.configured_iface,
      }),
    );
    _renderScannerStateTile(
      "mission-stat-ble-state", _classifyScannerState({
        paused: isPaused,
        running: scanners?.bluetooth?.running,
        last_error: scanners?.bluetooth?.last_error,
        // BLE has no off switch — it tries whatever adapter is configured.
        enabled: true,
      }),
    );
    _renderScannerStateTile(
      "mission-stat-btc-state", _classifyScannerState({
        paused: isPaused,
        running: scanners?.bluetooth?.classic_running,
        last_error: null,
        enabled: !!scanners?.bluetooth?.classic_enabled,
      }),
    );
    // Probe scanner has different state semantics: it's a long-running
    // capture that either runs continuously or doesn't. "running" maps
    // to scanning; absence of an interface = disabled.
    _renderScannerStateTile(
      "mission-stat-probe-state", _classifyScannerState({
        paused: isPaused,
        running: probe?.running,
        last_error: probe?.last_error,
        enabled: !!probe?.interface,
      }),
    );
  } catch {}
  // Active + historical mission + live ticker — best-effort, each
  // independently caught so a failed call doesn't break the others.
  try { await _refreshMissionLifecycle(); } catch {}
  try { await _refreshMissionTicker(); } catch {}
}

function _classifyScannerState({ paused, running, last_error, enabled }) {
  if (!enabled) return "disabled";
  if (paused) return "paused";
  if (last_error) return "error";
  if (running) return "scanning";
  return "idle";
}

function _renderScannerStateTile(id, state) {
  const el = document.getElementById(id);
  if (!el) return;
  el.textContent = state;
  // Drop any prior state-* class so the next tick repaints cleanly.
  el.className = "mission-scanner-state state-" + state;
}

async function _refreshMissionLifecycle() {
  const activeBlock = document.getElementById("mission-active-block");
  const startForm = document.getElementById("mission-start-form");
  const historyBody = document.querySelector("#mission-history-table tbody");
  if (!activeBlock || !startForm || !historyBody) return;

  const [activeRes, listRes] = await Promise.all([
    api("/api/missions/active"),
    api("/api/missions?limit=50"),
  ]);
  const active = activeRes.mission || null;
  const missions = listRes.missions || [];

  if (active) {
    const dur = active.started_at
      ? formatUptime((Date.now() - Date.parse(active.started_at)) / 1000)
      : "—";
    activeBlock.innerHTML = `
      <div class="mission-active-row">
        <div>
          <div class="mission-active-name">${escapeHtml(active.name)}</div>
          <div class="muted small">
            started ${escapeHtml(formatTime(active.started_at))}
            · running ${escapeHtml(dur)}
            ${active.description ? ` · ${escapeHtml(active.description)}` : ""}
          </div>
        </div>
        <button id="mission-end" type="button" class="danger">End mission</button>
      </div>`;
    startForm.hidden = true;
    document.getElementById("mission-end")?.addEventListener("click", _endActiveMission);
  } else {
    activeBlock.innerHTML = `<p class="muted small">No mission in progress. Start one to snapshot the DB and tag the report.</p>`;
    startForm.hidden = false;
  }

  if (!missions.length) {
    historyBody.innerHTML = `<tr><td colspan="8" class="muted">No missions yet.</td></tr>`;
    return;
  }
  historyBody.innerHTML = missions.map(m => {
    const start = m.started_at;
    const end = m.ended_at;
    let durStr = "—";
    if (start && end) {
      durStr = formatUptime((Date.parse(end) - Date.parse(start)) / 1000);
    } else if (start && !end) {
      durStr = formatUptime((Date.now() - Date.parse(start)) / 1000) + " (active)";
    }
    const stats0 = m.stats_start || {};
    const stats1 = m.stats_end || {};
    const diff = (k, sub) => {
      const a = sub ? (stats0.devices || {})[sub] : stats0[k];
      const b = sub ? (stats1.devices || {})[sub] : stats1[k];
      if (a == null || b == null) return "—";
      const v = (b || 0) - (a || 0);
      return v >= 0 ? `+${v.toLocaleString()}` : v.toLocaleString();
    };
    return `
      <tr data-id="${m.id}">
        <td>${escapeHtml(m.name)}</td>
        <td class="mono">${escapeHtml(formatTime(start))}</td>
        <td class="mono">${escapeHtml(end ? formatTime(end) : "—")}</td>
        <td class="mono">${escapeHtml(durStr)}</td>
        <td class="mono">${diff("observations")}</td>
        <td class="mono">${diff(null, "total")}</td>
        <td class="mono">${diff("alert_events")}</td>
        <td>
          <button class="icon-btn mission-delete-row danger" data-id="${m.id}" title="Remove this mission record (data is unaffected)" aria-label="Delete">×</button>
        </td>
      </tr>`;
  }).join("");
  for (const btn of historyBody.querySelectorAll(".mission-delete-row")) {
    btn.addEventListener("click", async () => {
      if (!confirm("Remove this mission record?\n\nMission metadata only — devices, observations, and alert history are untouched.")) return;
      try {
        await api(`/api/missions/${btn.dataset.id}`, { method: "DELETE" });
        await _refreshMissionLifecycle();
      } catch (e) { alert("Could not delete: " + (e.message || e)); }
    });
  }
}

async function _refreshMissionTicker() {
  const setTicker = (id, text, title) => {
    const el = document.getElementById(id);
    if (!el) return;
    el.textContent = text;
    if (title) el.title = title;
  };
  // Latest alert
  try {
    const r = await api("/api/alerts/events?limit=1");
    const e = (r.events || [])[0];
    if (e) {
      setTicker("mission-ticker-alert",
        `${e.rule_name || "rule " + e.rule_id} · ${e.device_id}`,
        `${formatTime(e.triggered_at)} · ${e.device_kind}`);
    } else {
      setTicker("mission-ticker-alert", "no alerts yet", "");
    }
  } catch {}
  // Latest device (across kinds) — pulled from the common-devices endpoint
  // is overkill, so use the active location's devices when available.
  try {
    const gps = await api("/api/gps").catch(() => ({}));
    const lid = gps.active_location_id;
    if (lid != null) {
      const dr = await api(`/api/locations/${lid}/devices`);
      const devs = dr.devices || [];
      // Pick the most-recently-seen one.
      devs.sort((a, b) => (b.last_seen || "").localeCompare(a.last_seen || ""));
      const d = devs[0];
      if (d) {
        setTicker("mission-ticker-device",
          `${formatKindLabel(d.kind, d.details)} · ${d.device_id}`,
          `${formatTime(d.last_seen)} · loc #${lid}`);
      } else {
        setTicker("mission-ticker-device", "no devices at active loc", "");
      }
    } else {
      setTicker("mission-ticker-device", "no active location", "");
    }
  } catch {}
  // Latest location (most recently created/seen)
  try {
    const lr = await api("/api/locations");
    const locs = lr.locations || [];
    if (locs.length) {
      locs.sort((a, b) => (b.last_seen_at || "").localeCompare(a.last_seen_at || ""));
      const l = locs[0];
      setTicker("mission-ticker-location",
        `${l.label || ("Loc " + l.id)}`,
        `seen ${formatTime(l.last_seen_at)} · #${l.id}`);
    } else {
      setTicker("mission-ticker-location", "no locations yet", "");
    }
  } catch {}
}

async function _endActiveMission() {
  const active = (await api("/api/missions/active").catch(() => ({}))).mission;
  if (!active) return;
  if (!confirm(`End mission "${active.name}"?\n\nSnapshots the current DB stats and closes the record. New scans keep running.`)) return;
  try {
    await api(`/api/missions/${active.id}/end`, { method: "POST" });
    await refreshMission();
  } catch (e) {
    alert("Could not end mission: " + (e.message || e));
  }
}

async function _missionAction(btn, label, fn) {
  const status = document.getElementById("mission-action-status");
  const orig = btn.textContent;
  btn.disabled = true;
  btn.textContent = label;
  if (status) status.textContent = "";
  try {
    const msg = await fn();
    if (status && msg) status.textContent = msg;
  } catch (e) {
    if (status) status.textContent = "error: " + (e.message || String(e));
  } finally {
    btn.disabled = false;
    btn.textContent = orig;
    await refreshMission();
  }
}

document.getElementById("mission-refresh")?.addEventListener("click", refreshMission);

document.getElementById("mission-pause")?.addEventListener("click", async () => {
  try {
    const r = await api("/api/system/pause", { method: "POST" });
    const btn = document.getElementById("mission-pause");
    if (btn) btn.textContent = r.paused ? "Resume scanning" : "Pause scanning";
    const stateEl = document.getElementById("mission-stat-state");
    if (stateEl) stateEl.textContent = r.paused ? "paused" : "running";
  } catch (e) {
    alert("Could not toggle pause: " + (e.message || e));
  }
});

document.getElementById("mission-reset")?.addEventListener("click", (e) => {
  const ok = confirm(
    "Reset auto-clustered locations?\n\n" +
    "Wipes every auto-clustered sensor location and the devices/" +
    "observations attached to them. Drawn geofences are kept, and " +
    "whitelisted devices' history is archived to the preserved list. " +
    "The temporary whitelist is left intact.\n\nThis cannot be undone."
  );
  if (!ok) return;
  _missionAction(e.target, "Resetting…", async () => {
    const res = await api("/api/locations/reset", { method: "POST" });
    const d = res.deleted || {};
    return `Removed ${d.locations || 0} location(s), `
         + `${d.devices || 0} device row(s), `
         + `${d.observations || 0} observation(s)`
         + (d.preserved ? ` · archived ${d.preserved} whitelist row(s)` : "");
  });
});

document.getElementById("mission-delete-all")?.addEventListener("click", (e) => {
  const ok = confirm(
    "Delete ALL locations?\n\n" +
    "This permanently removes every sensor location AND every device " +
    "and observation tied to them. The active location will be re-opened " +
    "from the next GPS fix.\n\nThis cannot be undone."
  );
  if (!ok) return;
  _missionAction(e.target, "Deleting…", async () => {
    const res = await api("/api/locations", { method: "DELETE" });
    const d = res.deleted || {};
    return `Deleted ${d.locations || 0} location(s), `
         + `${d.devices || 0} device(s), `
         + `${d.observations || 0} observation(s)`;
  });
});

document.getElementById("mission-purge")?.addEventListener("click", (e) => {
  const ok = confirm(
    "Run retention purge now?\n\nApplies the observation/device " +
    "retention thresholds from Settings → Retention immediately. " +
    "Whitelisted devices are exempt from the device sweep."
  );
  if (!ok) return;
  _missionAction(e.target, "Purging…", async () => {
    const r = await api("/api/maintenance/purge", { method: "POST", body: "{}" });
    const rm = r.removed || {};
    return `Removed ${rm.observations || 0} observation(s) and ${rm.devices || 0} device row(s)`;
  });
});

document.getElementById("mission-clear-alerts")?.addEventListener("click", (e) => {
  if (!confirm("Clear the entire alert feed? Rules will stay.")) return;
  _missionAction(e.target, "Clearing…", async () => {
    const r = await api("/api/alerts/events", { method: "DELETE" });
    return `Cleared ${r.deleted || 0} alert event(s)`;
  });
});

document.getElementById("mission-unlatch-all")?.addEventListener("click", (e) => {
  if (!confirm("Clear every active alarm latch?\n\nHistory is kept; rules can re-fire on those devices.")) return;
  _missionAction(e.target, "Unlatching…", async () => {
    const r = await api("/api/alerts/clear-all", { method: "POST" });
    return `Unlatched ${r.cleared || 0} pair(s)`;
  });
});

document.getElementById("mission-vacuum")?.addEventListener("click", (e) => {
  if (!confirm("Vacuum the database?\n\nRewrites the SQLite file to reclaim free space after large deletes. Can take a few seconds on a big DB.")) return;
  _missionAction(e.target, "Vacuuming…", async () => {
    const r = await api("/api/maintenance/vacuum", { method: "POST" });
    return `Size ${formatBytes(r.size_before_bytes || 0)} → `
         + `${formatBytes(r.size_after_bytes || 0)} `
         + `(saved ${formatBytes(r.saved_bytes || 0)})`;
  });
});

document.getElementById("mission-integrity")?.addEventListener("click", (e) => {
  _missionAction(e.target, "Checking…", async () => {
    const r = await api("/api/maintenance/integrity", { method: "POST" });
    return r.ok
      ? "DB integrity: ok"
      : `DB integrity issues: ${(r.findings || []).join(" · ")}`;
  });
});

document.getElementById("mission-backup")?.addEventListener("click", () => {
  // Direct download — browser handles the file save. No JSON shape here,
  // just a Content-Disposition: attachment response.
  window.location.href = "/api/maintenance/db/export";
});

document.getElementById("mission-restore")?.addEventListener("click", () => {
  const ok = confirm(
    "Restore from backup?\n\n" +
    "Replaces the live SQLite file with the .db you pick. Strongly "
    + "recommend pausing scanning first and downloading a current "
    + "backup so you can roll back. After restore you'll need to "
    + "restart the app to reload the new DB.\n\nContinue?"
  );
  if (!ok) return;
  document.getElementById("mission-restore-file")?.click();
});

document.getElementById("mission-restore-file")?.addEventListener("change", async (ev) => {
  const file = ev.target.files && ev.target.files[0];
  if (!file) return;
  const fd = new FormData();
  fd.append("file", file);
  const btn = document.getElementById("mission-restore");
  await _missionAction(btn, "Restoring…", async () => {
    const resp = await fetch("/api/maintenance/db/import", {
      method: "POST", body: fd,
    });
    if (!resp.ok) throw new Error(`${resp.status} ${resp.statusText}`);
    const r = await resp.json();
    return `Restored ${formatBytes(r.bytes || 0)} — restart the app to pick up the new DB.`;
  });
  ev.target.value = "";
});

// Per-kind delete (Wi-Fi APs / BLE / BR/EDR / Wi-Fi clients).
document.querySelectorAll(".mission-kind-delete").forEach((btn) => {
  btn.addEventListener("click", (ev) => {
    const kind = btn.dataset.kind;
    if (!kind) return;
    const label = btn.firstChild ? btn.firstChild.textContent.trim() : `kind=${kind}`;
    if (!confirm(`${label}?\n\nThis removes every kind="${kind}" device row plus their observations. Whitelisted devices are archived to the preserved list. Cannot be undone.`)) return;
    _missionAction(btn, "Deleting…", async () => {
      const r = await api(`/api/maintenance/delete-kind?kind=${encodeURIComponent(kind)}`, {
        method: "POST",
      });
      return `Removed ${r.devices || 0} device row(s) and ${r.observations || 0} observation(s)`;
    });
  });
});

// Mission lifecycle: start form.
document.getElementById("mission-start-form")?.addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const fd = new FormData(ev.target);
  const payload = {
    name: (fd.get("name") || "").toString().trim(),
    description: (fd.get("description") || "").toString().trim() || null,
  };
  if (!payload.name) return;
  try {
    await api("/api/missions", { method: "POST", body: JSON.stringify(payload) });
    ev.target.reset();
    await refreshMission();
  } catch (e) {
    alert("Could not start mission: " + (e.message || e));
  }
});

// Mission report — reuses the existing PDF report pipeline. V2 idea:
// pass mission_id and have the renderer scope to the mission's time
// window with a cover page showing the stats diff.
document.getElementById("mission-report")?.addEventListener("click", async () => {
  const status = document.getElementById("mission-report-status");
  const prog = document.getElementById("mission-report-progress");
  const btn = document.getElementById("mission-report");
  if (!btn) return;
  const origText = btn.textContent;
  btn.disabled = true; btn.textContent = "Building…";
  if (status) status.textContent = "starting…";
  if (prog) { prog.hidden = false; prog.value = 0; }
  try {
    // Find the most-recent mission so the PDF picks up a cover page
    // with the stats diff. Falls back to the generic report endpoint
    // when there's no mission yet.
    let missionId = null;
    try {
      const r = await api("/api/missions?limit=1");
      missionId = (r.missions || [])[0]?.id ?? null;
    } catch {}
    const startUrl = missionId
      ? `/api/missions/${missionId}/report/start`
      : "/api/locations/report/start";
    await api(startUrl, { method: "POST" });
    // Poll until ready, mirroring the Locations-tab Generate-report flow.
    let last = null;
    while (true) {
      await new Promise(r => setTimeout(r, 700));
      const s = await api("/api/locations/report/status");
      last = s;
      if (status && s.message) status.textContent = s.message;
      if (prog && typeof s.progress === "number") prog.value = s.progress;
      if (s.done || s.error) break;
    }
    if (last && last.error) throw new Error(last.error);
    if (status) status.textContent = "ready — downloading…";
    window.location.href = "/api/locations/report/result.pdf";
  } catch (e) {
    if (status) status.textContent = "error: " + (e.message || String(e));
  } finally {
    btn.disabled = false; btn.textContent = origText;
    if (prog) prog.hidden = true;
  }
});

// Quick toggles — all client-side, persisted in localStorage. Mute and
// stealth are passive (just inhibit UI behaviour). Skip-recording calls
// the server flag so the orchestrator stops writing rows but keeps
// alerting on what it sees.
(function setupMissionToggles() {
  const mute = document.getElementById("mission-mute-alarms");
  const stealth = document.getElementById("mission-stealth");
  const skipRec = document.getElementById("mission-skip-recording");
  const read = (k) => { try { return localStorage.getItem(k); } catch { return null; } };
  const write = (k, v) => { try { localStorage.setItem(k, v ? "1" : "0"); } catch {} };

  if (mute) {
    mute.checked = read("muteAudibleAlarms") === "1";
    window._missionMuteAlarms = mute.checked;
    mute.addEventListener("change", () => {
      window._missionMuteAlarms = mute.checked;
      write("muteAudibleAlarms", mute.checked);
    });
  }
  if (stealth) {
    stealth.checked = read("stealthMode") === "1";
    document.body.classList.toggle("stealth-mode", stealth.checked);
    stealth.addEventListener("change", () => {
      document.body.classList.toggle("stealth-mode", stealth.checked);
      write("stealthMode", stealth.checked);
    });
  }
  if (skipRec) {
    // Initial value pulled from the server; falls back to off when the
    // endpoint isn't implemented yet.
    api("/api/system/recording").then(r => {
      skipRec.checked = !r.recording;
    }).catch(() => {});
    skipRec.addEventListener("change", async () => {
      try {
        await api("/api/system/recording", {
          method: "POST",
          body: JSON.stringify({ recording: !skipRec.checked }),
        });
      } catch (e) {
        alert("Could not toggle recording: " + (e.message || e));
        skipRec.checked = !skipRec.checked; // revert
      }
    });
  }
})();

// Mission journal — pure localStorage, per browser. Auto-saves on
// input with a short debounce, and shows the saved timestamp so the
// operator can tell when their last edit landed.
(function setupMissionJournal() {
  const ta = document.getElementById("mission-journal");
  const status = document.getElementById("mission-journal-status");
  const clearBtn = document.getElementById("mission-journal-clear");
  if (!ta) return;
  const KEY = "missionJournal";
  try { ta.value = localStorage.getItem(KEY) || ""; } catch {}
  let saveTimer = null;
  const flushSave = () => {
    try { localStorage.setItem(KEY, ta.value); } catch {}
    if (status) status.textContent = `saved ${new Date().toLocaleTimeString()}`;
  };
  ta.addEventListener("input", () => {
    clearTimeout(saveTimer);
    saveTimer = setTimeout(flushSave, 400);
  });
  clearBtn?.addEventListener("click", () => {
    if (!confirm("Clear all mission journal notes?")) return;
    ta.value = "";
    flushSave();
  });
})();

// ---------- about tab ----------
async function refreshAbout() {
  try {
    const a = await api("/api/about");
    const setText = (id, v) => { const el = $(`#${id}`); if (el) el.textContent = v ?? "—"; };

    setText("about-app", a.app || "Gjallarhorn");
    setText("about-tagline", a.tagline || "");

    // Build block — branch / commit / subject / dirty / remote.
    const b = a.build || {};
    setText("about-branch", b.branch || "—");
    const cur = b.current || {};
    setText("about-commit", cur.short || cur.sha || "—");
    setText("about-subject", cur.subject || "—");
    setText("about-dirty", b.dirty ? "uncommitted changes" : "clean");
    setText("about-remote", b.remote_url || "—");

    // Runtime.
    setText("about-uptime", formatUptime(a.runtime?.uptime_seconds));
    setText("about-server-time", a.runtime?.server_time || "—");
    setText("about-python", a.platform?.python || "—");
    setText("about-os", `${a.platform?.system || ""} ${a.platform?.release || ""}`.trim() || "—");

    // DB stats.
    const s = a.stats || {};
    const d = s.devices || {};
    setText("about-stat-locations", (s.locations ?? 0).toLocaleString());
    setText("about-stat-devices",   (d.total ?? 0).toLocaleString());
    setText("about-stat-wifi",      (d.wifi ?? 0).toLocaleString());
    setText("about-stat-ble",       (d.bluetooth ?? 0).toLocaleString());
    setText("about-stat-btc",       (d.bluetooth_classic ?? 0).toLocaleString());
    setText("about-stat-clients",   (d.wifi_client ?? 0).toLocaleString());
    setText("about-stat-obs",       (s.observations ?? 0).toLocaleString());
    setText("about-stat-rules",     (s.alert_rules ?? 0).toLocaleString());
    setText("about-stat-events",    (s.alert_events ?? 0).toLocaleString());
    setText("about-stat-wl",        (s.whitelist ?? 0).toLocaleString());
    setText("about-db-path",        s.db_path || "—");
    setText("about-db-size",        formatBytes(s.db_size_bytes ?? 0));
  } catch (e) {
    const tagline = $("#about-tagline");
    if (tagline) tagline.textContent = "Could not load /api/about — " + (e.message || e);
  }
}

function formatUptime(seconds) {
  if (!Number.isFinite(seconds)) return "—";
  const s = Math.floor(seconds);
  const d = Math.floor(s / 86400);
  const h = Math.floor((s % 86400) / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = s % 60;
  const parts = [];
  if (d) parts.push(`${d}d`);
  if (h || d) parts.push(`${h}h`);
  if (m || h || d) parts.push(`${m}m`);
  parts.push(`${sec}s`);
  return parts.join(" ");
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
  refreshScannerStatus();
  setInterval(refreshScannerStatus, 3000);
  // Header clock — local time, ticks every second.
  const tickClock = () => {
    const el = $("#clock-status");
    if (!el) return;
    const d = new Date();
    const pad = (n) => String(n).padStart(2, "0");
    el.textContent = `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
  };
  tickClock();
  setInterval(tickClock, 1000);
  setInterval(tickProbeRelativeTimes, 1000);
  refreshPauseStatus();
  setInterval(refreshPauseStatus, 15000);
  setupMapToggleIcons();
  refreshWhitelist();
  refreshTempWhitelist();
  startLogsPolling();
  setInterval(pollGps, 1500);
  setInterval(refreshLocationMarkers, 5000);
  setInterval(refreshOuiStatus, 10000);
  setInterval(pollAlertsBadge, 4000);
})();
