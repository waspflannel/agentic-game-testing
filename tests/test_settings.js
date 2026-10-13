'use strict';
// Settings (ashad/static/views/settings.js) and Usage (views/usage.js).
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { pathToFileURL } = require('node:url');
const url = file => pathToFileURL(path.join(__dirname, '../ashad/static', file)).href;
const settings = () => import(url('views/settings.js'));
const usage = () => import(url('views/usage.js'));
const NOW = 2_000_000_000;
const data = (extra = {}) => ({
  workers: [{ id: 'w1', name: 'Lab PC', url: 'http://10.0.0.2:8101', credential_ref: 'worker:w1' }, { id: 'w2', name: 'Spare', url: 'http://10.0.0.3:8101' }],
  accounts: [{ id: 'a1', name: 'Main', worker_id: 'w1', steam_id: '7656', app_id: 3753020 }],
  builds: [{ id: 'b', name: '0.3.1', steam_app_id: 3753020 }],
  leases: [], runs: [], ...extra,
});

test('ChatGPT sign-in offers only the official HTTPS link, and the link clears after sign-in', async () => {
  const { officialLoginUrl, accountRegions, settleLogin } = await settings();
  const good = 'https://auth.openai.com/oauth/authorize?state=abc';
  assert.equal(officialLoginUrl(good), good);
  for (const bad of ['http://auth.openai.com/x', 'https://auth.openai.com.evil.example/x', 'https://user:pw@chatgpt.com/', 'https://chatgpt.com:8443/', 'javascript:alert(1)', 'not a url'])
    assert.equal(officialLoginUrl(bad), null, bad);
  const pending = accountRegions({ ai: { status: 'login_pending' }, login: { auth_url: good } }).actions;
  assert.match(pending, /href="https:\/\/auth\.openai\.com\/oauth\/authorize\?state=abc" target="_blank" rel="noopener noreferrer"/);
  assert.match(pending, /Cancel sign-in/);
  const spoofed = accountRegions({ ai: { status: 'login_pending' }, login: { auth_url: 'https://evil.example/login' } }).actions;
  assert.doesNotMatch(spoofed, /evil\.example/);
  assert.match(spoofed, /not an official ChatGPT page/);
  const login = { auth_url: good };
  assert.equal(settleLogin(login, { status: 'login_pending' }), login);
  assert.equal(settleLogin(login, { status: 'ready' }), null, 'signed in: the link is dropped');
  assert.equal(settleLogin(login, { status: 'configuration_required' }), null);
  const ready = Object.values(accountRegions({ ai: { status: 'ready' }, login: null })).join('');
  assert.doesNotMatch(ready, /auth\.openai/);
  assert.match(ready, /Sign out/);
});

test('sign-in buttons are disabled with the reason while Get game controls or an instance is active', async () => {
  const { accountBlocker, accountRegions } = await settings();
  assert.equal(accountBlocker(data(), false), '');
  assert.match(accountBlocker(data(), true), /Wait for Get game controls to finish before changing sign-in\./);
  assert.match(accountBlocker(data({ runs: [{ state: 'Running' }] }), false), /active instance/);
  assert.match(accountBlocker(data({ runs: [{ state: 'Paused', goal_recalculation: { status: 'planning' } }] }), false), /recalculation/);
  const html = accountRegions({ ai: { status: 'configuration_required' }, blocker: 'Pause or stop the active instance before changing sign-in.' }).actions;
  assert.match(html, /data-action="account-login"[^>]*aria-disabled="true"/);
  assert.match(html, /Pause or stop the active instance/);
});

test('the worker token is write-only', async () => {
  const { desktopsSkeleton, workerPayload } = await settings();
  const edit = desktopsSkeleton(data({ workers: [{ ...data().workers[0], token: 'leaked-secret' }] }), 'w1');
  assert.doesNotMatch(edit, /leaked-secret/);
  assert.match(edit, /<input id="w-token"[^>]*type="password"[^>]*placeholder="Stored\. Leave blank to keep"/);
  assert.doesNotMatch(edit.match(/<input id="w-token"[^>]*>/)[0], /value=/, 'the token field is never prefilled');
  assert.equal('token' in workerPayload({ url: 'http://10.0.0.2:8101', token: '  ', enabled: true }, 'w1').body, false, 'blank keeps the stored token');
  assert.equal(workerPayload({ url: 'http://10.0.0.2:8101', token: 't', enabled: true }, 'w1').body.token, 't');
  assert.match(workerPayload({ url: 'http://10.0.0.9:8101', enabled: true }).errors.token, /needs its service token/);
  assert.ok(workerPayload({ url: 'ftp://x', token: 't' }).errors.url);
});

test('leased resources show as disabled with the reason', async () => {
  const { desktopsRegions, steamRegions } = await settings();
  const d = data({ leases: [{ kind: 'worker', resource_id: 'w1', run_id: 'run_9' }] });
  const ui = { probing: new Set(), errors: new Map(), editId: 'w1' };
  const desk = desktopsRegions(d, ui, NOW);
  assert.match(desk.list, /data-action="worker-delete" data-id="w1"[^>]*aria-disabled="true" title="In use by instance run_9/);
  assert.doesNotMatch(desk.list, /data-action="worker-delete" data-id="w2"[^>]*aria-disabled/);
  assert.match(desk['form-actions'], /data-key="w-save" aria-disabled="true"/);
  assert.match(desk['form-status'], /Editing is disabled/);
  const steam = steamRegions(d, { busy: new Map(), results: new Map() }, NOW).list;
  for (const action of ['start', 'finish', 'verify', 'delete']) assert.match(steam, new RegExp(`data-action="steam-${action}"[^>]*aria-disabled="true"`), action);
  assert.match(steam, /In use by instance run_9; available after it ends/);
});

test('Steam rejects App ID 480 inline, and Verify needs a build with the same App ID', async () => {
  const { steamErrors, steamRegions } = await settings();
  assert.match(steamErrors({ steam_id: '7656', app_id: '480' }).app_id, /480/);
  assert.deepEqual(steamErrors({ steam_id: '7656', app_id: '3753020' }), {});
  assert.ok(steamErrors({ steam_id: '76x', app_id: '1' }).steam_id);
  const html = steamRegions(data({ builds: [] }), { busy: new Map(), results: new Map() }, NOW).list;
  assert.match(html, /data-action="steam-verify"[^>]*aria-disabled="true" title="No registered build has App ID 3753020/);
});

test('the settings nav has no Maintenance section', async () => {
  const { SECTIONS, settingsSkeleton } = await settings();
  assert.deepEqual(SECTIONS.map(([id]) => id), ['account', 'desktops', 'steam', 'limits']);
  const links = [...settingsSkeleton('account', '').matchAll(/class="row settings-link[^"]*" href="#\/settings\/(\w+)"/g)].map(m => m[1]);
  assert.deepEqual(links, ['account', 'desktops', 'steam', 'limits']);
});

test('usage totals count cached and reasoning tokens once and never invent a price', async () => {
  const { usageModel, newUsageUi } = await usage();
  const rows = [
    { timestamp: NOW - 60, model: 'm1', purpose: 'player', usage: { input_tokens: 1000, cached_input_tokens: 800, output_tokens: 50, reasoning_output_tokens: 30, total_tokens: 1050 }, estimated_cost_usd: null },
    { timestamp: NOW - 120, model: 'm2', purpose: 'tuning', usage: { input_tokens: 10, output_tokens: 5 }, estimated_cost_usd: null },
    { timestamp: NOW - 180, model: 'm1', purpose: 'player', usage: null, estimated_cost_usd: null },
    { timestamp: NOW - 40 * 86400, model: 'm1', purpose: 'player', usage: { total_tokens: 99 }, estimated_cost_usd: 0.5 },
  ];
  const week = usageModel(rows, newUsageUi(), NOW);
  assert.deepEqual([week.turns, week.tokens, week.uncounted, week.estimate], [3, 1065, 1, null], 'subscription rows: estimate unavailable, not $0');
  const all = usageModel(rows, { ...newUsageUi(), range: 'all' }, NOW);
  assert.deepEqual([all.turns, all.tokens, all.estimate], [4, 1164, 0.5]);
  assert.equal(usageModel(rows, { ...newUsageUi(), range: 'all', model: 'm2' }, NOW).turns, 1);
  assert.deepEqual(usageModel(rows, { ...newUsageUi(), sort: 'tokens', dir: 'asc' }, NOW).rows.map(r => r.purpose), ['player', 'tuning', 'player']);
  const many = Array.from({ length: 450 }, (_, i) => ({ timestamp: NOW - i, usage: { total_tokens: 1 } }));
  const capped = usageModel(many, newUsageUi(), NOW);
  assert.deepEqual([capped.rows.length, capped.remaining], [200, 250], 'rendering is capped at 200 rows');
});

test('an unloaded usage page reports unavailable rather than zeros', async () => {
  const { usageRegions, newUsageUi } = await usage();
  const failed = usageRegions({ error: 'Lost connection', loading: false }, newUsageUi(), new Map(), NOW);
  assert.match(failed.table, /Usage is unavailable: Lost connection/);
  assert.doesNotMatch(failed.summary, />0</);
  assert.match(failed.summary, /—/);
  assert.match(usageRegions(null, newUsageUi(), new Map(), NOW).table, /Loading usage/);
  const loaded = usageRegions({ rows: [], at: NOW }, newUsageUi(), new Map(), NOW);
  assert.match(loaded.summary, /Estimate, not a charge/);
});

test('usage and readiness are never fetched by the bootstrap poll', () => {
  const app = fs.readFileSync(path.join(__dirname, '../ashad/static/app.js'), 'utf8');
  const poll = app.slice(app.indexOf('async function poll()'), app.indexOf('function schedule()'));
  assert.ok(poll.length > 50);
  assert.doesNotMatch(poll, /usage|readiness|loadUsage|probeReadiness/i);
  assert.equal(app.match(/api\('\/api\/usage'\)/g).length, 1, 'one fetch site, in loadUsage');
  assert.deepEqual([...app.matchAll(/(?<!function )\bloadUsage\(\)/g)].length, 2, 'called on view open and from Refresh only');
});

test('settings and usage markup escape every server string', async () => {
  const s = await settings();
  const u = await usage();
  const evil = '<img src=x onerror=alert(1)>';
  const d = data({ workers: [{ id: evil, name: evil, url: evil, last_probe: { status: 'unavailable', message: evil }, desktop_url: evil }], accounts: [{ id: evil, name: evil, worker_id: evil, steam_id: evil, app_id: evil }], builds: [{ id: evil, name: evil, steam_app_id: evil }], projects: [{ id: evil, name: evil }] });
  const ui = { probing: new Set(), errors: new Map([[evil, evil]]), busy: new Map(), results: new Map([[evil, { ok: false, text: evil }]]), formError: evil, saved: evil };
  const html = [s.accountRegions({ ai: { status: evil, message: evil, plan_type: evil }, error: evil, login: { auth_url: evil } }), s.desktopsRegions(d, ui, NOW), s.steamRegions(d, ui, NOW), s.limitsRegions(d, ui)]
    .flatMap(Object.values).join('') + s.desktopsSkeleton(d, evil) + s.steamSkeleton(d)
    + Object.values(u.usageRegions({ rows: [{ timestamp: NOW, model: evil, purpose: evil, run_id: evil, status: evil }] }, { ...u.newUsageUi(), range: 'all' }, new Map([[evil, evil]]), NOW)).join('');
  assert.doesNotMatch(html, /<img/);
});

const claudeCard = (claude, extra = {}) => settings().then(m => m.accountRegions({ ai: { status: 'ready' }, claude, ...extra }));

test('the Claude token field is write-only and never echoes a value', async () => {
  const r = await claudeCard({ status: 'ready', message: 'sk-ant-oat01-SECRET', configured: true });
  const input = r['claude-actions'].match(/<input[^>]*name="claude-token"[^>]*>/)[0];
  assert.match(input, /type="password"/);
  assert.match(input, /autocomplete="off"/);
  assert.doesNotMatch(input, /value=/);
  assert.doesNotMatch(r['claude-actions'], /SECRET/);
  const evil = await claudeCard({ status: 'ready', message: '<img src=x onerror=alert(1)>' });
  assert.doesNotMatch(evil['claude-status'], /<img/);
});

test('the Claude card shows the terms note and status label', async () => {
  const { accountSkeleton } = await settings();
  assert.match(accountSkeleton(), /data-region="claude-status"[\s\S]*data-region="claude-actions"/);
  for (const [status, label] of [['configuration_required', 'Not signed in'], ['ready', 'Signed in'], ['invalid_authentication', 'Sign-in expired']]) {
    const r = await claudeCard({ status, message: 'Server says hi' });
    assert.match(r['claude-status'], new RegExp(label));
    assert.match(r['claude-status'], /Billing<\/dt><dd>Claude subscription/);
    assert.match(r['claude-status'], /Server says hi/);
    assert.match(r['claude-actions'], /unmodified Claude CLI\. See Anthropic's legal and compliance terms\./);
  }
});

test('Claude token buttons are disabled with the blocker reason', async () => {
  const blocked = (await claudeCard({ status: 'ready' }, { blocker: 'Pause or stop the active instance before changing sign-in.' }))['claude-actions'];
  for (const action of ['claude-token-save', 'claude-token-remove']) {
    const button = blocked.match(new RegExp(`<button[^>]*data-action="${action}"[^>]*>`))[0];
    assert.match(button, /aria-disabled="true"/);
    assert.match(button, /title="Pause or stop the active instance/);
  }
  const open = (await claudeCard({ status: 'ready' }))['claude-actions'];
  assert.doesNotMatch(open.match(/<button[^>]*data-action="claude-token-save"[^>]*>/)[0], /aria-disabled/);
});

test('a blank storage reserve is an error and never an error string in the payload', async () => {
  const { limitsPayload } = await settings();
  const { errors, body } = limitsPayload({ duration: '10', spend: '5', reserve: '' });
  assert.equal(errors.reserve, 'Enter a reserve, or 0 for none.');
  assert.equal(body.storage_reserve_bytes, null);
});

test('Claude token clicks reach the token action and Enter in the form saves instead of navigating', async () => {
  const app = fs.readFileSync(path.join(__dirname, '../ashad/static/app.js'), 'utf8');
  const route = new RegExp(app.match(/else if \(\/(.+?)\/\.test\(action\)\) \{\n    settingsAction/)[1]);
  const { accountRegions } = await settings();
  const buttons = accountRegions({ ai: { status: 'ready' }, claude: { status: 'ready' } })['claude-actions'];
  for (const action of ['claude-token-save', 'claude-token-remove']) {
    assert.match(buttons, new RegExp(`data-action="${action}"`));
    assert.match(action, route, `${action} is routed to settingsAction`);
  }
  assert.match(app, /matches\('\[data-form="claude-token"\]'\)\) \{\n\s+event\.preventDefault\(\);[^}]*settingsAction\('claude-token-save', event\.target\.querySelector\('\[data-action="claude-token-save"\]'\)\)/);
  const gate = app.slice(app.indexOf('async function settingsAction('), app.indexOf("return claudeTokenAction(action.slice(13))"));
  assert.match(gate, /aria-disabled'\) === 'true'\) return;/, 'Enter honours the disabled Save button');
});

test('limits form and payload have no fixer field', async () => {
  const { limitsSkeleton, limitsPayload } = await settings();
  assert.doesNotMatch(limitsSkeleton({}), /l-fixer|Fixer/);
  assert.equal('fixer_daily_minutes' in limitsPayload({ duration: '', spend: '', reserve: '1' }).body, false);
});

test('usage filters are dropdowns with an All row', async () => {
  const { usageRegions, newUsageUi } = await usage();
  const data = { rows: [{ timestamp: NOW, model: 'm1', purpose: 'plan' }, { timestamp: NOW, model: 'm2', purpose: 'plan' }], at: NOW };
  const controls = ui => usageRegions(data, { ...newUsageUi(), range: 'all', ...ui }, new Map([['m1', 'Model One']]), NOW).controls;
  const all = controls({});
  const first = all.match(/<button [^>]*data-field="usage-model"[^>]*>.*?<\/button>/)[0];
  assert.match(first, /data-value=""/);
  assert.match(first, /<span class="dropdown-option-label">All models<\/span>/);
  assert.match(all, /data-key="u-usage-model"[^>]*>.*?<span class="chip-text">All models<\/span>/);
  assert.doesNotMatch(all, /<select/);
  assert.match(controls({ model: 'm1' }), /data-key="u-usage-model"[^>]*>.*?<span class="chip-text">Model One<\/span>/);
});
