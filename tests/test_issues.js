'use strict';
// Project Issues board and issue view (ashad/static/views/issues.js).
const { test } = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');
const { pathToFileURL } = require('node:url');
const url = file => pathToFileURL(path.join(__dirname, '../ashad/static', file)).href;
const issues = () => import(url('views/issues.js'));

const NOW = 2_000_000_000;
const data = () => ({
  projects: [{ id: 'p1', name: 'Agronauts' }, { id: 'p2', name: 'Other' }],
  runs: [{ id: 'r1', project_id: 'p1', plan: { primary_goal: 'Craft a pickaxe' } }, { id: 'r2', project_id: 'p2' }],
});
const summary = (extra) => ({ issue_id: 'i1', type: 'unsupported_action', title: 'Cannot drag', status: 'open', occurrences: 12, first_at: NOW - 3 * 86400, last_at: NOW - 7200, first_run_id: 'r1', last_run_id: 'r1', runs: ['r1'], file: 'f', ...extra });
const SUMMARIES = [
  summary({ issue_id: 'i1', title: '<img src=x>' }),
  summary({ issue_id: 'i2', status: 'closed', title: 'Closed one', occurrences: 1, runs: ['r1', 'r9', 'r8'], last_run_id: 'r9' }),
  summary({ issue_id: 'i3', type: 'game_bug', title: 'Door stuck' }),
];
const rowOf = (html, id) => html.split('<li ').find(s => s.includes(`data-key="ib-${id}"`));

const example = (extra) => ({
  example_id: 'e1', at: '2026-10-01T10:00:00Z', run_id: 'r1', project_id: 'p1', player_id: 'pl', source: 'live_run',
  intent: { primary_goal: 'Craft', current_goal: { objective: 'Craft a pickaxe' } },
  recent_decisions: [{ event_id: 'd1', kind: 'tool_call', at: NOW - 10, payload: { a: 1 } }],
  game_state: { observation_id: 'o', observation: { hp: 3 }, telemetry: { completed_quests: ['q1', 'q2'] }, progress: { level: 2 } },
  screenshot: { status: 'ok', frames: [], url: '/api/runs/r1/shot.png', note: 'retained' },
  evidence: { run_dir: '/runs/r1', ledger: '/runs/r1/ledger.jsonl', report: '/runs/r1/report.md' }, ...extra,
});
const UNSUPPORTED = () => ({
  id: 'i1', type: 'unsupported_action', project_id: 'p1', title: 'Cannot drag', status: 'open', occurrences: 2, runs: ['r1'], file: 'issues/i1.json',
  first_seen: { at: '2026-10-01T10:00:00Z', run_id: 'r1' }, last_seen: { at: '2026-10-02T10:00:00Z', run_id: 'r1' }, examples_kept: 1, examples_dropped: 0,
  examples: [example({
    wanted: { tool: 'act', verb: 'drag', target_id: 't', target_kind: 'slot', target_label: 'Slot 3', blocker: 'no drag', controller: 'ctl', follow_up_error: null },
    outcome: { status: 'not_supported', reason: 'nope', error: null, error_code: null, http_status: 501 },
    capabilities: { game_verbs: ['click', 'type'], structured_tools: ['act'], observed_inputs: [], candidate_actions: ['click slot'], target_advertised_actions: [] },
  })],
});
const CRASH = () => ({
  ...UNSUPPORTED(), id: 'i4', type: 'crash', title: 'Game exited',
  examples: [example({ crash: { kind: 'process_exit', detector: 'watchdog', reason: 'died', exit_code: 139, signal: 'SIGSEGV', log_tail: 'segfault at 0x0' }, last_action: { verb: 'jump', target_id: 'pad', status: 'done' } })],
});
const BUG = () => ({
  ...UNSUPPORTED(), id: 'i3', type: 'game_bug', title: 'Door stuck',
  examples: [example({ report: { symptom: 'door_stuck', expected: 'opens', observed: 'stays shut', target_id: 'door1', target_kind: 'door', target_label: 'Door', target_state: { open: false }, related_action: { verb: 'use', target_id: 'door1', status: 'failed' } } })],
});
const ids = new Set(['r1']);
const view = (record, extra = {}) => ({ record, loading: false, error: '', exampleId: null, pending: false, ...extra });

test('boardModel groups by type in fixed order', async () => {
  const { boardModel } = await issues();
  const m = boardModel(data(), 'p1', null, { rows: [...SUMMARIES, summary({ issue_id: 'x', type: 'other' })] });
  assert.deepEqual(m.groups.map(g => g.type), ['unsupported_action', 'crash', 'game_bug']);
  assert.deepEqual(m.groups.map(g => g.rows.length), [2, 0, 1]);
  assert.equal(m.projectName, 'Agronauts');
});

test('boardModel is null groups before the first fetch', async () => {
  const { boardModel } = await issues();
  assert.equal(boardModel(data(), 'p1', null, { loading: true }).groups, null);
});

test('boardRegions rows show title link, status, count, source and seen times, and escape titles', async () => {
  const { boardModel, boardRegions } = await issues();
  const html = boardRegions(boardModel(data(), 'p1', null, { rows: SUMMARIES }), { rows: SUMMARIES }, NOW).groups;
  const row = rowOf(html, 'i1');
  assert.match(row, /&lt;img/);
  assert.doesNotMatch(row, /<img/);
  assert.match(row, /href="#\/p\/p1\/issues\/i1"/);
  assert.match(row, /12 times/);
  assert.match(row, />Open</);
  assert.match(row, new RegExp(`<time data-ts="${NOW - 3 * 86400}">`));
  assert.match(row, new RegExp(`<time data-ts="${NOW - 7200}">`));
});

test('closed rows say Closed and carry issue-closed', async () => {
  const { boardModel, boardRegions } = await issues();
  const html = boardRegions(boardModel(data(), 'p1', null, { rows: SUMMARIES }), { rows: SUMMARIES }, NOW).groups;
  const row = rowOf(html, 'i2');
  assert.match(row, /issue-closed/);
  assert.match(row, />Closed</);
  assert.doesNotMatch(rowOf(html, 'i1'), /issue-closed/);
});

test('empty groups say none recorded, or none in this instance when filtered', async () => {
  const { boardModel, boardRegions } = await issues();
  const board = { rows: SUMMARIES };
  assert.match(boardRegions(boardModel(data(), 'p1', null, board), board, NOW).groups, /No crashes recorded\./);
  const filtered = boardRegions(boardModel(data(), 'p1', 'r1', board), board, NOW).groups;
  assert.match(filtered, /None in this instance\./);
  assert.doesNotMatch(filtered, /No crashes recorded/);
});

test('run filter chip names the instance and links back to the unfiltered board', async () => {
  const { boardModel, boardRegions } = await issues();
  const board = { rows: [] };
  const known = boardRegions(boardModel(data(), 'p1', 'r1', board), board, NOW).filter;
  assert.match(known, /Instance: Craft a pickaxe/);
  assert.match(known, /href="#\/p\/p1\/issues"/);
  assert.match(boardRegions(boardModel(data(), 'p1', 'gone', board), board, NOW).filter, /Instance: Deleted instance/);
  assert.equal(boardRegions(boardModel(data(), 'p1', null, board), board, NOW).filter, '');
});

test('sources read the last instance, Deleted instance and +N', async () => {
  const { boardModel, boardRegions } = await issues();
  const html = boardRegions(boardModel(data(), 'p1', null, { rows: SUMMARIES }), { rows: SUMMARIES }, NOW).groups;
  const gone = rowOf(html, 'i2');
  assert.match(gone, /Deleted instance \+2/);
  assert.doesNotMatch(gone, /#\/i\/r9/);
  assert.match(rowOf(html, 'i1'), /<a href="#\/i\/r1">Craft a pickaxe<\/a>/);
});

test('board error and loading states', async () => {
  const { boardModel, boardRegions } = await issues();
  const err = { error: 'boom' };
  const e = boardRegions(boardModel(data(), 'p1', null, err), err, NOW).groups;
  assert.match(e, /Issues are unavailable: boom/);
  assert.match(e, /data-announce/);
  const loading = { loading: true };
  const l = boardRegions(boardModel(data(), 'p1', null, loading), loading, NOW);
  assert.match(l.groups, /Loading issues…/);
  assert.match(l.head, /aria-busy="true"/);
});

test('issueInfoHTML unsupported shows what happened, actions, decisions, screenshot img and game state in order', async () => {
  const { issueInfoHTML } = await issues();
  const r = UNSUPPORTED();
  const html = issueInfoHTML(r, r.examples[0], { runIds: ids });
  const at = ['What happened', 'Available actions', 'Recent decisions', 'Screenshot', 'Game state', 'Evidence'].map(t => html.indexOf(`>${t}<`));
  assert.ok(at.every(i => i >= 0));
  assert.deepEqual([...at].sort((a, b) => a - b), at);
  assert.match(html, /<img class="issue-shot" src="\/api\/runs\//);
  assert.match(html, /Slot 3, slot/);
  assert.match(html, /Candidate actions \(1\)/);
  assert.doesNotMatch(html, /Crash log|Bug report/);
});

test('issueInfoHTML crash shows the symptom and log tail and says actions are not recorded', async () => {
  const { issueInfoHTML } = await issues();
  const r = CRASH();
  const html = issueInfoHTML(r, r.examples[0], { runIds: ids });
  assert.match(html, /Not recorded for this issue type\./);
  assert.match(html, /segfault at 0x0/);
  assert.match(html, /jump/);
  assert.match(html, /SIGSEGV/);
  assert.match(html, />Crash log</);
});

test('issueInfoHTML bug shows the report and the related action', async () => {
  const { issueInfoHTML } = await issues();
  const r = BUG();
  const html = issueInfoHTML(r, r.examples[0], { runIds: ids });
  assert.match(html, />Bug report</);
  assert.match(html, /stays shut/);
  assert.match(html, /Door stuck/i);
  assert.match(html, /Related action/);
  assert.match(html, /use/);
  assert.match(html, /door1/);
});

test('issueInfoHTML screenshot notes read in words', async () => {
  const { issueInfoHTML } = await issues();
  const words = { pending: 'Saved when the instance ends.', deleted: 'Deleted with its instance.', missing: 'Frames were not retained.', unavailable: 'No screenshot was pinned for this hit.' };
  for (const [note, text] of Object.entries(words)) {
    const r = UNSUPPORTED();
    r.examples[0].screenshot = { status: 'x', frames: [], url: null, note };
    const html = issueInfoHTML(r, r.examples[0], { runIds: ids });
    assert.match(html, new RegExp(text.replace('.', '\\.')), note);
    assert.doesNotMatch(html, /issue-shot/);
  }
});

test('issueInfoHTML escapes game text everywhere', async () => {
  const { issueInfoHTML } = await issues();
  const X = '<script>x</script>';
  const u = UNSUPPORTED();
  Object.assign(u.examples[0].wanted, { target_label: X });
  u.examples[0].capabilities.candidate_actions = [X];
  u.examples[0].recent_decisions = [{ kind: 'k', at: 1, payload: X }];
  u.examples[0].game_state.observation = X;
  const c = CRASH();
  c.examples[0].crash.log_tail = X;
  const b = BUG();
  b.examples[0].report.observed = X;
  for (const r of [u, c, b]) {
    const html = issueInfoHTML(r, r.examples[0], { runIds: ids });
    assert.doesNotMatch(html, /<script>/);
    assert.match(html, /&lt;script&gt;/);
  }
  const html = issueInfoHTML(u, u.examples[0], { runIds: ids });
  assert.equal(html.split('&lt;script&gt;').length - 1 >= 4, true);
});

test('issueInfoHTML links the instance only when it exists', async () => {
  const { issueInfoHTML } = await issues();
  const r = UNSUPPORTED();
  const withRun = issueInfoHTML(r, r.examples[0], { runIds: ids });
  assert.match(withRun, /Open instance/);
  assert.match(withRun, /href="#\/i\/r1"/);
  const without = issueInfoHTML(r, r.examples[0], { runIds: new Set() });
  assert.match(without, /Instance deleted\./);
  assert.doesNotMatch(without, /Open instance/);
});

test('issueViewRegions offers Close for open and Reopen for closed, and marks a pending change busy', async () => {
  const { issueViewRegions } = await issues();
  const open = issueViewRegions(view(UNSUPPORTED()), data());
  assert.match(open.head, /data-status="closed"/);
  assert.match(open.head, /Close issue/);
  assert.doesNotMatch(open.head, /aria-busy/);
  const closed = issueViewRegions(view({ ...UNSUPPORTED(), status: 'closed' }), data());
  assert.match(closed.head, /data-status="open"/);
  assert.match(closed.head, /Reopen issue/);
  assert.match(issueViewRegions(view(UNSUPPORTED(), { pending: true }), data()).head, /aria-busy="true"/);
});

test('example picker lists newest first and defaults to the newest', async () => {
  const { issueViewRegions } = await issues();
  const r = UNSUPPORTED();
  const older = example({ example_id: 'old', at: '2026-09-01T10:00:00Z', game_state: { observation: 'OLDOBS', progress: null, telemetry: {} } });
  r.examples = [older, example({ example_id: 'new', game_state: { observation: 'NEWOBS', progress: null, telemetry: {} } })];
  r.examples_dropped = 2;
  const first = issueViewRegions(view(r), data());
  const options = first.examples.match(/<option [^>]*>/g);
  assert.match(options[0], /value="new"/);
  assert.match(options[0], /selected/);
  assert.doesNotMatch(options[1], /selected/);
  assert.match(first.examples, /2 older examples were not kept\./);
  assert.match(first.info, /NEWOBS/);
  const pick = issueViewRegions(view(r, { exampleId: 'old' }), data());
  assert.match(pick.examples.match(/<option [^>]*>/g)[1], /selected/);
  assert.doesNotMatch(pick.examples.match(/<option [^>]*>/g)[0], /selected/);
  assert.match(pick.info, /OLDOBS/);
  assert.doesNotMatch(pick.info, /NEWOBS/);
});

test('issueViewRegions shows loading and error before the record arrives', async () => {
  const { issueViewRegions } = await issues();
  const l = issueViewRegions(view(null, { loading: true }), data());
  assert.match(l.head, /Loading issue…/);
  assert.deepEqual([l.meta, l.examples, l.info], ['', '', '']);
  const e = issueViewRegions(view(null, { error: 'boom' }), data());
  assert.match(e.head, /Issue is unavailable: boom\./);
  assert.match(e.head, /data-announce/);
});
