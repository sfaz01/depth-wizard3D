// Three.js scene: metric terrain, live flood water, building overlays, markers.

import * as THREE from 'three';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';
import { PointerLockControls } from 'three/addons/controls/PointerLockControls.js';
import { floodMask, lowestThreshold } from './flood.js';

const WORLD = 500; // longest scene side in world units
export const SEVERITY_COLORS = {
  minor: [242, 214, 73],
  moderate: [245, 154, 35],
  severe: [228, 87, 46],
  critical: [176, 24, 94],
};
const HIGHLIGHT = [255, 176, 0];
const SELECTED = [255, 255, 255];
const SAFE = 0x3cc48a;

export class Viewer {
  constructor(container) {
    this.container = container;
    this.exaggeration = 1.5;
    this.sharpness = 0.6;
    this.shading = 'rgb';
    this.overlay = { highlight: new Set(), affected: new Map(), selected: null };
    this.flood = null; // { level, source, mask }
    this.targetLevel = null;
    this.listeners = {};
    this._initThree();
  }

  on(evt, fn) { (this.listeners[evt] ||= []).push(fn); }
  emit(evt, data) { (this.listeners[evt] || []).forEach(fn => fn(data)); }

  _initThree() {
    const c = this.container;
    this.scene = new THREE.Scene();
    this.scene.background = new THREE.Color(0x0c1626);
    this.scene.fog = new THREE.Fog(0x0c1626, 900, 2600);

    this.camera = new THREE.PerspectiveCamera(45, c.clientWidth / c.clientHeight, 0.5, 6000);
    this.renderer = new THREE.WebGLRenderer({ antialias: true, preserveDrawingBuffer: true });
    this.renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
    this.renderer.setSize(c.clientWidth, c.clientHeight);
    this.renderer.shadowMap.enabled = true;
    this.renderer.shadowMap.type = THREE.PCFSoftShadowMap;
    this.renderer.toneMapping = THREE.ACESFilmicToneMapping;
    this.renderer.toneMappingExposure = 1.05;
    c.appendChild(this.renderer.domElement);

    this.hemi = new THREE.HemisphereLight(0xdfeaff, 0x3a3020, 0.75);
    this.scene.add(this.hemi);
    this.sun = new THREE.DirectionalLight(0xfff4e0, 2.2);
    this.sun.castShadow = true;
    this.sun.shadow.mapSize.set(4096, 4096);
    const s = this.sun.shadow.camera;
    s.left = -WORLD * 0.75; s.right = WORLD * 0.75; s.top = WORLD * 0.75; s.bottom = -WORLD * 0.75;
    s.near = 10; s.far = 3000;
    this.sun.shadow.bias = -0.0004;
    this.sun.shadow.normalBias = 0.6;
    this.scene.add(this.sun);
    this.scene.add(this.sun.target);
    this.setSun(160, 55);

    this.orbit = new OrbitControls(this.camera, this.renderer.domElement);
    this.orbit.enableDamping = true;
    this.orbit.dampingFactor = 0.08;
    this.orbit.maxPolarAngle = Math.PI / 2 - 0.03;
    this.orbit.minDistance = 15;
    this.orbit.maxDistance = 2500;
    this.orbit.autoRotateSpeed = 0.6;

    this.fly = new PointerLockControls(this.camera, document.body);
    this.flyActive = false;
    this.move = { f: 0, b: 0, l: 0, r: 0, u: 0, d: 0 };
    this.fly.addEventListener('unlock', () => { if (this.flyActive) this.setFlythrough(false); });
    const key = (down) => (e) => {
      if (!this.flyActive) return;
      const m = { KeyW: 'f', KeyS: 'b', KeyA: 'l', KeyD: 'r', Space: 'u', KeyC: 'd' }[e.code];
      if (m) { this.move[m] = down ? 1 : 0; e.preventDefault(); }
    };
    document.addEventListener('keydown', key(true));
    document.addEventListener('keyup', key(false));

    this.markers = new THREE.Group();
    this.scene.add(this.markers);
    this.raycaster = new THREE.Raycaster();
    this.pointer = new THREE.Vector2();
    this.tween = null;

    const dom = this.renderer.domElement;
    let downAt = null;
    dom.addEventListener('pointerdown', (e) => { downAt = [e.clientX, e.clientY]; });
    dom.addEventListener('pointerup', (e) => {
      if (!downAt || Math.hypot(e.clientX - downAt[0], e.clientY - downAt[1]) > 5) return;
      const hit = this.pick(e);
      if (hit) this.emit('click', hit);
    });
    dom.addEventListener('pointermove', (e) => {
      if (this.flyActive) return;
      this.emit('hover', this.pick(e));
    });
    window.addEventListener('resize', () => this.resize());
    this.clock = new THREE.Clock();
    this.renderer.setAnimationLoop(() => this._frame());
  }

  resize() {
    const c = this.container;
    this.camera.aspect = c.clientWidth / c.clientHeight;
    this._applyInsets();
    this.renderer.setSize(c.clientWidth, c.clientHeight);
  }

  // Shift the projection centre into the part of the screen not covered by side panels,
  // so "look at the scene" really means the visible middle, not behind a drawer.
  setInsets(left, right) {
    this.insets = [left, right];
    this._applyInsets();
  }

  _applyInsets() {
    const [left, right] = this.insets || [0, 0];
    const W = this.container.clientWidth;
    const shiftPx = (left - right) / 2;
    const cam = this.camera;
    const frustumW = 2 * Math.tan(THREE.MathUtils.degToRad(cam.fov / 2)) * cam.aspect;
    cam.filmOffset = -(shiftPx / W) * frustumW * cam.getFilmWidth();
    cam.updateProjectionMatrix();
  }

  // ---------------------------------------------------------------- loading
  async load(result, fetchBinary) {
    const m = result.manifest;
    this.manifest = m;
    this.buildings = new Map(result.buildings.map(b => [b.id, b]));
    const w = m.width, h = m.height;
    this.w = w; this.h = h;
    const f32 = (buf) => new Float32Array(buf);
    const [dsm, sharp, ground, labels, water] = await Promise.all([
      fetchBinary(result.assets.heightmap_bin).then(f32),
      fetchBinary(result.assets.heightmap_sharp_bin).then(f32),
      result.assets.ground_bin ? fetchBinary(result.assets.ground_bin).then(f32) : null,
      result.assets.labels_bin ? fetchBinary(result.assets.labels_bin).then(b => new Int32Array(b)) : null,
      result.assets.water_bin ? fetchBinary(result.assets.water_bin).then(b => new Uint8Array(b)) : null,
    ]);
    this.permanentWater = water && water.some(v => v) ? water : null;
    this.dsm = dsm; this.sharp = sharp; this.ground = ground || dsm; this.labels = labels || new Int32Array(w * h);
    this.lowThresh = lowestThreshold(dsm);
    let mn = Infinity, mx = -Infinity;
    for (const v of dsm) { if (v < mn) mn = v; if (v > mx) mx = v; }
    this.minElev = mn; this.maxElev = mx;

    const img = await new Promise((res, rej) => {
      const i = new Image();
      i.onload = () => res(i);
      i.onerror = () => rej(new Error('Texture failed to load'));
      i.src = result.assets.texture;
    });
    this.baseCanvas = document.createElement('canvas');
    this.baseCanvas.width = w; this.baseCanvas.height = h;
    const bctx = this.baseCanvas.getContext('2d', { willReadFrequently: true });
    bctx.drawImage(img, 0, 0, w, h);
    this.basePixels = bctx.getImageData(0, 0, w, h);
    this.paintCanvas = document.createElement('canvas');
    this.paintCanvas.width = w; this.paintCanvas.height = h;

    this._computeSlope();
    this._buildMesh();
    this._buildWater();
    this.clearMarkers();
    this.overlay = { highlight: new Set(), affected: new Map(), selected: null };
    this.flood = null;
    this.targetLevel = null;
    this.water.visible = false;
    this.repaint();
    this.resetView();
  }

  _computeSlope() {
    const { w, h, dsm } = this;
    const g = this.manifest.gsd_m || 1;
    this.slope = new Float32Array(w * h);
    for (let y = 0; y < h; y++) {
      for (let x = 0; x < w; x++) {
        const i = y * w + x;
        const x0 = Math.max(0, x - 1), x1 = Math.min(w - 1, x + 1);
        const y0 = Math.max(0, y - 1), y1 = Math.min(h - 1, y + 1);
        const dx = (dsm[y * w + x1] - dsm[y * w + x0]) / ((x1 - x0) * g);
        const dy = (dsm[y1 * w + x] - dsm[y0 * w + x]) / ((y1 - y0) * g);
        this.slope[i] = Math.atan(Math.hypot(dx, dy)) * 180 / Math.PI;
      }
    }
  }

  get worldW() { return this.manifest.aspect_ratio >= 1 ? WORLD : WORLD * this.manifest.aspect_ratio; }
  get worldH() { return this.manifest.aspect_ratio >= 1 ? WORLD / this.manifest.aspect_ratio : WORLD; }
  get unitsPerMetre() { return this.worldW / (this.w * (this.manifest.gsd_m || 1)); }

  elevToY(e) { return (e - this.minElev) * this.unitsPerMetre * this.exaggeration; }
  cellToWorld(row, col, elev) {
    return new THREE.Vector3(
      (col / (this.w - 1) - 0.5) * this.worldW,
      this.elevToY(elev ?? this.dsm[Math.round(row) * this.w + Math.round(col)]),
      (row / (this.h - 1) - 0.5) * this.worldH,
    );
  }

  _buildMesh() {
    if (this.mesh) {
      this.scene.remove(this.mesh);
      this.mesh.geometry.dispose();
      this.mesh.material.dispose();
    }
    const geo = new THREE.PlaneGeometry(this.worldW, this.worldH, this.w - 1, this.h - 1);
    geo.rotateX(-Math.PI / 2);
    this.texture = new THREE.CanvasTexture(this.paintCanvas);
    this.texture.colorSpace = THREE.SRGBColorSpace;
    this.texture.anisotropy = this.renderer.capabilities.getMaxAnisotropy();
    const mat = new THREE.MeshStandardMaterial({ map: this.texture, roughness: 0.92, metalness: 0.0 });
    this.mesh = new THREE.Mesh(geo, mat);
    this.mesh.castShadow = true;
    this.mesh.receiveShadow = true;
    this.scene.add(this.mesh);

    // Skirt: a plinth under the terrain so the model reads as a solid block, not a sheet.
    if (this.skirt) { this.scene.remove(this.skirt); this.skirt.geometry.dispose(); }
    const skirtGeo = new THREE.BoxGeometry(this.worldW, 1, this.worldH);
    this.skirt = new THREE.Mesh(skirtGeo, new THREE.MeshStandardMaterial({ color: 0x1d2b40, roughness: 1 }));
    this.scene.add(this.skirt);
    this.applyHeights();
  }

  applyHeights() {
    const pos = this.mesh.geometry.attributes.position;
    const s = this.sharpness;
    for (let i = 0; i < pos.count; i++) {
      const e = this.dsm[i] * (1 - s) + this.sharp[i] * s;
      pos.setY(i, this.elevToY(e));
    }
    pos.needsUpdate = true;
    this.mesh.geometry.computeVertexNormals();
    this.mesh.geometry.computeBoundingSphere();
    this.mesh.geometry.computeBoundingBox();
    const depth = 12;
    this.skirt.scale.y = depth;
    this.skirt.position.y = -depth / 2 - 0.2;
    if (this.flood) this._placeWater(this.flood.level);
    this._placeMarkers();
  }

  setExaggeration(v) { this.exaggeration = v; if (this.mesh) this.applyHeights(); }
  setSharpness(v) { this.sharpness = v; if (this.mesh) this.applyHeights(); }

  // ---------------------------------------------------------------- painting
  setShading(mode) {
    this.shading = mode;
    if (!this.mesh) return;
    this.mesh.material.wireframe = mode === 'wire';
    this.repaint();
  }

  repaint() {
    const { w, h } = this;
    const out = new ImageData(new Uint8ClampedArray(this.basePixels.data), w, h);
    const d = out.data;
    const mode = this.shading;
    const { highlight, affected, selected } = this.overlay;
    const anyBuildingOverlay = highlight.size || affected.size || selected;
    for (let i = 0, p = 0; i < w * h; i++, p += 4) {
      let rgb = null;
      if (mode === 'heights') {
        rgb = heightRamp(Math.max(0, this.dsm[i] - this.ground[i]) / 40);
      } else if (mode === 'slope') {
        const s = this.slope[i];
        rgb = s < 15 ? [60, 196, 138] : s < 30 ? [245, 154, 35] : [228, 87, 46];
      }
      if (rgb) { d[p] = d[p] * 0.25 + rgb[0] * 0.75; d[p + 1] = d[p + 1] * 0.25 + rgb[1] * 0.75; d[p + 2] = d[p + 2] * 0.25 + rgb[2] * 0.75; }
      if (!anyBuildingOverlay) continue;
      const id = this.labels[i];
      if (!id) {
        if (highlight.size || affected.size) { d[p] *= 0.55; d[p + 1] *= 0.55; d[p + 2] *= 0.6; }
        continue;
      }
      let c = null;
      if (id === selected) c = SELECTED;
      else if (affected.has(id)) c = SEVERITY_COLORS[affected.get(id)];
      else if (highlight.has(id)) c = HIGHLIGHT;
      if (c) { d[p] = d[p] * 0.3 + c[0] * 0.7; d[p + 1] = d[p + 1] * 0.3 + c[1] * 0.7; d[p + 2] = d[p + 2] * 0.3 + c[2] * 0.7; }
      else if (highlight.size || affected.size) { d[p] *= 0.7; d[p + 1] *= 0.7; d[p + 2] *= 0.7; }
    }
    this.paintCanvas.getContext('2d').putImageData(out, 0, 0);
    this.texture.needsUpdate = true;
  }

  setHighlights(ids) { this.overlay.highlight = new Set(ids); this.repaint(); }
  setAffected(list) { this.overlay.affected = new Map(list.map(a => [a.id, a.severity])); this.repaint(); }
  setSelected(id) { this.overlay.selected = id; this.repaint(); }

  // ---------------------------------------------------------------- water
  _buildWater() {
    if (this.water) { this.scene.remove(this.water); this.water.geometry.dispose(); this.water.material.dispose(); }
    this.waterData = new Uint8Array(this.w * this.h * 4);
    this.waterTex = new THREE.DataTexture(this.waterData, this.w, this.h, THREE.RGBAFormat);
    this.waterTex.magFilter = THREE.LinearFilter;
    this.waterTex.minFilter = THREE.LinearFilter;
    this.waterTex.colorSpace = THREE.SRGBColorSpace;
    const geo = new THREE.PlaneGeometry(this.worldW, this.worldH);
    geo.rotateX(-Math.PI / 2);
    const mat = new THREE.MeshStandardMaterial({
      map: this.waterTex, transparent: true, roughness: 0.08, metalness: 0.15, depthWrite: false,
    });
    this.water = new THREE.Mesh(geo, mat);
    this.water.renderOrder = 2;
    this.water.receiveShadow = true;
    this.scene.add(this.water);
  }

  _placeWater(level) { this.water.position.y = this.elevToY(level) + 0.05; }

  setFlood(level, source = 'lowest') {
    if (!this.mesh) return null;
    const mask = floodMask(this.dsm, this.w, this.h, level, source, this.lowThresh, this.permanentWater);
    const d = this.waterData;
    const { w, h } = this;
    let wetCount = 0;
    for (let i = 0; i < mask.length; i++) {
      // DataTexture row 0 is the bottom (south) edge; grid row 0 is north.
      const p = (((h - 1 - Math.floor(i / w)) * w) + (i % w)) * 4;
      if (!mask[i]) { d[p + 3] = 0; continue; }
      wetCount++;
      const depth = Math.min(1, (level - this.dsm[i]) / 4);
      if (this.permanentWater && this.permanentWater[i]) {
        // The river / sea itself: clear blue.
        d[p] = 40 - 25 * depth; d[p + 1] = 120 - 50 * depth; d[p + 2] = 175 - 40 * depth; d[p + 3] = 170 + 60 * depth;
      } else {
        // New floodwater carries silt: muddy ochre, darker where deeper.
        d[p] = 150 - 60 * depth; d[p + 1] = 118 - 50 * depth; d[p + 2] = 72 - 32 * depth; d[p + 3] = 165 + 75 * depth;
      }
    }
    this.waterTex.needsUpdate = true;
    this.flood = { level, source, mask };
    this.water.visible = wetCount > 0;
    this._placeWater(level);
    return wetCount;
  }

  animateFloodTo(level, source, onStep) {
    const from = this.flood ? this.flood.level : (this.datum ?? this.lowThresh);
    this.targetLevel = { from, to: level, source, t0: performance.now(), dur: 1600, onStep };
  }

  clearFlood() {
    this.flood = null;
    this.targetLevel = null;
    if (this.water) this.water.visible = false;
  }

  // ---------------------------------------------------------------- markers
  clearMarkers() {
    for (const m of [...this.markers.children]) {
      this.markers.remove(m);
      m.traverse(o => { o.geometry?.dispose(); o.material?.map?.dispose(); o.material?.dispose(); });
    }
    this.markerSpecs = [];
  }

  addMarker({ row, col, label, color = SAFE, elev = null }) {
    const g = new THREE.Group();
    const pole = new THREE.Mesh(new THREE.CylinderGeometry(0.6, 0.6, 26, 8),
      new THREE.MeshBasicMaterial({ color }));
    pole.position.y = 13;
    const head = new THREE.Mesh(new THREE.SphereGeometry(3.2, 16, 12), new THREE.MeshBasicMaterial({ color }));
    head.position.y = 27;
    g.add(pole, head);
    if (label) {
      const sprite = textSprite(label, color);
      sprite.position.y = 38;
      g.add(sprite);
    }
    this.markers.add(g);
    this.markerSpecs.push({ group: g, row, col, elev });
    this._placeMarkers();
    return g;
  }

  _placeMarkers() {
    for (const m of this.markerSpecs || []) m.group.position.copy(this.cellToWorld(m.row, m.col, m.elev));
  }

  // ---------------------------------------------------------------- camera
  resetView() {
    const d = Math.max(this.worldW, this.worldH);
    this.flyToPose(new THREE.Vector3(d * 0.04, d * 0.95, d * 1.15), new THREE.Vector3(0, -d * 0.04, 0), 0);
  }

  flyToCell(row, col, distance = 140) {
    const target = this.cellToWorld(row, col);
    const dir = new THREE.Vector3().subVectors(this.camera.position, this.orbit.target).setY(0).normalize();
    if (!isFinite(dir.x)) dir.set(0, 0, 1);
    const pos = target.clone().add(dir.multiplyScalar(distance)).add(new THREE.Vector3(0, distance * 0.75, 0));
    this.flyToPose(pos, target, 1200);
  }

  flyToPose(pos, target, ms = 1000) {
    if (!ms) {
      this.camera.position.copy(pos);
      this.orbit.target.copy(target);
      this.orbit.update();
      return;
    }
    this.tween = { p0: this.camera.position.clone(), t0: this.orbit.target.clone(), p1: pos, t1: target,
      start: performance.now(), ms };
  }

  setAutoRotate(on) { this.orbit.autoRotate = on; }

  setFlythrough(on) {
    this.flyActive = on;
    this.orbit.enabled = !on;
    if (on) this.fly.lock(); else this.fly.unlock();
    this.emit('flythrough', on);
  }

  setSun(azimuth, elevation) {
    const az = THREE.MathUtils.degToRad(azimuth);
    const el = THREE.MathUtils.degToRad(Math.max(elevation, 1));
    const r = 1200;
    // North is -z, east is +x.
    this.sun.position.set(r * Math.sin(az) * Math.cos(el), r * Math.sin(el), -r * Math.cos(az) * Math.cos(el));
    const day = Math.max(0, Math.min(1, elevation / 20));
    this.sun.intensity = 0.15 + 2.1 * day;
    this.sun.color.setRGB(1, 0.75 + 0.2 * day, 0.55 + 0.4 * day);
    this.hemi.intensity = 0.25 + 0.5 * day;
  }

  // ---------------------------------------------------------------- picking
  pick(event) {
    if (!this.mesh) return null;
    const rect = this.renderer.domElement.getBoundingClientRect();
    this.pointer.set(((event.clientX - rect.left) / rect.width) * 2 - 1, -((event.clientY - rect.top) / rect.height) * 2 + 1);
    this.raycaster.setFromCamera(this.pointer, this.camera);
    const hit = this.raycaster.intersectObject(this.mesh)[0];
    if (!hit || !hit.uv) return null;
    const col = Math.min(this.w - 1, Math.max(0, Math.round(hit.uv.x * (this.w - 1))));
    const row = Math.min(this.h - 1, Math.max(0, Math.round((1 - hit.uv.y) * (this.h - 1))));
    const i = row * this.w + col;
    return {
      row, col,
      elev: this.dsm[i],
      ground: this.ground[i],
      slope: this.slope[i],
      buildingId: this.labels[i] || null,
      waterDepth: this.flood && this.flood.mask[i] ? this.flood.level - this.dsm[i] : 0,
      point: hit.point,
    };
  }

  screenshot() {
    this.renderer.render(this.scene, this.camera);
    return this.renderer.domElement.toDataURL('image/png');
  }

  // ---------------------------------------------------------------- loop
  _frame() {
    const dt = Math.min(this.clock.getDelta(), 0.05);
    const now = performance.now();
    if (this.tween) {
      const t = Math.min(1, (now - this.tween.start) / this.tween.ms);
      const k = t < 0.5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2;
      this.camera.position.lerpVectors(this.tween.p0, this.tween.p1, k);
      this.orbit.target.lerpVectors(this.tween.t0, this.tween.t1, k);
      if (t >= 1) this.tween = null;
    }
    if (this.targetLevel) {
      const tl = this.targetLevel;
      const t = Math.min(1, (now - tl.t0) / tl.dur);
      const k = 1 - Math.pow(1 - t, 3);
      const lvl = tl.from + (tl.to - tl.from) * k;
      this.setFlood(lvl, tl.source);
      tl.onStep?.(lvl, t >= 1);
      if (t >= 1) this.targetLevel = null;
    }
    if (this.flyActive && this.fly.isLocked) {
      const speed = 160 * dt;
      this.fly.moveForward((this.move.f - this.move.b) * speed);
      this.fly.moveRight((this.move.r - this.move.l) * speed);
      this.camera.position.y += (this.move.u - this.move.d) * speed;
    } else {
      this.orbit.update();
    }
    this.renderer.render(this.scene, this.camera);
  }
}

function heightRamp(t) {
  t = Math.max(0, Math.min(1, t));
  const stops = [[38, 70, 110], [47, 168, 224], [60, 196, 138], [242, 214, 73], [228, 87, 46]];
  const x = t * (stops.length - 1);
  const i = Math.min(stops.length - 2, Math.floor(x));
  const f = x - i;
  return stops[i].map((v, k) => v + (stops[i + 1][k] - v) * f);
}

function textSprite(text, color) {
  const canvas = document.createElement('canvas');
  const ctx = canvas.getContext('2d');
  const font = '600 44px Barlow, sans-serif';
  ctx.font = font;
  const w = Math.ceil(ctx.measureText(text).width) + 36;
  canvas.width = w; canvas.height = 72;
  ctx.font = font;
  ctx.fillStyle = 'rgba(15, 27, 45, 0.92)';
  ctx.beginPath();
  ctx.roundRect(0, 0, w, 72, 14);
  ctx.fill();
  ctx.fillStyle = '#' + new THREE.Color(color).getHexString();
  ctx.fillRect(0, 0, 8, 72);
  ctx.fillStyle = '#e8eef5';
  ctx.textBaseline = 'middle';
  ctx.fillText(text, 22, 38);
  const tex = new THREE.CanvasTexture(canvas);
  tex.colorSpace = THREE.SRGBColorSpace;
  const sprite = new THREE.Sprite(new THREE.SpriteMaterial({ map: tex, depthTest: false }));
  sprite.scale.set(w / 6, 12, 1);
  sprite.renderOrder = 10;
  return sprite;
}
