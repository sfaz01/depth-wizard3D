// DepthWizard app controller: wires the viewer, panels, gauge, copilot and dialogs.

import { api, waitForJob } from './api.js';
import { Viewer, SEVERITY_COLORS } from './viewer.js';
import { sunPosition, sceneDate } from './sun.js';
import { NewSceneDialog } from './newscene.js';

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];
const fmt = (v, d = 0) => Number(v).toLocaleString(undefined, { minimumFractionDigits: d, maximumFractionDigits: d });
const area = (m2) => (m2 >= 10000 ? `${fmt(m2 / 10000, 2)}<small>ha</small>` : `${fmt(m2)}<small>m²</small>`);
const esc = (s) => String(s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const rgb = (c) => `rgb(${c.join(',')})`;

const state = {
  jobId: null,
  result: null,
  level: null,
  source: 'lowest',
  point: null,
  report: null,
  zones: [],
  selected: null,
  month: 6,
  hour: 12,
};

const viewer = new Viewer($('#viewport'));
let toastTimer = null;

function toast(msg, ms = 4200) {
  const t = $('#toast');
  t.textContent = msg;
  t.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { t.hidden = true; }, ms);
}

// ------------------------------------------------------------------ scene loading

async function loadScene(jobId) {
  const result = await api.result(jobId);
  state.jobId = result.job_id;
  state.result = result;
  state.level = null;
  state.report = null;
  state.point = null;
  state.zones = [];
  state.selected = null;
  await viewer.load(result, api.binary);
  try { history.replaceState(null, '', `?scene=${encodeURIComponent(state.jobId)}`); } catch (_) { /* file:// */ }
  renderHeader();
  renderOverview();
  renderBuildings();
  setupGauge();
  renderFloodReport();
  renderExport();
  $('#card').hidden = true;
  $('#solar-out').innerHTML = '';
  applySun();
  chatSystem(`Loaded ${result.manifest.scene?.name || state.jobId}.`);
}

function renderHeader() {
  const m = state.result.manifest;
  const sc = m.scene || {};
  $('#scene-name').textContent = sc.name || state.jobId;
  const bits = [];
  if (sc.lat != null) bits.push(`${sc.lat.toFixed(4)}, ${sc.lon.toFixed(4)}`);
  bits.push(`${fmt(m.width * m.gsd_m)} × ${fmt(m.height * m.gsd_m)} m`);
  bits.push(`${fmt(m.gsd_m, 2)} m per pixel`);
  $('#scene-meta').textContent = bits.join(', ');
  document.title = `${sc.name || 'Scene'} - DepthWizard`;
}

function renderOverview() {
  const { analysis, manifest } = state.result;
  const b = analysis?.buildings || {};
  const t = analysis?.terrain || {};
  $('#ov-stats').innerHTML = `
    <div><dd>${fmt(b.count || 0)}</dd><dt>Buildings</dt></div>
    <div><dd>${fmt(b.tallest_m || 0)}<small>m</small></dd><dt>Tallest</dt></div>
    <div><dd>≈${fmt(b.est_occupants || 0)}</dd><dt>Estimated occupants</dt></div>
    <div><dd>${area(b.total_footprint_m2 || 0)}</dd><dt>Built footprint</dt></div>
    <div><dd>${fmt(analysis?.ground_min_m ?? 0, 1)}–${fmt(analysis?.ground_max_m ?? 0, 0)}<small>m</small></dd><dt>Ground elevation</dt></div>
    <div><dd>${fmt((t.hazard_gt30 || 0) * 100, 1)}<small>%</small></dd><dt>Terrain steeper than 30°</dt></div>`;

  const scale = manifest.scale || {};
  $('#scale-note').textContent = scale.note || '';
  const v = $('#scale-validation');
  if (scale.method === 'osm' && scale.osm) {
    const ho = scale.osm.holdout;
    const cls = ho.median_abs_pct < 30 ? 'good' : 'warn';
    v.innerHTML = `<div class="validation">
      <span>Checked against <b>${ho.n}</b> OpenStreetMap buildings not used for fitting</span>
      <span><strong>±${fmt(ho.mae_m, 1)} m</strong> mean height error <span class="badge ${cls}">${fmt(ho.median_abs_pct)}% median</span></span>
      <span class="note">Correlation r = ${fmt(scale.osm.pearson_r, 2)}. Monocular depth ranks heights well; absolute error grows on skyscrapers.</span>
    </div>`;
  } else if (scale.method === 'ground_truth') {
    v.innerHTML = `<div class="validation"><span class="badge good">Exact synthetic heights</span></div>`;
  } else {
    v.innerHTML = `<div class="validation"><span class="badge warn">Assumed scale</span><span class="note">Heights are relative until calibrated.</span></div>`;
  }
  $('#in-height-ref').value = Math.round(scale.height_ref_m || 35);
  $('#in-gsd').value = (scale.gsd_m || manifest.gsd_m).toFixed(2);
  $('#in-relief').value = Math.round(scale.terrain_relief_m ?? 8);
  const kind = manifest.scene?.kind;
  $('#field-gsd').hidden = kind === 'map' || kind === 'geotiff';
  $('#field-relief').hidden = kind === 'map' || kind === 'geotiff';
  $('details.calib').hidden = kind === 'demo';
}

// ------------------------------------------------------------------ flood

function floodRange() {
  const a = state.result.analysis || {};
  const lo = a.flood_datum_m ?? a.ground_min_m ?? viewer.minElev;
  const levels = a.suggested_flood_levels_m || {};
  const span = Math.max(4, Math.min(40, (levels.max ?? lo + 10) - lo));
  return { lo, span: Math.ceil(span) };
}

function setupGauge() {
  const { lo, span } = floodRange();
  viewer.datum = lo;
  const ticks = $('#gauge-ticks');
  ticks.innerHTML = '';
  const step = span <= 8 ? 1 : span <= 20 ? 2 : 5;
  for (let m = 0; m <= span; m += 0.5) {
    const y = 100 - (m / span) * 100;
    const isMajor = Math.abs(m % step) < 1e-6;
    const t = document.createElement('div');
    t.className = 't ' + (isMajor ? 'm' : 'h');
    t.style.top = `${y}%`;
    ticks.appendChild(t);
    if (isMajor && m > 0 && m < span) {
      const l = document.createElement('div');
      l.className = 'lbl';
      l.style.top = `${y}%`;
      l.textContent = m;
      ticks.appendChild(l);
    }
  }
  // Red/black "E" blocks like a real river staff gauge.
  for (let m = 0; m < span; m += 1) {
    const b = document.createElement('div');
    b.className = 'e-block';
    b.style.top = `${100 - ((m + 1) / span) * 100}%`;
    b.style.height = `${(0.5 / span) * 100}%`;
    b.style.background = m % 2 ? '#0b1320' : '#c8102e';
    ticks.appendChild(b);
  }
  $('#gauge-staff').setAttribute('aria-valuemax', span);
  updateGauge();
}

function updateGauge() {
  const { lo, span } = floodRange();
  const rise = state.level == null ? 0 : state.level - lo;
  const pct = Math.max(0, Math.min(100, (rise / span) * 100));
  $('#gauge-water').style.height = `${pct}%`;
  $('#gauge-handle').style.bottom = `calc(${pct}% - 2px)`;
  $('#gauge-rise').textContent = `${rise >= 0 ? '+' : ''}${fmt(rise, 1)} m`;
  $('#gauge-abs').textContent = state.level == null ? 'dry' : `${fmt(state.level, 1)} m elev.`;
  $('#gauge-staff').setAttribute('aria-valuenow', rise.toFixed(1));
}

function floodSource() {
  return state.source === 'point' ? (state.point || null) : state.source;
}

let reportTimer = null;
function setLevel(level, { animate = false } = {}) {
  state.level = level;
  const src = floodSource();
  if (state.source === 'point' && !src) {
    $('#breach-hint').hidden = false;
    updateGauge();
    return;
  }
  if (animate) {
    viewer.animateFloodTo(level, src, (lvl, done) => {
      state.level = lvl;
      updateGauge();
      if (done) { state.level = level; requestReport(0); }
    });
  } else {
    viewer.setFlood(level, src);
    updateGauge();
    requestReport(160);
  }
}

function requestReport(delay) {
  clearTimeout(reportTimer);
  reportTimer = setTimeout(fetchReport, delay);
}

async function fetchReport() {
  if (state.level == null || !state.jobId) return;
  const body = { level_m: state.level, source: state.source };
  if (state.source === 'point') {
    if (!state.point) return;
    body.point = state.point;
  }
  try {
    state.report = await api.flood(state.jobId, body);
    renderFloodReport();
  } catch (e) {
    toast(`Flood report failed: ${e.message}`);
  }
}

function renderFloodReport() {
  const r = state.report;
  $('#flood-report').hidden = !r;
  if (!r) { viewer.setAffected([]); return; }
  const lo = floodRange().lo;
  $('#flood-stats').innerHTML = `
    <div><dd>${area(r.flooded_area_m2)}</dd><dt>Under water (${fmt(r.flooded_fraction * 100, 1)}%)</dt></div>
    <div><dd>${fmt(r.max_depth_m, 1)}<small>m</small></dd><dt>Deepest water</dt></div>
    <div><dd>${fmt(r.buildings_affected)}</dd><dt>Buildings reached</dt></div>
    <div><dd>≈${fmt(r.est_people_affected)}</dd><dt>People on flooded floors</dt></div>
    <div><dd>${fmt(r.level_m - lo, 1)}<small>m</small></dd><dt>${state.result.analysis?.water_level_m != null ? 'Above normal water level' : 'Above lowest ground'}</dt></div>
    <div><dd>${fmt(r.water_volume_m3 / 1000, 0)}<small>k m³</small></dd><dt>Water volume</dt></div>`;
  const sev = r.severity_counts;
  const total = Math.max(1, r.buildings_affected);
  const names = { minor: 'Minor, under 0.3 m', moderate: 'Moderate, to 1 m', severe: 'Severe, to 2.5 m', critical: 'Critical, over 2.5 m' };
  $('#sevbar').innerHTML = Object.keys(names).map(k => `<span style="width:${(sev[k] / total) * 100}%;background:${rgb(SEVERITY_COLORS[k])}"></span>`).join('');
  $('#sev-legend').innerHTML = Object.entries(names).map(([k, n]) => `<li><i style="background:${rgb(SEVERITY_COLORS[k])}"></i>${n}<b>${sev[k]}</b></li>`).join('');
  $('#affected-list').innerHTML = r.affected.slice(0, 12).map(a => `
    <li data-id="${a.id}" data-row="${a.row}" data-col="${a.col}">
      <span class="sw" style="background:${rgb(SEVERITY_COLORS[a.severity])}"></span>
      Building ${a.id}${a.submerged ? ', fully submerged' : ''}
      <span class="num">${fmt(a.water_depth_m, 1)} m</span>
    </li>`).join('') || '<li class="note">No buildings reached yet.</li>';
  viewer.setAffected(Object.entries(r.affected_severity || {}).map(([id, severity]) => ({ id: +id, severity })));
}

async function findSafeZones() {
  if (state.level == null) { toast('Raise the water first, then find evacuation points.'); return; }
  const body = { level_m: state.level, source: state.source };
  if (state.source === 'point' && state.point) body.point = state.point;
  const res = await api.safeZones(state.jobId, body);
  showZones(res.zones);
}

function showZones(zones) {
  state.zones = zones;
  viewer.clearMarkers();
  if (state.point) viewer.addMarker({ row: state.point[0], col: state.point[1], label: 'Breach', color: 0xe4572e });
  zones.forEach((z, i) => {
    const label = z.kind === 'vertical_refuge' ? `Refuge ${i + 1}` : `Assembly ${i + 1}`;
    viewer.addMarker({ row: z.row, col: z.col, label, color: z.kind === 'vertical_refuge' ? 0xa7f3d0 : 0x3cc48a,
      elev: z.kind === 'vertical_refuge' ? z.elev_m : null });
  });
  $('#safe-list').innerHTML = zones.map((z, i) => `
    <li data-row="${z.row}" data-col="${z.col}">
      <span class="sw" style="background:${z.kind === 'vertical_refuge' ? '#a7f3d0' : '#3cc48a'}"></span>
      ${z.kind === 'vertical_refuge' ? `Refuge ${i + 1}, rooftop of building ${z.building_id}` : `Assembly ${i + 1}, ${fmt(z.area_m2)} m² open ground`}
      <span class="num">+${fmt(z.freeboard_m, 1)} m</span>
    </li>`).join('') || '<li class="note">No dry open ground is large enough. Use rooftops.</li>';
}

function clearFlood() {
  state.level = null;
  state.report = null;
  viewer.clearFlood();
  viewer.clearMarkers();
  state.zones = [];
  $('#safe-list').innerHTML = '';
  renderFloodReport();
  updateGauge();
}

function setSource(src) {
  state.source = src;
  $$('#seg-source button').forEach(b => b.classList.toggle('on', b.dataset.source === src));
  $('#breach-hint').hidden = src !== 'point' || !!state.point;
  if (state.level != null) setLevel(state.level);
}

function bindGauge() {
  const staff = $('#gauge-staff');
  const fromEvent = (e) => {
    const r = staff.getBoundingClientRect();
    const t = 1 - (e.clientY - r.top) / r.height;
    const { lo, span } = floodRange();
    return lo + Math.max(0, Math.min(1, t)) * span;
  };
  let dragging = false;
  staff.addEventListener('pointerdown', (e) => { dragging = true; staff.setPointerCapture(e.pointerId); setLevel(fromEvent(e)); });
  staff.addEventListener('pointermove', (e) => { if (dragging) setLevel(fromEvent(e)); });
  staff.addEventListener('pointerup', () => { dragging = false; });
  staff.addEventListener('keydown', (e) => {
    const { lo } = floodRange();
    const cur = state.level ?? lo;
    if (e.key === 'ArrowUp') { setLevel(cur + 0.1); e.preventDefault(); }
    if (e.key === 'ArrowDown') { setLevel(Math.max(lo, cur - 0.1)); e.preventDefault(); }
  });
}

// ------------------------------------------------------------------ buildings

function renderBuildings() {
  const list = state.result.buildings;
  const src = state.result.analysis?.footprint_source;
  $('#bld-source').textContent = src === 'osm_footprints'
    ? 'Footprints from OpenStreetMap, heights measured from the 3D reconstruction.'
    : 'Footprints segmented from the 3D reconstruction.';
  const maxH = Math.max(10, Math.ceil(list[0]?.height_m || 10));
  $('#in-minh').max = maxH;
  $('#in-minh').value = 0;
  $('#out-minh').textContent = 'off';
  renderBuildingList(list.slice(0, 60));
}

function renderBuildingList(items) {
  $('#bld-list').innerHTML = items.map(b => `
    <li data-id="${b.id}" data-row="${b.row}" data-col="${b.col}">
      <span class="sw" style="background:#ffb000"></span>Building ${b.id}
      <span class="note">${b.floors} fl, ${fmt(b.footprint_m2)} m²</span>
      <span class="num">${fmt(b.height_m, 1)} m</span>
    </li>`).join('');
}

function selectBuilding(id, fly = false) {
  const b = state.result.buildings.find(x => x.id === id);
  if (!b) return;
  state.selected = id;
  viewer.setSelected(id);
  if (fly) viewer.flyToCell(b.row, b.col);
  const aff = state.report?.affected?.find(a => a.id === id);
  const solar = state.solar?.buildings?.find(s => s.id === id);
  const card = $('#card');
  card.innerHTML = `
    <header><h3>Building ${b.id}</h3><span class="note">${b.osm_id ? `OSM way ${b.osm_id}` : 'Detected'}</span>
      <button class="icon-btn" id="card-close" aria-label="Close">×</button></header>
    <dl class="stats">
      <div><dd>${fmt(b.height_m, 1)}<small>m</small></dd><dt>Height${b.ref_height_m ? `, OSM says ${fmt(b.ref_height_m)} m` : ''}</dt></div>
      <div><dd>${b.floors}</dd><dt>Floors (est.)</dt></div>
      <div><dd>${fmt(b.footprint_m2)}<small>m²</small></dd><dt>Footprint</dt></div>
      <div><dd>≈${fmt(b.est_occupants)}</dd><dt>Occupants</dt></div>
      <div><dd>${fmt(b.base_elev_m, 1)}<small>m</small></dd><dt>Ground level</dt></div>
      <div><dd>${aff ? fmt(aff.water_depth_m, 1) + '<small>m</small>' : solar ? fmt(solar.pv_kwh_yr / 1000, 1) + '<small>MWh</small>' : fmt(b.volume_m3 / 1000, 1) + '<small>k m³</small>'}</dd>
        <dt>${aff ? `Water at door, ${aff.severity}` : solar ? 'Solar per year' : 'Volume'}</dt></div>
    </dl>
    <div class="row">
      <button class="btn" id="card-fly">Fly here</button>
      ${state.result.manifest.scene?.kind !== 'demo' ? `<label class="field" style="flex-direction:row;align-items:center;gap:6px">
        <input type="number" id="card-true" min="2" max="900" step="0.5" style="width:84px" placeholder="${fmt(b.height_m)}">
        <button class="btn" id="card-match">Match this height</button></label>` : ''}
    </div>`;
  card.hidden = false;
  $('#card-close').onclick = () => { card.hidden = true; state.selected = null; viewer.setSelected(null); };
  $('#card-fly').onclick = () => viewer.flyToCell(b.row, b.col);
  const match = $('#card-match');
  if (match) match.onclick = () => calibrateFromBuilding(b, parseFloat($('#card-true').value));
}

async function calibrateFromBuilding(b, trueHeight) {
  if (!trueHeight || trueHeight <= 0) { toast('Enter the real height of this building in metres.'); return; }
  const cur = state.result.manifest.scale?.height_ref_m || 35;
  const next = cur * trueHeight / Math.max(0.5, b.height_m);
  await rescale({ height_ref_m: Math.round(next * 10) / 10 });
  toast(`Rescaled so building ${b.id} is ${trueHeight} m tall.`);
}

async function rescale(body) {
  showProgress('Rebuilding with new scale', 'Recomputing heights and analytics', 0.6);
  try {
    await api.rescale(state.jobId, body);
    await loadScene(state.jobId);
  } catch (e) {
    toast(`Rescale failed: ${e.message}`);
  } finally {
    hideProgress();
  }
}

// ------------------------------------------------------------------ sun & solar

function applySun() {
  const sc = state.result?.manifest?.scene || {};
  const lat = sc.lat ?? 20, lon = sc.lon ?? 78;
  const p = sunPosition(lat, lon, sceneDate(lon, state.month, state.hour));
  viewer.setSun(p.azimuth, p.elevation);
  const h = Math.floor(state.hour), mm = Math.round((state.hour - h) * 60);
  $('#out-hour').textContent = `${String(h).padStart(2, '0')}:${String(mm).padStart(2, '0')}`;
  $('#sun-readout').textContent = p.elevation > 0
    ? `Sun ${fmt(p.elevation)}° up, bearing ${fmt(p.azimuth)}°`
    : 'Sun below the horizon';
  $('#in-hour').value = state.hour;
  $('#in-month').value = state.month;
}

let dayTimer = null;
function playDay() {
  if (dayTimer) { cancelAnimationFrame(dayTimer); dayTimer = null; $('#btn-day').textContent = 'Play the day'; return; }
  $('#btn-day').textContent = 'Stop';
  let last = performance.now();
  state.hour = 5.5;
  const step = (now) => {
    state.hour += (now - last) / 1000 * 1.2; // 1.2 hours per second
    last = now;
    if (state.hour > 19.5) state.hour = 5.5;
    applySun();
    dayTimer = requestAnimationFrame(step);
  };
  dayTimer = requestAnimationFrame(step);
}

async function runSolar() {
  const btn = $('#btn-solar');
  btn.disabled = true;
  btn.textContent = 'Tracing shadows through the year…';
  try {
    state.solar = await api.solar(state.jobId);
    renderSolar();
  } catch (e) {
    toast(`Solar estimate failed: ${e.message}`);
  } finally {
    btn.disabled = false;
    btn.textContent = 'Estimate rooftop solar';
  }
}

function renderSolar() {
  const s = state.solar;
  if (!s) return;
  const top = s.buildings.slice(0, 8);
  $('#solar-out').innerHTML = `
    <dl class="stats">
      <div><dd>${fmt(s.total_kwh_yr / 1000)}<small>MWh/yr</small></dd><dt>Rooftop potential</dt></div>
      <div><dd>≈${fmt(s.households_powered)}</dd><dt>Households powered</dt></div>
      <div><dd>${fmt(s.scene_mean_kwh_m2_yr)}<small>kWh/m²</small></dd><dt>Mean sunlight per year</dt></div>
      <div><dd>${fmt(s.buildings.reduce((a, b) => a + b.pv_kwp, 0))}<small>kWp</small></dd><dt>Installable capacity</dt></div>
    </dl>
    <h3>Best roofs</h3>
    <ol class="list">${top.map(b => `<li data-id="${b.id}"><span class="sw" style="background:#ffb000"></span>Building ${b.id}<span class="num">${fmt(b.pv_kwh_yr / 1000, 1)} MWh</span></li>`).join('')}</ol>
    <p class="note">Assumes ${s.assumptions.usable_roof_fraction * 100}% of each roof usable, ${s.assumptions.panel_efficiency * 100}% panels, ${s.assumptions.cloud_derating * 100}% of clear-sky sunlight.</p>`;
  viewer.setHighlights(s.buildings.slice(0, 25).map(b => b.id));
}

// ------------------------------------------------------------------ export

async function renderExport() {
  const id = state.jobId;
  $('#dl-glb').href = `/api/jobs/${id}/model.glb`;
  $('#dl-glb').download = `${id}.glb`;
  $('#dl-stl').href = `/api/jobs/${id}/model.stl`;
  $('#dl-geojson').href = `/api/jobs/${id}/buildings.geojson`;
  $('#dl-geojson').download = `${id}-buildings.geojson`;
  let base = location.origin;
  try {
    const info = await api.serverInfo();
    if ((location.hostname === 'localhost' || location.hostname === '127.0.0.1') && info.urls.length) base = info.urls[0];
  } catch (_) { /* keep origin */ }
  const url = `${base}/ar.html?scene=${encodeURIComponent(id)}`;
  $('#ar-link').href = url;
  $('#ar-link').textContent = url;
  const qrBox = $('#qr');
  qrBox.innerHTML = '';
  if (window.qrcode) {
    const qr = window.qrcode(0, 'M');
    qr.addData(url);
    qr.make();
    qrBox.innerHTML = qr.createSvgTag({ cellSize: 4, margin: 0 });
  }
}

function saveScreenshot() {
  const a = document.createElement('a');
  a.href = viewer.screenshot();
  a.download = `${state.jobId}.png`;
  a.click();
}

function openBrief() {
  try {
    const c = viewer.renderer.domElement;
    viewer.renderer.render(viewer.scene, viewer.camera);
    sessionStorage.setItem('dw-brief-shot', c.toDataURL('image/jpeg', 0.82));
  } catch (_) { /* storage full or blocked - brief renders without the snapshot */ }
  const p = new URLSearchParams({ scene: state.jobId, source: state.source });
  if (state.level != null) p.set('level', state.level.toFixed(2));
  if (state.point) p.set('point', state.point.join(','));
  window.open(`brief.html?${p}`, '_blank');
}

// ------------------------------------------------------------------ copilot

function chat(role, html) {
  const el = document.createElement('div');
  el.className = `msg ${role}`;
  el.innerHTML = html;
  $('#chat').appendChild(el);
  $('#chat').scrollTop = $('#chat').scrollHeight;
  return el;
}
const chatSystem = (text) => chat('system', esc(text));

async function ask(message) {
  if (!state.jobId || !message.trim()) return;
  chat('user', esc(message));
  const pending = chat('bot thinking', 'Working it out…');
  try {
    const res = await api.copilot(state.jobId, message, {
      flood_level_m: state.level, flood_source: state.source === 'point' ? 'lowest' : state.source,
    });
    pending.classList.remove('thinking');
    pending.innerHTML = esc(res.reply);
    $('#engine').textContent = res.engine === 'rules' ? 'Offline rules' : res.engine.replace('llm:', '');
    for (const a of res.actions) applyAction(a);
  } catch (e) {
    pending.classList.remove('thinking');
    pending.textContent = `Could not answer: ${e.message}`;
  }
}

function updateInsets() {
  if (window.innerWidth <= 820) { viewer.setInsets(0, 0); return; }
  const drawerOpen = !$('#drawer').classList.contains('closed');
  const left = drawerOpen ? $('#drawer').getBoundingClientRect().right : $('#rail').getBoundingClientRect().right;
  const right = $('#copilot').hidden ? 0 : window.innerWidth - $('#copilot').getBoundingClientRect().left;
  viewer.setInsets(left, right);
}

function switchTab(name) {
  $$('.rail-btn').forEach(b => b.classList.toggle('active', b.dataset.tab === name));
  $$('.tab').forEach(t => t.classList.toggle('active', t.dataset.tab === name));
  $('#drawer').classList.add('mobile-open');
  $('#gauge').hidden = !(name === 'flood' || state.level != null);
  updateInsets();
}

function applyAction(a) {
  switch (a.type) {
    case 'flood':
      switchTab('flood');
      if (a.source && a.source !== state.source) {
        state.source = a.source;
        $$('#seg-source button').forEach(b => b.classList.toggle('on', b.dataset.source === a.source));
      }
      setLevel(a.level_m, { animate: true });
      break;
    case 'flood_clear':
      clearFlood();
      break;
    case 'safe_zones':
      if (state.level == null) setLevel(a.level_m, { animate: true });
      switchTab('flood');
      showZones(a.zones);
      break;
    case 'highlight_buildings':
      viewer.setHighlights(a.ids);
      switchTab('buildings');
      renderBuildingList(state.result.buildings.filter(b => a.ids.includes(b.id)).slice(0, 60));
      break;
    case 'fly_to':
      selectBuilding(a.building_id, true);
      break;
    case 'sun':
      state.hour = a.hour;
      state.month = a.month;
      switchTab('sun');
      applySun();
      break;
    case 'solar':
      switchTab('sun');
      runSolar();
      break;
    case 'view':
      setShading(a.mode);
      break;
    default:
      break;
  }
}

function setShading(mode) {
  viewer.setShading(mode);
  $$('#seg-shading button').forEach(b => b.classList.toggle('on', b.dataset.mode === mode));
}

// ------------------------------------------------------------------ progress

function showProgress(title, stage, frac, hint = '') {
  $('#progress').hidden = false;
  $('#progress-title').textContent = title;
  $('#progress-stage').textContent = stage;
  $('#progress-bar').style.width = `${Math.round((frac || 0) * 100)}%`;
  $('#progress-hint').textContent = hint;
}
const hideProgress = () => { $('#progress').hidden = true; };

async function trackJob(jobId, title) {
  showProgress(title, 'Waiting in queue', 0.02, 'The first run downloads the depth model, about 100 MB.');
  try {
    await waitForJob(jobId, (st) => showProgress(title, st.stage || st.state, st.progress,
      st.state === 'queued' ? 'Another scene is processing; yours is next.' : ''));
    showProgress(title, 'Loading 3D twin', 1);
    await loadScene(jobId);
  } catch (e) {
    toast(`Could not build the scene: ${e.message}`, 8000);
  } finally {
    hideProgress();
  }
}

// ------------------------------------------------------------------ wiring

function bind() {
  $$('.rail-btn').forEach(b => b.addEventListener('click', () => {
    const already = b.classList.contains('active') && !$('#drawer').classList.contains('closed');
    $('#drawer').classList.toggle('closed', already);
    switchTab(b.dataset.tab);
    updateInsets();
  }));

  $('#in-exag').addEventListener('input', (e) => {
    viewer.setExaggeration(parseFloat(e.target.value));
    $('#out-exag').textContent = `${parseFloat(e.target.value).toFixed(1)}×`;
  });
  $('#in-sharp').addEventListener('input', (e) => {
    viewer.setSharpness(parseFloat(e.target.value));
    $('#out-sharp').textContent = `${Math.round(e.target.value * 100)}%`;
  });
  $$('#seg-shading button').forEach(b => b.addEventListener('click', () => setShading(b.dataset.mode)));
  let turntable = false;
  $('#btn-turntable').addEventListener('click', (e) => {
    turntable = !turntable;
    viewer.setAutoRotate(turntable);
    e.currentTarget.classList.toggle('on', turntable);
  });
  $('#btn-fly').addEventListener('click', () => viewer.setFlythrough(true));
  $('#btn-reset').addEventListener('click', () => viewer.resetView());
  $('#btn-apply-scale').addEventListener('click', () => {
    const body = { height_ref_m: parseFloat($('#in-height-ref').value) };
    if (!$('#field-gsd').hidden) body.gsd_m = parseFloat($('#in-gsd').value);
    if (!$('#field-relief').hidden) body.terrain_relief_m = parseFloat($('#in-relief').value);
    rescale(body);
  });

  $$('#seg-source button').forEach(b => b.addEventListener('click', () => setSource(b.dataset.source)));
  $$('#quick-rise [data-rise]').forEach(b => b.addEventListener('click', () => {
    setLevel(floodRange().lo + parseFloat(b.dataset.rise), { animate: true });
  }));
  $('#btn-clear-flood').addEventListener('click', clearFlood);
  $('#btn-safe').addEventListener('click', findSafeZones);
  const flyFromList = (e) => {
    const li = e.target.closest('li[data-row]');
    if (!li) return;
    if (li.dataset.id) selectBuilding(parseInt(li.dataset.id, 10), true);
    else viewer.flyToCell(parseFloat(li.dataset.row), parseFloat(li.dataset.col));
  };
  ['#affected-list', '#safe-list', '#bld-list'].forEach(s => $(s).addEventListener('click', flyFromList));
  $('#solar-out').addEventListener('click', (e) => {
    const li = e.target.closest('li[data-id]');
    if (li) selectBuilding(parseInt(li.dataset.id, 10), true);
  });

  $('#in-minh').addEventListener('input', (e) => {
    const v = parseFloat(e.target.value);
    $('#out-minh').textContent = v > 0 ? `${v} m` : 'off';
    const sel = v > 0 ? state.result.buildings.filter(b => b.height_m >= v) : [];
    viewer.setHighlights(sel.map(b => b.id));
    renderBuildingList((v > 0 ? sel : state.result.buildings).slice(0, 60));
  });

  $('#in-month').addEventListener('change', (e) => { state.month = parseInt(e.target.value, 10); applySun(); });
  $('#in-hour').addEventListener('input', (e) => { state.hour = parseFloat(e.target.value); applySun(); });
  $('#btn-day').addEventListener('click', playDay);
  $('#btn-solar').addEventListener('click', runSolar);
  $('#btn-shot').addEventListener('click', saveScreenshot);
  $('#btn-brief').addEventListener('click', openBrief);

  $('#ask').addEventListener('submit', (e) => {
    e.preventDefault();
    const v = $('#ask-input').value;
    $('#ask-input').value = '';
    ask(v);
  });
  $('#chips').addEventListener('click', (e) => { if (e.target.tagName === 'BUTTON') ask(e.target.textContent); });
  $('#btn-copilot-min').addEventListener('click', () => {
    $('#copilot').hidden = true;
    $('#copilot-tab').hidden = false;
    document.documentElement.style.setProperty('--copilot', '0px');
    updateInsets();
  });
  $('#copilot-tab').addEventListener('click', () => {
    $('#copilot').hidden = false;
    $('#copilot-tab').hidden = true;
    document.documentElement.style.removeProperty('--copilot');
    updateInsets();
  });
  window.addEventListener('resize', updateInsets);

  viewer.on('hover', (hit) => {
    const r = $('#readout');
    if (!hit) { r.hidden = true; return; }
    r.hidden = false;
    const above = Math.max(0, hit.elev - hit.ground);
    r.innerHTML = `<span>Elevation<b>${fmt(hit.elev, 1)} m</b></span>
      <span>Above ground<b>${fmt(above, 1)} m</b></span>
      <span>Slope<b>${fmt(hit.slope)}°</b></span>
      ${hit.waterDepth > 0 ? `<span>Water depth<b style="color:var(--water)">${fmt(hit.waterDepth, 2)} m</b></span>` : ''}
      ${hit.buildingId ? `<span>Building<b>${hit.buildingId}</b></span>` : ''}`;
  });
  viewer.on('click', (hit) => {
    if (state.source === 'point' && $('.tab[data-tab="flood"]').classList.contains('active')) {
      state.point = [hit.row, hit.col];
      $('#breach-hint').hidden = true;
      viewer.clearMarkers();
      viewer.addMarker({ row: hit.row, col: hit.col, label: 'Breach', color: 0xe4572e });
      setLevel(state.level ?? floodRange().lo + 2, { animate: state.level == null });
      return;
    }
    if (hit.buildingId) selectBuilding(hit.buildingId);
  });
  viewer.on('flythrough', (on) => {
    $('#btn-fly').classList.toggle('on', on);
    if (on) toast('Flying: W A S D to move, Space and C for up and down, Esc to stop.', 6000);
  });

  bindGauge();

  const dialog = new NewSceneDialog({
    onMap: async (bbox, name) => {
      const { job_id } = await api.createMap(bbox, name);
      trackJob(job_id, `Building ${name || 'your 3D twin'}`);
    },
    onUpload: async (file, fields) => {
      const { job_id } = await api.upload(file, fields);
      trackJob(job_id, `Building ${fields.name || file.name}`);
    },
    onOpen: (jobId) => loadScene(jobId).catch(e => toast(e.message)),
    toast,
  });
  $('#btn-new').addEventListener('click', () => dialog.open());

  window.addEventListener('dragover', (e) => e.preventDefault());
  window.addEventListener('drop', (e) => {
    e.preventDefault();
    const f = e.dataTransfer.files?.[0];
    if (f) dialog.open('upload', f);
  });
}

async function boot() {
  bind();
  switchTab('overview');
  const wanted = new URLSearchParams(location.search).get('scene');
  try {
    await loadScene(wanted || 'latest');
  } catch (e) {
    try { await loadScene('job_demo'); } catch (_) { toast('No scene available yet. Create one with New scene.', 8000); }
  }
  try {
    const h = await api.health();
    $('#engine').textContent = h.llm.available ? h.llm.model : 'Offline rules';
    chatSystem(h.llm.available
      ? `Copilot uses ${h.llm.model}, running locally and free.`
      : 'Copilot is in offline mode. Start Ollama for free-form questions; the suggestions below always work.');
  } catch (_) { /* server will report on first question */ }
}

boot();
