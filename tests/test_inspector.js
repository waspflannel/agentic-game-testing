'use strict';
// Goals, World state and Model usage panels: retained observations only, freshness badges, decision-time state, tokens and goals (ashad/static/views/inspector.js).
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { pathToFileURL } = require('node:url');
const load = () => import(pathToFileURL(path.join(__dirname, '../ashad/static/views/inspector.js')).href);
const loadGoals = () => import(pathToFileURL(path.join(__dirname, '../ashad/static/views/goals.js')).href);

const NOW = 10000;
const obsEvent = (seq, id, extra = {}, ts = NOW - 5) => ({ id: `evt_${seq}`, seq, kind: 'structured_decision_context', timestamp: ts,
  payload: { player_id: 'p1', full_observation: { observation_id: id, state: { player: { inventory: [{ item_id: 'wood', count: 2 }] } }, entities: [], ...extra } } });
const run = (state, extra = {}) => ({ id: 'r1', state, usage: [], ...extra });
const ctx = (events, extra = {}) => ({ latest: events.findLast(e => e.payload?.full_observation) || null, selectedEvent: null, events, view: 'latest', now: NOW, selectedGoal: null, seenNodes: null, ...extra });

test('the inspector never requests live game context', () => {
  for (const file of ['ashad/static/views/inspector.js', 'ashad/static/views/goals.js', 'ashad/static/app.js']) {
    assert.doesNotMatch(fs.readFileSync(path.join(__dirname, '..', file), 'utf8'), /\/context/, `${file} must not call …/context`);
  }
});

test('fresh, stale, partial and unknown render from fixtures', async () => {
  const { freshness, panelRegions, STALE_AFTER_S } = await load();
  const labels = (e, r, now = NOW) => freshness(e, r, now).map(b => b.label);
  assert.deepEqual(labels(null, run('Running')), ['Unknown']);
  assert.deepEqual(labels(obsEvent(1, 'o1'), run('Running')), ['Fresh']);
  assert.deepEqual(labels(obsEvent(1, 'o1', {}, NOW - STALE_AFTER_S - 1), run('Running')), ['Stale']);
  assert.deepEqual(labels(obsEvent(1, 'o1'), run('Paused')), ['Stale · as of pause']);
  assert.deepEqual(labels(obsEvent(1, 'o1', { complete: false }), run('Running')), ['Fresh', 'Partial']);
  const unknown = panelRegions(run('Running'), ctx([])).world;
  assert.match(unknown, /Unknown/);
  assert.match(unknown, /No structured observation has been retained/);
  const world = panelRegions(run('Running'), ctx([obsEvent(1, 'o1', { complete: false })])).world;
  assert.match(world, /observed 5s ago/);
  assert.match(world, /Partial/);
  assert.match(world, /Hotbar<\/h4><span class="insp-missing">Not observed/, 'a missing field says so instead of rendering empty');
  assert.match(world, /Objectives<\/h4><span class="insp-missing">Not observed/);
});

test('a decision without a retained observation says so instead of showing the latest', async () => {
  const { decisionObservation, panelRegions } = await load();
  const events = [obsEvent(1, 'obs_old'), { id: 'evt_2', seq: 2, kind: 'action_result', timestamp: NOW, payload: { result: { status: 'accepted' } } }, obsEvent(3, 'obs_latest')];
  const selected = { id: 'evt_4', seq: 4, kind: 'decision_selected', timestamp: NOW, payload: { observation_id: 'obs_missing', action: { tool: 'interact' } } };
  assert.equal(decisionObservation(events[1], events), null);
  assert.equal(decisionObservation(selected, events), null);
  const html = panelRegions(run('Running'), ctx(events, { selectedEvent: selected, view: 'decision' })).world;
  assert.match(html, /Not retained for this decision/);
  assert.doesNotMatch(html, /obs_latest/);
  assert.match(html, /At decision #4/);
  const found = { ...selected, payload: { observation_id: 'obs_old' } };
  assert.equal(decisionObservation(found, events).seq, 1, 'a retained observation is matched by ID');
  assert.match(panelRegions(run('Running'), ctx(events, { selectedEvent: found, view: 'decision' })).world, /obs_old/);
});

test('cached and reasoning tokens are subsets, never double-counted', async () => {
  const { usageTotals, panelRegions } = await load();
  const rows = [
    { usage: { input_tokens: 1000, cached_input_tokens: 600, output_tokens: 200, reasoning_output_tokens: 150, total_tokens: 1200 }, estimated_cost_usd: null },
    { usage: { input_tokens: 500, cached_input_tokens: 0, output_tokens: 100, reasoning_output_tokens: 40 }, estimated_cost_usd: null },
    { usage: null, estimated_cost_usd: null },
  ];
  const t = usageTotals(rows);
  assert.deepEqual([t.total, t.input, t.cached, t.output, t.reasoning, t.turns, t.counted, t.cost], [1800, 1500, 600, 300, 190, 3, 2, null]);
  const html = panelRegions(run('Running', { usage: rows }), ctx([])).usage;
  assert.match(html, /1,800/);
  assert.match(html, /1 of 3 turns recorded no token counts/);
  assert.match(html, /Estimate, not a charge\. Billed through your AI subscription\./);
  assert.match(html, /— not available/, 'no invented dollar figure for subscription usage');
  assert.match(panelRegions(run('Running', { usage: [{ usage: null }] }), ctx([])).usage, /— unknown/);
});

const applePlan = (over = {}) => ({ root_id: 'root', active_goal_id: 'g2', revision: 4, reason: 'Follow the recipe', goals: [
  { id: 'root', parent_id: null, objective: 'Make 3 apple pies', status: 'active' },
  { id: 'g1', parent_id: 'root', objective: 'Obtain 3 flour', status: 'pending' },
  { id: 'g2', parent_id: 'g1', objective: 'Obtain 30 wheat', status: 'active', measured: 8, attempts: 3,
    completion: { kind: 'inventory_quantity', quantity: 30, description: 'Have 30 wheat' } },
], ...over });

const farmPlan = (over = {}) => ({ root_id: 'root', active_goal_id: 'g2', revision: 7, status: 'active', reason: 'Follow the recipe', primary_goal: 'Reach the farm', goals: [
  { id: 'root', parent_id: null, objective: 'Reach the farm', status: 'active' },
  { id: 'g1', parent_id: 'root', objective: 'Find seeds', status: 'completed', attempts: 2, evidence: [{ source: 'observation', path: 'state.player', quote: 'seed bag' }],
    source_observation: { observation_id: 'o1' }, completion_evidence: { observation_id: 'o9' } },
  { id: 'g2', parent_id: 'root', objective: 'Get 3 wheat', status: 'active', measured: 1, completion: { kind: 'inventory_quantity', quantity: 3, description: 'Have 3 wheat' } },
  { id: 'g3', parent_id: 'g2', objective: 'Till soil', status: 'pending' },
  { id: 'g4', parent_id: 'root', objective: 'Cross bridge', status: 'blocked', blocked_reason: 'Bridge is broken' },
  { id: 'g5', parent_id: 'root', objective: 'Talk to elder', status: 'blocked', retired: true, blocked_reason: 'No longer needed.' },
], ...over });

test('planning pauses show a review note', async () => {
  const { panelRegions } = await load();
  const plan = applePlan();
  const goals = extra => panelRegions(run('Investigating', { subgoal_plan: plan, ...extra }), ctx([])).goals;
  assert.match(goals({ pause: { category: 'planner_rejected', reason: 'The planner proposed <x>' } }), /Needs review: The planner proposed &lt;x&gt;/);
  assert.match(goals({ pause: { category: 'planner_blocked', reason: 'blocked' } }), /Needs review: blocked/);
  assert.doesNotMatch(goals({ pause: { category: 'goal_recalculation', reason: 'r' }, goal_recalculation: { status: 'planning' } }), /goal-review/);
  assert.match(goals({ pause: { category: 'goal_recalculation', reason: 'r' }, goal_recalculation: { status: 'failed' } }), /goal-review/);
  assert.doesNotMatch(goals({ pause: { category: 'provider_failure', reason: 'r' } }), /goal-review/);
  const blocked = panelRegions(run('Running', { subgoal_plan: { ...plan, status: 'blocked', reason: 'stuck' } }), ctx([])).goals;
  assert.match(blocked, /Needs review: stuck/);
});

const pe = (seq, kind, payload) => ({ id: `evt_${seq}`, seq, kind, timestamp: NOW - seq, payload });

test('the goal graph nests goals and annotations under the goal they belong to', async () => {
  const { goalGraph } = await loadGoals();
  const plain = goalGraph(farmPlan(), []);
  assert.deepEqual(plain.children.get('root'), ['g1', 'g2', 'g4', 'g5']);
  assert.deepEqual(plain.children.get('g2'), ['g3']);
  assert.equal(plain.nodes.get('g5').status, 'retired');
  assert.equal(plain.nodes.get('g4').status, 'blocked');
  const graph = goalGraph(farmPlan(), [pe(13, 'subgoal_plan_rejected', { goal_id: 'g2', attempt: 1, reason: 'Too vague' }), pe(14, 'subgoal_plan_rejected', { goal_id: 'nope', attempt: 2, reason: 'x' })]);
  assert.deepEqual(graph.children.get('g2'), ['g3', 'rejected-13']);
  assert.equal(graph.children.get('root').at(-1), 'rejected-14', 'a rejection naming an unknown goal sits under the root');
  assert.equal(graph.nodes.get('rejected-13').title, 'Proposal rejected (attempt 1)');
});

test('operator instructions anchor to the goal the next planning round used them for', async () => {
  const { goalGraph } = await loadGoals();
  const graph = goalGraph(farmPlan(), [
    pe(20, 'intervention', { command: 'takeover_finish', reason: 'Use the hoe' }),
    pe(21, 'subgoal_planning_context', { context: { current_goal: { id: 'g2' }, operator_instruction: { text: 'Use the hoe' } } }),
    pe(22, 'intervention', { command: 'resume', reason: 'Go north' }),
    pe(23, 'intervention', { command: 'resume', reason: '' }),
    pe(24, 'intervention', { command: 'pause', reason: 'x' }),
  ]);
  assert.ok(graph.children.get('g2').includes('operator-20'));
  assert.equal(graph.nodes.get('operator-20').title, 'Hand back: Use the hoe');
  assert.ok(graph.children.get('root').includes('operator-22'), 'an instruction no planning round used sits under the root');
  assert.equal(graph.nodes.get('operator-22').title, 'Resume: Go north');
  assert.equal(graph.nodes.has('operator-23'), false);
  assert.equal(graph.nodes.has('operator-24'), false);
});

const recalculated = pe(31, 'subgoal_plan_recalculation_prepared', { previous_plan: { root_id: 'root', revision: 3, goals: [{ id: 'root', objective: 'Old root', status: 'active' },
  { id: 'goal-3-0', parent_id: 'root', objective: 'Old A', status: 'completed' }, { id: 'goal-3-1', parent_id: 'root', objective: 'Old B', status: 'pending' }] } });

test('replaced plans become earlier-plan nodes and older annotations fall back to the root', async () => {
  const { goalGraph } = await loadGoals();
  const graph = goalGraph(farmPlan(), [
    pe(30, 'subgoal_plan_rejected', { goal_id: 'g2', attempt: 1, reason: 'old' }),
    recalculated,
    pe(32, 'subgoal_plan_reset', { previous_plan: { revision: 5, goals: [] }, reason: 'Primary objective changed' }),
  ]);
  assert.ok(graph.children.get('root').includes('rejected-30'), 'a rejection from before the replan sits under the root');
  assert.ok(!graph.children.get('g2').includes('rejected-30'));
  assert.equal(graph.nodes.get('earlier-31').title, 'Earlier plan, revision 3');
  assert.equal(graph.nodes.get('earlier-32').title, 'Earlier plan, revision 5');
  assert.ok(graph.children.get('root').includes('earlier-31') && graph.children.get('root').includes('earlier-32'));
  assert.deepEqual(graph.children.get('root').slice(0, 4), ['g1', 'g2', 'g4', 'g5']);
});

test('plan history lists planning events newest first with jump chips', async () => {
  const { panelRegions } = await load();
  const { planHistory } = await loadGoals();
  const events = [
    pe(3, 'subgoal_planning_context', { context: { current_goal: { objective: 'Obtain wheat' } } }),
    pe(4, 'subgoal_plan_rejected', { attempt: 1, reason: 'Bad <b>' }),
    pe(5, 'subgoal_plan_updated', { plan: { revision: 2, reason: 'Recipe', goals: [{ id: 'root' }, { id: 'goal-2-0' }, { id: 'goal-2-1' }] } }),
    pe(6, 'goal_recalculation_failed', { reason: 'nope' }),
    pe(7, 'action_result', { result: { status: 'accepted' } }),
    pe(8, 'intervention', { command: 'takeover_finish', reason: 'Buy <the> hoe' }),
    pe(9, 'intervention', { command: 'resume', reason: '' }),
    pe(10, 'intervention', { command: 'pause', reason: 'x' }),
    pe(11, 'subgoal_planning_context', { context: { current_goal: { objective: 'Buy hoe' }, operator_instruction: { text: 'Buy the hoe', after: 'hand_back' } } }),
  ];
  const history = planHistory(events);
  assert.deepEqual(history.map(h => h.seq), [11, 8, 6, 5, 4, 3], 'resume with no reason and pause are not listed');
  assert.deepEqual(history.map(h => h.text), ['Planning for “Buy hoe” with your instruction', 'Hand back with instruction: Buy <the> hoe', 'Recalculation failed: nope', 'Revision 2 added 2 goals: Recipe', 'Proposal rejected (attempt 1): Bad <b>', 'Planning for “Obtain wheat”']);
  assert.equal(history.find(h => h.seq === 8).label, 'Operator');
  const html = panelRegions(run('Running', { subgoal_plan: applePlan() }), ctx(events)).goals;
  assert.equal((html.match(/<li><time data-ts="\d+"><\/time>/g) || []).length, 6);
  assert.match(html, /Buy &lt;the&gt; hoe/);
  assert.match(html, /Bad &lt;b&gt;/);
  assert.doesNotMatch(html, /<b>/);
  const many = Array.from({ length: 25 }, (_, i) => pe(i + 1, 'subgoal_plan_rejected', { attempt: i, reason: 'r' }));
  assert.equal(planHistory(many).length, 20);
  assert.match(panelRegions(run('Running', { subgoal_plan: applePlan() }), ctx([])).goals, /No planning activity is loaded/);
  assert.ok(planHistory([pe(1, 'subgoal_plan_reset', { reason: 'y'.repeat(300) })])[0].text.length <= 200);
});

// The goals region for a run; the pane is the selected-node section and nodeOf one node's button.
const goalsOf = async (r, events = [], extra = {}) => (await load()).panelRegions(r, ctx(events, extra)).goals;
const pane = html => html.match(/<section class="goal-detail".*?<\/section>/s)[0];
const nodeOf = (html, id) => html.match(new RegExp(`<button[^>]*data-node="${id}"[^>]*>.*?</button>`, 's'))[0];
const count = (html, pattern) => (html.match(new RegExp(pattern, 'g')) || []).length;

test('nodes show words and icons, and exactly one is current', async () => {
  const farm = over => goalsOf(run('Running', { subgoal_plan: farmPlan(over) }));
  const html = await farm();
  assert.equal(count(html, 'aria-current="step"'), 1);
  assert.ok(nodeOf(html, 'g2').includes('aria-current="step"'));
  assert.equal(count(html, 'dot-live'), 1);
  for (const text of ['Now, 1 of 3 so far', '#i-target']) assert.ok(nodeOf(html, 'g2').includes(text), text);
  for (const text of ['Completed', '#i-circle-check', 'goal-node-done']) assert.ok(nodeOf(html, 'g1').includes(text), text);
  for (const text of ['Primary goal', '#i-flag']) assert.ok(nodeOf(html, 'root').includes(text), text);
  assert.ok(nodeOf(html, 'g3').includes('Pending'));
  assert.match(html, /Judged by you, not by ASHAD\. Plan revision 7\./);
  assert.doesNotMatch(html, /Working on now|<details/, 'the Working on now block and the details tree are gone');
  const rootNow = await farm({ active_goal_id: 'root' });
  assert.ok(nodeOf(rootNow, 'root').includes('aria-current="step"'));
  assert.ok(pane(rootNow).includes('Choosing the next concrete goal under the primary goal.'));
  assert.ok(pane(await farm({ active_goal_id: 'root', status: 'blocked', reason: 'stuck' })).includes('Waiting for your review.'));
  assert.ok(nodeOf(await farm({ active_goal_id: 'gone' }), 'root').includes('aria-current="step"'), 'an unknown active goal falls back to the root');
  const unknown = farmPlan();
  unknown.goals[2].completion.kind = 'unknown';
  assert.doesNotMatch(await goalsOf(run('Running', { subgoal_plan: unknown })), /so far/);
  assert.match(await goalsOf(run('Ended')), /No goal plan was recorded for this instance\./);
});

test('blocked goals and rejected proposals are red with their reason; not-needed goals stay muted', async () => {
  const html = await goalsOf(run('Running', { subgoal_plan: farmPlan() }), [pe(13, 'subgoal_plan_rejected', { goal_id: 'g2', attempt: 1, reason: 'Too <b>vague' })]);
  for (const text of ['goal-node-danger', 'Blocked', '#i-circle-x', 'Bridge is broken']) assert.ok(nodeOf(html, 'g4').includes(text), text);
  for (const text of ['goal-node-danger', 'Proposal rejected (attempt 1)', 'Too &lt;b&gt;vague']) assert.ok(nodeOf(html, 'rejected-13').includes(text), text);
  for (const text of ['goal-node-muted', 'Not needed', 'No longer needed.']) assert.ok(nodeOf(html, 'g5').includes(text), text);
  assert.ok(!nodeOf(html, 'g5').includes('goal-node-danger'), 'retirement is not failure');
});

test('selecting a node shows its evidence, attempts and links', async () => {
  const events = [obsEvent(3, 'o1'), pe(12, 'subgoals_verified', { goal_ids: ['g1'] }), pe(13, 'subgoal_plan_rejected', { goal_id: 'g1', attempt: 1, reason: 'r' }),
    pe(20, 'intervention', { command: 'resume', reason: 'Go north' })];
  const farm = (selectedGoal, extra = {}) => goalsOf(run('Running', { subgoal_plan: farmPlan(), ...extra }), events, { selectedGoal });
  const current = pane(await farm(null, { recovery_instruction: 'Dig' }));
  for (const text of ['Get 3 wheat', 'Have 3 wheat', '1 of 3 so far', 'Why: Follow the recipe', 'Your instruction: Dig']) assert.ok(current.includes(text), text);
  const html = await farm('g1');
  for (const text of ['seed bag', 'Actions tried since progress: 2', 'Rejected proposals: 1', 'data-seq="3">Planned from #3', 'data-seq="12">Verified #12']) assert.ok(pane(html).includes(text), text);
  assert.ok(!pane(html).includes('Why:'), 'Why belongs to the current goal only');
  assert.equal(count(html, 'aria-pressed="true"'), 1);
  assert.ok(nodeOf(html, 'g1').includes('aria-pressed="true"'));
  assert.ok(pane(await farm('g3')).includes('No planning evidence recorded.'));
  assert.ok(pane(await farm('nope')).includes('Get 3 wheat'), 'an unknown selection shows the current node');
  assert.ok(pane(await farm('rejected-13')).includes('data-seq="13">Rejected #13'));
  const operator = pane(await farm('operator-20'));
  for (const text of ['Go north', 'data-seq="20">Operator #20']) assert.ok(operator.includes(text), text);
});

test('an earlier plan lists its goals in the pane', async () => {
  const html = await goalsOf(run('Running', { subgoal_plan: farmPlan() }), [recalculated], { selectedGoal: 'earlier-31' });
  for (const text of ['Earlier plan, revision 3', '#i-clock', 'goal-node-muted']) assert.ok(nodeOf(html, 'earlier-31').includes(text), text);
  for (const text of ['Old A', 'Completed', 'Old B', 'Pending', 'data-seq="31">Plan #31']) assert.ok(pane(html).includes(text), text);
  assert.ok(!pane(html).includes('Old root'), 'the old root is the primary goal, not a listed goal');
  assert.ok(nodeOf(html, 'g2'));
});

test('only nodes new since the last render animate in', async () => {
  const rejected = [pe(13, 'subgoal_plan_rejected', { goal_id: 'g2', attempt: 1, reason: 'r' })];
  assert.equal(count(await goalsOf(run('Running', { subgoal_plan: farmPlan() }), rejected), 'goal-node-new'), 0, 'the first render animates nothing');
  const html = await goalsOf(run('Running', { subgoal_plan: farmPlan() }), rejected, { seenNodes: new Set(['root', 'g1', 'g3', 'g4', 'g5']) });
  assert.ok(nodeOf(html, 'g2').includes('goal-node-new'));
  assert.ok(nodeOf(html, 'rejected-13').includes('goal-node-new'));
  assert.equal(count(html, 'goal-node-new'), 2);
});

test('a changed primary goal or rebuilt plan clears the goal selection and seen nodes', async () => {
  const { resetGoalState } = await loadGoals();
  const thread = () => ({ goalSelected: 'g2', goalSeen: new Set(['g2']) });
  const same = {};
  resetGoalState(same, farmPlan());
  Object.assign(same, thread());
  resetGoalState(same, farmPlan({ revision: 8 }));
  assert.equal(same.goalSelected, 'g2', 'the same plan keeps its selection');
  assert.deepEqual([...same.goalSeen], ['g2']);
  const changed = {};
  resetGoalState(changed, farmPlan());
  Object.assign(changed, thread());
  resetGoalState(changed, farmPlan({ primary_goal: 'Another goal' }));
  assert.deepEqual([changed.goalSelected, changed.goalSeen], [null, null], 'a new primary goal starts over');
  const rebuilt = {};
  resetGoalState(rebuilt, farmPlan({ revision: 3 }));
  Object.assign(rebuilt, thread());
  resetGoalState(rebuilt, farmPlan({ revision: 0 }));
  assert.deepEqual([rebuilt.goalSelected, rebuilt.goalSeen], [null, null], 'a revision-0 rebuild starts over');
  const html = await goalsOf(run('Running', { subgoal_plan: farmPlan() }), [], { selectedGoal: rebuilt.goalSelected, seenNodes: rebuilt.goalSeen });
  assert.equal(count(html, 'goal-node-new'), 0, 'no stale suppression or animation on the first render');
  assert.ok(pane(html).includes(farmPlan().goals.find(g => g.id === farmPlan().active_goal_id)?.objective ?? ''), 'the pane falls back to the current node');
});

test('goal text, reasons and instructions are escaped in nodes and the pane', async () => {
  const evil = '<img src=x onerror=alert(1)>';
  const plan = farmPlan({ primary_goal: evil, reason: evil });
  Object.assign(plan.goals[4], { objective: evil, completion: { description: evil }, blocked_reason: evil });
  const events = [
    pe(1, 'subgoal_plan_reset', { previous_plan: { revision: 2, goals: [{ id: 'old', parent_id: 'root', objective: evil, status: 'pending' }] }, reason: evil }),
    pe(2, 'subgoal_plan_rejected', { goal_id: 'g4', attempt: 1, reason: evil }),
    pe(3, 'intervention', { command: 'resume', reason: evil }),
  ];
  const r = run('Investigating', { subgoal_plan: plan, recovery_instruction: evil, pause: { category: 'planner_rejected', reason: evil } });
  for (const selectedGoal of [null, 'earlier-1', 'rejected-2', 'operator-3', 'g4']) {
    assert.doesNotMatch(await goalsOf(r, events, { selectedGoal }), /<img/, `selected ${selectedGoal}`);
  }
});
