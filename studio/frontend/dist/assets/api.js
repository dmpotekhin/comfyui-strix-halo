// Thin API client. Every call throws ApiError with the server's message so views can show
// exactly what went wrong instead of a generic failure.
//
// Since stage NET-1 the studio is reachable from the home LAN, so every request carries the
// session cookie and every state-changing request carries the per-session CSRF token. Both are
// same-origin only: nothing here talks to another host.

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

let csrfToken = null;
let currentUser = null;

const UNSAFE = new Set(['POST', 'PUT', 'PATCH', 'DELETE']);

/** Fired when the server says we are not (or no longer) logged in. */
export const UNAUTHORIZED = 'studio:unauthorized';

function onUnauthorized() {
  currentUser = null;
  csrfToken = null;
  window.dispatchEvent(new CustomEvent(UNAUTHORIZED));
}

async function request(path, options = {}) {
  const method = (options.method || 'GET').toUpperCase();
  const headers = {};
  if (!(options.body instanceof FormData)) headers['Content-Type'] = 'application/json';
  if (UNSAFE.has(method) && csrfToken) headers['X-CSRF-Token'] = csrfToken;
  const res = await fetch(path, {
    credentials: 'same-origin', // the session cookie must ride along, and only to us
    ...options,
    method,
    headers: { ...headers, ...(options.headers || {}) },
  });
  const text = await res.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch { data = { raw: text }; }
  if (res.status === 401 && !path.startsWith('/api/auth/login')) onUnauthorized();
  if (!res.ok) throw new ApiError(res.status, data);
  return data;
}

const get = (path) => request(path);
const post = (path, body) => request(path, { method: 'POST', body: body ? JSON.stringify(body) : undefined });
const put = (path, body) => request(path, { method: 'PUT', body: JSON.stringify(body) });
const patch = (path, body) => request(path, { method: 'PATCH', body: JSON.stringify(body) });
const del = (path) => request(path, { method: 'DELETE' });

export const auth = {
  current: () => currentUser,
  isAdmin: () => !!currentUser?.is_admin,
  /** Who am I? Also refreshes the CSRF token, which rotates with the session. */
  async me() {
    const body = await request('/api/auth/me');
    currentUser = body.user;
    csrfToken = body.user?.csrf_token || null;
    return currentUser;
  },
  async login(username, password) {
    const body = await post('/api/auth/login', { username, password });
    currentUser = body.user;
    csrfToken = body.user?.csrf_token || null;
    return currentUser;
  },
  async logout() {
    try { await post('/api/auth/logout'); } finally { onUnauthorized(); }
  },
  changePassword: (current_password, new_password) =>
    post('/api/auth/password', { current_password, new_password }),
};

export const api = {
  auth,
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

  // Admin only. A member gets 403 from the server; the UI hides these views too, but the
  // server is what actually enforces it.
  settings: () => get('/api/settings'),
  saveSettings: (patchBody) => put('/api/settings', patchBody),
  users: () => get('/api/users'),
  createUser: (body) => post('/api/users', body),
  updateUser: (id, body) => patch(`/api/users/${encodeURIComponent(id)}`, body),
  deleteUser: (id) => del(`/api/users/${encodeURIComponent(id)}`),
};
