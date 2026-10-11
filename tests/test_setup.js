'use strict';
// Setup flow (ashad/static/views/setup.js) and the shared request-ID helper (core.js submitOnce).
const { test } = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');
const { pathToFileURL } = require('node:url');
const url = file => pathToFileURL(path.join(__dirname, '../ashad/static', file)).href;
const load = () => import(url('views/setup.js'));
const core = () => import(url('core.js'));

const data = (extra = {}) => ({
  projects: [{ id: 'p', name: 'Agronauts' }],
  builds: [{ id: 'b', name: '0.3.1', project_id: 'p' }],
  configurations: [], model_options: [{ id: 'gpt-6-astra', label: 'GPT-6 Astra' }],
  ai: { status: 'ready', model: 'gpt-6-astra' }, ai_claude: { status: 'ready' }, ...extra,
});
const readyProbe = { buildId: 'b', loading: false, at: 0, result: { provider: { ready: true }, build_validation: { ready: true }, environments: [{ id: 'w', ready: true }], slot: { available: true }, steam: { ready: false } } };
const noWait = () => Promise.resolve();

function fakeApi(handler) {
  const calls = [];
  const api = async (u, init = {}) => { calls.push({ url: u, method: init.method || 'GET', body: init.body }); return handler(u, init, calls); };
  return { api, calls, posts: () => calls.filter(c => c.method === 'POST') };
}

test('re-entry jumps to the first incomplete step, and never past it', async () => {
  const { newSetup, setupModel, applyDefaults, STEPS } = await load();
  assert.equal(STEPS.length, 4);
  assert.equal(STEPS[3], 'Get controls');
  assert.equal(setupModel(data(), newSetup('')).step, 1, 'no project yet');
  assert.equal(setupModel(data({ builds: [] }), newSetup('p')).step, 2, 'project without a build');
  const s = Object.assign(newSetup('p'), { buildId: 'b' });
  assert.equal(setupModel(data(), s).step, 3, 'readiness is re-probed on entry, so the desktop step comes first');
  s.readiness = readyProbe;
  applyDefaults(data(), s);
  assert.equal(s.agentName, 'Agronauts', 'named after the project, not the long build name');
  assert.equal(setupModel(data(), s).step, 4, 'four steps; step 4 stays until the controls exist');
  assert.equal(setupModel(data({ ai_claude: { status: 'configuration_required' } }), s).blocked, 'Connect Claude to get game controls.');
  s.step = 6;
  assert.equal(setupModel(data(), s).step, 4, '?step= cannot go past the last step');
  s.agent = { id: 'a' };
  assert.equal(setupModel(data(), s).step, 4);
  assert.equal(setupModel(data(), s).blocked, '');
});

test('Get controls recovers a lost reply through tuning/<id> without a second POST', async () => {
  const { getControls } = await load();
  const { newOp } = await core();
  let polls = 0;
  const f = fakeApi(u => {
    if (u === '/api/configurations/controls') throw new TypeError('Failed to fetch');
    polls += 1;
    return polls < 3 ? { status: 'in_progress' } : { status: 'completed', configuration: { id: 'agent-1' } };
  });
  const op = newOp();
  await getControls(op, { build_id: 'b', project_id: 'p', name: 'Scout' }, f.api, noWait);
  assert.equal(op.status, 'done');
  assert.deepEqual(op.value, { id: 'agent-1' });
  assert.equal(f.posts().length, 1);
  assert.ok(f.calls.slice(1).every(c => c.url === `/api/configurations/controls/${op.requestId}`));
});

test('an unknown Get controls run after a lost reply says nothing was saved and retries the same ID', async () => {
  const { getControls } = await load();
  const { newOp } = await core();
  let lose = true;
  const f = fakeApi(u => {
    if (u === '/api/configurations/controls') { if (lose) throw new TypeError('lost'); return { id: 'agent-2' }; }
    return { status: 'unknown' };
  });
  const op = newOp();
  const payload = { build_id: 'b', project_id: 'p', name: 'Scout' };
  await getControls(op, payload, f.api, noWait);
  assert.equal(op.status, 'error');
  assert.match(op.error, /nothing was saved/);
  lose = false;
  await getControls(op, payload, f.api, noWait);
  assert.equal(op.status, 'done');
  assert.equal(new Set(f.posts().map(c => c.body.request_id)).size, 1);
});

test('one run at a time: setup says so, waits, and resends the same request', async () => {
  const { getControls, newSetup, setupModel, setupRegions } = await load();
  const { ApiError } = await core();
  let busy = 2;
  const s = Object.assign(newSetup('p'), { buildId: 'b', readiness: readyProbe, agentName: 'Scout', step: 4 });
  const seen = [];
  const f = fakeApi(() => {
    if (busy-- > 0) throw new ApiError(422, { detail: 'An agent is Game controls are already being gathered. Wait for them to finish.' });
    return { id: 'agent-3' };
  });
  await getControls(s.controls, { build_id: 'b' }, f.api, async () => { seen.push(setupRegions(data(), s, setupModel(data(), s)).main); });
  assert.equal(s.controls.status, 'done');
  assert.match(seen[0], /Another Get game controls run is in progress/);
  assert.match(seen[0], /Stop waiting/);
  assert.equal(new Set(f.posts().map(c => c.body.request_id)).size, 1, 'waiting never mints a new request ID');

  const stopped = Object.assign(newSetup('p'), {});
  const always = fakeApi(() => { throw new ApiError(422, { detail: 'An agent is already being gathered.' }); });
  await getControls(stopped.controls, { build_id: 'b' }, always.api, async () => { stopped.controls.stopWaiting = true; });
  assert.equal(stopped.controls.waiting, false);
  assert.match(stopped.controls.error, /Stopped waiting/);
});

test('closing setup restores draft protection: only unsent input is dirty', async () => {
  const { newSetup, setupDirty, discardSetup } = await load();
  const s = newSetup('');
  assert.equal(setupDirty(s), false);
  s.name = 'Agronauts';
  assert.equal(setupDirty(s), true, 'a typed, uncreated project name');
  s.project.status = 'busy';
  assert.equal(setupDirty(s), false, 'submitted input is no longer a draft');
  s.project.status = 'lost';
  assert.equal(setupDirty(s), true);
  discardSetup(s);
  assert.equal(setupDirty(s), false, 'discarding on leave clears the desktop dirty flag');
  const t = Object.assign(newSetup('p'), { agentName: 'Scout', edited: true });
  assert.equal(setupDirty(t), true, 'edited step 4 fields before tuning');
  t.controls.status = 'busy';
  assert.equal(setupDirty(t), false);
  Object.assign(t.controls, { status: 'done' });
  t.agent = { id: 'a' };
  assert.equal(setupDirty(t), false);
});

test('a lost reply locks the payload; a refusal frees the ID for corrected input', async () => {
  const { submitOnce, newOp, ApiError } = await core();
  const op = newOp();
  const sent = [];
  let mode = 'lost';
  const send = payload => async id => {
    sent.push([id, payload.name]);
    if (mode === 'lost') throw new TypeError('lost');
    if (mode === 'refuse') throw new ApiError(422, { detail: 'Bad name' });
    return { id: 'p1' };
  };
  await submitOnce(op, { name: 'A' }, send({ name: 'A' }));
  assert.equal(op.status, 'lost');
  await submitOnce(op, { name: 'B' }, send({ name: 'B' }));
  assert.equal(sent.length, 1, 'a changed payload is not sent under an ID whose fate is unknown');
  mode = 'refuse';
  await submitOnce(op, { name: 'A' }, send({ name: 'A' }));
  assert.equal(op.status, 'error');
  mode = 'ok';
  await submitOnce(op, { name: 'C' }, send({ name: 'C' }));
  assert.equal(op.status, 'done');
  assert.deepEqual(sent.map(s => s[0]), Array(3).fill(op.requestId));
});

test('Get controls shows real progress: batches, workers, step count and a stall warning', async () => {
  const { controlsProgressHTML, STALL_SECONDS } = await load();
  const { newOp } = await core();
  const op = Object.assign(newOp(), { status: 'busy', startedAt: 1000 });
  assert.match(controlsProgressHTML(op, 1010), /<progress class="progress" aria-label="Get controls in progress"><\/progress><p>Starting…/, 'no progress yet: indeterminate');
  op.progress = { stage: 'exploring', batch_count: 8, batches_done: 5, workers: 4, started_at: 1000, model_calls: 7, rejected_replies: 2, last_call_at: 1070 };
  let html = controlsProgressHTML(op, 1100);
  assert.match(html, /<progress class="progress" max="9" value="5" aria-label="Get controls progress, step 6 of 9">/);
  assert.match(html, /Reading source: 5 of 8 batches \(4 workers in parallel\) · <time data-elapsed="1000">1m 40s<\/time> elapsed/);
  assert.match(html, /7 model replies \(2 rejected and retried\) · last reply <time data-elapsed="1070">0m 30s<\/time> ago/);
  assert.doesNotMatch(html, /No model reply/);
  html = controlsProgressHTML(op, 1070 + STALL_SECONDS + 60);
  assert.match(html, /No model reply for 4 minutes[\s\S]*Getting game controls is still running\./, 'a long silence is called out');
  op.progress = { stage: 'writing', batch_count: 8, batches_done: 8, workers: 4, started_at: 1000 };
  assert.match(controlsProgressHTML(op, 1100), /max="9" value="8"[\s\S]*Writing the game controls from 8 batches of notes/);
  op.progress = { stage: 'writing', batch_count: 0, batches_done: 0, workers: 0, started_at: 1000, model_calls: 0, rejected_replies: 0, last_call_at: null };
  assert.match(controlsProgressHTML(op, 1030), /max="1" value="0"[\s\S]*Writing the game controls · [\s\S]*0 model replies · started <time data-elapsed="1000">0m 30s<\/time> ago/);
});

test('setup markup escapes names and build labels, including the done view', async () => {
  const { newSetup, setupModel, setupRegions, setupSkeleton, readinessHTML } = await load();
  const evil = '<img src=x onerror=alert(1)>';
  const d = data({ projects: [{ id: 'p', name: evil }], builds: [{ id: 'b', name: evil, project_id: 'p', source_revision: evil }] });
  const s = Object.assign(newSetup('p'), { buildId: 'b', agentName: evil, agent: { id: 'a', name: evil, build_id: evil, created_at: 1 } });
  let html = '';
  for (const step of [1, 2, 3, 4]) {
    s.step = step;
    html += setupSkeleton(d, s, step) + Object.values(setupRegions(d, s, { ...setupModel(d, s), step, first: 4 })).join('');
  }
  html += readinessHTML({ result: { provider: { message: evil }, build_validation: { reason: evil }, environments: [{ name: evil, reason: evil }], slot: { available: false, blocked_reason: evil }, steam: { reason: evil } } }, 0);
  assert.doesNotMatch(html, /<img/);
});

test('the readiness provider row names the connected providers, not a model', async () => {
  const { readinessHTML } = await load();
  const detail = provider => readinessHTML({ result: { provider } }, 0).match(/Provider and model<\/span><span class="check-detail">([^<]*)</)[1];
  assert.equal(detail({ ready: true, model: 'sonnet', providers: ['claude'] }), 'Claude set');
  assert.equal(detail({ ready: true, model: 'gpt-6-luna', providers: ['codex'] }), 'Codex set');
  assert.equal(detail({ ready: true, providers: ['claude', 'codex'] }), 'Claude and Codex set');
  assert.equal(detail({ ready: true, model: 'sonnet' }), 'AI provider set');
  assert.equal(detail({ ready: false, message: 'Set up AI in Settings › Account.', providers: [] }), 'Set up AI in Settings › Account.');
});

const bothProviders = (extra = {}) => data({
  model_options: [{ id: 'gpt-6-astra', label: 'GPT-6 Astra', provider: 'codex' }, { id: 'sonnet', label: 'Claude Sonnet', provider: 'claude' }],
  ai_claude: { status: 'configuration_required' }, ...extra,
});

test('step 4 has a configuration name field and no model picker', async () => {
  const { newSetup, setupSkeleton } = await load();
  const html = setupSkeleton(bothProviders(), Object.assign(newSetup('p'), { buildId: 'b', agentName: 'Scout' }), 4);
  assert.match(html, /<label for="setup-agent-name">Configuration name<\/label>/);
  assert.match(html, /id="setup-agent-name"[^>]*value="Scout"/);
  assert.match(html, /Shown when you start an instance\./);
  assert.doesNotMatch(html, /<select|setup-model|optgroup/);
});

test('step 4 is blocked with the Claude message when only Codex is ready', async () => {
  const { newSetup, setupModel, setupRegions } = await load();
  const s = Object.assign(newSetup('p'), { buildId: 'b', agentName: 'A', readiness: readyProbe });
  const d = bothProviders({ ai: { status: 'ready', model: 'gpt-6-astra' } });
  const m = setupModel(d, s);
  assert.equal(m.signedIn, false);
  assert.equal(m.step, 4);
  const { main, nav } = setupRegions(d, s, m);
  assert.match(nav, /data-action="setup-controls"[^>]*aria-disabled="true"/);
  assert.match(main, /uses Claude Sonnet/);
  assert.match(main, /Instances can still use Codex/);
  const ready = bothProviders({ ai_claude: { status: 'ready' } });
  const on = setupRegions(ready, s, setupModel(ready, s));
  assert.doesNotMatch(on.nav, /aria-disabled/);
  assert.match(on.nav, />Get game controls</);
});
test('step 4 keeps a running or finished Get game controls visible when Claude stops reporting ready', async () => {
  const { newSetup, setupModel, setupRegions } = await load();
  const d = bothProviders({ ai: { status: 'ready', model: 'gpt-6-astra' } });
  const busy = Object.assign(newSetup('p'), { buildId: 'b', agentName: 'A', readiness: readyProbe, step: 4 });
  Object.assign(busy.controls, { status: 'busy', progress: { stage: 'exploring', batch_count: 8, batches_done: 5, workers: 4, started_at: 1, model_calls: 5 } });
  const running = setupRegions(d, busy, setupModel(d, busy), 2);
  assert.match(running.main, /5 of 8 batches/);
  assert.doesNotMatch(running.main, /uses Claude Sonnet\. <a/);
  const done = Object.assign(newSetup('p'), { buildId: 'b', agentName: 'A', readiness: readyProbe, step: 4, agent: { id: 'a1', name: 'A', build_id: 'b' } });
  const finished = setupRegions(d, done, setupModel(d, done));
  assert.match(finished.main, /data-action="ov-controls" data-id="a1"/);
  assert.match(finished.nav, /Start an instance/);
});


test('step 1 lists existing projects as links without a delete button', async () => {
  const { newSetup, setupModel, setupRegions } = await load();
  const d = data({
    projects: [{ id: 'p', name: 'Agronauts' }, { id: 'q', name: 'Other' }],
    runs: [{ id: 'r1', project_id: 'q', state: 'Running', players: {} }],
    instance_slot: { owners: [{ run_id: 'r1', state: 'Running', reason: 'running' }] },
  });
  const s = newSetup('');
  const { main } = setupRegions(d, s, { ...setupModel(d, s), step: 1 });
  assert.ok(main.includes('href="#/setup?project=p"'));
  assert.ok(main.includes('href="#/setup?project=q"'));
  assert.doesNotMatch(main, /<button[^>]*data-(action="setup-delete|id="(p|q)")/);
});
