'use strict';
// Narrow widths, drawers, the live region and token contrast (spec §12, §13, §5).
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { pathToFileURL } = require('node:url');
const STATIC = path.join(__dirname, '../ashad/static');
const url = file => pathToFileURL(path.join(STATIC, file)).href;
const read = file => fs.readFileSync(path.join(STATIC, file), 'utf8');
const css = read('app.css');

// Every §9 state, as the backend reports it.
const run = (state, extra = {}) => ({ id: 'r1', state, ending_reason: 'stopped_by_user',
  plan: { primary_goal: 'Goal', players: [{ id: 'p1' }] }, players: { p1: { id: 'p1', epoch: 'e1', launched: state !== 'Queued' } }, subgoal_plan: { player_id: 'p1' }, ...extra });
const STATES = [
  [run('Queued')], [run('Starting')], [run('Running')], [run('Paused')], [run('Investigating')],
  [run('Running', { human_control: { player_id: 'p1' } })], [run('Paused', { goal_recalculation: { status: 'planning' } })],
  [run('Stopping')], [run('Ended'), { run_id: 'r1', reason: 'teardown_unconfirmed' }], [run('Ended')], [run('Mystery'), { run_id: 'r1', reason: 'active' }],
];

test('Stop stays the last control and the layout cannot scroll sideways at 800px, in every §9 state', async () => {
  const { instanceState, threadRegions } = await import(url('views/instance.js'));
  for (const [r, owner] of STATES) {
    const status = instanceState(r, owner);
    const { controls } = threadRegions(r, { status, owner, data: {}, ui: { inspectorOpen: false }, pending: new Set(), error: '', cameraStatus: 'offline', player: 'p1' });
    const bar = controls.match(/<div class="control-bar"[\s\S]*?<span class="control-right">([\s\S]*)<\/span><\/div>/);
    assert.ok(bar, `${r.state}: control bar renders`);
    if (status.stop) assert.match(bar[1], /data-ctl="stop"[^>]*>[\s\S]*Stop<\/button>$/, `${status.key}: Stop is the right-most control`);
    else assert.doesNotMatch(controls, /data-ctl="stop"/, status.key);
  }
  // The bar wraps rather than scrolling, Stop's group is pushed right, and below each breakpoint a pane leaves the row.
  assert.match(css, /\.control-bar \{[^}]*flex-wrap: wrap/);
  assert.match(css, /\.control-right \{[^}]*margin-left: auto/);
  assert.match(css, /@media \(max-width: 1279px\) \{ \.layout > \.inspector \{ display: none; \} \}/);
  assert.match(css, /@media \(max-width: 959px\) \{ \.layout > \.sidebar \{ display: none; \}/);
  assert.match(css, /@media \(max-width: 959px\) \{ \.thread-meta \{ display: none; \} \.meta-more \{ display: inline-grid;/);
  const workspaceMin = Number(css.match(/\.workspace \{[^}]*min-width: (\d+)px/)[1]);
  const sidebar = Number(css.match(/--sidebar-w: (\d+)px/)[1]);
  assert.ok(workspaceMin <= 800, 'the workspace alone fits at 800px');
  assert.ok(sidebar + workspaceMin <= 960, 'sidebar and workspace fit at 960px');
});

test('narrow headers keep their meta chips in a popover', async () => {
  const { instanceState, threadRegions } = await import(url('views/instance.js'));
  const r = run('Running', { model: 'gpt-6-astra', plan: { primary_goal: 'Goal', players: [{ id: 'p1' }], transport: { mode: 'enet' } } });
  const { head } = threadRegions(r, { status: instanceState(r), data: {}, ui: { inspectorOpen: false }, pending: new Set(), cameraStatus: 'live', player: 'p1' });
  assert.match(head, /<button type="button" class="icon-btn meta-more" popovertarget="meta-pop"[^>]*aria-label="Instance details"/);
  assert.match(head, /<div id="meta-pop" class="popover menu meta-pop" popover><span class="chip">gpt-6-astra<\/span><span class="chip">ENet<\/span><\/div>/);
});

function fakeDrawer() {
  const listeners = {};
  const log = [];
  const dialog = { open: false, children: [], addEventListener: (type, fn) => { listeners[type] = fn; },
    append(el) { this.children.push(el); log.push(`append ${el.id}`); },
    showModal() { this.open = true; log.push('showModal'); },
    close() { this.open = false; log.push('close'); listeners.close(); } };
  return { dialog, log };
}

test('drawers open modally, return the pane to the layout and focus to their toggle', async () => {
  const { createDrawer } = await import(url('core.js'));
  const { dialog, log } = fakeDrawer();
  const pane = { id: 'sidebar' };
  let toggle = { focus: () => log.push('focus old toggle') };
  const drawer = createDrawer(dialog, pane, p => log.push(`place ${p.id}`));
  drawer.show(() => toggle);
  drawer.show(() => toggle);  // A second open is ignored.
  assert.equal(drawer.open, true);
  toggle = { focus: () => log.push('focus toggle') };  // Closing re-renders and may replace the toggle element.
  drawer.close();
  drawer.close();  // Closing a closed drawer does nothing.
  assert.deepEqual(log, ['append sidebar', 'showModal', 'close', 'place sidebar', 'focus toggle']);
  assert.equal(drawer.open, false);
  const app = read('app.js');
  const onRoute = app.slice(app.indexOf('async function onRoute()'), app.indexOf('// ── Polling'));
  assert.match(onRoute, /sidebarDrawer\.close\(\);[\s\S]*inspectorDrawer\.close\(\);/, 'navigation closes both drawers');
  assert.match(read('index.html'), /<dialog id="drawer-left" class="drawer drawer-left" aria-label="Projects and instances"><\/dialog>/);
});

test('only state changes are announced, through the single live region', async () => {
  const { instanceState, stateAnnouncement } = await import(url('views/instance.js'));
  const running = instanceState(run('Running'));
  assert.equal(stateAnnouncement(null, running), null, 'opening a thread announces nothing');
  assert.equal(stateAnnouncement('running', running), null, 'a poll with the same state announces nothing');
  assert.equal(stateAnnouncement('running', instanceState(run('Paused'))), 'Instance paused.');
  assert.equal(stateAnnouncement('paused', instanceState(run('Ended'))), 'Instance ended · stopped by user.');
  const html = read('index.html');
  assert.equal(html.match(/aria-live/g).length, 1, 'one live region');
  assert.match(html, /<div id="announcer" class="sr-only" role="status" aria-live="polite"><\/div>/);
  assert.match(html, /<div id="toasts" class="toasts"><\/div>/, 'toasts are visual; their text goes through the announcer');
  // Patched view markup (camera pills, ticking ages, timeline cards, progress) never carries its own live region.
  for (const file of ['app.js', ...fs.readdirSync(path.join(STATIC, 'views')).map(f => `views/${f}`)]) {
    assert.doesNotMatch(read(file), /role="(status|alert|log)"|aria-live/, file);
  }
});

test('token pairs meet the §13 contrast floors', () => {
  const token = name => css.match(new RegExp(`--${name}: (#[0-9a-f]{6});`))[1];
  const lum = hex => {
    const [r, g, b] = [1, 3, 5].map(i => parseInt(hex.slice(i, i + 2), 16) / 255).map(v => (v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4));
    return 0.2126 * r + 0.7152 * g + 0.0722 * b;
  };
  const ratio = (a, b) => { const [x, y] = [lum(token(a)), lum(token(b))].sort((m, n) => n - m); return (x + 0.05) / (y + 0.05); };
  const surfaces = ['bg', 'bg-sidebar', 'surface', 'surface-2'];
  // Body and meta text: 4.5:1 on every surface text sits on.
  for (const text of ['text', 'text-2', 'text-3', 'accent', 'danger']) for (const bg of surfaces) assert.ok(ratio(text, bg) >= 4.5, `${text} on ${bg}: ${ratio(text, bg).toFixed(2)}`);
  // Focus ring and status dots: 3:1, including the hover fill a selected row uses.
  for (const mark of ['accent', 'live', 'attention', 'danger', 'muted']) for (const bg of [...surfaces, 'surface-3']) assert.ok(ratio(mark, bg) >= 3, `${mark} on ${bg}: ${ratio(mark, bg).toFixed(2)}`);
  assert.ok(ratio('accent-ink', 'accent') >= 4.5, 'primary button label');
});

test('the temporary styleguide is gone', async () => {
  const { parseRoute } = await import(url('core.js'));
  assert.deepEqual(parseRoute('#/styleguide'), { name: 'redirect', to: '#/' });
  for (const file of ['app.js', 'app.css', 'core.js']) assert.doesNotMatch(read(file), /styleguide|\bsg-/i, file);
});
