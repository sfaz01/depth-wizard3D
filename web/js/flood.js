// Connected flood fill - mirrors src/analytics.py:flood_mask (4-connectivity, same seeds),
// run in the browser so the water follows the slider at 60 fps.

export function lowestThreshold(dsm) {
  const sorted = Float32Array.from(dsm).sort();
  // numpy.percentile(..., 0.5) with linear interpolation
  const pos = 0.005 * (sorted.length - 1);
  const lo = Math.floor(pos);
  const hi = Math.min(sorted.length - 1, lo + 1);
  return sorted[lo] + (sorted[hi] - sorted[lo]) * (pos - lo);
}

// source: 'lowest' | 'edges' | 'rain' | [row, col]. `water` (Uint8Array, optional) marks
// permanent water; for 'lowest' it is the source, exactly like the Python version.
export function floodMask(dsm, w, h, level, source, lowThresh, water = null) {
  const n = w * h;
  const wet = new Uint8Array(n);
  if (source === 'rain') {
    for (let i = 0; i < n; i++) wet[i] = dsm[i] < level ? 1 : 0;
    return wet;
  }
  const queue = new Int32Array(n);
  let head = 0, tail = 0;
  const seed = (i) => {
    if (!wet[i] && dsm[i] < level) { wet[i] = 1; queue[tail++] = i; }
  };
  if (source === 'edges') {
    for (let x = 0; x < w; x++) { seed(x); seed((h - 1) * w + x); }
    for (let y = 0; y < h; y++) { seed(y * w); seed(y * w + w - 1); }
  } else if (Array.isArray(source)) {
    const r = Math.max(0, Math.min(h - 1, source[0] | 0));
    const c = Math.max(0, Math.min(w - 1, source[1] | 0));
    seed(r * w + c);
  } else if (water && water.some(v => v)) {
    for (let i = 0; i < n; i++) if (water[i]) seed(i);
  } else {
    for (let i = 0; i < n; i++) if (dsm[i] <= lowThresh) seed(i);
  }
  while (head < tail) {
    const i = queue[head++];
    const x = i % w;
    if (x > 0) seed(i - 1);
    if (x < w - 1) seed(i + 1);
    if (i >= w) seed(i - w);
    if (i < n - w) seed(i + w);
  }
  return wet;
}
