import { esc, icon, href, newId, newOp, submitOnce, api, parseRoute, legacyNewTarget, createEpoch, createDrawer, patchHTML, dirtyBridge, runGoal, ago, modelLabel, since, nextOptionIndex } from './core.js';
import { sidebarModel, sidebarHTML } from './views/sidebar.js';
import { emptyHTML, noAgentsHTML, projectAgents, readinessKey, newDraft, draftModel, composerModel, composerSkeleton, composerRegions, startInstance, checkStart } from './views/home.js';
import { isActiveRun, playerIds, instanceState, stateAnnouncement, cameraTarget, createCamera, createRunSlot, runCommand, threadSkeleton, threadRegions, defaultTab, nextTab } from './views/instance.js';
import { panelRegions, mergeEvents, latestObservation } from './views/inspector.js';
import { newSetup, projectBuilds, applyDefaults, setupModel, setupDirty, discardSetup, getControls, setupSkeleton, setupRegions, prepareDialogText, prepareDesktop, registerBuild, elapsed } from './views/setup.js';
import { settingsSkeleton, accountSkeleton, accountRegions, accountBlocker, settleLogin, desktopsSkeleton, desktopsRegions, workerPayload, steamSkeleton, steamRegions, steamErrors,
  limitsSkeleton, limitsRegions, limitsPayload, SECTIONS } from './views/settings.js';
import { newUsageUi, usageSkeleton, usageRegions } from './views/usage.js';
import { newOverview, readinessDue, overviewModel, overviewSkeleton, overviewRegions } from './views/project.js';
import { PAGE, MAX_EVENTS, createTimeline, beginPage, applyPage, failPage, seedFromRun, applyLive, timelineCards, timelineRegions, syncCards, cardBody, rawJSON, eventIndex } from './views/timeline.js';

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
  ui: { expanded: new Map(), showAll: new Set(), lastProject: null, goalOpen: new Set() },
  drafts: new Map(),   // projectId -> composer draft
  readiness: null,     // { key, loading, result, error } for the open composer; probed on view open only
  isCurrent: () => true,
  runSlot: createRunSlot(),  // GET /api/runs/<id> for the open thread
  runMissing: false,
  commands: new Map(),       // `${runId}:${action}` -> { id, pending, error }
  camera: null,
  thread: {},                // per-open-thread UI: player, instruction draft, last status key, timeline, selected tab and per-tab scroll, world view
  setups: new Map(),         // project ID ('' before step 1) -> setup state; long operations survive leaving the page
  settingsDirty: false,
  account: { checking: false, busy: false, error: '', login: null },  // login: { auth_url, login_id } only while sign-in is pending
  settingsUi: { probing: new Set(), errors: new Map(), saving: false, saved: '', formError: '', steamBusy: new Map(), steamResults: new Map() },
  overviews: new Map(),      // project ID -> overview state (selected build, readiness, chooser, prepare, refreshes)
  usage: null,               // { loading, rows, error, at }; fetched on view open and Refresh only
  usageUi: newUsageUi(),
};
const epoch = createEpoch();
const setDirty = dirtyBridge(() => window.ashadDesktop);

// ── Announcements, toasts and dialogs ───────────────────────────────────────
// The single polite live region. Only control outcomes, state changes and errors reach it; never telemetry or ticks.
function announce(text) {
  const el = $('#announcer');
  el.textContent = '';
  setTimeout(() => { el.textContent = text; }, 50);  // Cleared first, so a repeated message is read again.
}

function toast(message, kind = 'info') {
  announce(message);
  const node = document.createElement('div');
  node.className = `toast toast-${kind}`;
  node.innerHTML = `${icon(kind === 'error' ? 'alert' : kind === 'ok' ? 'check' : 'info')}<span>${esc(message)}</span>`;
  $('#toasts').append(node);
  setTimeout(() => node.remove(), kind === 'error' ? 8000 : 5000);
}

// Resolves to the form's FormData on confirm, or null. html is trusted markup built with esc().
// Closing empties the dialog, so a shown password or typed text never outlives it in the DOM.
function openDialog({ title, body = '', html = '', confirm = 'Confirm', cancel = 'Cancel', danger = false, wide = false }) {
  const dialog = $('#dialog');
  const opener = document.activeElement;
  dialog.classList.toggle('dialog-wide', wide);
  dialog.innerHTML = `<form method="dialog"><h2 id="dialog-title">${esc(title)}</h2>${body ? `<p>${esc(body)}</p>` : ''}${html}`
    + `<div class="dialog-actions"><button class="btn btn-secondary" value="cancel" formnovalidate${danger || !confirm ? ' autofocus' : ''}>${esc(cancel)}</button>`
    + (confirm ? `<button class="btn ${danger ? 'btn-danger-solid' : 'btn-primary'}" value="ok"${danger ? '' : ' autofocus'}>${esc(confirm)}</button>` : '')
    + `</div></form>`;
  dialog.returnValue = '';
  dialog.showModal();
  return new Promise(resolve => dialog.addEventListener('close', () => {
    const data = dialog.returnValue === 'ok' ? new FormData(dialog.querySelector('form')) : null;
    dialog.innerHTML = '';
    opener?.focus?.();
    resolve(data);
  }, { once: true }));
}
const confirmDialog = options => openDialog(options).then(Boolean);

// ── Rendering ───────────────────────────────────────────────────────────────
function renderSidebar() {
  const model = sidebarModel(state.data, { ...state.ui, route: state.route, now: Date.now() / 1000 });
  patchHTML($('#sidebar'), sidebarHTML(model), document.activeElement);
}

// Patches a block and announces any error it newly shows (marked data-announce in the markup).
function patchAnnounced(el, html) {
  if (!patchHTML(el, html, document.activeElement)) return;
  const text = [...el.querySelectorAll('[data-announce]')].map(n => n.textContent.trim()).join(' ');
  if (text && text !== el.announced) announce(text);
  el.announced = text;
}

function renderBanners() {
  const banners = [];
  if (state.failures >= LOST_AFTER_FAILURES) banners.push(`<div class="banner banner-danger" data-announce>${icon('alert')}<span>Lost connection to ASHAD. Data shown may be stale. Retrying…</span></div>`);
  if (state.data?.workspace_import?.interrupted) banners.push(`<div class="banner" data-announce>${icon('alert')}<span>Workspace import is interrupted. Changes are disabled until it is reconciled.</span></div>`);
  patchAnnounced($('#banners'), banners.join(''));
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
    case 'project': return notFound('Project');  // renderOverview handles existing projects.
    case 'instance': return state.runMissing ? notFound('Instance') : `<div class="boot"><p>Loading instance…</p></div>`;
    case 'setup': return notFound('Project');  // renderSetup handles every setup route whose project exists.
    case 'settings': return notFound('Settings section');  // renderSettings handles the known sections.
    default: return '';
  }
}

function windowTitle(route, data) {
  const projects = data?.projects || [];
  const composer = composerProject(route, data);
  if (composer) return projects.find(p => p.id === composer)?.name || '';
  if (route.name === 'instance') { const run = threadRun(); return run ? runGoal(run) : ''; }
  if (route.name === 'project' || route.name === 'composer') return projects.find(p => p.id === route.id)?.name || '';
  return { setup: 'Setup', usage: 'Usage', settings: 'Settings' }[route.name] || '';
}

function renderView() {
  const route = state.route;
  const title = windowTitle(route, state.data);
  $('#window-title').textContent = title;
  document.title = title ? `${title} · ASHAD` : 'ASHAD';
  if (!state.data) return;
  const view = $('#view');
  if (route.name === 'instance' && threadRun()) return renderThread(view);
  stopCamera();
  const composer = composerProject(route, state.data);
  if (composer && projectAgents(state.data, composer).length) return renderComposer(view, composer);
  if (route.name === 'setup' && (!setupKey(route) || findProject(setupKey(route)))) return renderSetup(view);
  if (route.name === 'settings' && SECTIONS.some(([id]) => id === route.section)) return renderSettings(view);
  if (route.name === 'usage') return renderUsage(view);
  if (route.name === 'project' && findProject(route.id)) return renderOverview(view);
  if (view.dataset.mount) { view.dataset.mount = ''; view.renderedHTML = undefined; }
  patchAnnounced(view, viewFor(route, state.data));
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
    if (el) patchAnnounced(el, html);
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
  draft.model = draftModel(state.data, draft);
  if (state.readiness?.key !== readinessKey(projectId, agent, draft.model)) loadReadiness(projectId, agent, draft.model);
  mount(view, `composer:${projectId}`, composerSkeleton(projectId, draft));
  patchRegions(view, composerRegions(composerModel(state.data, projectId, draft, state.readiness), draft));
}

async function loadReadiness(projectId, agent, model) {
  const record = state.readiness = { key: readinessKey(projectId, agent, model), loading: true };
  try { record.result = await api(`/api/projects/${enc(projectId)}/readiness?build_id=${enc(agent.build_id)}&agent_id=${enc(agent.id)}&model=${enc(model)}`); }
  catch (error) { record.error = error.message; }
  record.loading = false;
  if (state.readiness === record) renderView();
}

const dirtyComposer = () => {
  const id = composerProject(state.route, state.data);
  const draft = id && state.drafts.get(id);
  return draft && !draft.busy && draft.goal.trim() ? id : null;
};
const dirtySetup = () => (state.route.name === 'setup' && setupDirty(state.setups.get(setupKey(state.route))) ? state.setups.get(setupKey(state.route)) : null);
const dirtySettings = () => state.route.name === 'settings' && state.settingsDirty;
const syncDirty = () => setDirty(Boolean(dirtyComposer() || dirtySetup() || dirtySettings()));

// Unsent input that moving to `route` would discard (spec §11). Moving within the same draft's view is not leaving.
function dirtyGuard(route) {
  const composer = dirtyComposer();
  if (composer && composerProject(route, state.data) !== composer) {
    return { title: 'Discard this goal?', body: 'The goal you typed has not been started. Leaving discards it.', confirm: 'Discard goal', discard: () => state.drafts.delete(composer) };
  }
  const s = dirtySetup();
  if (s && !(route.name === 'setup' && setupKey(route) === s.projectId)) {
    return { title: 'Discard setup changes?', body: 'What you typed in setup has not been saved. Leaving discards it; anything already created stays.', confirm: 'Discard changes', discard: () => discardSetup(s) };
  }
  const sameForm = route.name === 'settings' && route.section === state.route.section && (route.query.edit || '') === (state.route.query.edit || '');
  if (dirtySettings() && !sameForm) {
    return { title: 'Discard unsaved settings?', body: 'This form has changes that were not saved. Leaving discards them.', confirm: 'Discard changes', discard: () => { state.settingsDirty = false; } };
  }
  return null;
}

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
    syncDirty();
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

// ── Setup (C8) ──────────────────────────────────────────────────────────────
const setupKey = route => route.query?.project || '';
const findProject = id => (state.data?.projects || []).find(p => p.id === id);

function setupFor(route) {
  const key = setupKey(route);
  if (!state.setups.has(key)) state.setups.set(key, newSetup(key));
  return state.setups.get(key);
}

function renderSetup(view) {
  const s = setupFor(state.route);
  const builds = projectBuilds(state.data, s.projectId);
  if (!builds.some(b => b.id === s.buildId)) s.buildId = builds[0]?.id || '';
  applyDefaults(state.data, s);
  let m = setupModel(state.data, s);
  if (m.step === 2 && m.build && s.catalog?.buildId !== m.build.id) loadCatalog(s, m.build.id);
  if (m.step === 3 && m.build && s.readiness?.buildId !== m.build.id) { probeReadiness(s, s.projectId, m.build.id); m = setupModel(state.data, s); }
  s.step = m.step;
  if (mount(view, `setup:${s.projectId}:${m.step}`, setupSkeleton(state.data, s, m.step))) view.scrollTop = 0;
  patchRegions(view, setupRegions(state.data, s, m));
  // Submitted input is locked until its request settles, so a retry can only resend the same payload.
  const name = view.querySelector('#setup-name');
  if (name) name.readOnly = ['busy', 'lost'].includes(s.project.status);
  for (const field of view.querySelectorAll('#setup-agent-name')) field.disabled = Boolean(s.agent) || ['busy', 'lost'].includes(s.controls.status) || s.controls.waiting;
}

// Readiness probes the machine: on view open and explicit Re-check only. owner.readiness is replaced by each probe.
async function probeReadiness(owner, projectId, buildId) {
  const record = owner.readiness = { buildId, loading: true };
  try { record.result = await api(`/api/projects/${enc(projectId)}/readiness?build_id=${enc(buildId)}`); }
  catch (error) { record.error = error.message; }
  Object.assign(record, { loading: false, at: Date.now() / 1000 });
  if (record.result) owner.validation?.set(buildId, record.result.build_validation || {});  // The overview's per-build validation state.
  if (owner.readiness === record) renderView();
}

async function loadCatalog(s, buildId) {
  const record = s.catalog = { buildId, loading: true };
  try { record.result = await api(`/api/builds/${enc(buildId)}/catalog`); }
  catch (error) { record.error = error.message; }
  record.loading = false;
  if (s.catalog === record) renderView();
}

async function createProject(s) {
  const name = s.name.trim();
  if (!name || s.project.status === 'busy') return;
  const current = state.isCurrent;
  const pending = submitOnce(s.project, { name }, id => api('/api/projects', { method: 'POST', body: { name, request_id: id } }));
  renderView();
  await pending;
  syncDirty();
  if (s.project.status !== 'done') { if (current()) renderView(); return; }
  const created = s.project.value;
  state.data.projects = [...state.data.projects.filter(p => p.id !== created.id), created];
  state.setups.delete('');
  state.setups.set(created.id, newSetup(created.id));
  if (current()) location.hash = `#/setup?project=${enc(created.id)}`;
  else toast(`Project ${created.name} created.`, 'ok');
  poll();
}

// The chooser result applies only to the form that opened it; a cancelled chooser changes nothing.
async function addBuild(form, projectId, kind) {
  if (form.chooser.busy) return;
  const pending = registerBuild(form, projectId, kind, { bridge: window.ashadDesktop, api, onBusy: renderView });
  const build = await pending;
  if (build) {
    state.data.builds = [build, ...state.data.builds.filter(b => b.id !== build.id)];
    toast(`Build ${build.name || build.id} added.`, 'ok');
    poll();
  }
  renderView();
}

async function deleteBuild(id) {
  const build = (state.data.builds || []).find(b => b.id === id);
  if (!await confirmDialog({ title: `Remove ${build?.name || id}?`, body: 'ASHAD forgets this build. Its files and past instances are kept; project configurations gathered on it can no longer start.', confirm: 'Remove', danger: true })) return;
  try {
    await api(`/api/builds/${enc(id)}`, { method: 'DELETE' });
    state.data.builds = state.data.builds.filter(b => b.id !== id);
    toast('Build removed.', 'ok');
  } catch (error) { toast(error.message, 'error'); }
  renderView();
  poll();
}

async function deleteInstance(id) {
  const run = (state.data.runs || []).find(r => r.id === id);
  const goal = run ? runGoal(run) : id;
  if (!await confirmDialog({ title: 'Delete this instance?', body: `"${goal}" and its timeline, report and saved evidence are removed from this computer. Usage totals are kept. This cannot be undone.`, confirm: 'Delete', danger: true })) return;
  try {
    const result = await api(`/api/runs/${enc(id)}`, { method: 'DELETE' });
    state.data.runs = state.data.runs.filter(r => r.id !== id);
    for (const key of [...state.commands.keys()]) if (key.startsWith(`${id}:`)) state.commands.delete(key);
    if (state.route.name === 'instance' && state.route.id === id) location.hash = '#/';
    toast(result.files_removed === false ? 'Instance deleted. Some files could not be removed.' : 'Instance deleted.', 'ok');
  } catch (error) { toast(error.message, 'error'); }
  render();
  poll();
}

async function deleteProject(id) {
  const project = (state.data.projects || []).find(p => p.id === id);
  const name = project?.name || id;
  const count = kind => (state.data[kind] || []).filter(r => r.project_id === id).length;
  const [runs, agents, builds] = [count('runs'), count('agents'), count('builds')];
  if (!await confirmDialog({ title: `Delete ${name}?`, body: `Deletes the project, its ${runs} instances, ${agents} project configurations and ${builds} build registrations. Build files on disk and usage totals are kept. This cannot be undone.`, confirm: 'Delete project', danger: true })) return;
  try {
    const result = await api(`/api/projects/${enc(id)}`, { method: 'DELETE' });
    for (const kind of ['runs', 'agents', 'builds']) state.data[kind] = (state.data[kind] || []).filter(r => r.project_id !== id);
    state.data.projects = state.data.projects.filter(p => p.id !== id);
    if (['project', 'composer'].includes(state.route.name) && state.route.id === id) location.hash = '#/';
    state.setups.delete(id); state.drafts.delete(id); state.overviews.delete(id); state.ui.expanded.delete(id); state.ui.showAll.delete(id);
    if (state.ui.lastProject === id) state.ui.lastProject = null;
    toast(result.files_removed === false ? `${name} deleted. Some files could not be removed.` : `${name} deleted.`, 'ok');
  } catch (error) { toast(error.message, 'error'); }
  render();
  poll();
}

async function deleteAgent(id) {
  const agent = (state.data.agents || []).find(a => a.id === id);
  if (!agent) return;
  const name = agent.name || id;
  const gathered = agent.created_at ? since(agent.created_at) : 'at an unrecorded time';
  if (!await confirmDialog({ title: `Delete ${name}?`, body: `Controls gathered ${gathered}. ASHAD forgets this project configuration and its game controls file. Instances that used it stay in history but are blocked until you get game controls again for this project and start them with the new configuration. This cannot be undone.`, confirm: 'Delete configuration', danger: true })) return;
  try {
    await api(`/api/agents/${enc(id)}`, { method: 'DELETE' });
    state.data.agents = state.data.agents.filter(a => a.id !== id);
    for (const ov of state.overviews.values()) ov.refreshes.delete(id);
    // A setup that finished while its page was closed still shows this configuration; it starts over at step 4.
    for (const s of state.setups.values()) if (s.agent?.id === id) Object.assign(s, { agent: null, controls: newOp() });
    toast(`${name} deleted.`, 'ok');
  } catch (error) { toast(error.message, 'error'); }
  render();
  poll();
}

async function prepare(owner, projectId, build) {
  if (!build || owner.prepare.status === 'busy') return;
  if (!await confirmDialog({ title: 'Prepare desktop?', body: prepareDialogText(build), confirm: 'Prepare desktop' })) return;
  const pending = prepareDesktop(owner.prepare, projectId, build.id, api);
  renderView();
  await pending;
  if (owner.prepare.status === 'done') {
    toast('Desktop prepared. Re-checking readiness.', 'ok');
    owner.prepare = newOp();  // The next preparation is a new request.
    owner.readiness = null;   // A user-started follow-up, not a poll: the open view probes again.
    poll();
  }
  renderView();
}

function onSetupAction(action, target, event) {
  const s = setupFor(state.route);
  const off = target.getAttribute('aria-disabled') === 'true';
  if (action === 'setup-finish') {  // The link navigates; the finished setup is dropped by onRoute.
    syncDirty();
    return;
  }
  if (off) return;
  if (action === 'setup-step') {
    s.step = Number(target.dataset.step);
    renderView();
    $('#setup-step-title')?.focus();
  } else if (action === 'setup-create') createProject(s);
  else if (action === 'setup-add-build') addBuild(s, s.projectId, target.dataset.kind);
  else if (action === 'setup-recheck') { s.readiness = null; renderView(); }
  else if (action === 'setup-prepare') prepare(s, s.projectId, setupModel(state.data, s).build);
  else if (action === 'setup-controls') startControls(s);
  else if (action === 'setup-stop-waiting') { s.controls.stopWaiting = true; renderView(); }
}

// While Get game controls runs, its progress is read every 3s from the recovery route: no probe, no inference.
async function watchControls(op) {
  while (op.status === 'busy' || op.waiting) {
    if (op.status === 'busy' && op.requestId && !document.hidden) {
      try {
        const r = await api(`/api/agents/controls/${enc(op.requestId)}`);
        if (r?.status === 'in_progress') op.progress = r.progress || null;
      } catch { /* The next tick retries; the run itself is unaffected. */ }
      renderView();
    }
    await sleep(3000);
  }
}

async function startControls(s) {
  if (s.controls.status === 'busy' || s.controls.waiting || !setupModel(state.data, s).signedIn || !s.agentName.trim()) return;
  const name = s.agentName.trim();
  const payload = { build_id: s.buildId, project_id: s.projectId, name };
  const pending = getControls(s.controls, payload, api, ms => { renderView(); return sleep(ms); });
  watchControls(s.controls);
  renderView();
  syncDirty();
  await pending;
  if (s.controls.status === 'done') {
    s.agent = s.controls.value;
    state.data.agents = [s.agent, ...state.data.agents.filter(a => a.id !== s.agent.id)];
    if (!(state.route.name === 'setup' && setupKey(state.route) === s.projectId && s.step === 4)) toast(`Game controls ready for ${name}.`, 'ok');
    poll();
  }
  syncDirty();
  renderView();
}

// ── Settings and Usage (C9) ─────────────────────────────────────────────────
const controlsActive = () => [...state.setups.values()].map(s => s.controls).concat([...state.overviews.values()].flatMap(ov => [...ov.refreshes.values()]))
  .some(op => op.status === 'busy' || op.waiting);

function renderSettings(view) {
  const { section, query } = state.route;
  const ui = state.settingsUi;
  const now = Date.now() / 1000;
  let key = `settings:${section}`, inner, regions;
  if (section === 'account') {
    inner = accountSkeleton();
    regions = accountRegions({ ai: state.data.ai, claude: state.data.ai_claude, ...state.account, blocker: accountBlocker(state.data, controlsActive()), modelLabel: (state.data.model_options || []).find(m => m.id === state.data.ai?.model)?.label });
  } else if (section === 'desktops') {
    const edit = (state.data.workers || []).some(w => w.id === query.edit) ? query.edit : '';
    key += `:${edit}`;
    inner = desktopsSkeleton(state.data, edit);
    regions = desktopsRegions(state.data, { ...ui, editId: edit }, now);
  } else if (section === 'steam') {
    inner = steamSkeleton(state.data);
    regions = steamRegions(state.data, { busy: ui.steamBusy, results: ui.steamResults, formError: ui.formError, saved: ui.saved }, now);
  } else {
    inner = limitsSkeleton(state.data);
    regions = limitsRegions(state.data, ui);
  }
  mount(view, key, settingsSkeleton(section, inner));
  patchRegions(view, regions);
}

// Field errors render next to their inputs (by name) and the first invalid field takes focus.
function showErrors(form, errors) {
  for (const el of form.querySelectorAll('[data-error-for]')) {
    const input = form.querySelector('#' + el.dataset.errorFor);
    const message = errors[input?.name] || '';
    el.textContent = message;
    if (message) input.setAttribute('aria-invalid', 'true'); else input?.removeAttribute('aria-invalid');
  }
  form.querySelector('[aria-invalid="true"]')?.focus();
  if (Object.keys(errors).length) announce(Object.values(errors)[0]);
  return !Object.keys(errors).length;
}

const formValues = form => {
  const values = {};
  for (const el of form.elements) if (el.name) values[el.name] = el.type === 'checkbox' ? el.checked : el.value;
  return values;
};

// Runs one settings save with the shared saving/saved/error state; resolves to the response, or undefined on failure.
async function settingsSave(send, saved) {
  const ui = state.settingsUi;
  if (ui.saving) return undefined;
  Object.assign(ui, { saving: true, saved: '', formError: '' });
  renderView();
  let value;
  try { value = await send(); ui.saved = saved; state.settingsDirty = false; syncDirty(); }
  catch (error) { ui.formError = error.message; }
  ui.saving = false;
  renderView();
  poll();
  return value;
}

async function refreshAccount() {
  const a = state.account;
  if (a.checking) return;
  a.checking = true;
  renderView();
  const errors = [];
  for (const [key, path] of [['ai', 'codex'], ['ai_claude', 'claude']]) {
    try { state.data[key] = await api(`/api/settings/${path}/status`); }
    catch (error) { errors.push(error.message); }
  }
  a.error = errors[0] || '';
  a.checking = false;
  a.login = settleLogin(a.login, state.data.ai);
  render();  // The sidebar's sign-in dot follows the status.
}

let loginTimer;
// While sign-in is pending, the open Account page re-checks status every 5s; the link is dropped once it settles.
function watchLogin() {
  clearTimeout(loginTimer);
  if (!state.account.login) return;
  loginTimer = setTimeout(async () => {
    if (state.route.name === 'settings' && state.route.section === 'account' && !document.hidden) await refreshAccount();
    watchLogin();
  }, 5000);
}

async function accountAction(kind) {
  const a = state.account;
  if (a.busy) return;
  Object.assign(a, { busy: true, error: '' });
  renderView();
  try {
    if (kind === 'login') {
      const { auth_url, login_id } = await api('/api/settings/codex/login', { method: 'POST', body: {} });
      a.login = { auth_url, login_id };
      state.data.ai = { ...state.data.ai, status: 'login_pending' };
      watchLogin();
    } else {
      state.data.ai = await api(`/api/settings/codex/${kind === 'cancel' ? 'login/cancel' : 'logout'}`, { method: 'POST', body: {} });
      a.login = null;
    }
  } catch (error) { a.error = error.message; }
  a.busy = false;
  render();
  poll();
}

// The token lives only in the input: it is cleared before the request and never enters state.
async function claudeTokenAction(kind) {
  const a = state.account;
  if (a.busy) return;
  const input = document.querySelector('[data-region="claude-actions"] input[name="claude-token"]');
  const token = kind === 'save' ? String(input?.value || '').trim() : '';
  if (kind === 'save' && !token) { a.claudeError = 'Paste the token from claude setup-token first.'; render(); return; }
  if (input) input.value = '';
  Object.assign(a, { busy: true, error: '', claudeError: '' });
  renderView();
  try {
    state.data.ai_claude = kind === 'save'
      ? await api('/api/settings/claude/token', { method: 'POST', body: { token } })
      : await api('/api/settings/claude/token', { method: 'DELETE' });
  } catch (error) { a.claudeError = error.message; }
  a.busy = false;
  render();
  poll();
}

const workerById = id => (state.data.workers || []).find(w => w.id === id);
const accountById = id => (state.data.accounts || []).find(a => a.id === id);

async function settingsAction(action, target) {
  const ui = state.settingsUi;
  const id = target.dataset.id;
  if (target.getAttribute('aria-disabled') === 'true') return;
  if (['account-login', 'account-cancel', 'account-logout'].includes(action)) return accountAction(action.slice(8));
  if (action === 'account-refresh') return refreshAccount();
  if (action === 'claude-token-save' || action === 'claude-token-remove') return claudeTokenAction(action.slice(13));
  if (action === 'worker-probe') {
    ui.probing.add(id);
    ui.errors.delete(id);
    renderView();
    try {
      const status = await api(`/api/workers/${enc(id)}/probe`, { method: 'POST', body: {} });
      Object.assign(workerById(id) || {}, { last_probe: status, last_probe_at: Date.now() / 1000 });
    } catch (error) { ui.errors.set(id, error.message); }
    ui.probing.delete(id);
    renderView();
    poll();
  } else if (action === 'worker-delete' || action === 'steam-delete') {
    const kind = action === 'worker-delete' ? 'workers' : 'accounts';
    const record = kind === 'workers' ? workerById(id) : accountById(id);
    const body = kind === 'workers' ? 'ASHAD forgets this desktop and its stored token. Files on the desktop are kept.' : 'ASHAD forgets this Steam account. Nothing changes on Steam.';
    if (!await confirmDialog({ title: `Delete ${record?.name || id}?`, body, confirm: 'Delete', danger: true })) return;
    try {
      await api(`/api/${kind}/${enc(id)}`, { method: 'DELETE' });
      state.data[kind] = state.data[kind].filter(r => r.id !== id);
      toast('Deleted.', 'ok');
    } catch (error) { kind === 'workers' ? ui.errors.set(id, error.message) : ui.steamResults.set(id, { ok: false, text: error.message }); }
    renderView();
    poll();
  } else if (action.startsWith('steam-')) {
    steamAction(action.slice(6), accountById(id));
  }
}

async function steamAction(kind, account) {
  if (!account) return;
  const ui = state.settingsUi;
  const worker = workerById(account.worker_id);
  let body = {};
  if (kind === 'verify') {
    const builds = (state.data.builds || []).filter(b => String(b.steam_app_id) === String(account.app_id));
    const form = await openDialog({ title: 'Verify Steam account?', confirm: 'Launch and verify',
      html: `<p>Verification launches the game on ${esc(worker?.name || account.worker_id)} to prove the licence. It needs the free instance slot and an unused desktop, and it stops when done.</p>`
        + `<div class="field"><label for="verify-build">Build</label><select id="verify-build" class="select" name="build">${builds.map(b => `<option value="${esc(b.id)}">${esc(b.name || b.id)}</option>`).join('')}</select></div>` });
    if (!form) return;
    body = { account_id: account.id, build_id: form.get('build') };
  }
  ui.steamBusy.set(account.id, kind);
  ui.steamResults.delete(account.id);
  renderView();
  try {
    const r = await api(`/api/workers/${enc(account.worker_id)}/steam/${kind}`, { method: 'POST', body });
    if (kind === 'finish') await api(`/api/workers/${enc(account.worker_id)}/steam/status`, { method: 'POST', body: {} });  // Refreshes the account's readiness.
    const desktop = kind === 'start' && /^https?:\/\//.test(worker?.desktop_url || '') ? worker.desktop_url : null;
    ui.steamResults.set(account.id, kind === 'verify'
      ? { ok: r.verified === true, text: r.verified ? 'Verified: the licence and identity match.' : `Not verified: ${r.reason || 'reason not reported'}` }
      : { ok: true, text: r.instructions || r.reason || (kind === 'start' ? 'Steam sign-in started on the desktop.' : 'Steam sign-in closed.'), desktop });
  } catch (error) { ui.steamResults.set(account.id, { ok: false, text: error.message }); }
  ui.steamBusy.delete(account.id);
  renderView();
  poll();
}

async function settingsSubmit(form) {
  const ui = state.settingsUi;
  const values = formValues(form);
  const kind = form.dataset.form;
  if (kind === 'settings-worker') {
    if (form.querySelector('[data-key="w-save"]')?.getAttribute('aria-disabled') === 'true') return;
    const { body, errors } = workerPayload(values, form.dataset.id);
    if (!showErrors(form, errors)) return;
    if (await settingsSave(() => api('/api/workers', { method: 'POST', body }), 'Desktop saved.')) {
      form.elements.token.value = '';  // Write-only: never kept in the DOM after saving.
      if (form.dataset.id) location.hash = '#/settings/desktops';
      else form.reset();
    }
  } else if (kind === 'settings-steam') {
    const errors = steamErrors(values);
    if (!String(values.name || '').trim()) errors.name = 'Name the account.';
    if (!values.steam_id) errors.steam_id ||= 'Enter the Steam ID64.';
    if (!values.app_id) errors.app_id ||= "Enter the game's App ID.";
    if (!values.worker_id) errors.worker_id = 'Add a desktop first.';
    if (!showErrors(form, errors)) return;
    const body = { name: values.name.trim(), worker_id: values.worker_id, steam_id: values.steam_id.trim(), app_id: Number(values.app_id) };
    if (await settingsSave(() => api('/api/accounts', { method: 'POST', body }), 'Account added. Start sign-in on its desktop next.')) form.reset();
  } else if (kind === 'settings-limits') {
    const { body, errors } = limitsPayload(values);
    if (!showErrors(form, errors)) return;
    await settingsSave(() => api('/api/settings', { method: 'POST', body }), 'Limits saved.');
  }
}

function renderUsage(view) {
  mount(view, 'usage', usageSkeleton());
  patchRegions(view, usageRegions(state.usage, state.usageUi, new Map((state.data.model_options || []).map(m => [m.id, m.label]))));
}

async function loadUsage() {
  const previous = state.usage;
  if (previous?.loading) return;
  const record = state.usage = { ...previous, loading: true, error: '' };
  renderView();
  try { Object.assign(record, { rows: (await api('/api/usage')).usage || [], at: Date.now() / 1000 }); }
  catch (error) { record.error = error.message; if (record.rows) toast(`Usage refresh failed: ${error.message}`, 'error'); }
  record.loading = false;
  if (state.usage === record) renderView();
}

// ── Project overview (C7) ───────────────────────────────────────────────────
function overviewFor(id) {
  if (!state.overviews.has(id)) state.overviews.set(id, newOverview(id));
  return state.overviews.get(id);
}

function renderOverview(view) {
  const ov = overviewFor(state.route.id);
  const m = overviewModel(state.data, ov);
  ov.buildId = m.build?.id || '';
  if (readinessDue(ov, ov.buildId)) probeReadiness(ov, ov.projectId, ov.buildId);
  mount(view, `overview:${ov.projectId}`, overviewSkeleton());
  patchRegions(view, overviewRegions(m, ov));
}

async function refreshControls(ov, agent) {
  const running = ov.refreshes.get(agent?.id);
  if (!agent || running?.status === 'busy' || running?.waiting) return;
  const build = (state.data.builds || []).find(b => b.id === agent.build_id);
  const name = agent.name || agent.id;
  if (!await confirmDialog({ title: 'Get game controls again?', confirm: 'Get controls',
    body: `ASHAD re-reads build ${build?.name || agent.build_id} with Claude Sonnet and saves a new project configuration named “${name}”. The current one stays as it is. This takes a few minutes and uses your Claude subscription.` })) return;
  const op = newOp();  // An explicit refresh is a new request with its own ID.
  ov.refreshes.set(agent.id, op);
  const pending = getControls(op, { build_id: agent.build_id, project_id: ov.projectId, name }, api, ms => { renderView(); return sleep(ms); });
  watchControls(op);
  renderView();
  await pending;
  if (op.status === 'done') {
    state.data.agents = [op.value, ...state.data.agents.filter(a => a.id !== op.value.id)];
    toast(`Saved game controls as ${op.value.name || op.value.id}.`, 'ok');
    poll();
  }
  renderView();
}

// Opens the controls file of one project configuration. Needs only the id, so the overview and the setup page share it.
async function viewControls(id) {
  const agent = (state.data.agents || []).find(a => a.id === id);
  try {
    const c = await api(`/api/agents/${enc(id)}/controls`);
    const by = c.generated_by?.method === 'legacy_tuning' ? `Converted from an earlier configuration written by ${modelLabel(state.data, c.generated_by.model)}` : 'Gathered by Claude Sonnet';
    openDialog({ title: agent?.name || id, confirm: null, cancel: 'Close', wide: true,
      html: `<p>${esc(by)}</p><pre class="instructions" tabindex="0">${esc(c.controls)}</pre>` });
  } catch (error) { toast(error.message, 'error'); }
}

function onOverviewAction(action, target) {
  const ov = overviewFor(state.route.id);
  if (target.getAttribute('aria-disabled') === 'true') return;
  const agent = (state.data.agents || []).find(a => a.id === target.dataset.id);
  if (action === 'ov-add-build') addBuild(ov, ov.projectId, target.dataset.kind);
  else if (action === 'ov-delete-build') deleteBuild(target.dataset.id);
  else if (action === 'ov-delete-project') deleteProject(ov.projectId);
  else if (action === 'ov-delete-agent') deleteAgent(target.dataset.id);
  else if (action === 'ov-recheck') { ov.readiness = null; renderView(); }
  else if (action === 'ov-prepare') prepare(ov, ov.projectId, (state.data.builds || []).find(b => b.id === ov.buildId));
  else if (action === 'ov-refresh') refreshControls(ov, agent);
  else if (action === 'ov-sidebar') {
    state.ui.expanded.set(ov.projectId, true);
    state.ui.showAll.add(ov.projectId);
    if (document.body.classList.contains('nav-collapsed')) toggleSidebar();
    renderSidebar();
    $(`#sidebar [data-key="g:${CSS.escape(ov.projectId)}"]`)?.focus();
  }
}

function render() {
  renderBanners();
  if (!state.data) return;
  renderSidebar();
  renderView();
}

// ── Instance thread (C4) ────────────────────────────────────────────────────
// The detailed GET wins over the bootstrap row for the open thread.
function threadRun() {
  const id = state.route.id;
  const detail = state.runSlot.get();
  return detail?.id === id ? detail : (state.data?.runs || []).find(r => r.id === id);
}

const threadOwner = id => (state.data?.instance_slot?.owners || []).find(o => o.run_id === id);

function stopCamera() {
  state.camera?.stop();
  state.camera = null;
}

function renderThread(view) {
  const run = threadRun();
  const t = state.thread;
  const key = `thread:${run.id}`;
  if (view.dataset.mount !== key) {
    stopCamera();  // Clear the old feed before its <img> is dropped with the old markup.
    mount(view, key, threadSkeleton());
    state.camera = createCamera(view.querySelector('[data-camera]'), { onChange: () => queueMicrotask(renderView) });
  }
  const owner = threadOwner(run.id);
  const status = instanceState(run, owner);
  const mine = () => [...state.commands].filter(([k]) => k.startsWith(`${run.id}:`));
  if (t.statusKey !== status.key) {
    const said = stateAnnouncement(t.statusKey, status);
    if (said) announce(said);
    // A failed command for an earlier state is moot once the state moves on.
    for (const [k, c] of mine()) if (!c.pending) state.commands.delete(k);
    t.statusKey = status.key;
  }
  t.tab ??= defaultTab(run);  // Chosen once per thread, so the tab never moves when the run ends.
  const player = selectedPlayer(run);
  state.camera.sync(cameraTarget(run, player, document.hidden || t.tab !== 'bot'));  // The MJPEG stream runs only while Bot view is shown.
  const pending = new Set(mine().filter(([, c]) => c.pending).map(([k]) => k.slice(run.id.length + 1)));
  const error = mine().find(([, c]) => c.error)?.[1].error;
  patchRegions(view, threadRegions(run, { status, owner, data: state.data, ui: t, pending, error, cameraStatus: state.camera.status, player, tab: t.tab }));
  for (const panel of view.querySelectorAll('.thread-panel')) panel.hidden = panel.id !== `panel-${t.tab}`;
  const reason = view.querySelector('#resume-reason');
  if (reason && reason.value !== t.instructionText) reason.value = t.instructionText;
  renderTimeline(view, run);
  renderPanels(view, run);
}

// Switching tabs never touches the route, so no new epoch, poll or scroll reset. Each tab gets its own scroll position back.
function setTab(tab) {
  const t = state.thread;
  const view = $('#view');
  if (t.tab === tab) return;
  t.scroll[t.tab] = view.scrollTop;
  if (t.tab === 'actions') t.anchor = latestInView() ? null : readingAnchor();  // A hidden list cannot hold its place, so remember the card being read.
  t.tab = tab;
  renderView();
  view.scrollTop = t.scroll[tab] ?? 0;
  if (tab === 'actions' && t.anchor?.li.isConnected) view.scrollTop += t.anchor.li.getBoundingClientRect().top - t.anchor.top;
  if (tab === 'actions' && t.tl.unseen && latestInView()) { t.tl.unseen = 0; renderView(); }
}

// ── Goals, World state and Model usage panels (C6) ──────────────────────────
// Built only from the run and its retained events; it never asks the game for fresh state.
function renderPanels(view, run) {
  const t = state.thread;
  const events = mergeEvents(run.events, t.tl.events);
  t.lastObs = latestObservation(events, t.lastObs);
  const selectedSeq = t.tl.selected ? Number(t.tl.selected.replace('m', '')) : null;
  const selectedEvent = events.find(e => e.seq === selectedSeq) || null;
  patchRegions(view, panelRegions(run, { latest: t.lastObs, selectedEvent, events, view: t.worldView, now: Date.now() / 1000, goalOpen: state.ui.goalOpen }));
}

// ── Activity timeline (C5) ──────────────────────────────────────────────────
// Newest first. "At the latest" means Actions is shown and the top of the list is in view below the sticky header, controls and tabs.
const AUTO_SCROLL_PX = 80;
const latestInView = () => {
  if (state.thread.tab !== 'actions') return false;  // A hidden list has no position; new events wait behind the "N new" pill.
  const list = timelineList();
  const covered = stickyBottom();
  return !list || list.getBoundingClientRect().top >= covered - AUTO_SCROLL_PX;
};
const stickyBottom = () => $('#view .thread-sticky')?.getBoundingClientRect().bottom ?? $('#view').getBoundingClientRect().top;
const scrollToLatest = () => $('#view .activity')?.scrollIntoView({ block: 'start' });

// New cards go in above; a reader further down keeps exactly what they were looking at.
// The first card below the sticky block and where it sits now.
function readingAnchor() {
  const covered = stickyBottom();
  const li = [...(timelineList()?.children || [])].find(card => card.getBoundingClientRect().bottom > covered);
  return li ? { li, top: li.getBoundingClientRect().top } : null;
}

function keepReadingPosition(render) {
  const anchor = readingAnchor();
  render();
  if (anchor?.li.isConnected) $('#view').scrollTop += anchor.li.getBoundingClientRect().top - anchor.top;
}
const timelineList = () => $('#view [data-timeline]');

function renderTimeline(view, run) {
  const tl = state.thread.tl;
  seedFromRun(tl, run);
  const cards = timelineCards(tl);
  patchRegions(view, timelineRegions(tl, { runTotal: run.events_page?.total, cards }));
  const search = view.querySelector('[data-key="tl-search"]');
  if (search && search.value !== tl.searchDraft) search.value = tl.searchDraft;  // The field is never re-rendered with typed text.
  const list = view.querySelector('[data-timeline]');
  syncCards(list, cards);
  for (const li of list.children) {
    li.classList.toggle('is-selected', li.dataset.card === tl.selected);
    fillBody(li);
  }
  updateTimes(list);
}

const updateTimes = root => { for (const t of root?.querySelectorAll('time[data-ts]') || []) { const text = ago(Number(t.dataset.ts)); if (t.textContent !== text) t.textContent = text; } };

// Bodies are built on first open (and after a grown model row is replaced), never on the poll.
function fillBody(li) {
  const body = li.querySelector('[data-body]');
  if (!body || body.childElementCount || !li.querySelector('details')?.open) return;
  const run = threadRun();
  const players = Object.fromEntries((run?.plan?.players || []).map(p => [p.id, p.name || p.id]));
  body.innerHTML = cardBody(li.card, eventIndex([...(Array.isArray(run?.events) ? run.events : []), ...state.thread.tl.events]), players);
}

const searchQuery = tl => (tl.search ? `&search=${enc(tl.search)}` : '');

// Replaces the window: the latest page, or the page before `before`. Only the newest request may apply.
async function loadPage(mode, before) {
  const tl = state.thread.tl;
  const runId = state.route.id;
  const current = state.isCurrent;
  const token = beginPage(tl);
  renderView();
  try {
    const page = await api(`/api/runs/${enc(runId)}/events?${mode === 'latest' ? 'tail=true' : `before=${before}`}&limit=${PAGE}${searchQuery(tl)}`);
    if (!current() || state.thread.tl !== tl || !applyPage(tl, token, page, mode, threadRun()?.events_page?.next)) return false;
  } catch (error) { failPage(tl, token, error); }
  if (current()) renderView();
  return true;
}

// Live edge: append newer events after the run poll. Auto-scroll only when already near the bottom.
async function pollTimeline(runId) {
  const tl = state.thread.tl;
  if (!tl?.loaded || tl.mode !== 'latest' || tl.loading) return;
  const after = tl.cursor, token = tl.token, current = state.isCurrent;
  let page;
  try { page = await api(`/api/runs/${enc(runId)}/events?after=${after}&limit=${MAX_EVENTS}${searchQuery(tl)}`); } catch { return; }  // The next run poll retries.
  if (!current() || state.thread.tl !== tl) return;
  const atLatest = latestInView();
  const added = applyLive(tl, token, page, after);
  if (added === 'reload') { loadPage('latest'); return; }
  if (!added) return;
  if (atLatest) return renderView();
  tl.unseen += added;
  keepReadingPosition(renderView);
}

const findCard = seq => [...(timelineList()?.children || [])].find(li => li.card?.events.some(e => e.seq === seq));

function selectCard(key) {
  state.thread.tl.selected = state.thread.tl.selected === key ? null : key;
  renderView();
}

// Scrolls to a card by seq, widening the filter or loading the page around it when it is not shown.
async function jumpTo(seq) {
  if (state.thread.tab !== 'actions') setTab('actions');  // Goal links jump here from other tabs.
  const tl = state.thread.tl;
  if (!findCard(seq) && tl.filter !== 'all') { tl.filter = 'all'; renderView(); }
  if (!findCard(seq)) {
    if (tl.search) tl.search = tl.searchDraft = '';
    await loadPage('history', seq + PAGE / 2);
  }
  const li = findCard(seq);
  if (!li) { toast('That activity is no longer retained.', 'error'); return; }
  tl.selected = li.dataset.card;
  renderView();
  li.scrollIntoView({ block: 'center' });
  li.querySelector('summary')?.focus({ preventScroll: true });
}

let runTimer;
function scheduleRun() {
  clearTimeout(runTimer);
  if (state.route.name !== 'instance') return;
  const live = isActiveRun(threadRun()) || Boolean(threadOwner(state.route.id));
  runTimer = setTimeout(() => (document.hidden ? scheduleRun() : pollRun()), live ? 2000 : 15000);
}

async function pollRun() {
  if (state.route.name !== 'instance') return;
  const id = state.route.id;
  const current = state.isCurrent;
  const apply = state.runSlot.begin();
  try {
    const run = await api(`/api/runs/${enc(id)}`);
    if (current() && apply(run)) state.runMissing = false;
  } catch (error) {
    if (current() && error.status === 404) state.runMissing = true;
  }
  if (!current()) return;
  renderView();
  scheduleRun();
  pollTimeline(id);
}

// Runs one control through its stable command ID. runOf picks the run a successful response carries.
async function command(action, send, ok, runOf = value => value) {
  const runId = state.route.id;
  const pending = runCommand(state.commands, `${runId}:${action}`, send);
  renderView();
  const { value, error, skipped } = await pending;
  if (skipped) return undefined;
  const onThread = state.route.name === 'instance' && state.route.id === runId;
  if (!error) {
    const run = runOf(value);
    if (run?.id === runId) state.runSlot.set(run);  // Also drops any GET that started before this response.
    if (ok) toast(ok, 'ok');
  } else if (!onThread) toast(error.message, 'error');
  if (onThread) { renderView(); pollRun(); }
  poll();
  return error ? undefined : value;
}

const runPath = run => `/api/runs/${enc(run.id)}`;
const control = (run, name, extra = {}) => id => api(`${runPath(run)}/control`, { method: 'POST', body: { command: name, command_id: id, ...extra } });

async function onControl(action) {
  const run = threadRun();
  if (!run) return;
  const goal = runGoal(run);
  switch (action) {
    case 'pause': return command('pause', control(run, 'pause'), 'Paused. Bot input is held; the game may keep simulating.');
    case 'resume': return command('resume', control(run, 'resume'), 'Resumed.');
    case 'resume-with':
      state.thread.instructionOpen = true;
      renderView();
      return $('#resume-reason')?.focus();
    case 'stop':
      if (!await confirmDialog({ title: 'Stop this instance?', body: `“${goal}” stops and its desktop is torn down. Retained history stays readable.`, confirm: 'Stop instance', danger: true })) return;
      return command('stop', control(run, 'stop'), 'Stop requested.');
    case 'extend': {
      const form = await openDialog({ title: 'Extend investigation', confirm: 'Extend',
        html: `<div class="field"><label for="extend-minutes">Extra minutes</label><input id="extend-minutes" class="input" name="minutes" type="number" min="1" max="1440" value="30" required><span class="hint">The run's overall duration limit is unchanged.</span></div>` });
      if (!form) return;
      return command('extend', control(run, 'extend', { seconds: Number(form.get('minutes')) * 60 }), 'Investigation extended.');
    }
    case 'recalculate':
      if (!await confirmDialog({ title: 'Recalculate goals?', body: 'Bot input is held while ASHAD replans from fresh game state under the same primary goal. The old plan stays in the history. Resume separately when you are ready.', confirm: 'Recalculate goals' })) return;
      return command('recalculate', id => api(`${runPath(run)}/recalculate-goals`, { method: 'POST', body: { command_id: id } }), 'Goals recalculated. Bot input stays paused until you resume.');
    case 'takeover': return takeOver(run);
    case 'handback': return command('handback', () => api(`${runPath(run)}/takeover/finish`, { method: 'POST', body: { resume: true, reason: '' } }), 'Control handed back. Bots resumed.');
    case 'handback-paused': return command('handback-paused', () => api(`${runPath(run)}/takeover/finish`, { method: 'POST', body: { resume: false, reason: '' } }), 'Control handed back. Bots stay paused.');
    case 'flag': {
      const form = await openDialog({ title: 'Flag an issue', confirm: 'Flag issue',
        html: `<div class="field"><label for="flag-description">What happened?</label><textarea id="flag-description" class="textarea" name="description" rows="3" required maxlength="4000"></textarea></div>`
          + `<div class="field"><label for="flag-expected">What did you expect? (optional)</label><textarea id="flag-expected" class="textarea" name="expected" rows="2" maxlength="4000"></textarea></div>` });
      if (!form) return;
      return command('flag', () => api(`${runPath(run)}/flag`, { method: 'POST', body: { description: form.get('description'), expected: form.get('expected').trim() || undefined } }), 'Issue flagged.', () => null);
    }
    case 'reconcile':
      if (!await confirmDialog({ title: 'Reconcile teardown?', body: 'ASHAD retries the desktop teardown. Reservations are released only after the worker confirms shutdown.', confirm: 'Reconcile teardown' })) return;
      return command('reconcile', () => api(`${runPath(run)}/reconcile`, { method: 'POST', body: {} }), 'Teardown reconciliation finished.', () => null);
    case 'report': return showReport(run);
    case 'evidence': return showEvidence(run);
  }
}

function selectedPlayer(run) {
  const ids = playerIds(run);
  return ids.includes(state.thread.player) ? state.thread.player : ids[0];
}

async function takeOver(run) {
  const player = selectedPlayer(run);
  const current = state.isCurrent;
  const result = await command('takeover', () => api(`${runPath(run)}/players/${enc(player)}/takeover`, { method: 'POST', body: {} }), null, value => value?.run);
  if (!result || !current()) return;
  let url = null;
  try { url = new URL(result.desktop_url); } catch { /* invalid below */ }
  const safe = url && ['http:', 'https:'].includes(url.protocol) && !url.username && !url.password && !url.search && !url.hash;
  // The password lives only in this dialog's DOM; openDialog empties it on close.
  await openDialog({ title: 'You have control', confirm: null, cancel: 'Close',
    html: `<p>All bots are paused. Open the desktop and enter this password. Closing the desktop window does not hand back; choose Hand back when you are done.</p>`
      + `<div class="field"><label for="takeover-password">Desktop password</label><div class="copy-row">`
      + `<input id="takeover-password" class="input mono" value="${esc(result.password || '')}" readonly autocomplete="off" spellcheck="false">`
      + `<button type="button" class="btn btn-secondary" data-action="copy-password">Copy</button></div><span class="hint">Shown once. Cleared when this dialog closes.</span></div>`
      + (safe ? `<a class="btn btn-primary" href="${esc(url.href)}" target="_blank" rel="noopener noreferrer">${icon('monitor')}Open desktop</a>`
        : `<p class="field-error">The desktop address is not valid. Hand back, then fix the worker's desktop URL in Settings.</p>`) });
}

async function showReport(run) {
  try {
    const { markdown } = await api(`${runPath(run)}/report`);
    await openDialog({ title: 'Report', confirm: null, cancel: 'Close', wide: true, html: `<pre class="report">${esc(markdown || 'The report is empty.')}</pre>` });
  } catch (error) { toast(`Report unavailable: ${error.message}`, 'error'); }
}

async function showEvidence(run) {
  try {
    const { files = [], gaps = [], status } = await api(`${runPath(run)}/evidence`);
    const list = files.filter(f => String(f.url).startsWith('/api/runs/')).map(f => `<li><a href="${esc(f.url)}" download>${icon('file-text')}${esc(f.name)}</a></li>`).join('');
    await openDialog({ title: 'Evidence', confirm: null, cancel: 'Close', wide: true,
      html: `<p>Status: ${esc(status || 'unknown')}</p>${list ? `<ul class="evidence">${list}</ul>` : '<p>No files were retained.</p>'}`
        + (gaps.length ? `<h3 class="section-title">Gaps</h3><ul class="evidence-gaps">${gaps.map(g => `<li>${esc(typeof g === 'string' ? g : JSON.stringify(g))}</li>`).join('')}</ul>` : '') });
  } catch (error) { toast(`Evidence unavailable: ${error.message}`, 'error'); }
}

// §12: the sidebar is a left drawer below 960px.
const wideSidebar = matchMedia('(min-width: 960px)');
const sidebarDrawer = createDrawer($('#drawer-left'), $('#sidebar'), pane => { $('.layout').prepend(pane); syncNavToggle(); });
const syncNavToggle = () => $('#nav-toggle').setAttribute('aria-expanded', String(wideSidebar.matches ? !document.body.classList.contains('nav-collapsed') : sidebarDrawer.open));

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
  const guard = dirtyGuard(route);
  if (guard) {
    const target = location.hash;
    history.replaceState(null, '', acceptedHash);
    if (!await confirmDialog({ ...guard, danger: true })) return;
    guard.discard();
    syncDirty();
    location.hash = target;
    return;
  }
  acceptedHash = location.hash;
  sidebarDrawer.close();  // The drawer closes on navigation and hands focus back to its toggle.
  const previous = state.route;
  if (previous.name === 'setup' && state.setups.get(setupKey(previous))?.agent) state.setups.delete(setupKey(previous));  // A finished setup starts fresh next time.
  if (route.name === 'setup' && (previous.name !== 'setup' || setupKey(previous) !== setupKey(route))) {
    // Re-entry jumps to the first incomplete step (or ?step=, never past it) and probes readiness again.
    Object.assign(setupFor(route), { step: Number(route.query.step) || 0, readiness: null });
  }
  state.route = route;
  state.isCurrent = epoch.next();  // Any response started for the previous route is now stale.
  if (composerProject(route, state.data)) state.readiness = null;  // Readiness probes on view open, never on the poll.
  if (route.name === 'composer' && route.query.agent) draftFor(route.id).agentId = route.query.agent;
  if (route.name === 'composer' && route.query.from) {  // "Start again" from a blocked instance prefills its goal and model.
    const run = (state.data?.runs || []).find(r => r.id === route.query.from);
    if (run) { const d = draftFor(route.id); d.goal ||= run.plan?.primary_goal || ''; d.model = run.plan?.model || d.model; }
  }
  if (route.name === 'instance' && state.thread.runId !== route.id) {
    state.thread = { runId: route.id, player: null, instructionOpen: false, instructionText: '', statusKey: null, tl: createTimeline(), worldView: 'latest', lastObs: null, tab: null, scroll: {} };
    state.runSlot.set(null);  // Also drops a late GET for the previous thread.
    state.runMissing = false;
  }
  if (route.name === 'settings' && (previous.name !== 'settings' || previous.section !== route.section)) {
    Object.assign(state.settingsUi, { saved: '', formError: '' });
    if (route.section === 'account') refreshAccount();  // Status on view open; it checks the account without inference.
  }
  if (route.name === 'usage' && previous.name !== 'usage') loadUsage();
  if (route.name === 'project' && (previous.name !== 'project' || previous.id !== route.id)) overviewFor(route.id).readiness = null;  // Probe on view open.
  rememberProject(route);
  syncDirty();
  render();
  $('#view').scrollTop = 0;
  if (route.name === 'instance') pollRun();
  else clearTimeout(runTimer);
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
  if (wideSidebar.matches) document.body.classList.toggle('nav-collapsed');
  else if (sidebarDrawer.open) sidebarDrawer.close();
  else sidebarDrawer.show(() => $('#nav-toggle'));
  syncNavToggle();
}

// Both model menus route here: the composer updates its draft (renderView re-probes readiness); the header sends a model control unless the run already uses that model.
function chooseModel(field, model) {
  if (field === 'run-model') {
    const run = threadRun();
    if (model === run.model) return;
    command(`model:${model}`, control(run, 'model', { model }), `Model set to ${modelLabel(state.data, model)}. Applies from the next decision.`);
    return;
  }
  draftFor(composerProject(state.route, state.data)).model = model;
  renderView();
}

document.addEventListener('click', async event => {
  const row = event.target.closest('.tl-row');
  if (row) selectCard(row.closest('.tl-card').dataset.card);  // The native toggle still opens the body.
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
  } else if (action === 'instance-delete') {
    if (target.getAttribute('aria-disabled') !== 'true') deleteInstance(id);
  } else if (action === 'ov-controls') {  // Shared by the project overview and the setup step 4 done view.
    viewControls(id);
  } else if (action.startsWith('setup-')) {
    onSetupAction(action, target, event);
  } else if (action.startsWith('ov-')) {
    onOverviewAction(action, target);
  } else if (/^(account|claude|worker|steam)-/.test(action)) {
    settingsAction(action, target);
  } else if (action === 'usage-refresh') {
    loadUsage();
  } else if (action === 'usage-range' || action === 'usage-sort' || action === 'usage-more') {
    const u = state.usageUi;
    if (action === 'usage-range') Object.assign(u, { range: target.dataset.range, limit: newUsageUi().limit });
    else if (action === 'usage-more') u.limit += newUsageUi().limit;
    else Object.assign(u, u.sort === target.dataset.sort ? { dir: u.dir === 'asc' ? 'desc' : 'asc' } : { sort: target.dataset.sort, dir: 'desc' });
    renderView();
  } else if (action === 'ctl') {
    target.closest('[popover]')?.hidePopover();
    if (target.getAttribute('aria-disabled') !== 'true') onControl(target.dataset.ctl);
  } else if (action === 'pick-model') {
    const menu = target.closest('[popover]');
    menu.hidePopover();
    document.querySelector(`[popovertarget="${menu.id}"]`)?.focus();  // patchHTML then keeps focus on the re-rendered trigger.
    chooseModel(target.dataset.field, target.dataset.value);
  } else if (action === 'thread-tab') {
    setTab(target.dataset.tab);
  } else if (action === 'instruction-cancel') {
    Object.assign(state.thread, { instructionOpen: false, instructionText: '' });
    renderView();
  } else if (action === 'insp-view') {
    state.thread.worldView = target.dataset.view;
    renderView();
  } else if (action === 'tl-filter') {
    state.thread.tl.filter = target.dataset.filter;
    renderView();
  } else if (action === 'tl-older') {
    if (target.getAttribute('aria-disabled') !== 'true') loadPage('history', state.thread.tl.events[0]?.seq);
  } else if (action === 'tl-latest') {
    if (await loadPage('latest')) scrollToLatest();
  } else if (action === 'tl-to-latest') {
    state.thread.tl.unseen = 0;
    scrollToLatest();
    renderView();
  } else if (action === 'tl-jump') {
    jumpTo(Number(target.dataset.seq));
  } else if (action === 'tl-raw-full') {
    const li = target.closest('.tl-card');
    li.querySelector('.tl-raw pre').textContent = rawJSON(li.card);
    target.remove();
  } else if (action === 'copy-password') {
    const field = $('#takeover-password');
    try { await navigator.clipboard.writeText(field.value); toast('Password copied.', 'ok'); }
    catch { field.select(); toast('Press Ctrl+C to copy the selected password.'); }
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
  if (event.target.classList?.contains('model-option')) {
    const options = [...event.target.parentElement.querySelectorAll('.model-option')];
    const next = nextOptionIndex(event.key, options.indexOf(event.target), options.length);
    if (next >= 0) { event.preventDefault(); options[next].focus(); }
  }
  if (event.target.getAttribute?.('role') === 'tab' && !event.ctrlKey && !event.altKey && !event.metaKey) {
    const tab = nextTab(state.thread.tab, event.key);
    if (!tab) return;
    event.preventDefault();
    setTab(tab);
    $(`#tab-${tab}`)?.focus();  // Selection follows focus.
  }
});

// Opening a model menu focuses the selected model; `toggle` does not bubble, so listen in the capture phase.
document.addEventListener('toggle', event => {
  if (event.newState === 'open' && event.target.classList?.contains('model-menu')) event.target.querySelector('[aria-selected="true"]')?.focus();
}, true);

// Composer fields update the in-memory draft; the textarea itself is never re-rendered.
document.addEventListener('input', event => {
  if (event.target.id === 'resume-reason') { state.thread.instructionText = event.target.value; return; }
  if (event.target.dataset.key === 'tl-search') {
    const tl = state.thread.tl;
    tl.searchDraft = event.target.value;
    if (!tl.searchDraft.trim() && tl.search) { tl.search = ''; loadPage('latest'); }  // Clearing the box shows everything again.
    return;
  }
  const settingsForm = event.target.closest?.('form[data-form^="settings-"]');
  if (settingsForm) {
    state.settingsDirty = true;
    syncDirty();
    if (settingsForm.dataset.form === 'settings-steam' && ['steam_id', 'app_id'].includes(event.target.name)) {
      const message = steamErrors(formValues(settingsForm))[event.target.name] || '';  // App ID 480 is rejected as you type.
      settingsForm.querySelector(`[data-error-for="${event.target.id}"]`).textContent = message;
      if (message) event.target.setAttribute('aria-invalid', 'true'); else event.target.removeAttribute('aria-invalid');
    }
    return;
  }
  const field = event.target.dataset?.field;
  if (field === 'setup-name' || field === 'setup-agent-name') {
    const s = setupFor(state.route);
    if (field === 'setup-name') s.name = event.target.value;
    else Object.assign(s, { agentName: event.target.value, edited: true });
    if (s.project.status === 'error') Object.assign(s.project, { status: 'idle', error: '' });
    syncDirty();
    renderView();
    return;
  }
  const form = event.target.closest?.('[data-form="composer"]');
  if (!form || event.target.dataset.field !== 'goal') return;
  const draft = draftFor(form.dataset.project);
  draft.goal = event.target.value;
  draft.requestId ||= newId();  // Created once, when the draft is first edited.
  if (draft.result?.field === 'goal') draft.result = null;
  syncDirty();
  renderView();
});

document.addEventListener('change', event => {
  const field = event.target.dataset?.field;
  if (field === 'player') { state.thread.player = event.target.value; renderView(); return; }
  if (field === 'ov-build') {
    overviewFor(state.route.id).buildId = event.target.value;  // A chosen build is checked at once; that is the user's request, not a poll.
    renderView();
    return;
  }
  if (field === 'usage-model' || field === 'usage-purpose') {
    Object.assign(state.usageUi, { [field.slice(6)]: event.target.value, limit: newUsageUi().limit });
    renderView();
    return;
  }
  if (field === 'setup-build') {
    const s = setupFor(state.route);
    Object.assign(s, { buildId: event.target.value, readiness: null });
    syncDirty();
    renderView();
    return;
  }
  const projectId = composerProject(state.route, state.data);
  if (!projectId || !['agent', 'transport', 'project'].includes(field)) return;
  if (field === 'project') { location.hash = href('p', event.target.value, 'new'); return; }
  const draft = draftFor(projectId);
  if (field === 'agent') draft.agentId = event.target.value;
  else draft[field] = event.target.value;
  renderView();
});

document.addEventListener('submit', async event => {
  if (event.target.matches('[data-form="tl-search"]')) {
    event.preventDefault();
    const tl = state.thread.tl;
    tl.search = tl.searchDraft.trim();
    loadPage('latest');
    return;
  }
  if (event.target.matches('[data-form="resume-instruction"]')) {
    event.preventDefault();
    const run = threadRun();
    const reason = state.thread.instructionText.trim();
    if (!run || !reason) return;
    if (await command('resume-with', control(run, 'resume', { reason }), 'Resumed with your instruction.')) {
      Object.assign(state.thread, { instructionOpen: false, instructionText: '' });
      renderView();
    }
    return;
  }
  if (event.target.matches('[data-form="claude-token"]')) {
    event.preventDefault();  // Enter must not fall through to a GET submit that puts the token in the URL.
    settingsAction('claude-token-save', event.target.querySelector('[data-action="claude-token-save"]'));  // Same disabled gate as clicking Save.
    return;
  }
  if (event.target.matches('[data-form^="settings-"]')) {
    event.preventDefault();
    settingsSubmit(event.target);
    return;
  }
  if (event.target.matches('[data-form^="setup-"]')) {
    event.preventDefault();
    $('#view [data-key="sn-create"], #view [data-key="sn-next"]')?.click();  // Enter acts like the step's primary button.
    return;
  }
  const form = event.target.closest('[data-form="composer"]');
  if (!form) return;
  event.preventDefault();
  startFromComposer(form.dataset.project);
});

$('#nav-toggle').addEventListener('click', toggleSidebar);
// A click on a drawer's backdrop (the dialog itself, outside the pane) closes it.
for (const drawer of document.querySelectorAll('.drawer')) drawer.addEventListener('click', event => { if (event.target === drawer) drawer.close(); });
// Crossing the breakpoint closes the drawer; the sidebar returns to the layout.
wideSidebar.addEventListener('change', () => { sidebarDrawer.close(); syncNavToggle(); renderView(); });
// <details> toggle does not bubble, so listen in the capture phase.
document.addEventListener('toggle', event => {
  const el = event.target;
  if (el.matches?.('.tl-card > details')) fillBody(el.parentElement);
  else if (el.dataset?.goal) el.open ? state.ui.goalOpen.add(el.dataset.goal) : state.ui.goalOpen.delete(el.dataset.goal);
}, true);
$('#view').addEventListener('scroll', () => {
  const tl = state.thread.tl;
  if (state.route.name === 'instance' && tl?.unseen && latestInView()) { tl.unseen = 0; renderView(); }
}, { passive: true });
// Ages tick locally (observation age, Fresh to Stale, card times); nothing is fetched.
setInterval(() => {
  if (document.hidden) return;
  for (const t of document.querySelectorAll('#view time[data-elapsed]')) t.textContent = elapsed(Date.now() / 1000 - Number(t.dataset.elapsed));
  if (state.route.name !== 'instance') return;
  updateTimes(timelineList());
  const run = threadRun();
  if (run && $('#view').dataset.mount === `thread:${run.id}`) renderPanels($('#view'), run);
}, 1000);
window.addEventListener('hashchange', onRoute);
// Hidden: the camera stops at once. Visible again: data refreshes and the camera reconnects.
document.addEventListener('visibilitychange', () => {
  renderView();
  if (!document.hidden) { poll(); pollRun(); }
});

onRoute();
poll().then(schedule);
