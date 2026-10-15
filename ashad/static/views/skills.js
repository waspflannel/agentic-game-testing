// Project Skills page. Fetched on view open and Refresh only; the model and markup are pure.
import { esc, icon, href, since } from '../core.js';

export const skillsSkeleton = () => `<div class="page skills-page"><div class="page-head row-between" data-region="head"></div><div data-region="list"></div></div>`;

const KIND_WORDS = { subgoal: 'Subgoal', decision: 'Decision' };

function lastResultHTML(last) {
  if (!last) return 'Not used yet';
  const words = `${last.result === 'won' ? 'Worked' : 'Failed'} on build ${esc(last.build_id || 'unknown')}`;
  const regression = last.regression ? ` <span class="skill-regression">${icon('alert')}(likely regression)</span>` : '';
  return `${words}${regression} · <time data-ts="${esc(last.at)}"></time>`;
}

function skillRow(skill, deleting) {
  const busy = deleting === skill.id ? ' aria-disabled="true" aria-busy="true"' : '';
  return `<li class="skill-row"><code class="mono skill-name">${esc(skill.name)}</code>`
    + `<span class="skill-meta">${esc(KIND_WORDS[skill.kind] || skill.kind)}</span>`
    + `<span class="skill-meta skill-uses">${skill.uses} ${skill.uses === 1 ? 'use' : 'uses'} · ${skill.wins} worked, ${skill.fails} failed</span>`
    + `<span class="skill-meta">${lastResultHTML(skill.last_result)}</span>`
    + `<button type="button" class="btn btn-ghost btn-sm" data-action="skill-delete" data-id="${esc(skill.id)}" data-key="sd-${esc(skill.id)}" aria-label="Delete skill: ${esc(skill.name)}"${busy}>${icon('trash')}Delete</button></li>`;
}

// page: { loading, rows, error, at, deleting } or null before the first fetch.
export function skillsRegions(projectId, projectName, page, now = Date.now() / 1000) {
  const head = `<div><h1>Skills</h1><p><a href="${href('p', projectId)}" data-key="skills-project">${esc(projectName)}</a></p></div>`
    + `<div class="btn-row"><span class="hint">${page?.at ? `Fetched ${since(page.at, now)}` : ''}</span>`
    + `<button type="button" class="btn btn-secondary" data-action="skills-refresh" data-key="skills-refresh"${page?.loading ? ' aria-busy="true" aria-disabled="true"' : ''}>${icon(page?.loading ? 'loader' : 'refresh', page?.loading ? 'icon spin' : 'icon')}Refresh</button></div>`;
  let list;
  if (!page?.rows) {
    list = page?.error ? `<p class="note note-error" data-announce>${icon('alert')}<span>Skills are unavailable: ${esc(page.error)}. Refresh to try again.</span></p>`
      : `<p class="note">${icon('loader', 'icon spin')}<span>Loading skills…</span></p>`;
  } else if (!page.rows.length) {
    list = `<p class="hint">None saved yet. A skill is saved when a goal is reached.</p>`;
  } else {
    list = `<ol class="skill-list">${page.rows.map(s => skillRow(s, page.deleting)).join('')}</ol>`;
  }
  return { head, list };
}
