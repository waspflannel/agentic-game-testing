import { esc, icon, href, ago, runGoal, runTime, runStatus } from '../core.js';

const THREADS_SHOWN = 10;
const byNewest = (a, b) => (runTime(b) || 0) - (runTime(a) || 0);

// ui: { expanded: Map<groupId, bool>, showAll: Set<groupId>, route, lastProject, now }
export function sidebarModel(data, ui) {
  const projects = data?.projects || [];
  const runs = [...(data?.runs || [])].sort(byNewest);
  const owners = data?.instance_slot?.owners || [];
  const ownerByRun = new Map(owners.map(o => [o.run_id, o]));
  const route = ui.route || {};
  const currentRun = route.name === 'instance' ? runs.find(r => r.id === route.id) : undefined;
  const projectIds = new Set(projects.map(p => p.id));
  const routeProject = ['project', 'composer'].includes(route.name) ? route.id : currentRun?.project_id;

  const thread = run => ({ id: run.id, goal: runGoal(run), age: ago(runTime(run), ui.now), status: runStatus(run, ownerByRun.get(run.id)), current: run.id === currentRun?.id });
  const group = (id, groupRuns, extra) => {
    const holdsFocus = groupRuns.some(r => r.id === currentRun?.id || ownerByRun.has(r.id));
    const expanded = ui.expanded.has(id) ? ui.expanded.get(id) : holdsFocus || id === routeProject;
    const showAll = ui.showAll.has(id);
    return { id, expanded, total: groupRuns.length, showAll,
      threads: expanded ? groupRuns.slice(0, showAll ? undefined : THREADS_SHOWN).map(thread) : [], ...extra };
  };

  const ownerRun = owners.length ? runs.find(r => r.id === owners[0].run_id) : undefined;
  const active = owners.length && !(route.name === 'instance' && route.id === owners[0].run_id)
    ? { runId: owners[0].run_id, linked: Boolean(ownerRun), goal: ownerRun ? runGoal(ownerRun) : 'Unresolved instance owner',
        status: runStatus(ownerRun || { state: owners[0].state }, owners[0]), age: ago(runTime(ownerRun), ui.now) }
    : null;

  const unassignedRuns = runs.filter(r => !projectIds.has(r.project_id));
  const newProject = routeProject && projectIds.has(routeProject) ? routeProject
    : projectIds.has(ui.lastProject) ? ui.lastProject : projects[0]?.id;
  return {
    newInstanceHref: newProject ? href('p', newProject, 'new') : '#/setup',
    projects: projects.map(p => group(p.id, runs.filter(r => r.project_id === p.id), { name: p.name || p.id, current: routeProject === p.id && !currentRun })),
    unassigned: unassignedRuns.length ? group('unassigned', unassignedRuns, { name: 'Unassigned' }) : null,
    active,
    signInNeeded: data?.ai?.status !== 'ready',
    route: route.name,
  };
}

const dot = status => `<span class="dot dot-${status.tone}" title="${esc(status.label)}"><span class="sr-only">${esc(status.label)}</span></span>`;

function threadRow(t) {
  return `<li><a class="row thread${t.current ? ' is-current' : ''}" href="${href('i', t.id)}" data-key="r:${esc(t.id)}" title="${esc(t.goal)}"${t.current ? ' aria-current="page"' : ''}>`
    + `${dot(t.status)}<span class="row-label">${esc(t.goal)}</span><span class="row-meta">${esc(t.age)}</span></a></li>`;
}

function groupBlock(g, isProject) {
  const name = esc(g.name);
  const tools = isProject
    ? `<a class="row-tool" href="${href('p', g.id)}" data-key="po:${esc(g.id)}" aria-label="Project overview: ${name}" title="Project overview">${icon('ellipsis')}</a>`
      + `<a class="row-tool" href="${href('p', g.id, 'new')}" data-key="pn:${esc(g.id)}" aria-label="New instance in ${name}" title="New instance">${icon('plus')}</a>`
    : '';
  let body = '';
  if (g.expanded) {
    body = g.threads.map(threadRow).join('');
    if (g.total > THREADS_SHOWN) body += `<li><button type="button" class="row row-more" data-action="show-all" data-id="${esc(g.id)}" data-key="sa:${esc(g.id)}">${g.showAll ? 'Show fewer' : `Show all (${g.total})`}</button></li>`;
    if (isProject) body += `<li><a class="row row-new" href="${href('p', g.id, 'new')}" data-key="nn:${esc(g.id)}">${icon('plus')}<span class="row-label">New instance</span></a></li>`;
    if (!g.total && !isProject) body = '';
    body = `<ul class="threads">${body}</ul>`;
  }
  return `<li class="group"><div class="row group-row${g.current ? ' is-current' : ''}">`
    + `<button type="button" class="row-main" aria-expanded="${g.expanded}" data-action="toggle-group" data-id="${esc(g.id)}" data-key="g:${esc(g.id)}">`
    + `${icon('chevron-right', 'icon chevron')}${icon(g.expanded ? 'folder-open' : 'folder')}<span class="row-label">${name}</span></button>${tools}</div>${body}</li>`;
}

export function sidebarHTML(m) {
  const active = m.active
    ? (m.active.linked
      ? `<a class="active-pill" href="${href('i', m.active.runId)}" data-key="active">`
      : `<div class="active-pill">`)
      + `${dot(m.active.status)}<span class="active-text"><span class="active-state">${esc(m.active.status.label)}</span><span class="row-label">${esc(m.active.goal)}</span></span><span class="row-meta">${esc(m.active.age)}</span>`
      + (m.active.linked ? '</a>' : '</div>')
    : '';
  const tree = m.projects.map(g => groupBlock(g, true)).join('') + (m.unassigned ? groupBlock(m.unassigned, false) : '');
  return `<div class="side-top"><a class="btn btn-secondary btn-block" href="${esc(m.newInstanceHref)}" data-key="new">${icon('square-pen')}New instance</a></div>`
    + `<div class="side-scroll" data-scroll="side">`
    + `<div class="side-heading"><h2 id="projects-heading">Projects</h2><a class="icon-btn icon-btn-sm" href="#/setup" data-key="setup" aria-label="Set up new game agent" title="Set up new game agent">${icon('plus')}</a></div>`
    + (tree ? `<ul class="tree" aria-labelledby="projects-heading">${tree}</ul>` : `<p class="side-empty">No game projects yet.</p>`)
    + `</div><div class="side-foot">${active}`
    + `<a class="row foot-link${m.route === 'usage' ? ' is-current' : ''}" href="#/usage" data-key="usage"${m.route === 'usage' ? ' aria-current="page"' : ''}>${icon('chart-column')}<span class="row-label">Usage</span></a>`
    + `<a class="row foot-link${m.route === 'settings' ? ' is-current' : ''}" href="#/settings" data-key="settings"${m.route === 'settings' ? ' aria-current="page"' : ''}>${icon('sliders')}<span class="row-label">Settings</span>`
    + (m.signInNeeded ? `<span class="dot dot-attention" title="ChatGPT sign-in needed"><span class="sr-only">ChatGPT sign-in needed</span></span>` : '')
    + `</a></div>`;
}
