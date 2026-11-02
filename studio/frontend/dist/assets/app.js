// Local AI Creator Studio — single-page front end, no build step, no framework.
// Views render into #view; state lives in this module. Hash routing keeps deep links working
// (#/run/txt2img-turbo) so a workflow can be bookmarked.

import { api, ApiError, auth, UNAUTHORIZED, randomId } from './api.js';

const view = document.getElementById('view');
const nav = document.getElementById('nav');
const loginScreen = document.getElementById('login');
const userChip = document.getElementById('user-chip');

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

/** "waiting ~3 min" — the ETA the server derives from this workflow's recent run times. */
function fmtEta(j, position) {
  if (j.eta_seconds == null) return '';
  const eta = j.eta_seconds < 90 ? `${Math.max(1, Math.round(j.eta_seconds))}s`
                                 : `${Math.round(j.eta_seconds / 60)} min`;
  const ahead = position ? ` · ${position} ahead` : '';
  return `~${eta}${ahead}`;
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
  ['discover', 'Find workflows', '⌕'],
  ['sources', 'Sources', '⇄', true],   // admin-only: it edits sources and starts syncs
  ['create', 'Create workflow', '✚'],
  ['jobs', 'Jobs', '≣'],
  ['gallery', 'Gallery', '▦'],
  ['models', 'Models', '▣'],
  ['settings', 'Settings', '⚙', true],   // admin-only: model downloads and engine controls live here
];

function renderNav(active) {
  const isAdmin = auth.isAdmin();
  nav.innerHTML = ROUTES
    .filter(([, , , adminOnly]) => isAdmin || !adminOnly)
    .map(([id, label, ico]) =>
      `<a class="nav-item" href="#/${id}" ${active === id ? 'aria-current="page"' : ''}>
       <span class="ico">${ico}</span><span>${label}</span>
       <span class="count" data-count="${id}"></span>
     </a>`).join('');
}

function renderUserChip() {
  const user = auth.current();
  if (!userChip) return;
  userChip.hidden = !user;
  const nameEl = document.getElementById('user-name');
  if (nameEl && user) {
    nameEl.textContent = user.display_name || user.username;
    // The label is a hint, not a permission: the API refuses what the role may not do anyway.
    nameEl.title = user.is_admin ? 'administrator'
      : auth.isRestricted() ? 'limited profile' : 'member';
  }
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

  let h, s, allowance;
  try {
    // /api/me/usage is the one place the allowance is computed. The dashboard only prints what
    // it is given — a limit that exists in the UI but not on the server would be a lie.
    [h, s, allowance] = await Promise.all([api.health(), api.stats(),
                                           api.usage().catch(() => null)]);
  } catch (e) {
    view.innerHTML = `<div class="page-head"><div><h1>Dashboard</h1></div></div>
      <div class="alert danger">Cannot reach the studio backend: ${esc(e.message)}</div>`;
    return;
  }

  const engine = h.comfy_connected
    ? `<span class="badge ok"><span class="dot"></span>connected</span>`
    : `<span class="badge danger"><span class="dot"></span>offline</span>`;

  const recent = await api.gallery({ limit: 8 }).catch(() => ({ outputs: [] }));

  // Allowance card: every number comes from /api/me/usage, nothing is recomputed here.
  const lm = allowance?.limits ?? null;
  const gb = (n) => (n == null ? '—' : (n / 1e9).toFixed(1));
  const meter = (used, cap) => {
    if (!cap || cap <= 0) return '';
    const pct = Math.min(100, (used / cap) * 100);
    const tone = pct >= 100 ? ' danger' : pct >= 80 ? ' warn' : '';
    return `<div class="meter${tone}"><i style="width:${pct.toFixed(1)}%"></i></div>`;
  };
  const allowanceCard = lm ? `
      <div class="card">
        <div class="card-head"><h2>Your allowance</h2><a href="#/jobs" class="dim">your jobs →</a></div>
        <div class="grid cols-2" style="gap:10px">
          <div>
            <div class="k dim">Runs today</div>
            <div class="num" style="font-size:var(--step-2)">${lm.jobs_today ?? 0}<span class="dim" style="font-size:var(--step-0)">
              ${lm.max_jobs_per_day == null ? '· no limit' : '/ ' + lm.max_jobs_per_day}</span></div>
            ${meter(lm.jobs_today || 0, lm.max_jobs_per_day)}
          </div>
          <div>
            <div class="k dim">Output space used</div>
            <div class="num" style="font-size:var(--step-2)">${gb(allowance.outputs?.bytes)}<span class="dim" style="font-size:var(--step-0)">
              ${lm.max_output_bytes == null ? 'GB · no limit' : '/ ' + gb(lm.max_output_bytes) + ' GB'}</span></div>
            ${meter(allowance.outputs?.bytes || 0, lm.max_output_bytes)}
          </div>
        </div>
        <p class="dim" style="margin:14px 0 0;font-size:12px">
          ${lm.active_jobs ?? 0} of ${lm.max_active_jobs == null ? '∞' : lm.max_active_jobs} slots busy ·
          ${s.jobs?.queued ?? 0} waiting
        </p>
      </div>` : '';

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
      ${allowanceCard}
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
  const [cats, list, studio] = await Promise.all([
    api.categories(),
    api.workflows(libState),
    api.studio.catalog().catch(() => ({ mine: [], editor: null })),
  ]);
  const mine = studio.mine || [];

  view.innerHTML = `
    <div class="page-head"><div><h1>Workflow library</h1>
      <p>Every entry is a graph validated against the live engine before it is offered.</p></div>
      <div class="dim">${list.length} shown / ${cats.total} total</div></div>

    <section class="mine-block">
      <div class="card-head"><h2>My workflows</h2>
        <a class="btn small" href="#/create">Create or import →</a></div>
      <p class="dim">Made in Studio or imported from ComfyUI. Edit them in ComfyUI's editor,
        then bring the file back — Studio checks it and keeps the new revision.</p>
      <div class="wf-grid">
        ${mine.map((w) => `
          <article class="wf-card mine" data-studio="${esc(w.id)}">
            <div class="row" style="justify-content:space-between">
              <h3>${esc(w.name)}</h3>
              <span class="badge ${w.runnable ? 'ok' : 'warn'}">${w.runnable ? 'runnable' : 'needs work'}</span>
            </div>
            <div class="desc">${esc(w.description || 'No description')}</div>
            <div class="meta">
              <span class="tag">${esc(w.format)} format</span>
              ${w.category ? `<span class="tag">${esc(w.category)}</span>` : ''}
              <span class="tag">${w.node_count} nodes</span>
              <span class="dim">edited ${esc((w.updated_at || '').slice(0, 16).replace('T', ' '))}</span>
            </div>
            <div class="row wf-actions">
              <button class="small primary" data-act="run">Run</button>
              <button class="small" data-act="open">Open in ComfyUI</button>
              <button class="small" data-act="edit">Edit</button>
              <button class="small ghost" data-act="duplicate">Duplicate</button>
              <button class="small ghost" data-act="validate">Validate</button>
              <a class="btn small ghost" href="${esc(api.studio.exportUrl(w.id, 'ui'))}" data-act="export-ui">UI JSON</a>
              <a class="btn small ghost" href="${esc(api.studio.exportUrl(w.id, 'api'))}" data-act="export-api">API JSON</a>
              <button class="small danger" data-act="delete">Delete</button>
            </div>
            <div class="editor-slot" hidden></div>
          </article>`).join('') || `<div class="empty">Nothing yet — generate one or import a file.</div>`}
      </div>
    </section>

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
  view.querySelectorAll('.wf-card:not(.mine)').forEach((card) => {
    card.addEventListener('click', () => { location.hash = `#/run/${card.dataset.id}`; });
  });

  view.querySelectorAll('[data-studio]').forEach((card) => {
    const id = card.dataset.studio;
    card.querySelectorAll('[data-act]').forEach((el) => {
      const act = el.dataset.act;
      if (act === 'export-ui' || act === 'export-api') return;   // plain download links
      el.addEventListener('click', async (e) => {
        e.stopPropagation();
        const slot = card.querySelector('.editor-slot');
        try {
          if (act === 'run') {
            location.hash = `#/run/${id}`;
          } else if (act === 'open') {
            const opened = await api.studio.openInEditor(id);
            toast(`Handed to ComfyUI as ${opened.filename}`);
            window.open(opened.editor_url, '_blank', 'noopener');
          } else if (act === 'validate') {
            const full = await api.studio.get(id);
            slot.hidden = false;
            slot.innerHTML = validationPanel(
              await api.studio.validateJson({ data: full.ui, filename: `${full.name}.json` }));
          } else if (act === 'duplicate') {
            const copy = await api.studio.duplicate(id);
            toast(`Duplicated as “${copy.record?.name}”`);
            route();
          } else if (act === 'delete') {
            if (!confirm('Delete this workflow? Revisions stay on disk until the next cleanup.')) return;
            await api.studio.remove(id);
            toast('Deleted');
            route();
          } else if (act === 'edit') {
            const full = await api.studio.get(id);
            slot.hidden = false;
            slot.innerHTML = `
              <div class="field"><label>Name</label><input id="ed-name" value="${esc(full.name)}"></div>
              <div class="field"><label>Description</label>
                <input id="ed-desc" value="${esc(full.description || '')}"></div>
              <div class="field"><label>UI workflow JSON</label>
                <textarea id="ed-json" rows="10" class="mono">${esc(JSON.stringify(full.ui, null, 2))}</textarea>
                <div class="hint">The graph itself is edited in ComfyUI — this is for inspecting or
                  pasting a corrected file.</div></div>
              <div class="row"><button class="primary small" id="ed-save">Save changes</button>
                <button class="small ghost" id="ed-cancel">Close</button></div>`;
            slot.querySelector('#ed-cancel').addEventListener('click', () => { slot.hidden = true; });
            slot.querySelector('#ed-save').addEventListener('click', async (ev) => {
              ev.target.disabled = true;
              try {
                await api.studio.update(id, {
                  name: slot.querySelector('#ed-name').value,
                  description: slot.querySelector('#ed-desc').value,
                  json_text: slot.querySelector('#ed-json').value,
                });
                toast('Saved as a new revision');
                route();
              } catch (err) { toast(err.message, true); ev.target.disabled = false; }
            });
          }
        } catch (err) {
          toast(err.message, true);
        }
      });
    });
  });
}

/* ------------------------------------------------------------ create workflow */

const studioState = { generated: null, imported: null, filename: null, savedId: null };

function validationPanel(report) {
  if (!report) return '';
  const list = (items, cls) => (items && items.length
    ? `<ul class="vlist">${items.map((i) => `<li class="${cls}">${esc(i)}</li>`).join('')}</ul>` : '');
  const models = (report.missing_models || []).map((m) => `${m.folder ? m.folder + '/' : ''}${m.file}`);
  return `
    <div class="card">
      <div class="card-head"><h3>Check result</h3>
        <div class="row">
          <span class="badge ${report.valid ? 'ok' : 'danger'}">${report.valid ? 'valid' : 'invalid'}</span>
          <span class="badge ${report.runnable ? 'ok' : 'warn'}">${report.runnable ? 'runnable' : 'not runnable yet'}</span>
          <span class="tag">${esc(report.format || 'unknown')} format</span>
        </div>
      </div>
      ${list(report.errors, 'bad')}
      ${list(report.warnings, 'warn')}
      ${list((report.missing_nodes || []).map((n) => `missing node class: ${n}`), 'bad')}
      ${list(models.map((m) => `missing model: ${m}`), 'bad')}
      ${list((report.missing_custom_nodes || []).map((n) => `missing custom node: ${n}`), 'bad')}
      ${report.valid ? `<div class="hint">Nothing blocks this workflow: its nodes exist, its links resolve,
        and every model it names is installed. It has <strong>not</strong> been run.</div>` : ''}
    </div>`;
}

const jsonPreview = (obj) => `<details class="preview"><summary>Workflow JSON (${Object.keys(obj || {}).length ? 'non-empty' : 'empty'})</summary>
  <pre>${esc(JSON.stringify(obj || {}, null, 2).slice(0, 6000))}</pre></details>`;

function download(obj, name) {
  const blob = new Blob([JSON.stringify(obj, null, 2)], { type: 'application/json' });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = name;
  a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 5000);
}

async function viewCreate() {
  const [tpl, editor] = await Promise.all([
    api.studio.templates(),
    api.studio.editorFiles().catch(() => ({ files: [] })),
  ]);
  const usable = tpl.templates.filter((t) => t.available);
  const blocked = tpl.templates.filter((t) => !t.available);

  view.innerHTML = `
    <div class="page-head"><div><h1>Create workflow</h1>
      <p>Studio writes and checks the graph; ComfyUI is where you edit it. Nothing is run from here.</p></div>
      <a class="btn small" href="http://${location.hostname}:8189/" target="_blank" rel="noopener">Open ComfyUI Editor ↗</a>
    </div>

    <div class="grid cols-2">
      <section class="card">
        <div class="card-head"><h2>Option 1 — Create with generator</h2></div>
        <div class="field">
          <label for="gen-desc">Describe the workflow</label>
          <textarea id="gen-desc" rows="3" placeholder="Create an image generation workflow using SDXL Turbo"></textarea>
          <div class="hint">Plain words. The generator matches them to a template it can actually build
            on this machine — it never invents a node.</div>
        </div>
        <div class="field">
          <label for="gen-tpl">Template</label>
          <select id="gen-tpl">
            <option value="">Pick automatically</option>
            ${usable.map((t) => `<option value="${esc(t.id)}">${esc(t.name)} — ${esc(t.category)}</option>`).join('')}
          </select>
          ${blocked.length ? `<div class="hint">Not buildable here: ${blocked.map((t) =>
            `<strong>${esc(t.name)}</strong> (${esc(t.reason)})`).join('; ')}</div>` : ''}
        </div>
        <div class="field"><label for="gen-name">Save as</label>
          <input id="gen-name" placeholder="optional name"></div>
        <div class="row"><button class="primary" id="gen-run">Generate workflow</button></div>
      </section>

      <section class="card">
        <div class="card-head"><h2>Option 2 — Import existing workflow</h2></div>
        <div class="field">
          <label for="imp-file">JSON file</label>
          <div id="imp-drop" class="drop">Drop a workflow .json here, or
            <input type="file" id="imp-file" accept=".json,application/json"></div>
          <div class="hint">Both formats are accepted: a UI workflow (the file ComfyUI exports for
            editing) and an API prompt. Nothing is queued automatically.</div>
        </div>
        <div class="field"><label for="imp-name">Save as</label>
          <input id="imp-name" placeholder="defaults to the file name"></div>
        <div class="row"><button class="primary" id="imp-check">Check file</button></div>
      </section>
    </div>

    <div id="gen-result"></div>
    <div id="imp-result"></div>

    <section class="card">
      <div class="card-head"><h2>Finished in ComfyUI? Bring it back</h2>
        <button class="small ghost" id="editor-refresh">Refresh</button></div>
      <p class="dim">Files in ComfyUI's own workflow folder. Save in the editor, then import here —
        Studio validates it and keeps a copy under your account.</p>
      <div id="editor-list">${editor.files.length
        ? editor.files.map((f) => `<div class="row file-chip"><span>${esc(f.name)}</span>
            <span class="dim">${fmtBytes(f.size)}${f.mine ? ' · from Studio' : ''}</span>
            <button class="small" data-import="${esc(f.name)}">Import into Studio</button></div>`).join('')
        : `<div class="empty">No saved workflows yet.</div>`}</div>
    </section>`;

  const resultBox = document.getElementById('gen-result');

  const showGenerated = (data, savedId) => {
    studioState.generated = data;
    studioState.savedId = savedId || null;
    resultBox.innerHTML = `
      <div class="card">
        <div class="card-head"><h3>${esc(data.name)}</h3>
          <div class="row"><span class="tag">${esc(data.template_name || '')}</span>
          <span class="tag">${esc(data.category || '')}</span>
          <span class="tag">${(data.ui?.nodes || []).length} nodes</span></div></div>
        <div class="row">
          <button class="primary" id="gen-save">Save to Studio</button>
          <button id="gen-open">Open in ComfyUI</button>
          <button class="ghost" id="gen-dl-ui">Download UI JSON</button>
          <button class="ghost" id="gen-dl-api">Download API JSON</button>
          ${studioState.savedId ? `<a class="btn small" href="#/run/${esc(studioState.savedId)}">Run it →</a>` : ''}
        </div>
        ${jsonPreview(data.ui)}
      </div>
      ${validationPanel(data.validation)}`;

    document.getElementById('gen-save').addEventListener('click', async (e) => {
      e.target.disabled = true;
      try {
        const saved = await api.studio.generate({
          description: document.getElementById('gen-desc').value,
          template_id: data.template_id,
          name: document.getElementById('gen-name').value || data.name,
          save: true,
        });
        toast('Saved to your workflows');
        showGenerated(saved, saved.record?.id);
      } catch (err) { toast(err.message, true); e.target.disabled = false; }
    });
    document.getElementById('gen-open').addEventListener('click', async (e) => {
      e.target.disabled = true;
      try {
        let id = studioState.savedId;
        if (!id) {
          const saved = await api.studio.generate({
            description: document.getElementById('gen-desc').value,
            template_id: data.template_id,
            name: document.getElementById('gen-name').value || data.name,
            save: true,
          });
          id = saved.record?.id;
          studioState.savedId = id;
        }
        const opened = await api.studio.openInEditor(id);
        toast(`Handed to ComfyUI as ${opened.filename}`);
        window.open(opened.editor_url, '_blank', 'noopener');
      } catch (err) { toast(err.message, true); }
      e.target.disabled = false;
    });
    document.getElementById('gen-dl-ui').addEventListener('click',
      () => download(studioState.generated.ui, `${studioState.generated.template_id}.ui.json`));
    document.getElementById('gen-dl-api').addEventListener('click',
      () => download(studioState.generated.api, `${studioState.generated.template_id}.api.json`));
  };

  document.getElementById('gen-run').addEventListener('click', async (e) => {
    const description = document.getElementById('gen-desc').value.trim();
    const template_id = document.getElementById('gen-tpl').value || undefined;
    if (!description && !template_id) { toast('Describe the workflow first', true); return; }
    e.target.disabled = true;
    resultBox.innerHTML = '<div class="card dim">Building the graph from live node schemas…</div>';
    try {
      showGenerated(await api.studio.generate({ description, template_id }), null);
    } catch (err) {
      const missing = err.payload?.missing_models || [];
      resultBox.innerHTML = `<div class="alert warn"><strong>Cannot build that here.</strong>
        <div>${esc(err.message)}</div>${missing.length ? `<div class="hint">Missing: ${missing.map(esc).join(', ')}</div>` : ''}</div>`;
    }
    e.target.disabled = false;
  });

  /* ---- import ---- */
  const impBox = document.getElementById('imp-result');
  let importedText = null;

  const showImported = (data, savedId) => {
    studioState.imported = data;
    // `/api/wf/validate` answers with the report itself, `/api/wf/import` nests it under
    // `validation`; the card must not care which door the answer came through.
    const report = data.validation || data;
    impBox.innerHTML = `
      <div class="card">
        <div class="card-head"><h3>${esc(studioState.filename || 'workflow.json')}</h3>
          <span class="tag">${esc(report.format || '')} format</span></div>
        <div class="row">
          ${report.valid ? `<button class="primary" id="imp-save">Save to Studio</button>` : ''}
          ${savedId ? `<button id="imp-open">Open in ComfyUI</button>
            <a class="btn small" href="#/run/${esc(savedId)}">Run it →</a>` : ''}
        </div>
        ${report.valid ? '' : `<div class="hint">Nothing is saved while the check reports errors.</div>`}
      </div>
      ${validationPanel(report)}`;
    const saveBtn = document.getElementById('imp-save');
    if (saveBtn) saveBtn.addEventListener('click', async (e) => {
      e.target.disabled = true;
      try {
        const saved = await api.studio.importJson({
          json_text: importedText,
          filename: studioState.filename,
          name: document.getElementById('imp-name').value || undefined,
          save: true,
        });
        toast('Saved to your workflows');
        showImported(saved, saved.record?.id);
      } catch (err) { toast(err.message, true); e.target.disabled = false; }
    });
    const openBtn = document.getElementById('imp-open');
    if (openBtn) openBtn.addEventListener('click', async () => {
      try {
        const opened = await api.studio.openInEditor(savedId);
        toast(`Handed to ComfyUI as ${opened.filename}`);
        window.open(opened.editor_url, '_blank', 'noopener');
      } catch (err) { toast(err.message, true); }
    });
  };

  const readFile = async (file) => {
    studioState.filename = file.name;
    importedText = await file.text();
    try {
      const result = await api.studio.validateJson({ json_text: importedText, filename: file.name });
      showImported(result, null);
    } catch (err) {
      impBox.innerHTML = `<div class="alert danger"><strong>${esc(err.message)}</strong>
        <div class="hint">A workflow file is JSON. If it came from another program, export it from
        ComfyUI as a workflow first.</div></div>`;
    }
  };

  const fileInput = document.getElementById('imp-file');
  fileInput.addEventListener('change', (e) => { if (e.target.files[0]) readFile(e.target.files[0]); });
  const drop = document.getElementById('imp-drop');
  ['dragover', 'dragenter'].forEach((evt) => drop.addEventListener(evt, (e) => {
    e.preventDefault(); drop.classList.add('over');
  }));
  ['dragleave', 'drop'].forEach((evt) => drop.addEventListener(evt, () => drop.classList.remove('over')));
  drop.addEventListener('drop', (e) => {
    e.preventDefault();
    const file = e.dataTransfer.files[0];
    if (file) readFile(file);
  });
  document.getElementById('imp-check').addEventListener('click', () => {
    if (!fileInput.files[0] && !importedText) { toast('Choose a file first', true); return; }
    if (fileInput.files[0]) readFile(fileInput.files[0]);
    else api.studio.validateJson({ json_text: importedText, filename: studioState.filename })
      .then((r) => showImported(r, null)).catch((err) => toast(err.message, true));
  });

  /* ---- editor round-trip ---- */
  const wireEditor = () => view.querySelectorAll('[data-import]').forEach((btn) => {
    btn.addEventListener('click', async () => {
      btn.disabled = true;
      try {
        const saved = await api.studio.importFromEditor({ filename: btn.dataset.import, save: true });
        const label = saved.record?.name || btn.dataset.import;
        toast(saved.updated ? `Updated ${label} — the previous version is kept` : `Imported ${label}`);
        route();
      } catch (err) {
        toast(err.message, true);
        btn.disabled = false;
      }
    });
  });
  wireEditor();
  document.getElementById('editor-refresh').addEventListener('click', () => route());
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
      ${auth.canUpload() ? `<div class="row" style="margin-top:6px">
        <input type="file" accept="image/png,image/jpeg,image/webp" data-upload="${esc(p.key)}"
               style="flex:1;font-size:11px;padding:4px">
      </div>` : ''}${hint}${p.alpha_warning ? '' : ''}</div>`;
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
    if (!auth.canUpload()) {           // the API would answer 403; say so before the upload starts
      toast('uploads are disabled for this account', true);
      inp.value = '';
      return;
    }
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
      // randomId(), not the native generator: the studio runs over plain http on the LAN,
      // where crypto.randomUUID does not exist. See api.js.
      const res = await api.submit(wf.id, collectParams(form), randomId());
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
        <span class="spacer"></span>
        <span class="dim">${j.status === 'queued' || j.status === 'submitted'
          ? fmtEta(j, j.queue_position) : fmtDuration(j.duration_s)}</span></div>
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
          <td>${badge(j.status)}${['queued', 'submitted'].includes(j.status) && j.eta_seconds != null
            ? `<div class="dim" style="font-size:11px;margin-top:2px">${esc(fmtEta(j, j.queue_position))}</div>` : ''}</td>
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

/* --------------------------------------------------- smart workflow discovery */

const SOURCE_STATUS = {
  ok: ['ok', 'reachable'],
  partial: ['warn', 'partial'],
  new: ['neutral', 'never synced'],
  unknown: ['neutral', 'not checked'],
  unreachable: ['danger', 'unreachable'],
  error: ['danger', 'error'],
  blocked: ['warn', 'rate limited'],
  unsupported: ['neutral', 'not supported'],
};

const sourceBadge = (status) => {
  const [cls, label] = SOURCE_STATUS[status] || ['neutral', status || 'unknown'];
  return `<span class="badge ${cls}">${esc(label)}</span>`;
};

const COMPAT_BADGE = {
  READY: ['ok', 'compatible'],
  PARTIAL: ['warn', 'partly compatible'],
  MISSING_MODELS: ['warn', 'models missing'],
  MISSING_NODES: ['danger', 'custom nodes missing'],
  INCOMPATIBLE: ['danger', 'incompatible'],
  UNKNOWN: ['neutral', 'not checked'],
};

const compatBadge = (status) => {
  const [cls, label] = COMPAT_BADGE[status] || COMPAT_BADGE.UNKNOWN;
  return `<span class="badge ${cls}">${esc(label)}</span>`;
};

const discoverState = {
  q: '', type: '', category: '', source: '', base_model: '',
  compatible_only: false, scope: '', sort: 'relevance', offset: 0, limit: 12,
  selected: [], result: null,
  error: null,          // { status, head, said } from the last rejected search, or null
  filtersOpen: null,    // null = follow the viewport; true/false = the user decided
};

const isNarrow = () => window.matchMedia('(max-width: 700px)').matches;

/** Turn a rejected search into something a person can act on, without losing the screen. */
function searchFailure(err) {
  const status = (err && err.status) || 0;
  const said = (err && err.message) || '';
  const known = {
    400: 'The search was rejected — check the filters and try again.',
    401: 'Your session has expired. Reload the page and sign in again.',
    403: 'You are not allowed to search this catalogue.',
    429: 'Too many searches in a row. Wait a moment and try again.',
    500: 'The server failed while searching. Nothing was lost.',
    502: 'The server is not answering right now.',
    503: 'The server is busy. Try again shortly.',
  };
  const head = known[status] || (status ? `The search failed (HTTP ${status}).` : 'The server could not be reached.');
  return { status, head, said: said && said !== `HTTP ${status}` ? said : '' };
}

/** How many filters are on — shown on the toggle so a collapsed panel never hides state. */
function activeFilterCount() {
  return [discoverState.type, discoverState.category, discoverState.source, discoverState.base_model]
    .filter(Boolean).length
    + (discoverState.compatible_only ? 1 : 0)
    + (discoverState.scope ? 1 : 0)
    + (discoverState.sort !== 'relevance' ? 1 : 0);
}

function discoveryCard(item, favorites) {
  const isFav = favorites.includes(item.id);
  const reasons = (item.explain && item.explain.reasons) || [];
  const stats = item.stats || {};
  const popular = stats.downloadCount || stats.downloads || stats.likes || 0;
  const picked = discoverState.selected.includes(item.id);
  const compat = (item.compatibility && item.compatibility.status) || '';
  return `
    <article class="wf-card disc-card" data-id="${esc(item.id)}" data-local="${item.origin === 'local' ? '1' : ''}">
      <div class="disc-media">${item.preview_url
        ? `<img src="${esc(item.preview_url)}" alt="" loading="lazy">`
        : `<span class="dim">no preview</span>`}</div>
      <div class="row" style="justify-content:space-between">
        <h3>${esc(item.title)}</h3>
        <span class="row" style="gap:6px">${compat ? compatBadge(compat) : ''}${isFav ? '<span class="badge ok">★</span>' : ''}</span>
      </div>
      <div class="desc">${esc(item.description || 'No description')}</div>
      <div class="meta">
        <span class="tag">${esc(item.type)}</span>
        ${item.workflow_format ? `<span class="tag">${esc(item.workflow_format)}</span>` : ''}
        ${item.origin === 'local' ? '<span class="tag">local</span>' : `<span class="tag">${esc(item.source_id)}</span>`}
        ${(item.categories || []).map((c) => `<span class="tag">${esc(c.name || c)}</span>`).join('')}
        ${popular ? `<span class="tag">${esc(String(popular))} downloads</span>` : ''}
      </div>
      <div class="meta dim">
        <span>${esc(item.author || 'unknown author')}</span>
        <span>·</span>
        <span>${esc(item.license || 'license unknown')}</span>
        ${item.source_url ? `<span>·</span><a href="${esc(item.source_url)}" target="_blank" rel="noopener">source</a>` : ''}
      </div>
      ${reasons.length ? `<div class="hint">Why: ${esc(reasons.slice(0, 4).join('; '))}</div>` : ''}
      <div class="row wf-actions">
        ${item.origin === 'local'
          /* A workflow of your own catalogue has no discovery record behind it: Analyze, Compare and
             Favourite all answered "no such resource". Offer the catalogue instead of dead buttons. */
          ? '<a class="btn small ghost" href="#/library">Open in the library</a>'
          : `<button class="small" data-act="analyze" data-graph="${item.has_graph ? '1' : ''}">Analyze</button>
             <button class="small ghost" data-act="pick">${picked ? 'Selected' : 'Compare'}</button>
             ${item.has_graph ? '<button class="small primary" data-act="import">Import</button>' : ''}
             ${item.has_graph ? '<button class="small" data-act="adapt">Adapt</button>' : ''}
             <button class="small ghost" data-act="fav">${isFav ? 'Unfavourite' : 'Favourite'}</button>`}
      </div>
      <div class="editor-slot" hidden></div>
    </article>`;
}

async function viewDiscover() {
  const [status, sources, cats, favorites] = await Promise.all([
    api.discovery.status(),
    api.discovery.sources().catch(() => ({ sources: [] })),
    api.categories().catch(() => ({ categories: [] })),
    api.discovery.favorites().catch(() => ({ items: [] })),
  ]);
  let searchError = null;
  try {
    discoverState.result = await api.discovery.search({
      query: discoverState.q,
      type: discoverState.type || undefined,
      category: discoverState.category || undefined,
      source: discoverState.source || undefined,
      base_model: discoverState.base_model,
      compatible_only: discoverState.compatible_only,
      local_only: discoverState.scope === 'local',
      external_only: discoverState.scope === 'external',
      sort: discoverState.sort,
      limit: discoverState.limit,
      offset: discoverState.offset,
    });
    discoverState.error = null;
  } catch (err) {
    // A rejected search must never take the page down: the last good result set stays on screen,
    // every filter stays set, and the error appears next to the box with a retry.
    searchError = discoverState.error = searchFailure(err);
  }
  const result = discoverState.result || { total: 0, results: [], notes: [] };
  const filtersOpen = discoverState.filtersOpen === null ? !isNarrow() : discoverState.filtersOpen;
  const activeFilters = activeFilterCount();
  const favIds = (favorites.items || []).map((f) => f.id);
  const total = result.total || 0;
  const shown = (result.results || []).length;
  const from = shown ? discoverState.offset + 1 : 0;
  const to = discoverState.offset + shown;

  view.innerHTML = `
    <div class="page-head"><div><h1>Find a workflow</h1>
      <p>Search the local catalogue and the sources that actually answer. Nothing is downloaded
         until you import it, and nothing is imported without being checked.</p></div>
      <div class="dim">${shown ? `${from}–${to} of ${total}` : `${total} found`}</div></div>

    ${searchError ? `<div class="alert danger disc-error" role="alert">
      <div class="row" style="justify-content:space-between;gap:12px;flex-wrap:wrap;align-items:flex-start">
        <div>
          <strong>${esc(searchError.head)}</strong>
          ${searchError.said ? `<div class="mono dim">${esc(searchError.said)}</div>` : ''}
          ${(result.results || []).length
            ? '<div class="dim">Below are the results of the last search that worked. Your filters were kept.</div>'
            : '<div class="dim">No results to show yet — fix the search and try again.</div>'}
        </div>
        <button class="small" id="d-retry">Try again</button>
      </div></div>` : ''}

    <div class="toolbar disc-filters${filtersOpen ? ' is-open' : ''}" id="d-filters">
      <input type="search" id="d-q" placeholder="anime portrait, интерьер, upscale…"
             value="${esc(discoverState.q)}">
      <div class="disc-extra">
      <select id="d-type">
        <option value="">Any type</option>
        ${['workflow', 'lora', 'checkpoint', 'reference'].map((t) =>
          `<option value="${t}" ${discoverState.type === t ? 'selected' : ''}>${t}</option>`).join('')}
      </select>
      <select id="d-cat">
        <option value="">Any category</option>
        ${(cats.categories || []).map((c) =>
          `<option value="${esc(c.id)}" ${discoverState.category === c.id ? 'selected' : ''}>${esc(c.label)}</option>`).join('')}
      </select>
      <select id="d-src">
        <option value="">Any source</option>
        ${(sources.sources || []).map((s) =>
          `<option value="${esc(s.source_id)}" ${discoverState.source === s.source_id ? 'selected' : ''}>${esc(s.name)}</option>`).join('')}
      </select>
      <input type="search" id="d-base" placeholder="base model" value="${esc(discoverState.base_model)}">
      <label class="row dim" style="gap:6px"><input type="checkbox" id="d-compat"
        ${discoverState.compatible_only ? 'checked' : ''}> compatible only</label>
      <select id="d-scope">
        <option value="">Local + external</option>
        <option value="local" ${discoverState.scope === 'local' ? 'selected' : ''}>Local only</option>
        <option value="external" ${discoverState.scope === 'external' ? 'selected' : ''}>External only</option>
      </select>
      <select id="d-sort">
        <option value="relevance" ${discoverState.sort === 'relevance' ? 'selected' : ''}>By relevance</option>
        <option value="popular" ${discoverState.sort === 'popular' ? 'selected' : ''}>By popularity</option>
        ${['relevance', 'popular'].includes(discoverState.sort) ? '' :
          `<option value="${esc(discoverState.sort)}" selected>${esc(discoverState.sort)} (not accepted)</option>`}
      </select>
      </div>
      <button class="primary small" id="d-go">Search</button>
      <button class="small ghost disc-filters-toggle" id="d-filters-toggle"
              aria-expanded="${filtersOpen}" aria-controls="d-filters"
              title="Show or hide the filters">Filters${activeFilters ? ` <span class="badge ok">${activeFilters}</span>` : ''}</button>
    </div>

    ${(result.notes || []).length
      ? `<div class="alert info">${result.notes.map((n) => esc(n)).join('<br>')}</div>` : ''}
    ${result.backend ? `<div class="hint">Ranking backend: ${esc(typeof result.backend === 'string' ? result.backend : (result.backend.model || 'unknown'))}${status.index && !status.index.semantic ? ' (hashed n-grams — no embedding model is installed, so this is not semantic search)' : ''}</div>` : ''}

    <div class="row" id="d-selection" ${discoverState.selected.length ? '' : 'hidden'}>
      <span class="dim">${discoverState.selected.length} selected for comparison</span>
      <button class="small primary" id="d-compare">Compare</button>
      <button class="small ghost" id="d-clear">Clear</button>
    </div>

    <div class="wf-grid">
      ${(result.results || []).map((item) => discoveryCard(item, favIds)).join('')
        || '<div class="empty">Nothing matched. Try a broader word, or turn off a filter.</div>'}
    </div>

    <div class="row" style="justify-content:center;margin-top:14px">
      <button class="small" id="d-prev" ${discoverState.offset ? '' : 'disabled'}>← Previous</button>
      <button class="small" id="d-next" ${to < total ? '' : 'disabled'}>Next →</button>
    </div>
    <div id="d-panel"></div>`;

  const panel = document.getElementById('d-panel');
  const rerender = () => route();
  const apply = () => { discoverState.offset = 0; rerender(); };
  const bind = (id, ev, fn) => document.getElementById(id)?.addEventListener(ev, fn);
  bind('d-go', 'click', () => {
    discoverState.q = document.getElementById('d-q').value.trim();
    apply();
  });
  bind('d-q', 'keydown', (e) => { if (e.key === 'Enter') document.getElementById('d-go').click(); });
  bind('d-type', 'change', (e) => { discoverState.type = e.target.value; apply(); });
  bind('d-cat', 'change', (e) => { discoverState.category = e.target.value; apply(); });
  bind('d-src', 'change', (e) => { discoverState.source = e.target.value; apply(); });
  bind('d-base', 'change', (e) => { discoverState.base_model = e.target.value.trim(); apply(); });
  bind('d-compat', 'change', (e) => { discoverState.compatible_only = e.target.checked; apply(); });
  bind('d-scope', 'change', (e) => { discoverState.scope = e.target.value; apply(); });
  bind('d-sort', 'change', (e) => { discoverState.sort = e.target.value; apply(); });
  bind('d-retry', 'click', () => rerender());
  // Collapsing the filters is a local, instant action: no refetch, no lost results, and the choice
  // is remembered in discoverState so it survives every rerender.
  bind('d-filters-toggle', 'click', () => {
    const bar = document.getElementById('d-filters');
    const toggle = document.getElementById('d-filters-toggle');
    if (!bar || !toggle) return;
    const open = !bar.classList.contains('is-open');
    discoverState.filtersOpen = open;
    bar.classList.toggle('is-open', open);
    toggle.setAttribute('aria-expanded', String(open));
  });
  bind('d-prev', 'click', () => {
    discoverState.offset = Math.max(0, discoverState.offset - discoverState.limit); rerender();
  });
  bind('d-next', 'click', () => { discoverState.offset += discoverState.limit; rerender(); });
  bind('d-clear', 'click', () => { discoverState.selected = []; rerender(); });
  bind('d-compare', 'click', async () => {
    try {
      const body = await api.discovery.compare(discoverState.selected);
      panel.innerHTML = `
        <section class="mine-block"><div class="card-head"><h2>Comparison</h2>
          <button class="small ghost" id="cmp-close">Close</button></div>
          <table><thead><tr><th>Resource</th><th>Nodes</th><th>Compatibility</th><th>Checkpoints / LoRAs</th></tr></thead>
          <tbody>${(body.items || []).map((it) => `<tr>
            <td>${esc(it.title)}</td><td>${it.node_count}</td>
            <td>${compatBadge((it.compatibility || {}).status)}</td>
            <td class="mono">${esc([...(it.checkpoints || []), ...(it.loras || [])].join(', ') || 'none')}</td>
          </tr>`).join('')}</tbody></table>
          <p class="dim">Easiest to run: <strong>${esc((body.verdict?.easiest_to_run || {}).title || '—')}</strong>
             (${esc((body.verdict?.easiest_to_run || {}).why || '')})</p>
          <p class="dim">Simplest graph: <strong>${esc((body.verdict?.simplest || {}).title || '—')}</strong>
             (${esc((body.verdict?.simplest || {}).why || '')})</p>
        </section>`;
      panel.querySelector('#cmp-close').addEventListener('click', () => { panel.innerHTML = ''; });
    } catch (err) { toast(err.message, true); }
  });

  view.querySelectorAll('.disc-card').forEach((card) => {
    const id = card.dataset.id;
    const slot = card.querySelector('.editor-slot');
    card.querySelectorAll('[data-act]').forEach((el) => {
      el.addEventListener('click', async (ev) => {
        ev.stopPropagation();
        const act = el.dataset.act;
        try {
          if (act === 'pick') {
            const at = discoverState.selected.indexOf(id);
            if (at >= 0) discoverState.selected.splice(at, 1);
            else if (discoverState.selected.length >= 2) toast('Pick two at most', true);
            else discoverState.selected.push(id);
            rerender();
          } else if (act === 'fav') {
            if (favIds.includes(id)) await api.discovery.removeFavorite(id);
            else await api.discovery.addFavorite(id);
            rerender();
          } else if (act === 'analyze') {
            slot.hidden = false;
            slot.innerHTML = '<div class="hint">Checking against the installed models and nodes…</div>';
            const [detail, analysed] = await Promise.all([
              api.discovery.resource(id),
              el.dataset.graph ? api.discovery.analyze(id) : Promise.resolve(null),
            ]);
            const compat = ((analysed || detail).analysis || {}).compatibility || {};
            const analysis = (analysed || detail).analysis || {};
            slot.innerHTML = validationPanel({
              valid: compat.status === 'READY', runnable: compat.status === 'READY',
              format: detail.resource.workflow_format, errors: [], warnings: [],
              missing_nodes: analysis.missing_nodes || [],
              missing_models: (analysis.missing_models || []).map((m) => ({ file: m })),
            }) + `
              <div class="hint">${esc(compat.detail || '')}</div>
              <div class="meta">${Object.entries(analysis.classes || {})
                .map(([cls, n]) => `<span class="tag">${esc(cls)}${n > 1 ? ` ×${n}` : ''}</span>`).join('')}</div>
              <div class="meta">${[...(analysis.checkpoints || []), ...(analysis.loras || [])]
                .map((m) => `<span class="tag">${esc(m)}</span>`).join('')}</div>`;
          } else if (act === 'import') {
            const body = await api.discovery.importResource(id, {});
            toast(`Imported as “${body.record.name}” — it is in your workflows`);
            rerender();
          } else if (act === 'adapt') {
            slot.hidden = false;
            const plan = await api.discovery.plan(id);
            const ops = plan.plan.operations || [];
            if (!ops.length) {
              slot.innerHTML = `<div class="alert info">Nothing to change.
                ${esc((plan.plan.notes || []).join(' ') || 'Every model this graph needs is installed.')}</div>`;
              return;
            }
            slot.innerHTML = `
              <div class="field"><label>Proposed changes (the original resource stays untouched)</label>
                <div class="vlist">${ops.map((op) => `<div>${esc(op.target_node_id)}:
                  <span class="mono">${esc(op.old_value)}</span> → <span class="mono">${esc(op.new_value)}</span>
                  <div class="hint">${esc(op.reason)}</div></div>`).join('')}</div></div>
              <div class="row"><button class="small primary" data-adapt="apply">Apply to a copy</button>
                <button class="small ghost" data-adapt="close">Cancel</button></div>`;
            slot.querySelector('[data-adapt="close"]').addEventListener('click', () => { slot.hidden = true; });
            slot.querySelector('[data-adapt="apply"]').addEventListener('click', async (e2) => {
              e2.target.disabled = true;
              const done = await api.discovery.apply({ resource_id: id, save: true,
                                                       name: `${plan.resource_title} (adapted)` });
              toast(done.validation.valid ? 'Adapted and saved to your workflows'
                : 'Adapted, but the result still has problems — see the validation', !done.validation.valid);
              rerender();
            });
          }
        } catch (err) {
          if (act === 'analyze' && slot) {
            slot.innerHTML = `<div class="alert danger">Could not check this workflow:
              ${esc(err.message)}</div>`;
          }
          toast(err.message, true);
        }
      });
    });
  });
}

/* ------------------------------------------------------------ source management */

let sourcesPoll = null;

async function viewSources() {
  clearTimeout(sourcesPoll);
  const [status, list, sync, index] = await Promise.all([
    api.discovery.status(),
    api.discovery.sources(),
    api.discovery.syncStatus().catch(() => ({})),
    api.discovery.indexStatus().catch(() => ({})),
  ]);
  const isAdmin = auth.isAdmin();
  const rows = list.sources || [];
  const counts = status.sources || {};

  view.innerHTML = `
    <div class="page-head"><div><h1>Sources</h1>
      <p>Every source here is described by what it can actually do. A source that has never been
         contacted says so; nothing is labelled “connected” on faith.</p></div>
      <div class="dim">${counts.enabled}/${counts.total} enabled · ${counts.never_synced} never synced</div></div>

    <div class="grid cols-4">
      <div class="stat"><div class="num">${counts.total || 0}</div><div class="dim">registered</div></div>
      <div class="stat"><div class="num">${counts.ok || 0}</div><div class="dim">answered last check</div></div>
      <div class="stat"><div class="num">${status.resources?.total || 0}</div><div class="dim">resources stored</div></div>
      <div class="stat"><div class="num">${Math.round((index.coverage || 0) * 100)}%</div>
        <div class="dim">indexed (${esc(index.model || 'none')})</div></div>
    </div>

    ${isAdmin ? `<div class="toolbar">
      <button class="small primary" id="s-sync">Sync now</button>
      <button class="small" id="s-index">Rebuild index</button>
    </div>
    <form class="row add-source" id="s-add">
      <input type="url" id="s-url" placeholder="https://github.com/owner/repo" required>
      <input type="text" id="s-name" placeholder="Name (optional)">
      <button class="small" type="submit">Add source</button>
    </form>` : `<div class="alert info">You can read this page. Syncing and editing sources is for administrators.</div>`}

    <div class="hint" id="s-sync-line">Last sync run: ${esc(sync.last_run_at ? sync.last_run_at.slice(0, 19).replace('T', ' ') : 'never')}
      ${sync.running ? `· running now${sync.current ? ` (${esc(sync.current)})` : ''}` : ''}</div>

    <table class="sources-table">
      <thead><tr><th>Source</th><th>Priority</th><th>Status</th><th>Formats</th>
        <th>Last sync</th><th>Resources</th><th>Notes</th><th></th></tr></thead>
      <tbody>
        ${rows.map((s) => `<tr data-source="${esc(s.source_id)}">
          <td><strong>${esc(s.name)}</strong><div class="dim mono">${esc(s.base_url)}</div></td>
          <td>${esc(s.priority)}</td>
          <td>${sourceBadge(s.status)}${s.rate_limit_status ? `<div class="hint">${esc(s.rate_limit_status)}</div>` : ''}</td>
          <td class="mono">${esc((s.resource_types || []).join(', '))}${s.api_available ? '' : '<div class="dim">link discovery only</div>'}</td>
          <td>${esc(s.last_sync_at ? s.last_sync_at.slice(0, 16).replace('T', ' ') : 'never')}</td>
          <td>${s.resources_count || 0}</td>
          <td class="dim">${esc(s.last_error || s.notes || '')}</td>
          <td class="row" style="gap:6px">
            <button class="small ghost" data-src-act="test">Test</button>
            ${isAdmin ? `<label class="row dim" style="gap:4px">
              <input type="checkbox" data-src-act="toggle" ${s.enabled ? 'checked' : ''}> on</label>` : ''}
            ${s.in_catalog ? '' : `<button class="small danger" data-src-act="remove">Remove</button>`}
          </td>
        </tr>`).join('')}
      </tbody>
    </table>`;

  // A sync runs in the background, so the panel watches it: whatever started the run — the button
  // here or a request from elsewhere — the table must not keep saying "never synced".
  const here = location.hash;
  let sawRun = Boolean(sync.running);
  const watch = async () => {
    if (location.hash !== here) return;          // left the page: stop quietly
    const now = await api.discovery.syncStatus().catch(() => null);
    if (!now || location.hash !== here) return;
    const line = document.getElementById('s-sync-line');
    if (now.running) {
      sawRun = true;
      if (line) {
        line.textContent = `Last sync run: ${now.started_at ? now.started_at.slice(0, 19).replace('T', ' ') : 'running'}` +
          ` · running now${now.current ? ` (${now.current})` : ''}`;
      }
      sourcesPoll = setTimeout(watch, 3000);
      return;
    }
    if (sawRun) {
      const done = now.results || [];
      const ok = done.filter((r) => r.status === 'ok' || r.status === 'partial').length;
      const bad = done.filter((r) => r.status !== 'ok' && r.status !== 'partial');
      toast(`Sync finished: ${ok} source(s) answered${bad.length ? `, ${bad.length} failed` : ''}`,
            bad.length > 0);
      route();
      return;
    }
    sourcesPoll = setTimeout(watch, 5000);
  };
  sourcesPoll = setTimeout(watch, sync.running ? 100 : 2500);

  view.querySelectorAll('[data-source]').forEach((row) => {
    const id = row.dataset.source;
    row.querySelectorAll('[data-src-act]').forEach((el) => {
      el.addEventListener('change', async () => {
        if (el.dataset.srcAct !== 'toggle') return;
        try {
          await api.discovery.toggleSource(id, el.checked);
          toast(`${id} ${el.checked ? 'enabled' : 'disabled'}`);
        } catch (err) { toast(err.message, true); el.checked = !el.checked; }
      });
      el.addEventListener('click', async (ev) => {
        const act = el.dataset.srcAct;
        if (act === 'toggle') return;
        try {
          if (act === 'test') {
            el.disabled = true;
            const report = await api.discovery.testSource(id);
            toast(`${id}: ${report.status}${report.error ? ` — ${report.error}` : ''}`,
                  report.status !== 'ok');
            el.disabled = false;
            route();
          } else if (act === 'remove') {
            if (!confirm(`Remove the source ${id}?`)) return;
            await api.discovery.removeSource(id);
            toast('Removed'); route();
          }
        } catch (err) { toast(err.message, true); }
      });
    });
  });

  if (!isAdmin) return;
  document.getElementById('s-sync').addEventListener('click', async (ev) => {
    ev.target.disabled = true;
    try {
      const started = await api.discovery.sync({});
      toast(started.started?.length ? `Syncing ${started.started.length} source(s) in the background`
        : 'Nothing to sync — every source is disabled or already running');
    } catch (err) { toast(err.message, true); }
    ev.target.disabled = false;
    route();
  });
  document.getElementById('s-index').addEventListener('click', async (ev) => {
    ev.target.disabled = true;
    try {
      const body = await api.discovery.index();
      toast(`Index: ${body.resources} resources, ${body.embedded} embedded`);
    } catch (err) { toast(err.message, true); }
    ev.target.disabled = false;
    route();
  });
  document.getElementById('s-add').addEventListener('submit', async (ev) => {
    ev.preventDefault();
    try {
      const body = await api.discovery.addSource({
        base_url: document.getElementById('s-url').value,
        name: document.getElementById('s-name').value || undefined,
      });
      toast(`Added ${body.source.name}`);
      route();
    } catch (err) { toast(err.message, true); }
  });
}

const VIEWS = {
  dashboard: viewDashboard,
  library: viewLibrary,
  discover: viewDiscover,
  sources: viewSources,
  create: viewCreate,
  jobs: viewJobs,
  gallery: viewGallery,
  models: viewModels,
  settings: viewSettings,
};

let booted = false;

async function route() {
  if (!booted) return;
  const hash = location.hash.replace(/^#\/?/, '') || 'dashboard';
  const [head, arg] = hash.split('/');
  const entry = ROUTES.find(([id]) => id === head);
  if (entry?.[3] && !auth.isAdmin()) {
    toast('That screen is for administrators', true);
    location.hash = '#/dashboard';
    return;
  }
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

/* ------------------------------------------------------------------ sign-in */

function showLogin(message = '') {
  booted = false;
  view.innerHTML = '';
  nav.innerHTML = '';
  renderUserChip();
  document.querySelector('.shell').hidden = true;
  loginScreen.hidden = false;
  loginScreen.innerHTML = `
    <form class="login-card" id="login-form">
      <div class="brand"><strong>Creator Studio</strong><span>${esc(location.host)}</span></div>
      <h1>Sign in</h1>
      <p class="dim" style="margin:0 0 4px">
        House accounts only. Nothing here talks to the internet.</p>
      ${message ? `<div class="alert info">${esc(message)}</div>` : ''}
      <div class="field"><label for="lu">Username</label>
        <input id="lu" name="username" autocapitalize="none" spellcheck="false"
               autocomplete="username" required></div>
      <div class="field"><label for="lp">Password</label>
        <input id="lp" name="password" type="password" autocomplete="current-password" required></div>
      <button class="primary" type="submit" style="width:100%">Sign in</button>
      <div class="alert danger" id="login-error" hidden></div>
    </form>`;
  document.getElementById('lu').focus();
  document.getElementById('login-form').addEventListener('submit', async (ev) => {
    ev.preventDefault();
    const form = ev.target;
    const btn = form.querySelector('button');
    const err = document.getElementById('login-error');
    btn.disabled = true;
    err.hidden = true;
    try {
      await auth.login(form.username.value.trim(), form.password.value);
      booted = true;
      loginScreen.hidden = true;
      loginScreen.innerHTML = '';
      document.querySelector('.shell').hidden = false;
      renderUserChip();
      await route();
    } catch (e) {
      err.hidden = false;
      err.textContent = e.status === 401
        ? 'Wrong username or password.'
        : e.status === 429 ? e.message : `Could not sign in: ${e.message}`;
      form.password.value = '';
      form.password.focus();
    } finally {
      btn.disabled = false;
    }
  });
}

async function boot() {
  try {
    await auth.me();
  } catch (e) {
    if (e.status === 401) { showLogin(); return; }
    view.innerHTML = `<div class="alert danger"><strong>Cannot reach the studio backend:</strong>
      ${esc(e.message)}</div>`;
    return;
  }
  booted = true;
  document.querySelector('.shell').hidden = false;
  loginScreen.hidden = true;
  renderUserChip();
  await route();
}

document.getElementById('logout')?.addEventListener('click', async (ev) => {
  ev.preventDefault();
  try { await auth.logout(); } catch { /* already gone */ }
  location.hash = '#/dashboard';
  showLogin('Signed out.');
});

// The session can expire while a tab sits open; the first request that comes back 401 lands here.
window.addEventListener(UNAUTHORIZED, () => showLogin('Session expired — sign in again.'));
window.addEventListener('hashchange', route);
boot();
