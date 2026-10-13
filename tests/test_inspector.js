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
const ctx = (events, extra = {}) => ({ latest: events.findLast(e => e.payload?.full_observation) || null, selectedEvent: null, events, view: 'latest', now: NOW, goalOpen: new Set(), ...extra });

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

test('blocked goals are escaped and explained; the primary goal is judged by the operator', async () => {
  const { panelRegions } = await load();
  const evil = '<img src=x onerror=alert(1)>';
  const plan = { root_id: 'root', active_goal_id: 'g2', goals: [
    { id: 'root', parent_id: null, objective: 'Craft a pickaxe', status: 'active', completion: { description: 'Manual' } },
    { id: 'g1', parent_id: 'root', objective: 'Collect stone', status: 'completed', completion: { description: '3 stone' }, completion_evidence: { observation_id: 'o9' } },
    { id: 'g2', parent_id: 'root', objective: `Cross ${evil}`, status: 'blocked', blocked_reason: `Bridge ${evil} is broken` },
    { id: 'g3', parent_id: 'root', objective: 'Talk to elder', status: 'blocked', retired: true, blocked_reason: 'No longer needed.' },
  ] };
  const verified = { id: 'evt_7', seq: 7, kind: 'subgoals_verified', timestamp: NOW, payload: { goal_ids: ['g1'] } };
  const html = panelRegions(run('Running', { subgoal_plan: plan }), ctx([verified])).goals;
  assert.doesNotMatch(html, /<img/);
  assert.match(html, /Bridge &lt;img/);
  assert.match(html, /Blocked/);
  assert.match(html, /Primary goal/);
  assert.match(html, /Judged by you, not by ASHAD\./);
  assert.doesNotMatch(html, /Needs your acceptance/);
  assert.doesNotMatch(html, /goal-review/);
  assert.match(html, /Not needed/);
  assert.match(html, /data-action="tl-jump" data-seq="7">Verified #7/, 'verification links to its timeline card');
  assert.match(html, /aria-label="Working on now">.*Cross &lt;img.*class="goal-path">Craft a pickaxe/s);
  assert.match(panelRegions(run('Ended'), ctx([])).goals, /No goal plan was recorded for this instance\./);
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

test('the active goal is marked Now with measured progress', async () => {
  const { panelRegions } = await load();
  const goals = (plan, goalOpen = new Set(['g2'])) => panelRegions(run('Running', { subgoal_plan: plan }), ctx([], { goalOpen })).goals;
  const html = goals(applePlan());
  const now = html.match(/<section class="goal-now".*?<\/section>/s)[0];
  for (const text of ['Obtain 30 wheat', 'Have 30 wheat', '8 of 30 so far', 'Why: Follow the recipe', 'Make 3 apple pies › Obtain 3 flour']) assert.ok(now.includes(text), text);
  assert.match(html, /Now, 8 of 30 so far/);
  assert.match(html, /Actions tried since progress: 3/);
  assert.equal((html.match(/class="goal-status">Now/g) || []).length, 1);
  assert.match(html, /Plan revision 4\./);
  const unknown = applePlan();
  unknown.goals[2].completion.kind = 'unknown';
  assert.doesNotMatch(goals(unknown), /so far/);
  assert.match(goals(applePlan({ active_goal_id: 'root' })), /Choosing the next concrete goal/);
  assert.match(goals(applePlan({ active_goal_id: 'root', status: 'blocked', reason: 'stuck' })), /goal-now-objective">Waiting for your review\./);
});

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

test('working on now shows the current instruction', async () => {
  const { panelRegions } = await load();
  const now = extra => panelRegions(run('Running', extra), ctx([])).goals.match(/<section class="goal-now".*?<\/section>/s)[0];
  assert.ok(now({ subgoal_plan: applePlan(), recovery_instruction: '<b>go</b>' }).includes('Your instruction: &lt;b&gt;go&lt;/b&gt;'));
  assert.ok(!now({ subgoal_plan: applePlan() }).includes('Your instruction'));
  assert.ok(now({ subgoal_plan: applePlan({ active_goal_id: 'root' }), recovery_instruction: 'go' }).includes('Your instruction: go'));
});

test('goal text and plan reasons are escaped in every block', async () => {
  const { panelRegions } = await load();
  const evil = '<img src=x onerror=alert(1)>';
  const plan = applePlan({ primary_goal: evil, reason: evil });
  plan.goals[2].objective = evil;
  plan.goals[2].completion.description = evil;
  const events = [pe(1, 'subgoal_plan_rejected', { attempt: 1, reason: evil })];
  const html = panelRegions(run('Investigating', { subgoal_plan: plan, pause: { category: 'planner_rejected', reason: evil } }), ctx(events)).goals;
  assert.doesNotMatch(html, /<img/);
});
