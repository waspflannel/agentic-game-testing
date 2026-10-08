import { esc, icon, href, newId, api, parseRoute, legacyNewTarget, createEpoch, patchHTML, dirtyBridge, runGoal, runStatus } from './core.js';
import { sidebarModel, sidebarHTML } from './views/sidebar.js';
import { emptyHTML, noAgentsHTML, projectAgents, readinessKey, newDraft, composerModel, composerSkeleton, composerRegions, startInstance, checkStart } from './views/home.js';

const $ = selector => document.querySelector(selector);
const enc = encodeURIComponent;
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
const POLL_MS = 5000;
const LOST_AFTER_FAILURES = 3;

// The app partition is memory-only, so UI memory lives here rather than in browser storage.
const state = {
  data: null,
  route: parseRoute(location.hash),
  failures: 0,
  polling: false,
  ui: { expanded: new Map(), showAll: new Set(), lastProject: null },
  drafts: new Map(),   // projectId -> composer draft
  readiness: null,     // { key, loading, result, error } for the open composer; probed on view open only
  isCurrent: () => true,
};
const epoch = createEpoch();
const setDirty = dirtyBridge(() => window.ashadDesktop);

// ── Toasts and dialogs ──────────────────────────────────────────────────────
function toast(message, kind = 'info') {
  const node = document.createElement('div');
  node.className = `toast toast-${kind}`;
  node.innerHTML = `${icon(kind === 'error' ? 'alert' : kind === 'ok' ? 'check' : 'info')}<span>${esc(message)}</span>`;
  $('#toasts').append(node);
  setTimeout(() => node.remove(), kind === 'error' ? 8000 : 5000);
}

function confirmDialog({ title, body, confirm = 'Confirm', danger = false }) {
  const dialog = $('#dialog');
  const opener = document.activeElement;
  dialog.innerHTML = `<form method="dialog"><h2 id="dialog-title">${esc(title)}</h2><p>${esc(body)}</p>`
    + `<div class="dialog-actions"><button class="btn btn-secondary" value="cancel"${danger ? ' autofocus' : ''}>Cancel</button>`
    + `<button class="btn ${danger ? 'btn-danger-solid' : 'btn-primary'}" value="ok"${danger ? '' : ' autofocus'}>${esc(confirm)}</button></div></form>`;
  dialog.returnValue = '';
  dialog.showModal();
  return new Promise(resolve => dialog.addEventListener('close', () => {
    opener?.focus?.();
    resolve(dialog.returnValue === 'ok');
  }, { once: true }));
}

// ── Rendering ───────────────────────────────────────────────────────────────
function renderSidebar() {
  const model = sidebarModel(state.data, { ...state.ui, route: state.route, now: Date.now() / 1000 });
  patchHTML($('#sidebar'), sidebarHTML(model), document.activeElement);
}

function renderBanners() {
  const banners = [];
  if (state.failures >= LOST_AFTER_FAILURES) banners.push(`<div class="banner banner-danger" role="alert">${icon('alert')}<span>Lost connection to ASHAD. Data shown may be stale. Retrying…</span></div>`);
  if (state.data?.workspace_import?.interrupted) banners.push(`<div class="banner" role="alert">${icon('alert')}<span>Workspace import is interrupted. Changes are disabled until it is reconciled.</span></div>`);
  patchHTML($('#banners'), banners.join(''), document.activeElement);
}

const placeholder = ({ iconName = 'monitor', tag, title, body = '', extra = '' }) =>
  `<div class="placeholder">${icon(iconName)}${tag ? `<span class="tag">${esc(tag)}</span>` : ''}<h1>${esc(title)}</h1>${body ? `<p>${esc(body)}</p>` : ''}${extra}</div>`;

function notFound(what) {
  return placeholder({ iconName: 'alert', title: `${what} not found`, body: 'It may have been removed or belongs to another workspace.', extra: '<a class="btn btn-secondary" href="#/">Go home</a>' });
}

function viewFor(route, data) {
  const projects = data?.projects || [];
  const project = id => projects.find(p => p.id === id);
  switch (route.name) {
    case 'home': return projects.length ? noAgentsHTML(project(composerProject(route, data))) : emptyHTML(data);
    case 'composer': return project(route.id) ? noAgentsHTML(project(route.id)) : notFound('Project');
    case 'project':
      return project(route.id) ? placeholder({ iconName: 'folder-open', tag: 'Coming in C7', title: project(route.id).name, body: 'Builds, agent configurations, desktop readiness and recent instances.' }) : notFound('Project');
    case 'instance': {
      const run = (data?.runs || []).find(r => r.id === route.id);
      if (!run) return notFound('Instance');
      const status = runStatus(run, (data.instance_slot?.owners || []).find(o => o.run_id === run.id));
      return placeholder({ tag: 'Coming in C4', title: runGoal(run), extra: `<span class="badge badge-${status.tone}"><span class="dot dot-${status.tone}"></span>${esc(status.label)}</span>` });
    }
    case 'setup': return placeholder({ iconName: 'gamepad', tag: 'Coming in C8', title: 'Set up a game agent', body: 'Project, build, desktop, agent and model, tuning, instruction review.' });
    case 'usage': return placeholder({ iconName: 'chart-column', tag: 'Coming in C9', title: 'Usage' });
    case 'settings': return placeholder({ iconName: 'sliders', tag: 'Coming in C9', title: 'Settings' });
    case 'styleguide': return styleguide();
    default: return '';
  }
}

function windowTitle(route, data) {
  const projects = data?.projects || [];
  const composer = composerProject(route, data);
  if (composer) return projects.find(p => p.id === composer)?.name || '';
  if (route.name === 'instance') { const run = (data?.runs || []).find(r => r.id === route.id); return run ? runGoal(run) : ''; }
  if (route.name === 'project' || route.name === 'composer') return projects.find(p => p.id === route.id)?.name || '';
  return { setup: 'Setup', usage: 'Usage', settings: 'Settings', styleguide: 'Styleguide' }[route.name] || '';
}

function renderView() {
  const route = state.route;
  const title = windowTitle(route, state.data);
  $('#window-title').textContent = title;
  document.title = title ? `${title} · ASHAD` : 'ASHAD';
  $('#inspector').hidden = route.name !== 'instance';
  if (!state.data) return;
  const view = $('#view');
  const composer = composerProject(route, state.data);
  if (composer && projectAgents(state.data, composer).length) return renderComposer(view, composer);
  if (view.dataset.mount) { view.dataset.mount = ''; view.renderedHTML = undefined; }
  patchHTML(view, viewFor(route, state.data), document.activeElement);
}

// Views with live inputs mount a skeleton once per key and then patch named regions, so polls never re-create them.
function mount(view, key, html) {
  if (view.dataset.mount === key) return false;
  view.dataset.mount = key;
  view.innerHTML = html;
  view.renderedHTML = undefined;
  return true;
}

function patchRegions(root, regions) {
  for (const [name, html] of Object.entries(regions)) {
    const el = root.querySelector(`[data-region="${name}"]`);
    if (el) patchHTML(el, html, document.activeElement);
  }
}

// ── Composer (C3) ───────────────────────────────────────────────────────────
// The composer shows on #/p/<id>/new, and on #/ for the last-opened project.
function composerProject(route, data) {
  const projects = data?.projects || [];
  if (route.name === 'composer') return projects.some(p => p.id === route.id) ? route.id : null;
  if (route.name === 'home' && projects.length) return (projects.find(p => p.id === state.ui.lastProject) || projects[0]).id;
  return null;
}

function draftFor(projectId) {
  if (!state.drafts.has(projectId)) state.drafts.set(projectId, newDraft());
  return state.drafts.get(projectId);
}

function renderComposer(view, projectId) {
  const draft = draftFor(projectId);
  const agents = projectAgents(state.data, projectId);
  if (!agents.some(a => a.id === draft.agentId)) draft.agentId = agents[0].id;
  const agent = agents.find(a => a.id === draft.agentId);
  if (state.readiness?.key !== readinessKey(projectId, agent)) loadReadiness(projectId, agent);
  mount(view, `composer:${projectId}`, composerSkeleton(projectId, draft));
  patchRegions(view, composerRegions(composerModel(state.data, projectId, draft, state.readiness), draft));
}

async function loadReadiness(projectId, agent) {
  const record = state.readiness = { key: readinessKey(projectId, agent), loading: true };
  try { record.result = await api(`/api/projects/${enc(projectId)}/readiness?build_id=${enc(agent.build_id)}&agent_id=${enc(agent.id)}`); }
  catch (error) { record.error = error.message; }
  record.loading = false;
  if (state.readiness === record) renderView();
}

const dirtyComposer = () => {
  const id = composerProject(state.route, state.data);
  const draft = id && state.drafts.get(id);
  return draft && !draft.busy && draft.goal.trim() ? id : null;
};

async function startFromComposer(projectId) {
  const draft = draftFor(projectId);
  if (draft.busy) return;
  if (!composerModel(state.data, projectId, draft, state.readiness).canStart) { $('#composer-status')?.scrollIntoView({ block: 'nearest' }); return; }
  const current = state.isCurrent;
  draft.busy = true;
  draft.result = null;
  renderView();
  let result = await startInstance(draft, projectId, api);
  while (result.kind === 'in-progress') { await sleep(2000); result = await checkStart(draft, api); }
  draft.busy = false;
  const run = result.run;
  if (run?.id) state.data.runs = [run, ...state.data.runs.filter(r => r.id !== run.id)];
  if (result.kind === 'accepted') {
    state.drafts.delete(projectId);
    setDirty(false);
    if (current()) location.hash = href('i', run.id);
    else toast('Instance started.', 'ok');
    poll();
    return;
  }
  draft.result = result;
  if (result.kind === 'conflict') state.data.instance_slot = { ...state.data.instance_slot, available: false, owners: result.owners };
  if (!current()) return;
  renderView();
  if (result.field === 'goal') $('#composer-goal')?.focus();
}

function render() {
  renderBanners();
  if (!state.data) return;
  renderSidebar();
  renderView();
}

// ── Routing ─────────────────────────────────────────────────────────────────
function rememberProject(route) {
  const run = route.name === 'instance' ? (state.data?.runs || []).find(r => r.id === route.id) : null;
  const id = ['project', 'composer'].includes(route.name) ? route.id : run?.project_id;
  if (id && (state.data?.projects || []).some(p => p.id === id)) state.ui.lastProject = id;
}

let acceptedHash = location.hash;
async function onRoute() {
  const route = parseRoute(location.hash);
  if (route.name === 'redirect' || (route.name === 'legacy-new' && state.data)) {
    history.replaceState(null, '', route.name === 'redirect' ? route.to : legacyNewTarget(route.agent, state.data));
    return onRoute();
  }
  // Leaving a typed, unsent goal asks first (spec §11). Moving between #/ and the same project's composer is not leaving.
  const dirty = dirtyComposer();
  if (dirty && composerProject(route, state.data) !== dirty) {
    const target = location.hash;
    history.replaceState(null, '', acceptedHash);
    if (!await confirmDialog({ title: 'Discard this goal?', body: 'The goal you typed has not been started. Leaving discards it.', confirm: 'Discard goal', danger: true })) return;
    state.drafts.delete(dirty);
    setDirty(false);
    location.hash = target;
    return;
  }
  acceptedHash = location.hash;
  state.route = route;
  state.isCurrent = epoch.next();  // Any response started for the previous route is now stale.
  if (composerProject(route, state.data)) state.readiness = null;  // Readiness probes on view open, never on the poll.
  if (route.name === 'composer' && route.query.agent) draftFor(route.id).agentId = route.query.agent;
  rememberProject(route);
  render();
  $('#view').scrollTop = 0;
}

// ── Polling ─────────────────────────────────────────────────────────────────
async function poll() {
  if (state.polling) return;
  state.polling = true;
  try {
    state.data = await api('/api/bootstrap');
    state.failures = 0;
    if (state.route.name === 'legacy-new') return onRoute();
    rememberProject(state.route);
  } catch {
    state.failures += 1;
  } finally {
    state.polling = false;
  }
  render();
}

function schedule() {
  setTimeout(async () => { if (!document.hidden) await poll(); schedule(); }, POLL_MS);
}

// ── Events ──────────────────────────────────────────────────────────────────
function toggleSidebar() {
  const collapsed = document.body.classList.toggle('nav-collapsed');
  $('#nav-toggle').setAttribute('aria-expanded', String(!collapsed));
}

document.addEventListener('click', async event => {
  const target = event.target.closest('[data-action]');
  if (!target) return;
  const { action, id } = target.dataset;
  if (action === 'toggle-group') {
    state.ui.expanded.set(id, target.getAttribute('aria-expanded') !== 'true');
    renderSidebar();
  } else if (action === 'show-all') {
    state.ui.showAll.has(id) ? state.ui.showAll.delete(id) : state.ui.showAll.add(id);
    renderSidebar();
  } else if (action === 'composer-retry') {
    startFromComposer(composerProject(state.route, state.data));
  } else if (action === 'composer-recheck') {
    state.readiness = null;
    renderView();
  } else if (action === 'sg-toast') {
    toast(target.dataset.message, target.dataset.kind);
  } else if (action === 'sg-confirm') {
    const ok = await confirmDialog({ title: 'Stop this instance?', body: 'The bot stops and its desktop is torn down. Retained history stays readable.', confirm: 'Stop instance', danger: true });
    toast(ok ? 'Confirmed (styleguide only, nothing was stopped).' : 'Cancelled.', ok ? 'ok' : 'info');
  }
});

document.addEventListener('keydown', event => {
  if (event.ctrlKey && !event.shiftKey && !event.altKey && event.key.toLowerCase() === 'b') {
    event.preventDefault();
    toggleSidebar();
  }
  // Plain Enter inserts a newline; launching is costly and stays explicit.
  if (event.ctrlKey && event.key === 'Enter' && event.target.id === 'composer-goal') {
    event.preventDefault();
    event.target.form.requestSubmit();
  }
});

// Composer fields update the in-memory draft; the textarea itself is never re-rendered.
document.addEventListener('input', event => {
  const form = event.target.closest?.('[data-form="composer"]');
  if (!form || event.target.dataset.field !== 'goal') return;
  const draft = draftFor(form.dataset.project);
  draft.goal = event.target.value;
  draft.requestId ||= newId();  // Created once, when the draft is first edited.
  if (draft.result?.field === 'goal') draft.result = null;
  setDirty(Boolean(draft.goal.trim()));
  renderView();
});

document.addEventListener('change', event => {
  const field = event.target.dataset?.field;
  const projectId = composerProject(state.route, state.data);
  if (!projectId || !['agent', 'transport', 'project'].includes(field)) return;
  if (field === 'project') { location.hash = href('p', event.target.value, 'new'); return; }
  draftFor(projectId)[field === 'agent' ? 'agentId' : 'transport'] = event.target.value;
  renderView();
});

document.addEventListener('submit', event => {
  const form = event.target.closest('[data-form="composer"]');
  if (!form) return;
  event.preventDefault();
  startFromComposer(form.dataset.project);
});

$('#nav-toggle').addEventListener('click', toggleSidebar);
window.addEventListener('hashchange', onRoute);
document.addEventListener('visibilitychange', () => { if (!document.hidden) poll(); });

// ── Styleguide (temporary; removed in C10) ─────────────────────────────────
function styleguide() {
  const tokens = ['bg', 'bg-sidebar', 'surface', 'surface-2', 'surface-3', 'border', 'text', 'text-2', 'text-3', 'accent', 'live', 'attention', 'danger', 'muted'];
  const swatches = tokens.map(t => `<div class="swatch"><span style="background:var(--${t})"></span><div><strong>--${t}</strong><code>${esc(getComputedStyle(document.documentElement).getPropertyValue('--' + t).trim())}</code></div></div>`).join('');
  const statuses = [['live', 'Running'], ['attention', 'Paused'], ['attention', 'Needs review'], ['attention', 'You have control'], ['pending', 'Starting'], ['pending', 'Stopping'], ['muted', 'Ended'], ['danger', 'Failed']];
  return `<div class="page">
    <div class="page-head"><h1>Styleguide</h1><p>Every token and base component. Temporary page for the C1 look-and-feel review.</p></div>
    <section><h2 class="section-title">Colour tokens</h2><div class="sg-grid">${swatches}</div></section>
    <section class="sg-type"><h2 class="section-title">Type</h2>
      <p style="font-size:var(--fs-hero);font-weight:600">What should the agent do in Agronauts?</p>
      <p style="font-size:var(--fs-title);font-weight:600">Reach the second village and craft a stone pickaxe</p>
      <p>Body 13px. The bot selected an action; selection is not dispatch, and delivered input is not proof of effect.</p>
      <p style="font-size:var(--fs-meta);color:var(--text-2)">Meta 12px · observed 14s ago · coverage partial</p>
      <p class="mono">interact(target="door_3", action="open") · obs_8f2c1a</p>
    </section>
    <section><h2 class="section-title">Buttons</h2><div class="sg-row">
      <button class="btn btn-primary">${icon('play')}Resume</button>
      <button class="btn btn-secondary">${icon('pause')}Pause</button>
      <button class="btn btn-ghost">Load older activity</button>
      <button class="btn btn-danger" data-action="sg-confirm">${icon('stop')}Stop</button>
      <button class="btn btn-primary" aria-disabled="true">Start</button>
      <button class="btn btn-primary btn-round" aria-label="Start instance">${icon('arrow-up')}</button>
      <button class="icon-btn" aria-label="Toggle inspector" title="Toggle inspector">${icon('panel-right')}</button>
    </div></section>
    <section><h2 class="section-title">Status and chips</h2>
      <div class="sg-row">${statuses.map(([tone, label]) => `<span class="badge badge-${tone}"><span class="dot dot-${tone}"></span>${label}</span>`).join('')}</div>
      <div class="sg-row" style="margin-top:12px"><span class="chip">${icon('gamepad')}Scout agent</span><span class="chip">GPT-6 Astra</span><span class="chip">build 0.3.1</span><button class="chip">ENet</button><span class="chip"><span class="dot dot-live"></span>Desktop ready</span></div>
    </section>
    <section><h2 class="section-title">Card and form controls</h2><div class="sg-forms">
      <div class="card"><h3>Desktop readiness</h3><p style="color:var(--text-2)">ChatGPT signed in · build validated · desktop ready</p></div>
      <div class="field"><label for="sg-name">Configuration name</label><input id="sg-name" class="input" placeholder="Scout agent"><span class="hint">Shown in the agent selector.</span></div>
      <div class="field"><label for="sg-bad">Goal</label><textarea id="sg-bad" class="textarea" rows="2" aria-invalid="true" aria-describedby="sg-bad-err"></textarea><span id="sg-bad-err" class="error">Describe the goal before starting.</span></div>
      <div class="field"><label for="sg-model">Model</label><select id="sg-model" class="select"><option>GPT-6 Astra</option><option>GPT-6 Luna</option></select></div>
      <label class="check"><input type="checkbox" checked> I understand this creates a desktop container</label>
    </div></section>
    <section><h2 class="section-title">Feedback</h2><div class="sg-row">
      <button class="btn btn-secondary" data-action="sg-toast" data-kind="info" data-message="Readiness re-checked.">Info toast</button>
      <button class="btn btn-secondary" data-action="sg-toast" data-kind="ok" data-message="Instance paused.">Success toast</button>
      <button class="btn btn-secondary" data-action="sg-toast" data-kind="error" data-message="Stop failed: worker did not confirm teardown.">Error toast</button>
      <button class="btn btn-secondary" data-action="sg-confirm">Confirm dialog</button>
    </div></section>
  </div>`;
}

onRoute();
poll().then(schedule);
