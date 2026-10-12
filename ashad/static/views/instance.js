// Instance thread: header, tabs, camera and control bar (spec §8.5 A–C, §9). Markup and state are pure; app.js owns I/O.
import { esc, icon, href, newId, runGoal, humanize, badge, modelLabel, providerModels, modelMenuHTML, runConfigurationId } from '../core.js';
import { typeLabel } from './issues.js';

const enc = encodeURIComponent;
const LIVE_STATES = new Set(['Starting', 'Running', 'Paused', 'Investigating', 'Stopping']);  // /live serves only these.
export const isActiveRun = run => run?.state === 'Queued' || LIVE_STATES.has(run?.state);
export const playerIds = run => Object.keys(run?.players || {});

// The four views of one instance. Status and controls stay outside them.
export const THREAD_TABS = [['bot', 'Bot view', 'monitor'], ['goals', 'Goals', 'list-checks'], ['world', 'World state', 'eye'], ['actions', 'Actions', 'send']];
// An ended run has no camera, so it opens on its record.
export const defaultTab = run => (isActiveRun(run) ? 'bot' : 'actions');

// Arrow keys move through the tab bar and wrap; Home and End jump to the ends. Any other key returns null.
export function nextTab(current, key) {
  const ids = THREAD_TABS.map(([id]) => id);
  const at = ids.indexOf(current);
  if (key === 'ArrowRight') return ids[(at + 1) % ids.length];
  if (key === 'ArrowLeft') return ids[(at - 1 + ids.length) % ids.length];
  if (key === 'Home') return ids[0];
  if (key === 'End') return ids.at(-1);
  return null;
}

// One player in v1; recalculation needs that player launched and nobody else holding control.
function canRecalculate(run) {
  const players = run?.plan?.players || [];
  const pid = run?.subgoal_plan?.player_id;
  return players.length === 1 && pid && pid === players[0]?.id && run.players?.[pid]?.launched === true && !run.human_control && !run.external_control;
}

// §9 status and control matrix. owner is this run's instance_slot owner, if any.
// left: primary actions; stop: Stop shown right-most; menu: the ⋯ menu.
export function instanceState(run, owner) {
  const launched = playerIds(run).some(id => run.players[id]?.launched);
  const recalc = canRecalculate(run) ? ['recalculate'] : [];
  const make = (key, tone, label, left, stop, menu, extra) => ({ key, badge: { tone, label }, left, stop, menu, ...extra });
  if (run?.state === 'Ended' && owner?.reason === 'teardown_unconfirmed') return make('unconfirmed', 'attention', 'Teardown unconfirmed', ['reconcile'], false, ['report', 'evidence']);
  if (run?.human_control && LIVE_STATES.has(run.state)) return make('human', 'attention', 'You have control', ['handback'], true, ['flag']);
  if (run?.goal_recalculation?.status === 'planning' && isActiveRun(run)) return make('replanning', 'pending', 'Replanning', [], true, []);
  switch (run?.state) {
    case 'Queued': case 'Starting': return make('starting', 'pending', 'Starting', [], true, ['flag']);
    case 'Running': return make('running', 'live', 'Running', ['pause', ...(launched ? ['takeover'] : [])], true, [...recalc, 'flag']);
    case 'Paused': return make('paused', 'attention', 'Paused', ['resume', ...(launched ? ['takeover'] : [])], true, [...recalc, 'flag']);
    // Extend lives here, not under Running: the backend only extends an open investigation.
    case 'Investigating': return make('investigating', 'attention', 'Needs review', ['resume', ...(launched ? ['takeover'] : [])], true, ['extend', ...recalc, 'flag'], { banner: true });
    case 'Stopping': return make('stopping', 'pending', 'Stopping', [], false, [], { note: 'Waiting for teardown confirmation' });
    case 'Ended': return make('ended', 'muted', `Ended · ${humanize(run.ending_reason) || 'reason not recorded'}`, ['report', 'evidence'], false, ['flag']);
    default: return make('unknown', 'muted', 'Unknown', [], Boolean(owner), []);
  }
}

// Announced once per change of the §9 state (Running → Paused, Ended…), never on a thread's first render or a poll.
export const stateAnnouncement = (previousKey, status) => (previousKey && previousKey !== status.key ? `Instance ${status.badge.label.toLowerCase()}.` : null);

// ── Camera ──────────────────────────────────────────────────────────────────
// The live URL is a pure function of (run, player, epoch); null means no feed may be requested.
export function cameraTarget(run, playerId, hidden) {
  const ps = run?.players?.[playerId];
  if (hidden || !ps?.launched || !ps.epoch || !LIVE_STATES.has(run.state)) return null;
  return { key: `${run.id}|${playerId}|${ps.epoch}`, src: `/api/runs/${enc(run.id)}/players/${enc(playerId)}/live?epoch=${enc(ps.epoch)}` };
}

// Owns at most one <img>. src is set only when an <img> is created; polls with the same target do nothing.
// A changed target tears the old <img> down (src='' then removed) before the new one exists. Failures retry with backoff.
export function createCamera(frame, { doc = globalThis.document, setTimer = setTimeout, clearTimer = clearTimeout, onChange = () => {} } = {}) {
  let img = null, key = null, target = null, timer = null, failures = 0, status = 'offline';
  const set = next => { if (next !== status) { status = next; onChange(status); } };
  const teardown = () => {
    if (img) { img.src = ''; img.remove(); img = null; }
    key = null;
  };
  const attach = t => {
    img = doc.createElement('img');
    img.className = 'camera-img';
    img.alt = 'Live view of the bot\'s desktop';
    img.addEventListener('load', () => { failures = 0; set('live'); });
    img.addEventListener('error', () => {
      const failed = key;
      teardown();
      failures += 1;
      set('reconnecting');
      // ponytail: exponential backoff capped at 30s; no jitter, one viewer per desktop.
      timer = setTimer(() => { timer = null; if (target?.key === failed) attach(target); }, Math.min(30000, 1000 * 2 ** Math.min(failures - 1, 5)));
    });
    key = t.key;
    img.src = t.src;
    frame.append(img);
    if (status !== 'reconnecting') set('connecting');
  };
  return {
    sync(next) {
      if (!next) { clearTimer(timer); timer = null; teardown(); target = null; failures = 0; set('offline'); return; }
      const same = target?.key === next.key;
      target = next;
      if (same && (img || timer)) return;  // Live, or waiting out a retry: never touch src.
      clearTimer(timer); timer = null;
      teardown();
      if (!same) failures = 0;
      attach(next);
    },
    stop() { this.sync(null); },
    get status() { return status; },
  };
}

// ── Run freshness and commands ──────────────────────────────────────────────
// A GET applies only if nothing newer (a later GET or a control response) has been applied since it started.
export function createRunSlot() {
  let run = null, version = 0;
  return {
    get: () => run,
    set(next) { version += 1; run = next; },
    begin() { const mine = ++version; return value => (mine === version ? ((run = value), true) : false); },
  };
}

// One command ID per control until it succeeds; a retry after a failure reuses it, and a click while pending is ignored.
export async function runCommand(commands, key, send) {
  let cmd = commands.get(key);
  if (cmd?.pending) return { skipped: true };
  if (!cmd) commands.set(key, cmd = { id: newId() });
  cmd.pending = true;
  cmd.error = '';
  try {
    const value = await send(cmd.id);
    commands.delete(key);
    return { value };
  } catch (error) {
    cmd.pending = false;
    cmd.error = error.message;
    return { error };
  }
}

// ── Markup ──────────────────────────────────────────────────────────────────
const panel = (id, body) => `<section class="thread-panel" role="tabpanel" id="panel-${id}" aria-labelledby="tab-${id}" tabindex="0" hidden>${body}</section>`;
// Mounted once per run. The panels' <details> and cards are never re-created, so their open state survives polls and tab switches.
export const threadSkeleton = () => `<div class="thread">`
  + `<div class="thread-head" data-region="head"></div>`
  + `<div class="thread-body">`
  + `<div data-region="banner"></div>`
  + `<div class="thread-sticky"><div class="control-wrap" data-region="controls"></div><div data-region="tabs"></div></div>`
  + `<div data-region="instruction"></div>`
  + panel('bot', `<div data-region="players"></div><div class="camera"><div class="camera-media" data-camera></div><div class="camera-overlay" data-region="overlay"></div></div>`)
  + panel('goals', `<div class="insp-body" data-region="goals"></div>`)
  + panel('world', `<div class="insp-body" data-region="world"></div>`)
  + panel('actions', `<details class="insp-section" open><summary class="insp-head">${icon('chevron-right', 'icon chevron')}<span data-region="issues-head">Issues</span></summary><div class="insp-body" data-region="issues"></div></details>`
    + `<details class="insp-section"><summary class="insp-head">${icon('chevron-right', 'icon chevron')}Model usage</summary><div class="insp-body" data-region="usage"></div></details>`
    + `<section class="activity" aria-labelledby="activity-heading"><div class="activity-head"><h2 id="activity-heading" class="section-title">Activity</h2><div class="tl-tools" data-region="tl-tools"></div></div>`
    + `<div class="tl-top" data-region="tl-top"></div><div class="tl-pill-wrap" data-region="tl-pill"></div><ol class="timeline" data-timeline></ol><div class="tl-foot" data-region="tl-foot"></div></section>`)
  + `</div></div>`;

// Standard ARIA tabs: only the selected tab is in the Tab order.
export const tabsHTML = active => `<div class="thread-tabs" role="tablist" aria-label="Instance views">`
  + THREAD_TABS.map(([id, label, iconName]) => `<button type="button" class="thread-tab" role="tab" id="tab-${id}" aria-controls="panel-${id}" aria-selected="${id === active}" tabindex="${id === active ? 0 : -1}"`
    + ` data-action="thread-tab" data-tab="${id}" data-key="tab-${id}">${icon(iconName)}${label}</button>`).join('')
  + `</div>`;

const ACTIONS = {
  pause: { label: 'Pause', icon: 'pause', cls: 'btn-secondary' },
  takeover: { label: 'Take over', icon: 'hand', cls: 'btn-secondary' },
  reconcile: { label: 'Reconcile teardown', icon: 'refresh', cls: 'btn-primary' },
  report: { label: 'Report', icon: 'file-text', cls: 'btn-secondary' },
  evidence: { label: 'Evidence', icon: 'folder-open', cls: 'btn-secondary' },
  recalculate: { label: 'Recalculate goals', icon: 'refresh' },
  extend: { label: 'Extend investigation', icon: 'clock' },
  flag: { label: 'Flag issue', icon: 'flag' },
  stop: { label: 'Stop', icon: 'stop', cls: 'btn-danger' },
};

export const issuesHead = issues => (issues.length ? `Issues · ${issues.length}` : 'Issues');

export function issuesHTML(issues, { truncated, issueOpen = new Set(), projectId, runId }) {
  if (!issues.length) return `<p class="hint">No issues recorded in this instance. ASHAD logs unsupported actions, crashes and game bugs here as they happen.</p>`;
  const rows = issues.map(issue => {
    const counts = [`${issue.hit_count} ${issue.hit_count === 1 ? 'time' : 'times'} in this instance`]
      .concat(issue.occurrences_total != null && issue.occurrences_total !== issue.hit_count ? [`${issue.occurrences_total} in all instances`] : []);
    const hits = issue.hits.map(hit => `<li class="issue-hit"><time data-ts="${esc(hit.at)}"></time><span>${esc(humanize(hit.source))}</span>`
      + `<span>${hit.kept ? 'Example kept' : 'Counted only'}</span></li>`).join('');
    const more = issue.hit_count > issue.hits.length ? `<p class="hint">Showing the newest ${issue.hits.length} of ${issue.hit_count} hits</p>` : '';
    return `<li class="issue-row"><p class="issue-title"><span class="issue-type">${esc(typeLabel(issue.type))}</span> <strong>${projectId ? `<a href="${href('p', projectId, 'issues', issue.issue_id)}" data-key="it-${esc(issue.issue_id)}">${esc(issue.title)}</a>` : esc(issue.title)}</strong></p>`
      + `<p class="issue-meta">${esc(counts.join(' · '))} · last <time data-ts="${esc(issue.last_at)}"></time> · ${esc(humanize(issue.status))}</p>`
      + (issue.file ? `<p class="issue-file"><span>File</span> <code>${esc(issue.file)}</code></p>` : '')
      + `<details class="issue-hits"${issueOpen.has(issue.issue_id) ? ' open' : ''} data-issue="${esc(issue.issue_id)}"><summary data-key="issue-${esc(issue.issue_id)}">Hits</summary><ol>${hits}</ol>${more}</details></li>`;
  }).join('');
  return (projectId ? `<p class="issue-board-link"><a href="${href('p', projectId, 'issues')}?run=${encodeURIComponent(runId)}" data-key="issues-board">View in project Issues</a></p>` : '')
    + `<ol class="issue-list">${rows}</ol>`
    + (truncated ? `<p class="hint">Only the newest 500 issue events were read.</p>` : '');
}

const MODEL_CHANGE_STATES = new Set(['Queued', 'Starting', 'Running', 'Paused', 'Investigating']);

export function threadRegions(run, ctx) {
  const { status, owner, data, ui, pending, error, cameraStatus, player, tab, issueOpen } = ctx;
  const goal = runGoal(run);
  const agent = (data?.configurations || []).find(a => a.id === runConfigurationId(run)) || run.configuration || run.agent_profile;
  const model = run.model && modelLabel(data, run.model);
  const build = run.build?.name || run.plan?.build_id;
  const transport = { enet: 'ENet', steam: 'Steam' }[run.plan?.transport?.mode];
  const textChip = text => `<span class="chip" title="${esc(text)}"><span class="chip-text">${esc(text)}</span></span>`;
  const chips = [agent?.name, model, build && `build ${build}`, transport].filter(Boolean).map(textChip).join('');
  const modelMenu = modelMenuHTML({ key: 't-model', field: 'run-model', options: providerModels(data, run.model), selected: run.model, defaultModel: run.plan?.model });
  const live = run.model && MODEL_CHANGE_STATES.has(run.state);
  const metaChips = [agent?.name && textChip(agent.name), model && (live ? modelMenu : textChip(model)), build && textChip(`build ${build}`), transport && textChip(transport)].filter(Boolean).join('');
  const head = `<button type="button" class="thread-goal" popovertarget="goal-full" data-key="t-goal" title="Show the full goal">${esc(goal)}</button>`
    + `<div id="goal-full" class="popover goal-pop" popover>${esc(goal)}</div>`
    + badge(status.badge)
    + `<div class="thread-meta">${metaChips}</div>`
    + (chips ? `<button type="button" class="icon-btn meta-more" popovertarget="meta-pop" data-key="t-meta" aria-label="Instance details" title="Instance details">${icon('ellipsis')}</button>`
      + `<div id="meta-pop" class="popover menu meta-pop" popover>${chips}</div>` : '');

  const banner = status.banner
    ? `<div class="thread-banner">${icon('alert')}<div><strong>Needs review.</strong> ${esc(run.pause?.reason || 'The monitor paused the bot. The reason was not recorded.')}`
      + `${run.pause?.recovery ? `<p>${esc(run.pause.recovery)}</p>` : ''}</div></div>`
    : '';

  const ids = playerIds(run);
  const players = ids.length > 1
    ? `<label class="chip chip-select player-switch">${icon('monitor')}<span class="sr-only">Player</span><select data-field="player" data-key="t-player" aria-label="Player">`
      + ids.map(id => `<option value="${esc(id)}"${id === player ? ' selected' : ''}>${esc((run.plan?.players || []).find(p => p.id === id)?.name || id)}</option>`).join('') + `</select></label>`
    : '';

  return { head, banner, players,
    issues: issuesHTML(run.issues || [], { truncated: run.issues_truncated, issueOpen, projectId: run.project_id, runId: run.id }),
    'issues-head': issuesHead(run.issues || []),
    overlay: overlayHTML(run, status, cameraStatus), controls: controlsHTML(status, pending, error), tabs: tabsHTML(tab), instruction: instructionHTML(ui, pending) };
}

function overlayHTML(run, status, cameraStatus) {
  if (status.key === 'ended' || (status.key === 'unconfirmed' && !isActiveRun(run))) {
    const at = run.ended_at ? new Date(run.ended_at * 1000).toLocaleString() : 'time not recorded';
    return `<div class="camera-ended">${icon('monitor')}<strong>Instance ended · camera offline</strong>`
      + `<span>${esc(run.state)} · ${esc(humanize(run.ending_reason) || 'reason not recorded')}</span><span>${esc(at)}</span></div>`;
  }
  const pill = (tone, iconName, text) => `<span class="pill pill-${tone}">${iconName ? icon(iconName) : `<span class="dot dot-${tone}" aria-hidden="true"></span>`}${esc(text)}</span>`;
  const pills = [];
  if (run.human_control) pills.push(pill('attention', 'hand', 'Human control'));
  else if (['Paused', 'Investigating'].includes(run.state)) pills.push(pill('attention', 'alert', 'Paused: bot input held (game may still simulate)'));
  if (cameraStatus === 'live' && !pills.length) pills.push(pill('live', '', 'Live'));
  else if (cameraStatus === 'reconnecting') pills.push(pill('attention', 'loader', 'Camera reconnecting… (doesn\'t mean the game froze)'));
  else if (cameraStatus === 'connecting') pills.push(pill('pending', '', 'Connecting camera…'));
  else if (cameraStatus === 'offline') pills.push(pill('muted', '', 'Camera offline'));
  const waiting = cameraStatus === 'offline' ? `<div class="camera-wait">${icon('monitor')}<span>${status.key === 'starting' ? 'Waiting for the desktop to start…' : 'No camera feed'}</span></div>` : '';
  return `<div class="camera-pills">${pills.join('')}</div>${waiting}`;
}

function button(action, pending, extra = '') {
  const a = ACTIONS[action];
  const busy = pending.has(action);
  return `<button type="button" class="btn ${a.cls}" data-action="ctl" data-ctl="${action}" data-key="ctl-${action}"${busy ? ' aria-busy="true" aria-disabled="true"' : ''}${extra}>`
    + `${icon(busy ? 'loader' : a.icon, busy ? 'icon spin' : 'icon')}${a.label}</button>`;
}

// Split buttons: the main half does the common thing, the caret opens the alternative.
function split(action, label, iconName, alts, pending) {
  const busy = pending.has(action) || alts.some(([a]) => pending.has(a));
  return `<span class="split"><button type="button" class="btn btn-primary" data-action="ctl" data-ctl="${action}" data-key="ctl-${action}"${busy ? ' aria-busy="true" aria-disabled="true"' : ''}>`
    + `${icon(busy ? 'loader' : iconName, busy ? 'icon spin' : 'icon')}${label}</button>`
    + `<button type="button" class="btn btn-primary split-caret" popovertarget="menu-${action}" data-key="ctl-${action}-more" aria-label="More ${label} options" title="More options">${icon('chevron-down')}</button>`
    + `<div id="menu-${action}" class="popover menu" popover>${alts.map(([a, l]) => `<button type="button" class="menu-item" data-action="ctl" data-ctl="${a}" data-key="ctl-${a}">${esc(l)}</button>`).join('')}</div></span>`;
}

function controlsHTML(status, pending, error) {
  const left = status.left.map(action => action === 'resume' ? split('resume', 'Resume', 'play', [['resume-with', 'Resume with an instruction…']], pending)
    : action === 'handback' ? split('handback', 'Hand back', 'play', [['handback-with', 'Hand back with an instruction…'], ['handback-paused', 'Hand back, keep bots paused']], pending)
    : button(action, pending)).join('');
  const menuBusy = status.menu.some(action => pending.has(action));
  const menu = status.menu.length
    ? `<button type="button" class="icon-btn" popovertarget="menu-more" data-key="ctl-more" aria-label="More actions" title="More actions"${menuBusy ? ' aria-busy="true"' : ''}>${icon(menuBusy ? 'loader' : 'ellipsis', menuBusy ? 'icon spin' : 'icon')}</button>`
      + `<div id="menu-more" class="popover menu" popover>${status.menu.map(action => `<button type="button" class="menu-item" data-action="ctl" data-ctl="${action}" data-key="ctl-${action}"${pending.has(action) ? ' aria-disabled="true"' : ''}>${icon(ACTIONS[action].icon)}${ACTIONS[action].label}</button>`).join('')}</div>`
    : '';
  const stop = status.stop ? button('stop', pending) : '';
  const note = status.note ? `<span class="control-note">${icon('loader', 'icon spin')}${esc(status.note)}</span>` : '';
  const bar = `<div class="control-bar" role="toolbar" aria-label="Instance controls">${left}${note}<span class="control-right">${menu}${stop}</span></div>`;
  return bar + (error ? `<p class="note note-error control-error" data-announce>${icon('alert')}<span>${esc(error)} Try again to retry the same command.</span></p>` : '');
}

function instructionHTML(ui, pending) {
  if (!ui.instructionOpen) return '';
  const handback = ui.instructionOpen === 'handback';
  return `<form class="instruction" data-form="resume-instruction"><label for="resume-reason">Recovery instruction</label>`
    // The typed text is not in the markup (app.js restores it), so a poll never re-creates the field mid-typing.
    + `<textarea id="resume-reason" class="textarea" name="reason" rows="2" required maxlength="4000" data-key="ins-text" placeholder="${handback ? 'For example: I opened the shop menu; buy the hoe, then return to the field' : 'For example: try the path past the bridge before returning to the objective'}"></textarea>`
    + `<p class="hint">Used until the next resume.</p><div class="instruction-actions">`
    + `<button type="button" class="btn btn-ghost" data-action="instruction-cancel" data-key="ins-cancel">Cancel</button>`
    + `<button type="submit" class="btn btn-primary" data-key="ins-send"${pending.has(handback ? 'handback-with' : 'resume-with') ? ' aria-busy="true" aria-disabled="true"' : ''}>${icon('play')}${handback ? 'Hand back' : 'Resume'} with instruction</button></div></form>`;
}
