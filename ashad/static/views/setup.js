// Setup flow (spec §8.4). Model, markup and the Get controls flow are pure; app.js owns the DOM, the chooser and I/O.
import { esc, icon, href, since, newOp, submitOnce } from '../core.js';

const enc = encodeURIComponent;
export const STEPS = ['Project', 'Build', 'Desktop', 'Get controls'];

// One setup per project ('' until step 1 creates it). Ops keep their request IDs while the page is closed.
export const newSetup = projectId => ({
  projectId: projectId || '', step: 0, name: '', project: newOp(),
  buildId: '', chooser: { busy: false, error: '' }, catalog: null, readiness: null, prepare: newOp(),
  agentName: '', edited: false, controls: newOp(), agent: null,
});

export const projectBuilds = (data, projectId) => (data?.builds || []).filter(b => b.project_id === projectId);

// Get game controls always runs Claude Opus, so only Claude's sign-in matters here.
const claudeReady = data => data?.ai_claude?.status === 'ready';

// Step 4 starts from the project's name until the user edits it.
export function applyDefaults(data, s) {
  const build = projectBuilds(data, s.projectId).find(b => b.id === s.buildId);
  if (s.edited || !build) return;
  const project = (data?.projects || []).find(p => p.id === s.projectId);
  s.agentName ||= `${project?.name || build.name || build.id}`;
}

// Steps are 1-based. Earlier steps' completion comes from data; desktop readiness and game controls from this session.
export function setupModel(data, s) {
  const project = (data?.projects || []).find(p => p.id === s.projectId) || null;
  const builds = project ? projectBuilds(data, project.id) : [];
  const build = builds.find(b => b.id === s.buildId) || null;
  const check = build && s.readiness?.buildId === build.id ? s.readiness : null;
  const r = check && !check.loading && !check.error ? check.result : null;
  const envReady = (r?.environments || []).some(e => e.ready === true);
  const signedIn = claudeReady(data);
  const done = [Boolean(project), Boolean(build), Boolean(r?.build_validation?.ready && envReady),
    Boolean(s.agent)];
  const first = done.includes(false) ? done.indexOf(false) + 1 : done.length;
  const step = Math.min(Math.max(s.step || first, 1), first);
  const reasons = [
    'Name the project to continue.',
    'Add or choose a build to continue.',
    !check || check.loading ? 'Checking readiness…' : check.error ? 'Re-check readiness to continue.'
      : !r.build_validation?.ready ? 'The build is not validated.' : 'No desktop is ready yet. Prepare one below.',
    s.controls.status === 'busy' ? 'Getting game controls…' : !signedIn ? 'Connect Claude to get game controls.' : !s.agentName.trim() ? 'Name the configuration.' : '',
  ];
  return { project, builds, build, check, done, first, step, signedIn, blocked: done[step - 1] ? '' : reasons[step - 1] };
}

// Unsaved input: a typed project name not yet created, or step 4 edits not yet sent.
export const setupDirty = s => Boolean(s && ((!s.projectId && s.name.trim() && s.project.status !== 'busy')
  || (s.edited && !s.agent && s.controls.status !== 'busy')));

export function discardSetup(s) {
  Object.assign(s, { name: '', agentName: '', edited: false });
}

// ── Get game controls ──────────────────────────────────────────────────────────────────
// One run at a time: "already being gathered" waits and resends the same request until the user stops waiting.
// A lost reply is recovered through controls/<request_id> (completed | in_progress | unknown); it is never resent blind.
export async function getControls(op, payload, api, wait = ms => new Promise(resolve => setTimeout(resolve, ms))) {
  const send = id => api('/api/configurations/controls', { method: 'POST', body: { ...payload, request_id: id } });
  const recover = async id => {
    for (;;) {
      const r = await api(`/api/configurations/controls/${enc(id)}`);
      if (r?.status === 'completed' && r.configuration) return { status: 'done', value: r.configuration, error: '' };
      if (r?.status !== 'in_progress') return { status: 'error', sent: null, error: 'Getting game controls did not finish and nothing was saved. Retry sends the same request.' };
      op.status = 'busy';
      await wait(3000);
    }
  };
  for (;;) {
    await submitOnce(op, payload, send, recover);
    op.waiting = op.status === 'error' && /already being gathered/i.test(op.error);
    if (op.waiting && !op.stopWaiting) await wait(5000);
    if (!op.waiting || op.stopWaiting) {
      if (op.waiting) Object.assign(op, { waiting: false, error: 'Stopped waiting. Get game controls again when the other run has finished.' });
      op.stopWaiting = false;
      return op;
    }
  }
}

// Preparation is idempotent per request ID; a retry after an error or a lost reply resumes the same request.
export const prepareDesktop = (op, projectId, buildId, api) => submitOnce(op, { build_id: buildId },
  id => api(`/api/projects/${enc(projectId)}/environments/prepare`, { method: 'POST', body: { build_id: buildId, request_id: id, confirmed: true } }));

// Native chooser, then POST /api/builds. Only `form` (the form that opened the chooser) changes; null when nothing was added.
export async function registerBuild(form, projectId, kind, { bridge, api, onBusy = () => {} }) {
  if (!bridge?.chooseBuild) { form.chooser = { busy: false, error: 'Choosing a build needs the ASHAD desktop app.' }; return null; }
  let build = null;
  try {
    const manifest = await bridge.chooseBuild(kind);
    if (!manifest) return null;  // Cancelled.
    form.chooser = { busy: true, error: '' };
    onBusy();
    build = await api('/api/builds', { method: 'POST', body: { manifest_path: manifest, project_id: projectId } });
    form.buildId = build.id;
    form.chooser = { busy: false, error: '' };
  } catch (error) { form.chooser = { busy: false, error: error.message }; }
  return build;
}

// Live progress from GET /api/configurations/controls/<id> (op.progress), shared with the overview's refresh row.
// Batches and the final call are counted steps; a long silence is called out so a stall doesn't look like work.
export const STALL_SECONDS = 180;
export function controlsProgressHTML(op, now) {
  const p = op.progress;
  const steps = p?.batch_count ? p.batch_count + 1 : p?.stage === 'writing' ? 1 : 0;
  const done = p?.stage === 'exploring' ? p.batches_done : p?.stage === 'writing' ? p.batch_count || 0 : 0;
  const stage = !p ? 'Starting…' : p.stage === 'reading_source' ? 'Reading the build source'
    : p.stage === 'exploring' ? `Reading source: ${p.batches_done} of ${p.batch_count} batches done, ${p.batches_running ?? 0} being read now`
      : `Writing the game controls${p.batch_count ? ` from ${p.batch_count} batches of notes` : ''}`;
  const bar = steps ? `<progress class="progress" max="${steps}" value="${done}" aria-label="Get controls progress, step ${done + 1} of ${steps}"></progress>`
    : `<progress class="progress" aria-label="Get controls in progress"></progress>`;
  const started = op.startedAt || p?.started_at || now;
  const lastAt = p?.last_call_at || p?.started_at || started;
  const calls = !p ? '' : `${p.model_calls ?? 0} model ${(p.model_calls ?? 0) === 1 ? 'reply' : 'replies'}${p.rejected_replies ? ` (${p.rejected_replies} rejected and retried)` : ''}`
    + ` · ${p.last_call_at ? 'last reply' : 'started'} <time data-elapsed="${lastAt}">${elapsed(now - lastAt)}</time> ago`;
  const stalled = p && now - lastAt > STALL_SECONDS
    ? `<p class="note note-attention">${icon('clock')}<span>No model reply for ${Math.floor((now - lastAt) / 60)} minutes. A call usually takes under 2 minutes; the provider may be slow or rate limited. Getting game controls is still running.</span></p>` : '';
  const progressHint = op.progressAt && now - op.progressAt > 15
    ? `<p class="hint">Progress last updated <time data-elapsed="${op.progressAt}">${elapsed(now - op.progressAt)}</time> ago.</p>` : '';
  return `${bar}<p>${esc(stage)} · <time data-elapsed="${started}">${elapsed(now - started)}</time> elapsed</p>${calls ? `<p class="hint">${calls}</p>` : ''}${progressHint}${stalled}`;
}

// ── Markup ──────────────────────────────────────────────────────────────────
export const elapsed = seconds => { const s = Math.max(0, Math.floor(seconds)); return `${Math.floor(s / 60)}m ${String(s % 60).padStart(2, '0')}s`; };
const noteHTML = (kind, text, extra = '') => `<p class="note${kind ? ` note-${kind}` : ''}"${kind === 'error' ? ' data-announce' : ''}>${icon(kind === 'error' ? 'alert' : 'info')}<span>${text}</span>${extra}</p>`;

const PROVIDER_LABELS = { claude: 'Claude', codex: 'Codex', grok: 'Grok' };
const providersSetText = ids => ids?.length ? `${ids.map(id => PROVIDER_LABELS[id] || id).join(' and ')} set` : 'AI provider set';

// Desktop readiness checklist, shared with the project overview. record: { loading, result, error, at }.
export function readinessHTML(record, now) {
  if (!record || record.loading) return `<p class="note">${icon('loader', 'icon spin')}<span>Checking this computer. This probes each desktop and can take a few seconds.</span></p>`;
  if (record.error) return noteHTML('error', `Readiness check failed: ${esc(record.error)}`);
  const r = record.result;
  const row = (ok, label, detail) => `<li class="check-row">${ok === true ? icon('check', 'icon check-ok') : ok === false ? icon('alert', 'icon check-bad') : icon('info', 'icon check-info')}`
    + `<span class="check-text"><span>${esc(label)}</span><span class="check-detail">${esc(detail)}</span></span></li>`;
  const envs = r.environments || [];
  const rows = [
    row(r.provider?.ready === true, 'AI provider', r.provider?.ready ? providersSetText(r.provider.providers) : r.provider?.message || 'Not available'),
    row(r.build_validation?.ready === true, 'Build validation', r.build_validation?.reason || 'Not validated'),
    ...(envs.length ? envs.map(e => row(e.ready === true, e.name || e.id, e.ready ? 'Ready' : e.reason || 'Not ready'))
      : [row(false, 'Desktops', r.build_validation?.ready ? (r.other_build_desktops ? `No desktop is registered for this build (${r.other_build_desktops} registered for other builds). Remove them under Settings › Desktops, or Prepare one.` : 'No desktop is registered. Prepare one, or add one under Settings › Desktops.') : 'Checked once the build validates')]),
    row(r.slot?.available !== false, 'Instance slot', r.slot?.available === false ? r.slot.blocked_reason || 'Taken' : 'Free'),
    row(r.steam?.ready ? true : null, 'Steam (optional)', r.steam?.reason || 'Unknown'),
  ];
  return `<ul class="checks" aria-label="Readiness">${rows.join('')}</ul><p class="check-age">Checked ${esc(since(record.at, now))}</p>`;
}

export const catalogSummary = c => ['goals', 'items', 'recipes', 'biomes', 'islands'].filter(k => Array.isArray(c?.[k])).map(k => `${c[k].length} ${k}`).join(' · ');

function stepper(m, s) {
  return `<ol class="stepper" aria-label="Setup steps">${STEPS.map((label, i) => {
    const n = i + 1;
    const inner = `<span class="step-num">${n < m.first && n !== m.step ? icon('check') : n}</span><span class="step-label">${esc(label)}</span>`;
    if (n === m.step) return `<li><span class="step is-current" aria-current="step">${inner}</span></li>`;
    if (n < m.first) return `<li><button type="button" class="step is-done" data-action="setup-step" data-step="${n}" data-key="st-${n}">${inner}<span class="sr-only"> (done, review)</span></button></li>`;
    return `<li><span class="step">${inner}</span></li>`;
  }).join('')}</ol>`;
}

// The skeleton holds the step's inputs and mounts once per (setup, step), so a poll never re-creates a field.
export function setupSkeleton(data, s, step) {
  let fields = '';
  if (step === 1 && !s.projectId) {
    fields = `<form class="setup-form" data-form="setup-project"><div class="field"><label for="setup-name">Project name</label>`
      + `<input id="setup-name" class="input" data-field="setup-name" maxlength="200" autocomplete="off" spellcheck="false" placeholder="For example, Agronauts" value="${esc(s.name)}" aria-describedby="setup-name-hint">`
      + `<span id="setup-name-hint" class="hint">Usually the game's name. Builds, project configurations and instances are grouped under it.</span></div></form>`;
  } else if (step === 4) {
    fields = `<form class="setup-form" data-form="setup-agent"><div class="field"><label for="setup-agent-name">Configuration name</label>`
      + `<input id="setup-agent-name" class="input" data-field="setup-agent-name" maxlength="200" autocomplete="off" value="${esc(s.agentName)}" aria-describedby="setup-agent-name-hint"><span id="setup-agent-name-hint" class="hint">Shown when you start an instance.</span></div></form>`;
  }
  return `<div class="center-col setup-page"><h1 class="hero">Set up a game</h1><nav data-region="stepper" aria-label="Setup progress"></nav>`
    + `<section class="card setup-card" aria-labelledby="setup-step-title"><h2 id="setup-step-title" class="setup-title" tabindex="-1">${step}. ${esc(STEPS[step - 1])}</h2>`
    + `${fields}<div data-region="main" class="setup-main"></div><div data-region="nav" class="setup-nav"></div></section></div>`;
}

export function setupRegions(data, s, m, now = Date.now() / 1000) {
  return { stepper: stepper(m, s), main: mainHTML(data, s, m, now), nav: navHTML(s, m) };
}

function mainHTML(data, s, m, now) {
  switch (m.step) {
    case 1: {
      if (m.project) return `<p>Project <strong>${esc(m.project.name || m.project.id)}</strong> exists. Continue to its build.</p>`;
      const op = s.project;
      const others = (data?.projects || []).map(p => `<li class="link-row"><a href="#/setup?project=${enc(p.id)}" data-key="sp-${esc(p.id)}">${esc(p.name || p.id)}</a></li>`).join('');
      return (op.status === 'busy' ? `<p class="note">${icon('loader', 'icon spin')}<span>Creating the project…</span></p>`
        : op.error ? noteHTML('error', esc(op.error), op.status === 'lost' ? ' <button type="button" class="btn btn-secondary btn-sm" data-action="setup-create" data-key="sp-retry">Retry</button>' : '') : '')
        + (others ? `<div class="setup-aside"><p class="hint">Or add a project configuration to an existing project:</p><ul class="link-list">${others}</ul></div>` : '');
    }
    case 2: {
      const list = m.builds.map(b => `<li><label class="radio-row"><input type="radio" name="setup-build" data-field="setup-build" value="${esc(b.id)}"${b.id === s.buildId ? ' checked' : ''}>`
        + `<span class="radio-text"><span>${esc(b.name || b.id)}</span><span class="mono check-detail" title="${esc(b.manifest_sha256 || '')}">${esc(b.source_revision ? String(b.source_revision).slice(0, 40) : b.id)}</span></span></label></li>`).join('');
      const chooser = s.chooser;
      const catalog = !m.build ? '' : !s.catalog || s.catalog.buildId !== m.build.id || s.catalog.loading ? `<p class="note">${icon('loader', 'icon spin')}<span>Reading the build catalog…</span></p>`
        : s.catalog.error ? noteHTML('', `Catalog unavailable: ${esc(s.catalog.error)}`)
        : `<p class="note">${icon('list-checks')}<span>Catalog: ${esc(catalogSummary(s.catalog.result) || 'no goals or items listed')}</span></p>`;
      return (list ? `<ul class="radio-list" aria-label="Builds in this project">${list}</ul>` : `<p class="lede">No build is registered for this project yet.</p>`)
        + `<div class="btn-row"><button type="button" class="btn btn-secondary" data-action="setup-add-build" data-kind="manifest" data-key="sb-manifest"${chooser.busy ? ' aria-disabled="true"' : ''}>${icon('file-text')}Choose manifest.json…</button>`
        + `<button type="button" class="btn btn-secondary" data-action="setup-add-build" data-kind="folder" data-key="sb-folder"${chooser.busy ? ' aria-disabled="true"' : ''}>${icon('folder-open')}Choose build folder…</button></div>`
        + (chooser.busy ? `<p class="note">${icon('loader', 'icon spin')}<span>Registering the build…</span></p>` : chooser.error ? noteHTML('error', esc(chooser.error)) : '')
        + catalog;
    }
    case 3: {
      const op = s.prepare;
      const prep = op.status === 'busy' ? `<p class="note">${icon('loader', 'icon spin')}<span>Preparing the desktop. This can take several minutes; you can leave this page and it keeps going.</span></p>`
        : op.error ? noteHTML('error', esc(op.error)) : '';
      return `<div class="row-between"><p class="hint">Probes run when this step opens and when you re-check, never in the background.</p>`
        + `<button type="button" class="btn btn-ghost btn-sm" data-action="setup-recheck" data-key="s3-recheck"${m.check?.loading ? ' aria-disabled="true"' : ''}>${icon('refresh')}Re-check</button></div>`
        + readinessHTML(m.check, now)
        + `<div class="btn-row"><button type="button" class="btn ${m.done[2] ? 'btn-secondary' : 'btn-primary'}" data-action="setup-prepare" data-key="s3-prepare"${op.status === 'busy' || m.check?.loading ? ' aria-disabled="true"' : ''}>${icon('monitor')}Prepare desktop…</button></div>${prep}`;
    }
    case 4: {
      if (s.agent) {
        const a = s.agent;
        return `<dl class="kv"><dt>Configuration</dt><dd>${esc(a.name || a.id)}</dd><dt>Build</dt><dd>${esc(m.build?.name || a.build_id)}</dd><dt>Gathered by</dt><dd>Claude Opus</dd>`
          + `<dt>Gathered</dt><dd>${a.created_at ? esc(new Date(a.created_at * 1000).toLocaleString()) : 'Not recorded'}</dd></dl>`
          + `<div class="btn-row"><button type="button" class="btn btn-secondary btn-sm" data-action="ov-controls" data-id="${esc(a.id)}" data-key="s4-view">View controls</button></div>`;
      }
      const op = s.controls;
      const about = `<p class="hint">ASHAD reads the build source with up to 4 Claude Opus workers and writes the game controls file every instance loads, whichever model it uses.</p>`;
      if (op.waiting) return about + `<p class="note">${icon('clock')}<span>Another Get game controls run is in progress; this one starts when it finishes.</span>`
        + ` <button type="button" class="btn btn-secondary btn-sm" data-action="setup-stop-waiting" data-key="s4-stop">Stop waiting</button></p>`;
      if (op.status === 'busy') return about + `<div class="tuning">${controlsProgressHTML(op, now)}<p class="hint">You can leave this page; it keeps going.</p></div>`;
      if (!m.signedIn) return `<p class="note note-error">${icon('alert')}<span>Getting game controls uses Claude Opus. <a href="#/settings/account" data-key="s4-signin">Connect Claude under Settings › Account</a>, then come back. Instances can still use Codex.</span></p>`;
      return about + (op.error ? noteHTML('error', esc(op.error)) : '');
    }
    default: return '';
  }
}

function navHTML(s, m) {
  const back = m.step > 1 ? `<button type="button" class="btn btn-ghost" data-action="setup-step" data-step="${m.step - 1}" data-key="sn-back">Back</button>` : '<span></span>';
  const blocked = m.step === 1 && !m.project && (s.name.trim() || s.project.status !== 'idle') ? '' : m.blocked;
  const reason = blocked ? `<span class="setup-reason" id="setup-reason">${esc(m.blocked)}</span>` : '';
  const off = blocked ? ' aria-disabled="true" aria-describedby="setup-reason"' : '';
  let primary;
  if (m.step === 1 && !m.project) {
    const busy = s.project.status === 'busy';
    const off1 = !s.name.trim() || busy || s.project.status === 'lost';
    primary = `<button type="button" class="btn btn-primary" data-action="setup-create" data-key="sn-create"${off1 ? ' aria-disabled="true"' : ''}${busy ? ' aria-busy="true"' : ''}>Create project</button>`;
  } else if (m.step === 4 && !s.agent && s.controls.status !== 'busy' && !s.controls.waiting) {
    const off4 = !m.signedIn || !s.agentName.trim();
    primary = `<button type="button" class="btn btn-primary" data-action="setup-controls" data-key="sn-controls"${off4 ? ' aria-disabled="true"' : ''}>${icon('play')}${s.controls.status === 'idle' ? 'Get game controls' : 'Retry'}</button>`;
  } else if (m.step === 4 && s.agent) {
    primary = `<a class="btn btn-primary" href="${href('p', s.projectId, 'new')}?configuration=${enc(s.agent?.id || '')}" data-action="setup-finish" data-key="sn-start">${icon('arrow-up')}Start an instance</a>`;
  } else {
    primary = `<button type="button" class="btn btn-primary" data-action="setup-step" data-step="${m.step + 1}" data-key="sn-next"${off}>Continue</button>`;
  }
  return `${back}<span class="setup-nav-right">${reason}${primary}</span>`;
}

export const prepareDialogText = build => `ASHAD creates or reuses a private desktop for ${build?.name || build?.id || 'this build'} on this computer: `
  + 'its container, images, volumes and service credentials. It never starts the game. This can take several minutes; leaving the page does not cancel it.';

