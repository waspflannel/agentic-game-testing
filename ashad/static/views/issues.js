// Project Issues board and issue view. Fetched on view open and Refresh only; the model and markup are pure.
import { esc, icon, href, since, humanize, runGoal } from '../core.js';

export const ISSUE_TYPES = [['unsupported_action', 'Unsupported action', 'Unsupported actions'], ['crash', 'Crash', 'Crashes'], ['game_bug', 'Game bug', 'Game bugs']];
export const typeLabel = type => ISSUE_TYPES.find(([t]) => t === type)?.[1] || humanize(type);
export const statusWord = status => ({ open: 'Open', closed: 'Closed' })[status] || humanize(status);

const times = n => `${n} ${n === 1 ? 'time' : 'times'}`;
const loadingNote = text => `<p class="note">${icon('loader', 'icon spin')}<span>${text}</span></p>`;

export function boardModel(data, projectId, runId, board) {
  const runs = data?.runs || [];
  const project = (data?.projects || []).find(p => p.id === projectId);
  const run = runId ? runs.find(r => r.id === runId) : null;
  const row = r => {
    const last = runs.find(x => x.id === r.last_run_id);
    return { id: r.issue_id, title: r.title, status: r.status, count: r.occurrences, source: { id: r.last_run_id, label: last ? runGoal(last) : 'Deleted instance', linked: Boolean(last) },
      more: Math.max(0, (r.runs?.length || 0) - 1), first: r.first_at, last: r.last_at };
  };
  return {
    projectId, projectName: project?.name || projectId,
    runFilter: runId ? { id: runId, goal: run ? runGoal(run) : 'Deleted instance' } : null,
    groups: board?.rows ? ISSUE_TYPES.map(([type, , heading]) => ({ type, heading, rows: board.rows.filter(r => r.type === type).map(row) })) : null,
  };
}

export const boardSkeleton = () => `<div class="page issues-page"><div class="page-head row-between" data-region="head"></div><div data-region="filter"></div><div class="issue-board" data-region="groups"></div></div>`;

function boardRow(m, r) {
  const { source } = r;
  return `<li class="issue-board-row${r.status === 'closed' ? ' issue-closed' : ''}">`
    + `<a class="issue-board-title" href="${href('p', m.projectId, 'issues', r.id)}" data-key="ib-${esc(r.id)}">${esc(r.title)}</a>`
    + `<span class="issue-board-status">${esc(statusWord(r.status))}</span>`
    + `<span class="issue-board-count">${times(r.count)}</span>`
    + `<span class="issue-board-source">${source.linked ? `<a href="${href('i', source.id)}">${esc(source.label)}</a>` : esc(source.label)}${r.more ? ` +${r.more}` : ''}</span>`
    + `<span class="issue-board-seen">first <time data-ts="${esc(r.first)}"></time>, last <time data-ts="${esc(r.last)}"></time></span></li>`;
}

// board: { loading, rows, error, at } or null before the first fetch.
export function boardRegions(m, board, now = Date.now() / 1000) {
  const head = `<div><h1>Issues</h1><p><a href="${href('p', m.projectId)}" data-key="issues-project">${esc(m.projectName)}</a></p></div>`
    + `<div class="btn-row"><span class="hint">${board?.at ? `Fetched ${since(board.at, now)}` : ''}</span>`
    + `<button type="button" class="btn btn-secondary" data-action="issues-refresh" data-key="issues-refresh"${board?.loading ? ' aria-busy="true" aria-disabled="true"' : ''}>${icon(board?.loading ? 'loader' : 'refresh', board?.loading ? 'icon spin' : 'icon')}Refresh</button></div>`;
  const goal = m.runFilter?.goal;
  const filter = m.runFilter ? `<a class="chip issue-filter" href="${href('p', m.projectId, 'issues')}" data-key="issues-unfilter" aria-label="Remove filter: instance ${esc(goal)}"><span class="chip-text">Instance: ${esc(goal)}</span>${icon('circle-x')}</a>` : '';
  if (!m.groups) {
    const groups = board?.error ? `<p class="note note-error" data-announce>${icon('alert')}<span>Issues are unavailable: ${esc(board.error)}. Refresh to try again.</span></p>` : loadingNote('Loading issues…');
    return { head, filter, groups };
  }
  const groups = m.groups.map(({ type, heading, rows }) => `<section class="issue-group" aria-labelledby="ig-${type}"><h2 id="ig-${type}" class="issue-group-head">${heading} <span class="issue-count">${rows.length}</span></h2>`
    + (rows.length ? `<ol class="issue-board-list">${rows.map(r => boardRow(m, r)).join('')}</ol>`
      : `<p class="hint">${m.runFilter ? 'None in this instance.' : `No ${heading.toLowerCase()} recorded.`}</p>`) + `</section>`).join('');
  return { head, filter, groups };
}

export const issueSkeleton = () => `<div class="page issue-page"><div data-region="head"></div><div data-region="meta"></div><div data-region="examples"></div><div class="issue-info" data-region="info"></div></div>`;

// view: { record, loading, error, exampleId, pending }.
export function issueViewRegions(view, data) {
  const r = view.record;
  if (!r) {
    const head = view.error ? `<p class="note note-error" data-announce>${icon('alert')}<span>Issue is unavailable: ${esc(view.error)}.</span></p>` : loadingNote('Loading issue…');
    return { head, meta: '', examples: '', info: '' };
  }
  const closed = r.status === 'closed';
  const head = `<p><a href="${href('p', r.project_id, 'issues')}" data-key="issue-back">All issues</a></p>`
    + `<div class="page-head row-between"><div><p class="issue-type">${esc(typeLabel(r.type))}</p><h1>${esc(r.title)}</h1><p class="issue-status">${esc(statusWord(r.status))}</p></div>`
    + `<button type="button" class="btn btn-secondary" data-action="issue-status" data-status="${closed ? 'open' : 'closed'}" data-key="issue-status"${view.pending ? ' aria-busy="true" aria-disabled="true"' : ''}>${closed ? 'Reopen issue' : 'Close issue'}</button></div>`;
  const runs = r.runs || [];
  const meta = `<p class="issue-meta">${times(r.occurrences)} in ${runs.length} ${runs.length === 1 ? 'instance' : 'instances'}, first <time data-ts="${Date.parse(r.first_seen.at) / 1000}"></time>, last <time data-ts="${Date.parse(r.last_seen.at) / 1000}"></time></p>`
    + `<p class="issue-file"><span>File</span> <code>${esc(r.file)}</code></p>`;
  const selected = r.examples.find(e => e.example_id === view.exampleId) || r.examples.at(-1);
  let examples;
  if (!r.examples.length) examples = `<p class="hint">No example was kept for this issue.</p>`;
  else {
    const options = [...r.examples].reverse().map(ex => `<option value="${esc(ex.example_id)}"${ex === selected ? ' selected' : ''}>${esc(new Date(ex.at).toLocaleString())} · ${esc(humanize(ex.source))}</option>`).join('');
    const n = r.examples_dropped;
    examples = `<label class="chip chip-select">${icon('list-checks')}<span class="sr-only">Example</span><select data-field="issue-example" data-key="issue-example" aria-label="Example">${options}</select></label>`
      + (n > 0 ? `<p class="hint">${n} older ${n === 1 ? 'example was' : 'examples were'} not kept.</p>` : '');
  }
  const info = selected ? issueInfoHTML(r, selected, { runIds: new Set((data?.runs || []).map(x => x.id)) }) : '';
  return { head, meta, examples, info };
}

const section = (title, body) => `<section class="issue-info-section"><h2 class="section-title">${title}</h2>${body}</section>`;
const blank = v => v === null || v === undefined || v === '';
function fields(pairs) {
  const rows = pairs.filter(([, v]) => !blank(v)).map(([label, v]) => `<dt>${label}</dt><dd>${esc(Array.isArray(v) ? v.join(', ') : v)}</dd>`);
  return rows.length ? `<dl class="issue-fields">${rows.join('')}</dl>` : '';
}
const json = (summary, value) => `<details><summary>${summary}</summary><pre class="mono">${esc(typeof value === 'string' ? value : JSON.stringify(value, null, 2))}</pre></details>`;
const SHOT_NOTES = { pending: 'Saved when the instance ends.', deleted: 'Deleted with its instance.', missing: 'Frames were not retained.' };

export function issueInfoHTML(issue, example, { runIds }) {
  const type = issue.type;
  const { wanted, outcome, capabilities, crash, report } = example;
  const intent = example.intent || {};
  const sections = [];

  let happened = '';
  if (type === 'unsupported_action') {
    happened = fields([['Goal', intent.current_goal?.objective || intent.primary_goal], ['Verb', wanted?.verb], ['Tool', wanted?.tool],
      ['Target', [wanted?.target_label, wanted?.target_kind].filter(Boolean).join(', ')], ['Blocker', wanted?.blocker], ['Controller', wanted?.controller], ['Follow-up error', wanted?.follow_up_error]]);
    if (outcome) happened += fields([['Outcome', humanize(outcome.status)], ['Reason', outcome.reason], ['Error', outcome.error], ['Error code', outcome.error_code ?? outcome.http_status]]);
  } else if (type === 'crash') {
    happened = fields([['Kind', humanize(crash?.kind)], ['Detector', crash?.detector], ['Reason', crash?.reason], ['Exit code', crash?.exit_code], ['Signal', crash?.signal]]);
  } else if (type === 'game_bug') {
    happened = fields([['Symptom', humanize(report?.symptom)], ['Expected', report?.expected], ['Observed', report?.observed], ['Target', [report?.target_label, report?.target_kind].filter(Boolean).join(', ')]]);
  }
  sections.push(section('What happened', happened));

  let actions;
  if (capabilities) {
    const candidates = capabilities.candidate_actions || [];
    const target = capabilities.target_advertised_actions || [];
    actions = fields([['Game verbs', capabilities.game_verbs], ['Structured tools', capabilities.structured_tools], ['Target actions', target.length ? target : 'None']])
      + (candidates.length ? `<details><summary>Candidate actions (${candidates.length})</summary><ul class="issue-candidates">${candidates.slice(0, 20).map(a => `<li>${esc(a)}</li>`).join('')}</ul></details>` : '');
  } else {
    const last = type === 'crash' ? example.last_action : report?.related_action;
    actions = `<p class="hint">Not recorded for this issue type.</p>` + fields([['Last action', last?.verb], ['Target', last?.target_id], ['Result', humanize(last?.status)]]);
  }
  sections.push(section('Available actions', actions));

  const decisions = example.recent_decisions || [];
  sections.push(section('Recent decisions', decisions.length
    ? `<ol class="issue-decisions">${decisions.map(d => `<li><span>${esc(humanize(d.kind))}</span> <time data-ts="${esc(d.at)}"></time>${json('Payload', d.payload)}</li>`).join('')}</ol>`
    : `<p class="hint">No decisions recorded.</p>`));

  const shot = example.screenshot;
  sections.push(section('Screenshot', shot?.url ? `<img class="issue-shot" src="${esc(shot.url)}" alt="Screen 10 seconds before the issue" loading="lazy">`
    : `<p class="note">${icon('info')}<span>${SHOT_NOTES[shot?.note] || 'No screenshot was pinned for this hit.'}</span></p>`));

  const game = example.game_state || {};
  const stateBody = fields([['Completed quests', game.telemetry?.completed_quests]])
    + (game.progress != null ? json('Progress', game.progress) : '') + (game.observation != null ? json('Observation', game.observation) : '');
  sections.push(section('Game state', stateBody || `<p class="hint">No game state recorded.</p>`));

  if (type === 'crash') sections.push(section('Crash log', crash?.log_tail ? `<pre class="mono">${esc(crash.log_tail)}</pre>` : `<p class="hint">No log was captured.</p>`));
  if (type === 'game_bug') {
    const targetState = report?.target_state;
    sections.push(section('Bug report', fields([['Target ID', report?.target_id], ['Target state', targetState !== null && typeof targetState === 'object' ? JSON.stringify(targetState) : targetState]])
      + (report?.related_action ? json('Related action', report.related_action) : '')));
  }

  const ev = example.evidence || {};
  const paths = [['Run folder', ev.run_dir], ['Ledger', ev.ledger], ['Report', ev.report]].filter(([, p]) => p);
  sections.push(section('Evidence', (paths.length ? `<dl class="issue-fields">${paths.map(([label, p]) => `<dt>${label}</dt><dd><code>${esc(p)}</code></dd>`).join('')}</dl>` : '')
    + (runIds.has(example.run_id) ? `<p><a href="${href('i', example.run_id)}" data-key="issue-instance">Open instance</a></p>` : `<p class="hint">Instance deleted.</p>`)));
  return sections.join('');
}
