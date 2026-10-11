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
  configurations: [{ id: 'a', name: 'Scout', build_id: 'b', project_id: 'p', status: 'ready' }],
  default_model: 'gpt-6-astra',
  model_options: [
    { id: 'gpt-6-luna', label: 'GPT-6 Luna', provider: 'codex' }, { id: 'gpt-6-sol', label: 'GPT-6 Sol', provider: 'codex' },
    { id: 'gpt-6.1-sol', label: 'GPT-6.1 Sol', provider: 'codex' }, { id: 'gpt-6-astra', label: 'GPT-6 Astra', provider: 'codex' },
    { id: 'sonnet', label: 'Claude Sonnet', provider: 'claude' }, { id: 'opus', label: 'Claude Opus', provider: 'claude' }, { id: 'haiku', label: 'Claude Haiku', provider: 'claude' },
  ],
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
const draftWith = async goal => ({ ...(await load()).newDraft(), goal, configurationId: 'a', requestId: 'req-1' });

test('a 409 keeps goal, transport and request ID and links the owning instance', async () => {
  const { startInstance, composerRegions, composerModel } = await load();
  const { ApiError } = await core();
  const f = fakeApi({ '/api/instances': () => { throw new ApiError(409, { detail: 'One instance already owns the global slot', code: 'active_instance_conflict', owning_instances: [{ run_id: 'r-owner' }] }); } });
  const draft = await draftWith('Craft a pickaxe');
  const result = await startInstance(draft, 'p', f.api);
  assert.equal(result.kind, 'conflict');
  assert.deepEqual([draft.goal, draft.transport, draft.requestId], ['Craft a pickaxe', 'enet', 'req-1']);
  draft.result = result;
  const html = composerRegions(composerModel(data(), 'p', draft, { key: 'p|b|a|gpt-6-astra', result: ready }), draft).status;
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

test('a changed goal is not resent while ASHAD cannot say what happened to the earlier start', async () => {
  const { startInstance } = await load();
  let reachable = false;
  const f = fakeApi({
    '/api/instances/requests/': () => { if (!reachable) throw new TypeError('Failed to fetch'); return { status: 'unknown' }; },
    '/api/instances': () => { throw new TypeError('Failed to fetch'); },
  });
  const draft = await draftWith('First goal');
  assert.equal((await startInstance(draft, 'p', f.api)).kind, 'not-started');
  draft.goal = 'Changed goal';
  assert.equal((await startInstance(draft, 'p', f.api)).kind, 'not-started');
  assert.equal(draft.requestId, 'req-1', 'a failed check keeps the earlier ID');
  assert.equal(f.posts().length, 1, 'the changed goal was not sent');
  reachable = true;  // ASHAD answers that the earlier request never launched.
  await startInstance(draft, 'p', f.api);
  assert.notEqual(draft.requestId, 'req-1');
  assert.equal(f.posts().length, 2);
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
  const f = fakeApi({ '/api/instances/requests/': { status: 'unknown' }, '/api/instances': async (u, init) => (init.body.primary_goal === 'Second' ? { id: 'r2' } : Promise.reject(new TypeError('lost'))) });
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
  const draft = { ...(await (await load()).newDraft()), configurationId: 'a', transport: 'steam', goal: 'g' };
  for (const readiness of [null, { key: 'p|b|a|gpt-6-astra', loading: true }, { key: 'p|b|a|gpt-6-astra', result: ready }, { key: 'p|other|a|gpt-6-astra', result: { ...ready, steam: { ready: true } } }]) {
    const m = composerModel(stale, 'p', draft, readiness);
    assert.equal(m.steam.enabled, false);
    assert.equal(m.canStart, false);
    assert.match(m.blockers.join(' '), /No ENet fallback/);
    assert.match(composerRegions(m, draft).bar, /<option value="steam" selected disabled>Steam \(unavailable\)<\/option>/);
  }
  const fresh = composerModel(stale, 'p', draft, { key: 'p|b|a|gpt-6-astra', result: { ...ready, steam: { ready: true } } });
  assert.equal(fresh.steam.enabled, true, 'only a fresh probe for this build enables Steam');
  assert.equal(fresh.canStart, true);
});

test('disabled Start always says why', async () => {
  const { composerModel, newDraft } = await load();
  const draft = { ...newDraft(), configurationId: 'a' };
  const reasons = (d, readiness = { key: 'p|b|a|gpt-6-astra', result: ready }) => composerModel(d, 'p', draft, readiness).blockers.join(' | ');
  assert.equal(reasons(data()), '');
  assert.match(reasons(data({ ai: { status: 'not_validated' } })), /No AI provider is connected/);
  assert.match(reasons(data({ instance_slot: { available: false, owners: [{ run_id: 'r' }], blocked_reason: 'One instance already owns the global slot' } })), /already owns the global slot/);
  assert.match(reasons(data(), { key: 'p|b|a|gpt-6-astra', result: { ...ready, ready: false, environments: [{ ready: false, reason: 'Docker is not running' }] } }), /Desktop not ready: Docker is not running/);
  assert.match(reasons(data(), { key: 'p|b|a|gpt-6-astra', loading: true }), /Checking desktop readiness/);
});

test('long agent and build names stay on one composer row', async () => {
  const { composerModel, composerRegions, newDraft } = await load();
  const long = 'Agronauts DEMO - Decisions and corrected input scaling';
  const d = data({ builds: [{ id: 'b', name: long, project_id: 'p' }], configurations: [{ ...data().configurations[0], name: `${long} agent` }] });
  const draft = { ...newDraft(), configurationId: 'a' };
  const r = composerRegions(composerModel(d, 'p', draft, { key: 'p|b|a|gpt-6-astra', result: ready }), draft);
  assert.match(r.bar, /<option value="a" selected>Agronauts DEMO - Decisions and corrected input scaling agent<\/option>/, 'the chip shows the name; the model is in the summary');
  assert.match(r.bar, /<label class="chip chip-select" title="Agronauts DEMO[^"]* agent · build Agronauts DEMO[^"]*">/);
  assert.match(r.bar, /<span class="composer-summary" title="build Agronauts DEMO[^"]*· Fresh world">/);
  assert.doesNotMatch(r.status, /class="note"[^>]*id="composer-steam-note"/, 'with ENet chosen, the Steam reason is not shown as a note');
  assert.match(r.status, /<span class="sr-only" id="composer-steam-note">/, 'but stays available to assistive tech');
  const css = require('node:fs').readFileSync(require('node:path').join(__dirname, '../ashad/static/app.css'), 'utf8');
  const bar = css.match(/\.composer-bar \{[^}]*\}/)[0];
  assert.doesNotMatch(bar, /flex-wrap: wrap/, 'the bar never wraps; the summary ellipsizes instead');
  assert.match(css, /\.composer-summary \{ min-width: 0;[^}]*text-overflow: ellipsis/);
});

test('composer and empty workspace escape project names, agents and goals', async () => {
  const { composerModel, composerRegions, composerSkeleton, emptyHTML, noConfigurationsHTML, newDraft } = await load();
  const evil = '<img src=x onerror=alert(1)>';
  const d = data({ projects: [{ id: 'p', name: evil }], configurations: [{ ...data().configurations[0], name: evil }] });
  const draft = { ...newDraft(), configurationId: 'a', goal: `</textarea>${evil}` };
  const html = Object.values(composerRegions(composerModel(d, 'p', draft, null), draft)).join('') + composerSkeleton('p', draft) + noConfigurationsHTML(d.projects[0]) + emptyHTML(d);
  assert.doesNotMatch(html, /<img/);
  assert.doesNotMatch(html, /<\/textarea><img/);
});

test('the empty project state uses project configuration wording', async () => {
  const { noConfigurationsHTML, emptyHTML } = await load();
  const html = noConfigurationsHTML({ id: 'p', name: 'Agronauts' });
  assert.match(html, /This project has no project configuration yet/);
  assert.match(html, /pairs a build of <strong>Agronauts<\/strong> with the game controls gathered from it\./);
  assert.match(html, />Add project configuration</);
  assert.match(emptyHTML(data()), /Set up your first game<[\s\S]*gathers the game's controls[\s\S]*Set up a game</);
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

test('composer offers every model and marks the server default model as Default', async () => {
  const { composerModel, composerRegions, newDraft } = await load();
  const draft = { ...newDraft(), configurationId: 'a' };
  const bar = composerRegions(composerModel(data(), 'p', draft, null), draft).bar;
  assert.doesNotMatch(bar, /<select data-field="model"/);
  const options = [...bar.matchAll(/<button [^>]*role="option"[^>]*data-action="pick-model"[^>]*>.*?<\/button>/g)].map(x => x[0]);
  assert.deepEqual(options.map(o => o.match(/data-value="([^"]+)"/)[1]), data().model_options.map(m => m.id));
  const astra = options.find(o => o.includes('data-value="gpt-6-astra"'));
  assert.match(astra, /aria-selected="true"/);
  assert.match(astra, /model-default">Default</);
  assert.equal(options.filter(o => o.includes('model-default')).length, 1);
  assert.match(bar, /<span class="chip-text">GPT-6 Astra<\/span>/);
  const other = { ...draft, model: 'opus' };
  const bar2 = composerRegions(composerModel(data(), 'p', other, null), other).bar;
  assert.match(bar2, /aria-selected="true"[^>]*data-value="opus"/);
  const unknown = { ...draft, model: 'gone' };
  assert.equal(composerModel(data(), 'p', unknown, null).model, 'gpt-6-astra', 'an unknown model falls back to the server default');
});

test('changing the configuration keeps the chosen model', async () => {
  const { composerModel, draftModel } = await load();
  const d = data({ configurations: [...data().configurations, { id: 'a2', name: 'Other', build_id: 'b', project_id: 'p', status: 'ready' }] });
  const draft = { goal: '', configurationId: 'a', model: 'sonnet' };
  assert.equal(composerModel(d, 'p', draft, null).model, 'sonnet');
  draft.configurationId = 'a2';
  assert.equal(composerModel(d, 'p', draft, null).model, 'sonnet');
  assert.equal(draftModel(d, draft), 'sonnet');
});

test('retired configurations are not offered in the composer', async () => {
  const { projectConfigurations } = await load();
  const d = data({ configurations: [...data().configurations, { id: 'old', name: 'Old', build_id: 'b', project_id: 'p', status: 'retired' }] });
  assert.deepEqual(projectConfigurations(d, 'p').map(a => a.id), ['a']);
});

test('Start sends the chosen model and a changed model is a changed payload', async () => {
  const { startInstance } = await load();
  const f = fakeApi({
    '/api/instances/requests/': { status: 'unknown' },
    '/api/instances': async (u, init) => (init.body.model === 'gpt-6-luna' ? { id: 'r2' } : Promise.reject(new TypeError('lost'))),
  });
  const draft = await draftWith('Goal');
  draft.model = 'gpt-6-sol';
  await startInstance(draft, 'p', f.api);
  assert.equal(f.posts()[0].body.model, 'gpt-6-sol');
  draft.model = 'gpt-6-luna';
  assert.equal((await startInstance(draft, 'p', f.api)).kind, 'accepted');
  const [first, second] = f.posts();
  assert.equal(second.body.model, 'gpt-6-luna');
  assert.notEqual(first.body.request_id, second.body.request_id);
});

const fixMenu = html => html.slice(html.indexOf('data-key="c-fix"'), html.indexOf('composer-bar-right'));
const fixOptions = html => Object.fromEntries((fixMenu(html).match(/<button type="button" role="option"[\s\S]*?<\/button>/g) || []).map(o => [o.match(/data-value="([^"]*)"/)[1], o]));

test('fix menu shows why Manual and Auto are disabled when the fixer is not ready', async () => {
  const { composerRegions, composerModel, newDraft } = await load();
  const fixer = { ready: false, reasons: ['Put jr on PATH.', 'Sign in with gh auth login.'] };
  const bar = draft => composerRegions(composerModel(data(), 'p', draft, { key: 'p|b|a|gpt-6-astra', result: ready }, fixer), draft).bar;
  const draft = { ...newDraft(), configurationId: 'a' };
  const html = bar(draft);
  assert.doesNotMatch(html, /data-field="fix" data-key/);
  assert.ok(html.indexOf('data-key="c-transport"') < html.indexOf('data-key="c-fix"'));
  assert.match(html, /<span class="chip-text">Fixes off<\/span>/);
  assert.match(html, /data-key="c-fix" aria-describedby="c-fix-note"/);
  const rows = fixOptions(html);
  assert.deepEqual(Object.keys(rows), ['off', 'manual', 'auto']);
  assert.doesNotMatch(rows.off, /aria-disabled/);
  for (const id of ['manual', 'auto']) {
    assert.match(rows[id], /aria-disabled="true"/);
    assert.match(rows[id], /<span class="model-option-note">Put jr on PATH\.<\/span>/);
  }
  // All reasons are visible text in the menu footer that the trigger points at, never sr-only.
  const footer = html.match(/<div class="menu-footer" id="c-fix-note">([\s\S]*?)<\/div>/)[1];
  assert.match(footer, /<p>Put jr on PATH\.<\/p><p>Sign in with gh auth login\.<\/p>/);
  assert.doesNotMatch(html, /sr-only" id=/);
  assert.match(rows.off, /aria-selected="true"[\s\S]*#i-check/);
  assert.match(bar({ ...draft, fix: 'manual' }), /<span class="chip-text">Fixes: manual<\/span>/);
  const waiting = composerRegions(composerModel(data(), 'p', draft, null), draft).bar;
  assert.match(waiting, /<p>Checking the fixer…<\/p>/);
  assert.match(fixOptions(waiting).auto, /aria-disabled="true"[\s\S]*Checking the fixer…/);
});

test('fix menu enables Manual and Auto when the fixer is ready, and Auto is selectable', async () => {
  const { composerRegions, composerModel, newDraft, startInstance } = await load();
  const draft = { ...newDraft(), configurationId: 'a' };
  const bar = d => composerRegions(composerModel(data(), 'p', d, null, { ready: true, reasons: [] }), d).bar;
  const rows = fixOptions(bar(draft));
  assert.doesNotMatch(fixMenu(bar(draft)), /menu-footer|aria-disabled="true"/);
  assert.match(rows.manual, /You merge each fix PR/);
  assert.match(rows.auto, /ASHAD merges, checks and reverts/);
  for (const id of ['off', 'manual', 'auto']) assert.match(rows[id], /data-action="pick-fix" data-field="fix"/);
  const auto = { ...draft, fix: 'auto' };
  const autoRows = fixOptions(bar(auto));
  assert.match(autoRows.auto, /aria-selected="true"[\s\S]*#i-check/);
  assert.doesNotMatch(autoRows.off, /#i-check/);
  assert.match(bar(auto), /<span class="chip-text">Fixes: auto<\/span>/);
  const f = fakeApi({ '/api/instances': { id: 'r1' } });
  await startInstance({ ...(await draftWith('Craft')), fix: 'auto' }, 'p', f.api);
  assert.equal(f.posts()[0].body.fix_mode, 'auto');
});

test('startInstance sends fix_mode', async () => {
  const { startInstance } = await load();
  const f = fakeApi({ '/api/instances': { id: 'r1' } });
  await startInstance(await draftWith('Craft'), 'p', f.api);
  assert.equal(f.posts()[0].body.fix_mode, 'off');
  const draft = await draftWith('Craft');
  draft.fix = 'auto';
  await startInstance(draft, 'p', f.api);
  assert.equal(f.posts()[1].body.fix_mode, 'auto');
});
