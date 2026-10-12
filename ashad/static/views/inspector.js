// Goals, World state and Model usage panels (spec §8.6). Pure: renders only what the run and its retained events recorded. It never probes
// the game, so there is no live-context call anywhere in this module.
import { esc, icon, humanize, badge } from '../core.js';
import { observationOf, short, clip } from './timeline.js';

export const STALE_AFTER_S = 30;  // Fresh vs Stale, while Running.
const NOT_OBSERVED = '<span class="insp-missing">Not observed</span>';
const secondsAgo = s => (s < 60 ? `${Math.floor(s)}s` : s < 3600 ? `${Math.floor(s / 60)}m` : `${Math.floor(s / 3600)}h`);

// Every loaded event once, oldest first: the run detail's newest page plus the timeline window.
export function mergeEvents(...lists) {
  const bySeq = new Map();
  for (const list of lists) for (const e of Array.isArray(list) ? list : []) bySeq.set(e.seq, e);
  return [...bySeq.values()].sort((a, b) => a.seq - b.seq);
}

// The newest event that retained a structured observation; keeps `previous` if it is newer.
export function latestObservation(events, previous = null) {
  const found = events.findLast(e => observationOf(e));
  return !found || (previous && previous.seq >= found.seq) ? previous : found;
}

// The observation retained for one decision, or null. Never falls back to the latest observation.
export function decisionObservation(event, events) {
  if (!event) return null;
  if (observationOf(event)) return event;
  const id = event.payload?.observation_id || event.payload?.source_observation;
  return (id && events.find(e => observationOf(e)?.observation_id === id)) || null;
}

// Freshness uses the controller's receipt time (event.timestamp); game clocks may be skewed.
export function freshness(event, run, now) {
  if (!event) return [{ tone: 'muted', label: 'Unknown' }];
  const age = Math.max(0, now - event.timestamp);
  const badges = [run?.state === 'Running' && age < STALE_AFTER_S ? { tone: 'live', label: 'Fresh' }
    : { tone: 'attention', label: ['Paused', 'Investigating'].includes(run?.state) || run?.human_control ? 'Stale · as of pause' : 'Stale' }];
  if (observationOf(event).complete === false) badges.push({ tone: 'attention', label: 'Partial' });
  return badges;
}

const show = value => (Array.isArray(value) ? value.map(v => (typeof v === 'object' ? JSON.stringify(v) : v)).join(', ') : typeof value === 'object' && value !== null ? JSON.stringify(value) : String(value));
const group = (title, body) => `<div class="insp-group"><h4>${esc(title)}</h4>${body}</div>`;
const list = items => (items.length ? `<ul class="insp-list">${items.map(item => `<li>${item}</li>`).join('')}</ul>` : '<p class="insp-empty">None</p>');
const field = (value, render) => (value === undefined ? NOT_OBSERVED : value === null ? '<p class="insp-empty">None</p>' : render(value));

function worldBody(observation) {
  const state = observation.state || {};
  const player = state.player;
  const playerRows = player && typeof player === 'object'
    ? Object.entries(player).filter(([k]) => !['inventory', 'hotbar'].includes(k)).map(([k, v]) => `<dt>${esc(humanize(k))}</dt><dd>${esc(show(v))}</dd>`).join('')
    : '';
  const item = i => (typeof i === 'object' && i ? `${esc(i.item_id ?? i.id ?? i.name ?? '?')}${i.count !== undefined ? ` <span class="insp-dim">× ${esc(i.count)}</span>` : ''}` : esc(i));
  const entities = Array.isArray(observation.entities) ? [...observation.entities].sort((a, b) => !a.actions?.length - !b.actions?.length) : undefined;
  const ui = state.ui ?? state.modal;
  return group('Player', player === undefined ? NOT_OBSERVED : playerRows ? `<dl class="insp-dl">${playerRows}</dl>` : '<p class="insp-empty">No player fields</p>')
    + group('Inventory', field(player?.inventory, inv => list(inv.map(item))))
    + group('Hotbar', field(player?.hotbar, bar => list(bar.map(h => (typeof h === 'object' && h?.slot !== undefined ? `<span class="insp-dim">${esc(h.slot)}</span> ${item(h)}` : item(h))))))
    + group(`Nearby entities${entities ? ` (${entities.length})` : ''}`, field(entities, all => list(all.slice(0, 12).map(e => `${esc(e.label || e.id)} <span class="insp-dim">${esc(e.kind || '')}</span>`
      + (e.actions?.length ? ` ${e.actions.map(a => `<code class="insp-action">${esc(a)}</code>`).join(' ')}` : ''))) + (all.length > 12 ? `<p class="insp-dim">+${all.length - 12} more</p>` : '')))
    + group('Objectives', field(state.objectives, objs => list(objs.map(o => esc(typeof o === 'object' && o ? o.title || o.name || o.id : o)))))
    + group('Completed quests', field(state.completed_quests, done => list(done.map(q => esc(typeof q === 'object' && q ? q.title || q.id : q)))))
    + group('UI and action state', `<dl class="insp-dl"><dt>Active action</dt><dd>${field(observation.active_action, a => esc(typeof a === 'object' ? `${a.verb || 'action'}(${a.target_id || ''})` : a))}</dd>`
      + `<dt>Modal</dt><dd>${field(ui, value => esc(show(value)))}</dd></dl>`);
}

function worldHTML(run, ctx) {
  const { latest, selectedEvent, events, view, now } = ctx;
  const atDecision = selectedEvent && view === 'decision';
  const toggle = selectedEvent
    ? `<div class="seg" role="group" aria-label="World state as of"><button type="button" class="seg-btn" data-action="insp-view" data-view="latest" data-key="insp-latest" aria-pressed="${!atDecision}">Latest</button>`
      + `<button type="button" class="seg-btn" data-action="insp-view" data-view="decision" data-key="insp-decision" aria-pressed="${Boolean(atDecision)}">At decision #${selectedEvent.seq}</button></div>`
    : '';
  const shown = atDecision ? decisionObservation(selectedEvent, events) : latest;
  if (!shown) {
    const why = atDecision ? 'Not retained for this decision. ASHAD shows only the observation recorded with it, never a later one.' : 'No structured observation has been retained for this instance yet.';
    return `${toggle}<div class="insp-meta">${badge({ tone: 'muted', label: 'Unknown' })}</div><p class="insp-empty">${esc(why)}</p>`;
  }
  const observation = observationOf(shown);
  const age = Math.max(0, now - shown.timestamp);
  const badges = atDecision ? [{ tone: 'muted', label: `Decision #${selectedEvent.seq}` }, ...freshness(shown, run, now).filter(b => b.label === 'Partial')] : freshness(shown, run, now);
  return `${toggle}<div class="insp-meta"><code title="${esc(observation.observation_id || '')}">${esc(short(observation.observation_id) || 'no ID')}</code>`
    + `<span>observed ${secondsAgo(age)} ago</span>${badges.map(badge).join('')}</div>${worldBody(observation)}`;
}

// Cached input and reasoning output are subsets of input and output; the total never adds them again.
export function usageTotals(rows) {
  const t = { turns: 0, counted: 0, input: 0, cached: 0, output: 0, reasoning: 0, cost: null };
  for (const row of Array.isArray(rows) ? rows : []) {
    t.turns += 1;
    const u = row?.usage;
    if (Number.isFinite(row?.estimated_cost_usd)) t.cost = (t.cost ?? 0) + row.estimated_cost_usd;
    if (!u || !Number.isFinite(u.input_tokens) || !Number.isFinite(u.output_tokens)) continue;
    t.counted += 1;
    t.input += u.input_tokens;
    t.output += u.output_tokens;
    t.cached += Number.isFinite(u.cached_input_tokens) ? u.cached_input_tokens : 0;
    t.reasoning += Number.isFinite(u.reasoning_output_tokens) ? u.reasoning_output_tokens : 0;
  }
  t.total = t.input + t.output;
  return t;
}

function tokensHTML(run) {
  if (!Array.isArray(run.usage)) return '<p class="insp-empty">Loading usage…</p>';
  const t = usageTotals(run.usage);
  const n = value => (t.counted ? value.toLocaleString() : '<span title="unknown">— unknown</span>');
  const row = (label, value, sub = false) => `<dt${sub ? ' class="insp-sub"' : ''}>${label}</dt><dd>${value}</dd>`;
  return `<dl class="insp-dl insp-tokens">${row('Total tokens', n(t.total))}${row('Input', n(t.input))}${row('of which cached', n(t.cached), true)}`
    + `${row('Output', n(t.output))}${row('of which reasoning', n(t.reasoning), true)}${row('Turns', t.turns.toLocaleString())}`
    + `${row('API-equivalent estimate', t.cost === null ? '<span title="unknown">— not available</span>' : `$${t.cost.toFixed(2)}`)}</dl>`
    + (t.turns > t.counted ? `<p class="insp-dim">${t.turns - t.counted} of ${t.turns} turns recorded no token counts; totals cover the rest.</p>` : '')
    + '<p class="insp-note">Estimate, not a charge. Billed through your AI subscription.</p>';
}

const GOAL_STATUS = {
  completed: { label: 'Completed', icon: 'circle-check', tone: 'live' },
  active: { label: 'Now', icon: 'target', tone: 'accent' },
  blocked: { label: 'Blocked', icon: 'alert', tone: 'attention' },
  retired: { label: 'Not needed', icon: 'ban', tone: 'muted' },
  pending: { label: 'Pending', icon: 'circle', tone: 'muted' },
  root: { label: 'Primary goal', icon: 'flag', tone: 'muted' },
};
const goalStatus = (goal, rootId) => GOAL_STATUS[goal.id === rootId ? 'root' : goal.retired ? 'retired' : goal.status] || GOAL_STATUS.pending;

// Planning evidence: the card that retained the goal's source observation. Verification: the subgoals_verified card naming it.
function goalLinks(goal, events) {
  const plannedAt = goal.source_observation?.observation_id;
  const planned = plannedAt && events.find(e => observationOf(e)?.observation_id === plannedAt);
  const verified = events.find(e => e.kind === 'subgoals_verified' && e.payload?.goal_ids?.includes(goal.id));
  const link = (label, e, id) => (e ? `<button type="button" class="chip" data-action="tl-jump" data-seq="${e.seq}">${label} #${e.seq}</button>`
    : id ? `<span class="chip mono" title="Not in the loaded activity">${label} ${esc(short(id))}</span>` : '');
  return [link('Planned from', planned, plannedAt), link('Verified', verified, goal.completion_evidence?.observation_id)].filter(Boolean).join('');
}

const MEASURED = new Set(['inventory_quantity', 'observed_count']);
// "8 of 30 so far" only when the controller measured a countable condition.
const progress = goal => (Number.isFinite(goal?.measured) && MEASURED.has(goal.completion?.kind)
  ? `${goal.measured} of ${goal.completion.quantity} so far` : '');

const plural = (n, word) => `${n} ${word}${n === 1 ? '' : 's'}`;
const HISTORY_KINDS = {
  subgoal_planning_context: { label: 'Observed', text: p => `Planning for “${p.context?.current_goal?.objective ?? 'the primary goal'}”` },
  subgoal_plan_updated: { label: 'Plan', text: p => {
    const added = (p.plan?.goals || []).filter(g => String(g.id).startsWith(`goal-${p.plan.revision}-`)).length;
    return added ? `Revision ${p.plan.revision} added ${plural(added, 'goal')}: ${p.plan.reason ?? ''}` : `Revision ${p.plan?.revision}: ${p.plan?.reason ?? ''}`;
  } },
  subgoal_plan_rejected: { label: 'Rejected', text: p => `Proposal rejected (attempt ${p.attempt ?? '?'}): ${p.reason ?? ''}` },
  subgoal_planning_cancelled: { label: 'Cancelled', text: p => `Planning cancelled: ${p.reason ?? ''}` },
  subgoal_plan_recalculation_prepared: { label: 'Plan', text: p => `Goals recalculated: ${p.plan?.reason ?? ''}` },
  subgoal_plan_reset: { label: 'Plan', text: p => `Plan reset: ${p.reason ?? ''}` },
  goal_changed: { label: 'Goal', text: p => `Goal changed to “${p.goal ?? ''}”` },
  goal_recalculation_failed: { label: 'Failed', text: p => `Recalculation failed: ${p.reason ?? ''}` },
  subgoals_verified: { label: 'Verified', text: p => `Verified ${plural(p.goal_ids?.length ?? 0, 'goal')}` },
};
const HISTORY_ROWS = 20;

// Newest planning events first, each as one clipped sentence with the label of its Activity chip.
export function planHistory(events) {
  return events.filter(e => HISTORY_KINDS[e.kind])
    .map(e => ({ seq: e.seq, timestamp: e.timestamp, text: clip(HISTORY_KINDS[e.kind].text(e.payload || {}), 200), label: HISTORY_KINDS[e.kind].label }))
    .sort((a, b) => b.seq - a.seq).slice(0, HISTORY_ROWS);
}

const primaryHTML = (plan, root) => `<p class="section-title">Primary goal</p><p class="goal-primary">${esc(plan.primary_goal ?? root?.objective)}</p>`
  + `<p class="insp-dim">Judged by you, not by ASHAD. Plan revision ${plan.revision ?? 0}.</p>`;

function workingNowHTML(plan, nodes, rootId) {
  const active = nodes.get(plan.active_goal_id);
  const open = '<section class="goal-now" aria-label="Working on now"><p class="goal-now-title">Working on now</p>';
  if (!active || active.id === rootId) return `${open}<p class="goal-now-objective">Choosing the first concrete goal under the primary goal.</p></section>`;
  const ancestors = [];
  for (let g = nodes.get(active.parent_id); g && ancestors.length < 8; g = nodes.get(g.parent_id)) ancestors.unshift(g.objective);
  return `${open}<p class="goal-now-objective">${esc(active.objective)}</p>`
    + `${active.completion?.description ? `<p>${esc(active.completion.description)}</p>` : ''}`
    + `${progress(active) ? `<p>${progress(active)}</p>` : ''}`
    + `${plan.reason ? `<p class="insp-dim">Why: ${esc(plan.reason)}</p>` : ''}`
    + `${ancestors.length ? `<p class="goal-path">${ancestors.map(esc).join(' › ')}</p>` : ''}</section>`;
}

// A review note only for pauses that wait on the operator; "Recalculating goals…" is progress, and recovery text stays in the thread banner.
function reviewHTML(run, plan) {
  const recalculating = run.goal_recalculation?.status === 'planning';
  const pause = run.pause;
  const reason = ['planner_rejected', 'planner_blocked'].includes(pause?.category) || (pause?.category === 'goal_recalculation' && !recalculating) ? pause.reason
    : plan.status === 'blocked' ? plan.reason : '';
  return reason ? `<p class="note goal-review">${icon('alert')}<span>Needs review: ${esc(reason)}</span></p>` : '';
}

function historyHTML(events) {
  const items = planHistory(events).map(h => `<li><time data-ts="${h.timestamp}"></time><span>${esc(h.text)}</span>`
    + `<button type="button" class="chip" data-action="tl-jump" data-seq="${h.seq}">${h.label} #${h.seq}</button></li>`).join('');
  return `<section class="plan-history" aria-label="Plan history"><p class="section-title">Plan history</p>`
    + `${items ? `<ol>${items}</ol>` : '<p class="hint">No planning activity is loaded. Older history is in Actions.</p>'}</section>`;
}

function goalHTML(run, { events, goalOpen }) {
  const plan = run.subgoal_plan;
  if (!plan?.goals?.length) return '<p class="insp-empty">No goal plan was recorded for this instance.</p>';
  const nodes = new Map(plan.goals.map(g => [g.id, g]));
  const rootId = plan.root_id || plan.goals.find(g => !g.parent_id)?.id;
  const children = id => plan.goals.filter(g => g.parent_id === id);
  const row = (goal, depth) => {
    if (depth > 8) return '';
    const s = goalStatus(goal, rootId);
    const quotes = (goal.evidence || []).map(e => `<li><span class="insp-dim">${esc(e.source)}${e.path ? ` ${esc(e.path)}` : ''}</span> “${esc(e.quote)}”</li>`).join('');
    const links = goalLinks(goal, events);
    return `<li class="goal goal-${s.tone}"><details${goalOpen.has(goal.id) ? ' open' : ''} data-goal="${esc(goal.id)}"><summary class="goal-row" data-key="goal-${esc(goal.id)}">`
      + `${icon(s.icon, 'icon goal-icon')}<span class="goal-text"><span class="goal-title">${esc(goal.objective)}</span><span class="goal-status">${s.label}${goal.status === 'active' && progress(goal) ? `, ${progress(goal)}` : ''}</span>`
      + `${goal.completion?.description ? `<span class="goal-cond">${esc(goal.completion.description)}</span>` : ''}`
      + `${goal.status === 'blocked' && goal.blocked_reason ? `<span class="${goal.retired ? 'goal-cond' : 'goal-reason'}">${esc(goal.blocked_reason)}</span>` : ''}</span></summary>`
      + `<div class="goal-body">${goal.attempts > 0 ? `<p class="insp-dim">Actions tried since progress: ${goal.attempts}</p>` : ''}${quotes ? `<p class="insp-dim">Planning evidence</p><ul class="insp-list">${quotes}</ul>` : '<p class="insp-dim">No planning evidence recorded.</p>'}`
      + `${links ? `<div class="goal-links">${links}</div>` : ''}</div></details>`
      + `${children(goal.id).length ? `<ul class="goal-tree">${children(goal.id).map(c => row(c, depth + 1)).join('')}</ul>` : ''}</li>`;
  };
  const root = nodes.get(rootId);
  return primaryHTML(plan, root) + workingNowHTML(plan, nodes, rootId) + reviewHTML(run, plan)
    + (root ? `<p class="section-title">Goal tree</p><ul class="goal-tree goal-root">${row(root, 0)}</ul>` : '') + historyHTML(events);
}

export function panelRegions(run, ctx) {
  return { goals: goalHTML(run, ctx), world: worldHTML(run, ctx), usage: tokensHTML(run) };
}
