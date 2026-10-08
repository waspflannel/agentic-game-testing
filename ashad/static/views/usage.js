// Usage page (spec §8.7). Fetched on view open and Refresh only; the model and markup are pure.
import { esc, icon, href, ago, humanize } from '../core.js';

export const PAGE_ROWS = 200;
export const RANGES = [['7d', '7 days', 7], ['30d', '30 days', 30], ['all', 'All time', null]];
const COLUMNS = [['time', 'Time'], ['purpose', 'Purpose'], ['model', 'Model'], ['run', 'Instance'], ['tokens', 'Tokens'], ['estimate', 'Estimate'], ['status', 'Status']];
export const newUsageUi = () => ({ range: '7d', model: '', purpose: '', sort: 'time', dir: 'desc', limit: PAGE_ROWS });

// Total = input + output; cached input and reasoning output are subsets and never added again.
export const rowTokens = r => {
  const u = r?.usage;
  if (Number.isFinite(u?.total_tokens)) return u.total_tokens;
  return Number.isFinite(u?.input_tokens) && Number.isFinite(u?.output_tokens) ? u.input_tokens + u.output_tokens : null;
};
const cost = r => (Number.isFinite(r?.estimated_cost_usd) ? r.estimated_cost_usd : null);

export function usageModel(rows, ui, now = Date.now() / 1000) {
  const days = RANGES.find(([id]) => id === ui.range)?.[2];
  const inRange = (rows || []).filter(r => !days || (r.timestamp || 0) >= now - days * 86400);
  const filtered = inRange.filter(r => (!ui.model || r.model === ui.model) && (!ui.purpose || r.purpose === ui.purpose));
  const counted = filtered.map(rowTokens).filter(n => n !== null);
  const costs = filtered.map(cost).filter(n => n !== null);
  const key = { time: r => r.timestamp || 0, purpose: r => r.purpose || '', model: r => r.model || '', run: r => r.run_id || '', tokens: r => rowTokens(r) ?? -1, estimate: r => cost(r) ?? -1, status: r => r.status || '' }[ui.sort] || (r => r.timestamp || 0);
  const sorted = [...filtered].sort((a, b) => { const x = key(a), y = key(b); return (x < y ? -1 : x > y ? 1 : 0) * (ui.dir === 'asc' ? 1 : -1); });
  return {
    turns: filtered.length,
    tokens: counted.reduce((a, b) => a + b, 0),
    uncounted: filtered.length - counted.length,
    estimate: costs.length ? costs.reduce((a, b) => a + b, 0) : null,  // null: nothing priced (subscription), never zero.
    rows: sorted.slice(0, ui.limit),
    remaining: Math.max(0, sorted.length - ui.limit),
    models: [...new Set(inRange.map(r => r.model).filter(Boolean))].sort(),
    purposes: [...new Set(inRange.map(r => r.purpose).filter(Boolean))].sort(),
  };
}

const fmt = n => n.toLocaleString('en-US');
const money = n => `$${n.toFixed(n < 1 ? 4 : 2)}`;
const NOT_A_CHARGE = 'Estimate, not a charge. Billed through your ChatGPT subscription.';

export const usageSkeleton = () => `<div class="page usage-page"><div class="page-head row-between"><div><h1>Usage</h1><p>Every model turn ASHAD recorded: gameplay, planning and tuning.</p></div><div data-region="refresh"></div></div>`
  + `<div data-region="summary"></div><div data-region="controls"></div><div data-region="table"></div></div>`;

// usage: { loading, rows, error, at } or null before the first fetch.
export function usageRegions(usage, ui, modelLabels, now = Date.now() / 1000) {
  const refresh = `<span class="hint">${usage?.at ? `Fetched ${ago(usage.at, now) === 'now' ? 'just now' : `${ago(usage.at, now)} ago`}` : ''}</span>`
    + `<button type="button" class="btn btn-secondary" data-action="usage-refresh" data-key="u-refresh"${usage?.loading ? ' aria-busy="true" aria-disabled="true"' : ''}>${icon(usage?.loading ? 'loader' : 'refresh', usage?.loading ? 'icon spin' : 'icon')}Refresh</button>`;
  if (!usage?.rows) {
    const body = usage?.error ? `<p class="note note-error" data-announce>${icon('alert')}<span>Usage is unavailable: ${esc(usage.error)}. Refresh to try again.</span></p>`
      : `<p class="note">${icon('loader', 'icon spin')}<span>Loading usage…</span></p>`;
    return { refresh, summary: summaryHTML(null), controls: '', table: body };
  }
  const m = usageModel(usage.rows, ui, now);
  const label = id => modelLabels.get(id) || id || 'Unknown';
  const seg = RANGES.map(([id, text]) => `<button type="button" class="seg-btn" data-action="usage-range" data-range="${id}" data-key="u-range-${id}" aria-pressed="${ui.range === id}">${text}</button>`).join('');
  const select = (field, all, values, current, name) => `<label class="chip chip-select"><span class="sr-only">${name}</span><select data-field="${field}" data-key="u-${field}" aria-label="${name}">`
    + `<option value="">${all}</option>${values.map(v => `<option value="${esc(v)}"${v === current ? ' selected' : ''}>${esc(field === 'usage-model' ? label(v) : humanize(v))}</option>`).join('')}</select></label>`;
  const controls = `<div class="usage-controls"><div class="seg" role="group" aria-label="Range">${seg}</div>`
    + select('usage-model', 'All models', m.models, ui.model, 'Model') + select('usage-purpose', 'All purposes', m.purposes, ui.purpose, 'Purpose') + `</div>`;
  const th = ([key, text]) => `<th scope="col" aria-sort="${ui.sort === key ? (ui.dir === 'asc' ? 'ascending' : 'descending') : 'none'}"><button type="button" class="th-btn" data-action="usage-sort" data-sort="${key}" data-key="u-sort-${key}">${text}${ui.sort === key ? icon(ui.dir === 'asc' ? 'arrow-up' : 'arrow-down', 'icon th-icon') : ''}</button></th>`;
  const rows = m.rows.map(r => {
    const tokens = rowTokens(r);
    return `<tr><td class="nowrap">${r.timestamp ? esc(new Date(r.timestamp * 1000).toLocaleString()) : '—'}</td><td>${esc(humanize(r.purpose) || '—')}</td><td>${esc(label(r.model))}</td>`
      + `<td>${r.run_id ? `<a class="mono" href="${href('i', r.run_id)}">${esc(String(r.run_id).slice(0, 14))}</a>` : '—'}</td>`
      + `<td class="num">${tokens === null ? '<span title="unknown">— <span class="sr-only">unknown</span></span>' : fmt(tokens)}</td>`
      + `<td class="num">${cost(r) === null ? '<span title="Not applicable: subscription usage has no price">—</span>' : money(cost(r))}</td><td>${esc(humanize(r.status) || '—')}</td></tr>`;
  }).join('');
  const table = m.turns
    ? `<div class="table-wrap"><table class="table"><caption class="sr-only">Usage rows</caption><thead><tr>${COLUMNS.map(th).join('')}</tr></thead><tbody>${rows}</tbody></table></div>`
      + (m.remaining ? `<button type="button" class="btn btn-ghost" data-action="usage-more" data-key="u-more">Show more (${fmt(m.remaining)} more)</button>` : '')
    : `<p class="hint">No usage in this range.</p>`;
  return { refresh, summary: summaryHTML(m), controls, table };
}

function summaryHTML(m) {
  const tile = (label, value, sub = '') => `<div class="tile"><span class="tile-label">${label}</span><span class="tile-value">${value}</span>${sub ? `<span class="tile-sub">${sub}</span>` : ''}</div>`;
  return `<div class="tiles">${tile('Turns', m ? fmt(m.turns) : '—')}`
    + tile('Total tokens', m ? fmt(m.tokens) : '—', m?.uncounted ? `${fmt(m.uncounted)} turns without token counts` : 'Cached and reasoning tokens are included once')
    + tile('API-equivalent estimate', !m ? '—' : m.estimate === null ? 'Not available' : money(m.estimate), NOT_A_CHARGE) + `</div>`;
}
