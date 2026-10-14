// Goals tab (spec §8.6). Pure: built only from the run's goal plan and its loaded events; it never probes the game.
import { esc, icon } from '../core.js';
import { observationOf, short, clip } from './timeline.js';

// One word, icon and tone per node status. Tones become goal-node-<tone> classes; red (danger) only where something is stuck.
const NODE_STATUS = {
  root: { label: 'Primary goal', icon: 'flag', tone: 'plain' },
  reached: { label: 'Reached, awaiting you', icon: 'circle-check', tone: 'done' },
  active: { label: 'Now', icon: 'target', tone: 'plain' },
  completed: { label: 'Completed', icon: 'circle-check', tone: 'done' },
  blocked: { label: 'Blocked', icon: 'circle-x', tone: 'danger' },
  retired: { label: 'Not needed', icon: 'ban', tone: 'muted' },
  skipped: { label: 'Skipped', icon: 'ban', tone: 'muted' },
  pending: { label: 'Pending', icon: 'circle', tone: 'muted' },
  rejected: { label: 'Rejected', icon: 'circle-x', tone: 'danger' },
  operator: { label: 'Operator', icon: 'hand', tone: 'operator' },
  earlier: { label: 'Earlier plan', icon: 'clock', tone: 'muted' },
};
const skipPrefix = goal => `Skipped blocked goal "${goal.objective}": `;
const skippedBy = (goal, goals) => {
  const reason = String(goal.blocked_reason || '');
  if (!goal.retired || !reason) return null;
  return goals.find(g => reason.startsWith(skipPrefix(g))) || null;
};
const goalStatus = (goal, rootId, goals) => (goal.id === rootId ? 'root'
  : goal.retired && skippedBy(goal, goals) ? 'skipped'
  : goal.retired ? 'retired' : NODE_STATUS[goal.status] ? goal.status : 'pending');
const REPLACED_PLAN = new Set(['subgoal_plan_recalculation_prepared', 'subgoal_plan_reset']);
const OPERATOR_COMMANDS = { takeover_finish: 'Hand back', resume: 'Resume' };

// The plan as a tree: goals in plan order, then loaded rejections, operator instructions, "Not done" answers and replaced plans by seq.
// Annotation ids (rejected-<seq>, operator-<seq>, earlier-<seq>) never collide with goal ids.
export function goalGraph(plan, events) {
  const rootId = plan.root_id || plan.goals.find(g => !g.parent_id)?.id;
  const nodes = new Map(plan.goals.map(g => {
    const skipper = skippedBy(g, plan.goals);
    const status = goalStatus(g, rootId, plan.goals);
    return [g.id, { id: g.id, parentId: g.parent_id, status, title: g.objective, reason: skipper ? g.blocked_reason.slice(skipPrefix(skipper).length) : g.blocked_reason, goal: g }];
  }));
  if (plan.status === 'goal_reached' && nodes.has(rootId)) nodes.get(rootId).status = 'reached';
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
    } else if (e.kind === 'intervention' && p.command === 'goal_reached' && p.choice === 'keep_going') {
      add({ id: `operator-${e.seq}`, parentId: rootId, status: 'operator', title: p.reason ? `Not done: ${p.reason}` : 'Not done', event: e });
    } else if (REPLACED_PLAN.has(e.kind)) {
      add({ id: `earlier-${e.seq}`, parentId: rootId, status: 'earlier', title: `Earlier plan, revision ${p.previous_plan?.revision ?? 0}`, event: e });
    }
  }
  const children = new Map();
  for (const node of nodes.values()) if (node.id !== rootId) children.set(node.parentId, [...(children.get(node.parentId) || []), node.id]);
  return { rootId, nodes, children };
}

const jumpChip = (label, seq) => `<button type="button" class="chip" data-action="tl-jump" data-seq="${seq}">${label} #${seq}</button>`;

// Cited observation quotes; shared by the Goals pane, the Goal reached popup and the False completion issue view. All text escaped.
export const evidenceListHTML = evidence => (evidence?.length
  ? `<ul class="claim-evidence">${evidence.map(e => `<li><span class="claim-source">${esc(e.source)}${e.path ? ` · ${esc(e.path)}` : ''}</span><pre class="mono claim-quote">${esc(e.quote)}</pre></li>`).join('')}</ul>`
  : '<p class="hint">No evidence was recorded.</p>');

// The claim's proof, shared by the Goal reached popup and the root's pane.
export function claimEvidenceHTML(claim, plan) {
  const goals = (claim?.verified_goal_ids || []).map(id => plan.goals.find(g => g.id === id)).filter(Boolean)
    .map(g => `<li>${icon('check')}<span>${esc(g.objective)}${g.completion?.description ? ` <span class="insp-dim">— ${esc(g.completion.description)}</span>` : ''}</span></li>`).join('');
  return `<p class="section-title">Evidence</p>${evidenceListHTML(claim?.evidence)}`
    + `<p class="section-title">Verified goals</p><ul class="claim-verified">${goals}</ul>`;
}

// Planning evidence: the card that retained the goal's source observation. Verification: the subgoals_verified card naming it.
function goalLinks(goal, events) {
  const plannedAt = goal.source_observation?.observation_id;
  const planned = plannedAt && events.find(e => observationOf(e)?.observation_id === plannedAt);
  const verified = events.find(e => e.kind === 'subgoals_verified' && e.payload?.goal_ids?.includes(goal.id));
  const link = (label, e, id) => (e ? jumpChip(label, e.seq)
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
  goal_reached: { label: 'Plan', text: p => `Planner judged the primary goal reached: ${p.claim?.reason ?? ''}` },
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
    + `${jumpChip(h.label, h.seq)}</li>`).join('');
  return `<section class="plan-history" aria-label="Plan history"><p class="section-title">Plan history</p>`
    + `${items ? `<ol>${items}</ol>` : '<p class="hint">No planning activity is loaded. Older history is in Actions.</p>'}</section>`;
}

const REASON_SHOWN = new Set(['blocked', 'retired', 'rejected', 'skipped']);

// One node button and its children; view = { currentId, selectedId, seenNodes }.
function nodeHTML(graph, id, depth, view) {
  if (depth > 8) return '';
  const node = graph.nodes.get(id);
  const s = NODE_STATUS[node.status];
  const current = id === view.currentId;
  const isNew = view.seenNodes instanceof Set && !view.seenNodes.has(id);
  const statusText = node.status === 'active' && progress(node.goal) ? `${s.label}, ${progress(node.goal)}` : s.label;
  const reasonLine = node.reason && REASON_SHOWN.has(node.status);
  const kids = graph.children.get(id) || [];
  return `<li class="goal-branch"><button type="button" class="goal-node goal-node-${s.tone}${current ? ' goal-node-current' : ''}${isNew ? ' goal-node-new' : ''}"`
    + ` data-action="goal-select" data-node="${esc(id)}" data-key="goal-node-${esc(id)}" aria-pressed="${id === view.selectedId}"${current ? ' aria-current="step"' : ''}>`
    + `${icon(s.icon, 'icon goal-icon')}<span class="goal-text"><span class="goal-title">${esc(node.title)}</span><span class="goal-status">${statusText}</span>`
    + `${reasonLine ? `<span class="goal-node-reason">${esc(node.reason)}</span>` : ''}</span>${current ? '<span class="dot dot-live"></span>' : ''}</button>`
    + `${kids.length ? `<ul>${kids.map(k => nodeHTML(graph, k, depth + 1, view)).join('')}</ul>` : ''}</li>`;
}

function goalDetailHTML(node, graph, { plan, run, events, currentId }) {
  const goal = node.goal;
  const current = node.id === currentId;
  const isRoot = node.id === graph.rootId;
  const claim = isRoot && plan.status === 'goal_reached' ? goal.completion_claim : null;
  const claimEvent = claim && events.findLast(e => e.kind === 'goal_reached');
  // The root never completes, so it is current both before the first goal and after every child completes; a blocked root waits on review, a claimed root on your answer.
  const rootNow = claim ? `<p>Planner's reason: ${esc(claim.reason)}</p>${claimEvidenceHTML(claim, plan)}${claimEvent ? `<div class="goal-links">${jumpChip('Goal reached', claimEvent.seq)}</div>` : ''}`
    : isRoot && current ? `<p>${plan.status === 'blocked' ? 'Waiting for your review.' : 'Choosing the next concrete goal under the primary goal.'}</p>` : '';
  const rejected = (graph.children.get(node.id) || []).filter(k => graph.nodes.get(k).status === 'rejected').length;
  const quotes = (goal.evidence || []).map(e => `<li><span class="insp-dim">${esc(e.source)}${e.path ? ` ${esc(e.path)}` : ''}</span> “${esc(e.quote)}”</li>`).join('');
  const reason = !node.reason ? '' : node.status === 'blocked' ? `<p class="goal-node-reason">${esc(node.reason)}</p>`
    : node.status === 'retired' || node.status === 'skipped' ? `<p class="insp-dim">${esc(node.reason)}</p>` : '';
  const links = goalLinks(goal, events);
  return rootNow
    + `${goal.completion?.description ? `<p>${esc(goal.completion.description)}</p>` : ''}`
    + `${progress(goal) ? `<p>${progress(goal)}</p>` : ''}`
    + `${goal.attempts > 0 ? `<p class="insp-dim">Actions tried since progress: ${goal.attempts}</p>` : ''}`
    + `${rejected > 0 ? `<p class="insp-dim">Rejected proposals: ${rejected}</p>` : ''}`
    + `${quotes ? `<p class="insp-dim">Planning evidence</p><ul class="insp-list">${quotes}</ul>` : claim ? '' : '<p class="insp-dim">No planning evidence recorded.</p>'}`
    + reason
    + `${links ? `<div class="goal-links">${links}</div>` : ''}`
    + `${current && !isRoot && plan.reason && !plan.reason.startsWith('Skipped blocked goal "') ? `<p class="insp-dim">Why: ${esc(plan.reason)}</p>` : ''}`
    + `${current && run.recovery_instruction ? `<p class="insp-dim">Your instruction: ${esc(run.recovery_instruction)}</p>` : ''}`;
}

function earlierPlanHTML(node) {
  const old = node.event.payload.previous_plan || {};
  const oldRoot = old.root_id || 'root';
  const goals = (old.goals || []).filter(g => g.id !== oldRoot);
  return (goals.length ? `<ul class="insp-list">${goals.map(g => `<li>${esc(g.objective)} <span class="insp-dim">${NODE_STATUS[goalStatus(g, oldRoot, old.goals || [])].label}</span></li>`).join('')}</ul>`
    : '<p class="insp-dim">No goals were recorded.</p>')
    + `<div class="goal-links">${jumpChip('Plan', node.event.seq)}</div>`;
}

// The selected node's pane: goal facts, or the rejection, operator instruction or replaced plan behind an annotation node.
function detailHTML(node, graph, ctx) {
  const body = node.goal ? goalDetailHTML(node, graph, ctx)
    : node.status === 'rejected' ? `${node.reason ? `<p class="goal-node-reason">${esc(node.reason)}</p>` : ''}<div class="goal-links">${jumpChip('Rejected', node.event.seq)}</div>`
    : node.status === 'operator' ? `${node.event.payload.reason ? `<p>${esc(node.event.payload.reason)}</p>` : ''}<div class="goal-links">${jumpChip('Operator', node.event.seq)}</div>`
    : earlierPlanHTML(node);
  return `<section class="goal-detail" aria-label="Selected goal"><p class="goal-detail-title">${esc(node.title)}</p><p class="goal-status">${NODE_STATUS[node.status].label}</p>${body}</section>`;
}

// Selection and seen ids belong to one plan lineage: a new primary goal or root, or a revision that went backwards (a rebuilt plan restarts at 0 and reuses ids like goal-1-0), starts them over.
export function resetGoalState(thread, plan) {
  const key = `${plan?.primary_goal}|${plan?.root_id}`;
  const revision = plan?.revision ?? 0;
  if (thread.goalKey !== key || revision < thread.goalRevision) Object.assign(thread, { goalSelected: null, goalSeen: null });
  Object.assign(thread, { goalKey: key, goalRevision: revision });
}

// seenNodes: the node ids of the previous render (null on a thread's first render); only ids missing from it animate in.
export function goalsHTML(run, { events, selectedGoal, seenNodes }) {
  const plan = run.subgoal_plan;
  if (!plan?.goals?.length) return '<p class="insp-empty">No goal plan was recorded for this instance.</p>';
  const graph = goalGraph(plan, events);
  const currentId = graph.nodes.get(plan.active_goal_id)?.goal ? plan.active_goal_id : graph.rootId;
  const selected = graph.nodes.get(selectedGoal) || graph.nodes.get(currentId);
  const root = graph.nodes.get(graph.rootId);
  return primaryHTML(plan, root?.goal) + reviewHTML(run, plan)
    + (root ? '<p class="section-title">Goal graph</p><div class="goal-graph-layout">'
      + `<ul class="goal-graph" aria-label="Goal graph">${nodeHTML(graph, graph.rootId, 0, { currentId, selectedId: selected.id, seenNodes })}</ul>`
      + `${detailHTML(selected, graph, { plan, run, events, currentId })}</div>` : '')
    + historyHTML(events);
}
