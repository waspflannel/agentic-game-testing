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
  agents: [], model_options: [{ id: 'gpt-6-astra', label: 'GPT-6 Astra' }],
  ai: { status: 'ready', model: 'gpt-6-astra' }, ...extra,
});
const readyProbe = { buildId: 'b', loading: false, at: 0, result: { provider: { ready: true }, build_validation: { ready: true }, environments: [{ id: 'w', ready: true }], slot: { available: true }, steam: { ready: false } } };
const noWait = () => Promise.resolve();

function fakeApi(handler) {
  const calls = [];
  const api = async (u, init = {}) => { calls.push({ url: u, method: init.method || 'GET', body: init.body }); return handler(u, init, calls); };
  return { api, calls, posts: () => calls.filter(c => c.method === 'POST') };
}

test('re-entry jumps to the first incomplete step, and never past it', async () => {
  const { newSetup, setupModel, applyDefaults } = await load();
  assert.equal(setupModel(data(), newSetup('')).step, 1, 'no project yet');
  assert.equal(setupModel(data({ builds: [] }), newSetup('p')).step, 2, 'project without a build');
  const s = Object.assign(newSetup('p'), { buildId: 'b' });
  assert.equal(setupModel(data(), s).step, 3, 'readiness is re-probed on entry, so the desktop step comes first');
  s.readiness = readyProbe;
  applyDefaults(data(), s);
  assert.equal(s.agentName, 'Agronauts agent', 'named after the project, not the long build name');
  assert.equal(setupModel(data(), s).step, 5, 'step 4 is complete once signed in with a name and model');
  assert.equal(setupModel(data({ ai: { status: 'configuration_required' } }), s).first, 4, 'signed out blocks at step 4');
  s.step = 6;
  assert.equal(setupModel(data(), s).step, 5, '?step= cannot skip an incomplete step');
  s.agent = { id: 'a', instructions: 'x' };
  assert.equal(setupModel(data(), s).step, 6);
});

test('tuning recovers a lost reply through tuning/<id> without a second POST', async () => {
  const { tuneAgent } = await load();
  const { newOp } = await core();
  let polls = 0;
  const f = fakeApi(u => {
    if (u === '/api/agents/tune') throw new TypeError('Failed to fetch');
    polls += 1;
    return polls < 3 ? { status: 'in_progress' } : { status: 'completed', agent: { id: 'agent-1' } };
  });
  const op = newOp();
  await tuneAgent(op, { build_id: 'b', model: 'm', project_id: 'p', name: 'Scout' }, f.api, noWait);
  assert.equal(op.status, 'done');
  assert.deepEqual(op.value, { id: 'agent-1' });
  assert.equal(f.posts().length, 1);
  assert.ok(f.calls.slice(1).every(c => c.url === `/api/agents/tuning/${op.requestId}`));
});

test('an unknown tuning after a lost reply says nothing was saved and retries the same ID', async () => {
  const { tuneAgent } = await load();
  const { newOp } = await core();
  let lose = true;
  const f = fakeApi(u => {
    if (u === '/api/agents/tune') { if (lose) throw new TypeError('lost'); return { id: 'agent-2' }; }
    return { status: 'unknown' };
  });
  const op = newOp();
  const payload = { build_id: 'b', model: 'm', project_id: 'p', name: 'Scout' };
  await tuneAgent(op, payload, f.api, noWait);
  assert.equal(op.status, 'error');
  assert.match(op.error, /nothing was saved/);
  lose = false;
  await tuneAgent(op, payload, f.api, noWait);
  assert.equal(op.status, 'done');
  assert.equal(new Set(f.posts().map(c => c.body.request_id)).size, 1);
});

test('one tuning at a time: setup says so, waits, and resends the same request', async () => {
  const { tuneAgent, newSetup, setupModel, setupRegions } = await load();
  const { ApiError } = await core();
  let busy = 2;
  const s = Object.assign(newSetup('p'), { buildId: 'b', readiness: readyProbe, agentName: 'Scout', model: 'gpt-6-astra', step: 5 });
  const seen = [];
  const f = fakeApi(() => {
    if (busy-- > 0) throw new ApiError(422, { detail: 'An agent is already being tuned. Wait for it to finish.' });
    return { id: 'agent-3' };
  });
  await tuneAgent(s.tune, { build_id: 'b' }, f.api, async () => { seen.push(setupRegions(data(), s, setupModel(data(), s)).main); });
  assert.equal(s.tune.status, 'done');
  assert.match(seen[0], /Only one tuning runs at a time/);
  assert.match(seen[0], /Stop waiting/);
  assert.equal(new Set(f.posts().map(c => c.body.request_id)).size, 1, 'waiting never mints a new request ID');

  const stopped = Object.assign(newSetup('p'), {});
  const always = fakeApi(() => { throw new ApiError(422, { detail: 'An agent is already being tuned.' }); });
  await tuneAgent(stopped.tune, { build_id: 'b' }, always.api, async () => { stopped.tune.stopWaiting = true; });
  assert.equal(stopped.tune.waiting, false);
  assert.match(stopped.tune.error, /Stopped waiting/);
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
  t.tune.status = 'busy';
  assert.equal(setupDirty(t), false);
  Object.assign(t.tune, { status: 'done' });
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

test('tuning shows real progress: batch, step count, replies and a stall warning', async () => {
  const { tuningProgressHTML, STALL_SECONDS } = await load();
  const { newOp } = await core();
  const op = Object.assign(newOp(), { status: 'busy', startedAt: 1000 });
  assert.match(tuningProgressHTML(op, 1010), /<progress class="progress" aria-label="Tuning in progress"><\/progress><p>Starting…/, 'no progress yet: indeterminate');
  op.progress = { stage: 'source_notes', batch: 6, batch_count: 8, started_at: 1000, model_calls: 10, rejected_replies: 4, last_call_at: 1550 };
  let html = tuningProgressHTML(op, 1580);
  assert.match(html, /<progress class="progress" max="9" value="5" aria-label="Tuning progress, step 6 of 9">/);
  assert.match(html, /Writing source notes: batch 6 of 8 · <time data-elapsed="1000">9m 40s<\/time> elapsed/);
  assert.match(html, /10 model replies \(4 rejected and retried\) · last reply <time data-elapsed="1550">0m 30s<\/time> ago/);
  assert.doesNotMatch(html, /No model reply/);
  html = tuningProgressHTML(op, 1550 + STALL_SECONDS + 60);
  assert.match(html, /No model reply for 4 minutes/, 'a long silence is called out');
  op.progress = { ...op.progress, stage: 'instructions' };
  assert.match(tuningProgressHTML(op, 1600), /max="9" value="8"[\s\S]*Writing the final instructions from 8 batches of notes/);
  op.progress = { stage: 'instructions', batch: 0, batch_count: 0, started_at: 1000, model_calls: 0, rejected_replies: 0, last_call_at: null };
  assert.match(tuningProgressHTML(op, 1030), /max="1" value="0"[\s\S]*0 model replies · started <time data-elapsed="1000">0m 30s<\/time> ago/);
});

test('setup markup escapes names, build labels and instructions', async () => {
  const { newSetup, setupModel, setupRegions, setupSkeleton, readinessHTML } = await load();
  const evil = '<img src=x onerror=alert(1)>';
  const d = data({ projects: [{ id: 'p', name: evil }], builds: [{ id: 'b', name: evil, project_id: 'p', source_revision: evil }] });
  const s = Object.assign(newSetup('p'), { buildId: 'b', agentName: evil, model: 'gpt-6-astra', agent: { id: 'a', name: evil, instructions: evil } });
  let html = '';
  for (const step of [1, 2, 3, 4, 5, 6]) {
    s.step = step;
    html += setupSkeleton(d, s, step) + Object.values(setupRegions(d, s, { ...setupModel(d, s), step, first: 6 })).join('');
  }
  html += readinessHTML({ result: { provider: { message: evil }, build_validation: { reason: evil }, environments: [{ name: evil, reason: evil }], slot: { available: false, blocked_reason: evil }, steam: { reason: evil } } }, 0);
  assert.doesNotMatch(html, /<img/);
});

const bothProviders = (extra = {}) => data({
  model_options: [{ id: 'gpt-6-astra', label: 'GPT-6 Astra', provider: 'codex' }, { id: 'sonnet', label: 'Claude Sonnet', provider: 'claude' }, { id: 'opus', label: 'Claude Opus', provider: 'claude' }],
  ai_claude: { status: 'configuration_required' }, ...extra,
});

test('the model picker groups Codex and Claude models', async () => {
  const { newSetup, setupSkeleton } = await load();
  const html = setupSkeleton(bothProviders(), Object.assign(newSetup('p'), { buildId: 'b', model: 'sonnet' }), 4);
  const codex = html.indexOf('<optgroup label="ChatGPT (Codex)">');
  const claude = html.indexOf('<optgroup label="Claude">');
  assert.ok(codex >= 0 && claude > codex);
  assert.ok(html.indexOf('value="gpt-6-astra"') > codex && html.indexOf('value="gpt-6-astra"') < claude);
  assert.ok(html.indexOf('value="sonnet"') > claude && html.indexOf('value="opus"') > claude);
  assert.match(html, /value="sonnet" selected/);
});

test('a Claude model is enabled when only Claude is ready and disabled with the provider reason when it is not', async () => {
  const { newSetup, setupModel, applyDefaults } = await load();
  const s = Object.assign(newSetup('p'), { buildId: 'b', agentName: 'A', model: 'sonnet', readiness: readyProbe });
  const off = setupModel(bothProviders({ ai: { status: 'configuration_required' } }), s);
  assert.equal(off.signedIn, false);
  assert.equal(off.step, 4);
  assert.equal(off.blocked, 'Connect the provider for this model first.');
  const on = setupModel(bothProviders({ ai: { status: 'configuration_required' }, ai_claude: { status: 'ready' } }), s);
  assert.equal(on.signedIn, true);
  assert.equal(on.step, 5, 'step 4 is complete');
  const fresh = Object.assign(newSetup('p'), { buildId: 'b' });
  applyDefaults(bothProviders({ ai: { status: 'configuration_required' }, ai_claude: { status: 'ready' } }), fresh);
  assert.equal(fresh.model, 'sonnet');
});
