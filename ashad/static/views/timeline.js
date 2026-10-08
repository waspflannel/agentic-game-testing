// Activity timeline (spec §10): event kinds, the bounded window and card markup. Pure; app.js owns fetching and the DOM.
import { esc, icon, humanize } from '../core.js';

export const PAGE = 100;        // One page of history; "Load older" replaces the window with the previous page.
export const MAX_EVENTS = 200;  // The live window grows to this and then drops its oldest events.
export const RAW_LIMIT = 4096;

// ── Kinds ───────────────────────────────────────────────────────────────────
export const KINDS = {
  observed: { label: 'Observed', icon: 'eye' },
  plan: { label: 'Plan', icon: 'list-checks' },
  selected: { label: 'Selected', icon: 'target' },
  sent: { label: 'Sent', icon: 'send' },
  outcome: { label: 'Outcome', icon: 'circle-check' },
  tool: { label: 'Tool', icon: 'wrench' },
  model: { label: 'Model', icon: 'bot' },
  control: { label: 'Control', icon: 'sliders' },
  problem: { label: 'Problem', icon: 'alert' },
  other: { label: 'Other', icon: 'info' },
};
const EXACT = {
  structured_decision_context: 'observed', subgoal_planning_context: 'observed',
  subgoal_planning_cancelled: 'plan', subgoals_verified: 'plan', goal_changed: 'plan',
  decision_selected: 'selected', choice: 'selected',
  structured_action_intent: 'sent',
  structured_action_outcome: 'outcome', action_result: 'outcome',
  mcp_tool_result: 'tool', structured_tool_result: 'tool',
  inference_usage: 'model',
  launch_approved: 'control', run_paused: 'control', intervention: 'control', resume_failed: 'control', stopping: 'control', run_ended: 'control',
  // Lifecycle milestones read as control rows rather than "Other".
  capacity_reserved: 'control', player_launched: 'control', player_ready: 'control', world_ready: 'control', session_running: 'control', inputs_released: 'control', coordinator_restarted: 'control',
  investigation_started: 'problem', incident_flag: 'problem', agent_task_failed: 'problem', agent_task_cancelled: 'problem', decision_cancelled: 'problem',
};
const PREFIXES = [['subgoal_plan_', 'plan'], ['goal_recalculation_', 'plan'], ['provider_request_', 'model'], ['agent_turn_', 'model'], ['human_control_', 'control'], ['teardown_', 'control']];

export function eventKind(type) {
  const t = String(type || '');
  if (EXACT[t]) return EXACT[t];
  if (t.endsWith('_failed')) return 'problem';
  return PREFIXES.find(([prefix]) => t.startsWith(prefix))?.[1] || 'other';
}

export const FILTERS = [['all', 'All'], ['actions', 'Actions'], ['plan', 'Plan'], ['problems', 'Problems'], ['model', 'Model']];
const FILTER_KINDS = { actions: ['selected', 'sent', 'outcome', 'tool'], plan: ['plan'], problems: ['problem'], model: ['model'] };

// ── Labels that must never merge: selected ≠ delivered ≠ confirmed ──────────
export const SELECTED_LABEL = 'selected, not yet sent';
export function outcomeLabel(result) {
  const status = result?.status || 'unknown';
  if (status === 'completed') return { tone: 'live', text: 'Effect confirmed' };
  if (status === 'accepted' || status === 'running') return { tone: 'muted', text: 'Input delivered · effect not confirmed' };
  if (['failed', 'rejected', 'expired', 'cancelled'].includes(status)) {
    const reason = result.reason || result.error || result.message;
    return { tone: 'danger', text: `Failed: ${status === 'failed' ? '' : `${status} · `}${reason || 'no reason recorded'}` };
  }
  return { tone: 'attention', text: 'Outcome unknown' };
}

// The structured observation an event retained, or null. Shared with the inspector.
export const observationOf = event => event?.payload?.full_observation || event?.payload?.context?.observation || null;
export const observationId = event => observationOf(event)?.observation_id || event?.payload?.observation_id || event?.payload?.source_observation || null;
const count = value => (Array.isArray(value) ? value.length : null);
const short = id => { const text = String(id || ''); return text.length > 18 ? `…${text.slice(-14)}` : text; };
const tokens = n => (n >= 1000 ? `${(n / 1000).toFixed(1)}k` : String(n));
const clip = (text, n = 160) => { const s = String(text ?? ''); return s.length > n ? `${s.slice(0, n - 1)}…` : s; };
const args = value => clip(Object.entries(value || {}).filter(([k]) => k !== 'reason').map(([k, v]) => `${k}=${typeof v === 'string' ? v : JSON.stringify(v)}`).join(', '), 120);

function summary(event, kind) {
  const p = event.payload || {};
  const reasoned = text => (p.reason || p.error ? `${text}: ${p.reason || p.error}` : text);
  switch (kind) {
    case 'observed': {
      const o = observationOf(event);
      if (!o) return { text: 'Observation not retained' };
      const entities = count(o.entities);
      const inventory = count(o.state?.player?.inventory);
      return { text: [short(o.observation_id) || 'no ID', `coverage ${o.complete === false ? 'partial' : 'complete'}`,
        entities === null ? 'entities not observed' : `${entities} entities`, inventory === null ? 'inventory not observed' : `${inventory} inventory`].join(' · '), mono: true };
    }
    case 'selected': {
      const a = p.action || {};
      return { call: `${a.tool || 'action'}(${args(a.args)})`, text: SELECTED_LABEL };
    }
    case 'sent': {
      const input = p.input || {};
      return { call: `${input.verb || 'action'}(${input.target_id || ''})`, text: 'Action dispatched' };
    }
    case 'outcome': { const o = outcomeLabel(p.result); return { text: o.text, tone: o.tone }; }
    case 'tool': return { text: `${p.tool || 'tool'} · ${p.result?.status || 'returned'}` };
    case 'problem': return { text: reasoned(humanize(event.kind)), tone: event.kind.endsWith('_failed') ? 'danger' : 'attention' };
    case 'plan':
      if (event.kind === 'subgoal_plan_updated') return { text: `Plan revision ${p.plan?.revision ?? '?'} · ${count(p.plan?.goals) ?? 0} goals` };
      if (event.kind === 'subgoals_verified') return { text: `Verified ${count(p.goal_ids) ?? 0} goal${count(p.goal_ids) === 1 ? '' : 's'}` };
      if (event.kind === 'goal_changed') return { text: `Goal changed: ${clip(p.goal)}` };
      return { text: reasoned(humanize(event.kind)) };
    case 'control':
      if (event.kind === 'intervention') return { text: reasoned(`Operator: ${p.command || 'intervention'}`) };
      return { text: reasoned(humanize(event.kind)) };
    default: return { text: humanize(event.kind) || 'Unnamed event' };
  }
}

export const describeEvent = event => summary(event, eventKind(event.kind)).text;

// Adjacent model events collapse into one "Model turn" row.
function modelSummary(events) {
  let total = 0, known = false, seconds = 0;
  for (const e of events) {
    const usage = e.kind === 'inference_usage' ? e.payload?.usage : null;
    if (usage && Number.isFinite(usage.total_tokens)) { total += usage.total_tokens; known = true; }
    if (e.kind === 'inference_usage' && Number.isFinite(e.payload?.latency_seconds)) seconds += e.payload.latency_seconds;
    if (e.kind === 'agent_turn_completed' && Number.isFinite(e.payload?.duration_seconds)) seconds = Math.max(seconds, e.payload.duration_seconds);
  }
  return { text: ['Model turn', known ? `${tokens(total)} tokens` : 'tokens unknown', seconds ? `${seconds.toFixed(1)}s` : null].filter(Boolean).join(' · ') };
}

// ── Window state ────────────────────────────────────────────────────────────
// mode 'latest' follows the live edge; 'history' shows one older page until "Return to latest".
export const createTimeline = () => ({ events: [], mode: 'latest', loaded: false, filter: 'all', search: '', searchDraft: '',
  selected: null, older: 0, newer: 0, total: 0, cursor: 0, unseen: 0, token: 0, loading: false, error: '' });

// Each window-replacing request takes a token; only the newest one may apply, so a late page can't undo "Return to latest".
export function beginPage(tl) { tl.loading = true; tl.error = ''; return ++tl.token; }

export function applyPage(tl, token, page, mode, latestSeq = 0) {
  if (token !== tl.token) return false;
  const events = (page.events || []).slice(-PAGE);
  Object.assign(tl, { events, mode, loaded: true, loading: false, older: page.older_count ?? 0, newer: page.newer_count ?? 0, total: page.total ?? 0, unseen: 0,
    cursor: events.length ? events.at(-1).seq : Math.max(latestSeq, page.next ?? 0) });
  return true;
}

export function failPage(tl, token, error) { if (token === tl.token) Object.assign(tl, { loading: false, error: error.message }); }

// The run detail already carries the newest events, so an unfiltered thread opens without its own request.
export function seedFromRun(tl, run) {
  if (tl.loaded || tl.search || !Array.isArray(run?.events) || !run.events_page) return false;  // Bootstrap rows carry no event page.
  const events = run.events.slice(-PAGE);
  const page = run.events_page || {};
  return applyPage(tl, tl.token, { events, older_count: page.recent_100_older_count ?? Math.max(0, (page.older_count ?? 0) + run.events.length - events.length), newer_count: 0, total: page.total ?? events.length, next: page.next }, 'latest');
}

// A live page appends only at the live edge and only after the cursor it was asked for. 'reload' means a gap.
export function applyLive(tl, token, page, after) {
  if (token !== tl.token || tl.mode !== 'latest' || tl.cursor !== after) return 0;
  if (page.has_more) return 'reload';
  const fresh = (page.events || []).filter(e => e.seq > after);
  tl.total = page.total ?? tl.total;
  if (!fresh.length) return 0;
  const merged = [...tl.events, ...fresh];
  tl.older += Math.max(0, merged.length - MAX_EVENTS);
  tl.events = merged.slice(-MAX_EVENTS);
  tl.cursor = fresh.at(-1).seq;
  return fresh.length;
}

// Ledger rows newer than a history page (counts every row, not just search matches).
export const newerCount = (tl, runTotal) => (tl.mode === 'history' ? tl.newer + Math.max(0, (runTotal ?? tl.total) - tl.total) : 0);

// ── Cards ───────────────────────────────────────────────────────────────────
export function timelineCards(tl) {
  const allowed = FILTER_KINDS[tl.filter];
  const cards = [];
  for (const event of tl.events) {
    const kind = eventKind(event.kind);
    if (allowed && !allowed.includes(kind)) continue;
    const last = cards.at(-1);
    if (kind === 'model' && last?.kind === 'model') { last.events.push(event); continue; }
    cards.push({ key: kind === 'model' ? `m${event.seq}` : String(event.seq), kind, events: [event] });
  }
  return cards.reverse().map(card => ({ ...card, html: cardHTML(card) }));  // Newest first; model rows still group in time order.
}

// The row only; the body is built when a card is first opened, so a page of heavy events stays small.
function cardHTML(card) {
  const first = card.events[0];
  const s = card.kind === 'model' ? modelSummary(card.events) : summary(first, card.kind);
  const k = KINDS[card.kind];
  const tone = s.tone ? ` tone-${s.tone}` : '';
  const iconName = card.kind !== 'outcome' ? k.icon : { live: 'circle-check', danger: 'circle-x' }[s.tone] || 'circle';  // Only a confirmed effect gets a check.
  return `<details><summary class="tl-row${tone}" data-key="tl-${esc(card.key)}">${icon(iconName, 'icon tl-icon')}<span class="tl-kind">${k.label}</span>`
    + `<span class="tl-sum">${s.call ? `<code class="tl-call">${esc(s.call)}</code> ` : ''}<span${s.mono ? ' class="mono"' : ''}>${esc(s.text)}</span></span>`
    + `<time class="tl-time" data-ts="${Number(card.events.at(-1).timestamp) || ''}"></time></summary><div class="tl-body" data-body></div></details>`;
}

const FIELD_KEYS = ['reason', 'category', 'error', 'description', 'actual', 'command', 'tool', 'status', 'provider_status', 'action_id', 'observation_id', 'source_observation', 'revision', 'goal', 'candidate_count', 'duration_seconds', 'latency_seconds', 'model'];

// Expanded body: structured fields, correlated cards and an escaped, bounded raw block.
export function cardBody(card, index, players = {}) {
  const first = card.events[0];
  const p = first.payload || {};
  const playerId = p.player_id || p.players?.[0];
  const fields = [
    ['Time', new Date(first.timestamp * 1000).toLocaleString()],
    ['Seq', card.events.length > 1 ? `${first.seq}–${card.events.at(-1).seq}` : first.seq],
    ['Type', [...new Set(card.events.map(e => e.kind))].join(', ')],
    ['Attribution', playerId ? (players[playerId] || playerId) : 'Instance activity'],
  ];
  if (card.kind === 'outcome') fields.push(['Outcome', outcomeLabel(p.result).text]);
  for (const key of FIELD_KEYS) {
    const value = p[key] ?? p.result?.[key];
    if (value !== undefined && value !== null && typeof value !== 'object' && !fields.some(([label]) => label === humanize(key))) fields.push([humanize(key), value]);
  }
  const related = relatedOf(first, index);
  const raw = JSON.stringify(card.events.length > 1 ? card.events : first, null, 2);
  const truncated = raw.length > RAW_LIMIT;
  return `<dl class="tl-fields">${fields.map(([label, value]) => `<dt>${esc(label)}</dt><dd>${esc(value)}</dd>`).join('')}</dl>`
    + (related.length ? `<div class="tl-related"><span>Related</span>${related.map(r => r.seq
      ? `<button type="button" class="chip" data-action="tl-jump" data-seq="${r.seq}">${esc(r.label)} #${r.seq}</button>`
      : `<span class="chip mono" title="Not in the loaded window">${esc(r.label)} ${esc(short(r.id))}</span>`).join('')}</div>` : '')
    + `<div class="tl-raw"><div class="tl-raw-head"><span>Raw</span>${truncated ? `<button type="button" class="btn btn-ghost btn-sm" data-action="tl-raw-full">Show full (${Math.ceil(raw.length / 1024)}KB)</button>` : ''}</div>`
    + `<pre class="mono">${esc(truncated ? `${raw.slice(0, RAW_LIMIT)}\n…` : raw)}</pre></div>`;
}

export const rawJSON = card => JSON.stringify(card.events.length > 1 ? card.events : card.events[0], null, 2);

// Lookup tables for correlated IDs over every loaded event.
export function eventIndex(events) {
  const byId = new Map(), byAction = new Map(), byObservation = new Map();
  for (const e of events) {
    byId.set(e.id, e.seq);
    if (e.kind === 'structured_action_intent' && e.payload?.action_id) byAction.set(e.payload.action_id, e.seq);
    const obs = observationOf(e)?.observation_id;
    if (obs && !byObservation.has(obs)) byObservation.set(obs, e.seq);
  }
  return { byId, byAction, byObservation };
}

function relatedOf(event, { byId, byAction, byObservation }) {
  const p = event.payload || {};
  const out = [];
  const add = (label, id, map) => { if (id) { const seq = map.get(id); if (seq !== event.seq) out.push({ label, id, seq }); } };
  add('Intent', p.intent_event_id, byId);
  if (event.kind !== 'structured_action_intent') add('Intent', p.action_id || p.result?.action_id, byAction);
  add('Observation', p.observation_id || p.source_observation, byObservation);
  return out.filter((r, i) => out.findIndex(o => o.seq === r.seq && o.id === r.id) === i);
}

// ── Regions ─────────────────────────────────────────────────────────────────
export function timelineRegions(tl, { runTotal, cards }) {
  const tools = `<div class="tl-filters" role="group" aria-label="Filter activity">${FILTERS.map(([id, label]) => `<button type="button" class="chip" data-action="tl-filter" data-filter="${id}" data-key="tl-f-${id}" aria-pressed="${tl.filter === id}">${label}</button>`).join('')}</div>`
    + `<form class="tl-search" data-form="tl-search" role="search">${icon('search')}<input type="search" class="input" name="q" data-key="tl-search" aria-label="Search activity" placeholder="Search activity" maxlength="200" autocomplete="off"></form>`;
  const newer = newerCount(tl, runTotal);
  // Newest first: "Return to latest" sits above the list and "Load older" below it.
  const top = tl.mode === 'history' ? `<span class="tl-note">Showing older activity${newer ? ` · ${newer} newer` : ''}</span><button type="button" class="btn btn-secondary btn-sm" data-action="tl-latest" data-key="tl-latest">${icon('arrow-up')}Return to latest</button>` : '';
  const label = FILTERS.find(([id]) => id === tl.filter)[1].toLowerCase();
  const empty = !tl.loaded ? (tl.loading ? 'Loading activity…' : '')
    : cards.length ? ''
      : tl.search ? `No activity matches “${tl.search}”.`
        : !tl.events.length ? 'No activity recorded yet.' : `No ${label} activity in the loaded window.`;
  const foot = (empty ? `<p class="tl-empty">${esc(empty)}</p>` : '')
    + (tl.older > 0 ? `<button type="button" class="btn btn-ghost btn-sm" data-action="tl-older" data-key="tl-older"${tl.loading ? ' aria-disabled="true"' : ''}>Load older activity (${tl.older} earlier)</button>` : '')
    + (tl.error ? `<p class="note note-error" data-announce>${icon('alert')}<span>Activity could not load: ${esc(tl.error)}</span></p>` : '');
  const pill = tl.unseen > 0 && tl.mode === 'latest' ? `<button type="button" class="tl-pill" data-action="tl-to-latest" data-key="tl-pill">${icon('arrow-up')}${tl.unseen} new</button>` : '';
  return { 'tl-tools': tools, 'tl-top': top, 'tl-foot': foot, 'tl-pill': pill };
}

// Keyed reconcile: cards whose row is unchanged keep their element, so open bodies, focus and scroll survive polls.
export function syncCards(list, cards, doc = globalThis.document) {
  const existing = new Map([...list.children].map(el => [el.dataset.card, el]));
  let prev = null;
  for (const card of cards) {
    let el = existing.get(card.key);
    existing.delete(card.key);
    if (!el || el.cardHTML !== card.html) {
      const fresh = doc.createElement('li');
      fresh.className = `tl-card tl-${card.kind}`;
      fresh.dataset.card = card.key;
      fresh.innerHTML = card.html;
      fresh.cardHTML = card.html;
      if (el) { if (el.querySelector('details')?.open) fresh.querySelector('details').open = true; el.replaceWith(fresh); }
      el = fresh;
    }
    el.card = card;
    const want = prev ? prev.nextSibling : list.firstChild;
    if (want !== el) list.insertBefore(el, want);
    prev = el;
  }
  for (const el of existing.values()) el.remove();
}
