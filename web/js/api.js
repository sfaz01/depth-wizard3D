// Thin wrapper over the DepthWizard REST API.

async function request(path, options = {}) {
  const res = await fetch(path, options);
  if (!res.ok) {
    let detail = `${res.status} ${res.statusText}`;
    try {
      const body = await res.json();
      if (body.detail) detail = typeof body.detail === 'string' ? body.detail : JSON.stringify(body.detail);
    } catch (_) { /* not JSON */ }
    throw new Error(detail);
  }
  return res;
}

const json = (path, options) => request(path, options).then(r => r.json());
const post = (path, body) => json(path, {
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify(body),
});

export const api = {
  health: () => json('/api/health'),
  serverInfo: () => json('/api/server-info'),
  jobs: () => json('/api/jobs'),
  status: (id) => json(`/api/jobs/${id}`),
  result: (id) => json(`/api/result/${id}`),
  binary: (url) => request(url).then(r => r.arrayBuffer()),
  createMap: (bbox, name) => post('/api/scene/map', { ...bbox, name }),
  upload: (file, fields = {}) => {
    const fd = new FormData();
    fd.append('file', file);
    for (const [k, v] of Object.entries(fields)) if (v !== '' && v != null) fd.append(k, v);
    return json('/api/process', { method: 'POST', body: fd });
  },
  rescale: (id, body) => post(`/api/jobs/${id}/scale`, body),
  flood: (id, body) => post(`/api/jobs/${id}/flood`, body),
  safeZones: (id, body) => post(`/api/jobs/${id}/safe_zones`, body),
  solar: (id) => json(`/api/jobs/${id}/solar`),
  copilot: (id, message, state) => post(`/api/jobs/${id}/copilot`, { message, state }),
};

// Poll a job until it finishes. onUpdate receives every status object.
export async function waitForJob(id, onUpdate, intervalMs = 600) {
  for (;;) {
    const st = await api.status(id);
    onUpdate?.(st);
    if (st.state === 'done') return st;
    if (st.state === 'error') throw new Error(st.error || 'Processing failed');
    await new Promise(r => setTimeout(r, intervalMs));
  }
}
