// Setup flow (spec §8.4). Model, markup and the tuning flow are pure; app.js owns the DOM, the chooser and I/O.
import { esc, icon, href, ago, newOp, submitOnce } from '../core.js';

const enc = encodeURIComponent;
export const STEPS = ['Project', 'Build', 'Desktop', 'Agent and model', 'Tuning', 'Instructions'];

// One setup per project ('' until step 1 creates it). Ops keep their request IDs while the page is closed.
export const newSetup = projectId => ({
  projectId: projectId || '', step: 0, name: '', project: newOp(),
  buildId: '', chooser: { busy: false, error: '' }, catalog: null, readiness: null, prepare: newOp(),
  agentName: '', model: '', edited: false, tune: newOp(), agent: null,
});

export const projectBuilds = (data, projectId) => (data?.builds || []).filter(b => b.project_id === projectId);

// Step 4 starts from "<build> agent" and the signed-in model until the user edits either.
export function applyDefaults(data, s) {
  const build = projectBuilds(data, s.projectId).find(b => b.id === s.buildId);
  if (s.edited || !build) return;
  s.agentName ||= `${build.name || build.id} agent`;
  const models = (data?.model_options || []).map(m => m.id);
  s.model ||= models.includes(data?.ai?.model) ? data.ai.model : models[0] || '';
}

// Steps are 1-based. Earlier steps' completion comes from data; desktop readiness and tuning from this session.
export function setupModel(data, s) {
  const project = (data?.projects || []).find(p => p.id === s.projectId) || null;
  const builds = project ? projectBuilds(data, project.id) : [];
  const build = builds.find(b => b.id === s.buildId) || null;
  const check = build && s.readiness?.buildId === build.id ? s.readiness : null;
  const r = check && !check.loading && !check.error ? check.result : null;
  const envReady = (r?.environments || []).some(e => e.ready === true);
  const signedIn = data?.ai?.status === 'ready';
  const done = [Boolean(project), Boolean(build), Boolean(r?.build_validation?.ready && envReady),
    Boolean(signedIn && s.agentName.trim() && s.model), Boolean(s.agent), false];
  const first = done.indexOf(false) + 1;
  const step = Math.min(Math.max(s.step || first, 1), first);
  const reasons = [
    'Name the project to continue.',
    'Add or choose a build to continue.',
    !check || check.loading ? 'Checking readiness…' : check.error ? 'Re-check readiness to continue.'
      : !r.build_validation?.ready ? 'The build is not validated.' : 'No desktop is ready yet. Prepare one below.',
    !signedIn ? 'Sign in to ChatGPT first.' : !s.agentName.trim() ? 'Name the configuration.' : 'Choose a model.',
    s.tune.status === 'busy' ? 'Tuning is running.' : 'Run tuning to continue.',
    '',
  ];
  return { project, builds, build, check, done, first, step, signedIn, blocked: done[step - 1] ? '' : reasons[step - 1] };
}

// Unsaved input: a typed project name not yet created, or step 4 edits not yet tuned.
export const setupDirty = s => Boolean(s && ((!s.projectId && s.name.trim() && s.project.status !== 'busy')
  || (s.edited && !s.agent && s.tune.status !== 'busy')));

export function discardSetup(s) {
  Object.assign(s, { name: '', agentName: '', model: '', edited: false });
}

// ── Tuning ──────────────────────────────────────────────────────────────────
// One tuning runs at a time: "already being tuned" waits and resends the same request until the user stops waiting.
// A lost reply is recovered through tuning/<request_id> (completed | in_progress | unknown); it is never resent blind.
export async function tuneAgent(op, payload, api, wait = ms => new Promise(resolve => setTimeout(resolve, ms))) {
  const send = id => api('/api/agents/tune', { method: 'POST', body: { ...payload, request_id: id } });
  const recover = async id => {
    for (;;) {
      const r = await api(`/api/agents/tuning/${enc(id)}`);
      if (r?.status === 'completed' && r.agent) return { status: 'done', value: r.agent, error: '' };
      if (r?.status !== 'in_progress') return { status: 'error', sent: null, error: 'Tuning did not finish and nothing was saved. Retry sends the same request.' };
      op.status = 'busy';
      await wait(3000);
    }
  };
  for (;;) {
    await submitOnce(op, payload, send, recover);
    op.waiting = op.status === 'error' && /already being tuned/i.test(op.error);
    if (op.waiting && !op.stopWaiting) await wait(5000);
    if (!op.waiting || op.stopWaiting) {
      if (op.waiting) Object.assign(op, { waiting: false, error: 'Stopped waiting. Start tuning again when the other tuning has finished.' });
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

// ── Markup ──────────────────────────────────────────────────────────────────
const since = (t, now) => { const a = ago(t, now); return a === 'now' ? 'just now' : `${a} ago`; };
export const elapsed = seconds => { const s = Math.max(0, Math.floor(seconds)); return `${Math.floor(s / 60)}m ${String(s % 60).padStart(2, '0')}s`; };
const note = (kind, text, extra = '') => `<p class="note${kind ? ` note-${kind}` : ''}"${kind === 'error' ? ' data-announce' : ''}>${icon(kind === 'error' ? 'alert' : 'info')}<span>${text}</span>${extra}</p>`;

// Desktop readiness checklist, shared with the project overview. record: { loading, result, error, at }.
export function readinessHTML(record, now) {
  if (!record || record.loading) return `<p class="note">${icon('loader', 'icon spin')}<span>Checking this computer. This probes each desktop and can take a few seconds.</span></p>`;
  if (record.error) return note('error', `Readiness check failed: ${esc(record.error)}`);
  const r = record.result;
  const row = (ok, label, detail) => `<li class="check-row">${ok === true ? icon('check', 'icon check-ok') : ok === false ? icon('alert', 'icon check-bad') : icon('info', 'icon check-info')}`
    + `<span class="check-text"><span>${esc(label)}</span><span class="check-detail">${esc(detail)}</span></span></li>`;
  const envs = r.environments || [];
  const rows = [
    row(r.provider?.ready === true, 'ChatGPT and model', r.provider?.ready ? `${r.provider.model || 'Model'} available` : r.provider?.message || 'Not available'),
    row(r.build_validation?.ready === true, 'Build validation', r.build_validation?.reason || 'Not validated'),
    ...(envs.length ? envs.map(e => row(e.ready === true, e.name || e.id, e.ready ? 'Ready' : e.reason || 'Not ready'))
      : [row(false, 'Desktops', r.build_validation?.ready ? 'No desktop is registered. Prepare one, or add one under Settings › Desktops.' : 'Checked once the build validates')]),
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
      + `<span id="setup-name-hint" class="hint">Usually the game's name. Builds, agent configurations and instances are grouped under it.</span></div></form>`;
  } else if (step === 4) {
    const models = data?.model_options || [];
    fields = `<form class="setup-form" data-form="setup-agent"><div class="field"><label for="setup-agent-name">Configuration name</label>`
      + `<input id="setup-agent-name" class="input" data-field="setup-agent-name" maxlength="200" autocomplete="off" value="${esc(s.agentName)}"><span class="hint">Shown in the composer's agent selector.</span></div>`
      + `<div class="field"><label for="setup-model">Model</label><select id="setup-model" class="select" data-field="setup-model">`
      + models.map(o => `<option value="${esc(o.id)}"${o.id === s.model ? ' selected' : ''}>${esc(o.label || o.id)}</option>`).join('')
      + `</select><span class="hint">Tuning and every turn of this agent use this model through your ChatGPT subscription.</span></div></form>`;
  }
  return `<div class="center-col setup-page"><h1 class="hero">Set up a game agent</h1><nav data-region="stepper" aria-label="Setup progress"></nav>`
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
      const others = (data?.projects || []).map(p => `<li><a href="#/setup?project=${enc(p.id)}" data-key="sp-${esc(p.id)}">${esc(p.name || p.id)}</a></li>`).join('');
      return (op.status === 'busy' ? `<p class="note">${icon('loader', 'icon spin')}<span>Creating the project…</span></p>`
        : op.error ? note('error', esc(op.error), op.status === 'lost' ? ' <button type="button" class="btn btn-secondary btn-sm" data-action="setup-create" data-key="sp-retry">Retry</button>' : '') : '')
        + (others ? `<div class="setup-aside"><p class="hint">Or add an agent configuration to an existing project:</p><ul class="link-list">${others}</ul></div>` : '');
    }
    case 2: {
      const list = m.builds.map(b => `<li><label class="radio-row"><input type="radio" name="setup-build" data-field="setup-build" value="${esc(b.id)}"${b.id === s.buildId ? ' checked' : ''}>`
        + `<span class="radio-text"><span>${esc(b.name || b.id)}</span><span class="mono check-detail" title="${esc(b.manifest_sha256 || '')}">${esc(b.source_revision ? String(b.source_revision).slice(0, 40) : b.id)}</span></span></label></li>`).join('');
      const chooser = s.chooser;
      const catalog = !m.build ? '' : !s.catalog || s.catalog.buildId !== m.build.id || s.catalog.loading ? `<p class="note">${icon('loader', 'icon spin')}<span>Reading the build catalog…</span></p>`
        : s.catalog.error ? note('', `Catalog unavailable: ${esc(s.catalog.error)}`)
        : `<p class="note">${icon('list-checks')}<span>Catalog: ${esc(catalogSummary(s.catalog.result) || 'no goals or items listed')}</span></p>`;
      return (list ? `<ul class="radio-list" aria-label="Builds in this project">${list}</ul>` : `<p class="lede">No build is registered for this project yet.</p>`)
        + `<div class="btn-row"><button type="button" class="btn btn-secondary" data-action="setup-add-build" data-kind="manifest" data-key="sb-manifest"${chooser.busy ? ' aria-disabled="true"' : ''}>${icon('file-text')}Choose manifest.json…</button>`
        + `<button type="button" class="btn btn-secondary" data-action="setup-add-build" data-kind="folder" data-key="sb-folder"${chooser.busy ? ' aria-disabled="true"' : ''}>${icon('folder-open')}Choose build folder…</button></div>`
        + (chooser.busy ? `<p class="note">${icon('loader', 'icon spin')}<span>Registering the build…</span></p>` : chooser.error ? note('error', esc(chooser.error)) : '')
        + catalog;
    }
    case 3: {
      const op = s.prepare;
      const prep = op.status === 'busy' ? `<p class="note">${icon('loader', 'icon spin')}<span>Preparing the desktop. This can take several minutes; you can leave this page and it keeps going.</span></p>`
        : op.error ? note('error', esc(op.error)) : '';
      return `<div class="row-between"><p class="hint">Probes run when this step opens and when you re-check, never in the background.</p>`
        + `<button type="button" class="btn btn-ghost btn-sm" data-action="setup-recheck" data-key="s3-recheck"${m.check?.loading ? ' aria-disabled="true"' : ''}>${icon('refresh')}Re-check</button></div>`
        + readinessHTML(m.check, now)
        + `<div class="btn-row"><button type="button" class="btn ${m.done[2] ? 'btn-secondary' : 'btn-primary'}" data-action="setup-prepare" data-key="s3-prepare"${op.status === 'busy' || m.check?.loading ? ' aria-disabled="true"' : ''}>${icon('monitor')}Prepare desktop…</button></div>${prep}`;
    }
    case 4:
      return m.signedIn ? `<p class="note">${icon('check')}<span>ChatGPT is signed in.</span></p>`
        : `<p class="note note-error">${icon('alert')}<span>ChatGPT is not signed in. Tuning and every instance need it. <a href="#/settings/account" data-key="s4-signin">Sign in under Settings › Account</a>, then come back.</span></p>`;
    case 5: {
      const op = s.tune;
      const model = (data?.model_options || []).find(o => o.id === s.model)?.label || s.model;
      const summary = `<dl class="kv"><dt>Configuration</dt><dd>${esc(s.agentName.trim())}</dd><dt>Model</dt><dd>${esc(model)}</dd><dt>Build</dt><dd>${esc(m.build?.name || s.buildId)}</dd></dl>`;
      const about = `<p class="hint">Tuning reads the build's source and writes a controls manual for the agent. This prepares instructions, not model weights.</p>`;
      if (op.waiting) return summary + `<p class="note">${icon('clock')}<span>Another agent is being tuned. Only one tuning runs at a time, so this one starts when that finishes.</span>`
        + ` <button type="button" class="btn btn-secondary btn-sm" data-action="setup-stop-waiting" data-key="s5-stop">Stop waiting</button></p>`;
      if (op.status === 'busy') return summary + `<div class="tuning"><progress class="progress" aria-label="Tuning in progress"></progress>`
        + `<p>Tuning… <time data-elapsed="${op.startedAt}">${elapsed(now - op.startedAt)}</time> elapsed. You can leave this page; tuning keeps going.</p></div>` + about;
      return summary + about + (op.error ? note('error', esc(op.error)) : '');
    }
    case 6: {
      const a = s.agent || {};
      const model = (data?.model_options || []).find(o => o.id === a.model)?.label || a.model;
      return `<dl class="kv"><dt>Configuration</dt><dd>${esc(a.name || a.id)}</dd><dt>Model</dt><dd>${esc(model || 'Unknown')}</dd><dt>Build</dt><dd>${esc(m.build?.name || a.build_id)}</dd>`
        + `<dt>Tuned</dt><dd>${a.created_at ? esc(new Date(a.created_at * 1000).toLocaleString()) : 'Not recorded'}</dd></dl>`
        + `<pre class="instructions" tabindex="0" aria-label="Saved instructions">${esc(a.instructions || 'No instructions were returned.')}</pre>`;
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
  } else if (m.step === 5 && !s.agent && s.tune.status !== 'busy' && !s.tune.waiting) {
    const off5 = !m.done[3];
    primary = `<button type="button" class="btn btn-primary" data-action="setup-tune" data-key="sn-tune"${off5 ? ' aria-disabled="true"' : ''}>${icon('play')}${s.tune.status === 'idle' ? 'Start tuning' : 'Retry tuning'}</button>`;
  } else if (m.step === 6) {
    primary = `<a class="btn btn-primary" href="${href('p', s.projectId, 'new')}?agent=${enc(s.agent?.id || '')}" data-action="setup-finish" data-key="sn-start">${icon('arrow-up')}Start an instance</a>`;
  } else {
    primary = `<button type="button" class="btn btn-primary" data-action="setup-step" data-step="${m.step + 1}" data-key="sn-next"${off}>Continue</button>`;
  }
  return `${back}<span class="setup-nav-right">${reason}${primary}</span>`;
}

export const prepareDialogText = build => `ASHAD creates or reuses a private desktop for ${build?.name || build?.id || 'this build'} on this computer: `
  + 'its container, images, volumes and service credentials. It never starts the game. This can take several minutes; leaving the page does not cancel it.';

