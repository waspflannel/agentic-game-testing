'use strict';
// Project overview (ashad/static/views/project.js), with the build chooser and preparation it shares with setup.
const { test } = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');
const { pathToFileURL } = require('node:url');
const url = file => pathToFileURL(path.join(__dirname, '../ashad/static', file)).href;
const project = () => import(url('views/project.js'));
const setup = () => import(url('views/setup.js'));
const core = () => import(url('core.js'));

const NOW = 2_000_000_000;
const data = (extra = {}) => ({
  projects: [{ id: 'p', name: 'Agronauts' }, { id: 'q', name: 'Other' }],
  builds: [{ id: 'b', name: '0.3.1', project_id: 'p', source_revision: 'merged:abc' }, { id: 'c', name: '0.4.0', project_id: 'p' }],
  agents: [{ id: 'a', name: 'Scout', build_id: 'b', project_id: 'p', status: 'ready', created_at: NOW - 7200 }],
  runs: Array.from({ length: 12 }, (_, i) => ({ id: `r${i}`, project_id: 'p', state: 'Ended', ending_reason: 'stopped_by_user', created_at: NOW - i * 60, started_at: NOW - i * 60, ended_at: NOW - i * 60 + 300, plan: { primary_goal: `Goal ${i}` } })),
  model_options: [{ id: 'gpt-6-astra', label: 'GPT-6 Astra' }], instance_slot: { owners: [] }, ...extra,
});

test('readiness never runs on the poll: only view open, Re-check or a new build make it due', async () => {
  const { newOverview, readinessDue, overviewModel } = await project();
  const ov = newOverview('p');
  ov.buildId = overviewModel(data(), ov, NOW).build.id;
  assert.equal(readinessDue(ov, ov.buildId), true, 'view open (no record yet)');
  ov.readiness = { buildId: ov.buildId, loading: true };
  for (let poll = 0; poll < 5; poll += 1) assert.equal(readinessDue(ov, ov.buildId), false, 'a poll re-render while probing');
  Object.assign(ov.readiness, { loading: false, result: {} });
  assert.equal(readinessDue(ov, ov.buildId), false, 'a poll re-render after the probe');
  ov.readiness = null;  // Re-check, or the next view open.
  assert.equal(readinessDue(ov, ov.buildId), true);
  assert.equal(readinessDue(newOverview('empty'), ''), false, 'nothing to probe without a build');
});

test('a cancelled chooser does nothing', async () => {
  const { registerBuild } = await setup();
  const calls = [];
  const form = { buildId: 'b', chooser: { busy: false, error: 'earlier error' } };
  const before = JSON.stringify(form);
  const result = await registerBuild(form, 'p', 'manifest', { bridge: { chooseBuild: async () => null }, api: async (...args) => calls.push(args) });
  assert.equal(result, null);
  assert.equal(calls.length, 0, 'nothing is posted');
  assert.equal(JSON.stringify(form), before, 'the form is untouched');
  const missing = { buildId: '', chooser: { busy: false, error: '' } };
  assert.equal(await registerBuild(missing, 'p', 'folder', { bridge: undefined, api: async () => assert.fail() }), null);
  assert.match(missing.chooser.error, /desktop app/);
});

test('a late chooser result changes only the form that opened it', async () => {
  const { registerBuild } = await setup();
  let pick;
  const bridge = { chooseBuild: () => new Promise(resolve => { pick = resolve; }) };
  const posted = [];
  const api = async (u, init) => { posted.push(init.body); return { id: 'new-build', project_id: init.body.project_id }; };
  const overviewForm = { buildId: 'b', chooser: { busy: false, error: '' } };
  const otherForm = { buildId: 'z', chooser: { busy: false, error: '' } };
  const pending = registerBuild(overviewForm, 'p', 'manifest', { bridge, api });
  // Meanwhile the user moves to another project's form.
  pick('C:\\Games\\Agronauts\\manifest.json');
  const build = await pending;
  assert.equal(build.id, 'new-build');
  assert.deepEqual(posted, [{ manifest_path: 'C:\\Games\\Agronauts\\manifest.json', project_id: 'p' }], 'membership comes from the opening form');
  assert.equal(overviewForm.buildId, 'new-build');
  assert.deepEqual(otherForm, { buildId: 'z', chooser: { busy: false, error: '' } });
});

test('prepare sends confirmed:true with one stable request ID across retries', async () => {
  const { prepareDesktop } = await setup();
  const { newOp, ApiError } = await core();
  const bodies = [];
  let outcome = 'lost';
  const api = async (u, init) => {
    bodies.push({ url: u, ...init.body });
    if (outcome === 'lost') throw new TypeError('Failed to fetch');
    if (outcome === 'refused') throw new ApiError(422, { detail: 'Wait for admission, tuning, sign-in and setup operations before preparation' });
    return { status: 'completed', request_id: init.body.request_id };
  };
  const op = newOp();
  await prepareDesktop(op, 'p', 'b', api);
  assert.equal(op.status, 'lost');
  outcome = 'refused';
  await prepareDesktop(op, 'p', 'b', api);
  assert.equal(op.status, 'error');
  outcome = 'ok';
  await prepareDesktop(op, 'p', 'b', api);
  assert.equal(op.status, 'done');
  assert.ok(bodies.every(b => b.url === '/api/projects/p/environments/prepare' && b.confirmed === true && b.build_id === 'b'));
  assert.deepEqual(Object.keys(bodies[0]).sort(), ['build_id', 'confirmed', 'request_id', 'url']);
  assert.equal(new Set(bodies.map(b => b.request_id)).size, 1);
  const next = newOp();
  await prepareDesktop(next, 'p', 'b', api);
  assert.notEqual(bodies.at(-1).request_id, bodies[0].request_id, 'a later preparation is a new request');
});

test('the overview lists builds with validation, agents, readiness and the last 10 instances', async () => {
  const { newOverview, overviewModel, overviewRegions } = await project();
  const ov = newOverview('p');
  ov.validation.set('b', { ready: false, reason: 'Engine SHA-256 does not match' });
  const m = overviewModel(data(), ov, NOW);
  const r = overviewRegions(m, ov, NOW);
  assert.match(r.builds, /Not validated: Engine SHA-256 does not match/);
  assert.match(r.builds, /Not checked yet/);
  assert.match(r.agents, /build 0\.3\.1 · controls gathered 2h ago/);
  assert.doesNotMatch(r.agents, /GPT-6 Astra/, 'configurations carry no model');
  assert.match(r.agents, /data-action="ov-controls" data-id="a" data-key="ov-ctl-a">View controls</);
  assert.match(r.agents, /data-action="ov-refresh" data-id="a" data-key="ov-refresh-a">Get controls again…</);
  assert.match(r.agents, /href="#\/p\/p\/new\?agent=a"/);
  assert.match(r.head, /href="#\/setup\?project=p"/);
  assert.match(r.readiness, /data-field="ov-build"/, 'a build picker when there are several');
  assert.match(r.readiness, /Checking this computer/);
  assert.equal(m.recent.length, 10);
  assert.match(r.recent, /View all 12 in sidebar/);
  assert.match(r.recent, /Ended · stopped by user · 5m · now/);
});

test('the overview head offers Delete project, disabled while one of its instances is live', async () => {
  const { newOverview, overviewModel, overviewRegions } = await project();
  const head = d => { const ov = newOverview('p'); return overviewRegions(overviewModel(d, ov, NOW), ov, NOW).head; };
  const idle = head(data());
  assert.match(idle, /data-action="ov-delete-project"/);
  assert.doesNotMatch(idle, /aria-disabled/);
  const live = head(data({ runs: [{ id: 'r1', project_id: 'p', state: 'Running' }], instance_slot: { owners: [{ run_id: 'r1' }] } }));
  assert.ok(live.includes('aria-disabled="true" title="Stop its active instance before deleting this project"'));
});

test('each configuration row has a Delete action, disabled during a refresh or while an instance using it is live', async () => {
  const { newOverview, overviewModel, overviewRegions } = await project();
  const button = (d, ov) => overviewRegions(overviewModel(d, ov, NOW), ov, NOW).agents.match(/<button[^>]*data-action="ov-delete-agent"[^>]*>/)[0];
  const ov = newOverview('p');
  const idle = button(data(), ov);
  assert.ok(idle.includes('data-action="ov-delete-agent" data-id="a"'));
  assert.doesNotMatch(idle, /aria-disabled/);
  ov.refreshes.set('a', { status: 'busy' });
  assert.ok(button(data(), ov).includes('title="Wait for Get game controls to finish before deleting"'));
  const live = data({ runs: [{ id: 'r1', project_id: 'p', state: 'Queued', plan: { agent_id: 'a' } }], instance_slot: { owners: [{ run_id: 'r1' }] } });
  assert.ok(button(live, newOverview('p')).includes('title="Stop the instance using this project configuration before deleting it"'));
});

test('a retired configuration shows its reason and only Delete', async () => {
  const { newOverview, overviewModel, overviewRegions } = await project();
  const d = data({ agents: [{ id: 'old', name: 'Old', build_id: 'b', project_id: 'p', status: 'retired', retired_reason: 'Saved before game controls existed, and its saved instructions are invalid.', created_at: NOW - 60 }] });
  const ov = newOverview('p');
  const html = overviewRegions(overviewModel(d, ov, NOW), ov, NOW).agents;
  assert.match(html, /Retired: Saved before game controls existed/);
  assert.deepEqual([...html.matchAll(/data-action="([^"]+)"/g)].map(x => x[1]), ['ov-delete-agent']);
  assert.doesNotMatch(html, /New instance|View controls|Get controls again/);
});

test('an empty project says it has no project configuration', async () => {
  const { newOverview, overviewModel, overviewRegions } = await project();
  const ov = newOverview('p');
  const r = overviewRegions(overviewModel(data({ agents: [] }), ov, NOW), ov, NOW);
  assert.match(r.agents, /No project configuration yet\. <a href="#\/setup\?project=p"[^>]*>Add one<\/a> to start instances\./);
  assert.match(r.head, />Add project configuration</);
});

test('overview markup escapes project, build, agent and goal text', async () => {
  const { newOverview, overviewModel, overviewRegions } = await project();
  const evil = '<img src=x onerror=alert(1)>';
  const d = data({ projects: [{ id: 'p', name: evil }], builds: [{ id: evil, name: evil, project_id: 'p', source_revision: evil }], agents: [{ id: evil, name: evil, build_id: evil, project_id: 'p' }, { id: 'old', name: evil, build_id: evil, project_id: 'p', status: 'retired', retired_reason: evil }],
    runs: [{ id: evil, project_id: 'p', state: 'Ended', ending_reason: evil, plan: { primary_goal: evil } }] });
  const ov = newOverview('p');
  ov.chooser.error = evil;
  ov.refreshes.set(evil, { status: 'error', error: evil });
  ov.validation.set(evil, { ready: false, reason: evil });
  assert.doesNotMatch(Object.values(overviewRegions(overviewModel(d, ov, NOW), ov, NOW)).join(''), /<img/);
});
