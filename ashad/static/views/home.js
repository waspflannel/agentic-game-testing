// Empty workspace and the new-instance composer (spec §8.1, §8.2). Markup and the start flow are pure; app.js owns the DOM.
import { esc, icon, href, newId, menuHTML, modelMenuHTML, ApiError } from '../core.js';

const enc = encodeURIComponent;
const chipDot = tone => `<span class="dot dot-${tone}" aria-hidden="true"></span>`;

// ── Empty workspace ─────────────────────────────────────────────────────────
export function emptyHTML(data) {
  const signedIn = data?.ai?.status === 'ready' || data?.ai_claude?.status === 'ready';
  const desktops = (data?.workers || []).length;
  const item = (ok, label, detail, fix) => `<li class="prereq">${ok === null ? icon('info', 'icon prereq-info') : ok ? icon('check', 'icon prereq-ok') : icon('alert', 'icon prereq-missing')}`
    + `<span class="prereq-text"><span>${esc(label)}</span><span class="prereq-detail">${esc(detail)}</span></span>${fix && !ok ? fix : ''}</li>`;
  return `<div class="center-col empty">`
    + `<h1 class="hero">Set up your first game</h1>`
    + `<p class="lede">ASHAD prepares a desktop, gathers the game's controls from your build, then runs one bot against a goal you give it.</p>`
    + `<ul class="prereqs" aria-label="Prerequisites">`
    + item(signedIn, 'AI provider connected', signedIn ? 'Ready' : 'Not signed in', `<a href="#/settings/account" data-key="fix-account">Sign in</a>`)
    + item(desktops > 0, 'A worker desktop registered', desktops ? `${desktops} registered` : 'None registered', `<a href="#/settings/desktops" data-key="fix-desktops">Add a desktop</a>`)
    + item(null, 'A build manifest or folder on this computer', 'Checked during setup')
    + `</ul><a class="btn btn-primary" href="#/setup" data-key="setup-first">Set up a game</a></div>`;
}

// ── Composer model ──────────────────────────────────────────────────────────
// Saved, ready configurations whose build is registered in this project.
export function projectConfigurations(data, projectId) {
  const builds = new Map((data?.builds || []).map(b => [b.id, b]));
  return (data?.configurations || []).filter(a => builds.has(a.build_id)
    && (a.project_id || builds.get(a.build_id).project_id) === projectId
    && [undefined, 'ready', 'completed'].includes(a.status));  // Retired configurations are hidden.
}

export const readinessKey = (projectId, agent, model) => `${projectId}|${agent?.build_id}|${agent?.id}|${model}`;
export const newDraft = () => ({ goal: '', configurationId: '', model: '', transport: 'enet', requestId: null, sent: null, busy: false, result: null });

export const draftModel = (data, draft) => ((data?.model_options || []).some(m => m.id === draft.model) ? draft.model : data?.default_model);

function desktopReason(r) {
  if (r.build_validation?.ready !== true) return r.build_validation?.reason || 'Build is not validated';
  const envs = r.environments || [];
  if (!envs.some(e => e.ready === true)) return envs.find(e => e.reason)?.reason || 'No desktop is registered';
  return '';
}

// readiness: { key, loading, result, error } from the probe run on view open. A record for another agent or build is ignored.
export function composerModel(data, projectId, draft, readiness, fixer) {
  const project = (data?.projects || []).find(p => p.id === projectId);
  const agents = projectConfigurations(data, projectId);
  const agent = agents.find(a => a.id === draft.configurationId) || agents[0];
  const build = agent && (data.builds || []).find(b => b.id === agent.build_id);
  const model = agent && draftModel(data, draft);
  const check = agent && readiness?.key === readinessKey(projectId, agent, model) ? readiness : null;
  const r = check && !check.loading && !check.error ? check.result : null;
  const slot = data?.instance_slot || {};
  const owner = slot.owners?.[0];

  // Steam only from a fresh probe for this exact build; bootstrap account rows are history, never readiness.
  const steam = r?.steam?.ready === true ? { enabled: true, reason: '' }
    : { enabled: false, reason: r ? `Steam unavailable: ${r.steam?.reason || 'readiness unknown'}` : 'Steam unavailable until readiness is checked' };

  let desktop;
  if (slot.available === false && owner) desktop = { tone: 'live', label: 'Instance running', href: href('i', owner.run_id) };
  else if (!check || check.loading) desktop = { tone: 'pending', label: 'Checking desktop…' };
  else if (check.error) desktop = { tone: 'attention', label: `Not ready: ${check.error}`, href: href('p', projectId) };
  else if (desktopReason(r)) desktop = { tone: 'attention', label: `Not ready: ${desktopReason(r)}`, href: href('p', projectId) };
  else desktop = { tone: 'live', label: 'Desktop ready' };

  const blockers = [];
  if (slot.available === false) blockers.push(slot.blocked_reason || 'Another instance owns the slot.');
  if (data?.ai?.status !== 'ready' && data?.ai_claude?.status !== 'ready') blockers.push('No AI provider is connected. Connect one under Settings › Account.');
  else if (r && r.provider?.ready === false) blockers.push(r.provider.message || 'The model is not available.');
  if (slot.available !== false) {
    if (!check || check.loading) blockers.push('Checking desktop readiness…');
    else if (check.error) blockers.push(`Readiness check failed: ${check.error}`);
    else if (desktopReason(r)) blockers.push(`Desktop not ready: ${desktopReason(r)}`);
  }
  if (draft.transport === 'steam' && !steam.enabled) blockers.push(`${steam.reason}. No ENet fallback.`);

  return {
    fixer: fixer || { ready: false, reasons: ['Checking the fixer…'] },
    project, agents, agent, build, model, modelOptions: data?.model_options || [], steam, desktop, blockers,
    projects: data?.projects || [],
    agentOptions: agents.map(a => ({ id: a.id, label: a.name || a.id, title: `${a.name || a.id} · build ${(data?.builds || []).find(b => b.id === a.build_id)?.name || a.build_id}` })),
    summary: agent ? `build ${build?.name || agent.build_id} · Fresh world` : '',
    defaultModel: data?.default_model,
    canStart: !blockers.length && !draft.busy,
  };
}

// ── Composer markup (regions patched independently, so the textarea is never re-created by a poll) ──
export function composerSkeleton(projectId, draft) {
  return `<div class="center-col composer-page">`
    + `<h1 class="hero" data-region="hero"></h1>`
    + `<div class="composer-chips" data-region="chips"></div>`
    + `<form class="composer" data-form="composer" data-project="${esc(projectId)}" novalidate>`
    + `<label class="sr-only" for="composer-goal">Goal</label>`
    + `<textarea id="composer-goal" class="composer-goal" data-field="goal" maxlength="4000" required aria-describedby="composer-goal-error composer-status"`
    + ` placeholder="Describe the goal, e.g. finish the tutorial and craft a stone pickaxe">${esc(draft.goal)}</textarea>`
    + `<div id="composer-goal-error" data-region="goal-error"></div>`
    + `<div class="composer-bar" data-region="bar"></div></form>`
    + `<div id="composer-status" class="composer-status" data-region="status"></div></div>`;
}

export function noConfigurationsHTML(project) {
  return `<div class="center-col empty"><h1 class="hero">This project has no project configuration yet</h1>`
    + `<p class="lede">A project configuration pairs a build of <strong>${esc(project.name || project.id)}</strong> with the game controls gathered from it.</p>`
    + `<a class="btn btn-primary" href="#/setup?project=${enc(project.id)}" data-key="add-agent">Add project configuration</a></div>`;
}

// Fixes picker. While the fixer is not ready (or still being checked) Manual and Auto are aria-disabled and say why.
function fixMenuHTML(fixer, fix) {
  const blocked = !fixer.ready;
  const reason = fixer.reasons[0];
  const gated = (id, label, secondary) => ({ id, label, chipLabel: `Fixes: ${id}`, disabled: blocked, secondary: blocked ? reason : secondary });
  return menuHTML({
    key: 'c-fix', field: 'fix', action: 'pick-fix', label: 'Fixes', iconName: 'wrench', selected: fix || 'off',
    options: [{ id: 'off', label: 'Off', chipLabel: 'Fixes off' }, gated('manual', 'Manual', 'You merge each fix PR'), gated('auto', 'Auto', 'ASHAD merges, checks and reverts')],
    footer: blocked ? fixer.reasons.map(r => `<p>${esc(r)}</p>`).join('') : '',
  });
}

export function composerRegions(m, draft) {
  const name = esc(m.project.name || m.project.id);
  const projectChip = `<label class="chip chip-select">${icon('folder')}<span class="sr-only">Project</span>`
    + `<select data-field="project" data-key="c-project" aria-label="Project">${m.projects.map(p => `<option value="${esc(p.id)}"${p.id === m.project.id ? ' selected' : ''}>${esc(p.name || p.id)}</option>`).join('')}</select></label>`;
  const desktopChip = m.desktop.href
    ? `<a class="chip chip-${m.desktop.tone}" href="${esc(m.desktop.href)}" data-key="c-desktop">${m.desktop.tone === 'attention' ? icon('alert') : chipDot(m.desktop.tone)}<span class="chip-text">${esc(m.desktop.label)}</span></a>`
    : `<span class="chip chip-${m.desktop.tone}">${chipDot(m.desktop.tone)}<span class="chip-text">${esc(m.desktop.label)}</span></span>`;
  const recheck = `<button type="button" class="icon-btn icon-btn-sm" data-action="composer-recheck" data-key="c-recheck" aria-label="Re-check readiness" title="Re-check readiness">${icon('refresh')}</button>`;

  const agentChip = `<label class="chip chip-select" title="${esc(m.agentOptions.find(o => o.id === m.agent.id)?.title || '')}">${icon('bot')}<span class="sr-only">Configuration</span>`
    + `<select data-field="configuration" data-key="c-agent" aria-label="Project configuration">${m.agentOptions.map(o => `<option value="${esc(o.id)}"${o.id === m.agent.id ? ' selected' : ''}>${esc(o.label)}</option>`).join('')}</select></label>`;
  const modelChip = modelMenuHTML({ key: 'c-model', field: 'model', options: m.modelOptions, selected: m.model, defaultModel: m.defaultModel });
  const transportChip = `<label class="chip chip-select"${m.steam.enabled ? '' : ` title="${esc(m.steam.reason)}"`}>${icon('network')}<span class="sr-only">Transport</span>`
    + `<select data-field="transport" data-key="c-transport" aria-label="Transport" aria-describedby="composer-steam-note">`
    + `<option value="enet"${draft.transport === 'enet' ? ' selected' : ''}>ENet</option>`
    + `<option value="steam"${draft.transport === 'steam' ? ' selected' : ''}${m.steam.enabled ? '' : ' disabled'}>Steam${m.steam.enabled ? '' : ' (unavailable)'}</option></select></label>`;
  const fixChip = fixMenuHTML(m.fixer, draft.fix);
  const start = `<button type="submit" class="btn btn-primary btn-round" data-key="c-start" aria-label="Start instance" title="Start instance (Ctrl+Enter)"`
    + `${m.canStart ? '' : ' aria-disabled="true"'}${draft.busy ? ' aria-busy="true"' : ''}>${icon(draft.busy ? 'loader' : 'arrow-up', draft.busy ? 'icon spin' : 'icon')}</button>`;

  const res = draft.result;
  const goalError = res?.kind === 'error' && res.field === 'goal' ? `<p class="field-error" data-announce>${esc(res.message)}</p>` : '';
  const notes = [];
  if (draft.busy) notes.push(`<p class="note">${icon('loader', 'icon spin')}Starting… This can take a minute while the desktop is checked.</p>`);
  else if (res) notes.push(resultHTML(res));
  if (!m.steam.enabled) notes.push(draft.transport === 'steam' ? `<p class="note" id="composer-steam-note">${esc(m.steam.reason)}.</p>` : `<span class="sr-only" id="composer-steam-note">${esc(m.steam.reason)}.</span>`);
  if (!draft.busy && m.blockers.length) notes.push(`<div class="note note-blocked"><p>Start is unavailable:</p><ul>${m.blockers.map(b => `<li>${esc(b)}</li>`).join('')}</ul></div>`);
  notes.push(`<p class="note note-hint"><span><kbd>Ctrl</kbd>+<kbd>Enter</kbd> to start</span></p>`);

  return {
    hero: `What should the agent do in <strong>${name}</strong>?`,
    chips: projectChip + desktopChip + recheck,
    'goal-error': goalError,
    bar: `<div class="composer-bar-left">${agentChip}${modelChip}${transportChip}${fixChip}</div><div class="composer-bar-right"><span class="composer-summary" title="${esc(m.summary)}">${esc(m.summary)}</span>${start}</div>`,
    status: notes.join(''),
  };
}

function resultHTML(res) {
  const runLink = (id, label) => `<a href="${href('i', id)}" data-key="c-run">${esc(label)}</a>`;
  switch (res.kind) {
    case 'conflict': {
      const owner = res.owners?.[0]?.run_id;
      return `<p class="note note-error" data-announce>${icon('alert')}<span>${esc(res.message || 'Another instance owns the slot.')} ${owner ? runLink(owner, 'Open the running instance') + '.' : ''} Your goal is kept.</span></p>`;
    }
    case 'not-started':
      return `<p class="note note-error" data-announce>${icon('alert')}<span>Not started. ${esc(res.message || '')}</span> <button type="button" class="btn btn-secondary btn-sm" data-action="composer-retry" data-key="c-retry">Retry</button></p>`;
    case 'earlier-accepted':
      return `<p class="note">${icon('info')}<span>An earlier start was accepted: ${runLink(res.run.id, 'open that instance')}. Your changed goal was not sent; Start again to launch it separately.</span></p>`;
    case 'error':
      return res.field === 'goal' ? '' : `<p class="note note-error" data-announce>${icon('alert')}<span>${esc(res.message)}</span></p>`;
    default: return '';
  }
}

// ── Start flow ──────────────────────────────────────────────────────────────
// The request ID is bound to one payload. A retry with the same payload reuses it; a changed payload first learns
// what happened to the earlier one, so an accepted start is never silently resubmitted with different inputs.
// Returns { kind: 'accepted' | 'in-progress' | 'not-started' | 'conflict' | 'earlier-accepted' | 'error', ... }.
export async function startInstance(draft, projectId, api) {
  const payload = { configuration_id: draft.configurationId, project_id: projectId, primary_goal: draft.goal.trim(), transport: draft.transport, model: draft.model, fix_mode: draft.fix || 'off' };
  if (!payload.configuration_id) return { kind: 'error', field: 'configuration', message: 'Choose a project configuration.' };
  if (!payload.primary_goal) return { kind: 'error', field: 'goal', message: 'Describe the goal before starting.' };
  const signature = JSON.stringify(payload);
  if (draft.sent && draft.sent !== signature) {
    const prior = await checkStart(draft, api);
    if (prior.kind === 'accepted') { draft.requestId = newId(); draft.sent = null; return { kind: 'earlier-accepted', run: prior.run }; }
    if (prior.kind === 'in-progress') return prior;
    if (prior.unanswered) return prior;  // The earlier start may have been accepted; keep its ID and check again on retry.
    draft.requestId = newId();  // ASHAD said the earlier request never launched; this payload gets its own ID.
  }
  draft.requestId ||= newId();
  draft.sent = signature;
  try {
    return { kind: 'accepted', run: await api('/api/instances', { method: 'POST', body: { ...payload, request_id: draft.requestId } }) };
  } catch (error) {
    if (!(error instanceof ApiError) || error.status >= 500) return checkStart(draft, api);  // Reply lost or ambiguous.
    draft.sent = null;  // Refused before anything was stored; the same ID may carry corrected inputs.
    if (error.status === 409 && error.body?.code === 'active_instance_conflict') return { kind: 'conflict', owners: error.body.owning_instances || [], message: error.message };
    const loc = Array.isArray(error.body?.detail) ? error.body.detail[0]?.loc || [] : [];
    return { kind: 'error', field: loc.includes('primary_goal') ? 'goal' : '', message: error.message };
  }
}

export async function checkStart(draft, api) {
  let prior;
  try { prior = await api(`/api/instances/requests/${enc(draft.requestId)}`); } catch { prior = null; }
  if (prior?.status === 'accepted' && prior.run?.id) return { kind: 'accepted', run: prior.run };
  if (prior?.status === 'in_progress') return { kind: 'in-progress' };
  return { kind: 'not-started', unanswered: !prior, message: prior ? 'Nothing was launched. Retry sends the same request.' : 'ASHAD did not answer. Retry checks the same request first.' };
}
