// Goals tab (spec §8.6). Pure: built only from the run's goal plan and its loaded events; it never probes the game.
import { esc, icon } from '../core.js';
import { observationOf, short, clip } from './timeline.js';

// One word, icon and tone per node status. Tones become goal-node-<tone> classes; red (danger) only where something is stuck.
const NODE_STATUS = {
  root: { label: 'Primary goal', icon: 'flag', tone: 'plain' },
  active: { label: 'Now', icon: 'target', tone: 'plain' },
  completed: { label: 'Completed', icon: 'circle-check', tone: 'done' },
  blocked: { label: 'Blocked', icon: 'circle-x', tone: 'danger' },
  retired: { label: 'Not needed', icon: 'ban', tone: 'muted' },
  pending: { label: 'Pending', icon: 'circle', tone: 'muted' },
  rejected: { label: 'Rejected', icon: 'circle-x', tone: 'danger' },
  operator: { label: 'Operator', icon: 'hand', tone: 'operator' },
  earlier: { label: 'Earlier plan', icon: 'clock', tone: 'muted' },
};
const goalStatus = (goal, rootId) => (goal.id === rootId ? 'root' : goal.retired ? 'retired' : NODE_STATUS[goal.status] ? goal.status : 'pending');
const REPLACED_PLAN = new Set(['subgoal_plan_recalculation_prepared', 'subgoal_plan_reset']);
const OPERATOR_COMMANDS = { takeover_finish: 'Hand back', resume: 'Resume' };

// The plan as a tree: goals in plan order, then loaded rejections, operator instructions and replaced plans by seq.
// Annotation ids (rejected-<seq>, operator-<seq>, earlier-<seq>) never collide with goal ids.
export function goalGraph(plan, events) {
  const rootId = plan.root_id || plan.goals.find(g => !g.parent_id)?.id;
  const nodes = new Map(plan.goals.map(g => [g.id, { id: g.id, parentId: g.parent_id, status: goalStatus(g, rootId), title: g.objective, reason: g.blocked_reason, goal: g }]));
  const goalIds = new Set(nodes.keys());
  // A replaced plan's goal ids can repeat in the new plan (revisions restart at 0), so older annotations go under the root.
  const replacedAt = Math.max(0, ...events.filter(e => REPLACED_PLAN.has(e.kind)).map(e => e.seq));
  const anchor = (goalId, e) => (e.seq > replacedAt && goalIds.has(goalId) ? goalId : rootId);
  const add = node => nodes.set(node.id, node);
  for (const e of events) {
    const p = e.payload || {};
    if (e.kind === 'subgoal_plan_rejected') {
      add({ id: `rejected-${e.seq}`, parentId: anchor(p.goal_id, e), status: 'rejected', title: `Proposal rejected (attempt ${p.attempt ?? '?'})`, reason: p.reason, event: e });
    } else if (e.kind === 'intervention' && OPERATOR_COMMANDS[p.command] && p.reason) {
      const used = events.find(c => c.seq > e.seq && c.kind === 'subgoal_planning_context' && c.payload?.context?.operator_instruction?.text === p.reason);
      add({ id: `operator-${e.seq}`, parentId: anchor(used?.payload.context.current_goal?.id, e), status: 'operator', title: `${OPERATOR_COMMANDS[p.command]}: ${p.reason}`, event: e });
    } else if (REPLACED_PLAN.has(e.kind)) {
      add({ id: `earlier-${e.seq}`, parentId: rootId, status: 'earlier', title: `Earlier plan, revision ${p.previous_plan?.revision ?? 0}`, event: e });
    }
  }
  const children = new Map();
  for (const node of nodes.values()) if (node.id !== rootId) children.set(node.parentId, [...(children.get(node.parentId) || []), node.id]);
  return { rootId, nodes, children };
}

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
  subgoal_planning_context: { label: 'Observed', text: p => `Planning for “${p.context?.current_goal?.objective ?? 'the primary goal'}”${p.context?.operator_instruction?.text ? ' with your instruction' : ''}` },
  intervention: { label: 'Operator', when: p => ['resume', 'takeover_finish'].includes(p.command) && Boolean(p.reason),
    text: p => `${p.command === 'takeover_finish' ? 'Hand back' : 'Resume'} with instruction: ${p.reason}` },
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
  return events.filter(e => HISTORY_KINDS[e.kind] && (HISTORY_KINDS[e.kind].when?.(e.payload || {}) ?? true))
    .map(e => ({ seq: e.seq, timestamp: e.timestamp, text: clip(HISTORY_KINDS[e.kind].text(e.payload || {}), 200), label: HISTORY_KINDS[e.kind].label }))
    .sort((a, b) => b.seq - a.seq).slice(0, HISTORY_ROWS);
}

const primaryHTML = (plan, root) => `<p class="section-title">Primary goal</p><p class="goal-primary">${esc(plan.primary_goal ?? root?.objective)}</p>`
  + `<p class="insp-dim">Judged by you, not by ASHAD. Plan revision ${plan.revision ?? 0}.</p>`;

function workingNowHTML(plan, nodes, rootId, run) {
  const instruction = run.recovery_instruction ? `<p class="insp-dim">Your instruction: ${esc(run.recovery_instruction)}</p>` : '';
  const active = nodes.get(plan.active_goal_id);
  const open = '<section class="goal-now" aria-label="Working on now"><p class="goal-now-title">Working on now</p>';
  // The root never completes, so it is active both before the first goal and after every child completes; a blocked root waits on review.
  if (!active || active.id === rootId) {
    return `${open}<p class="goal-now-objective">${plan.status === 'blocked' ? 'Waiting for your review.' : 'Choosing the next concrete goal under the primary goal.'}</p>${instruction}</section>`;
  }
  const ancestors = [];
  for (let g = nodes.get(active.parent_id); g && ancestors.length < 8; g = nodes.get(g.parent_id)) ancestors.unshift(g.objective);
  return `${open}<p class="goal-now-objective">${esc(active.objective)}</p>`
    + `${active.completion?.description ? `<p>${esc(active.completion.description)}</p>` : ''}`
    + `${progress(active) ? `<p>${progress(active)}</p>` : ''}`
    + `${plan.reason ? `<p class="insp-dim">Why: ${esc(plan.reason)}</p>` : ''}${instruction}`
    + `${ancestors.length ? `<p class="goal-path">${ancestors.map(esc).join(' › ')}</p>` : ''}</section>`;
}

// A review note only for pauses that wait on the operator; "Recalculating goals…" is progress, and recovery text stays in the thread banner.
function reviewHTML(run, plan) {
  const recalculating = run.goal_recalculation?.status === 'planning';
  const pause = run.pause;
  const reason = ['planner_rejected', 'planner_blocked'].includes(pause?.category) || (pause?.category === 'goal_recalculation' && !recalculating) ? pause.reason
    : plan.status === 'blocked' ? plan.reason : '';
  return reason ? `<p class="note goal-review">Needs review: ${esc(reason)}</p>` : '';
}

function historyHTML(events) {
  const items = planHistory(events).map(h => `<li><time data-ts="${h.timestamp}"></time><span>${esc(h.text)}</span>`
    + `<button type="button" class="chip" data-action="tl-jump" data-seq="${h.seq}">${h.label} #${h.seq}</button></li>`).join('');
  return `<section class="plan-history" aria-label="Plan history"><p class="section-title">Plan history</p>`
    + `${items ? `<ol>${items}</ol>` : '<p class="hint">No planning activity is loaded. Older history is in Actions.</p>'}</section>`;
}

export function goalsHTML(run, { events, goalOpen }) {
  const plan = run.subgoal_plan;
  if (!plan?.goals?.length) return '<p class="insp-empty">No goal plan was recorded for this instance.</p>';
  const nodes = new Map(plan.goals.map(g => [g.id, g]));
  const rootId = plan.root_id || plan.goals.find(g => !g.parent_id)?.id;
  const children = id => plan.goals.filter(g => g.parent_id === id);
  const row = (goal, depth) => {
    if (depth > 8) return '';
    const s = NODE_STATUS[goalStatus(goal, rootId)];
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
  return primaryHTML(plan, root) + workingNowHTML(plan, nodes, rootId, run) + reviewHTML(run, plan)
    + (root ? `<p class="section-title">Goal tree</p><ul class="goal-tree goal-root">${row(root, 0)}</ul>` : '') + historyHTML(events);
}
