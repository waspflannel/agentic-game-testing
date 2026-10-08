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

test('sign-in buttons are disabled with the reason while tuning or an instance is active', async () => {
  const { accountBlocker, accountRegions } = await settings();
  assert.equal(accountBlocker(data(), false), '');
  assert.match(accountBlocker(data(), true), /tuning/);
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

test('a changed migration grouping invalidates the reviewed preview', async () => {
  const { parseGroups, previewValid, maintenanceRegions } = await settings();
  const text = JSON.stringify([{ project_id: 'p', name: 'Agronauts', build_ids: ['b'] }]);
  const migration = { groupsText: text, preview: { signature: parseGroups(text).signature, result: { projects: { p: 'Agronauts' }, blockers: [], preview_token: 't' } } };
  const ui = { migration };
  assert.equal(previewValid(migration, text), true);
  assert.doesNotMatch(maintenanceRegions(ui)['migration-actions'], /aria-disabled/);
  migration.groupsText = text.replace('Agronauts', 'Agronauts 2');
  assert.equal(previewValid(migration, migration.groupsText), false);
  assert.match(maintenanceRegions(ui)['migration-actions'], /aria-disabled="true"[^>]*>Apply migration<\/button><span class="hint">Preview this grouping first/);
  assert.match(maintenanceRegions(ui)['migration-preview'], /changed since the preview/);
  migration.groupsText = text;
  migration.preview.result.blockers = ['Resource leases remain'];
  assert.match(maintenanceRegions(ui)['migration-actions'], /Resolve the blockers first/);
});

test('maintenance inputs must be absolute paths with every confirmation ticked', async () => {
  const { importPayload, isAbsolutePath } = await settings();
  for (const p of ['C:\\data', 'D:/x', '\\\\server\\share', '/home/x']) assert.equal(isAbsolutePath(p), true, p);
  for (const p of ['data', '.\\data', '..\\x', '']) assert.equal(isAbsolutePath(p), false, p);
  const bad = importPayload({ source_root: 'old', backup_dir: 'D:\\bk', external_build_paths: 'builds' });
  assert.deepEqual(Object.keys(bad.errors).sort(), ['confirmed', 'external_backup_confirmed', 'external_build_paths', 'source_idle_confirmed', 'source_root']);
  const ok = importPayload({ source_root: 'C:\\old\\data\\', backup_dir: 'D:\\bk', external_build_paths: 'C:\\b1\n\nC:\\b2', confirmed: true, source_idle_confirmed: true, external_backup_confirmed: true });
  assert.deepEqual(ok.errors, {});
  assert.equal(ok.body.external_inventory.codex_home, 'C:\\old\\data\\codex', 'codex home is <source_root>\\codex');
  assert.deepEqual(ok.body.external_inventory.external_build_paths, ['C:\\b1', 'C:\\b2']);
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
  const ui = { probing: new Set(), errors: new Map([[evil, evil]]), busy: new Map(), results: new Map([[evil, { ok: false, text: evil }]]), formError: evil, saved: evil, migration: { groupsText: evil, preview: null }, importError: evil };
  const html = [s.accountRegions({ ai: { status: evil, message: evil, plan_type: evil }, error: evil, login: { auth_url: evil } }), s.desktopsRegions(d, ui, NOW), s.steamRegions(d, ui, NOW), s.limitsRegions(d, ui), s.maintenanceRegions(ui)]
    .flatMap(Object.values).join('') + s.desktopsSkeleton(d, evil) + s.steamSkeleton(d) + s.maintenanceSkeleton(d, s.suggestGroups(d))
    + Object.values(u.usageRegions({ rows: [{ timestamp: NOW, model: evil, purpose: evil, run_id: evil, status: evil }] }, { ...u.newUsageUi(), range: 'all' }, new Map([[evil, evil]]), NOW)).join('');
  assert.doesNotMatch(html, /<img/);
});
