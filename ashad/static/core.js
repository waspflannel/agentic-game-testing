// Pure renderer helpers. Nothing here touches the DOM at import time, so Node tests import it directly.

const ESCAPES = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' };
export const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ESCAPES[c]);

export class ApiError extends Error {
  constructor(status, body) {
    const detail = body?.detail;
    const text = typeof detail === 'string' ? detail
      : Array.isArray(detail) ? detail.map(item => item?.msg || String(item)).join('; ') : '';
    super(text || `Request failed (${status})`);
    this.status = status;
    this.body = body;
  }
}

// Mutations carry the boundary header and always send a JSON body (FastAPI dict bodies reject an empty one).
export async function api(path, { method = 'GET', body, signal, fetchImpl = globalThis.fetch } = {}) {
  const init = { method, signal, cache: 'no-store', headers: { Accept: 'application/json' } };
  if (method !== 'GET') {
    init.headers['X-ASHAD-Request'] = '1';
    init.headers['Content-Type'] = 'application/json';
    init.body = JSON.stringify(body ?? {});
  }
  const response = await fetchImpl(path, init);
  const data = await response.json().catch(() => null);
  if (!response.ok) throw new ApiError(response.status, data);
  return data;
}

export const href = (...parts) => '#/' + parts.map(encodeURIComponent).join('/');

export function parseRoute(hash) {
  const [path, search = ''] = String(hash || '').replace(/^#\/?/, '').split('?');
  let parts;
  try { parts = path.split('/').filter(Boolean).map(decodeURIComponent); } catch { return { name: 'redirect', to: '#/' }; }
  const query = Object.fromEntries(new URLSearchParams(search));
  const [head, id, sub] = parts;
  const n = parts.length;
  if (!n) return { name: 'home', query };
  if (head === 'p' && n === 2) return { name: 'project', id, query };
  if (head === 'p' && n === 3 && sub === 'new') return { name: 'composer', id, query };
  if (head === 'i' && n === 2) return { name: 'instance', id, query };
  if (head === 'setup' && n === 1) return { name: 'setup', query };
  if (head === 'usage' && n === 1) return { name: 'usage', query };
  if (head === 'settings' && n <= 2) return { name: 'settings', section: id || 'account', query };
  if (head === 'styleguide' && n === 1) return { name: 'styleguide', query };
  if (head === 'new' && n === 2) return { name: 'legacy-new', agent: id };
  return { name: 'redirect', to: '#/' };  // Includes legacy #/sessions and #/agents.
}

export function legacyNewTarget(agentId, data) {
  const agent = (data?.agents || []).find(a => a.id === agentId);
  const project = agent && (data.projects || []).find(p => p.id === agent.project_id);
  return project ? href('p', project.id, 'new') + '?agent=' + encodeURIComponent(agent.id) : '#/';
}

export function ago(seconds, now = Date.now() / 1000) {
  if (!Number.isFinite(seconds)) return '';
  const s = Math.max(0, now - seconds);
  if (s < 60) return 'now';
  if (s < 3600) return `${Math.floor(s / 60)}m`;
  if (s < 86400) return `${Math.floor(s / 3600)}h`;
  return `${Math.floor(s / 86400)}d`;
}

// Each view render takes a ticket; a response whose ticket is no longer current is dropped.
export function createEpoch() {
  let current = 0;
  return { next() { const mine = ++current; return () => mine === current; } };
}

export const runGoal = run => run?.plan?.primary_goal || run?.plan?.players?.[0]?.goal || run?.plan?.title || run?.id || 'Instance';
export const runTime = run => run?.started_at || run?.created_at;

// tone drives colour (live | attention | pending | muted); label is always shown or announced with it.
export function runStatus(run, owner) {
  if (run?.human_control) return { tone: 'attention', label: 'You have control' };
  if (owner?.reason === 'teardown_unconfirmed') return { tone: 'attention', label: 'Teardown unconfirmed' };
  switch (run?.state) {
    case 'Running': return { tone: 'live', label: 'Running' };
    case 'Paused': return { tone: 'attention', label: 'Paused' };
    case 'Investigating': return { tone: 'attention', label: 'Needs review' };
    case 'Queued': case 'Starting': return { tone: 'pending', label: 'Starting' };
    case 'Stopping': return { tone: 'pending', label: 'Stopping' };
    case 'Ended': return { tone: 'muted', label: 'Ended' };
    default: return { tone: 'muted', label: 'Unknown' };
  }
}

// Replace a region's markup only when it changed, restoring focus (by data-key) and scroll (by data-scroll).
export function patchHTML(el, html, activeElement) {
  if (el.renderedHTML === html) return false;
  const focusKey = activeElement && el.contains(activeElement) ? activeElement.dataset?.key : undefined;
  const scrolls = [...el.querySelectorAll('[data-scroll]')].map(node => [node.dataset.scroll, node.scrollTop]);
  el.innerHTML = html;
  el.renderedHTML = html;
  for (const [key, top] of scrolls) {
    const node = [...el.querySelectorAll('[data-scroll]')].find(n => n.dataset.scroll === key);
    if (node) node.scrollTop = top;
  }
  if (focusKey) [...el.querySelectorAll('[data-key]')].find(n => n.dataset.key === focusKey)?.focus();
  return true;
}
