'use strict';
// Instance thread: camera lifecycle, controls and the §9 matrix (ashad/static/views/instance.js).
const { test } = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');
const { pathToFileURL } = require('node:url');
const load = () => import(pathToFileURL(path.join(__dirname, '../ashad/static/views/instance.js')).href);

const run = (state, extra = {}) => ({
  id: 'r1', state, model: 'gpt-6-astra', configuration_id: 'a',
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
  const regions = threadRegions(r, { status, data: { configurations: [{ id: 'a', name: evil }] }, ui: {}, pending: new Set(), error: evil, cameraStatus: 'offline', player: 'p1', tab: 'bot' });
  const html = Object.values(regions).join('');
  assert.doesNotMatch(html, /<img/);
  assert.match(regions.banner, /&lt;img/);
  const buttons = regions.controls.match(/data-ctl="[\w-]+"/g);
  assert.equal(buttons.at(-1), 'data-ctl="stop"');
  const ended = threadRegions(run('Ended'), { status: instanceState(run('Ended')), data: {}, ui: {}, pending: new Set(), cameraStatus: 'offline', player: 'p1', tab: 'bot' });
  assert.match(ended.overlay, /Instance ended · camera offline/);
  assert.doesNotMatch(Object.values(ended).join(''), /\/live/);
});

test('an active run opens on Bot view and an ended one on Actions', async () => {
  const { defaultTab } = await load();
  for (const state of ['Running', 'Queued', 'Paused']) assert.equal(defaultTab(run(state)), 'bot');
  assert.equal(defaultTab(run('Ended')), 'actions');
});

test('arrow keys wrap through the tabs; Home and End jump to the ends', async () => {
  const { nextTab } = await load();
  assert.equal(nextTab('bot', 'ArrowRight'), 'goals');
  assert.equal(nextTab('actions', 'ArrowRight'), 'bot');
  assert.equal(nextTab('bot', 'ArrowLeft'), 'actions');
  assert.equal(nextTab('world', 'Home'), 'bot');
  assert.equal(nextTab('goals', 'End'), 'actions');
  assert.equal(nextTab('bot', 'a'), null);
  assert.equal(nextTab('bot', '1'), null);
});

test('the tab bar marks exactly one selected tab', async () => {
  const { tabsHTML } = await load();
  const html = tabsHTML('goals');
  const count = re => (html.match(re) || []).length;
  assert.equal(count(/role="tablist"/g), 1);
  assert.equal(count(/role="tab"/g), 4);
  assert.equal(count(/aria-selected="true"/g), 1);
  assert.match(html, /id="tab-goals"[^>]*aria-selected="true" tabindex="0"/);
  assert.equal(count(/tabindex="-1"/g), 3);
  for (const id of ['bot', 'goals', 'world', 'actions']) assert.match(html, new RegExp(`aria-controls="panel-${id}"`));
  assert.doesNotMatch(html, /aria-live/);
});

test('controls and the instruction form sit outside every panel; camera and timeline sit in their own panels', async () => {
  const { threadSkeleton } = await load();
  const s = threadSkeleton();
  const ids = ['bot', 'goals', 'world', 'actions'];
  assert.equal((s.match(/role="tabpanel"/g) || []).length, 4);
  const starts = ids.map(id => s.indexOf(`id="panel-${id}"`));
  const slice = id => { const i = ids.indexOf(id); return s.slice(starts[i], starts[i + 1] ?? s.length); };
  for (const id of ids) {
    assert.match(s, new RegExp(`id="panel-${id}" aria-labelledby="tab-${id}"[^>]*\\bhidden>`));
  }
  assert.match(slice('bot'), /data-camera/);
  assert.match(slice('actions'), /data-timeline/);
  assert.match(slice('actions'), /data-region="usage"/);
  assert.match(slice('goals'), /data-region="goals"/);
  assert.match(slice('world'), /data-region="world"/);
  const firstPanel = s.indexOf('role="tabpanel"');
  for (const region of ['controls', 'tabs', 'instruction']) {
    const at = s.indexOf(`data-region="${region}"`);
    assert.ok(at > -1 && at < firstPanel, region);
  }
});

test('the header has no inspector toggle', async () => {
  const { instanceState, threadRegions } = await load();
  const r = run('Running');
  const regions = threadRegions(r, { status: instanceState(r), data: {}, ui: {}, pending: new Set(), cameraStatus: 'offline', player: 'p1', tab: 'bot' });
  assert.doesNotMatch(regions.head, /toggle-?inspector|Ctrl\+I/i);
  assert.match(regions.tabs, /role="tablist"/);
});

test('header shows a same-provider model menu for a live run and a plain chip for an ended run', async () => {
  const { instanceState, threadRegions } = await load();
  const data = { model_options: [
    { id: 'gpt-6-sol', label: 'GPT-6 Sol', provider: 'codex' }, { id: 'gpt-6-astra', label: 'GPT-6 Astra', provider: 'codex' },
    { id: 'sonnet', label: 'Claude Sonnet', provider: 'claude' }, { id: 'opus', label: 'Claude Opus', provider: 'claude' },
  ] };
  const head = r => threadRegions(r, { status: instanceState(r), data, ui: {}, pending: new Set(), cameraStatus: 'offline', player: 'p1', tab: 'bot' }).head;
  const live = head(run('Running', { plan: { model: 'gpt-6-sol', transport: { mode: 'enet' } } }));
  const meta = live.match(/<div class="thread-meta">(.*?)<button type="button" class="icon-btn meta-more"/s)[1];
  assert.match(meta, /data-field="run-model"/);
  assert.deepEqual([...meta.matchAll(/data-value="([^"]+)"/g)].map(x => x[1]), ['gpt-6-sol', 'gpt-6-astra']);
  assert.match(meta, /aria-selected="true"[^>]*data-value="gpt-6-astra"/);
  assert.match(meta, /data-value="gpt-6-sol"><span class="model-option-label">GPT-6 Sol<\/span><span class="model-default">Default<\/span>/);
  assert.equal(meta.match(/model-default/g).length, 1);
  const pop = live.match(/id="meta-pop"[^>]*>(.*)<\/div>$/s)[1];
  assert.doesNotMatch(pop, /<select|role="listbox"|model-trigger/);
  assert.match(pop, /GPT-6 Astra/);
  const ended = head(run('Ended'));
  assert.doesNotMatch(ended, /data-field="run-model"|model-trigger/);
  assert.match(ended, /<span class="chip-text">GPT-6 Astra<\/span>/);
  assert.doesNotMatch(head(run('Running')), /model-default/);
});

test('save now is in the menu for launched running, paused and investigating only', async () => {
  const { instanceState } = await load();
  for (const state of ['Running', 'Paused', 'Investigating']) {
    assert.ok(instanceState(run(state)).menu.includes('save'), state);
    const unlaunched = run(state, { players: { p1: { id: 'p1', launched: false } } });
    assert.ok(!instanceState(unlaunched).menu.includes('save'), `${state} unlaunched`);
  }
  for (const state of ['Starting', 'Ended']) assert.ok(!instanceState(run(state)).menu.includes('save'), state);
});

const SAVES = [
  { id: 's1', created_at: 1000, trigger: 'risky_action', trigger_detail: { why: 'first_attempt', verb: 'interact', target_kind: 'chest' }, game: { status: 'confirmed' } },
  { id: 's2', created_at: 900, trigger: 'issue', trigger_detail: { issue_type: 'unsupported_action' }, game: { status: 'none' } },
  { id: 's3', created_at: 800, trigger: 'manual', trigger_detail: { label: 'before boss' }, game: { status: 'on_disk' } },
];

test('savesHTML empty state explains automatic saves', async () => {
  const { savesHTML } = await load();
  const html = savesHTML([], { slotFree: true, pending: new Set() });
  assert.match(html, /No saves yet/);
  assert.ok(!html.includes('<button'));
});

test('savesHTML rows describe trigger and game state in words', async () => {
  const { savesHTML } = await load();
  const html = savesHTML(SAVES, { slotFree: true, pending: new Set() });
  for (const text of ['Before first interact on chest', 'Issue: Unsupported action', 'Manual: before boss', 'Last game save on disk', 'Game saved', 'No game save']) assert.ok(html.includes(text), text);
  assert.equal(html.split('data-action="load-save"').length - 1, 3);
  assert.ok(!html.includes('aria-disabled'));
});

test('savesHTML disables load while the slot is occupied', async () => {
  const { savesHTML } = await load();
  const html = savesHTML(SAVES, { slotFree: false, pending: new Set() });
  assert.equal(html.split('aria-disabled="true"').length - 1, 3);
  assert.ok(html.includes('Stop the running instance before loading a save.'));
});

test('savesHTML marks a pending load busy', async () => {
  const { savesHTML } = await load();
  const html = savesHTML(SAVES, { slotFree: true, pending: new Set(['load-save:s2']) });
  const row = html.split('<li ').find(r => r.includes('data-id="s2"'));
  assert.ok(row.includes('aria-busy="true"') && row.includes('i-loader'));
  assert.equal(html.split('aria-busy').length - 1, 1);
});

const ISSUES = [{ issue_id: 'issue_a', type: 'unsupported_action', title: 'Agent could not <img src=x> slot #', status: 'open', occurrences_total: 7,
  file: '/data/issues/unsupported_action__0123.json', hit_count: 3, first_at: 700, last_at: 1000,
  hits: [{ at: 1000, example_id: 'ua_1', source: 'agent_signal', kept: true, save_before: 's1' },
         { at: 900, example_id: 'ua_2', source: 'worker_rejected', kept: false, save_before: 'gone' },
         { at: 800, example_id: 'ua_3', source: 'worker_rejected', kept: false, save_before: null }] }];
const issueOpts = extra => ({ saves: SAVES, slotFree: true, pending: new Set(), ...extra });

test('issues section sits first in the Actions panel', async () => {
  const { threadSkeleton } = await load();
  const s = threadSkeleton();
  const at = t => s.indexOf(t);
  assert.ok(at('data-region="issues"') > -1 && at('data-region="issues"') < at('data-region="saves"') && at('data-region="saves"') < at('Model usage'));
});

test('issuesHTML empty state explains what is logged', async () => {
  const { issuesHTML, issuesHead } = await load();
  assert.match(issuesHTML([], { saves: [], slotFree: true, pending: new Set() }), /No issues recorded in this instance/);
  assert.equal(issuesHead([]), 'Issues');
});

test('issuesHTML rows use words, counts and escape titles', async () => {
  const { issuesHTML, issuesHead } = await load();
  const html = issuesHTML(ISSUES, issueOpts());
  assert.ok(!html.includes('<img src=x>') && html.includes('&lt;img'));
  for (const text of ['Unsupported action', '3 times in this instance', '7 in all instances', 'Open', '/data/issues/unsupported_action__0123.json']) assert.ok(html.includes(text), text);
  assert.equal(issuesHead(ISSUES), 'Issues · 1');
  assert.ok(!issuesHTML([{ ...ISSUES[0], occurrences_total: 3 }], issueOpts()).includes('in all instances'));
});

test('issuesHTML hit save states', async () => {
  const { issuesHTML } = await load();
  const html = issuesHTML(ISSUES, issueOpts());
  assert.equal(html.split('data-action="load-save"').length - 1, 1);
  assert.ok(html.includes('data-id="s1"') && html.includes('data-key="issue-hit-issue_a-1000"'));
  for (const text of ['Save no longer kept', 'No save before this hit', 'Example kept', 'Counted only']) assert.ok(html.includes(text), text);
});

test('issuesHTML disables load while the slot is occupied', async () => {
  const { issuesHTML } = await load();
  const html = issuesHTML(ISSUES, issueOpts({ slotFree: false }));
  assert.equal(html.split('data-action="load-save"').length - 1, html.split('aria-disabled="true"').length - 1);
  assert.ok(html.includes('aria-disabled="true"') && html.includes('Stop the running instance before loading a save.'));
});

test('issuesHTML marks a pending load busy', async () => {
  const { issuesHTML } = await load();
  assert.ok(issuesHTML(ISSUES, issueOpts({ pending: new Set(['load-save:s1']) })).includes('aria-busy="true"'));
});

test('issuesHTML shows the hit and truncation notes', async () => {
  const { issuesHTML } = await load();
  const many = [{ ...ISSUES[0], hit_count: 25, hits: Array.from({ length: 20 }, (_, i) => ({ at: 1000 - i, example_id: `e${i}`, source: 'agent_signal', kept: false, save_before: null })) }];
  assert.ok(issuesHTML(many, issueOpts()).includes('Showing the newest 20 of 25 hits'));
  assert.ok(issuesHTML(ISSUES, issueOpts({ truncated: true })).includes('Only the newest 500 issue events were read.'));
});

test('saveToast says what was actually saved', async () => {
  const { saveToast } = await load();
  assert.equal(saveToast({ game: { status: 'confirmed' } }), 'Saved.');
  assert.match(saveToast({ game: { status: 'on_disk' } }), /last game save on disk/);
  assert.match(saveToast({ game: { status: 'none' } }), /No game save was found/);
});

test('issuesHTML keeps an opened hits list open across renders', async () => {
  const { issuesHTML } = await load();
  assert.ok(!issuesHTML(ISSUES, issueOpts()).includes('<details class="issue-hits" open'));
  assert.ok(issuesHTML(ISSUES, issueOpts({ issueOpen: new Set(['issue_a']) })).includes('<details class="issue-hits" open data-issue="issue_a">'));
});

test('issuesHTML only asks to stop the instance when a hit can load, and keys each hit uniquely', async () => {
  const { issuesHTML } = await load();
  const noSaves = [{ ...ISSUES[0], hits: ISSUES[0].hits.map(h => ({ ...h, save_before: null })) }];
  assert.ok(!issuesHTML(noSaves, issueOpts({ slotFree: false })).includes('Stop the running instance'));
  assert.ok(issuesHTML(ISSUES, issueOpts({ slotFree: false })).includes('Stop the running instance before loading a save.'));
  const counted = [{ ...ISSUES[0], hits: [
    { at: 1000, example_id: null, source: 'worker_rejected', kept: false, save_before: 's1' },
    { at: 900, example_id: null, source: 'worker_rejected', kept: false, save_before: 's1' }] }];
  const keys = [...issuesHTML(counted, issueOpts()).matchAll(/data-key="(issue-hit-[^"]+)"/g)].map(m => m[1]);
  assert.equal(keys.length, 2);
  assert.equal(new Set(keys).size, 2);
});

const loop = (extra = {}) => ({
  id: 'loop_1', mode: 'auto', phase: 'gating', issue: { issue_id: 'i1', title: 'Agent could not ui_drag on slot' },
  attempt: { n: 2, of: 3, minutes: 6, pr: { number: 41, url: 'https://github.com/waspflannel/agentic-game-testing/pull/41' } },
  minutes_today: 38, daily_minutes: 240, progress: [], needs_you: null, verify: null, ...extra,
});

test('fixing state shows only the badge', async () => {
  const { instanceState } = await load();
  for (const [phase, label, tone] of [['gating', 'Fixing', 'pending'], ['verifying', 'Fixing', 'pending'], ['needs_you', 'Needs you', 'attention']]) {
    const s = instanceState(run('Fixing', { loop: loop({ phase }) }), { reason: 'fixing' });
    assert.deepEqual([s.key, s.badge, s.left, s.stop, s.menu], ['fixing', { tone, label }, [], false, []]);
  }
});

test('loopHTML off says fixes are off', async () => {
  const { loopHTML, loopHead } = await load();
  assert.match(loopHTML(null), /Fixes are off for this instance\./);
  assert.equal(loopHead(null), 'Improvement loop');
  assert.equal(loopHead(loop()), 'Improvement loop · Auto');
  assert.equal(loopHead(loop({ mode: 'manual' })), 'Improvement loop · Manual');
});

test('loopHTML phase, attempt, PR link and escaping', async () => {
  const { loopHTML } = await load();
  const html = loopHTML(loop({ issue: { title: '<img src=x onerror=1>' } }));
  assert.match(html, /Running tests on the PR/);
  assert.match(html, /Attempt 2 of 3/);
  assert.match(html, /<a href="https:\/\/github\.com\/waspflannel\/agentic-game-testing\/pull\/41" target="_blank" rel="noreferrer">PR #41<\/a>/);
  assert.match(html, /38 of 240 fixer minutes today/);
  assert.doesNotMatch(html, /<img/);
  assert.match(html, /&lt;img/);
  const v = loopHTML(loop({ phase: 'verifying', verify: { decisions: 12, paused: true } }));
  assert.match(v, /Checking the fix: 12 of 30 decisions, paused\. Resume the instance to continue\./);
  assert.doesNotMatch(loopHTML(loop({ phase: 'verifying', verify: { decisions: 12 } })), /paused/);
});

test('loopHTML needs-you sentence and offered buttons', async () => {
  const { loopHTML, NEEDS_YOU_TEXT } = await load();
  const html = loopHTML(loop({ phase: 'needs_you', needs_you: { reason: 'verify_failed', message: 'x', offers: ['revert', 'dispatch', 'cancel'] } }));
  assert.match(html, /The issue came back after the fix\. Revert it, run the fixer again, or cancel\./);
  assert.match(html, /class="loop-needs"><svg[^>]*><use href="#i-alert"/);
  assert.match(html, /Needs you:/);
  const cmds = [...html.matchAll(/data-command="(\w+)"/g)].map(m => m[1]);
  assert.deepEqual(cmds.sort(), ['cancel', 'dispatch', 'revert']);
  assert.match(html, /class="btn btn-primary" data-action="loop-command" data-command="dispatch"/);
  assert.match(html, /class="loop-cancel btn btn-danger" data-action="loop-command" data-command="cancel"/);
  assert.match(html, /class="btn btn-secondary" data-action="loop-command" data-command="revert"/);
  const busy = loopHTML(loop({ phase: 'needs_you', needs_you: { reason: 'dispatch', offers: ['dispatch', 'cancel'] } }), { pending: new Set(['loop:dispatch']) });
  assert.match(busy, /data-command="dispatch"[^>]*aria-busy="true" aria-disabled="true"/);
  assert.doesNotMatch(busy, /data-command="cancel"[^>]*aria-busy/);
  assert.match(loopHTML(loop({ phase: 'needs_you', needs_you: { reason: 'new_reason', message: 'Fallback <b>', offers: ['cancel'] } })), /Fallback &lt;b&gt;/);
  const reasons = 'dispatch review_pr review_revert attempt_failed issue_attempts loop_attempts loop_fixes repeat_failures budget usage_limit fixer_unavailable needs_worker_change no_save teardown_unconfirmed checkout_dirty restart_needed verify_failed verify_inconclusive verify_launch_failed regression reverted revert_failed merged_untested'.split(' ');
  for (const r of reasons) assert.ok(NEEDS_YOU_TEXT[r], r);
});

test('loopHTML ended has no buttons', async () => {
  const { loopHTML } = await load();
  const html = loopHTML(loop({ phase: 'ended' }));
  assert.match(html, />Ended</);
  assert.doesNotMatch(html, /<button/);
});

test('progressHTML table and empty state', async () => {
  const { progressHTML } = await load();
  assert.match(progressHTML([]), /Progress appears after the first instance in this loop stops\./);
  const rows = [{ run_id: 'run_a', fix_before: null, quests: 2, goals_completed: 5, action_kinds: 7, stopped_by: '<b>t</b>', further: null },
    ...['more', 'same', 'less'].map((further, i) => ({ run_id: `run_${i}`, fix_before: 'fix_1a2b', quests: 3, goals_completed: 8, action_kinds: 9, stopped_by: 'user_stopped', further }))];
  const html = progressHTML(rows);
  assert.equal((html.match(/<th scope="col">/g) || []).length, 7);
  assert.match(html, /<td class="num">7<\/td>/);
  for (const w of ['More', 'Same', 'Less']) assert.match(html, new RegExp(`<td>${w}</td>`));
  assert.match(html, /<td>—<\/td><td class="num">2/);
  assert.match(html, /&lt;b&gt;t/);
});

test('threadRegions carries the loop head and body', async () => {
  const { threadRegions, instanceState } = await load();
  const r = run('Fixing', { loop: loop() });
  const regions = threadRegions(r, { status: instanceState(r), data: {}, ui: {}, pending: new Set(), tab: 'actions' });
  assert.equal(regions['loop-head'], 'Improvement loop · Auto');
  assert.match(regions.loop, /Running tests on the PR/);
});
