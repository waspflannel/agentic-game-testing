'use strict';
// Pure renderer helpers (ashad/static/core.js), imported as the browser loads them.
const { test } = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');
const { pathToFileURL } = require('node:url');
const load = () => import(pathToFileURL(path.join(__dirname, '../ashad/static/core.js')).href);

test('esc neutralises markup and attribute breakouts in untrusted text', async () => {
  const { esc } = await load();
  assert.equal(esc(`<img src=x onerror="alert(1)">'&`), '&lt;img src=x onerror=&quot;alert(1)&quot;&gt;&#39;&amp;');
  assert.equal(esc(null), '');
  assert.equal(esc(0), '0');
});

test('router maps current routes and sends legacy and unknown links home', async () => {
  const { parseRoute } = await load();
  assert.deepEqual(parseRoute('#/p/game%201/new?agent=a1'), { name: 'composer', id: 'game 1', query: { configuration: 'a1' } });
  assert.deepEqual(parseRoute('#/p/g/new?configuration=a1'), { name: 'composer', id: 'g', query: { configuration: 'a1' } });
  assert.deepEqual(parseRoute('#/p/g/new?agent=old&configuration=a1').query, { configuration: 'a1' });
  assert.equal(parseRoute('#/i/run_1').name, 'instance');
  assert.equal(parseRoute('').name, 'home');
  assert.equal(parseRoute('#/settings').section, 'account');
  assert.equal(parseRoute('#/settings/desktops').section, 'desktops');
  for (const legacy of ['#/sessions', '#/agents', '#/nope/x/y', '#/p/%E0%A4%A']) assert.deepEqual(parseRoute(legacy), { name: 'redirect', to: '#/' });
  assert.deepEqual(parseRoute('#/new/a2'), { name: 'legacy-new', configuration: 'a2' });
});

test('legacy new-instance link opens that configuration\'s project composer, or home without a project', async () => {
  const { legacyNewTarget } = await load();
  const data = { projects: [{ id: 'p' }], configurations: [{ id: 'a2', project_id: 'p' }, { id: 'loose', project_id: null }] };
  assert.equal(legacyNewTarget('a2', data), '#/p/p/new?configuration=a2');
  assert.equal(legacyNewTarget('loose', data), '#/');
  assert.equal(legacyNewTarget('missing', data), '#/');
});

test('a late response for an earlier route is recognisably stale', async () => {
  const { createEpoch } = await load();
  const epoch = createEpoch();
  const first = epoch.next();
  assert.equal(first(), true);
  const second = epoch.next();
  assert.equal(first(), false, 'navigation invalidates the earlier ticket');
  assert.equal(second(), true);
});

test('api sends the boundary header and JSON body only on mutations and surfaces detail', async () => {
  const { api, ApiError } = await load();
  const calls = [];
  const fetchImpl = async (url, init) => { calls.push([url, init]); return { ok: url !== '/bad', status: url === '/bad' ? 409 : 200, json: async () => (url === '/bad' ? { detail: 'Slot taken' } : { ok: true }) }; };
  await api('/api/bootstrap', { fetchImpl });
  assert.equal(calls[0][1].headers['X-ASHAD-Request'], undefined);
  assert.equal(calls[0][1].body, undefined);
  await api('/api/x', { method: 'POST', fetchImpl });
  assert.equal(calls[1][1].headers['X-ASHAD-Request'], '1');
  assert.equal(calls[1][1].body, '{}');
  await assert.rejects(api('/bad', { method: 'POST', body: { a: 1 }, fetchImpl }), error => error instanceof ApiError && error.status === 409 && error.message === 'Slot taken');
});

test('run status never reports a teardown-unconfirmed or human-controlled run as plainly ended or running', async () => {
  const { runStatus } = await load();
  assert.deepEqual(runStatus({ state: 'Ended' }, { reason: 'teardown_unconfirmed' }), { tone: 'attention', label: 'Teardown unconfirmed' });
  assert.equal(runStatus({ state: 'Running', human_control: { player_id: 'p' } }).label, 'You have control');
  assert.equal(runStatus({ state: 'Paused' }).tone, 'attention');
  assert.equal(runStatus({ state: 'Weird' }).label, 'Unknown');
});

test('since words an age and says never when there is no time', async () => {
  const { since } = await load();
  assert.equal(since(1000, 1030), 'just now');
  assert.equal(since(1000, 1000 + 180), '3m ago');
  assert.equal(since(undefined), 'never');
});

test('ago is compact and never negative', async () => {
  const { ago } = await load();
  assert.equal(ago(1000, 1030), 'now');
  assert.equal(ago(1000, 1000 + 180), '3m');
  assert.equal(ago(0, 5 * 3600), '5h');
  assert.equal(ago(0, 2 * 86400), '2d');
  assert.equal(ago(2000, 1000), 'now');
  assert.equal(ago(undefined), '');
});

// Minimal element stand-in: assigning innerHTML replaces every child with fresh nodes, like the DOM.
function fakeRegion() {
  let html = '';
  const fresh = () => [{ dataset: { scroll: 'side' }, scrollTop: 0 }, { dataset: { key: 'g:p' }, focused: false, focus() { this.focused = true; } }];
  const region = {
    nodes: fresh(),
    get innerHTML() { return html; },
    set innerHTML(value) { html = value; region.nodes = fresh(); },
    contains: node => region.nodes.includes(node),
    querySelectorAll: selector => region.nodes.filter(n => (selector === '[data-scroll]' ? n.dataset.scroll : n.dataset.key) !== undefined),
  };
  return region;
}

test('patchHTML skips identical markup and restores focus and scroll after a real change', async () => {
  const { patchHTML } = await load();
  const region = fakeRegion();
  assert.equal(patchHTML(region, '<a>', null), true);
  assert.equal(patchHTML(region, '<a>', null), false, 'unchanged markup is not re-rendered');
  region.nodes[0].scrollTop = 240;
  assert.equal(patchHTML(region, '<b>', region.nodes[1]), true);
  assert.equal(region.nodes[0].scrollTop, 240);
  assert.equal(region.nodes[1].focused, true);
});

const MODEL_OPTS = [{ id: 'gpt-6-sol', label: 'GPT-6 Sol' }, { id: 'gpt-6-astra', label: 'GPT-6 Astra' }];
const menuArgs = (over = {}) => ({ key: 'c-model', field: 'model', options: MODEL_OPTS, selected: 'gpt-6-astra', defaultModel: 'gpt-6-sol', ...over });
const optionButtons = html => html.match(/<button type="button" role="option"[\s\S]*?<\/button>/g) || [];

test('modelMenuHTML renders a trigger and a listbox of the given options', async () => {
  const { modelMenuHTML } = await load();
  const html = modelMenuHTML(menuArgs());
  assert.match(html, /popovertarget="c-model-menu"/);
  assert.match(html, /aria-haspopup="listbox"/);
  assert.match(html, /data-key="c-model"/);
  assert.match(html, /<span class="chip-text">GPT-6 Astra<\/span>/);
  assert.match(html, /id="c-model-menu" popover/);
  assert.match(html, /role="listbox"/);
  assert.match(html, /aria-label="Model"/);
  assert.deepEqual([...html.matchAll(/data-value="([^"]*)"/g)].map(m => m[1]), ['gpt-6-sol', 'gpt-6-astra']);
  const opts = optionButtons(html);
  assert.equal(opts.length, 2);
  for (const o of opts) assert.match(o, /data-action="pick-model" data-field="model"/);
  assert.doesNotMatch(html, /<select/);
});

test('modelMenuHTML marks exactly one selected option with a check and the default with a pill', async () => {
  const { modelMenuHTML } = await load();
  const html = modelMenuHTML(menuArgs());
  const opts = optionButtons(html);
  const selected = opts.filter(o => o.includes('aria-selected="true"'));
  assert.equal(selected.length, 1);
  assert.match(selected[0], /data-value="gpt-6-astra"/);
  assert.equal(html.split('#i-check').length - 1, 1);
  assert.match(selected[0], /#i-check/);
  assert.equal(html.split('model-default').length - 1, 1);
  assert.match(opts.find(o => o.includes('model-default')), /data-value="gpt-6-sol"/);
  assert.doesNotMatch(modelMenuHTML(menuArgs({ defaultModel: undefined })), /model-default/);
});

test('modelMenuHTML escapes labels and ids', async () => {
  const { modelMenuHTML } = await load();
  const html = modelMenuHTML(menuArgs({ options: [{ id: 'a"b', label: '<b>"x"</b>' }], selected: 'a"b', defaultModel: undefined }));
  assert.ok(html.includes('&lt;b&gt;&quot;x&quot;&lt;/b&gt;'));
  assert.ok(html.includes('data-value="a&quot;b"'));
  assert.ok(!html.includes('<b>'));
});

test('modelMenuHTML falls back to the raw id when the selected model is not listed', async () => {
  const { modelMenuHTML } = await load();
  const html = modelMenuHTML(menuArgs({ selected: 'opus' }));
  assert.match(html, /<span class="chip-text">opus<\/span>/);
  assert.doesNotMatch(html, /aria-selected="true"/);
});

test('nextOptionIndex clamps arrows and jumps with Home/End', async () => {
  const { nextOptionIndex } = await load();
  assert.equal(nextOptionIndex('ArrowDown', 3, 4), 3);
  assert.equal(nextOptionIndex('ArrowDown', 1, 4), 2);
  assert.equal(nextOptionIndex('ArrowUp', 0, 4), 0);
  assert.equal(nextOptionIndex('Home', 2, 4), 0);
  assert.equal(nextOptionIndex('End', 0, 4), 3);
  assert.equal(nextOptionIndex('Tab', 1, 4), -1);
});

test('runConfigurationId reads the new key, then legacy run and plan keys', async () => {
  const { runConfigurationId } = await load();
  assert.equal(runConfigurationId({ configuration_id: 'c', agent_id: 'a', plan: { configuration_id: 'pc', agent_id: 'pa' } }), 'c');
  assert.equal(runConfigurationId({ agent_id: 'a', plan: { configuration_id: 'pc' } }), 'a');
  assert.equal(runConfigurationId({ plan: { configuration_id: 'pc', agent_id: 'pa' } }), 'pc');
  assert.equal(runConfigurationId({ plan: { agent_id: 'pa' } }), 'pa');
  assert.equal(runConfigurationId({}), undefined);
  assert.equal(runConfigurationId(null), undefined);
});

test('runStatus Fixing is pending Fixing, needs_you is attention Needs you', async () => {
  const { runStatus } = await load();
  assert.deepEqual(runStatus({ state: 'Fixing', loop: { phase: 'gating' } }), { tone: 'pending', label: 'Fixing' });
  assert.deepEqual(runStatus({ state: 'Fixing' }), { tone: 'pending', label: 'Fixing' });
  assert.deepEqual(runStatus({ state: 'Fixing', loop: { phase: 'needs_you' } }), { tone: 'attention', label: 'Needs you' });
});
