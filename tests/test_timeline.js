'use strict';
// Activity timeline: bounded window, paging, kinds and labels (ashad/static/views/timeline.js).
const { test } = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');
const { pathToFileURL } = require('node:url');
const load = () => import(pathToFileURL(path.join(__dirname, '../ashad/static/views/timeline.js')).href);

const heavy = 'x'.repeat(30000);
const ev = (seq, kind = 'structured_decision_context', payload = { full_observation: { observation_id: `obs_${seq}`, blob: heavy, entities: [], state: { player: { inventory: [] } } } }) =>
  ({ id: `evt_${seq}`, seq, kind, timestamp: 1000 + seq, payload });
const range = (from, to, make = ev) => Array.from({ length: to - from + 1 }, (_, i) => make(from + i));
const runWith = events => ({ id: 'r1', events, events_page: { total: events.at(-1)?.seq ?? 0, next: events.at(-1)?.seq ?? 0, recent_100_older_count: Math.max(0, (events.at(-1)?.seq ?? 0) - 100) } });

test('100 heavy events render a bounded page on every poll', async () => {
  const { createTimeline, seedFromRun, applyLive, timelineCards, cardBody, eventIndex, PAGE, MAX_EVENTS, RAW_LIMIT } = await load();
  const tl = createTimeline();
  seedFromRun(tl, runWith(range(101, 300)));
  assert.equal(tl.events.length, PAGE, 'the newest 200 arrive, the latest 100 render');
  assert.equal(tl.older, 200);
  for (let poll = 0; poll < 5; poll++) {
    const after = tl.cursor;
    assert.equal(applyLive(tl, tl.token, { events: range(after + 1, after + 100), total: after + 100, has_more: false }, after), 100);
    const cards = timelineCards(tl);
    assert.ok(tl.events.length <= MAX_EVENTS && cards.length <= MAX_EVENTS);
    assert.ok(cards.reduce((n, c) => n + c.html.length, 0) < cards.length * 1200, 'rows never carry the payload');
  }
  const body = cardBody(timelineCards(tl)[0], eventIndex(tl.events));
  assert.ok(body.length < RAW_LIMIT + 2000, 'the raw block is truncated');
  assert.match(body, /Show full \(\d+KB\)/);
});

test('older pages replace the window instead of accumulating', async () => {
  const { createTimeline, seedFromRun, beginPage, applyPage, applyLive, newerCount } = await load();
  const tl = createTimeline();
  seedFromRun(tl, runWith(range(301, 500)));
  let token = beginPage(tl);
  assert.ok(applyPage(tl, token, { events: range(301, 400), older_count: 300, newer_count: 100, total: 500 }, 'history'));
  token = beginPage(tl);
  assert.ok(applyPage(tl, token, { events: range(201, 300), older_count: 200, newer_count: 200, total: 500 }, 'history'));
  assert.equal(tl.events.length, 100);
  assert.deepEqual([tl.events[0].seq, tl.events.at(-1).seq], [201, 300]);
  assert.equal(newerCount(tl, 512), 212, 'rows written since the page loaded count as newer');
  assert.equal(applyLive(tl, tl.token, { events: range(501, 510) }, tl.cursor), 0, 'history mode never appends');
});

test('a late history response cannot override Return to latest', async () => {
  const { createTimeline, beginPage, applyPage } = await load();
  const tl = createTimeline();
  const older = beginPage(tl);
  const latest = beginPage(tl);
  assert.ok(applyPage(tl, latest, { events: range(401, 500), older_count: 400, newer_count: 0, total: 500 }, 'latest'));
  assert.equal(applyPage(tl, older, { events: range(1, 100), older_count: 0, newer_count: 400, total: 500 }, 'history'), false);
  assert.equal(tl.mode, 'latest');
  assert.equal(tl.events[0].seq, 401);
});

test('a live page applies only at its cursor, and a gap reloads the latest page', async () => {
  const { createTimeline, seedFromRun, applyLive } = await load();
  const tl = createTimeline();
  seedFromRun(tl, runWith(range(1, 50)));
  assert.equal(applyLive(tl, tl.token, { events: range(40, 60) }, 40), 0, 'a reply for an old cursor is dropped');
  assert.equal(applyLive(tl, tl.token, { events: range(51, 250), has_more: true }, 50), 'reload');
  assert.equal(tl.events.at(-1).seq, 50);
});

test('unknown event types render as Other rather than being dropped, escaped', async () => {
  const { eventKind, timelineCards, createTimeline } = await load();
  assert.equal(eventKind('brand_new_<img src=x>'), 'other');
  assert.equal(eventKind('teardown_failed'), 'problem');
  assert.equal(eventKind('resume_failed'), 'control');
  const tl = createTimeline();
  tl.events = [ev(1, 'brand_new_<img src=x onerror=alert(1)>', {}), ev(2, 'investigation_started', { reason: '<b>stuck</b>' })];
  const html = timelineCards(tl).map(c => c.html).join('');
  assert.equal(timelineCards(tl).length, 2);
  assert.match(html, /Other/);
  assert.doesNotMatch(html, /<img|<b>/);
  assert.match(html, /&lt;b&gt;stuck/);
});

test('"selected", "delivered" and "confirmed" labels never merge', async () => {
  const { timelineCards, createTimeline, outcomeLabel, SELECTED_LABEL } = await load();
  const tl = createTimeline();
  tl.events = [
    ev(1, 'decision_selected', { action: { tool: 'interact', args: { target_id: 'door_3' } } }),
    ev(2, 'action_result', { result: { status: 'accepted' } }),
    ev(3, 'structured_action_outcome', { result: { status: 'completed' } }),
    ev(4, 'structured_action_outcome', { result: { status: 'rejected', reason: 'target moved' } }),
  ];
  const [failed, confirmed, delivered, selected] = timelineCards(tl).map(c => c.html);  // Newest first.
  assert.match(selected, new RegExp(SELECTED_LABEL));
  assert.match(selected, /interact\(target_id=door_3\)/);
  assert.match(delivered, /Input delivered/);
  assert.match(confirmed, /Effect confirmed/);
  assert.match(failed, /Failed: rejected · target moved/);
  const labels = ['selected, not yet sent', 'Input delivered', 'Effect confirmed', 'Failed'];
  for (const [i, html] of [selected, delivered, confirmed, failed].entries()) {
    labels.forEach((label, j) => assert.equal(html.includes(label), i === j, `card ${i} vs label "${label}"`));
  }
  assert.equal(outcomeLabel(undefined).text, 'Outcome unknown');
});

test('adjacent model events collapse into one turn row; filters narrow by kind', async () => {
  const { timelineCards, createTimeline } = await load();
  const tl = createTimeline();
  tl.events = [
    ev(1, 'provider_request_started', {}), ev(2, 'inference_usage', { usage: { total_tokens: 2100 }, latency_seconds: 4.2 }),
    ev(3, 'decision_selected', { action: { tool: 'move_to', args: {} } }), ev(4, 'provider_request_finished', {}),
  ];
  const cards = timelineCards(tl);
  assert.deepEqual(cards.map(c => c.key), ['m4', '3', 'm1'], 'newest first, model rows still grouped');
  assert.match(cards[2].html, /Model turn · 2\.1k tokens · 4\.2s/);
  tl.filter = 'actions';
  assert.deepEqual(timelineCards(tl).map(c => c.key), ['3']);
});

// Minimal DOM: enough of <ol>/<li> for the keyed reconcile.
function fakeList() {
  const list = { children: [],
    get firstChild() { return this.children[0] || null; },
    insertBefore(el, ref) { this.children = this.children.filter(c => c !== el); const i = ref ? this.children.indexOf(ref) : -1; i < 0 ? this.children.push(el) : this.children.splice(i, 0, el); el.parent = this; } };
  const doc = { createElement: () => {
    const details = { open: false };
    return { dataset: {}, details, querySelector: () => details,
      get nextSibling() { const c = this.parent.children; return c[c.indexOf(this) + 1] || null; },
      replaceWith(other) { const c = this.parent.children; c[c.indexOf(this)] = other; other.parent = this.parent; },
      remove() { this.parent.children = this.parent.children.filter(c => c !== this); } };
  } };
  return { list, doc };
}

test('newest activity is on top: Return to latest above the list, Load older below, the new pill points up', async () => {
  const { createTimeline, seedFromRun, timelineCards, timelineRegions } = await load();
  const tl = createTimeline();
  seedFromRun(tl, runWith(range(1, 150, seq => ev(seq, 'run_paused', { reason: 'r' }))));
  const cards = timelineCards(tl);
  assert.deepEqual([cards[0].key, cards.at(-1).key], ['150', '51']);
  tl.unseen = 3;
  let r = timelineRegions(tl, { runTotal: 150, cards });
  assert.equal(r['tl-top'], '');
  assert.match(r['tl-foot'], /data-action="tl-older"[^>]*>Load older activity \(50 earlier\)/);
  assert.match(r['tl-pill'], /data-action="tl-to-latest"[\s\S]*#i-arrow-up[\s\S]*3 new/);
  tl.mode = 'history';
  r = timelineRegions(tl, { runTotal: 150, cards });
  assert.match(r['tl-top'], /data-action="tl-latest"[\s\S]*Return to latest/);
  assert.equal(r['tl-pill'], '', 'no pill while reading history');
});

test('polls keep existing cards, so an open card stays open', async () => {
  const { timelineCards, syncCards, createTimeline } = await load();
  const { list, doc } = fakeList();
  const tl = createTimeline();
  tl.events = range(1, 3, seq => ev(seq, 'run_paused', { reason: 'r' }));
  syncCards(list, timelineCards(tl), doc);
  assert.deepEqual(list.children.map(c => c.dataset.card), ['3', '2', '1']);
  tl.events = range(2, 5, seq => ev(seq, 'run_paused', { reason: 'r' }));
  syncCards(list, timelineCards(tl), doc);
  assert.deepEqual(list.children.map(c => c.dataset.card), ['5', '4', '3', '2'], 'new cards go in at the top');
  const kept = list.children.find(c => c.dataset.card === '3');
  tl.events = range(2, 5, seq => ev(seq, 'run_paused', { reason: 'r' }));
  kept.details.open = true;
  syncCards(list, timelineCards(tl), doc);
  assert.equal(list.children.find(c => c.dataset.card === '3'), kept, 'an unchanged card is the same element');
  assert.equal(kept.details.open, true);
});

test('a rejected proposal row reads attempt and reason', async () => {
  const { createTimeline, timelineCards, cardBody, eventIndex } = await load();
  const tl = createTimeline();
  tl.events = [ev(1, 'subgoal_plan_rejected', { player_id: 'p1', attempt: 2, reason: 'Completion threshold <b>', objective: 'Plant <i>', revision: 3 })];
  const [card] = timelineCards(tl);
  assert.match(card.html, /Proposal rejected \(attempt 2\): Completion threshold &lt;b&gt;/);
  assert.match(card.html, /tone-attention/);
  assert.doesNotMatch(card.html, /<b>/);
  const body = cardBody(card, eventIndex(tl.events));
  assert.match(body, /Attempt/);
  assert.match(body, /Objective/);
  assert.match(body, /Plant &lt;i&gt;/);
});
