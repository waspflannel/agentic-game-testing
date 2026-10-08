'use strict';
// Empty workspace and composer (ashad/static/views/home.js).
const { test } = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');
const { pathToFileURL } = require('node:url');
const url = file => pathToFileURL(path.join(__dirname, '../ashad/static', file)).href;
const load = () => import(url('views/home.js'));
const core = () => import(url('core.js'));

const data = (extra = {}) => ({
  projects: [{ id: 'p', name: 'Agronauts' }],
  builds: [{ id: 'b', name: '0.3.1', project_id: 'p' }],
  agents: [{ id: 'a', name: 'Scout', build_id: 'b', project_id: 'p', model: 'gpt-6-astra', status: 'ready' }],
  model_options: [{ id: 'gpt-6-astra', label: 'GPT-6 Astra' }],
  workers: [{ id: 'w' }], accounts: [], runs: [],
  ai: { status: 'ready' }, instance_slot: { available: true, owners: [] }, ...extra,
});
const ready = { ready: true, provider: { ready: true }, build_validation: { ready: true }, environments: [{ id: 'w', ready: true }], slot: { available: true }, steam: { ready: false, reason: 'No fresh matching licensed Steam identity is proven; no App ID 480 or ENet fallback' } };

// Fake API: routes map a URL prefix to a result (or a function returning/throwing one).
function fakeApi(routes) {
  const calls = [];
  const api = async (u, init = {}) => {
    calls.push({ url: u, method: init.method || 'GET', body: init.body });
    const key = Object.keys(routes).find(k => u.startsWith(k));
    const value = routes[key];
    return typeof value === 'function' ? value(u, init) : value;
  };
  return { api, calls, posts: () => calls.filter(c => c.method === 'POST') };
}
const draftWith = async goal => ({ ...(await load()).newDraft(), goal, agentId: 'a', requestId: 'req-1' });

test('a 409 keeps goal, transport and request ID and links the owning instance', async () => {
  const { startInstance, composerRegions, composerModel } = await load();
  const { ApiError } = await core();
  const f = fakeApi({ '/api/instances': () => { throw new ApiError(409, { detail: 'One instance already owns the global slot', code: 'active_instance_conflict', owning_instances: [{ run_id: 'r-owner' }] }); } });
  const draft = await draftWith('Craft a pickaxe');
  const result = await startInstance(draft, 'p', f.api);
  assert.equal(result.kind, 'conflict');
  assert.deepEqual([draft.goal, draft.transport, draft.requestId], ['Craft a pickaxe', 'enet', 'req-1']);
  draft.result = result;
  const html = composerRegions(composerModel(data(), 'p', draft, { key: 'p|b|a', result: ready }), draft).status;
  assert.match(html, /href="#\/i\/r-owner"/);
  assert.match(html, /Your goal is kept/);
  await startInstance(draft, 'p', f.api);
  assert.deepEqual(f.posts().map(c => c.body.request_id), ['req-1', 'req-1'], 'a retry after a refusal reuses the ID');
});

test('a lost reply recovers through requests/<id> without a second POST', async () => {
  const { startInstance } = await load();
  const f = fakeApi({
    '/api/instances/requests/req-1': { status: 'accepted', request_id: 'req-1', run: { id: 'r-new', state: 'Starting' } },
    '/api/instances': () => { throw new TypeError('Failed to fetch'); },
  });
  const result = await startInstance(await draftWith('Goal'), 'p', f.api);
  assert.deepEqual(result, { kind: 'accepted', run: { id: 'r-new', state: 'Starting' } });
  assert.equal(f.posts().length, 1);
  assert.ok(f.calls.some(c => c.url === '/api/instances/requests/req-1'));
});

test('an unresolved lost reply says not started, and Retry resends the same request', async () => {
  const { startInstance } = await load();
  let status = 'unknown';
  const f = fakeApi({ '/api/instances/requests/': () => ({ status }), '/api/instances': () => { throw new TypeError('Failed to fetch'); } });
  const draft = await draftWith('Goal');
  assert.equal((await startInstance(draft, 'p', f.api)).kind, 'not-started');
  status = 'in_progress';
  assert.equal((await startInstance(draft, 'p', f.api)).kind, 'in-progress');
  assert.deepEqual(f.posts().map(c => c.body.request_id), ['req-1', 'req-1']);
});

test('a changed goal after an accepted start is not silently resubmitted', async () => {
  const { startInstance } = await load();
  let accepted = false;
  const f = fakeApi({
    '/api/instances/requests/': () => (accepted ? { status: 'accepted', run: { id: 'r-first' } } : { status: 'unknown' }),
    '/api/instances': () => { throw new TypeError('Failed to fetch'); },
  });
  const draft = await draftWith('First goal');
  assert.equal((await startInstance(draft, 'p', f.api)).kind, 'not-started');
  accepted = true;  // The first request did launch after all.
  draft.goal = 'Changed goal';
  const result = await startInstance(draft, 'p', f.api);
  assert.equal(result.kind, 'earlier-accepted');
  assert.equal(result.run.id, 'r-first');
  assert.equal(f.posts().length, 1, 'the changed goal was not sent');
  assert.notEqual(draft.requestId, 'req-1', 'the next explicit start gets its own ID');
  assert.equal(draft.goal, 'Changed goal');
});

test('a changed goal after a never-launched start goes out under a new ID', async () => {
  const { startInstance } = await load();
  const f = fakeApi({ '/api/instances/requests/': { status: 'prepared' }, '/api/instances': async (u, init) => (init.body.primary_goal === 'Second' ? { id: 'r2' } : Promise.reject(new TypeError('lost'))) });
  const draft = await draftWith('First');
  await startInstance(draft, 'p', f.api);
  draft.goal = 'Second';
  assert.equal((await startInstance(draft, 'p', f.api)).kind, 'accepted');
  const [first, second] = f.posts();
  assert.notEqual(first.body.request_id, second.body.request_id);
});

test('Steam never enables from stale account rows or App ID 480', async () => {
  const { composerModel, composerRegions } = await load();
  const stale = data({ accounts: [{ id: 'acct', readiness: 'ready', steam_id: '7656', app_id: 480, worker_id: 'w' }] });
  const draft = { ...(await (await load()).newDraft()), agentId: 'a', transport: 'steam', goal: 'g' };
  for (const readiness of [null, { key: 'p|b|a', loading: true }, { key: 'p|b|a', result: ready }, { key: 'p|other|a', result: { ...ready, steam: { ready: true } } }]) {
    const m = composerModel(stale, 'p', draft, readiness);
    assert.equal(m.steam.enabled, false);
    assert.equal(m.canStart, false);
    assert.match(m.blockers.join(' '), /No ENet fallback/);
    assert.match(composerRegions(m, draft).bar, /<option value="steam" selected disabled>Steam \(unavailable\)<\/option>/);
  }
  const fresh = composerModel(stale, 'p', draft, { key: 'p|b|a', result: { ...ready, steam: { ready: true } } });
  assert.equal(fresh.steam.enabled, true, 'only a fresh probe for this build enables Steam');
  assert.equal(fresh.canStart, true);
});

test('disabled Start always says why', async () => {
  const { composerModel, newDraft } = await load();
  const draft = { ...newDraft(), agentId: 'a' };
  const reasons = (d, readiness = { key: 'p|b|a', result: ready }) => composerModel(d, 'p', draft, readiness).blockers.join(' | ');
  assert.equal(reasons(data()), '');
  assert.match(reasons(data({ ai: { status: 'not_validated' } })), /ChatGPT is not signed in/);
  assert.match(reasons(data({ instance_slot: { available: false, owners: [{ run_id: 'r' }], blocked_reason: 'One instance already owns the global slot' } })), /already owns the global slot/);
  assert.match(reasons(data(), { key: 'p|b|a', result: { ...ready, ready: false, environments: [{ ready: false, reason: 'Docker is not running' }] } }), /Desktop not ready: Docker is not running/);
  assert.match(reasons(data(), { key: 'p|b|a', loading: true }), /Checking desktop readiness/);
});

test('long agent and build names stay on one composer row', async () => {
  const { composerModel, composerRegions, newDraft } = await load();
  const long = 'Agronauts DEMO - Decisions and corrected input scaling';
  const d = data({ builds: [{ id: 'b', name: long, project_id: 'p' }], agents: [{ ...data().agents[0], name: `${long} agent` }] });
  const draft = { ...newDraft(), agentId: 'a' };
  const r = composerRegions(composerModel(d, 'p', draft, { key: 'p|b|a', result: ready }), draft);
  assert.match(r.bar, /<option value="a" selected>Agronauts DEMO - Decisions and corrected input scaling agent<\/option>/, 'the chip shows the name; the model is in the summary');
  assert.match(r.bar, /<label class="chip chip-select" title="Agronauts DEMO[^"]* agent · GPT-6 Astra">/);
  assert.match(r.bar, /<span class="composer-summary" title="GPT-6 Astra · build Agronauts DEMO[^"]*· Fresh world">/);
  assert.doesNotMatch(r.status, /class="note"[^>]*id="composer-steam-note"/, 'with ENet chosen, the Steam reason is not shown as a note');
  assert.match(r.status, /<span class="sr-only" id="composer-steam-note">/, 'but stays available to assistive tech');
  const css = require('node:fs').readFileSync(require('node:path').join(__dirname, '../ashad/static/app.css'), 'utf8');
  const bar = css.match(/\.composer-bar \{[^}]*\}/)[0];
  assert.doesNotMatch(bar, /flex-wrap: wrap/, 'the bar never wraps; the summary ellipsizes instead');
  assert.match(css, /\.composer-summary \{ min-width: 0;[^}]*text-overflow: ellipsis/);
});

test('composer and empty workspace escape project names, agents and goals', async () => {
  const { composerModel, composerRegions, composerSkeleton, emptyHTML, noAgentsHTML, newDraft } = await load();
  const evil = '<img src=x onerror=alert(1)>';
  const d = data({ projects: [{ id: 'p', name: evil }], agents: [{ ...data().agents[0], name: evil }] });
  const draft = { ...newDraft(), agentId: 'a', goal: `</textarea>${evil}` };
  const html = Object.values(composerRegions(composerModel(d, 'p', draft, null), draft)).join('') + composerSkeleton('p', draft) + noAgentsHTML(d.projects[0]) + emptyHTML(d);
  assert.doesNotMatch(html, /<img/);
  assert.doesNotMatch(html, /<\/textarea><img/);
});

test('empty workspace lists live prerequisites with fix links', async () => {
  const { emptyHTML } = await load();
  const missing = emptyHTML(data({ ai: { status: 'not_validated' }, workers: [] }));
  assert.match(missing, /href="#\/settings\/account"/);
  assert.match(missing, /href="#\/settings\/desktops"/);
  assert.match(missing, /href="#\/setup"/);
  const ok = emptyHTML(data());
  assert.doesNotMatch(ok, /settings\/account/);
});

test('the dirty-draft bridge sends only booleans and skips repeats', async () => {
  const { dirtyBridge } = await core();
  const sent = [];
  const set = dirtyBridge(() => ({ setDirtyDraft: async value => sent.push(value) }));
  for (const value of ['typed goal', true, 1, undefined, false, false, true]) set(value);
  assert.deepEqual(sent, [false, true, false, true]);
  assert.ok(sent.every(v => typeof v === 'boolean'));
  dirtyBridge(() => undefined)(true);  // No bridge outside the desktop app.
  dirtyBridge(() => ({ setDirtyDraft: () => Promise.reject(new Error('rejected')) }))(true);
});
