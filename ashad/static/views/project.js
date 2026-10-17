// Project overview (spec §8.3). Model and markup are pure; app.js owns the DOM, the chooser and I/O.
import { esc, icon, href, dropdownHTML, ago, since, newOp, runGoal, runTime, runStatus } from '../core.js';
import { readinessHTML, projectBuilds, controlsProgressHTML } from './setup.js';

const enc = encodeURIComponent;
const RECENT = 10;

// Per-project UI. The chooser, preparation and Get controls refreshes keep running while the page is closed.
export const newOverview = projectId => ({ projectId, buildId: '', readiness: null, validation: new Map(), chooser: { busy: false, error: '' }, prepare: newOp(), refreshes: new Map() });

// Readiness probes the machine, so it is due only when no probe exists for the selected build: on view open
// (app.js clears the record), on Re-check and after a preparation. A poll re-render never makes it due.
export const readinessDue = (ov, buildId) => Boolean(buildId) && ov.readiness?.buildId !== buildId;

const duration = run => {
  const start = run.started_at || run.created_at, end = run.ended_at;
  if (!Number.isFinite(start) || !Number.isFinite(end)) return '';
  const m = Math.max(0, Math.round((end - start) / 60));
  return m < 60 ? `${m}m` : `${Math.floor(m / 60)}h ${m % 60}m`;
};

export function overviewModel(data, ov, now = Date.now() / 1000) {
  const project = (data?.projects || []).find(p => p.id === ov.projectId);
  const builds = projectBuilds(data, ov.projectId);
  const build = builds.find(b => b.id === ov.buildId) || builds[0] || null;
  const buildName = id => builds.find(b => b.id === id)?.name || id;
  const agents = (data?.configurations || []).filter(a => (a.project_id || builds.find(b => b.id === a.build_id)?.project_id) === ov.projectId)
    .sort((a, b) => (b.created_at || 0) - (a.created_at || 0));
  const owners = new Map((data?.instance_slot?.owners || []).map(o => [o.run_id, o]));
  const runs = (data?.runs || []).filter(r => r.project_id === ov.projectId).sort((a, b) => (runTime(b) || 0) - (runTime(a) || 0));
  return {
    busy: runs.some(r => owners.has(r.id)),
    project, builds, build, agents, hasReady: agents.some(a => a.status !== 'retired'), total: runs.length,
    recent: runs.slice(0, RECENT).map(r => ({ id: r.id, goal: runGoal(r), age: ago(runTime(r), now), status: runStatus(r, owners.get(r.id)), state: r.state, reason: r.ending_reason, duration: duration(r) })),
    buildName,
  };
}

export const validatedProjectBuilds = (data, projectId, validation) => projectBuilds(data, projectId)
  .filter(b => validation.get(b.id)?.ready === true)
  .sort((a, b) => (b.created_at || 0) - (a.created_at || 0));

export const defaultRegatherBuildId = (agent, validated) => (
  validated.some(b => b.id === agent.build_id) ? agent.build_id : validated[0]?.id || ''
);

export function configurationPinWarning(agent, builds, validation) {
  if (!agent || agent.status === 'retired') return '';
  const pinned = agent.build_id;
  if (!builds.some(b => b.id === pinned)) {
    return `Pinned build ${pinned} is no longer registered. Get controls again on a current build to continue.`;
  }
  const check = validation.get(pinned);
  if (check?.ready === false && !check.unchecked) {
    return `Pinned build ${pinned} is no longer valid. Get controls again on a current build to continue.`;
  }
  return '';
}

export function regatherDialogHTML({ validated, selectedBuildId, buildName, agentName }) {
  const picker = dropdownHTML({ key: 'ov-refresh-build', field: 'ov-refresh-build', label: 'Build to regather on', iconName: 'file-text',
    options: validated.map(b => ({ id: b.id, label: b.name || b.id })), selected: selectedBuildId });
  const chosen = buildName(selectedBuildId);
  return `<input type="hidden" name="build_id" id="refresh-build-id" value="${esc(selectedBuildId)}">${picker}`
    + `<p id="refresh-build-desc">ASHAD re-reads <strong>${esc(chosen)}</strong> with Claude Opus and updates project configuration “${esc(agentName)}”. This takes a few minutes and uses your Claude subscription.</p>`;
}

export const overviewSkeleton = () => `<div class="page overview"><div class="page-head row-between" data-region="head"></div>`
  + `<div class="overview-grid"><section class="card" aria-labelledby="ov-builds"><h2 id="ov-builds" class="card-title">Builds</h2><div data-region="builds"></div></section>`
  + `<section class="card" aria-labelledby="ov-agents"><h2 id="ov-agents" class="card-title">Project configurations</h2><div data-region="agents"></div></section>`
  + `<section class="card" aria-labelledby="ov-ready"><h2 id="ov-ready" class="card-title">Desktop readiness</h2><div data-region="readiness"></div></section>`
  + `<section class="card" aria-labelledby="ov-recent"><h2 id="ov-recent" class="card-title">Recent instances</h2><div data-region="recent"></div></section></div></div>`;

const note = (kind, text) => `<p class="note${kind ? ` note-${kind}` : ''}"${kind === 'error' ? ' data-announce' : ''}>${icon(kind === 'error' ? 'alert' : kind === 'busy' ? 'loader' : 'info', kind === 'busy' ? 'icon spin' : 'icon')}<span>${esc(text)}</span></p>`;

export function overviewRegions(m, ov, now = Date.now() / 1000) {
  const pid = ov.projectId;
  const head = `<h1>${esc(m.project.name || m.project.id)}</h1><div class="btn-row">`
    + `<button type="button" class="btn btn-ghost" data-action="ov-delete-project" data-key="ov-delete-project"${m.busy ? ' aria-disabled="true" title="Stop its active instance before deleting this project"' : ''}>${icon('trash')}Delete project</button>`
    + (m.hasReady ? '' : `<a class="btn btn-secondary" href="#/setup?project=${enc(pid)}" data-key="ov-add-agent">${icon('plus')}Add project configuration</a>`)
    + `<a class="btn btn-secondary" href="${href('p', pid, 'skills')}" data-key="ov-skills">${icon('list-checks')}Skills</a>`
    + `<a class="btn btn-secondary" href="${href('p', pid, 'issues')}" data-key="ov-issues">${icon('flag')}Issues</a>`
    + `<a class="btn btn-primary" href="${href('p', pid, 'new')}" data-key="ov-new">${icon('square-pen')}New instance</a></div>`;

  const validation = b => {
    const v = ov.validation.get(b.id);
    return !v ? 'Not checked yet' : v.ready ? 'Validated' : `${v.unchecked ? 'Not checked' : 'Not validated'}: ${v.reason || 'reason not reported'}`;
  };
  const builds = (m.builds.length ? `<ul class="list">${m.builds.map(b => `<li class="list-row"><div class="list-main"><span class="list-title">${esc(b.name || b.id)}</span>`
    + `<span class="mono check-detail" title="${esc(b.manifest_sha256 || '')}">${esc(String(b.source_revision || b.manifest_sha256 || b.id).slice(0, 48))}</span>`
    + `<span class="check-detail">${ov.validation.get(b.id)?.ready ? icon('check', 'icon check-ok') : ov.validation.get(b.id) ? icon('alert', 'icon check-bad') : ''}${esc(validation(b))}</span></div>`
    + `<div class="list-actions"><button type="button" class="btn btn-ghost btn-sm" data-action="ov-delete-build" data-id="${esc(b.id)}" data-key="ov-del-${esc(b.id)}">Remove</button></div></li>`).join('')}</ul>`
    : `<p class="hint">No build is registered yet.</p>`)
    + `<div class="btn-row"><button type="button" class="btn btn-ghost btn-sm" data-action="ov-add-build" data-kind="manifest" data-key="ov-manifest"${ov.chooser.busy ? ' aria-disabled="true"' : ''}>${icon('plus')}Add manifest…</button>`
    + `<button type="button" class="btn btn-ghost btn-sm" data-action="ov-add-build" data-kind="folder" data-key="ov-folder"${ov.chooser.busy ? ' aria-disabled="true"' : ''}>${icon('plus')}Add build folder…</button></div>`
    + (ov.chooser.busy ? note('busy', 'Registering the build…') : ov.chooser.error ? note('error', ov.chooser.error) : '');

  const agents = m.agents.length ? `<ul class="list">${m.agents.map(a => {
    const title = `<span class="list-title">${esc(a.name || a.id)}</span>`;
    const pinWarning = configurationPinWarning(a, m.builds, ov.validation);
    const op = ov.refreshes.get(a.id);
    const busy = op && (op.status === 'busy' || op.waiting);
    const status = !op ? '' : op.waiting ? note('', 'Another Get game controls run is in progress; this one starts when it finishes.')
      : op.status === 'busy' ? `<div class="tuning">${controlsProgressHTML(op, now)}</div>`
      : op.status === 'done' ? note('', 'Game controls updated for this configuration.') : op.error ? note('error', op.error) : '';
    const refresh = `<button type="button" class="btn btn-ghost btn-sm" data-action="ov-refresh" data-id="${esc(a.id)}" data-key="ov-refresh-${esc(a.id)}"${busy ? ' aria-busy="true" aria-disabled="true"' : ''}>Get controls again…</button>`;
    if (a.status === 'retired') {
      return `<li class="list-row"><div class="list-main">${title}${note('error', `Retired: ${a.retired_reason || 'reason not recorded'}`)}${status}</div>`
        + `<div class="list-actions">${refresh}</div></li>`;
    }
    return `<li class="list-row"><div class="list-main">${title}`
      + (pinWarning ? `<p class="note note-attention">${icon('alert')}<span>${esc(pinWarning)}</span></p>` : '')
      + `<span class="check-detail">build <span class="mono" title="${esc(m.buildName(a.build_id))}">${esc(a.build_id)}</span> · controls gathered ${a.created_at ? esc(since(a.created_at, now)) : 'at an unrecorded time'}</span>${status}</div>`
      + `<div class="list-actions"><button type="button" class="btn btn-ghost btn-sm" data-action="ov-controls" data-id="${esc(a.id)}" data-key="ov-ctl-${esc(a.id)}">View controls</button>`
      + refresh
      + `<a class="btn btn-ghost btn-sm" href="${href('p', pid, 'new')}?configuration=${enc(a.id)}" data-key="ov-run-${esc(a.id)}">New instance</a></div></li>`;
  }).join('')}</ul>` : `<p class="hint">No project configuration yet. <a href="#/setup?project=${enc(pid)}" data-key="ov-setup">Add one</a> to start instances.</p>`;

  let readiness;
  if (!m.build) readiness = `<p class="hint">Add a build to check this computer.</p>`;
  else {
    const picker = m.builds.length > 1 ? dropdownHTML({ key: 'ov-build', field: 'ov-build', label: 'Build to check', iconName: 'file-text', options: m.builds.map(b => ({ id: b.id, label: b.name || b.id })), selected: m.build.id }) : `<span class="hint">For build ${esc(m.build.name || m.build.id)}</span>`;
    const check = ov.readiness?.buildId === m.build.id ? ov.readiness : null;
    const op = ov.prepare;
    readiness = `<div class="row-between">${picker}<button type="button" class="btn btn-ghost btn-sm" data-action="ov-recheck" data-key="ov-recheck"${check?.loading ? ' aria-disabled="true"' : ''}>${icon('refresh')}Re-check</button></div>`
      + readinessHTML(check, now)
      + `<div class="btn-row"><button type="button" class="btn btn-secondary" data-action="ov-prepare" data-key="ov-prepare"${op.status === 'busy' || check?.loading ? ' aria-disabled="true"' : ''}>${icon('monitor')}Prepare desktop…</button></div>`
      + (op.status === 'busy' ? note('busy', 'Preparing the desktop. This can take several minutes; leaving the page does not cancel it.') : op.error ? note('error', op.error) : '');
  }

  const recent = m.recent.length ? `<ul class="list recent">${m.recent.map(r => `<li><a class="row thread" href="${href('i', r.id)}" data-key="ov-r-${esc(r.id)}" title="${esc(r.goal)}">`
    + `<span class="dot dot-${r.status.tone}" aria-hidden="true"></span><span class="row-label">${esc(r.goal)}</span>`
    + `<span class="row-meta">${esc([r.state === 'Ended' ? `${r.status.label}${r.reason ? ` · ${r.reason.replaceAll('_', ' ')}` : ''}` : r.status.label, r.duration, r.age].filter(Boolean).join(' · '))}</span></a></li>`).join('')}</ul>`
    + (m.total ? `<button type="button" class="btn btn-ghost btn-sm" data-action="ov-sidebar" data-key="ov-sidebar">View all ${m.total} in sidebar</button>` : '')
    : `<p class="hint">No instances yet.</p>`;

  return { head, builds, agents, readiness, recent };
}
