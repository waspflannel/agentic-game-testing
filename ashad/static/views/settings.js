// Settings (spec §8.8). Models, validation and markup are pure; app.js owns the DOM and I/O.
// Forms live in each section's skeleton (mounted once per section and edited record), so polls never touch typed input.
import { esc, icon, since, badge } from '../core.js';

const enc = encodeURIComponent;
export const SECTIONS = [['account', 'Account'], ['desktops', 'Desktops'], ['steam', 'Steam accounts'], ['limits', 'Limits']];
const errorNote = text => (text ? `<p class="note note-error" data-announce>${icon('alert')}<span>${esc(text)}</span></p>` : '');
const okNote = text => (text ? `<p class="note">${icon('check')}<span>${esc(text)}</span></p>` : '');
const disabled = reason => (reason ? ` aria-disabled="true" title="${esc(reason)}"` : '');

export function settingsSkeleton(section, inner) {
  const nav = SECTIONS.map(([id, label]) => `<a class="row settings-link${id === section ? ' is-current' : ''}" href="#/settings/${id}" data-key="sn-${id}"${id === section ? ' aria-current="page"' : ''}>${esc(label)}</a>`).join('');
  return `<div class="settings-page"><nav class="settings-nav" aria-label="Settings sections">${nav}</nav><div class="settings-body">${inner}</div></div>`;
}
const head = (title, lede) => `<div class="page-head"><h1>${esc(title)}</h1>${lede ? `<p>${esc(lede)}</p>` : ''}</div>`;

// ── Account ─────────────────────────────────────────────────────────────────
const AI_LABELS = { ready: 'Signed in', configuration_required: 'Not signed in', login_pending: 'Sign-in pending', not_validated: 'Not checked yet',
  invalid_authentication: 'Sign-in expired', model_unavailable: 'Model unavailable', connection_failure: 'Cannot reach provider', rate_limited: 'Rate limited', provider_failure: 'Provider error' };

// Only the official HTTPS sign-in hosts the main process also allows; anything else is never offered.
export function officialLoginUrl(value) {
  let u;
  try { u = new URL(value); } catch { return null; }
  return u.protocol === 'https:' && !u.username && !u.password && !u.port && ['auth.openai.com', 'chatgpt.com'].includes(u.hostname) ? u.href : null;
}

// The sign-in link is kept only while the backend still reports a pending sign-in.
export const settleLogin = (login, ai) => (ai?.status === 'login_pending' ? login : null);

// Mirrors the backend's sign-in guard, so the buttons say why before a click is refused.
export function accountBlocker(data, controls) {
  if (controls) return 'Wait for Get game controls to finish before changing sign-in.';
  const runs = data?.runs || [];
  if (runs.some(r => ['Queued', 'Starting', 'Running', 'Stopping'].includes(r.state))) return 'Pause or stop the active instance before changing sign-in.';
  if (runs.some(r => r.goal_recalculation?.status === 'planning')) return 'Wait for goal recalculation to finish before changing sign-in.';
  return '';
}

// a: { ai, claude, login, blocker, checking, busy, error }. The sign-in link exists only while a login is pending.
export function accountRegions(a) {
  const ai = a.ai || {};
  const status = ai.status || 'not_validated';
  const tone = status === 'ready' ? 'live' : status === 'login_pending' ? 'pending' : 'attention';
  const facts = [['Status', badge({ tone, label: AI_LABELS[status] || status })],
    ['Billing', 'ChatGPT subscription'], ['Model', esc(a.modelLabel || ai.model || 'Unknown')], ...(ai.plan_type ? [['Plan', esc(ai.plan_type)]] : [])];
  const pending = a.login || status === 'login_pending';
  const url = a.login && officialLoginUrl(a.login.auth_url);
  const actions = [];
  if (status === 'ready') actions.push(`<button type="button" class="btn btn-secondary" data-action="account-logout" data-key="acc-logout"${disabled(a.blocker || (a.busy && 'Working…'))}>${icon('log-out')}Sign out</button>`);
  else if (!pending) actions.push(`<button type="button" class="btn btn-primary" data-action="account-login" data-key="acc-login"${disabled(a.blocker || (a.busy && 'Working…'))}>${icon('log-in')}Sign in with ChatGPT</button>`);
  if (pending) actions.push(`<button type="button" class="btn btn-secondary" data-action="account-cancel" data-key="acc-cancel"${disabled(a.busy && 'Working…')}>Cancel sign-in</button>`);
  actions.push(`<button type="button" class="btn btn-ghost" data-action="account-refresh" data-key="acc-refresh"${a.checking ? ' aria-busy="true" aria-disabled="true"' : ''}>${icon(a.checking ? 'loader' : 'refresh', a.checking ? 'icon spin' : 'icon')}Refresh status</button>`);
  const link = !a.login ? '' : url
    ? `<div class="signin-step"><p>Finish signing in with your browser. ASHAD notices when it completes.</p><a class="btn btn-primary" href="${esc(url)}" target="_blank" rel="noopener noreferrer" data-key="acc-open">${icon('log-in')}Open ChatGPT sign-in</a></div>`
    : errorNote('ASHAD returned a sign-in address that is not an official ChatGPT page, so it is not offered. Cancel sign-in and try again.');
  const claude = a.claude || {};
  const cStatus = claude.status || 'not_validated';
  const cTone = cStatus === 'ready' ? 'live' : 'attention';
  const cFacts = [['Status', badge({ tone: cTone, label: AI_LABELS[cStatus] || cStatus })], ['Billing', 'Claude subscription']];
  const cDisabled = disabled(a.blocker || (a.busy && 'Working…'));
  const claudeActions = `<form class="claude-token-form" data-form="claude-token" novalidate><div class="field"><label for="claude-token">Claude token</label>`
    + `<input id="claude-token" class="input mono" type="password" autocomplete="off" name="claude-token" spellcheck="false" placeholder="Paste token from claude setup-token">`
    + `<span class="hint">Write-only: stored in the OS credential store and never shown again.</span></div>`
    + `<div class="btn-row"><button type="button" class="btn btn-primary" data-action="claude-token-save" data-key="claude-save"${cDisabled}>Save token</button>`
    + `<button type="button" class="btn btn-secondary" data-action="claude-token-remove" data-key="claude-remove"${cDisabled}>Remove token</button></div></form>`
    + (a.blocker ? `<p class="note">${icon('info')}<span>${esc(a.blocker)}</span></p>` : '')
    + `<p class="hint">Personal use of your own subscription through the unmodified Claude CLI. See Anthropic's legal and compliance terms.</p>${errorNote(a.claudeError)}`;
  return {
    'claude-status': `<dl class="kv">${cFacts.map(([k, v]) => `<dt>${k}</dt><dd>${v}</dd>`).join('')}</dl>${claude.message ? `<p class="hint">${esc(claude.message)}</p>` : ''}`,
    'claude-actions': claudeActions,
    status: `<dl class="kv">${facts.map(([k, v]) => `<dt>${k}</dt><dd>${v}</dd>`).join('')}</dl>${ai.message ? `<p class="hint">${esc(ai.message)}</p>` : ''}`,
    actions: `<div class="btn-row">${actions.join('')}</div>${a.blocker && !pending ? `<p class="note">${icon('info')}<span>${esc(a.blocker)}</span></p>` : ''}${link}${errorNote(a.error)}`,
  };
}

export const accountSkeleton = () => head('Account', 'ASHAD uses your ChatGPT (Codex) or Claude subscription. There are no API keys.')
  + `<section class="card settings-card"><h2 class="card-title">ChatGPT</h2><div data-region="status"></div><div data-region="actions" class="settings-actions"></div></section>`
  + `<section class="card settings-card"><h2 class="card-title">Claude</h2><div data-region="claude-status"></div><div data-region="claude-actions" class="settings-actions"></div></section>`;

// ── Desktops ────────────────────────────────────────────────────────────────
export const leaseOf = (data, kind, id) => (data?.leases || []).find(l => l.kind === kind && l.resource_id === id);
const leaseReason = lease => (lease ? `In use by instance ${lease.run_id || 'unknown'}; available after it ends.` : '');

export function desktopsSkeleton(data, editId) {
  const w = editId ? (data?.workers || []).find(x => x.id === editId) : null;
  const val = key => esc(w?.[key] || '');
  const form = `<form class="card settings-card settings-form" data-form="settings-worker" data-id="${esc(w?.id || '')}" novalidate>`
    + `<h2 class="card-title">${w ? `Edit ${esc(w.name || w.id)}` : 'Add a desktop'}</h2>`
    + field('w-name', 'Name', `<input id="w-name" class="input" name="name" maxlength="200" value="${val('name')}" autocomplete="off">`)
    + field('w-url', 'Service URL', `<input id="w-url" class="input mono" name="url" required value="${val('url')}" placeholder="http://192.168.1.20:8101" autocomplete="off" spellcheck="false">`, 'The worker\'s private HTTP(S) address, without credentials.')
    + field('w-token', 'Service token', `<input id="w-token" class="input mono" name="token" type="password" autocomplete="new-password" spellcheck="false" placeholder="${w ? 'Stored. Leave blank to keep' : 'Required for a new desktop'}">`,
      w ? 'Write-only. A moved URL needs the token again; a stored token is never sent to a different address.' : 'Write-only: stored in the system credential store and never shown again.')
    + field('w-desktop', 'Desktop viewer URL (optional)', `<input id="w-desktop" class="input mono" name="desktop_url" value="${val('desktop_url')}" autocomplete="off" spellcheck="false">`)
    + field('w-host', 'Host address (optional)', `<input id="w-host" class="input mono" name="host_address" value="${val('host_address')}" autocomplete="off" spellcheck="false">`)
    + `<label class="check"><input type="checkbox" name="enabled"${w?.enabled === false ? '' : ' checked'}> Enabled</label>`
    + `<div data-region="form-status"></div><div class="btn-row" data-region="form-actions"></div></form>`;
  return head('Desktops', 'Worker desktops run the game for an instance. One instance at a time in this release.')
    + `<section class="card settings-card"><h2 class="card-title">Registered desktops</h2><div data-region="list"></div></section>${form}`;
}

// ui: { probing: Set, errors: Map, editId, saving, saved, formError }
export function desktopsRegions(data, ui, now) {
  const workers = data?.workers || [];
  const rows = workers.map(w => {
    const lease = leaseOf(data, 'worker', w.id);
    const probe = w.last_probe;
    const ok = probe && probe.status !== 'unavailable';
    const probeText = !probe ? 'Not probed yet' : `${ok ? 'Reachable' : 'Unavailable'}${probe.message ? `: ${probe.message}` : ''} · ${since(w.last_probe_at, now)}`;
    const busy = ui.probing.has(w.id);
    return `<li class="list-row"><div class="list-main"><span class="list-title">${esc(w.name || w.id)}${w.enabled === false ? ' <span class="tag-muted">Disabled</span>' : ''}${lease ? ' <span class="tag-muted">In use</span>' : ''}</span>`
      + `<span class="mono check-detail">${esc(w.url || '')}</span><span class="check-detail">${probe ? (ok ? icon('check', 'icon check-ok') : icon('alert', 'icon check-bad')) : ''}${esc(probeText)}</span>`
      + `${errorNote(ui.errors.get(w.id))}</div><div class="list-actions">`
      + `<button type="button" class="btn btn-ghost btn-sm" data-action="worker-probe" data-id="${esc(w.id)}" data-key="wp-${esc(w.id)}"${busy ? ' aria-busy="true" aria-disabled="true"' : ''}>${icon(busy ? 'loader' : 'refresh', busy ? 'icon spin' : 'icon')}Probe</button>`
      + `<a class="btn btn-ghost btn-sm" href="#/settings/desktops?edit=${enc(w.id)}" data-key="we-${esc(w.id)}">Edit</a>`
      + `<button type="button" class="btn btn-ghost btn-sm" data-action="worker-delete" data-id="${esc(w.id)}" data-key="wd-${esc(w.id)}"${disabled(leaseReason(lease))}>Delete</button></div>`
      + (lease ? `<p class="list-reason">${esc(leaseReason(lease))}</p>` : '') + `</li>`;
  }).join('');
  const editLease = ui.editId && leaseOf(data, 'worker', ui.editId);
  return {
    list: rows ? `<ul class="list">${rows}</ul>` : `<p class="hint">No desktop is registered. Setup can prepare one on this computer, or add a remote one below.</p>`,
    'form-status': (editLease ? `<p class="note">${icon('info')}<span>${esc(leaseReason(editLease))} Editing is disabled until then.</span></p>` : '') + errorNote(ui.formError) + okNote(ui.saved),
    'form-actions': `<button type="submit" class="btn btn-primary" data-key="w-save"${disabled(leaseReason(editLease) || (ui.saving && 'Saving…'))}${ui.saving ? ' aria-busy="true"' : ''}>Save desktop</button>`
      + (ui.editId ? `<a class="btn btn-ghost" href="#/settings/desktops" data-key="w-cancel">Cancel</a>` : ''),
  };
}

// Only fields with values are sent; a blank token means "keep the stored one".
export function workerPayload(values, id) {
  const text = key => String(values[key] ?? '').trim();
  const body = { url: text('url'), enabled: values.enabled === true };
  if (id) body.id = id;
  for (const key of ['name', 'desktop_url', 'host_address', 'token']) if (text(key)) body[key] = text(key);
  const errors = {};
  if (!/^https?:\/\/[^\s/@]+/i.test(body.url)) errors.url = 'Enter an http:// or https:// address without credentials.';
  if (!id && !body.token) errors.token = 'A new desktop needs its service token.';
  return { body, errors };
}

// ── Steam accounts ──────────────────────────────────────────────────────────
export function steamErrors(values) {
  const errors = {};
  const steamId = String(values.steam_id ?? '').trim();
  const appId = String(values.app_id ?? '').trim();
  if (steamId && !/^\d+$/.test(steamId)) errors.steam_id = 'A Steam ID64 has digits only.';
  if (appId === '480') errors.app_id = 'App ID 480 (Spacewar) is a test app and is rejected. Use the game\'s own App ID.';
  else if (appId && !/^[1-9]\d*$/.test(appId)) errors.app_id = 'Enter the game\'s numeric App ID.';
  return errors;
}

export function steamSkeleton(data) {
  const workers = data?.workers || [];
  const options = workers.map(w => `<option value="${esc(w.id)}">${esc(w.name || w.id)}</option>`).join('');
  return head('Steam accounts', 'Steam transport needs a licensed account signed in on a persistent desktop. ENet needs none of this.')
    + `<section class="card settings-card"><h2 class="card-title">Accounts</h2><div data-region="list"></div></section>`
    + `<form class="card settings-card settings-form" data-form="settings-steam" novalidate><h2 class="card-title">Add an account</h2>`
    + field('s-name', 'Name', `<input id="s-name" class="input" name="name" required maxlength="200" autocomplete="off">`)
    + field('s-worker', 'Desktop', workers.length ? `<select id="s-worker" class="select" name="worker_id">${options}</select>` : `<select id="s-worker" class="select" name="worker_id" disabled><option value="">Add a desktop first</option></select>`, 'Each account lives on one persistent desktop.')
    + field('s-steam', 'Steam ID64', `<input id="s-steam" class="input mono" name="steam_id" required inputmode="numeric" autocomplete="off" aria-describedby="s-steam-error">`, '', 's-steam-error')
    + field('s-app', 'Game App ID', `<input id="s-app" class="input mono" name="app_id" required inputmode="numeric" autocomplete="off" aria-describedby="s-app-error">`, 'The App ID of the game build, not 480.', 's-app-error')
    + `<div data-region="form-status"></div><div class="btn-row"><button type="submit" class="btn btn-primary" data-key="s-save">Add account</button></div></form>`;
}

// ui: { busy: Map(accountId -> action), results: Map(accountId -> {ok, text}), formError, saved }
export function steamRegions(data, ui, now) {
  const workers = new Map((data?.workers || []).map(w => [w.id, w]));
  const rows = (data?.accounts || []).map(a => {
    const lease = leaseOf(data, 'worker', a.worker_id) || leaseOf(data, 'account', a.id);
    const reason = leaseReason(lease);
    const builds = (data?.builds || []).filter(b => String(b.steam_app_id) === String(a.app_id));
    const busy = ui.busy.get(a.id);
    const btn = (action, label, extra = '') => `<button type="button" class="btn btn-ghost btn-sm" data-action="steam-${action}" data-id="${esc(a.id)}" data-key="s${action}-${esc(a.id)}"${busy === action ? ' aria-busy="true" aria-disabled="true"' : disabled(reason || extra)}>${busy === action ? icon('loader', 'icon spin') : ''}${label}</button>`;
    const result = ui.results.get(a.id);
    const worker = workers.get(a.worker_id);
    return `<li class="list-row"><div class="list-main"><span class="list-title">${esc(a.name || a.id)} <span class="tag-muted">${a.readiness === 'ready' ? 'Verified' : 'Sign-in needed'}</span></span>`
      + `<span class="mono check-detail">Steam ID ${esc(a.steam_id)} · App ID ${esc(a.app_id)}</span>`
      + `<span class="check-detail">On ${esc(worker?.name || a.worker_id)}${a.last_check ? ` · checked ${esc(since(a.last_check, now))}` : ''}</span>`
      + (result ? (result.ok ? okNote(result.text) : errorNote(result.text)) : '')
      + (result?.desktop ? `<a class="btn btn-secondary btn-sm" href="${esc(result.desktop)}" target="_blank" rel="noopener noreferrer" data-key="sopen-${esc(a.id)}">${icon('monitor')}Open desktop</a>` : '')
      + `</div><div class="list-actions">${btn('start', 'Start sign-in')}${btn('finish', 'Finish')}${btn('verify', 'Verify…', builds.length ? '' : `No registered build has App ID ${a.app_id}.`)}`
      + `<button type="button" class="btn btn-ghost btn-sm" data-action="steam-delete" data-id="${esc(a.id)}" data-key="sdel-${esc(a.id)}"${disabled(reason)}>Delete</button></div>`
      + (reason ? `<p class="list-reason">${esc(reason)}</p>` : '') + `</li>`;
  }).join('');
  return {
    list: rows ? `<ul class="list">${rows}</ul>` : `<p class="hint">No Steam accounts yet.</p>`,
    'form-status': errorNote(ui.formError) + okNote(ui.saved),
  };
}

// ── Limits ──────────────────────────────────────────────────────────────────
const GB = 1024 ** 3;
const gb = bytes => (Number.isFinite(bytes) ? `${(bytes / GB).toFixed(1)} GB` : 'Unknown');

export function limitsSkeleton(data) {
  const s = data?.settings || {};
  const minutes = s.duration_seconds ? String(Math.round(s.duration_seconds / 60)) : '';
  return head('Limits', 'Guards for every instance. Leave a field blank for no limit.')
    + `<form class="card settings-card settings-form" data-form="settings-limits" novalidate>`
    + field('l-duration', 'Session duration (minutes)', `<input id="l-duration" class="input" name="duration" inputmode="decimal" value="${esc(minutes)}" placeholder="No limit" aria-describedby="l-duration-error">`, 'An instance stops when it runs this long.', 'l-duration-error')
    + field('l-spend', 'Spend cap (USD, API-equivalent estimate)', `<input id="l-spend" class="input" name="spend" inputmode="decimal" value="${esc(s.spend_usd ?? '')}" placeholder="No limit" aria-describedby="l-spend-error">`, 'Estimates only apply to metered rows; subscription turns have no price.', 'l-spend-error')
    + field('l-fixer', 'Fixer time per day (minutes)', `<input id="l-fixer" class="input" name="fixer" inputmode="numeric" value="${esc(s.fixer_daily_minutes ?? 240)}" aria-describedby="l-fixer-error">`, "The fixer stops for the day when it has run this long. It also stops when your Claude plan's usage limit is reached.", 'l-fixer-error')
    + field('l-reserve', 'Storage reserve (GB)', `<input id="l-reserve" class="input" name="reserve" inputmode="decimal" value="${esc(Number.isFinite(s.storage_reserve_bytes) ? +(s.storage_reserve_bytes / GB).toFixed(2) : '')}" aria-describedby="l-reserve-error">`, 'Free disk space kept back for evidence and saves.', 'l-reserve-error')
    + `<p class="hint">Concurrent instances: 1 (fixed in this release).</p>`
    + `<div data-region="form-status"></div><div class="btn-row"><button type="submit" class="btn btn-primary" data-key="l-save">Save limits</button></div></form>`
    + `<section class="card settings-card"><h2 class="card-title">This computer</h2><div data-region="hardware"></div></section>`;
}

export function limitsPayload(values) {
  const errors = {};
  const num = (key, name, scale = 1, allowZero = false) => {
    const raw = String(values[key] ?? '').trim();
    if (!raw) return null;
    const n = Number(raw);
    if (!Number.isFinite(n) || n < 0 || (!allowZero && n === 0)) { errors[key] = `${name} must be a positive number${allowZero ? ' or 0' : ''}.`; return undefined; }
    return n * scale;
  };
  const duration = num('duration', 'Duration', 60);
  const spend = num('spend', 'Spend cap');
  const reserve = num('reserve', 'Storage reserve', GB, true);
  const fixerRaw = String(values.fixer ?? '').trim();
  const fixer = /^\d+$/.test(fixerRaw) ? Number(fixerRaw) : NaN;
  if (!(fixer >= 1 && fixer <= 1440)) errors.fixer = 'Fixer time must be a whole number of minutes from 1 to 1440.';
  if (reserve === null) errors.reserve = 'Enter a reserve, or 0 for none.';
  return { errors, body: { duration_seconds: duration, spend_usd: spend, storage_reserve_bytes: Number.isFinite(reserve) ? Math.round(reserve) : reserve, fixer_daily_minutes: fixer } };
}

export function limitsRegions(data, ui) {
  const h = data?.settings?.hardware || {};
  const pct = v => (Number.isFinite(v) ? `${Math.round(v)}%` : 'Unknown');
  return {
    hardware: `<dl class="kv"><dt>CPU</dt><dd>${pct(h.cpu_percent)} in use</dd><dt>Memory</dt><dd>${pct(h.memory_percent)} in use · ${gb(h.memory_available)} available</dd><dt>Disk free</dt><dd>${gb(h.disk_free)}</dd><dt>GPU</dt><dd>${h.gpu ? esc(JSON.stringify(h.gpu)) : 'Not reported'}</dd></dl>`,
    'form-status': errorNote(ui.formError) + okNote(ui.saved),
  };
}

function field(id, label, control, hint = '', errorId = '') {
  return `<div class="field"><label for="${id}">${esc(label)}</label>${control}${hint ? `<span class="hint">${esc(hint)}</span>` : ''}<span class="error" id="${errorId || id + '-error'}" data-error-for="${id}"></span></div>`;
}

