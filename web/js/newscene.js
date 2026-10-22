// "New scene" dialog: pick an area on a satellite map, upload an image, or reopen a scene.

import { api } from './api.js';

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

// Flood-prone and planning-relevant places, each checked against the imagery.
const PRESETS = [
  { name: 'Srinagar, Jhelum', note: '2014 floods', lat: 34.0700, lon: 74.8100, size: 1200 },
  { name: 'Guwahati, Brahmaputra', note: 'monsoon floods', lat: 26.1920, lon: 91.7470, size: 1200 },
  { name: 'Patna, Ganga', note: 'riverfront', lat: 25.6200, lon: 85.1500, size: 1200 },
  { name: 'Chennai, Adyar', note: '2015 floods', lat: 13.0140, lon: 80.2520, size: 1200 },
  { name: 'Mumbai, Mithi river', note: 'waterlogging', lat: 19.0660, lon: 72.8720, size: 1200 },
  { name: 'Kochi', note: 'backwaters', lat: 9.9700, lon: 76.2800, size: 1200 },
  { name: 'Shimla', note: 'landslide slopes', lat: 31.1048, lon: 77.1734, size: 800 },
  { name: 'Venice', note: 'acqua alta', lat: 45.4340, lon: 12.3380, size: 800 },
  { name: 'Lower Manhattan', note: 'skyline', lat: 40.7070, lon: -74.0120, size: 800 },
];

const EARTH = 40075016.686;

export class NewSceneDialog {
  constructor({ onMap, onUpload, onOpen, toast }) {
    this.cb = { onMap, onUpload, onOpen, toast };
    this.dlg = $('#dlg-new');
    this.file = null;
    this.map = null;
    this.placeName = '';

    $$('#seg-new button').forEach(b => b.addEventListener('click', () => this.show(b.dataset.pane)));
    $('#area-size').addEventListener('change', () => {
      const c = this.map.getCenter();
      this.fitArea(c.lat, c.lng);
    });
    $('#btn-build-map').addEventListener('click', () => this.buildMap());
    $('#place-search').addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); this.search(); } });

    $('#presets').innerHTML = PRESETS.map((p, i) =>
      `<button data-i="${i}"><b>${p.name}</b>, ${p.note}</button>`).join('');
    $('#presets').addEventListener('click', (e) => {
      const b = e.target.closest('button[data-i]');
      if (!b) return;
      const p = PRESETS[+b.dataset.i];
      this.placeName = p.name;
      $('#area-size').value = String(p.size);
      this.fitArea(p.lat, p.lon);
    });

    const drop = $('#drop');
    $('#file').addEventListener('change', (e) => this.setFile(e.target.files[0]));
    drop.addEventListener('dragover', (e) => { e.preventDefault(); drop.classList.add('over'); });
    drop.addEventListener('dragleave', () => drop.classList.remove('over'));
    drop.addEventListener('drop', (e) => {
      e.preventDefault();
      e.stopPropagation();
      drop.classList.remove('over');
      this.setFile(e.dataTransfer.files[0]);
    });
    $('#btn-upload').addEventListener('click', () => this.upload());
    $('#recent').addEventListener('click', (e) => {
      const b = e.target.closest('button[data-job]');
      if (!b) return;
      this.dlg.close();
      this.cb.onOpen(b.dataset.job);
    });
  }

  open(pane = 'map', file = null) {
    if (!this.dlg.open) this.dlg.showModal();
    this.show(pane);
    if (file) this.setFile(file);
  }

  show(pane) {
    $$('#seg-new button').forEach(b => b.classList.toggle('on', b.dataset.pane === pane));
    $$('#dlg-new .pane').forEach(p => p.classList.toggle('on', p.dataset.pane === pane));
    if (pane === 'map') this.ensureMap();
    if (pane === 'recent') this.loadRecent();
  }

  ensureMap() {
    if (this.map) { setTimeout(() => { this.map.invalidateSize(); this.updateFrame(); }, 50); return; }
    const L = window.L;
    this.map = L.map('map', { zoomControl: true, attributionControl: false }).setView([26.1920, 91.7470], 15);
    L.tileLayer('https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}', {
      maxZoom: 19,
    }).addTo(this.map);
    this.map.on('move zoom resize', () => this.updateFrame());
    this.map.on('dragstart', () => { this.placeName = ''; });
    setTimeout(() => { this.map.invalidateSize(); this.updateFrame(); }, 50);
  }

  // Centre on a point and pick the closest zoom at which the selection frame fits.
  fitArea(lat, lon) {
    const box = $('#map').getBoundingClientRect();
    const size = parseFloat($('#area-size').value);
    const avail = Math.max(120, Math.min(box.width, box.height) - 40);
    const z = Math.floor(Math.log2(EARTH * Math.cos(lat * Math.PI / 180) * avail / (256 * size)));
    this.map.setView([lat, lon], Math.max(3, Math.min(18, z)));
    this.updateFrame();
  }

  metresPerPixel() {
    const lat = this.map.getCenter().lat;
    return EARTH * Math.cos(lat * Math.PI / 180) / (256 * 2 ** this.map.getZoom());
  }

  updateFrame() {
    if (!this.map) return;
    const size = parseFloat($('#area-size').value);
    const px = size / this.metresPerPixel();
    const box = $('#map').getBoundingClientRect();
    const frame = $('#map-frame');
    frame.style.width = `${px}px`;
    frame.style.height = `${px}px`;
    const fits = px < Math.min(box.width, box.height) - 10;
    $('#btn-build-map').disabled = !fits;
    $('#btn-build-map').textContent = fits ? 'Build 3D twin' : 'Zoom out to fit the frame';
  }

  bbox() {
    const c = this.map.getCenter();
    const half = parseFloat($('#area-size').value) / 2;
    const dLat = half / 111320;
    const dLon = half / (111320 * Math.cos(c.lat * Math.PI / 180));
    return { west: c.lng - dLon, south: c.lat - dLat, east: c.lng + dLon, north: c.lat + dLat };
  }

  async search() {
    const q = $('#place-search').value.trim();
    if (!q) return;
    try {
      const res = await fetch(`https://nominatim.openstreetmap.org/search?format=json&limit=1&q=${encodeURIComponent(q)}`, {
        headers: { 'Accept-Language': navigator.language || 'en' },
      });
      const hits = await res.json();
      if (!hits.length) { this.cb.toast(`No place called "${q}" found. Try adding the city.`); return; }
      this.placeName = q;
      this.fitArea(parseFloat(hits[0].lat), parseFloat(hits[0].lon));
    } catch (e) {
      this.cb.toast('Place search is unavailable. Pan the map instead.');
    }
  }

  async buildMap() {
    const b = this.bbox();
    const c = this.map.getCenter();
    const name = this.placeName || `${c.lat.toFixed(4)}, ${c.lng.toFixed(4)}`;
    this.dlg.close();
    try {
      await this.cb.onMap(b, name);
    } catch (e) {
      this.cb.toast(`Could not start: ${e.message}`);
    }
  }

  setFile(f) {
    if (!f) return;
    this.file = f;
    $('#drop strong').textContent = f.name;
    $('#drop span').textContent = `${(f.size / 1e6).toFixed(1)} MB, ready to build`;
    $('#btn-upload').disabled = false;
    if (!$('#up-name').value) $('#up-name').value = f.name.replace(/\.[^.]+$/, '');
  }

  async upload() {
    if (!this.file) return;
    const fields = {
      name: $('#up-name').value.trim(),
      gsd_m: $('#up-gsd').value,
      lat: $('#up-lat').value,
      lon: $('#up-lon').value,
    };
    const f = this.file;
    this.dlg.close();
    try {
      await this.cb.onUpload(f, fields);
    } catch (e) {
      this.cb.toast(`Upload failed: ${e.message}`);
    }
  }

  async loadRecent() {
    const box = $('#recent');
    box.innerHTML = '<p class="note">Loading…</p>';
    try {
      const jobs = await api.jobs();
      const kinds = { map: 'Map area', upload: 'Uploaded image', geotiff: 'GeoTIFF', demo: 'Offline demo', legacy: 'Older scene' };
      box.innerHTML = jobs.map(j => `
        <button data-job="${j.job_id}">
          <img src="${j.thumb}" alt="" loading="lazy">
          <span>${j.name.replace(/</g, '&lt;')}</span>
          <small>${kinds[j.kind] || j.kind}, ${new Date(j.updated * 1000).toLocaleDateString()}</small>
        </button>`).join('') || '<p class="note">No scenes yet. Pick an area on the map to build your first.</p>';
    } catch (e) {
      box.innerHTML = `<p class="note">Could not list scenes: ${e.message}</p>`;
    }
  }
}
