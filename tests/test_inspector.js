'use strict';
// Inspector: retained observations only, freshness badges, decision-time state, tokens and goals (ashad/static/views/inspector.js).
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { pathToFileURL } = require('node:url');
const load = () => import(pathToFileURL(path.join(__dirname, '../ashad/static/views/inspector.js')).href);

const NOW = 10000;
const obsEvent = (seq, id, extra = {}, ts = NOW - 5) => ({ id: `evt_${seq}`, seq, kind: 'structured_decision_context', timestamp: ts,
  payload: { player_id: 'p1', full_observation: { observation_id: id, state: { player: { inventory: [{ item_id: 'wood', count: 2 }] } }, entities: [], ...extra } } });
const run = (state, extra = {}) => ({ id: 'r1', state, usage: [], ...extra });
const ctx = (events, extra = {}) => ({ latest: events.findLast(e => e.payload?.full_observation) || null, selectedEvent: null, events, view: 'latest', now: NOW, goalOpen: new Set(), ...extra });

test('the inspector never requests live game context', () => {
  for (const file of ['ashad/static/views/inspector.js', 'ashad/static/app.js']) {
    assert.doesNotMatch(fs.readFileSync(path.join(__dirname, '..', file), 'utf8'), /\/context/, `${file} must not call …/context`);
  }
});

test('fresh, stale, partial and unknown render from fixtures', async () => {
  const { freshness, inspectorRegions, STALE_AFTER_S } = await load();
  const labels = (e, r, now = NOW) => freshness(e, r, now).map(b => b.label);
  assert.deepEqual(labels(null, run('Running')), ['Unknown']);
  assert.deepEqual(labels(obsEvent(1, 'o1'), run('Running')), ['Fresh']);
  assert.deepEqual(labels(obsEvent(1, 'o1', {}, NOW - STALE_AFTER_S - 1), run('Running')), ['Stale']);
  assert.deepEqual(labels(obsEvent(1, 'o1'), run('Paused')), ['Stale · as of pause']);
  assert.deepEqual(labels(obsEvent(1, 'o1', { complete: false }), run('Running')), ['Fresh', 'Partial']);
  const unknown = inspectorRegions(run('Running'), ctx([]))['insp-world'];
  assert.match(unknown, /Unknown/);
  assert.match(unknown, /No structured observation has been retained/);
  const world = inspectorRegions(run('Running'), ctx([obsEvent(1, 'o1', { complete: false })]))['insp-world'];
  assert.match(world, /observed 5s ago/);
  assert.match(world, /Partial/);
  assert.match(world, /Hotbar<\/h4><span class="insp-missing">Not observed/, 'a missing field says so instead of rendering empty');
  assert.match(world, /Objectives<\/h4><span class="insp-missing">Not observed/);
});

test('a decision without a retained observation says so instead of showing the latest', async () => {
  const { decisionObservation, inspectorRegions } = await load();
  const events = [obsEvent(1, 'obs_old'), { id: 'evt_2', seq: 2, kind: 'action_result', timestamp: NOW, payload: { result: { status: 'accepted' } } }, obsEvent(3, 'obs_latest')];
  const selected = { id: 'evt_4', seq: 4, kind: 'decision_selected', timestamp: NOW, payload: { observation_id: 'obs_missing', action: { tool: 'interact' } } };
  assert.equal(decisionObservation(events[1], events), null);
  assert.equal(decisionObservation(selected, events), null);
  const html = inspectorRegions(run('Running'), ctx(events, { selectedEvent: selected, view: 'decision' }))['insp-world'];
  assert.match(html, /Not retained for this decision/);
  assert.doesNotMatch(html, /obs_latest/);
  assert.match(html, /At decision #4/);
  const found = { ...selected, payload: { observation_id: 'obs_old' } };
  assert.equal(decisionObservation(found, events).seq, 1, 'a retained observation is matched by ID');
  assert.match(inspectorRegions(run('Running'), ctx(events, { selectedEvent: found, view: 'decision' }))['insp-world'], /obs_old/);
});

test('cached and reasoning tokens are subsets, never double-counted', async () => {
  const { usageTotals, inspectorRegions } = await load();
  const rows = [
    { usage: { input_tokens: 1000, cached_input_tokens: 600, output_tokens: 200, reasoning_output_tokens: 150, total_tokens: 1200 }, estimated_cost_usd: null },
    { usage: { input_tokens: 500, cached_input_tokens: 0, output_tokens: 100, reasoning_output_tokens: 40 }, estimated_cost_usd: null },
    { usage: null, estimated_cost_usd: null },
  ];
  const t = usageTotals(rows);
  assert.deepEqual([t.total, t.input, t.cached, t.output, t.reasoning, t.turns, t.counted, t.cost], [1800, 1500, 600, 300, 190, 3, 2, null]);
  const html = inspectorRegions(run('Running', { usage: rows }), ctx([]))['insp-tokens'];
  assert.match(html, /1,800/);
  assert.match(html, /1 of 3 turns recorded no token counts/);
  assert.match(html, /Estimate, not a charge\. Billed through your ChatGPT subscription\./);
  assert.match(html, /— not available/, 'no invented dollar figure for subscription usage');
  assert.match(inspectorRegions(run('Running', { usage: [{ usage: null }] }), ctx([]))['insp-tokens'], /— unknown/);
});

test('blocked goals are escaped and explained; the primary goal always needs acceptance', async () => {
  const { inspectorRegions } = await load();
  const evil = '<img src=x onerror=alert(1)>';
  const plan = { root_id: 'root', active_goal_id: 'g2', goals: [
    { id: 'root', parent_id: null, objective: 'Craft a pickaxe', status: 'active', completion: { description: 'Manual' } },
    { id: 'g1', parent_id: 'root', objective: 'Collect stone', status: 'completed', completion: { description: '3 stone' }, completion_evidence: { observation_id: 'o9' } },
    { id: 'g2', parent_id: 'root', objective: `Cross ${evil}`, status: 'blocked', blocked_reason: `Bridge ${evil} is broken` },
    { id: 'g3', parent_id: 'root', objective: 'Talk to elder', status: 'blocked', retired: true, blocked_reason: 'No longer needed.' },
  ] };
  const verified = { id: 'evt_7', seq: 7, kind: 'subgoals_verified', timestamp: NOW, payload: { goal_ids: ['g1'] } };
  const html = inspectorRegions(run('Running', { subgoal_plan: plan }), ctx([verified]))['insp-goal'];
  assert.doesNotMatch(html, /<img/);
  assert.match(html, /Bridge &lt;img/);
  assert.match(html, /Blocked/);
  assert.match(html, /Needs your acceptance/);
  assert.match(html, /Not needed/);
  assert.match(html, /data-action="tl-jump" data-seq="7">Verified #7/, 'verification links to its timeline card');
  assert.match(html, /aria-label="Currently chasing"><span>Craft a pickaxe<\/span>.*<span>Cross &lt;img/);
  assert.match(inspectorRegions(run('Ended'), ctx([]))['insp-goal'], /No goal plan was recorded for this instance\./);
});
