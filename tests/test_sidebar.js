'use strict';
// Sidebar model and markup (ashad/static/views/sidebar.js).
const { test } = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');
const { pathToFileURL } = require('node:url');
const load = () => import(pathToFileURL(path.join(__dirname, '../ashad/static/views/sidebar.js')).href);

const NOW = 1_000_000;
const run = (id, project_id, extra = {}) => ({ id, project_id, state: 'Ended', created_at: NOW - 600, plan: { primary_goal: `Goal ${id}` }, players: {}, ...extra });
const ui = (extra = {}) => ({ expanded: new Map(), showAll: new Set(), lastProject: null, route: { name: 'home' }, now: NOW, ...extra });
const data = (extra = {}) => ({
  projects: [{ id: 'p', name: 'Agronauts' }, { id: 'q', name: 'Other' }],
  runs: [run('r1', 'p', { created_at: NOW - 3600 }), run('r2', 'p', { created_at: NOW - 60 }), run('legacy', null), run('ghost', 'deleted-project')],
  instance_slot: { available: true, owners: [] }, ai: { status: 'ready' }, ...extra,
});

test('threads sit under their project newest first; missing or unknown projects group as Unassigned', async () => {
  const { sidebarModel } = await load();
  const m = sidebarModel(data(), ui({ expanded: new Map([['p', true], ['unassigned', true]]) }));
  assert.deepEqual(m.projects[0].threads.map(t => t.id), ['r2', 'r1']);
  assert.equal(m.projects[0].threads[0].age, '1m');
  assert.deepEqual(m.unassigned.threads.map(t => t.id).sort(), ['ghost', 'legacy']);
  assert.equal(m.projects[1].total, 0);
});

test('only the project holding the current or active instance expands by default; explicit choices stick', async () => {
  const { sidebarModel } = await load();
  let m = sidebarModel(data(), ui({ route: { name: 'instance', id: 'r1' } }));
  assert.equal(m.projects[0].expanded, true);
  assert.equal(m.projects[1].expanded, false);
  assert.equal(m.projects[0].threads.find(t => t.id === 'r1').current, true);
  m = sidebarModel(data(), ui({ route: { name: 'instance', id: 'r1' }, expanded: new Map([['p', false], ['q', true]]) }));
  assert.equal(m.projects[0].expanded, false);
  assert.deepEqual(m.projects[0].threads, [], 'collapsed groups render no thread rows');
  assert.equal(m.projects[1].expanded, true);
});

test('long histories show ten threads until Show all is chosen', async () => {
  const { sidebarModel, sidebarHTML } = await load();
  const runs = Array.from({ length: 14 }, (_, i) => run(`r${i}`, 'p', { created_at: NOW - i }));
  let m = sidebarModel(data({ runs }), ui({ expanded: new Map([['p', true]]) }));
  assert.equal(m.projects[0].threads.length, 10);
  assert.match(sidebarHTML(m), /Show all \(14\)/);
  m = sidebarModel(data({ runs }), ui({ expanded: new Map([['p', true]]), showAll: new Set(['p']) }));
  assert.equal(m.projects[0].threads.length, 14);
});

test('active-instance indicator appears elsewhere and hides on its own thread', async () => {
  const { sidebarModel, sidebarHTML } = await load();
  const live = data({ runs: [run('live', 'p', { state: 'Running' })], instance_slot: { available: false, owners: [{ run_id: 'live', state: 'Running', reason: 'running' }] } });
  const elsewhere = sidebarModel(live, ui());
  assert.equal(elsewhere.active.runId, 'live');
  assert.equal(elsewhere.active.status.label, 'Running');
  assert.match(sidebarHTML(elsewhere), /class="active-pill" href="#\/i\/live"/);
  assert.equal(sidebarModel(live, ui({ route: { name: 'instance', id: 'live' } })).active, null);
  const orphan = sidebarModel(data({ instance_slot: { owners: [{ run_id: 'gone', state: 'Unknown', reason: 'orphaned_lease' }] } }), ui());
  assert.equal(orphan.active.linked, false, 'an owner without a run record is shown but not linked');
});

test('New instance targets the route project, then the last project, then setup', async () => {
  const { sidebarModel } = await load();
  assert.equal(sidebarModel(data(), ui({ route: { name: 'project', id: 'q' } })).newInstanceHref, '#/p/q/new');
  assert.equal(sidebarModel(data(), ui({ route: { name: 'instance', id: 'r1' } })).newInstanceHref, '#/p/p/new');
  assert.equal(sidebarModel(data(), ui({ lastProject: 'q' })).newInstanceHref, '#/p/q/new');
  assert.equal(sidebarModel(data({ projects: [] }), ui()).newInstanceHref, '#/setup');
});

test('project names and goals are escaped everywhere they appear', async () => {
  const { sidebarModel, sidebarHTML } = await load();
  const evil = '<img src=x onerror=alert(1)>"';
  const html = sidebarHTML(sidebarModel(data({ projects: [{ id: 'p', name: evil }], runs: [run('r1', 'p', { plan: { primary_goal: evil } })] }), ui({ expanded: new Map([['p', true]]) })));
  assert.doesNotMatch(html, /<img/);
  assert.match(html, /&lt;img src=x onerror=alert\(1\)&gt;&quot;/);
});

test('every interactive row has a stable data-key so focus survives polls', async () => {
  const { sidebarModel, sidebarHTML } = await load();
  const html = sidebarHTML(sidebarModel(data(), ui({ expanded: new Map([['p', true]]) })));
  for (const tag of html.match(/<(a|button)\b[^>]*>/g)) assert.match(tag, /data-key="/, tag);
  assert.match(html, /aria-expanded="true" data-action="toggle-group" data-id="p"/);
  assert.match(html, /aria-label="Set up a game"/);
});

test('Settings carries a sign-in marker only when no provider is ready', async () => {
  const { sidebarModel, sidebarHTML } = await load();
  assert.doesNotMatch(sidebarHTML(sidebarModel(data(), ui())), /AI sign-in needed/);
  assert.match(sidebarHTML(sidebarModel(data({ ai: { status: 'not_validated' } }), ui())), /AI sign-in needed/);
});

test('instance rows carry a delete button beside, not inside, the link', async () => {
  const { sidebarModel, sidebarHTML } = await load();
  const html = sidebarHTML(sidebarModel(data(), ui({ expanded: new Map([['p', true]]) })));
  for (const id of ['r1', 'r2']) {
    assert.ok(html.includes(`data-action="instance-delete" data-id="${id}" data-key="rd:${id}" aria-label="Delete instance: Goal ${id}"`), id);
  }
  const anchors = html.match(/<a class="row thread[\s\S]*?<\/a>/g);
  assert.equal(anchors.length, 2);
  for (const a of anchors) assert.ok(!a.includes('<button'));
});

test("the active instance's delete button is disabled with a reason", async () => {
  const { sidebarModel, sidebarHTML } = await load();
  const d = data({ instance_slot: { owners: [{ run_id: 'r1', state: 'Running', reason: 'running' }] } });
  const html = sidebarHTML(sidebarModel(d, ui({ expanded: new Map([['p', true]]) })));
  const tag = id => html.match(new RegExp(`<button[^>]*data-action="instance-delete" data-id="${id}"[^>]*>`))[0];
  assert.match(tag('r1'), /aria-disabled="true"/);
  assert.match(tag('r1'), /title="Stop this instance before deleting it"/);
  assert.doesNotMatch(tag('r2'), /aria-disabled/);
  assert.match(tag('r2'), /title="Delete instance"/);
});
