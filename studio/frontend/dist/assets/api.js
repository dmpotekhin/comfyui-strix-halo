// Thin API client. Every call throws ApiError with the server's message so views can show
// exactly what went wrong instead of a generic failure.

export class ApiError extends Error {
  constructor(status, payload) {
    const msg =
      payload?.detail?.message ||
      (typeof payload?.detail === 'string' ? payload.detail : null) ||
      payload?.message ||
      `HTTP ${status}`;
    super(msg);
    this.status = status;
    this.fields = payload?.detail?.fields || null;
    this.payload = payload;
  }
}

async function request(path, options = {}) {
  const res = await fetch(path, {
    headers: options.body instanceof FormData ? {} : { 'Content-Type': 'application/json' },
    ...options,
  });
  const text = await res.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch { data = { raw: text }; }
  if (!res.ok) throw new ApiError(res.status, data);
  return data;
}

const get = (path) => request(path);
const post = (path, body) => request(path, { method: 'POST', body: body ? JSON.stringify(body) : undefined });
const put = (path, body) => request(path, { method: 'PUT', body: JSON.stringify(body) });

export const api = {
  health: () => get('/api/health'),
  stats: () => get('/api/stats'),
  refreshEngine: () => post('/api/engine/refresh'),

  workflows: (params = {}) => {
    const q = new URLSearchParams(Object.entries(params).filter(([, v]) => v !== '' && v != null));
    return get(`/api/workflows?${q}`);
  },
  categories: () => get('/api/workflows/categories'),
  workflow: (id) => get(`/api/workflows/${encodeURIComponent(id)}`),
  validate: (id) => get(`/api/workflows/${encodeURIComponent(id)}/validate`),

  submit: (workflow_id, params, client_token) =>
    post('/api/jobs', { workflow_id, params, client_token }),
  jobs: (params = {}) => {
    const q = new URLSearchParams(Object.entries(params).filter(([, v]) => v !== '' && v != null));
    return get(`/api/jobs?${q}`);
  },
  job: (id) => get(`/api/jobs/${encodeURIComponent(id)}`),
  cancelJob: (id) => post(`/api/jobs/${encodeURIComponent(id)}/cancel`),
  rerunJob: (id) => post(`/api/jobs/${encodeURIComponent(id)}/rerun`),

  gallery: (params = {}) => {
    const q = new URLSearchParams(Object.entries(params).filter(([, v]) => v !== '' && v != null));
    return get(`/api/gallery?${q}`);
  },
  inputs: () => get('/api/inputs'),
  models: () => get('/api/models'),

  upload: (file) => {
    const fd = new FormData();
    fd.append('file', file);
    return request('/api/uploads', { method: 'POST', body: fd });
  },

  settings: () => get('/api/settings'),
  saveSettings: (patch) => put('/api/settings', patch),
};
