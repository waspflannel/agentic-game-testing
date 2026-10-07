'use strict';
(() => {
const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const arr = value => Array.isArray(value) ? value : [];
const title = value => String(value ?? 'Unknown').replace(/[_-]/g, ' ').replace(/\b\w/g, c => c.toUpperCase());
const icons = {
 grid:'<rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/><rect x="3" y="14" width="7" height="7" rx="1"/><rect x="14" y="14" width="7" height="7" rx="1"/>',
 layers:'<path d="m12 3 10 5-10 5L2 8Z"/><path d="m2 12 10 5 10-5M2 16l10 5 10-5"/>',
 flag:'<path d="M5 21V4m0 0c5-4 8 4 14 0v10c-6 4-9-4-14 0"/>',
 chart:'<path d="M4 3v17h17M8 16v-5m5 5V7m5 9V4"/>',
 settings:'<path d="m10 3-1 3-3 1-3 3 2 3-1 3 3 3 3-1 3 2 3-3 1-3 3-1-1-4-3-1-1-3Z"/><circle cx="11.5" cy="11.5" r="3"/>',
 monitor:'<rect x="2" y="3" width="20" height="14" rx="2"/><path d="M8 21h8m-4-4v4"/>',
 plus:'<path d="M12 5v14M5 12h14"/>',
 chevron:'<path d="m9 5 7 7-7 7"/>',
 check:'<path d="m5 12 4 4L19 6"/>',
 arrow:'<path d="M4 12h16m-6-6 6 6-6 6"/>',
 play:'<path d="m8 4 12 8-12 8Z"/>',
 pause:'<path d="M8 4v16M16 4v16"/>',
 stop:'<rect x="5" y="5" width="14" height="14" rx="1"/>',
 close:'<path d="m6 6 12 12M6 18 18 6"/>',
 refresh:'<path d="M20 8a9 9 0 1 0 1 7M20 3v5h-5"/>',
 key:'<circle cx="8" cy="8" r="5"/><path d="m12 12 9 9m-3-3 3-3m-6 0 3-3"/>',
 folder:'<path d="M3 6a2 2 0 0 1 2-2h5l2 3h7a2 2 0 0 1 2 2v10a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2Z"/>',
 code:'<path d="m8 6-6 6 6 6m8-12 6 6-6 6m-3-15-2 18"/>',
 users:'<circle cx="9" cy="7" r="3"/><path d="M3 21v-3a6 6 0 0 1 12 0v3m0-17a3 3 0 0 1 0 6m4 11v-3a6 6 0 0 0-3-5"/>',
 clock:'<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
 info:'<circle cx="12" cy="12" r="9"/><path d="M12 11v6m0-10h.01"/>',
 warning:'<path d="m12 3 10 18H2Z"/><path d="M12 9v5m0 3h.01"/>',
 target:'<circle cx="12" cy="12" r="9"/><circle cx="12" cy="12" r="4"/><path d="M12 1v4m0 14v4M1 12h4m14 0h4"/>',
 expand:'<path d="M8 3H3v5m13-5h5v5M3 16v5h5m13-5v5h-5"/>',
 search:'<circle cx="10" cy="10" r="7"/><path d="m15 15 6 6"/>',
 document:'<path d="M14 2H5a1 1 0 0 0-1 1v18a1 1 0 0 0 1 1h14a1 1 0 0 0 1-1V8ZM14 2v6h6M8 13h8M8 17h6"/>',
 download:'<path d="M12 3v12m-4-4 4 4 4-4M4 16v5h16v-5"/>',
 external:'<path d="M14 3h7v7m0-7L10 14M10 3H3v18h18v-7"/>',
 menu:'<path d="M4 6h16M4 12h16M4 18h16"/>',
 bolt:'<path d="m13 2-9 12h7l-1 8L21 9h-8Z"/>',
 trash:'<path d="M3 6h18M9 6V3h6v3M5 6l1 15h12l1-15M10 10v7m4-7v7"/>',
 save:'<path d="M4 3h13l4 4v14H3V3Zm3 0v6h10V3M7 21v-8h10v8"/>',
 eye:'<path d="M2 12s4-7 10-7 10 7 10 7-4 7-10 7S2 12 2 12Z"/><circle cx="12" cy="12" r="3"/>',
 message:'<path d="M21 15a3 3 0 0 1-3 3H8l-6 4V5a3 3 0 0 1 3-3h13a3 3 0 0 1 3 3Z"/>',
 shield:'<path d="m12 2 9 4v6c0 5-9 10-9 10S3 17 3 12V6Z"/><path d="m8 12 3 3 5-6"/>',
 spark:'<path d="m12 2 2.8 7.2L22 12l-7.2 2.8L12 22l-2.8-7.2L2 12l7.2-2.8Z"/>'
};
const icon = name => `<svg class="icon" viewBox="0 0 24 24" aria-hidden="true">${icons[name] || icons.info}</svg>`;
const state = {data:null,dataRevision:0,route:'projects',parts:[],draft:null,run:null,events:[],eventNext:0,eventTotal:0,search:'',connected:true,polling:false,renderEpoch:0,codexLogin:null,authEpoch:0,tuning:false,tuneError:'',launching:false,launchError:''};
function markDraftDirty(value){state.dirtyDraft=Boolean(value);globalThis.window?.ashadDesktop?.setDirtyDraft(state.dirtyDraft)?.catch(error=>toast(errorMessage(error),true));}
function allowDraftNavigation(next){if(!state.dirtyDraft||next===state.parts.join('/'))return true;if(globalThis.window?.confirm?.('Discard the unsent form changes and leave this page?')===false)return false;markDraftDirty(false);return true;}
async function chooseBuildManifest(trigger){const form=trigger.closest('form'),field=form?.querySelector('[name="manifest_path"]');if(!field)return;const selected=await globalThis.window?.ashadDesktop?.chooseBuild(trigger.dataset.kind||'manifest');if(!selected||!form.isConnected||form!==$('#modal-form'))return;field.value=selected;field.dispatchEvent(new Event('input',{bubbles:true}));field.focus();}
const activeStates = ['queued','starting','running','paused','investigating','stopping'];
const active = r => activeStates.includes(String(r.state).toLowerCase());
const known = v => v === undefined || v === null || v === '' ? 'Unknown' : esc(v);
const number = v => v == null ? 'Unknown' : Number(v).toLocaleString();
const money = v => v == null ? 'Unknown' : new Intl.NumberFormat('en-US',{style:'currency',currency:'USD',maximumFractionDigits:4}).format(v);
const dateObj = v => v ? new Date(typeof v === 'number' || /^\d+(\.\d+)?$/.test(String(v)) ? Number(v) * 1000 : v) : null;
const date = v => dateObj(v)?.toLocaleString(undefined,{month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'}) || 'Unknown';
const time = v => dateObj(v)?.toLocaleTimeString(undefined,{hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:false}) || '—';
const ago = v => {const d=dateObj(v);if(!d)return 'Unknown';const s=Math.max(0,Math.round((Date.now()-d.getTime())/1000));return s<60?`${s}s ago`:s<3600?`${Math.floor(s/60)}m ago`:`${Math.floor(s/3600)}h ago`;};
const duration = v => v == null ? 'Unset' : Number(v)<60?`${v}s`:Number(v)<3600?`${Math.round(v/60)} min`:`${(v/3600).toFixed(1)} hr`;
const bytes = v => v == null ? 'Unknown' : v>=1073741824?`${(v/1073741824).toFixed(1)} GB`:v>=1048576?`${(v/1048576).toFixed(1)} MB`:`${number(v)} B`;
const badge = (value, color) => {const s=String(value || 'unknown').toLowerCase();color ||= ['running','ready','completed','connected','valid','available','configured'].includes(s)?'good':['investigating','paused','starting','queued','suspected','partial'].includes(s)?'warn':['failed','invalid','terminated','error','missing','unavailable'].includes(s)?'bad':'neutral';return `<span class="tag ${color}"><span class="dot ${color==='good'?'good':color==='warn'?'warn':color==='bad'?'bad':''}"></span>${esc(title(s))}</span>`;};
const button = (text, action, ico, cls='', extra='') => `<button type="button" class="button ${cls}" data-action="${action}" ${extra}>${ico?icon(ico):''}${esc(text)}</button>`;
const empty = (heading, copy, action='') => `<div class="empty-inline"><h3>${esc(heading)}</h3><p>${esc(copy)}</p>${action?`<div style="margin-top:18px">${action}</div>`:''}</div>`;
const notice = (copy,type='info') => `<div class="notice ${type}">${icon(type==='error'||type==='warning'?'warning':'info')}<div>${copy}</div></div>`;
function get(obj,path){return path.split('.').reduce((o,k)=>o?.[k],obj);}
function set(obj,path,value){const keys=path.split('.');let o=obj;for(const key of keys.slice(0,-1)){if(o[key]==null)o[key]={};o=o[key];}o[keys.at(-1)]=value;}
function field(label,path,options={}) {const {type='text',value=get(state.draft,path),help='',full=false,placeholder='',min,max,step,nullable=false,readonly=false}=options;const id=`field-${path.replaceAll('.','-')}`;return `<div class="field${full?' full':''}"><label for="${esc(id)}">${esc(label)}</label>${type==='textarea'?`<textarea id="${id}" data-path="${esc(path)}" placeholder="${esc(placeholder)}">${esc(value)}</textarea>`:`<input id="${id}" type="${type}" data-path="${esc(path)}" value="${esc(value)}" placeholder="${esc(placeholder)}" ${min!=null?`min="${min}"`:''} ${max!=null?`max="${max}"`:''} ${step!=null?`step="${step}"`:''} ${nullable?'data-nullable="true"':''} ${readonly?'readonly':''}>`}${help?`<small>${esc(help)}</small>`:''}</div>`;}
function select(label,path,options,help='') {const val=get(state.draft,path),id=`field-${path.replaceAll('.','-')}`;return `<div class="field"><label for="${id}">${esc(label)}</label><select id="${id}" data-path="${esc(path)}">${options.map(o=>{const [v,l]=Array.isArray(o)?o:[o,title(o)];return `<option value="${esc(v)}" ${String(val??'')===String(v)?'selected':''}>${esc(l)}</option>`;}).join('')}</select>${help?`<small>${esc(help)}</small>`:''}</div>`;}

function entityOptions(kind,first='Choose…'){return [['',first],...arr(state.data?.[kind]).map(x=>[x.id,x.name||x.id])];}
function toast(message,error=false){const node=document.createElement('div');node.className=`toast${error?' error':''}`;node.innerHTML=`${icon(error?'warning':'check')}<span>${esc(message)}</span><button type="button" aria-label="Dismiss notification">×</button>`;node.querySelector('button').onclick=()=>node.remove();$('#toast-region').append(node);setTimeout(()=>node.remove(),error?14000:6000);}
function errorMessage(error){const detail=error?.detail??error?.message??error;if(typeof detail==='string')return detail;if(Array.isArray(detail))return detail.map(e=>e.msg||e.message||JSON.stringify(e)).join('; ');return JSON.stringify(detail);}
async function api(path, method='GET', body) {
 const options = {method, headers:{Accept:'application/json'}};
 if (method !== 'GET') options.headers['X-ASHAD-Request'] = '1';
 if (body !== undefined) {
  options.headers['Content-Type'] = 'application/json';
  options.body = JSON.stringify(body);
 }
 let response;
 try {
  response = await fetch(path, options);
 } catch {
  state.connected = false;
  throw new Error('The ASHAD service is unreachable. Check the server, then try again.');
 }
 let result;
 try {
  result = await response.json();
 } catch {
  result = {detail:response.statusText};
 }
 if (!response.ok) {
  const error = new Error(errorMessage(result.detail || result));
  error.status = response.status;
  error.detail = result.detail;
  error.code = result.code;
  error.owning_instances = result.owning_instances;
  throw error;
 }
 state.connected = true;
 return result;
}
async function refresh(){const revision=state.dataRevision,data=await api('/api/bootstrap');if(revision===state.dataRevision){state.data=data;globalThis.workspaceUpdateSidebar?.();}return state.data;}
function navigate(route){location.hash=`#/${route}`;}
function heading(eyebrow,heading,copy,actions=''){return `<header class="page-head"><div><div class="head-eyebrow">${esc(eyebrow)}</div><h1>${esc(heading)}</h1>${copy?`<p>${esc(copy)}</p>`:''}</div>${actions?`<div class="actions">${actions}</div>`:''}</header>`;}
function navLink(route,label,ico,count){const selected=state.route===route || (route==='sessions'&&state.route==='runs') || (route==='agents'&&state.route==='new');return `<a class="nav-link${selected?' active':''}" href="#/${route}" ${selected?'aria-current="page"':''}>${icon(ico)}<span>${label}</span>${count!=null?`<span class="count">${count}</span>`:''}</a>`;}
function shell(content,background=false){const d=state.data,r=state.route;const labels={projects:state.parts[2]==='new'?'New instance':'Game projects',history:'Unassigned history',sessions:'Session control',new:'New instance',agents:'Agent setup',runs:state.run?.plan?.title||'Run details',usage:'Usage & capacity',settings:'Settings'};const markup=`<div class="layout"><button class="mobile-nav-scrim" data-action="menu" aria-label="Close navigation"></button><aside class="sidebar"><a class="brand" href="#/projects" aria-label="ASHAD home"><span class="brand-mark" aria-hidden="true"><i></i><i></i><i></i><i></i></span><span><span class="brand-name">ASHAD</span><span class="brand-caption" style="display:block">AGENTIC PLAYTESTING</span></span></a><nav aria-label="Main navigation">${globalThis.workspaceSidebarView?.()||'<a href="#/projects">Projects</a><a href="#/usage">Usage</a><a href="#/settings">Settings</a>'}</nav><div class="sidebar-footer"><div class="environment-name">Local game workspace</div><div class="connection-line" id="coordinator-status"><span class="dot ${state.connected?'good':'bad'}"></span>${state.connected?'Coordinator connected':'Connection lost'}</div><div class="tiny subtle" style="margin-top:10px">LOCAL WORKSPACE <span class="mono" style="font-size:9px">/ v1</span></div></div></aside><div class="content"><div class="topbar"><div class="breadcrumbs"><button class="mobile-menu" data-action="menu" aria-label="Open navigation">${icon('menu')}</button><span>Workspace</span><span class="subtle">/</span><span>${esc(labels[r]||'ASHAD')}</span></div><div class="topbar-meta"><span class="model-label">${esc((r==='runs'?(state.run?.model||state.run?.approved_plan?.model||state.run?.plan?.model):['agents','new'].includes(r)?(selectedAgent()?.model||state.draft?.model):d.ai?.model)||'gpt-6-luna')}</span><span class="connection-line"><span class="dot ${chatgptConnected(d.ai)?'good':'warn'}"></span>${chatgptConnected(d.ai)?'ChatGPT connected':'ChatGPT sign-in required'}</span></div></div><div id="global-alerts">${globalAlerts()}</div><main id="main" tabindex="-1">${content}</main></div></div>`;if(background)patchHTML($('#app'),markup);else {stopCameras($('#app'));$('#app').innerHTML=markup;}bindImages();syncNavigation();}

function setupChecks(){const d=state.data;return [{name:'AI connection',ok:chatgptConnected(d.ai),description:chatgptConnected(d.ai)?d.ai.message||'ChatGPT subscription connected':'Sign in with ChatGPT',tab:'ai'},{name:'Pinned game build',ok:d.builds.length>0,description:d.builds.length?`${d.builds.length} build${d.builds.length===1?'':'s'} registered`:'Register a verified build manifest',tab:'builds'},{name:'Player environments',ok:d.workers.some(w=>w.enabled!==false),description:d.workers.length?`${d.workers.length} independent worker${d.workers.length===1?'':'s'} configured`:'Connect an independent desktop worker',tab:'workers'},{name:'Operating limits',ok:true,description:`${d.settings.concurrent_sessions||1} concurrent session · duration ${duration(d.settings.duration_seconds).toLowerCase()}`,tab:'operation'}];}
function runRows(runs){return `<div class="data-table-wrap"><table class="data-table"><thead><tr><th>Instance / objective</th><th>Agent</th><th>State</th><th>Transport</th><th>Created</th><th></th></tr></thead><tbody>${runs.map(r=>{const agent=arr(state.data.agents).find(a=>a.id===(r.agent_id||r.plan?.agent_id));return `<tr><td><a class="row-title" href="#/runs/${esc(r.id)}">${esc(r.plan?.primary_goal||r.plan?.title||r.title||r.id)}</a><span class="row-sub mono">${esc(r.id)}</span></td><td>${agent?`<a href="#/agents/${esc(agent.id)}">${esc(agent.name)}</a>`:esc(r.agent_profile?.name||'Legacy session')}</td><td>${badge(r.state)}</td><td class="mono">${esc((r.plan?.transport?.mode||'unknown').toUpperCase())}</td><td class="muted">${date(r.created_at)}</td><td><a class="button ghost small" href="#/runs/${esc(r.id)}">Open ${icon('arrow')}</a></td></tr>`;}).join('')}</tbody></table></div>`;}
function sessionsView(){const d=state.data,live=d.runs.filter(active),ended=d.runs.filter(r=>!active(r)),checks=setupChecks();return heading('Operations','Session control','Tune an agent, start a fresh world, and follow its progress.',button('New instance','new','plus','primary'))+`<div class="overview-strip"><div class="overview-stat"><div class="overview-stat-label">Active sessions</div><div class="overview-stat-value">${live.length}<span class="muted" style="font-size:14px"> / ${d.settings.concurrent_sessions||1}</span></div><div class="overview-stat-sub">Configured concurrent capacity</div></div><div class="overview-stat"><div class="overview-stat-label">Player environments</div><div class="overview-stat-value">${d.workers.length}<span class="muted" style="font-size:14px"> configured</span></div><div class="overview-stat-sub">Independent desktops</div></div><div class="overview-stat"><div class="overview-stat-label">Retained runs</div><div class="overview-stat-value">${ended.length}</div><div class="overview-stat-sub">Completed instances</div></div><div class="overview-stat"><div class="overview-stat-label">AI usage</div><div class="overview-stat-value" style="font-size:19px">${totalCost(d.usage)}</div><div class="overview-stat-sub">Subscription and historical API usage</div></div></div><div class="workspace-grid"><section><div class="section-head"><h2>Live sessions</h2><span class="small muted">${live.length?`${live.length} active`:'Awaiting launch'}</span></div>${live.length?runRows(live):`<div class="empty-monitor"><span class="monitor-corner">PLAYER OBSERVATION</span><span class="monitor-corner right"><span class="dot"></span> NO SIGNAL</span><div class="empty-monitor-inner"><div class="monitor-glyph">${icon('monitor')}</div><h2>No active session</h2><p>Choose a tuned agent and a primary objective. Its game screen appears here when the client connects.</p>${button('Create an instance','new','plus','primary')}</div></div>`}<div class="section-head"><h2>Recent runs</h2><span class="small muted">${ended.length} retained</span></div>${ended.length?runRows(ended.slice(0,8)):empty('No run history yet','Ended instances retain their activity and usage records.')}</section><aside class="context-panel"><div class="section-head"><h2>Workspace readiness</h2><span class="tag neutral">${checks.filter(c=>c.ok).length} / ${checks.length}</span></div><div class="setup-list">${checks.map((c,i)=>`<div class="setup-row"><span class="setup-marker ${c.ok?'ready':''}">${c.ok?icon('check'):i+1}</span><div class="setup-row-text"><strong>${c.name}</strong><p>${esc(c.description)}</p></div><a href="#/settings/${c.tab}">${c.ok?'View':'Set up'}</a></div>`).join('')}</div><div class="notice"><div><strong style="font-weight:500;font-size:11px">Fresh world, every instance</strong><p style="font-size:11px">Each instance uses a saved agent profile, one objective, and one host. Start instance launches the game.</p></div></div></aside></div>`;}
function subscriptionUsage(row){return row.billing==='subscription'||row.api==='codex';}
function totalCost(usage) {
 const rows=arr(usage),metered=rows.filter(row=>!subscriptionUsage(row));
 if(!rows.length)return 'No usage yet';
 if(!metered.length)return 'Subscription';
 const values=metered.map(row=>row.estimated_cost_usd??row.cost_usd??row.cost);
 const cost=values.some(value=>value==null)?'Unknown API cost':money(values.reduce((sum,value)=>sum+Number(value),0));
 return metered.length===rows.length?cost:`Subscription + ${cost}${values.some(value=>value==null)?'':' API'}`;
}
// Standard short-context USD per million tokens, verified 2026-10-06.
const apiRates = {'gpt-6-luna':[.10,.01,.125,.50],'gpt-6-sol':[2,.20,2.50,10],'gpt-6.1-sol':[2,.10,2.50,10],'gpt-6-astra':[10,1,12.50,50]};
function tokenCount(value){return typeof value==='number'&&Number.isSafeInteger(value)&&value>=0?value:null;}
function usageTokens(row) {
 const u=row.usage||row;
 const input=tokenCount(u.input_tokens??u.inputTokens),output=tokenCount(u.output_tokens??u.outputTokens);
 return {input,output,cached:tokenCount(u.cached_input_tokens??u.cachedInputTokens??u.input_tokens_details?.cached_tokens),
  cacheWrite:tokenCount(u.cache_write_input_tokens??u.cacheWriteInputTokens??u.input_tokens_details?.cache_write_tokens),
  reasoning:tokenCount(u.reasoning_output_tokens??u.reasoningOutputTokens??u.output_tokens_details?.reasoning_tokens),
  total:input!=null&&output!=null?input+output:tokenCount(u.total_tokens??u.totalTokens)};
}
function apiEstimate(row) {
 if(!subscriptionUsage(row)){const cost=row.estimated_cost_usd??row.cost_usd??row.cost;return typeof cost==='number'&&Number.isFinite(cost)&&cost>=0?cost:null;}
 const rates=apiRates[row.model],t=usageTokens(row);
 if(!rates||[t.input,t.output,t.cached,t.cacheWrite].some(v=>v==null)||t.cached+t.cacheWrite>t.input)return null;
 return ((t.input-t.cached-t.cacheWrite)*rates[0]+t.cached*rates[1]+t.cacheWrite*rates[2]+t.output*rates[3])/1e6;
}
function usageSummary(rows) {
 const summary={requests:rows.length,partial:rows.some(row=>subscriptionUsage(row)&&row.usage!=null&&!['selection_total','tuning_total'].includes(row.usage_scope))};
 for(const key of ['input','output','cached','cacheWrite','reasoning','total','cost']){
  const values=rows.map(row=>key==='cost'?apiEstimate(row):usageTokens(row)[key]).filter(value=>value!=null);
  summary[key]={value:values.reduce((sum,value)=>sum+value,0),known:values.length};
 }
 return summary;
}
function estimateMoney(value){return value>0&&value<.000001?'<$0.000001':new Intl.NumberFormat('en-US',{style:'currency',currency:'USD',minimumFractionDigits:2,maximumFractionDigits:6}).format(value);}
function usageTotal(metric,requests,currency=false){return requests&& !metric.known?'Unknown':`${currency?estimateMoney(metric.value):number(metric.value)}${metric.known<requests?' + unknown':''}`;}
function summaryValue(summary,key){return usageTotal(summary[key],summary.requests,key==='cost')+(summary.partial?' (partial)':'');}
function usageSummaryView(rows) {
 const s=usageSummary(rows),stat=(label,key,detail)=>`<div class="overview-stat"><div class="overview-stat-label">${label}</div><div class="overview-stat-value">${esc(summaryValue(s,key))}</div><div class="overview-stat-sub">${detail}</div></div>`;
 return `<div class="overview-strip usage-summary">${stat('Input tokens','input',`${esc(usageTotal(s.cached,s.requests))} cached · ${esc(usageTotal(s.cacheWrite,s.requests))} cache write`)}${stat('Output tokens','output',`${esc(usageTotal(s.reasoning,s.requests))} reasoning (included)`)}${stat('Total tokens','total',`${s.requests} recorded request${s.requests===1?'':'s'}`)}${stat('API price estimate','cost',`USD · ${s.cost.known}/${s.requests} requests priced`)}</div>${s.partial?'<p class="section-note">Partial — older records may omit internal requests. Totals and estimates use reported tokens only.</p>':''}`;
}
function pricingNote(){return `<p class="section-note">Equivalent API cost, not a subscription charge. <a href="https://developers.openai.com/api/docs/pricing" target="_blank" rel="noopener noreferrer">Standard short-context rates</a>, 2026-10-06: input / cached input / cache write / output per 1M tokens: Luna $0.10 / $0.01 / $0.125 / $0.50; Sol $2 / $0.20 / $2.50 / $10; 6.1 Sol $2 / $0.10 / $2.50 / $10; Astra $10 / $1 / $12.50 / $50. Cached and cache-write tokens are subsets of input; reasoning is included in output. Missing usage or cache breakdown stays unknown. Other tiers and unreported fees are excluded. Historical API rows use their recorded cost.</p>`;}
function usageLedger(rows) {
 if(!rows.length)return empty('No requests recorded','Token totals update when provider requests finish.');
 return `<div class="data-table-wrap"><table class="data-table"><thead><tr><th>Time / purpose</th><th>Provider / model</th><th>Input tokens</th><th>Output tokens</th><th>Total tokens</th><th>API estimate / recorded cost</th><th>Billing / details</th></tr></thead><tbody>${rows.map(row=>{const t=usageTokens(row),cost=apiEstimate(row);return `<tr><td>${date(row.created_at||row.timestamp)}<span class="row-sub">${esc(title(row.kind||row.purpose||row.category))}</span></td><td class="mono">${esc(row.api||'Historical API')} / ${esc(row.model||'Unknown')}</td><td>${number(t.input)}<span class="row-sub">${number(t.cached)} cached<br>${number(t.cacheWrite)} cache write</span></td><td>${number(t.output)}<span class="row-sub">${number(t.reasoning)} reasoning (included)</span></td><td>${number(t.total)}</td><td>${cost==null?'Unknown':esc(estimateMoney(cost))}<span class="row-sub">${subscriptionUsage(row)?'API price estimate':'Recorded API cost'}</span></td><td>${subscriptionUsage(row)?'Subscription':money(row.estimated_cost_usd??row.cost_usd??row.cost)}<details><summary>Request details</summary><pre class="json-details">${esc(JSON.stringify(row,null,2))}</pre></details></td></tr>`;}).join('')}</tbody></table></div>`;
}
function runUsageView(run){const rows=arr(run.usage);return usageSummaryView(rows)+`<details class="usage-details"><summary>Token usage and price details · ${rows.length} requests</summary>${pricingNote()}${usageLedger(rows)}</details>`;}
function instanceUsageView(usage,runs) {
 const groups=new Map(arr(runs).map(run=>[run.id,{run,rows:[]}]));
 for(const row of usage){if(!row.run_id)continue;if(!groups.has(row.run_id))groups.set(row.run_id,{run:{id:row.run_id},rows:[]});groups.get(row.run_id).rows.push(row);}
 const assigned=usage.filter(row=>row.run_id),total=usageSummary(assigned);
 return `<div class="section-head"><h2>Instance usage</h2><span class="small muted">Grouped by instance; tuning usage is separate</span></div><div class="data-table-wrap"><table class="data-table"><thead><tr><th>Instance</th><th>Model</th><th>Input tokens</th><th>Output tokens</th><th>API estimate / recorded cost</th><th>Requests</th></tr></thead><tbody>${[...groups.values()].map(({run,rows})=>{const s=usageSummary(rows),models=[...new Set(rows.map(row=>row.model).filter(Boolean))];return `<tr><td><a href="#/runs/${esc(run.id)}">${esc(run.plan?.title||run.title||run.id)}</a><span class="row-sub mono">${esc(run.id)}</span></td><td>${esc(models.join(', ')||run.model||run.approved_plan?.model||run.plan?.model||'Unknown')}</td><td>${esc(summaryValue(s,'input'))}</td><td>${esc(summaryValue(s,'output'))}</td><td>${esc(summaryValue(s,'cost'))}</td><td>${rows.length}</td></tr>`;}).join('')}</tbody><tfoot><tr><th colspan="2">All instances</th><td>${esc(summaryValue(total,'input'))}</td><td>${esc(summaryValue(total,'output'))}</td><td>${esc(summaryValue(total,'cost'))}</td><td>${total.requests}</td></tr></tfoot></table></div>`;
}
function modelSelector() {
 const selected=state.draft.model||'gpt-6-luna',options=arr(state.data.model_options),available=state.data.ai?.available_models;
 if(!options.length)return `<div class="field"><span class="label">Agent model</span><strong>${esc(selected)}</strong><small>Model selection is available after the controller update is loaded.</small></div>`;
 return `<div class="field"><label for="agent-model">Agent model</label><select id="agent-model" data-path="model">${options.map(option=>{const unavailable=state.data.ai?.configured===true&&['ready','model_unavailable'].includes(state.data.ai.status)&&Array.isArray(available)&&!available.includes(option.id);return `<option value="${esc(option.id)}" ${selected===option.id?'selected':''} ${unavailable?'disabled':''}>${esc(option.label||option.id)}${unavailable?' — unavailable for this account':''}</option>`;}).join('')}</select><small>Used to tune this agent and play its instances. Defaults to GPT-6 Luna.</small></div>`;
}
function section(name,copy,body){return `<section class="form-section"><div class="form-section-title"><h3>${name}</h3><p>${copy}</p></div><div>${body}</div></section>`;}
function selectedAgent(){return arr(state.data?.agents).find(agent=>agent.id===state.parts[1]);}
function agentsView() {
 const profile=selectedAgent(),profiles=arr(state.data.agents);
 if(state.parts[1]&&!profile)return heading('Agent setup','Agent unavailable','This profile is no longer available.')+`<a class="button" href="#/agents">Agent setup</a>`;
 if(profile){
  const runs=arr(state.data.runs).filter(run=>(run.agent_id||run.plan?.agent_id)===profile.id);
  return heading('Agent setup',profile.name,'Reusable controls and interaction knowledge for this game build.',`<a class="button primary" href="#/new/${esc(profile.id)}">${icon('plus')}New instance</a>`)+
   `<dl class="definition-list agent-details"><dt>Build</dt><dd>${esc(state.data.builds.find(build=>build.id===profile.build_id)?.name||profile.build_id)}</dd><dt>Source revision</dt><dd class="mono">${esc(profile.source_revision)}</dd><dt>Model</dt><dd>${esc(profile.model)}</dd><dt>Tuned</dt><dd>${date(profile.created_at)} ${badge(profile.status||'ready')}</dd></dl><div class="section-head"><h2>Instances</h2><span class="small muted">${runs.length} total</span></div>${runs.length?runRows(runs):empty('No instances yet','Start a fresh world with a primary objective.')}<details class="agent-instructions"><summary>Game controls and interaction guide</summary><pre class="json-details">${esc(profile.instructions)}</pre>${arr(profile.limitations).length?notice(`<strong>Guide limitations</strong><ul>${profile.limitations.map(limit=>`<li>${esc(limit)}</li>`).join('')}</ul>`,'warning'):''}<p class="section-note">Source files: ${esc(arr(profile.source_files).map(file=>typeof file==='string'?file:file.path||file.name||JSON.stringify(file)).join(', '))}</p></details>`;
 }
 return heading('Agent setup','Tune an agent','Read the game code to prepare a reusable guide to controls, world interactions, and UI.')+
  section('Game knowledge','Tuning creates saved instructions. Each instance uses this guide with its current game state.',`<form id="tune-agent-form" aria-busy="${state.tuning}"><fieldset ${state.tuning?'disabled':''}><div class="field-grid">${select('Game build','build_id',entityOptions('builds','Select a game build'))}${field('Configuration name','agent_name',{placeholder:'For example: beach scout (optional)'})}${modelSelector()}</div><div class="form-actions"><button class="button primary ${state.tuning?'spinning':''}" type="submit">${icon(state.tuning?'refresh':'spark')}${state.tuning?'Tuning agent…':'Tune agent'}</button><span class="muted" role="status">${state.tuning?'Reading the game code and preparing controls. This can take several minutes.':'The game stays closed while tuning.'}</span></div></fieldset>${state.tuneError?notice(esc(state.tuneError),'error'):''}${!state.data.builds.length?notice('Add a game build in <a href="#/settings/builds">Settings → Builds</a> first.'):''}${!chatgptConnected(state.data.ai)?notice('Connect ChatGPT in <a href="#/settings/ai">Settings → AI connection</a> to tune an agent.'):''}</form>`)+
  `<div class="section-head"><h2>Your agents</h2><span class="small muted">${profiles.length} saved</span></div>${profiles.length?profiles.map(agent=>`<article class="setup-entity"><div><h3><a href="#/agents/${esc(agent.id)}">${esc(agent.name)}</a> ${badge(agent.status||'ready')}</h3><p>${esc(agent.model)} · ${esc(state.data.builds.find(build=>build.id===agent.build_id)?.name||agent.build_id)}</p><div class="meta-row">Tuned ${date(agent.created_at)} · ${arr(state.data.runs).filter(run=>(run.agent_id||run.plan?.agent_id)===agent.id).length} instances</div></div><div class="actions"><a class="button ghost small" href="#/agents/${esc(agent.id)}">View guide</a><a class="button primary small" href="#/new/${esc(agent.id)}">${icon('plus')}New instance</a></div></article>`).join(''):empty('No tuned agents','Select a game build and tune your first agent.')}`;
}
function slotConflictView(){
 const slot=state.data.instance_slot;
 if(slot?.available!==false)return '';
 return notice(esc(slot.blocked_reason||'Another instance owns the global slot.')+' This draft will not auto-launch. '+arr(slot.owners).map(owner=>
  arr(state.data.runs).some(run=>run.id===owner.run_id)?`<a href="#/runs/${encodeURIComponent(owner.run_id)}">Return to active instance: ${esc(owner.run_id)}</a>`:'<a href="#/settings/workers">Reconcile unresolved desktop ownership</a>').join(' · '),'warning');
}
function draftView() {
 const agent=selectedAgent();
 if(!agent)return heading('New instance','Choose an agent','Tune an agent before starting an instance.')+`<a class="button primary" href="#/agents">${icon('spark')}Agent setup</a>`;
 return heading('New instance',agent.name,'Start one bot in a fresh world with this agent’s saved game knowledge.',`<a class="button ghost" href="#/agents/${esc(agent.id)}">View agent</a>`)+slotConflictView()+
  `<p class="section-note">${esc(agent.model)} · ${esc(state.data.builds.find(build=>build.id===agent.build_id)?.name||agent.build_id)} · Fresh world</p><form id="instance-form" class="instance-form" aria-busy="${state.launching}"><fieldset ${state.launching?'disabled':''}><div class="field-grid">${field('Primary objective','primary_goal',{type:'textarea',full:true,placeholder:'For example: explore the world and learn how to gather wood.'})}${select('Transport','transport',[['enet','ENet'],['steam','Steam']])}</div><p class="section-note">An available game environment is selected automatically. Steam uses a verified account configured in Settings.</p><div class="form-actions"><button class="button primary ${state.launching?'spinning':''}" type="submit" ${state.data.instance_slot?.available===false?'disabled':''}>${icon(state.launching?'refresh':'play')}${state.launching?'Starting instance…':'Start instance'}</button></div></fieldset>${state.launchError?notice(esc(state.launchError),'error'):''}</form>`;
}
async function tuneAgent() {
 if(state.tuning)return;
 const payload={build_id:state.draft.build_id,model:state.draft.model||'gpt-6-luna'};
 if(!payload.build_id)throw new Error('Select a game build first.');
 const projectId=state.data.builds.find(build=>build.id===payload.build_id)?.project_id;
 if(projectId)payload.project_id=projectId;
 if(state.draft.agent_name?.trim())payload.name=state.draft.agent_name.trim();
 const signature=JSON.stringify(payload),retry=state.tuneRequest?.signature===signature;
 if(!retry)state.tuneRequest={signature,id:crypto.randomUUID()};
 payload.request_id=state.tuneRequest.id;
 const epoch=state.renderEpoch;
 state.tuning=true;state.tuneError='';render();
 try {
  let profile;
  if(retry){const attempt=await api(`/api/agents/tuning/${encodeURIComponent(payload.request_id)}`);if(attempt.status==='completed')profile=attempt.agent;else if(attempt.status==='in_progress')throw new Error('This request is still tuning. Wait, then retry to check the same attempt; no second inference was started.');}
  profile??=await api('/api/agents/tune','POST',payload);
  state.dataRevision++;state.tuneRequest=null;
  state.data.agents=[profile,...arr(state.data.agents).filter(agent=>agent.id!==profile.id)];
  if(epoch===state.renderEpoch){markDraftDirty(false);navigate(`agents/${profile.id}`);}
  toast('Agent tuned. Its game guide is saved and ready for a new instance.');
 } catch(error) {state.tuneError=errorMessage(error);throw error;}
 finally {state.tuning=false;if(state.route==='agents')render(true);}
}
async function startInstance() {
 if(state.launching)return;
 const agent=selectedAgent(),draft=state.draft;
 if(!agent)throw new Error('Choose a tuned agent first.');
 if(!draft.primary_goal?.trim())throw new Error('Enter a primary objective.');
 if(state.data.instance_slot?.available===false)throw new Error('Another instance owns the global slot. Return to it to stop or reconcile; this draft will not auto-launch.');
 const payload={agent_id:agent.id,primary_goal:draft.primary_goal.trim(),transport:draft.transport,request_id:draft.request_id};
 const projectId=draft.project_id||agent.project_id||state.data.builds.find(build=>build.id===agent.build_id)?.project_id;
 if(projectId)payload.project_id=projectId;
 const epoch=state.renderEpoch;
 state.launching=true;state.launchError='';render();
 try {
  const run=await api('/api/instances','POST',payload);
  state.dataRevision++;
  state.data.runs=[run,...arr(state.data.runs).filter(item=>item.id!==run.id)];
  if(epoch===state.renderEpoch){markDraftDirty(false);navigate(`runs/${run.id}`);}
  toast('Instance started. Follow the game and agent in Sessions.');
 } catch(error) {state.launchError=errorMessage(error);if(error.code==='active_instance_conflict')state.data.instance_slot={available:false,limit:1,owners:arr(error.owning_instances)};throw error;}
 finally {state.launching=false;if(state.route==='new')render(true);}
}
function feed(player,status={},large=false){const rid=state.run.id;const obs=status.observation||status.last_observation||{};const stamp=status.feed_captured_at||obs.captured_at||obs.timestamp||status.last_capture_at||status.observed_at;const phase=(!player.initial&&['waiting',undefined].includes(status.phase)?'scheduled':status.phase||status.connection_phase||status.state||'waiting');const live=Boolean(status.launched&&active(state.run));return `<article class="feed" data-key="${esc(player.id)}"><div class="feed-top"><span class="dot ${phase==='ready'?'good':''}"></span><strong>${esc(player.name||player.id)}</strong><span class="role">${esc(player.role)}</span>${badge(phase)}${live&&!['queued','stopping'].includes(String(state.run.state).toLowerCase())?button(state.run.human_control?.player_id===player.id?'Reopen desktop':'Take over','takeover','monitor','ghost small',`data-player="${esc(player.id)}" aria-label="Take over ${esc(player.name||player.id)}" ${state.run.human_control&&state.run.human_control.player_id!==player.id?'disabled title="Finish the current takeover first"':''}`):''}${large?'':`<button class="icon-button" data-action="expand-feed" data-player="${esc(player.id)}" aria-label="Enlarge ${esc(player.name||player.id)} feed">${icon('expand')}</button>`}</div><div class="feed-view" data-player-view="${esc(player.id)}">${live?`<img data-live="true" src="/api/runs/${encodeURIComponent(rid)}/players/${encodeURIComponent(player.id)}/live?epoch=${encodeURIComponent(status.epoch||'')}" alt="Live desktop of ${esc(player.name||player.id)}, ${esc(player.role)}" data-feed="${esc(player.id)}">`:`<div class="feed-empty">${icon(phase==='scheduled'?'clock':'monitor')}<span>${phase==='scheduled'?'Waiting for participation trigger':active(state.run)?'Waiting for a real client observation':'Camera offline — session ended'}</span></div>`}<div class="feed-overlays"><span>${esc(status.worker_id||player.worker_id||'Environment unassigned')}</span><span>${live?'LIVE VIEW · ':'OFFLINE · '}<time data-clock>${new Date().toLocaleTimeString()}</time></span></div></div><div class="feed-bottom"><div class="goal">${icon('target')}<span>${esc(player.goal||'No goal recorded')}</span></div>${agentDiagnostics(player,status)}${structuredInspector(status)}</div></article>`;}
// Background polling must not restart page animation, image loading, or disclosures.
function patchHTML(parent,html){
 const template=document.createElement('template');template.innerHTML=html;
 const key=node=>node.nodeType===1?`${node.nodeName}:${node.id||node.getAttribute('data-key')||node.getAttribute('class')||''}`:node.nodeName;
 function patch(current,next){
  if(current.nodeType!==1){if(current.nodeValue!==next.nodeValue)current.nodeValue=next.nodeValue;return;}
  const local=name=>(current.tagName==='DETAILS'&&name==='open')||(current.matches('img[data-feed]')&&name==='hidden');
  for(const attr of [...current.attributes])if(!local(attr.name)&&!next.hasAttribute(attr.name))current.removeAttribute(attr.name);
  for(const attr of next.attributes)if(!local(attr.name)&&current.getAttribute(attr.name)!==attr.value)current.setAttribute(attr.name,attr.value);
  children(current,next);
 }
 function children(current,next){
  let cursor=current.firstChild;
  // ponytail: sibling scan suits these small dashboard lists; index keys if lists grow large.
  for(const desired of [...next.childNodes]){
   let match=cursor;
   while(match&&key(match)!==key(desired))match=match.nextSibling;
   if(match){if(match!==cursor)current.insertBefore(match,cursor);patch(match,desired);cursor=match.nextSibling;}
   else current.insertBefore(desired.cloneNode(true),cursor);
  }
  while(cursor){const removed=cursor;cursor=cursor.nextSibling;
   if(removed.nodeType===1&&removed.matches('.feed-loading-error')&&current.querySelector('img[data-feed][hidden]'))continue;
   stopCameras(removed);removed.remove();
  }
 }
 children(parent,template.content);
}
function stopCameras(root){
 if(root?.nodeType!==1)return;
 const images=[...(root.matches('img[data-live]')?[root]:[]),...$$('img[data-live]',root)];
 images.forEach(img=>{delete img.dataset.live;img.removeAttribute('src');});
}
const boundImages=new WeakSet();
function bindImages(){$$('img[data-feed]').forEach(img=>{
 if(boundImages.has(img))return;boundImages.add(img);
 img.addEventListener('load',()=>{img.hidden=false;img.parentElement?.querySelector('.feed-loading-error')?.remove();});
 img.addEventListener('error',()=>{if(img.dataset.live)setTimeout(()=>{if(img.dataset.live&&img.isConnected&&!document.hidden&&(!img.closest('dialog')||img.closest('dialog').open))img.src=img.getAttribute('src');},3000);img.hidden=true;if(img.parentElement&&!img.parentElement.querySelector('.feed-loading-error')){const div=document.createElement('div');div.className='feed-loading-error';div.innerHTML=`${icon('monitor')}<span>Camera disconnected. Reconnecting…<br>This does not mean the game has frozen.</span>`;img.parentElement.append(div);}});
});}
function syncNavigation(){const sidebar=$('.sidebar');if(!sidebar)return;const mobile=Boolean(window.matchMedia?.('(max-width: 740px)').matches),open=document.body.classList.contains('nav-open');sidebar.inert=mobile&&!open;const content=$('.content');if(content)content.inert=mobile&&open;const toggle=$('.mobile-menu');if(toggle)toggle.setAttribute('aria-expanded',String(open));}
function pauseNotice(run) {
 if (run.human_control && active(run)) {
  const player=arr(run.plan?.players).find(p=>p.id===run.human_control.player_id);
  const confirmed=run.human_control.status==='active',opening=run.human_control.status==='opening';
  const issue=run.pause&&run.pause.category!=='human_control'?run.pause:null;
  return `<div class="takeover-banner" role="status"><div><h3>${icon('monitor')} ${confirmed?'Manual control active':opening?'Preparing manual control':'Desktop control unconfirmed'} · ${esc(player?.name||run.human_control.player_id)}</h3><p>${confirmed?'All bots and scheduled joins are paused while you play. The game world can keep running.':opening?'All bots are paused while desktop access is prepared.':'All bots are paused. Desktop access could not be confirmed; Hand back to bots will retry closing it before bots can resume.'}</p>${issue?`<div class="notice warning"><div><strong>${esc(issue.reason)}</strong>${issue.recovery?`<p>${esc(issue.recovery)}</p>`:''}</div></div>`:''}${run.investigation_deadline?`<p>Automatic termination in <strong data-deadline="${esc(run.investigation_deadline)}">${countdown(run.investigation_deadline)}</strong>. Extend investigation to keep it open.</p>`:''}<p>Closing the desktop does not resume the bots. Choose Hand back to bots when you are ready.</p></div><div class="actions">${button(confirmed?'Reopen desktop':'Retry desktop','takeover','external','ghost',`data-player="${esc(run.human_control.player_id)}"`)}${button('Hand back to bots','finish-takeover','play','primary')}</div></div>`;
 }
 const investigating = String(run.state).toLowerCase() === 'investigating';
 if (!investigating && String(run.state).toLowerCase() !== 'paused') return '';
 const pause = run.pause || {};
 const affected = arr(pause.players).map(id => arr(run.plan?.players).find(p => p.id === id)?.name || id);
 return `<div class="investigation-banner"><div><h3>${icon('warning')} ${investigating?'Warning: bots paused':'Bot inputs paused'}</h3><p class="pause-reason" role="status">${esc(pause.reason || (investigating?'The monitor detected a possible blocker. Open the action ledger for the recorded event.':'Paused by an operator.'))}</p><p>${affected.length?`Affected: ${esc(affected.join(', '))}. `:''}All bot input is held. The world may continue simulating.</p><p><strong>To continue:</strong> ${esc(pause.recovery || 'Inspect the player diagnostics and fix the issue, then choose Resume inputs. You can add recovery instructions if the bot needs a different approach.')}</p><p>Resume works without instructions. Use Stop run or Terminate now to end this session.</p></div>${investigating&&run.investigation_deadline?`<div class="pause-deadline"><span>Automatic termination in</span><div class="countdown" data-deadline="${esc(run.investigation_deadline)}">${countdown(run.investigation_deadline)}</div><small>Extend investigation to keep it open.</small></div>`:''}</div>`;
}
function agentDiagnostics(player, status) {
 const agent = status.agent_status || {};
 const context = status.structured_context || {};
 const observation = context.timestamp || status.last_observation?.captured_at || status.last_observation?.timestamp;
 const error = agent.last_error || status.error;
 const errorText = typeof error === 'object' ? error.message || error.detail || JSON.stringify(error) : error;
 return `<section class="agent-diagnostics" aria-label="${esc(player.name||player.id)} agent diagnostics"><div class="agent-phase"><strong>${esc(title(agent.phase||status.phase||'Waiting for agent status'))}</strong>${agent.updated_at?`<span>Updated <time data-age="${esc(agent.updated_at)}">${ago(agent.updated_at)}</time></span>`:''}</div><div class="activity">${esc(agent.detail||status.activity||status.current_activity||'No activity has been reported yet.')}</div>${agent.detail&&status.activity&&agent.detail!==status.activity?`<p class="small muted">Last activity: ${esc(status.activity)}</p>`:''}${errorText?`<div class="notice warning"><div><strong>Last agent error${agent.error_at?` &middot; ${date(agent.error_at)}`:''}</strong><p>${esc(errorText)}</p></div></div>`:''}<dl class="definition-list"><dt>Game observation</dt><dd>${observation?`<time data-age="${esc(observation)}">${ago(observation)}</time>${context.revision!=null?` &middot; revision ${esc(context.revision)}`:''}`:'Not received yet'}</dd><dt>Last decision completed</dt><dd>${agent.completed_at?`<time data-age="${esc(agent.completed_at)}">${ago(agent.completed_at)}</time>`:'Not recorded yet'}</dd>${agent.action_id?`<dt>Current action</dt><dd><code>${esc(agent.action_id)}</code></dd>`:''}${agent.wait_until?`<dt>Expected wait ends</dt><dd>${time(agent.wait_until)}</dd>`:''}${status.last_state_change_at?`<dt>Last game state change</dt><dd><time data-age="${esc(status.last_state_change_at)}">${ago(status.last_state_change_at)}</time></dd>`:''}<dt>Rejected actions</dt><dd>${number(status.invalid_actions||0)}</dd></dl><details><summary>Inspect agent record</summary><pre class="json-details">${esc(JSON.stringify({agent_status:agent,last_observation:status.last_observation,worker_status:status.worker_status},null,2))}</pre></details></section>`;
}
function globalAlerts() {
 const affected = arr(state.data?.runs).filter(r => ['paused','investigating'].includes(String(r.state).toLowerCase()) && !(state.route==='runs'&&state.parts[1]===r.id));
 return `${state.connected?'':`<div class="connection-error">Coordinator connection lost. Showing the last received records; run state and observations may be out of date.</div>`}${affected.map(r=>`<div class="global-investigation-alert">${icon('warning')}<span><strong>${esc(r.plan?.title||r.id)}: ${r.human_control?(r.human_control.status==='active'?'manual control active':'desktop control needs attention'):'bots paused'}.</strong> ${r.human_control&&r.pause?.category==='human_control'?'All bots are paused until you hand control back.':esc(r.pause?.reason||'Open the session for the pause reason and recovery controls.')}</span><a href="#/runs/${esc(r.id)}">${r.human_control?'Return to run':'View warning &amp; resume'} ${icon('arrow')}</a></div>`).join('')}`;
}
function updateStatusUI(){const region=$('#global-alerts');if(region)patchHTML(region,globalAlerts());const connection=$('#coordinator-status');if(connection)patchHTML(connection,`<span class="dot ${state.connected?'good':'bad'}"></span>${state.connected?'Coordinator connected':'Connection lost'}`);}
const goalRecalculations = new Map();
function canRecalculateGoals(run) {
 const players=arr(run?.plan?.players),plan=run?.subgoal_plan;
 return Boolean(run?.id&&plan&&typeof plan==='object'&&!Array.isArray(plan)&&
  ['starting','running','paused','investigating'].includes(String(run.state).toLowerCase())&&
  players.length===1&&plan.player_id&&plan.player_id===players[0]?.id&&run.players?.[plan.player_id]?.launched===true&&!run.human_control&&!run.external_control);
}
function recalculateGoalsControl(run) {
 if(!canRecalculateGoals(run))return '';
 const request=goalRecalculations.get(run.id),busy=request?.busy||request?.cancelling||run.goal_recalculation?.status==='planning';
 return `<div class="actions">${button(busy?'Recalculating goals…':'Recalculate goals','recalculate-goals','refresh','ghost small',`data-key="recalculate-goals" aria-haspopup="dialog" ${busy?'disabled aria-busy="true"':''}`)}${busy?button(request?.cancelling?'Cancelling…':'Cancel recalculation','cancel-recalculation','close','ghost small',`data-key="cancel-recalculation" ${request?.cancelling?'disabled aria-busy="true"':''}`):''}</div>${request?.cancelling?'<p role="status">Cancelling recalculation… Inputs stay paused.</p>':busy?'<p role="status">Holding inputs and replanning from fresh game state…</p>':request?.error?`<p class="danger-text" role="alert">${esc(request.error)} Retry Recalculate goals to check the same request.</p>`:(request?.done||request?.cancelled||request?.alreadyFinished)&&String(run.state).toLowerCase()==='paused'?`<p role="status">${request.alreadyFinished?'Recalculation already finished; inputs remain paused.':`${request.cancelled?'Recalculation cancelled.':'Goals recalculated.'} Inputs remain paused.`} Choose Resume inputs separately when ready.</p>`:''}${request?.cancelError?`<p class="danger-text" role="alert">${esc(request.cancelError)} Try Cancel recalculation again.</p>`:''}`;
}
function subgoalPlanView(run) {
 const plan=run.subgoal_plan;
 if(!plan||typeof plan!=='object'||Array.isArray(plan))return '<div class="subgoal-plan"><h3>Currently chasing</h3><p class="section-note" role="status">No generated subgoals yet. No active goal is recorded.</p></div>';
 const text=(value,fallback='')=>typeof value==='string'&&value.trim()?value:fallback;
 const status=value=>['pending','active','completed','blocked'].includes(value)?value:'unknown';
 const label=goal=>text(goal.objective,'Objective unavailable');
 const byId=new Map();let invalid=false;
 for(const goal of arr(plan.goals)){
  if(!goal||typeof goal!=='object'||!text(goal.id)||byId.has(goal.id)){invalid=true;continue;}
  byId.set(goal.id,goal);
 }
 const nodes=[...byId.values()],root=byId.get(text(plan.root_id,'root')),current=byId.get(plan.active_goal_id),planStatus=status(plan.status);
 if(nodes.length&&(!root||root.parent_id!=null))invalid=true;
 const ancestry=[],ancestors=new Set();let ancestor=current;
 while(ancestor&&!ancestors.has(ancestor.id)&&ancestry.length<8){
  ancestors.add(ancestor.id);ancestry.unshift(ancestor);ancestor=byId.get(ancestor.parent_id);
 }
 const partialAncestry=Boolean(ancestor)||(current&&ancestry[0]!==root);
 const waiting=plan.active_goal_id&&!current?'Recorded active goal is unavailable; awaiting planning.':planStatus==='blocked'?'No active goal — planner is blocked.':planStatus==='completed'?'No active goal — planner reports completion.':'Awaiting observations and planning.';
 // Match the planner's 64-goal / 8-level bounds; corrupt records must not grow the sidebar indefinitely.
 const seen=new Set();let limited=false;
 function goalView(goal,depth=0){
  if(seen.has(goal.id)){invalid=true;return '';}
  if(seen.size>=64||depth>=8){limited=true;return '';}
  seen.add(goal.id);
  const goalStatus=status(goal.status),evidence=arr(goal.evidence).filter(item=>item&&typeof item==='object');
  const children=nodes.filter(node=>node.parent_id===goal.id).map(node=>goalView(node,depth+1)).join('');
  return `<li class="goal-node" data-key="goal-${esc(goal.id)}" data-status="${goalStatus}" ${goal===current?'aria-current="step"':''}><div class="goal-line"><span class="goal-check" aria-hidden="true">${goalStatus==='completed'?'☑':'☐'}</span><span class="goal-state">${esc(title(goalStatus))}</span>${goal===current?'<span class="goal-current">Currently chasing</span>':''}${goal.retired===true?'<span class="goal-retired">No longer needed · not verified</span>':''}</div><p class="goal-objective">${esc(label(goal))}</p><p class="goal-condition"><strong>Complete when:</strong> ${esc(text(goal.completion?.description,'Awaiting a recorded completion condition.'))}</p>${goalStatus==='blocked'||text(goal.blocked_reason)?`<p class="goal-blocked"><strong>Blocked reason:</strong> ${esc(text(goal.blocked_reason,'No blocked reason recorded.'))}</p>`:''}<details class="goal-evidence"><summary>Evidence &amp; verification</summary>${text(goal.completion?.kind)?`<p>Completion check: ${esc(title(goal.completion.kind))}</p>`:''}${evidence.length?`<ul>${evidence.slice(0,4).map(item=>`<li><strong>${esc(title(text(item.source,'Unknown source')))}</strong>${text(item.path)?` · <code>${esc(item.path)}</code>`:''}<blockquote>${esc(text(item.quote,'No quote recorded.'))}</blockquote></li>`).join('')}</ul>${evidence.length>4?'<p>Additional evidence omitted from this compact view.</p>':''}`:'<p>No supporting evidence recorded yet.</p>'}${goal.completion_evidence&&typeof goal.completion_evidence==='object'?`<p><strong>Completion evidence</strong></p><pre class="json-details">${esc(JSON.stringify(goal.completion_evidence,null,2))}</pre>`:'<p>No completion evidence recorded yet.</p>'}</details>${children?`<ul class="goal-checklist" role="list">${children}</ul>`:''}</li>`;
 }
 const hierarchy=root?goalView(root):'';let unlinked='';
 for(const goal of nodes){
  if(limited)break;
  if(!seen.has(goal.id)){invalid=true;unlinked+=goalView(goal);}
 }
 return `<div class="subgoal-plan"><div class="goal-focus"><h3>Currently chasing</h3><p role="status">${current?`${esc(label(current))} <span class="goal-state">${esc(title(status(current.status)))}</span>`:waiting}</p>${recalculateGoalsControl(run)}</div>${current?`<div class="goal-ancestry"><span class="muted">Goal ancestry</span><ol aria-label="Goal ancestry">${partialAncestry?'<li>… (partial ancestry)</li>':''}${ancestry.map(goal=>`<li ${goal===current?'aria-current="step"':''}>${esc(label(goal))}</li>`).join('')}</ol></div>`:''}<p class="goal-plan-status">Planner status: <strong>${esc(title(planStatus))}</strong></p>${text(plan.reason)?`<p class="goal-plan-reason"><strong>${planStatus==='blocked'?'Planner blocked:':'Planner reason:'}</strong> ${esc(plan.reason)}</p>`:''}<h3 class="goal-heading">Goal checklist</h3>${hierarchy?`<ul class="goal-checklist" role="list" aria-label="Goal checklist">${hierarchy}</ul>`:''}${unlinked?`<h3 class="goal-heading">Unlinked goals</h3><ul class="goal-checklist" role="list" aria-label="Unlinked goals">${unlinked}</ul>`:''}${!nodes.length?'<p class="section-note">Awaiting observations and planning. No goals recorded.</p>':''}${invalid?'<p class="section-note">Some goal records or hierarchy links are missing, duplicated, or cyclic.</p>':''}${limited?'<p class="section-note">Hierarchy display limited to 64 goals and 8 levels.</p>':''}</div>`;
}
function progressView(run){const progress=run.progress||{},completed=arr(progress.completed_quests||progress.quests);return `<section><div class="section-head"><h2>Primary objective</h2></div><p class="objective-copy">${esc(run.subgoal_plan?.primary_goal||run.plan?.primary_goal||run.plan?.players?.[0]?.goal||'No objective recorded')}</p>${subgoalPlanView(run)}<p class="section-note">Overall success requires verified game evidence, not just completed subgoals. You can pause or stop this instance at any time.</p><dl class="definition-list instance-details"><dt>Agent</dt><dd>${run.agent_id?`<a href="#/agents/${esc(run.agent_id)}">${esc(run.agent_profile?.name||run.agent_id)}</a>`:'Legacy session'}</dd><dt>Build</dt><dd><code>${esc(run.build?.source_revision||run.build?.commit||run.plan?.build_id||'Unknown')}</code></dd><dt>World</dt><dd>${esc(title(run.plan?.world_setup?.mode||'unknown'))}</dd><dt>Latest stage</dt><dd>${known(progress.stage||progress.phase||completed.at(-1))}</dd><dt>Last progress</dt><dd>${(progress.last_meaningful_progress||progress.last_progress_at)?date(progress.last_meaningful_progress||progress.last_progress_at):'Unknown'}</dd><dt>Duration cap</dt><dd>${esc(duration(run.plan?.limits?.duration_seconds))}</dd></dl><details class="agent-instructions"><summary>Observed quest progress</summary><pre class="json-details">${esc(JSON.stringify(progress,null,2))}</pre></details></section>`;}
function eventView(e){const type=e.type||e.kind||e.event_type||'event';return `<li class="timeline-item" id="event-${esc(e.id||e.event_id)}"><span class="timeline-time">${time(e.created_at||e.received_at||e.timestamp||e.time)}</span>${icon(type.includes('incident')?'flag':type.includes('action')?'target':type.includes('message')?'message':'bolt')}<div><h3>${esc(e.title||e.summary||title(type))}</h3><p>${esc(e.description||e.message||e.reason||e.payload?.reason||e.payload?.actual||e.payload?.message||e.data?.reason||e.data?.summary||'')}${(e.player_id||e.payload?.player_id)?` <span class="mono">${esc(e.player_id||e.payload?.player_id)}</span>`:''}</p><details><summary>${esc(e.id||e.event_id||'Event details')} · view record</summary><pre class="json-details">${esc(JSON.stringify(e,null,2))}</pre></details></div></li>`;}
function timelineView(events,searchable=false){return `${searchable?`<div class="filter-row"><div class="search-field">${icon('search')}<input id="event-search" type="search" placeholder="Search the entire action ledger…" value="${esc(state.search)}" aria-label="Search action ledger"></div>${button('Search','search-events','search','ghost')}<span class="small muted">${state.search?`${state.events.length} matches loaded · `:''}${number(state.eventTotal)} total records</span></div>`:''}${events.length?`<ol class="timeline">${events.map(eventView).join('')}</ol>`:empty(searchable&&state.search?'No matching records':'No events recorded',searchable?'Search is backed by the retained chronological ledger.':'Activity appears here as the run proceeds.')}${searchable&&state.eventHasMore?`<div style="margin-top:18px">${button('Load more records','more-events','plus','ghost')}</div>`:''}`;}

function runView() {
 const r=state.run,tab=state.parts[2]==='timeline'?'timeline':'live';
 if(!r)return empty('Loading instance','Fetching current game observations.');
 const players=arr(r.plan?.players),status=String(r.state).toLowerCase();
 const body=tab==='timeline'?timelineView(state.events,true):runUsageView(r)+`<div class="workspace-grid"><section><div class="section-head"><h2>Live game</h2><span class="small muted">${players.length} player${players.length===1?'':'s'}</span></div><div class="feed-grid ${players.length===1?'single':''}">${players.map(p=>feed(p,r.players?.[p.id])).join('')}</div><div class="section-head"><h2>Recent activity</h2><a class="text-link" href="#/runs/${esc(r.id)}/timeline">Full ledger ${icon('arrow')}</a></div>${timelineView(arr(r.events).slice(-8).reverse())}</section><aside class="context-panel">${progressView(r)}</aside></div>`;
 return heading('Instance / '+r.id,r.plan?.title||'Instance',`${(r.plan?.transport?.mode||'unknown').toUpperCase()} · ${players.length} player${players.length===1?'':'s'} · ${r.model||r.approved_plan?.model||r.plan?.model||'gpt-6-luna'} · Started ${date(r.started_at||r.created_at)}`,badge(r.state)+(!active(r)&&arr(state.data.leases).some(l=>l.run_id===r.id)?button('Reconcile teardown','reconcile','refresh','ghost'):'')+(r.agent_id?`<a class="button ghost" href="#/new/${esc(r.agent_id)}">${icon('plus')}New instance</a>`:''))+
  `${pauseNotice(r)}${active(r)?`<div class="run-controls">${r.human_control?'':button(['paused','investigating'].includes(status)?'Resume inputs':'Pause bot inputs',['paused','investigating'].includes(status)?'resume':'pause',['paused','investigating'].includes(status)?'play':'pause')}${status==='investigating'?button('Extend investigation','extend','clock','ghost'):''}<span class="spacer"></span>${button('Stop run','stop','stop','ghost')}${button('Terminate now','terminate','close','danger')}<span class="world-status"><span class="dot ${['paused','investigating'].includes(status)?'warn':'good'}"></span>World ${(r.world_simulating??r.progress?.world_simulating)===false?'not simulating':(r.world_simulating??r.progress?.world_simulating)===true?'simulating':'simulation unknown'}</span></div>`:`<div class="run-info"><span>Ending reason <strong>${esc(title(r.ending_reason||'unknown'))}</strong></span></div>`}<nav class="tabs" aria-label="Instance views"><a class="tab ${tab==='live'?'active':''}" href="#/runs/${esc(r.id)}/live" ${tab==='live'?'aria-current="page"':''}>Live game</a><a class="tab ${tab==='timeline'?'active':''}" href="#/runs/${esc(r.id)}/timeline" ${tab==='timeline'?'aria-current="page"':''}>Action ledger</a></nav>${body}`;
}
function countdown(v){if(!v)return 'Unknown';const remaining=Math.max(0,Math.ceil((dateObj(v).getTime()-Date.now())/1000));return `${Math.floor(remaining/60).toString().padStart(2,'0')}:${(remaining%60).toString().padStart(2,'0')}`;}
function chatgptConnected(ai){return ai?.configured===true&&ai.auth_method==='chatgpt';}
function codexAuthUrl(value) {
 const url=new URL(value);
 if(url.protocol!=='https:'||!['auth.openai.com','chatgpt.com'].includes(url.hostname)||url.username||url.password||url.port)throw new Error('The sign-in service returned an unrecognized login URL.');
 return url.href;
}
function codexSettingsView() {
 const ai=state.data.ai||{},connected=chatgptConnected(ai),login=state.codexLogin;
 return section('ChatGPT subscription','Sign in on the official ChatGPT page to connect Codex to this app.',`<div class="credential-state">${icon('users')}<div><strong>${connected?'ChatGPT connected':'ChatGPT sign-in required'}</strong><span>${esc(ai.message||'Connect your ChatGPT account to tune agents and play the game.')}</span></div><span style="margin-left:auto">${badge(ai.status||'not_connected')}</span></div><div class="key-value-grid"><div><span>Selected model</span><strong>${esc(ai.model||'gpt-6-luna')}</strong></div><div><span>Connection</span><strong>Codex &middot; ChatGPT</strong></div><div><span>Usage</span><strong>Subscription${ai.plan_type?` &middot; ${esc(title(ai.plan_type))}`:''}</strong></div></div>${login?`<div class="notice"><div><strong>Complete sign-in in your browser</strong><p>Open the official sign-in page, finish signing in, then return here.</p><p><a class="button primary" href="${esc(login.auth_url)}" target="_blank" rel="noopener noreferrer">Continue with ChatGPT ${icon('external')}</a></p><p role="status">${Date.now()<login.pollUntil?'Waiting for ChatGPT sign-in. This page checks for up to five minutes.':'Automatic checks have stopped. Use Refresh sign-in status after completing sign-in.'}</p></div></div>`:''}<div class="form-actions">${!connected&&!login?button('Sign in with ChatGPT','codex-login','users','primary'):''}${button('Refresh sign-in status','codex-status','refresh','ghost')}${login?button('Cancel sign-in','codex-cancel','close','ghost'):''}${connected?button('Sign out','codex-logout','close','danger'):''}</div><div class="provider-disclosure"><strong>Subscription usage</strong><br>Codex uses your ChatGPT subscription and its account limits. Requests are recorded as subscription usage, with token counts when available. API-equivalent estimates are shown for comparison, not as subscription charges or dollar caps. Historical API usage retains its original charges.${ai.rate_limits?`<details><summary>Reported account limits</summary><pre class="json-details">${esc(JSON.stringify(ai.rate_limits,null,2))}</pre></details>`:''}<br><br><strong>Sent to Codex</strong><br>Tuning reads game source files to prepare a controls guide. Gameplay agents receive that guide, structured game state, available actions, and their primary objective. Desktop images remain for human observation and evidence. Hidden live diagnostics and other players' private views are excluded.</div>`);
}
async function refreshCodexStatus() {
 const epoch=state.authEpoch;
 const result=await api('/api/settings/codex/status');
 if(epoch!==state.authEpoch)return;
 state.data.ai=result.ai||result;
 if(chatgptConnected(state.data.ai)&&state.codexLogin){state.codexLogin=null;toast('ChatGPT connected.');}
}
async function startCodexLogin() {
 const epoch=++state.authEpoch;
 const result=await api('/api/settings/codex/login','POST',{});
 if(epoch!==state.authEpoch)return;
 try {
  const auth_url=codexAuthUrl(result.auth_url);
  if(!result.login_id)throw new Error('The sign-in service did not return a login session.');
  state.codexLogin={auth_url,login_id:result.login_id,pollUntil:Date.now()+300000};
 } catch(error) {
  await api('/api/settings/codex/login/cancel','POST',{});
  throw error;
 }
 if(state.route==='settings'&&(!state.parts[1]||state.parts[1]==='ai'))render(true);
}
function settingsView(){const tab=['ai','builds','workers','accounts','operation'].includes(state.parts[1])?state.parts[1]:'ai',d=state.data;const tabs=[['ai','AI connection'],['builds','Builds'],['workers','Environments'],['accounts','Steam accounts'],['operation','Operation']];let body='';if(tab==='ai')body=codexSettingsView();else if(tab==='operation'){const s=d.settings;body=section('Capacity & ceilings','One occupied instance is allowed globally, including paused, human-owned and unresolved work. Legacy saved caps do not increase this limit.',`<form id="settings-form"><div class="field-grid"><div class="field"><label for="concurrency">Global active instance limit</label><input id="concurrency" type="number" value="1" disabled><small>Fixed for this release.${Number(s.saved_concurrent_sessions)>1?` Saved legacy value: ${Number(s.saved_concurrent_sessions)} (not enforced).`:''}</small></div><div class="field"><label for="reserve">Storage admission reserve (bytes)</label><input id="reserve" name="storage_reserve_bytes" type="number" min="1" value="${esc(s.storage_reserve_bytes||1073741824)}" required></div><div class="field"><label for="default-duration">Default duration ceiling (seconds)</label><input id="default-duration" name="duration_seconds" type="number" min="1" value="${esc(s.duration_seconds)}" placeholder="Unset — no ceiling"></div></div><p class="section-note">Existing runs retain their approved configuration. ChatGPT subscription usage has no per-run dollar cap. Investigation extension does not extend a run-duration ceiling.</p><div class="form-actions"><button class="button primary" type="submit">${icon('save')}Save settings</button></div></form>`)+section('Observed host resources','Unknown metrics are shown explicitly. Hardware size does not establish usable player capacity.',hardwareView(s.hardware||{}));}else{
 const configs={builds:{name:'Build artifacts',singular:'build',copy:'Import a verified manifest with the source revision, engine, artifact hash, and runtime configuration.',ico:'code'},workers:{name:'Player environments',singular:'environment',copy:'Each worker owns an independent rendered desktop, input channel, capture, and isolated game data.',ico:'monitor'},accounts:{name:'Reusable Steam accounts',singular:'account',copy:'Human setup handles authentication. One persistent account is reserved for each planned Steam player.',ico:'users'}};const c=configs[tab]||configs.builds;body=`<div class="section-head"><div><h2>${c.name}</h2><p>${c.copy}</p></div>${button('Add '+c.singular,'add-entity','plus','primary',`data-kind="${tab}"`)}</div>${arr(d[tab]).length?arr(d[tab]).map(e=>entityRow(e,tab)).join(''):empty(`No ${c.name.toLowerCase()} configured`,c.copy,button('Add '+c.singular,'add-entity',c.ico,'ghost',`data-kind="${tab}"`))}${tab==='workers'?notice('A shared Windows cursor cannot independently control several clients. Connect one isolated worker desktop per active player; the worker setup link opens its own desktop.'):''}${tab==='accounts'?notice('Sign in to Steam in the assigned worker desktop and complete Steam Guard there. Readiness is verified from that worker; App ID 480 is not a substitute for the selected game.','warning'):''}`;}
 if(tab==='builds')body=section('Game projects','Stable ownership for builds, saved agents and instance history; names do not merge games.',`${button('Create project','create-project','plus','primary')}${button('Import existing workspace…','import-workspace',null,'ghost')}${d.workspace_import?.interrupted?notice('An import was interrupted. Editing and admission are blocked. Keep the journal, retained evidence and verified backup for operator reconciliation.','error'):''}<ul>${arr(d.projects).map(project=>`<li>${esc(project.name)} · ${arr(d.builds).filter(build=>build.project_id===project.id).length} builds</li>`).join('')}</ul>${arr(d.projects).length?'':'<p class="section-note">Create a project before registering its build. Existing unassigned history is retained until an explicit migration.</p>'}`)+body;
 return heading('System','Settings','Configure the provider, game builds, independent desktops, and operating capacity.')+`<div class="settings-layout"><nav class="tabs settings-tabs" aria-label="Settings categories">${tabs.map(([key,label])=>`<a class="tab ${tab===key?'active':''}" href="#/settings/${key}" ${tab===key?'aria-current="page"':''}>${label}</a>`).join('')}</nav>${body}</div>`;}
function safeUrl(value){try{if(typeof value!=='string'||!value.trim())return '#';const url=new URL(value,location.origin);return ['http:','https:'].includes(url.protocol)&&!url.username&&!url.password?url.href:'#';}catch{return '#';}}
function entityRow(e,kind){let description='',meta='',actions='';if(kind==='builds'){description=e.manifest_path||e.path||'';meta=`${esc(e.commit||e.manifest?.commit||e.source_revision||'Revision unknown')} · ${esc(e.engine_version||e.manifest?.engine_version||'Runtime unknown')}`;}if(kind==='workers'){description=e.url||'';meta=`${esc(e.id)} · ${e.enabled===false?'Disabled':'Enabled'} · ${esc(e.host_address||'Host address unspecified')}`;actions=button('Probe','probe-worker','refresh','ghost small',`data-id="${esc(e.id)}"`)+button('Steam setup','steam-start','users','ghost small',`data-id="${esc(e.id)}"`)+button('Finish setup','steam-finish','check','ghost small',`data-id="${esc(e.id)}"`)+(e.desktop_url?`<a class="button ghost small" href="${esc(safeUrl(e.desktop_url))}" target="_blank" rel="noopener noreferrer">Desktop ${icon('external')}</a>`:'');}if(kind==='accounts'){description=`Steam ID ${e.steam_id||'unspecified'} · App ID ${e.app_id||'unspecified'}`;meta=`Worker ${esc(e.worker_id||'unassigned')} · ${esc(title(e.readiness||'human_sign_in_required'))}`;if(e.worker_id)actions=button('Verify identity & App ID','steam-verify','shield','ghost small',`data-id="${esc(e.id)}"`)+button('Status','steam-status','info','ghost small',`data-id="${esc(e.worker_id)}"`);}return `<article class="setup-entity"><div><h3>${icon(kind==='builds'?'code':kind==='workers'?'monitor':kind==='accounts'?'users':'folder')}${esc(e.name||e.id)}${e.status?badge(e.status):''}</h3><p class="mono">${esc(description)}</p><div class="meta-row">${meta}</div>${e.last_error?`<p class="danger-text">${esc(e.last_error)}</p>`:''}</div><div class="actions">${actions}${button('Edit','edit-entity',null,'ghost small',`data-kind="${kind}" data-id="${esc(e.id)}"`)}<button class="icon-button" data-action="delete-entity" data-kind="${kind}" data-id="${esc(e.id)}" aria-label="Delete ${esc(e.name||e.id)}">${icon('trash')}</button></div></article>`;}
function hardwareView(h){return `<div class="key-value-grid"><div><span>CPU utilization</span><strong>${h.cpu_percent==null?'Unknown':`${number(h.cpu_percent)}%`}</strong></div><div><span>Memory utilization</span><strong>${h.memory_percent==null?'Unknown':`${number(h.memory_percent)}%`}</strong></div><div><span>Memory available</span><strong>${bytes(h.memory_available)}</strong></div><div><span>Disk free</span><strong>${bytes(h.disk_free)}</strong></div><div><span>GPU / video memory</span><strong>${h.gpu==null?'Unknown':esc(typeof h.gpu==='string'?h.gpu:JSON.stringify(h.gpu))}</strong></div></div>`;}
function usageView() {
 const usage=arr(state.data.usage),assigned=usage.filter(row=>row.run_id),unassigned=usage.filter(row=>!row.run_id);
 const types=['tuning','gameplay','other'],latencies=usage.map(row=>row.latency_seconds).filter(value=>value!=null);
 return heading('System','Usage & capacity','Per-instance token usage and equivalent API prices; subscription billing is unchanged.')+
  usageSummaryView(assigned)+instanceUsageView(usage,state.data.runs)+pricingNote()+
  `<p class="section-note">Billing: ${esc(totalCost(usage))}. Average request latency: ${latencies.length?`${(latencies.reduce((sum,value)=>sum+Number(value),0)/latencies.length).toFixed(2)}s`:'Unknown'}.</p><div class="usage-breakdown">${types.map(kind=>{const rows=usage.filter(row=>{const purpose=row.kind||row.purpose||row.category;return purpose===kind||(kind==='gameplay'&&purpose==='player')||(kind==='other'&&!['tuning','gameplay','player'].includes(purpose));});return `<div class="usage-type"><h3>${title(kind)}</h3><strong>${rows.length}</strong><p>requests · ${totalCost(rows)}</p></div>`;}).join('')}</div><details class="usage-details"><summary>Unassigned usage · ${unassigned.length} requests (excluded from instance total)</summary>${usageSummaryView(unassigned)}${usageLedger(unassigned)}</details><div class="section-head"><h2>Provider ledger</h2><span class="small muted">Includes assigned and unassigned requests</span></div>${usageLedger(usage)}<div class="section-head"><h2>Host resources</h2><a class="button ghost small" href="#/settings/operation">Operating settings ${icon('arrow')}</a></div>${hardwareView(state.data.settings.hardware||{})}`;
}

function modal(name,body,submitLabel,onSubmit,{danger=false,wide=false}={}){const d=$('#dialog');d.className=wide?'feed-detail-modal':'';stopCameras(d);d.innerHTML=`<div class="dialog-head"><h2 id="dialog-title">${esc(name)}</h2><button type="button" class="icon-button" data-action="close-dialog" aria-label="Close dialog">${icon('close')}</button></div><form id="modal-form"><div class="dialog-body">${body}</div>${submitLabel?`<div class="dialog-actions">${button('Cancel','close-dialog',null,'ghost')}<button class="button ${danger?'danger':'primary'}" type="submit">${esc(submitLabel)}</button></div>`:''}</form>`;const form=$('#modal-form');form.dataset.requestId=crypto.randomUUID();form.onsubmit=async e=>{e.preventDefault();const btn=$('button[type="submit"]',form);if(btn?.disabled)return;if(!form.reportValidity())return;try{if(btn)btn.disabled=true;await onSubmit(Object.fromEntries(new FormData(form)),form);if(d.open&&$('#modal-form')===form)d.close();}catch(error){if(d.open&&$('#modal-form')===form)toast(errorMessage(error),true);}finally{if(btn)btn.disabled=false;}};if(!d.open)d.showModal();bindImages();}
function dialogField(label,name,value='',type='text',help='',required=false){return `<div class="field"><label for="modal-${name}">${esc(label)}</label>${type==='textarea'?`<textarea id="modal-${name}" name="${esc(name)}" ${required?'required':''}>${esc(value)}</textarea>`:`<input id="modal-${name}" name="${esc(name)}" type="${type}" value="${esc(value)}" ${required?'required':''} ${type==='password'?'autocomplete="new-password" spellcheck="false"':''}>`}${help?`<small>${esc(help)}</small>`:''}</div>`;}
function dialogSelect(label,name,options,value){return `<div class="field"><label for="modal-${name}">${esc(label)}</label><select id="modal-${name}" name="${esc(name)}">${options.map(o=>{const [v,l]=Array.isArray(o)?o:[o,title(o)];return `<option value="${esc(v)}" ${String(value??'')===String(v)?'selected':''}>${esc(l)}</option>`;}).join('')}</select></div>`;}
function importWorkspaceModal(){
 const body=notice('Only controller SQLite and run evidence are copied. This is NOT a full workspace backup. Codex home, registrations, external builds, OS keyring and Docker volumes are inventory only; never paste credentials. Import requires an empty target, stopped source controller/watchdog and confirmed idle worker/account pool.','warning')+
  dialogField('Legacy controller data root','source_root','','text','Absolute folder containing ashad.sqlite3 and runs. No paths are detected or imported automatically.',true)+
  dialogField('Fresh verified-backup destination','backup_dir','','text','An absolute, new folder outside both source and target roots.',true)+
  dialogField('Worker registration locations (one absolute path per line)','registrations','','textarea')+
  dialogField('External build locations (one absolute file/folder per line)','build_paths','','textarea','Must cover every recorded manifest, source, catalog and brief path.')+
  dialogField('Docker volume names (one per line)','volumes','','textarea')+
  dialogField('Keyring service names only (one per line)','services','ASHAD.Bot.Local','textarea','Inventory of references only; no passwords, tokens or keyring exports.')+
  '<label class="checkbox-row"><input name="idle_confirmed" type="checkbox" required> I stopped the source controller/watchdog, verified the worker/account pool is idle and reconciled all ownership.</label>'+
  '<label class="checkbox-row"><input name="backup_confirmed" type="checkbox" required> I made and verified separate backups of Codex/configuration, registrations, external builds and persistent volumes, and reviewed credential portability.</label>';
 modal('Review existing workspace import',body,'Confirm backed-up import',async(data)=>{
  const lines=value=>String(value||'').split(/\r?\n/).map(line=>line.trim()).filter(Boolean);
  const source=data.source_root;
  await api('/api/workspace/import','POST',{source_root:source,backup_dir:data.backup_dir,confirmed:true,
   source_idle_confirmed:data.idle_confirmed==='on',external_backup_confirmed:data.backup_confirmed==='on',
   external_inventory:{codex_home:source+'/codex',worker_registration_paths:lines(data.registrations),external_build_paths:lines(data.build_paths),docker_volumes:lines(data.volumes),keyring_services:lines(data.services)}});
  state.dataRevision++;await refresh();render();toast('History imported. Project grouping and current sign-in/desktop readiness still require explicit review.');
 });
}
function createProjectModal(){modal('Create game project',dialogField('Project name','name','','text','A project owns one game’s builds, saved agents and instance history. Creating it never tunes or launches.',true),'Create project',async(data,form)=>{await api('/api/projects','POST',{name:data.name,request_id:form.dataset.requestId});state.dataRevision++;await refresh();render();toast('Project created. Register its pinned build next.');});}
function entityModal(kind,entity={}){const edit=Boolean(entity.id);const names={builds:'build',workers:'environment',accounts:'Steam account'};let fields=dialogField('Name','name',entity.name||'','text','',true);if(kind==='builds')fields+=dialogSelect('Game project','project_id',[['','Select a project'],...arr(state.data.projects).map(project=>[project.id,project.name])],entity.project_id||(arr(state.data.projects).length===1?state.data.projects[0].id:''))+dialogField('Build manifest path','manifest_path',entity.manifest_path||'','text','An absolute path accessible to the coordinator. Import verifies artifact and source hashes.',true)+(globalThis.window?.ashadDesktop?.chooseBuild?`<div class="actions">${button('Choose manifest…','choose-build',null,'ghost','data-kind="manifest"')}${button('Choose build folder…','choose-build',null,'ghost','data-kind="folder"')}</div>`:'');if(kind==='workers')fields+=dialogField('Worker service URL','url',entity.url||'','url','For example http://worker-host:8766',true)+dialogField(edit?'Replace worker token (leave empty to retain)':'Worker authentication token','token','','password','Stored only by the backend credential store.',!edit)+dialogField('Desktop setup URL','desktop_url',entity.desktop_url||'','url','The worker noVNC desktop, commonly port 6080.')+dialogField('Private host address','host_address',entity.host_address||'','text','Reachable ENet host address for other environments.')+`<label class="checkbox-row"><input type="checkbox" name="enabled" ${entity.enabled===false?'':'checked'}>Environment enabled for allocation</label>`;if(kind==='accounts')fields+=dialogSelect('Assigned environment','worker_id',entityOptions('workers','Select environment'),entity.worker_id)+dialogField('Persistent Steam ID','steam_id',entity.steam_id||'','text','Complete sign-in in the worker desktop first.',true)+dialogField('Real game App ID','app_id',entity.app_id||'','number','Use the selected licensed game App ID.',true);modal(`${edit?'Edit':'Add'} ${names[kind]}`,`<div class="field-grid" style="grid-template-columns:1fr">${fields}</div>`,'Save '+names[kind],async (data,form)=>{
 const payload={...data};if(edit)payload.id=entity.id;if(kind==='builds'){if(!data.project_id)throw new Error('Select a game project first. Create one in Settings → Builds if needed.');if(!edit)payload.id='build_'+form.dataset.requestId;}if(kind==='workers'){payload.enabled=data.enabled==='on';if(!data.token)delete payload.token;}if(kind==='accounts'){payload.app_id=Number(data.app_id);payload.readiness=entity.readiness||'human_sign_in_required';}await api(`/api/${kind}`,'POST',payload);state.dataRevision++;await refresh();render();toast(`${title(names[kind])} saved.`);});}

async function loadEvents(append=false) {
 const runId = state.parts[1];
 const epoch = state.renderEpoch;
 const search = state.search;
 const after = append ? state.eventNext : 0;
 const data = await api(`/api/runs/${encodeURIComponent(runId)}/events?after=${after}&limit=200&search=${encodeURIComponent(search)}`);
 // A previous route or search no longer owns the visible ledger.
 if (epoch !== state.renderEpoch || runId !== state.parts[1] || search !== state.search) return;
 state.events = append ? [...state.events, ...arr(data.events)] : arr(data.events);
 state.eventNext = data.next ?? state.events.length;
 state.eventTotal = data.total ?? state.events.length;
 state.eventHasMore = data.has_more ?? (arr(data.events).length >= 200 && state.eventNext < state.eventTotal);
}
async function loadRun() {
 const id = state.parts[1],epoch=state.renderEpoch,previous=state.run;
 const run = await api(`/api/runs/${encodeURIComponent(id)}`);
 if (state.parts[1]===id&&epoch===state.renderEpoch&&state.run===previous) state.run=run;
}

async function startTakeover(playerId) {
 const run=state.run,epoch=state.renderEpoch;
 const result=await api(`/api/runs/${encodeURIComponent(run.id)}/players/${encodeURIComponent(playerId)}/takeover`,'POST',{});
 await refresh();
 if(epoch!==state.renderEpoch||state.parts[1]!==run.id){toast('Manual control started. Open the run to access its desktop or hand back to bots.');return;}
 await loadRun();
 if(epoch!==state.renderEpoch||state.parts[1]!==run.id)return;
 render();
 const url=safeUrl(result.desktop_url);
 if(url==='#'||new URL(url).searchParams.has('password'))throw new Error('Bots are paused, but the desktop address is invalid. Hand back to bots or correct the worker desktop URL.');
 const player=arr(run.plan?.players).find(p=>p.id===playerId);
 modal(`Take over ${player?.name||playerId}`,`<p>All bots are paused. Copy the desktop password, open the game desktop, then connect and use your keyboard and mouse to play.</p>${result.password?`<div class="field desktop-password"><label for="takeover-password">Desktop password</label><div><input id="takeover-password" value="${esc(result.password)}" readonly autocomplete="off" spellcheck="false">${button('Copy','copy-desktop-password','key','ghost')}</div></div>`:'<p>Use the desktop password configured for this worker if the viewer asks for one.</p>'}<p><a class="button primary" href="${esc(url)}" target="_blank" rel="noopener noreferrer">Open game desktop ${icon('external')}</a></p><p>The desktop opens in a separate tab. If your browser blocks it, allow this link to open a new tab.</p><p>Closing the desktop or this dialog leaves all bots paused. Return here and choose <strong>Hand back to bots</strong> when you are done.</p><div class="actions">${button('Hand back to bots','finish-takeover','play','ghost')}${button('Close instructions','close-dialog',null,'ghost')}</div>`,null,null);
}
function finishTakeoverDialog() {
 const run=state.run;
 modal('Hand back to bots',`<p>Desktop control will close before bots can act again. They will read the updated game state and continue toward their goals.</p><div class="field-grid" style="grid-template-columns:1fr">${dialogField('Recovery instructions (optional)','reason','','textarea','For example: I crossed the bridge. Continue the tutorial from here. Leave empty to continue the current goal.')}${dialogSelect('After closing desktop control','after',[['resume','Resume all bots'],['paused','Leave all bots paused']],'resume')}</div>`,'Finish manual control',async data=>{
  const resume=data.after!=='paused';
  await api(`/api/runs/${encodeURIComponent(run.id)}/takeover/finish`,'POST',{resume,reason:data.reason||''});
  await refresh();
  if(state.route==='runs'&&state.parts[1]===run.id){await loadRun();render();}
  toast(resume?'Desktop control closed. Bots resumed.':'Desktop control closed. All bots remain paused.');
 });
}
function controlDialog(command) {
 const run = state.run;
 if(command==='resume'&&run.human_control)return finishTakeoverDialog();
 const labels = {pause:'Pause all bot inputs',resume:'Resume inputs',stop:'Stop run',terminate:'Terminate now',extend:'Extend investigation'};
 const descriptions = {
  pause:'Hold all bot inputs and gameplay decisions. Capture continues when available. The game world may keep simulating.',
  resume:'Resume bot inputs using fresh game observations. You can continue without entering instructions. Optional instructions guide the next decisions.',
  stop:'Release controls and request graceful shutdown. Available evidence and the report are finalized. Saving depends on support from the game and worker.',
  terminate:'End this session and its game processes now. No text is required. A fresh game save is not guaranteed; available evidence is retained.',
  extend:'Extend the time available to investigate. The run-duration ceiling remains unchanged.'
 };
 const note = command==='terminate' ? '' : dialogField(command==='resume'?'Recovery instructions (optional)':'Note (optional)','reason','','textarea',command==='resume'?'For example: try the nearby path before returning to the objective. Leave empty to continue the current goal.':'Saved in the action ledger. You can leave this empty.');
 modal(labels[command],`${command==='resume'&&run.pause?.reason?`<div class="notice warning"><div><strong>Paused because:</strong> ${esc(run.pause.reason)}${run.pause.recovery?`<p>${esc(run.pause.recovery)}</p>`:''}</div></div>`:''}<p>${descriptions[command]}</p><div class="field-grid" style="grid-template-columns:1fr">${note}${command==='extend'?dialogField('Additional investigation seconds','seconds',1800,'number','1 to 86400 seconds.',true):''}</div>`,labels[command],async (data,form)=>{
  await api(`/api/runs/${encodeURIComponent(run.id)}/control`,'POST',{command,reason:data.reason||'',command_id:form.dataset.requestId,...(command==='extend'?{seconds:Number(data.seconds)}:{})});
  await loadRun();render();toast(`${labels[command]} recorded.`);
 },{danger:command==='terminate'});
}

function recalculateGoalsDialog(trigger) {
 const run=state.run,epoch=state.renderEpoch;
 if(!canRecalculateGoals(run)||goalRecalculations.get(run.id)?.busy||goalRecalculations.get(run.id)?.cancelling||run.goal_recalculation?.status==='planning')return;
 const current=()=>epoch===state.renderEpoch&&state.route==='runs'&&state.parts[1]===run.id&&state.run?.id===run.id;
 const previous=goalRecalculations.get(run.id);
 modal('Recalculate goals',`<p>Hold bot inputs, cancel the current decision, and read fresh game-state JSON to generate a new plan under the unchanged primary objective, model, and agent profile.</p><p>Retire the old unfinished checklist, not mark it completed. The previous plan and its evidence remain in the action ledger.</p><p><strong>Inputs remain paused.</strong> Review the new goals, then choose Resume inputs separately when you are ready.</p><p class="section-note" role="${previous?.error?'alert':'status'}" data-recalculate-status>${previous?.error?`${esc(previous.error)} Retry checks the same request without repeating a completed replan.`:''}</p>`,'Recalculate goals',async (_data,form)=>{
  if(!current()||goalRecalculations.get(run.id)?.busy||goalRecalculations.get(run.id)?.cancelling||state.run.goal_recalculation?.status==='planning')return;
  if(!canRecalculateGoals(state.run))throw new Error('Recalculation is no longer available. Check the run state and control owner.');
  const request={commandId:goalRecalculations.get(run.id)?.commandId||form.dataset.requestId,busy:true,error:'',done:false};
  goalRecalculations.set(run.id,request);
  const status=$('[data-recalculate-status]',form),submit=$('button[type="submit"]',form),cancel=$('[data-action="close-dialog"]',form);
  cancel.textContent='Cancel recalculation';cancel.dataset.action='cancel-recalculation';
  form.setAttribute('aria-busy','true');
  status.setAttribute('role','status');status.textContent='Holding inputs and replanning from fresh game state… This may take a minute.';
  submit.textContent='Recalculating goals…';render(true);
  try {
   const updated=await api(`/api/runs/${encodeURIComponent(run.id)}/recalculate-goals`,'POST',{command_id:request.commandId});
   if(goalRecalculations.get(run.id)!==request)return;
   if(updated?.id!==run.id)throw new Error('Recalculation returned an unexpected instance. Retry to check the same request.');
   state.dataRevision++;
   state.data.runs=[updated,...arr(state.data.runs).filter(item=>item.id!==run.id)];
   request.commandId=null;request.done=true;
   if(current())state.run=updated;
  } catch(error) {
   if(goalRecalculations.get(run.id)!==request)return;
   request.error=errorMessage(error);
   status.setAttribute('role','alert');status.textContent=`${request.error} Retry checks the same request without repeating a completed replan.`;
   throw error;
  } finally {
   request.busy=false;form.setAttribute('aria-busy','false');submit.textContent='Recalculate goals';
   cancel.textContent='Cancel';cancel.dataset.action='close-dialog';
   if(goalRecalculations.get(run.id)===request&&current())render(true);
  }
 });
 const dialog=$('#dialog'),form=$('#modal-form');
 dialog.addEventListener('close',()=>{if(current()&&$('#modal-form')===form&&trigger?.isConnected)trigger.focus();},{once:true});
}

async function cancelGoalRecalculation(trigger) {
 const run=state.run,epoch=state.renderEpoch,request=goalRecalculations.get(run?.id)||{};
 if(!canRecalculateGoals(run)||request.cancelling||!(request.busy||run.goal_recalculation?.status==='planning'))return;
 if(trigger?.closest('#modal-form'))$('#dialog').close();
 const current=()=>epoch===state.renderEpoch&&state.route==='runs'&&state.parts[1]===run.id&&state.run?.id===run.id;
 request.cancelling=true;request.cancelError='';request.cancelCommandId ||= crypto.randomUUID();
 goalRecalculations.set(run.id,request);render(true);
 let applied=false;
 try {
  const updated=await api(`/api/runs/${encodeURIComponent(run.id)}/control`,'POST',{command:'pause',command_id:request.cancelCommandId,reason:'Cancel goal recalculation'});
  if(goalRecalculations.get(run.id)!==request)return;
  if(updated?.id!==run.id)throw new Error('Cancellation returned an unexpected instance.');
  // Retire the local request too: its delayed response no longer owns this run.
  const alreadyFinished=updated.goal_recalculation?.status==='completed';
  goalRecalculations.set(run.id,{commandId:alreadyFinished?null:request.commandId,cancelled:!alreadyFinished,alreadyFinished});
  state.dataRevision++;state.data.runs=[updated,...arr(state.data.runs).filter(item=>item.id!==run.id)];
  if(current())state.run=updated;
  applied=true;
 } catch(error) {
  request.cancelError=`Could not cancel recalculation: ${errorMessage(error)}`;
 } finally {
  request.cancelling=false;
  if(current()){render(true);if(applied)$('[data-action="recalculate-goals"]')?.focus();}
 }
}

const actions={
 menu:()=>{const open=document.body.classList.toggle('nav-open');syncNavigation();(open?$('.sidebar a'):$('.mobile-menu'))?.focus();},
 'close-dialog':()=>$('#dialog').close(),
 'choose-build':chooseBuildManifest,
 new:()=>navigate(arr(state.data.agents).length===1?`new/${state.data.agents[0].id}`:'agents'),
 pause:()=>controlDialog('pause'),resume:()=>controlDialog('resume'),stop:()=>controlDialog('stop'),terminate:()=>controlDialog('terminate'),extend:()=>controlDialog('extend'),
 takeover:el=>startTakeover(el.dataset.player),
 'finish-takeover':finishTakeoverDialog,
 'recalculate-goals':recalculateGoalsDialog,
 'cancel-recalculation':cancelGoalRecalculation,
 'copy-desktop-password':async()=>{const field=$('#takeover-password');if(!field)return;try{await navigator.clipboard.writeText(field.value);toast('Desktop password copied.');}catch{field.focus();field.select();toast('Password selected. Copy it, then paste it in the desktop connection dialog.');}},

 'expand-feed':el=>{const p=state.run.plan.players.find(p=>p.id===el.dataset.player);modal(`${p.name||p.id} · ${title(p.role)}`,feed(p,state.run.players?.[p.id],true),null,null,{wide:true});},
 'search-events':async()=>{state.search=$('#event-search').value;await loadEvents();render();},
 'more-events':async()=>{await loadEvents(true);render();},

 reconcile:()=>modal('Reconcile worker teardown',`<p>Check retained worker bindings and process status before releasing unresolved environment or account leases. A lease remains reserved unless shutdown can be confirmed.</p>`,'Check teardown & leases',async()=>{await api(`/api/runs/${encodeURIComponent(state.run.id)}/reconcile`,'POST',{});await refresh();await loadRun();render();toast('Teardown reconciliation completed. Inspect remaining limitations and leases.');}),

 'import-workspace':()=>importWorkspaceModal(),
 'create-project':()=>createProjectModal(),
 'add-entity':el=>entityModal(el.dataset.kind),
 'edit-entity':el=>entityModal(el.dataset.kind,state.data[el.dataset.kind].find(x=>x.id===el.dataset.id)),
 'delete-entity':el=>{const entity=state.data[el.dataset.kind].find(x=>x.id===el.dataset.id);modal('Remove configuration',`<p>Remove <strong>${esc(entity?.name||el.dataset.id)}</strong> from ${esc(el.dataset.kind)}. Active leases prevent removal.</p>`,'Remove',async()=>{await api(`/api/${el.dataset.kind}/${encodeURIComponent(el.dataset.id)}`,'DELETE');await refresh();render();toast('Configuration removed.');},{danger:true});},
 'probe-worker':async el=>{const r=await api(`/api/workers/${encodeURIComponent(el.dataset.id)}/probe`,'POST',{});await refresh();render();modal('Environment probe',`<pre class="json-details" style="max-height:60vh">${esc(JSON.stringify(r,null,2))}</pre>`,null,null);},
 'steam-start':async el=>{const r=await api(`/api/workers/${encodeURIComponent(el.dataset.id)}/steam/start`,'POST',{});const w=state.data.workers.find(x=>x.id===el.dataset.id);modal('Steam authentication setup',`<p>Complete Steam sign-in and Steam Guard in this environment's desktop. This authentication setup is kept outside gameplay-agent capture.</p>${w.desktop_url?`<a class="button primary" href="${esc(safeUrl(w.desktop_url))}" target="_blank" rel="noopener noreferrer">Open worker desktop ${icon('external')}</a>`:''}<pre class="json-details">${esc(JSON.stringify(r,null,2))}</pre>`,null,null);},
 'steam-status':async el=>{const r=await api(`/api/workers/${encodeURIComponent(el.dataset.id)}/steam/status`,'POST',{});await refresh();render();modal('Verified Steam status',`<pre class="json-details">${esc(JSON.stringify(r,null,2))}</pre>`,null,null);},
 'steam-finish':el=>modal('Finish Steam setup',`<p>Close the human authentication setup and return this independent desktop to managed capture. Complete sign-in before finishing setup.</p>`,'Finish setup',async()=>{await api(`/api/workers/${encodeURIComponent(el.dataset.id)}/steam/finish`,'POST',{});await refresh();render();toast('Steam authentication setup finished.');}),
 'steam-verify':el=>{const account=state.data.accounts.find(a=>a.id===el.dataset.id);modal('Verify Steam identity & game',`<p>Start the selected game build in this worker to verify the actual signed-in identity, real App ID, and licensed access. This check does not mark an offline fallback as a Steam pass.</p>${dialogSelect('Selected game build','build_id',entityOptions('builds','Select build'),state.data.builds[0]?.id)}`,'Verify account & App ID',async (data,form)=>{if(!data.build_id)throw new Error('Select the game build for verification.');const result=await api(`/api/workers/${encodeURIComponent(account.worker_id)}/steam/verify`,'POST',{account_id:account.id,build_id:data.build_id});await refresh();render();toast(result.message||'Steam verification completed. Inspect the account readiness status.');});},
 'codex-login':startCodexLogin,
 'codex-status':async()=>{await refreshCodexStatus();render(true);},
 'codex-cancel':async()=>{state.authEpoch++;await api('/api/settings/codex/login/cancel','POST',{});state.codexLogin=null;await refreshCodexStatus();render(true);toast('ChatGPT sign-in cancelled.');},
 'codex-logout':()=>modal('Sign out of ChatGPT for ASHAD',`<p>Sign out of the ChatGPT connection used by this app. Future bot decisions and agent tuning will need sign-in again. Your Codex desktop sign-in and retained run records are unchanged.</p>`,'Sign out',async()=>{state.authEpoch++;await api('/api/settings/codex/logout','POST',{});state.codexLogin=null;await refreshCodexStatus();render(true);toast('ASHAD signed out of ChatGPT.');},{danger:true}),


};
function render(background=false){const views={projects:()=>state.parts[2]==='new'?globalThis.projectComposerView?.(state.parts[1]):globalThis.projectOverviewView?.(state.parts[1]),history:()=>globalThis.unassignedHistoryView?.(),sessions:sessionsView,agents:agentsView,new:draftView,runs:runView,settings:settingsView,usage:usageView};shell((views[state.route]||sessionsView)(),background);document.title=`ASHAD — ${state.route==='runs'?(state.run?.plan?.title||'Instance'):title(state.route==='new'?'New instance':state.route)}`;}
async function route() {
 const next=location.hash.replace(/^#\/?/, '').split('/').filter(Boolean).join('/');
 if(globalThis.workspaceBeforeNavigate&&!await globalThis.workspaceBeforeNavigate(next)){globalThis.history?.replaceState(null,'',`#/${state.parts.join('/')}`);return;}
 if(!allowDraftNavigation(next)){globalThis.history?.replaceState(null,'',`#/${state.parts.join('/')}`);return;}
 if ($('#dialog')?.open) $('#dialog').close();
 const epoch = ++state.renderEpoch;
 document.body.classList.remove('nav-open');
 const previous = state.parts.join('/');
 state.parts = location.hash.replace(/^#\/?/, '').split('/').filter(Boolean);
 state.route = state.parts[0] || 'projects';
 if(!['projects','history','sessions','agents','new','runs','settings','usage'].includes(state.route))state.route='projects';
 if(state.route==='settings'&&!['ai','builds','workers','accounts','operation',undefined].includes(state.parts[1]))state.parts[1]='ai';
 if(state.route==='agents'&&!state.parts[1]&&(previous!==state.parts.join('/')||!state.draft))state.draft={build_id:state.data.builds[0]?.id||'',model:state.data.ai?.model||'gpt-6-luna'};
 if (state.route === 'new') {
  if(previous!==state.parts.join('/')||!state.draft) {
   state.draft={primary_goal:'',transport:'enet',request_id:crypto.randomUUID()};
   state.launchError='';
  }
 } else if (state.route === 'runs') {
  await loadRun();
  if (epoch !== state.renderEpoch) return;
  if (state.parts[2] === 'timeline') {
   if (previous !== state.parts.join('/')) state.search = '';
   await loadEvents();
  }
 } else if (state.route==='settings'&&(!state.parts[1]||state.parts[1]==='ai')) {
  await refreshCodexStatus();
  if(epoch!==state.renderEpoch)return;
 }
 if (epoch === state.renderEpoch) {render();if(previous!==state.parts.join('/'))globalThis.window?.scrollTo(0,0);}
}
document.addEventListener('click',async event=>{const el=event.target.closest('[data-action]');if(!el||el.disabled)return;const action=actions[el.dataset.action];if(!action)return;event.preventDefault();try{el.disabled=true;await action(el);}catch(error){toast(errorMessage(error),true);}finally{el.disabled=false;}});
document.addEventListener('input',event=>{const el=event.target;if(el.dataset.path&&state.draft)set(state.draft,el.dataset.path,el.value);if(el.closest('#instance-form,#tune-agent-form,#settings-form'))markDraftDirty(true);});
document.addEventListener('change',event=>{const el=event.target;if(el.dataset.path&&el.tagName==='SELECT')el.dispatchEvent(new Event('input',{bubbles:true}));});
document.addEventListener('keydown',event=>{if(event.target.id==='event-search'&&event.key==='Enter'){event.preventDefault();actions['search-events']().catch(e=>toast(errorMessage(e),true));}});
document.addEventListener('submit',async event=>{
 const form=event.target;
 if(!['settings-form','tune-agent-form','instance-form'].includes(form.id))return;
 event.preventDefault();
 const submit=$('button[type="submit"]',form);
 if(submit.disabled)return;
 submit.disabled=true;
 try {
  if(form.id==='tune-agent-form')await tuneAgent();
  else if(form.id==='instance-form')await startInstance();
  else {
   const data=Object.fromEntries(new FormData(form));
   for(const key of ['storage_reserve_bytes','duration_seconds'])data[key]=data[key]===''?null:Number(data[key]);
   await api('/api/settings','POST',data);markDraftDirty(false);await refresh();render();toast('Operating settings saved.');
  }
 }catch(error){toast(errorMessage(error),true);}finally{submit.disabled=false;}
});
window.addEventListener('hashchange',()=>route().catch(error=>toast(errorMessage(error),true)));
window.addEventListener('resize',syncNavigation);
window.addEventListener('beforeunload',event=>{if(state.dirtyDraft&&!window.ashadDesktop){event.preventDefault();event.returnValue='';}});
$('#dialog').addEventListener('close',()=>{stopCameras($('#dialog'));const password=$('#takeover-password');if(password){password.value='';password.removeAttribute('value');}});
setInterval(()=>{$$('[data-clock]').forEach(el=>el.textContent=new Date().toLocaleTimeString());$$('[data-deadline]').forEach(el=>el.textContent=countdown(el.dataset.deadline));$$('[data-age]').forEach(el=>{if(el.dataset.age)el.textContent=ago(isNaN(Number(el.dataset.age))?el.dataset.age:Number(el.dataset.age));});},1000);
async function poll() {
 if (!state.data || state.polling || document.hidden || $('#dialog').open) return;
 state.polling = true;
 const epoch = state.renderEpoch;
 try {
  if(state.route==='settings'&&(!state.parts[1]||state.parts[1]==='ai')&&state.codexLogin) {
   if(Date.now()<state.codexLogin.pollUntil)await refreshCodexStatus();
   if(epoch===state.renderEpoch)render(true);
  } else if (state.route === 'runs' && ['live', undefined].includes(state.parts[2])) {
   await loadRun();
   await refresh();
   if (epoch === state.renderEpoch) render(true);
  } else if (['projects','history','sessions','usage'].includes(state.route)) {
   await refresh();
   if (epoch === state.renderEpoch) render(true);
  } else {
   await refresh();
  }
 } catch {
  state.connected = false;
 } finally {
  state.polling = false;
  updateStatusUI();
 }
}
setInterval(poll, 5000);
globalThis.ashadWorkspaceContext={state,api,esc,arr,heading,section,notice,modal,markDraftDirty,navigate,refresh,render,patchHTML,errorMessage,toast};
async function init(){try{await refresh();await route();}catch(error){$('#app').innerHTML=`<div class="boot"><span class="boot-mark">A</span><h2>ASHAD is unavailable</h2><p>${esc(errorMessage(error))}</p><button class="button primary" id="retry-connection">Reconnect ${icon('refresh')}</button></div>`;$('#retry-connection').onclick=init;}}
document.addEventListener('DOMContentLoaded',init,{once:true});
function structuredInspector(status){const c=status.structured_context;if(!c)return '';const a=c.active_action||{};return `<details class="structured-inspector"><summary>Game context · ${esc(c.ready?'ready':'menus / synchronizing')} · revision ${esc(c.revision)}</summary><p>Observed ${ago(c.timestamp)} · ${arr(c.entities).length} visible targets · ${c.complete?'complete view':'partial view'}</p><p>Action: ${esc(a.verb||'none')} ${esc(a.status||'')}</p><p>${esc(status.agent_memory||'No saved subgoal')}</p><pre>${esc(JSON.stringify({player:c.state?.player,objectives:c.state?.objectives,entities:c.entities},null,2))}</pre></details>`;}
})();
