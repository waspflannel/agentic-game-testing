import { esc, icon, href, newId, api, parseRoute, legacyNewTarget, createEpoch, patchHTML, dirtyBridge, runGoal, ago } from './core.js';
import { sidebarModel, sidebarHTML } from './views/sidebar.js';
import { emptyHTML, noAgentsHTML, projectAgents, readinessKey, newDraft, composerModel, composerSkeleton, composerRegions, startInstance, checkStart } from './views/home.js';
import { isActiveRun, playerIds, instanceState, cameraTarget, createCamera, createRunSlot, runCommand, threadSkeleton, threadRegions } from './views/instance.js';
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
  ui: { expanded: new Map(), showAll: new Set(), lastProject: null, inspectorOpen: true },
  drafts: new Map(),   // projectId -> composer draft
  readiness: null,     // { key, loading, result, error } for the open composer; probed on view open only
  isCurrent: () => true,
  runSlot: createRunSlot(),  // GET /api/runs/<id> for the open thread
  runMissing: false,
  commands: new Map(),       // `${runId}:${action}` -> { id, pending, error }
  camera: null,
  thread: {},                // per-open-thread UI: player, instruction draft, last status key, timeline
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
    case 'instance': return state.runMissing ? notFound('Instance') : `<div class="boot"><p>Loading instance…</p></div>`;
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
  if (route.name === 'instance') { const run = threadRun(); return run ? runGoal(run) : ''; }
  if (route.name === 'project' || route.name === 'composer') return projects.find(p => p.id === route.id)?.name || '';
  return { setup: 'Setup', usage: 'Usage', settings: 'Settings', styleguide: 'Styleguide' }[route.name] || '';
}

function renderView() {
  const route = state.route;
  const title = windowTitle(route, state.data);
  $('#window-title').textContent = title;
  document.title = title ? `${title} · ASHAD` : 'ASHAD';
  $('#inspector').hidden = route.name !== 'instance' || !state.ui.inspectorOpen;
  if (!state.data) return;
  const view = $('#view');
  if (route.name === 'instance' && threadRun()) return renderThread(view);
  stopCamera();
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
    // A failed command for an earlier state is moot once the state moves on.
    for (const [k, c] of mine()) if (!c.pending) state.commands.delete(k);
    t.statusKey = status.key;
  }
  const ids = playerIds(run);
  const player = ids.includes(t.player) ? t.player : ids[0];
  state.camera.sync(cameraTarget(run, player, document.hidden));
  const pending = new Set(mine().filter(([, c]) => c.pending).map(([k]) => k.slice(run.id.length + 1)));
  const error = mine().find(([, c]) => c.error)?.[1].error;
  patchRegions(view, threadRegions(run, { status, owner, data: state.data, ui: { ...t, inspectorOpen: state.ui.inspectorOpen }, pending, error, cameraStatus: state.camera.status, player }));
  const reason = view.querySelector('#resume-reason');
  if (reason && reason.value !== t.instructionText) reason.value = t.instructionText;
  renderTimeline(view, run);
}

// ── Activity timeline (C5) ──────────────────────────────────────────────────
const AUTO_SCROLL_PX = 80;
const nearBottom = () => { const v = $('#view'); return v.scrollHeight - v.scrollTop - v.clientHeight < AUTO_SCROLL_PX; };
const scrollToBottom = () => { const v = $('#view'); v.scrollTop = v.scrollHeight; };
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
  const atBottom = nearBottom();
  const added = applyLive(tl, token, page, after);
  if (added === 'reload') { loadPage('latest'); return; }
  if (!added) return;
  if (!atBottom) tl.unseen += added;
  renderView();
  if (atBottom) scrollToBottom();
}

const findCard = seq => [...(timelineList()?.children || [])].find(li => li.card?.events.some(e => e.seq === seq));

function selectCard(key) {
  state.thread.tl.selected = state.thread.tl.selected === key ? null : key;
  renderView();
}

// Scrolls to a card by seq, widening the filter or loading the page around it when it is not shown.
async function jumpTo(seq) {
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

async function takeOver(run) {
  const player = playerIds(run).includes(state.thread.player) ? state.thread.player : playerIds(run)[0];
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

function toggleInspector() {
  state.ui.inspectorOpen = !state.ui.inspectorOpen;
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
  if (route.name === 'instance' && state.thread.runId !== route.id) {
    state.thread = { runId: route.id, player: null, instructionOpen: false, instructionText: '', statusKey: null, tl: createTimeline() };
    state.runSlot.set(null);  // Also drops a late GET for the previous thread.
    state.runMissing = false;
  }
  rememberProject(route);
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
  const collapsed = document.body.classList.toggle('nav-collapsed');
  $('#nav-toggle').setAttribute('aria-expanded', String(!collapsed));
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
  } else if (action === 'ctl') {
    target.closest('[popover]')?.hidePopover();
    if (target.getAttribute('aria-disabled') !== 'true') onControl(target.dataset.ctl);
  } else if (action === 'toggle-inspector') {
    toggleInspector();
  } else if (action === 'instruction-cancel') {
    Object.assign(state.thread, { instructionOpen: false, instructionText: '' });
    renderView();
  } else if (action === 'tl-filter') {
    state.thread.tl.filter = target.dataset.filter;
    renderView();
  } else if (action === 'tl-older') {
    if (target.getAttribute('aria-disabled') !== 'true') loadPage('history', state.thread.tl.events[0]?.seq);
  } else if (action === 'tl-latest') {
    if (await loadPage('latest')) scrollToBottom();
  } else if (action === 'tl-to-bottom') {
    state.thread.tl.unseen = 0;
    scrollToBottom();
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
  if (event.ctrlKey && !event.shiftKey && !event.altKey && event.key.toLowerCase() === 'i') {
    event.preventDefault();
    toggleInspector();
  }
  if (event.ctrlKey && event.key === 'Enter' && event.target.id === 'composer-goal') {
    event.preventDefault();
    event.target.form.requestSubmit();
  }
});

// Composer fields update the in-memory draft; the textarea itself is never re-rendered.
document.addEventListener('input', event => {
  if (event.target.id === 'resume-reason') { state.thread.instructionText = event.target.value; return; }
  if (event.target.dataset.key === 'tl-search') {
    const tl = state.thread.tl;
    tl.searchDraft = event.target.value;
    if (!tl.searchDraft.trim() && tl.search) { tl.search = ''; loadPage('latest'); }  // Clearing the box shows everything again.
    return;
  }
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
  if (field === 'player') { state.thread.player = event.target.value; renderView(); return; }
  const projectId = composerProject(state.route, state.data);
  if (!projectId || !['agent', 'transport', 'project'].includes(field)) return;
  if (field === 'project') { location.hash = href('p', event.target.value, 'new'); return; }
  draftFor(projectId)[field === 'agent' ? 'agentId' : 'transport'] = event.target.value;
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
  const form = event.target.closest('[data-form="composer"]');
  if (!form) return;
  event.preventDefault();
  startFromComposer(form.dataset.project);
});

$('#nav-toggle').addEventListener('click', toggleSidebar);
// <details> toggle does not bubble, so listen in the capture phase.
document.addEventListener('toggle', event => { if (event.target.matches?.('.tl-card > details')) fillBody(event.target.parentElement); }, true);
$('#view').addEventListener('scroll', () => {
  const tl = state.thread.tl;
  if (state.route.name === 'instance' && tl?.unseen && nearBottom()) { tl.unseen = 0; renderView(); }
}, { passive: true });
// Relative times tick locally; nothing is fetched.
setInterval(() => { if (state.route.name === 'instance' && !document.hidden) updateTimes(timelineList()); }, 15000);
window.addEventListener('hashchange', onRoute);
// Hidden: the camera stops at once. Visible again: data refreshes and the camera reconnects.
document.addEventListener('visibilitychange', () => {
  renderView();
  if (!document.hidden) { poll(); pollRun(); }
});

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
