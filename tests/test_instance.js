'use strict';
// Instance thread: camera lifecycle, controls and the §9 matrix (ashad/static/views/instance.js).
const { test } = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');
const { pathToFileURL } = require('node:url');
const load = () => import(pathToFileURL(path.join(__dirname, '../ashad/static/views/instance.js')).href);

const run = (state, extra = {}) => ({
  id: 'r1', state, model: 'gpt-6-astra', agent_id: 'a',
  plan: { primary_goal: 'Craft a pickaxe', players: [{ id: 'p1', name: 'Player 1' }], transport: { mode: 'enet' } },
  players: { p1: { id: 'p1', epoch: 'e1', launched: state !== 'Ended' && state !== 'Queued' } },
  subgoal_plan: { player_id: 'p1' }, ...extra,
});

// Minimal DOM stand-in that logs every src write, append and removal in order.
function fakeCamera(createCamera) {
  const log = [];
  const timers = [];
  const frame = { children: [], append(el) { this.children.push(el); log.push(`append ${el.n}`); } };
  let n = 0;
  const doc = {
    createElement() {
      const el = { n: ++n, listeners: {}, value: '', addEventListener(type, fn) { this.listeners[type] = fn; },
        remove() { frame.children = frame.children.filter(c => c !== this); log.push(`remove ${this.n}`); } };
      Object.defineProperty(el, 'src', { get() { return this.value; }, set(v) { this.value = v; log.push(`src ${this.n} ${v}`); } });
      return el;
    },
  };
  const camera = createCamera(frame, { doc, setTimer: (fn, ms) => timers.push({ fn, ms }), clearTimer: id => { if (id) timers[id - 1].fn = null; } });
  const fire = () => { const t = timers.find(x => x.fn); const fn = t.fn; t.fn = null; fn(); };
  return { camera, frame, log, timers, fire };
}

test('camera: an epoch change tears down the old <img> before creating the new one', async () => {
  const { createCamera, cameraTarget } = await load();
  const f = fakeCamera(createCamera);
  f.camera.sync(cameraTarget(run('Running'), 'p1', false));
  f.camera.sync(cameraTarget(run('Running', { players: { p1: { epoch: 'e2', launched: true } } }), 'p1', false));
  assert.deepEqual(f.log, ['src 1 /api/runs/r1/players/p1/live?epoch=e1', 'append 1', 'src 1 ', 'remove 1', 'src 2 /api/runs/r1/players/p1/live?epoch=e2', 'append 2']);
  assert.equal(f.frame.children.length, 1);
});

test('camera: polling with the same target never rewrites src', async () => {
  const { createCamera, cameraTarget } = await load();
  const f = fakeCamera(createCamera);
  for (let i = 0; i < 5; i++) f.camera.sync(cameraTarget({ ...run('Running'), events: [i] }, 'p1', false));
  assert.equal(f.log.filter(line => line.startsWith('src')).length, 1);
});

test('camera: a hidden page stops the feed and a visible one restarts it', async () => {
  const { createCamera, cameraTarget } = await load();
  const f = fakeCamera(createCamera);
  f.camera.sync(cameraTarget(run('Running'), 'p1', false));
  assert.equal(cameraTarget(run('Running'), 'p1', true), null);
  f.camera.sync(cameraTarget(run('Running'), 'p1', true));
  assert.deepEqual(f.log.slice(-2), ['src 1 ', 'remove 1']);
  assert.equal(f.camera.status, 'offline');
  f.camera.sync(cameraTarget(run('Running'), 'p1', false));
  assert.equal(f.frame.children.length, 1);
});

test('camera: a failed feed retries with backoff, and only while visible', async () => {
  const { createCamera, cameraTarget } = await load();
  const f = fakeCamera(createCamera);
  const live = cameraTarget(run('Running'), 'p1', false);
  f.camera.sync(live);
  f.frame.children[0].listeners.error();
  assert.equal(f.camera.status, 'reconnecting');
  assert.equal(f.frame.children.length, 0, 'the failed <img> is released');
  f.camera.sync(live);  // A poll during the backoff does not jump the queue.
  assert.equal(f.frame.children.length, 0);
  f.fire();
  assert.equal(f.frame.children.length, 1, 'retried after the backoff');
  f.frame.children[0].listeners.error();
  assert.ok(f.timers.at(-1).ms > f.timers[0].ms, 'backoff grows');
  f.camera.sync(null);  // Page hidden mid-backoff.
  assert.equal(f.timers.filter(t => t.fn).length, 0, 'the pending retry is cancelled');
  f.camera.sync(live);  // Visible again: reconnect at once.
  assert.equal(f.frame.children.length, 1);
});

test('camera: an ended or not-yet-launched run never requests /live', async () => {
  const { cameraTarget } = await load();
  assert.equal(cameraTarget(run('Ended', { players: { p1: { epoch: 'e1', launched: true } } }), 'p1', false), null);
  assert.equal(cameraTarget(run('Queued'), 'p1', false), null);
  assert.equal(cameraTarget(run('Starting', { players: { p1: { epoch: 'e1', launched: false } } }), 'p1', false), null);
  assert.equal(cameraTarget(run('Running'), 'missing', false), null);
  assert.match(cameraTarget(run('Stopping'), 'p1', false).src, /epoch=e1$/);
});

test('controls: each command keeps one command_id across retries and ignores clicks while pending', async () => {
  const { runCommand } = await load();
  const commands = new Map();
  const ids = [];
  let fail = true;
  const send = async id => { ids.push(id); if (fail) throw new Error('Worker did not answer'); return { id: 'r1' }; };
  assert.equal((await runCommand(commands, 'r1:pause', send)).error.message, 'Worker did not answer');
  assert.equal(commands.get('r1:pause').error, 'Worker did not answer');
  fail = false;
  const first = runCommand(commands, 'r1:pause', send);
  assert.deepEqual(await runCommand(commands, 'r1:pause', send), { skipped: true }, 'a second click while pending sends nothing');
  await first;
  assert.equal(ids.length, 2);
  assert.equal(ids[0], ids[1], 'the retry reused the command ID');
  assert.equal(commands.has('r1:pause'), false);
  await runCommand(commands, 'r1:pause', send);
  assert.notEqual(ids[2], ids[0], 'a new click after success is a new command');
});

test('controls: recalculate pauses once and a late GET cannot overwrite its result', async () => {
  const { runCommand, createRunSlot } = await load();
  const slot = createRunSlot();
  const commands = new Map();
  let posts = 0;
  let release;
  const lateGet = slot.begin();  // A run poll already in flight.
  const recalc = runCommand(commands, 'r1:recalculate', () => { posts += 1; return new Promise(resolve => { release = resolve; }); });
  await runCommand(commands, 'r1:recalculate', () => { posts += 1; });
  release({ id: 'r1', state: 'Paused', goal_recalculation: { status: 'completed' } });
  slot.set((await recalc).value);
  assert.equal(lateGet({ id: 'r1', state: 'Running' }), false, 'the stale GET is dropped');
  assert.equal(slot.get().state, 'Paused');
  assert.equal(posts, 1);
  const fresh = slot.begin();
  assert.equal(fresh({ id: 'r1', state: 'Paused' }), true, 'a GET started afterwards applies');
});

test('ownership: paused, ended and unconfirmed teardown map to the §9 matrix', async () => {
  const { instanceState } = await load();
  const paused = instanceState(run('Paused'));
  assert.deepEqual([paused.badge.label, paused.left, paused.stop], ['Paused', ['resume', 'takeover'], true]);
  const ended = instanceState(run('Ended', { ending_reason: 'operator_stop' }));
  assert.deepEqual([ended.badge.label, ended.left, ended.stop, ended.menu], ['Ended · Operator stop', ['report', 'evidence'], false, ['flag']]);
  const unconfirmed = instanceState(run('Ended'), { run_id: 'r1', reason: 'teardown_unconfirmed' });
  assert.deepEqual([unconfirmed.badge.label, unconfirmed.left, unconfirmed.stop, unconfirmed.menu], ['Teardown unconfirmed', ['reconcile'], false, ['report', 'evidence']]);
  const human = instanceState(run('Paused', { human_control: { player_id: 'p1', status: 'active' } }));
  assert.deepEqual([human.badge.label, human.left, human.stop], ['You have control', ['handback'], true]);
  assert.deepEqual(instanceState(run('Stopping')).note, 'Waiting for teardown confirmation');
  assert.equal(instanceState(run('Stopping')).stop, false);
  assert.deepEqual(instanceState(run('Paused', { goal_recalculation: { status: 'planning' } })).left, []);
  assert.equal(instanceState(run('Weird'), { reason: 'orphaned_lease' }).stop, true, 'an unknown run still owning the slot can be stopped');
  assert.equal(instanceState(run('Weird')).stop, false);
  assert.ok(instanceState(run('Investigating')).menu.includes('extend'));
  assert.ok(!instanceState(run('Running')).menu.includes('extend'), 'the backend only extends an investigation');
  assert.ok(!instanceState(run('Running', { external_control: { owner: 'mcp' } })).menu.includes('recalculate'));
});

test('thread markup escapes goals and pause reasons and keeps Stop right-most', async () => {
  const { instanceState, threadRegions } = await load();
  const evil = '<img src=x onerror=alert(1)>';
  const r = run('Investigating', { plan: { ...run('x').plan, primary_goal: evil }, pause: { reason: evil, recovery: evil } });
  const status = instanceState(r);
  const regions = threadRegions(r, { status, data: { agents: [{ id: 'a', name: evil }] }, ui: { inspectorOpen: true }, pending: new Set(), error: evil, cameraStatus: 'offline', player: 'p1' });
  const html = Object.values(regions).join('');
  assert.doesNotMatch(html, /<img/);
  assert.match(regions.banner, /&lt;img/);
  const buttons = regions.controls.match(/data-ctl="[\w-]+"/g);
  assert.equal(buttons.at(-1), 'data-ctl="stop"');
  const ended = threadRegions(run('Ended'), { status: instanceState(run('Ended')), data: {}, ui: {}, pending: new Set(), cameraStatus: 'offline', player: 'p1' });
  assert.match(ended.overlay, /Instance ended · camera offline/);
  assert.doesNotMatch(Object.values(ended).join(''), /\/live/);
});
