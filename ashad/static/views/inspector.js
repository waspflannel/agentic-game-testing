// Right inspector (spec §8.6). Pure: renders only what the run and its retained events recorded. It never probes
// the game, so there is no live-context call anywhere in this module.
import { esc, icon, humanize } from '../core.js';
import { observationOf, eventKind, describeEvent } from './timeline.js';

export const STALE_AFTER_S = 30;  // Fresh vs Stale, while Running.
const NOT_OBSERVED = '<span class="insp-missing">Not observed</span>';
const short = id => { const text = String(id || ''); return text.length > 18 ? `…${text.slice(-14)}` : text; };
const secondsAgo = s => (s < 60 ? `${Math.floor(s)}s` : s < 3600 ? `${Math.floor(s / 60)}m` : `${Math.floor(s / 3600)}h`);

export const SECTIONS = [['world', 'Observed world state'], ['recent', 'Recent activity'], ['tokens', 'Tokens and cost'], ['goal', 'Goal']];
// Mounted once; the <details> elements are never re-created, so their open state survives polls and route changes.
export const inspectorSkeleton = () => SECTIONS.map(([id, title]) => `<details class="insp-section" open data-section="${id}">`
  + `<summary class="insp-head">${icon('chevron-right', 'icon chevron')}${title}</summary><div class="insp-body" data-region="insp-${id}"></div></details>`).join('');

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

const badge = b => `<span class="badge badge-${b.tone}"><span class="dot dot-${b.tone}" aria-hidden="true"></span>${esc(b.label)}</span>`;
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

// The last 8 lifecycle, control and problem events; each row jumps to its timeline card.
function recentHTML(events) {
  const rows = events.filter(e => ['control', 'problem'].includes(eventKind(e.kind))).slice(-8).reverse();
  if (!rows.length) return '<p class="insp-empty">No control or lifecycle events in the loaded activity.</p>';
  return `<ul class="insp-recent">${rows.map(e => `<li><button type="button" class="insp-row" data-action="tl-jump" data-seq="${e.seq}" data-key="insp-r-${e.seq}">`
    + `${icon(eventKind(e.kind) === 'problem' ? 'alert' : 'sliders', `icon${eventKind(e.kind) === 'problem' ? ' insp-warn' : ''}`)}<span class="insp-row-text">${esc(describeEvent(e))}</span><span class="insp-dim">#${e.seq}</span></button></li>`).join('')}</ul>`;
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
    + '<p class="insp-note">Estimate, not a charge. Billed through your ChatGPT subscription.</p>';
}

const GOAL_STATUS = {
  completed: { label: 'Completed', icon: 'circle-check', tone: 'live' },
  active: { label: 'Active', icon: 'target', tone: 'accent' },
  blocked: { label: 'Blocked', icon: 'alert', tone: 'attention' },
  retired: { label: 'Not needed', icon: 'ban', tone: 'muted' },
  pending: { label: 'Pending', icon: 'circle', tone: 'muted' },
  root: { label: 'Needs your acceptance', icon: 'flag', tone: 'attention' },
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

function goalHTML(run, { events, goalOpen }) {
  const plan = run.subgoal_plan;
  if (!plan?.goals?.length) return '<p class="insp-empty">No goal plan was recorded for this instance.</p>';
  const nodes = new Map(plan.goals.map(g => [g.id, g]));
  const rootId = plan.root_id || plan.goals.find(g => !g.parent_id)?.id;
  const path = [];
  for (let g = nodes.get(plan.active_goal_id); g && path.length <= 8; g = nodes.get(g.parent_id)) path.unshift(g);
  const chasing = path.length ? `<nav class="insp-crumbs" aria-label="Currently chasing">${path.map(g => `<span>${esc(g.objective)}</span>`).join(icon('chevron-right', 'icon crumb-sep'))}</nav>` : '';
  const children = id => plan.goals.filter(g => g.parent_id === id);
  const row = (goal, depth) => {
    if (depth > 8) return '';
    const s = goalStatus(goal, rootId);
    const quotes = (goal.evidence || []).map(e => `<li><span class="insp-dim">${esc(e.source)}${e.path ? ` ${esc(e.path)}` : ''}</span> “${esc(e.quote)}”</li>`).join('');
    const links = goalLinks(goal, events);
    return `<li class="goal goal-${s.tone}"><details${goalOpen.has(goal.id) ? ' open' : ''} data-goal="${esc(goal.id)}"><summary class="goal-row" data-key="goal-${esc(goal.id)}">`
      + `${icon(s.icon, 'icon goal-icon')}<span class="goal-text"><span class="goal-title">${esc(goal.objective)}</span><span class="goal-status">${s.label}</span>`
      + `${goal.completion?.description ? `<span class="goal-cond">${esc(goal.completion.description)}</span>` : ''}`
      + `${goal.status === 'blocked' && goal.blocked_reason ? `<span class="${goal.retired ? 'goal-cond' : 'goal-reason'}">${esc(goal.blocked_reason)}</span>` : ''}</span></summary>`
      + `<div class="goal-body">${quotes ? `<p class="insp-dim">Planning evidence</p><ul class="insp-list">${quotes}</ul>` : '<p class="insp-dim">No planning evidence recorded.</p>'}`
      + `${links ? `<div class="goal-links">${links}</div>` : ''}</div></details>`
      + `${children(goal.id).length ? `<ul class="goal-tree">${children(goal.id).map(c => row(c, depth + 1)).join('')}</ul>` : ''}</li>`;
  };
  const root = nodes.get(rootId);
  return chasing + (root ? `<ul class="goal-tree goal-root">${row(root, 0)}</ul>` : '');
}

export function inspectorRegions(run, ctx) {
  return { 'insp-world': worldHTML(run, ctx), 'insp-recent': recentHTML(ctx.events), 'insp-tokens': tokensHTML(run), 'insp-goal': goalHTML(run, ctx) };
}
