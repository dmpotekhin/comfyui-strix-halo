// Local AI Creator Studio — single-page front end, no build step, no framework.
// Views render into #view; state lives in this module. Hash routing keeps deep links working
// (#/run/txt2img-turbo) so a workflow can be bookmarked.

import { api, ApiError } from './api.js';

const view = document.getElementById('view');
const nav = document.getElementById('nav');

/* ------------------------------------------------------------------ helpers */

const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) =>
  ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

function fmtBytes(n) {
  if (n == null) return '—';
  const u = ['B', 'KB', 'MB', 'GB', 'TB'];
  let i = 0, v = n;
  while (v >= 1024 && i < u.length - 1) { v /= 1024; i++; }
  return `${v < 10 && i > 1 ? v.toFixed(2) : Math.round(v)} ${u[i]}`;
}

function fmtDuration(s) {
  if (s == null) return '—';
  if (s < 90) return `${s.toFixed(1)}s`;
  const m = Math.floor(s / 60);
  return `${m}m ${Math.round(s - m * 60)}s`;
}

function fmtWhen(iso) {
  if (!iso) return '—';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  const diff = (Date.now() - d.getTime()) / 1000;
  if (diff < 60) return 'just now';
  if (diff < 3600) return `${Math.floor(diff / 60)} min ago`;
  if (diff < 86400) return `${Math.floor(diff / 3600)} h ago`;
  return d.toLocaleDateString();
}

const STATUS_BADGE = {
  available: ['ok', 'available'],
  ready_to_test: ['info', 'ready to test'],
  missing_models: ['warn', 'missing models'],
  missing_nodes: ['warn', 'missing nodes'],
  incompatible: ['danger', 'incompatible'],
  planned: ['neutral', 'planned'],
  queued: ['neutral', 'queued'],
  submitted: ['info', 'submitted'],
  running: ['info', 'running'],
  completed: ['ok', 'completed'],
  failed: ['danger', 'failed'],
  cancelled: ['neutral', 'cancelled'],
};

const badge = (status) => {
  const [cls, label] = STATUS_BADGE[status] || ['neutral', status || 'unknown'];
  return `<span class="badge ${cls}">${esc(label)}</span>`;
};

function toast(msg, bad = false) {
  const box = document.getElementById('toasts');
  const el = document.createElement('div');
  el.className = `toast${bad ? ' bad' : ''}`;
  el.textContent = msg;
  box.appendChild(el);
  setTimeout(() => el.remove(), bad ? 9000 : 4000);
}

const mediaUrl = (o) =>
  `/api/outputs/${encodeURIComponent(o.filename)}?type=${encodeURIComponent(o.type || 'output')}` +
  `&subfolder=${encodeURIComponent(o.subfolder || '')}`;

/* ------------------------------------------------------------------ shell */

const ROUTES = [
  ['dashboard', 'Dashboard', '◈'],
  ['library', 'Workflows', '▤'],
  ['jobs', 'Jobs', '≣'],
  ['gallery', 'Gallery', '▦'],
  ['models', 'Models', '▣'],
  ['settings', 'Settings', '⚙'],
];

function renderNav(active) {
  nav.innerHTML = ROUTES.map(([id, label, ico]) =>
    `<a class="nav-item" href="#/${id}" ${active === id ? 'aria-current="page"' : ''}>
       <span class="ico">${ico}</span><span>${label}</span>
       <span class="count" data-count="${id}"></span>
     </a>`).join('');
}

async function updateNavCounts() {
  try {
    const s = await api.stats();
    const put = (id, v) => {
      const el = nav.querySelector(`[data-count="${id}"]`);
      if (el) el.textContent = v || '';
    };
    put('library', s.workflows?.total);
    put('jobs', s.jobs?.queued + s.jobs?.running || '');
    put('gallery', s.outputs?.count);
    put('models', s.models?.files);
  } catch { /* counts are decoration; never block the UI on them */ }
}

/* ------------------------------------------------------------------ dashboard */

async function viewDashboard() {
  view.innerHTML = `<div class="page-head"><div><h1>Dashboard</h1>
      <p>Live state of the local generation stack.</p></div></div>
    <div class="empty">Loading…</div>`;

  let h, s;
  try {
    [h, s] = await Promise.all([api.health(), api.stats()]);
  } catch (e) {
    view.innerHTML = `<div class="page-head"><div><h1>Dashboard</h1></div></div>
      <div class="alert danger">Cannot reach the studio backend: ${esc(e.message)}</div>`;
    return;
  }

  const engine = h.comfy_connected
    ? `<span class="badge ok"><span class="dot"></span>connected</span>`
    : `<span class="badge danger"><span class="dot"></span>offline</span>`;

  const recent = await api.gallery({ limit: 8 }).catch(() => ({ outputs: [] }));

  view.innerHTML = `
    <div class="page-head">
      <div><h1>Dashboard</h1><p>Local generation via ComfyUI — nothing leaves this machine.</p></div>
      <div class="row">
        ${engine}
        <button class="small" id="refresh-engine">Refresh engine</button>
      </div>
    </div>

    ${h.error ? `<div class="alert danger"><strong>ComfyUI unreachable:</strong> ${esc(h.error)}</div>` : ''}

    <div class="grid cols-4" style="margin-bottom:var(--gap)">
      <div class="stat"><div class="k">Engine</div><div class="v" style="font-size:var(--step-2)">
        ${esc(h.comfy_version || '—')}</div><div class="s">${esc(h.device || h.comfy_url)}</div></div>
      <div class="stat"><div class="k">VRAM free</div><div class="v">${h.vram_free_gb ?? '—'}<span class="s"> / ${h.vram_total_gb ?? '—'} GB</span></div>
        <div class="s">${esc(h.backend || 'backend unknown')}</div></div>
      <div class="stat"><div class="k">Disk free</div><div class="v">${h.disk_free_gb ?? '—'}<span class="s"> GB</span></div>
        <div class="s">models ${s.models?.total_gb ?? '—'} GB</div></div>
      <div class="stat"><div class="k">Workflows runnable</div><div class="v">${h.available_workflows}<span class="s"> / ${h.workflow_count}</span></div>
        <div class="s">nodes ${h.node_count ?? '—'}</div></div>
    </div>

    <div class="grid cols-2">
      <div class="card">
        <div class="card-head"><h2>Queue</h2><a href="#/jobs" class="dim">all jobs →</a></div>
        <div class="grid cols-4" style="gap:10px">
          <div><div class="k dim">running</div><div class="num" style="font-size:var(--step-2)">${s.jobs?.running ?? 0}</div></div>
          <div><div class="k dim">queued</div><div class="num" style="font-size:var(--step-2)">${s.jobs?.queued ?? 0}</div></div>
          <div><div class="k dim">completed</div><div class="num" style="font-size:var(--step-2)">${s.jobs?.completed ?? 0}</div></div>
          <div><div class="k dim">failed</div><div class="num" style="font-size:var(--step-2)">${s.jobs?.failed ?? 0}</div></div>
        </div>
        <p class="dim" style="margin:14px 0 0;font-size:12px">
          One heavy worker runs at a time on purpose: model swaps are expensive and
          unified memory is shared with the OS.</p>
      </div>
      <div class="card">
        <div class="card-head"><h2>Models</h2><a href="#/models" class="dim">manage →</a></div>
        <div class="row" style="gap:8px">
          ${Object.entries(s.models?.folders ? {} : {}).length ? '' : ''}
          <div class="stat" style="flex:1"><div class="k">installed files</div>
            <div class="v" style="font-size:var(--step-2)">${s.models?.files ?? 0}</div></div>
          <div class="stat" style="flex:1"><div class="k">folders</div>
            <div class="v" style="font-size:var(--step-2)">${s.models?.folders ?? 0}</div></div>
          <div class="stat" style="flex:1"><div class="k">total</div>
            <div class="v" style="font-size:var(--step-2)">${s.models?.total_gb ?? 0}<span class="s"> GB</span></div></div>
        </div>
      </div>
    </div>

    <h2 style="margin:26px 0 12px">Recent outputs</h2>
    <div class="gallery">
      ${(recent.outputs || []).map((o) => `
        <figure>
          ${o.kind === 'video'
            ? `<video src="${mediaUrl(o)}" muted preload="metadata"></video>`
            : o.kind === 'audio'
            ? `<audio src="${mediaUrl(o)}" preload="metadata"></audio>`
            : o.kind === 'text'
            ? `<span class="file-chip">${esc(o.filename)}</span>`
            : `<img src="${mediaUrl(o)}" loading="lazy" alt="">`}
          <figcaption><span>${esc(o.workflow_name || o.workflow_id)}</span>
            <span class="dim">${fmtWhen(o.created_at)}</span></figcaption>
        </figure>`).join('') || `<div class="empty">Nothing generated yet.</div>`}
    </div>`;

  document.getElementById('refresh-engine')?.addEventListener('click', async (ev) => {
    ev.target.disabled = true;
    try { await api.refreshEngine(); toast('Engine re-probed'); route(); }
    catch (e) { toast(e.message, true); ev.target.disabled = false; }
  });
}

/* ------------------------------------------------------------------ library */

let libState = { q: '', category: '', status: '' };

async function viewLibrary() {
  const [cats, list] = await Promise.all([
    api.categories(),
    api.workflows(libState),
  ]);

  view.innerHTML = `
    <div class="page-head"><div><h1>Workflow library</h1>
      <p>Every entry is a graph validated against the live engine before it is offered.</p></div>
      <div class="dim">${list.length} shown / ${cats.total} total</div></div>

    <div class="toolbar">
      <input type="search" id="q" placeholder="Search name, description, tag…" value="${esc(libState.q)}">
      <select id="cat">
        <option value="">All categories</option>
        ${cats.categories.map((c) => `<option value="${esc(c.id)}" ${libState.category === c.id ? 'selected' : ''}>
          ${esc(c.label)} (${c.count})</option>`).join('')}
      </select>
      <select id="status">
        <option value="">Any status</option>
        ${Object.entries(cats.statuses || {}).map(([k, v]) =>
          `<option value="${esc(k)}" ${libState.status === k ? 'selected' : ''}>${esc(k)} (${v})</option>`).join('')}
      </select>
    </div>

    <div class="wf-grid">
      ${list.map((w) => `
        <article class="wf-card" data-id="${esc(w.id)}">
          <div class="row" style="justify-content:space-between">
            <h3>${esc(w.name)}</h3>${badge(w.status)}
          </div>
          <div class="desc">${esc(w.description)}</div>
          <div class="meta">
            <span class="tag">${esc(w.category)}</span>
            <span class="tag">${esc(w.output_kind)}</span>
            ${w.min_vram_gb ? `<span class="tag">≥${w.min_vram_gb} GB VRAM</span>` : ''}
            <span class="tag">${w.model_count} model${w.model_count === 1 ? '' : 's'}</span>
            ${w.test_status === 'passed' ? `<span class="badge ok">tested</span>` : ''}
            ${w.missing_models ? `<span class="badge warn">${w.missing_models} missing</span>` : ''}
          </div>
        </article>`).join('') || `<div class="empty">No workflow matches this filter.</div>`}
    </div>`;

  const rerender = () => route();
  document.getElementById('q').addEventListener('input', (e) => {
    libState.q = e.target.value;
    clearTimeout(viewLibrary._t);
    viewLibrary._t = setTimeout(rerender, 220);
  });
  document.getElementById('cat').addEventListener('change', (e) => { libState.category = e.target.value; rerender(); });
  document.getElementById('status').addEventListener('change', (e) => { libState.status = e.target.value; rerender(); });
  view.querySelectorAll('.wf-card').forEach((card) => {
    card.addEventListener('click', () => { location.hash = `#/run/${card.dataset.id}`; });
  });
}

/* ------------------------------------------------------------------ run */

function paramField(p, value, inputs) {
  const val = value ?? p.default ?? '';
  const hint = p.description ? `<div class="hint">${esc(p.description)}</div>` : '';
  const common = `name="${esc(p.key)}" data-type="${esc(p.type)}" id="p-${esc(p.key)}"`;

  if (p.type === 'text') {
    return `<div class="field"><label for="p-${esc(p.key)}">${esc(p.label)}${p.required ? ' *' : ''}</label>
      <textarea ${common}>${esc(val)}</textarea>${hint}</div>`;
  }
  if (p.type === 'bool') {
    return `<div class="field"><label><input type="checkbox" ${common} ${val ? 'checked' : ''}
      style="width:auto;margin-right:8px">${esc(p.label)}</label>${hint}</div>`;
  }
  if (p.type === 'choice') {
    const opts = (p.options || []).map((o) =>
      `<option value="${esc(o)}" ${String(o) === String(val) ? 'selected' : ''}>${esc(o)}</option>`).join('');
    return `<div class="field"><label for="p-${esc(p.key)}">${esc(p.label)}</label>
      <select ${common}>${opts || `<option value="${esc(val)}">${esc(val)}</option>`}</select>${hint}</div>`;
  }
  if (p.type === 'image') {
    const opts = inputs.map((i) =>
      `<option value="${esc(i.name)}" ${i.name === val ? 'selected' : ''}>${esc(i.name)} (${fmtBytes(i.size)})</option>`).join('');
    return `<div class="field"><label for="p-${esc(p.key)}">${esc(p.label)}${p.required ? ' *' : ''}</label>
      <select ${common}>${opts || '<option value="">— no images in input folder —</option>'}</select>
      <div class="row" style="margin-top:6px">
        <input type="file" accept="image/png,image/jpeg,image/webp" data-upload="${esc(p.key)}"
               style="flex:1;font-size:11px;padding:4px">
      </div>${hint}${p.alpha_warning ? '' : ''}</div>`;
  }
  if (p.type === 'seed') {
    return `<div class="field"><label for="p-${esc(p.key)}">${esc(p.label)}</label>
      <div class="row" style="gap:6px;flex-wrap:nowrap">
        <input type="number" ${common} value="${esc(val)}" step="1" style="flex:1">
        <button type="button" class="small ghost" data-rand="${esc(p.key)}">random</button>
      </div>
      <div class="hint">-1 picks a random seed and records it with the job.</div></div>`;
  }
  const step = p.step ?? (p.type === 'int' ? 1 : 0.01);
  const min = p.min != null ? `min="${p.min}"` : '';
  const max = p.max != null ? `max="${p.max}"` : '';
  return `<div class="field"><label for="p-${esc(p.key)}">${esc(p.label)}${p.unit ? ` (${esc(p.unit)})` : ''}</label>
    <input type="number" ${common} value="${esc(val)}" step="${step}" ${min} ${max}>${hint}</div>`;
}

function collectParams(form) {
  const out = {};
  form.querySelectorAll('[data-type]').forEach((el) => {
    const key = el.name;
    const type = el.dataset.type;
    if (type === 'bool') out[key] = el.checked;
    else if (type === 'int' || type === 'seed') out[key] = el.value === '' ? null : parseInt(el.value, 10);
    else if (type === 'float') out[key] = el.value === '' ? null : parseFloat(el.value);
    else out[key] = el.value;
  });
  Object.keys(out).forEach((k) => { if (out[k] === null) delete out[k]; });
  return out;
}

async function viewRun(id) {
  view.innerHTML = `<div class="empty">Loading workflow…</div>`;
  let wf, inputs;
  try {
    [wf, inputs] = await Promise.all([api.workflow(id), api.inputs().then((r) => r.inputs || [])]);
  } catch (e) {
    view.innerHTML = `<div class="alert danger">${esc(e.message)}</div>
      <a href="#/library">← back to library</a>`;
    return;
  }

  const runnable = wf.status === 'available' || wf.status === 'ready_to_test';
  const depRows = [
    ...(wf.models || []).map((m) =>
      `<tr><td class="mono">${esc(m.folder)}/${esc(m.file)}</td>
        <td>${esc(m.purpose || '')}</td>
        <td class="num">${fmtBytes(m.size_bytes)}</td>
        <td>${m.present === false ? '<span class="badge warn">missing</span>' : '<span class="badge ok">present</span>'}</td></tr>`),
  ].join('');

  view.innerHTML = `
    <div class="page-head">
      <div><a href="#/library" class="dim">← library</a>
        <h1 style="margin-top:6px">${esc(wf.name)}</h1>
        <p>${esc(wf.description)}</p></div>
      <div class="row">${badge(wf.status)}
        ${wf.test_status === 'passed' ? '<span class="badge ok">tested here</span>' : ''}</div>
    </div>

    ${!runnable ? `<div class="alert warn"><strong>Not runnable on this machine.</strong>
      ${wf.missing_models ? `Missing models: ${wf.missing_models}. ` : ''}
      ${(wf.missing_nodes || []).length ? `Missing nodes: ${wf.missing_nodes.join(', ')}.` : ''}
      <a href="#/models">Model manager</a></div>` : ''}

    <div class="run-layout">
      <form class="card" id="run-form">
        <div class="card-head"><h2>Parameters</h2>
          <span class="dim">${(wf.parameters || []).length} inputs</span></div>
        ${(wf.parameters || []).map((p) => paramField(p, undefined, inputs)).join('')}
        <div class="row" style="margin-top:6px">
          <button class="primary" type="submit" ${runnable ? '' : 'disabled'}>Run</button>
          <button type="button" class="ghost" id="reset">Reset</button>
        </div>
        <div id="form-errors"></div>
      </form>

      <div>
        <div class="card" id="run-status">
          <div class="card-head"><h2>Result</h2><span class="dim" id="run-stage"></span></div>
          <div class="empty">Nothing submitted yet.</div>
        </div>

        <div class="card" style="margin-top:var(--gap)">
          <div class="card-head"><h2>Dependencies</h2></div>
          <table><thead><tr><th>File</th><th>Purpose</th><th class="num">Size</th><th>State</th></tr></thead>
          <tbody>${depRows || '<tr><td colspan="4" class="dim">No model files required.</td></tr>'}</tbody></table>
          ${wf.notes ? `<p class="dim" style="margin:12px 0 0;font-size:12px">${esc(wf.notes)}</p>` : ''}
        </div>
      </div>
    </div>`;

  const form = document.getElementById('run-form');

  form.querySelectorAll('[data-rand]').forEach((b) => b.addEventListener('click', () => {
    form.querySelector(`[name="${b.dataset.rand}"]`).value = -1;
  }));

  form.querySelectorAll('[data-upload]').forEach((inp) => inp.addEventListener('change', async () => {
    const file = inp.files?.[0];
    if (!file) return;
    inp.disabled = true;
    try {
      const res = await api.upload(file);
      const sel = form.querySelector(`select[name="${inp.dataset.upload}"]`);
      sel.add(new Option(`${res.name} (${fmtBytes(res.size)})`, res.name, true, true));
      toast(`Uploaded ${res.name}`);
    } catch (e) {
      toast(e.message, true);
    } finally {
      inp.disabled = false;
      inp.value = '';
    }
  }));

  document.getElementById('reset').addEventListener('click', () => viewRun(id));

  form.addEventListener('submit', async (ev) => {
    ev.preventDefault();
    const btn = form.querySelector('button[type="submit"]');
    const errBox = document.getElementById('form-errors');
    errBox.innerHTML = '';
    btn.disabled = true;
    btn.textContent = 'Submitting…';
    try {
      const res = await api.submit(wf.id, collectParams(form), crypto.randomUUID());
      if (res.warnings?.length) {
        errBox.innerHTML = res.warnings.map((w) => `<div class="alert warn">${esc(w)}</div>`).join('');
      }
      if (!res.created) toast('This submission was already queued (idempotent retry)');
      watchJob(res.job.id, form, btn);
    } catch (e) {
      btn.disabled = false;
      btn.textContent = 'Run';
      if (e instanceof ApiError && e.fields) {
        errBox.innerHTML = `<div class="alert danger"><strong>Check the inputs:</strong><br>` +
          Object.entries(e.fields).map(([k, v]) => `<code>${esc(k)}</code>: ${esc(v)}`).join('<br>') + '</div>';
      } else {
        errBox.innerHTML = `<div class="alert danger">${esc(e.message)}</div>`;
      }
    }
  });
}

function watchJob(jobId, form, btn) {
  const status = document.getElementById('run-status');
  const stageEl = document.getElementById('run-stage');
  let done = false;

  const render = (j) => {
    const pct = Math.round((j.progress || 0) * 100);
    stageEl.textContent = j.stage || j.status;
    status.innerHTML = `
      <div class="card-head"><h2>Result</h2>${badge(j.status)}
        <span class="spacer"></span><span class="dim">${fmtDuration(j.duration_s)}</span></div>
      <div class="progress" style="margin-bottom:12px"><i style="width:${pct}%"></i></div>
      ${j.error ? `<div class="alert danger">${esc(j.error)}</div>` : ''}
      ${j.outputs?.length
        ? `<div class="result-grid">${j.outputs.map((o) => o.kind === 'video'
            ? `<div><video class="result-media" src="${mediaUrl(o)}" controls loop></video>
                 <div class="row" style="margin-top:6px"><a href="${mediaUrl(o)}&download=1" class="dim">download</a></div></div>`
            : o.kind === 'audio'
            ? `<div><audio class="result-media" src="${mediaUrl(o)}" controls preload="metadata"></audio>
                 <div class="row" style="margin-top:6px"><a href="${mediaUrl(o)}&download=1" class="dim">download</a></div></div>`
            : o.kind === 'text'
            ? `<div><a class="file-card" href="${mediaUrl(o)}" target="_blank" rel="noopener">${esc(o.filename)}</a>
                 <div class="row" style="margin-top:6px"><a href="${mediaUrl(o)}&download=1" class="dim">download</a></div></div>`
            : `<div><a href="${mediaUrl(o)}" target="_blank" rel="noopener">
                 <img class="result-media" src="${mediaUrl(o)}" alt=""></a>
               <div class="row" style="margin-top:6px"><a href="${mediaUrl(o)}&download=1" class="dim">download</a></div></div>`).join('')}</div>`
        : `<div class="empty">${j.status === 'running' ? 'Generating…' : 'No output yet.'}</div>`}
      <div class="row" style="margin-top:12px">
        <code class="dim">job ${esc(j.id)}</code>
        <span class="spacer"></span>
        ${['queued', 'submitted', 'running'].includes(j.status)
          ? `<button class="small danger" id="cancel-btn">Cancel</button>` : ''}
        ${j.status === 'completed' ? `<button class="small" id="rerun-btn">Run again</button>` : ''}
      </div>`;

    status.querySelector('#cancel-btn')?.addEventListener('click', async (ev) => {
      ev.target.disabled = true;
      try { await api.cancelJob(j.id); toast('Cancellation requested'); }
      catch (e) { toast(e.message, true); ev.target.disabled = false; }
    });
    status.querySelector('#rerun-btn')?.addEventListener('click', async () => {
      try { const nj = await api.rerunJob(j.id); location.hash = `#/run/${nj.workflow_id}`; toast('Queued again'); }
      catch (e) { toast(e.message, true); }
    });
  };

  const tick = async () => {
    if (done) return;
    try {
      const j = await api.job(jobId);
      render(j);
      if (['completed', 'failed', 'cancelled'].includes(j.status)) {
        done = true;
        updateNavCounts();
        if (form) { btn.disabled = false; btn.textContent = 'Run'; }
        if (j.status === 'completed') toast('Job completed'); else toast(`Job ${j.status}`, true);
        return;
      }
    } catch (e) {
      if (e.status === 404) { done = true; return; }
    }
    setTimeout(tick, 1200);
  };
  tick();
}

/* ------------------------------------------------------------------ jobs */

async function viewJobs() {
  const data = await api.jobs({ limit: 60 });
  view.innerHTML = `
    <div class="page-head"><div><h1>Jobs</h1>
      <p>Durable queue. One heavy job runs at a time.</p></div>
      <button class="small ghost" id="reload">Refresh</button></div>
    <div class="card">
      <table><thead><tr>
        <th>Job</th><th>Workflow</th><th>Status</th><th class="num">Progress</th>
        <th class="num">Duration</th><th>Created</th><th></th></tr></thead>
      <tbody>
        ${(data.jobs || []).map((j) => `<tr data-job="${esc(j.id)}">
          <td class="mono dim">${esc(j.id.slice(0, 8))}</td>
          <td>${esc(j.workflow_name || j.workflow_id)}</td>
          <td>${badge(j.status)}</td>
          <td class="num">${Math.round((j.progress || 0) * 100)}%</td>
          <td class="num">${fmtDuration(j.duration_s)}</td>
          <td class="dim">${fmtWhen(j.created_at)}</td>
          <td class="row" style="gap:4px">
            ${['queued', 'submitted', 'running'].includes(j.status)
              ? `<button class="small danger" data-cancel="${esc(j.id)}">cancel</button>` : ''}
            ${j.status === 'completed' ? `<button class="small ghost" data-rerun="${esc(j.id)}">rerun</button>` : ''}
            <a class="small" href="#/run/${esc(j.workflow_id)}">open</a>
          </td></tr>`).join('') || '<tr><td colspan="7" class="empty">No jobs yet.</td></tr>'}
      </tbody></table>
    </div>
    ${(data.jobs || []).some((j) => j.error) ? `<div class="card" style="margin-top:var(--gap)">
      <div class="card-head"><h2>Errors</h2></div>
      ${data.jobs.filter((j) => j.error).map((j) =>
        `<div class="alert danger"><code>${esc(j.id.slice(0, 8))}</code> ${esc(j.error)}</div>`).join('')}
    </div>` : ''}`;

  document.getElementById('reload').addEventListener('click', () => route());
  view.querySelectorAll('[data-cancel]').forEach((b) => b.addEventListener('click', async () => {
    b.disabled = true;
    try { await api.cancelJob(b.dataset.cancel); toast('Cancelling'); setTimeout(route, 600); }
    catch (e) { toast(e.message, true); b.disabled = false; }
  }));
  view.querySelectorAll('[data-rerun]').forEach((b) => b.addEventListener('click', async () => {
    b.disabled = true;
    try { await api.rerunJob(b.dataset.rerun); toast('Queued again'); setTimeout(route, 600); }
    catch (e) { toast(e.message, true); b.disabled = false; }
  }));
}

/* ------------------------------------------------------------------ gallery */

async function viewGallery() {
  const data = await api.gallery({ limit: 120 });
  view.innerHTML = `
    <div class="page-head"><div><h1>Gallery</h1>
      <p>Everything this studio has produced, read from the local output folder.</p></div>
      <div class="dim">${data.total} outputs</div></div>
    <div class="gallery">
      ${(data.outputs || []).map((o) => `<figure>
        ${o.kind === 'video'
          ? `<video src="${mediaUrl(o)}" controls preload="metadata"></video>`
          : o.kind === 'audio'
          ? `<audio src="${mediaUrl(o)}" controls preload="metadata"></audio>`
          : o.kind === 'text'
          ? `<a class="file-card" href="${mediaUrl(o)}" target="_blank" rel="noopener">${esc(o.filename)}</a>`
          : `<a href="${mediaUrl(o)}" target="_blank" rel="noopener"><img src="${mediaUrl(o)}" loading="lazy" alt=""></a>`}
        <figcaption>
          <span title="${esc(o.prompt || '')}">${esc(o.workflow_name || o.workflow_id)}</span>
          <a href="${mediaUrl(o)}&download=1" class="dim">save</a>
        </figcaption>
      </figure>`).join('') || '<div class="empty">Nothing here yet — run a workflow.</div>'}
    </div>`;
}

/* ------------------------------------------------------------------ models */

async function viewModels() {
  const data = await api.models();
  const byFolder = {};
  for (const m of data.models || []) (byFolder[m.folder] ||= []).push(m);

  const rows = Object.entries(byFolder).map(([folder, files]) => `
    <tr><td colspan="4" style="background:var(--bg-raised);font-weight:600">
      ${esc(folder)} <span class="dim">(${files.length})</span></td></tr>
    ${files.map((m) => `<tr>
      <td class="mono">${esc(m.file)}</td>
      <td class="num">${fmtBytes(m.size_bytes)}</td>
      <td>${m.used_by?.length
        ? m.used_by.map((w) => `<a href="#/run/${esc(w)}" class="tag" style="text-decoration:none">${esc(w)}</a>`).join(' ')
        : '<span class="dim">unused by any workflow</span>'}</td>
      <td>${m.license ? `<span class="dim">${esc(m.license)}</span>` : '<span class="dim">—</span>'}</td>
    </tr>`).join('')}`).join('');

  view.innerHTML = `
    <div class="page-head"><div><h1>Models</h1>
      <p>Read directly from ComfyUI's model folders. Sizes are real, measured on disk.</p></div>
      <div class="dim">${data.file_count} files · ${data.total_gb} GB · ${data.empty_folders?.length || 0} empty folders</div></div>

    ${data.missing_referenced?.length ? `<div class="alert warn">
      <strong>Referenced by a workflow but not installed:</strong>
      ${data.missing_referenced.map((f) => `<code>${esc(f)}</code>`).join(', ')}</div>` : ''}

    <div class="card">
      <table><thead><tr><th>File</th><th class="num">Size</th><th>Used by</th><th>License</th></tr></thead>
      <tbody>${rows || '<tr><td colspan="4" class="empty">No models installed.</td></tr>'}</tbody></table>
    </div>

    <div class="alert info" style="margin-top:var(--gap)">
      Downloads are approval-gated: the studio will only fetch an exact plan you have confirmed,
      from a curated official source, with a free-space check before writing. Nothing is
      downloaded from this screen automatically.
    </div>`;
}

/* ------------------------------------------------------------------ settings */

async function viewSettings() {
  const s = await api.settings();
  const cur = s.settings || s;
  const field = (key, label, value, type = 'text', hint = '') => `
    <div class="field"><label for="s-${key}">${label}</label>
      <input id="s-${key}" name="${key}" type="${type}" value="${esc(value ?? '')}">
      ${hint ? `<div class="hint">${hint}</div>` : ''}</div>`;

  view.innerHTML = `
    <div class="page-head"><div><h1>Settings</h1>
      <p>Stored locally. The studio binds to 127.0.0.1 and never calls a cloud service.</p></div></div>
    <form class="card" id="settings-form" style="max-width:560px">
      ${field('comfy_url', 'ComfyUI endpoint', cur.comfy_url)}
      ${field('comfy_timeout_s', 'Health timeout (s)', cur.comfy_timeout_s, 'number')}
      ${field('max_upload_mb', 'Max upload size (MB)', cur.max_upload_mb, 'number')}
      ${field('output_root', 'Output folder', cur.output_root)}
      <div class="row"><button class="primary" type="submit">Save</button>
        <span class="dim" id="saved"></span></div>
    </form>
    <div class="card" style="margin-top:var(--gap);max-width:560px">
      <div class="card-head"><h2>What this app never does</h2></div>
      <ul class="muted" style="margin:0;padding-left:18px;font-size:12px;line-height:1.9">
        <li>No telemetry, no external inference, no account.</li>
        <li>No path outside the project's approved folders can be read or written.</li>
        <li>No model is downloaded without an approved plan.</li>
        <li>ComfyUI is never started, stopped or replaced by this app.</li>
      </ul>
    </div>`;

  document.getElementById('settings-form').addEventListener('submit', async (ev) => {
    ev.preventDefault();
    const form = ev.target;
    const patch = {};
    new FormData(form).forEach((v, k) => {
      if (v === '') return;
      patch[k] = /^\d+(\.\d+)?$/.test(v) ? Number(v) : v;
    });
    try {
      await api.saveSettings(patch);
      document.getElementById('saved').textContent = 'saved';
      toast('Settings saved');
      setTimeout(() => route(), 700);
    } catch (e) { toast(e.message, true); }
  });
}

/* ------------------------------------------------------------------ router */

const VIEWS = {
  dashboard: viewDashboard,
  library: viewLibrary,
  jobs: viewJobs,
  gallery: viewGallery,
  models: viewModels,
  settings: viewSettings,
};

async function route() {
  const hash = location.hash.replace(/^#\/?/, '') || 'dashboard';
  const [head, arg] = hash.split('/');
  renderNav(head === 'run' ? 'library' : head);

  try {
    if (head === 'run' && arg) {
      await viewRun(decodeURIComponent(arg));
    } else if (VIEWS[head]) {
      await VIEWS[head]();
    } else {
      location.hash = '#/dashboard';
      return;
    }
  } catch (e) {
    view.innerHTML = `<div class="alert danger"><strong>${esc(e.message)}</strong>
      ${e.status ? `<div class="hint">HTTP ${e.status}</div>` : ''}</div>`;
  }
  document.querySelector('.main')?.scrollTo?.(0, 0);
  window.scrollTo(0, 0);
  updateNavCounts();
}

window.addEventListener('hashchange', route);
route();
