'use strict';
// Classic-script integration contract:
// globalThis.ashadInstanceContext = {esc(value), arr(value), date(unixSeconds),
//   time(unixSeconds), nowSeconds?: number}. No other parent helpers are used.
// Set nowSeconds once per parent render for deterministic freshness/age; absent clock = unknown.
// Ages are emitted as data-age / data-stale-at so the parent's 1s ticker keeps aging them between polls.
// events: ledger array or {events, total?: nonnegative integer, older_count?: nonnegative integer}.
// Parent owns fetching/pagination, composition and patching. These views never perform I/O.
// selectedDecision: an actual ledger event, not an inferred/matched description.
(() => {
 const C = () => globalThis.ashadInstanceContext;
 const e = value => C().esc(typeof value==='string' && value.length>2000 ? `${value.slice(0,2000)} [omitted ${value.length-2000} characters]` : value);
 const list = value => C().arr(value);
 const object = value => value && typeof value === 'object' && !Array.isArray(value) ? value : {};
 const exists = value => value !== undefined && value !== null && value !== '';
 const count = value => Number.isSafeInteger(value) && value >= 0 ? value : null;
 const labels = {observing:'Observing', planning:'Planning', selecting:'Selecting', acting:'Acting', checking:'Checking outcome', waiting:'Waiting', blocked:'Blocked / error', human:'Human / lifecycle', unknown:'Instance activity'};
 const kinds = {
  structured_decision_context:'observing', subgoal_planning_context:'planning',
  subgoal_plan_updated:'planning', subgoal_plan_reset:'planning', subgoal_plan_recalculation_prepared:'planning',
  goal_recalculation_started:'planning', decision_selected:'selecting', decision_cancelled:'selecting',
  structured_action_intent:'acting', action_result:'acting', structured_action_outcome:'checking', subgoals_verified:'checking',
  agent_turn_failed:'blocked', agent_task_failed:'blocked',
  subgoal_plan_rejected:'blocked', goal_recalculation_failed:'blocked', investigation_started:'blocked', incident_flag:'blocked',
  human_control_failed:'blocked', resume_failed:'blocked', schedule_failed:'blocked',
  launch_approved:'human', capacity_reserved:'human', player_launched:'human', player_ready:'human', world_ready:'human',
  session_running:'human', run_paused:'human', inputs_released:'human', intervention:'human',
  human_control_requested:'human', human_control_started:'human', human_control_finished:'human', human_control_lost:'human',
  coordinator_restarted:'human', stopping:'human', teardown_result:'human', teardown_reconciled:'human', run_ended:'human',
  goal_changed:'planning', goal_recalculation_cancelled:'planning', subgoal_planning_cancelled:'planning', agent_task_cancelled:'human'
 };
 const operational = new Set(['launch_approved','player_launched','player_ready','world_ready','session_running','run_paused',
  'intervention','human_control_requested','human_control_started','human_control_finished','human_control_lost','human_control_failed',
  'coordinator_restarted','investigation_started','incident_flag','resume_failed','stopping','teardown_result','teardown_reconciled','run_ended',
  'goal_changed','subgoal_plan_updated','subgoal_plan_reset','goal_recalculation_started','subgoal_plan_recalculation_prepared',
  'goal_recalculation_failed','goal_recalculation_cancelled','subgoals_verified','agent_turn_failed','agent_task_failed','subgoal_plan_rejected']);

 // Retained IDs are losslessly UTF-16 encoded; defensive overlong legacy identities use bounded hashes.
 // No page indices or prose parsing establish record identity.
 function token(value) {
  const source = String(value ?? '');
  if (source.length>200) {
   let a = 2166136261, b = 5381;
   for (let i=0;i<source.length;i++) { a = Math.imul(a ^ source.charCodeAt(i),16777619); b = Math.imul(b,33) ^ source.charCodeAt(i); }
   return `long-${source.length}-${(a>>>0).toString(16)}-${(b>>>0).toString(16)}`;
  }
  return Array.from(source, ch => {
  let encoded = '';
  for (let i = 0; i < ch.length; i++) encoded += ch.charCodeAt(i).toString(16).padStart(4, '0');
  return encoded;
 }).join('') || 'none'; }
 function anchor(event, run) {
  const identity = exists(event.id) ? `id:${event.id}` : exists(event.seq) ? `seq:${event.seq}` :
   `legacy:${event.kind || event.type || 'unknown'}:${event.timestamp ?? ''}:${JSON.stringify(event)}`;
  return `activity-${token(event.run_id ?? run?.id)}-${token(identity)}`;
 }
 function page(input) {
  const envelope = object(input), all = list(Array.isArray(input) ? input : envelope.events);
  const events = all.filter(item => item && typeof item === 'object').map((event, index) => ({event,index}));
  events.sort((a,b) => Number.isSafeInteger(a.event.seq) && Number.isSafeInteger(b.event.seq) ? a.event.seq-b.event.seq : a.index-b.index);
  const retained = events.slice(-100).map(item => item.event), omitted = events.length-retained.length;
  const total = count(envelope.total), prior = count(envelope.older_count);
  return {events:retained, older:prior !== null ? prior+omitted : total !== null ? Math.max(0,total-retained.length) :
   omitted > 0 ? omitted : null, omitted, total};
 }
 function category(event) {
  const p = object(event.payload), result = object(p.result);
  if (event.kind === 'provider_request_started' || event.kind === 'provider_request_finished') {
   if (event.kind === 'provider_request_finished' && ['unknown','failed','rejected','expired'].includes(resultStatus(p.status))) return 'blocked';
   return p.purpose === 'planning' ? 'planning' : p.purpose === 'player' ? 'selecting' : 'unknown';
  }
  if (event.kind === 'agent_turn_completed') return result.status==='dispatched' ? object(result.action).tool==='wait_for_change' ? 'waiting' : 'acting' : 'unknown';
  if (event.kind === 'structured_tool_result') {
   if (p.tool === 'observe') return 'observing';
   if (p.tool === 'wait_for_change') return 'waiting';
   if (p.tool === 'get_action_status') return 'checking';
   if (p.tool === 'perform_action' || p.tool === 'cancel_action') return ['rejected','failed','expired'].includes(result.status) ? 'blocked' : 'acting';
  }
  if (['action_result','structured_action_outcome'].includes(event.kind) && ['rejected','failed','expired'].includes(result.status)) return 'blocked';
  return typeof event.kind==='string' && Object.hasOwn(kinds,event.kind) ? kinds[event.kind] : 'unknown';
 }
 function resultStatus(status) {
  if (status === 'request_cancelled') return 'cancelled';
  return ['requested','dispatched','accepted','running','completed','rejected','cancelled','failed','expired','waiting'].includes(status) ? status : 'unknown';
 }
 function status(event) {
  const p = object(event.payload), result = object(p.result);
  if (['structured_action_intent','provider_request_started','human_control_requested','intervention','stopping','goal_recalculation_started'].includes(event.kind)) return 'requested';
  if (event.kind === 'launch_approved') return 'accepted';
  if (['decision_cancelled','subgoal_planning_cancelled','goal_recalculation_cancelled','agent_task_cancelled'].includes(event.kind)) return 'cancelled';
  if (event.kind === 'subgoal_plan_rejected') return 'rejected';
  if (event.kind === 'provider_request_finished') return resultStatus(p.status);
  if (['action_result','structured_action_outcome','structured_tool_result','agent_turn_completed'].includes(event.kind)) return resultStatus(result.status);
  // A selected candidate is not an action request and a lifecycle event is not input delivery.
  return 'unknown';
 }
 function correlation(event) {
  const p = object(event.payload), result = object(p.result), context = object(p.context);
  const observation = object(context.observation), full = object(p.full_observation), evidence = object(p.evidence);
  return {event:event.id, sequence:event.seq, run:event.run_id ?? p.run_id, player:event.player_id ?? p.player_id ?? object(context.player).id,
   agent:event.agent_id ?? p.agent_id,
   request:['provider_request_started','provider_request_finished'].includes(event.kind) ? p.id : p.request_id,
   action:p.action_id ?? result.action_id ?? object(p.input).action_id,
   observation:p.observation_id ?? p.source_observation ?? observation.observation_id ?? full.observation_id ?? evidence.observation_id ?? result.observation_id,
   intent:p.intent_event_id, command:p.command_id};
 }
 function attribution(event, run) {
  const c = correlation(event);
  if (exists(c.player)) {
   const player = list(run?.plan?.players).find(item => item?.id === c.player);
   return `${player?.name || c.player} · Player ${c.player}`;
  }
  if (exists(c.agent)) return `Agent ${c.agent}`;
  return 'Instance activity';
 }
 // Bound raw records too: native details are lazy disclosures, not lazy DOM creation.
 // ponytail: JSON evidence is a compact preview; parent ledger retains the complete record.
 function bounded(value, budget, depth = 0) {
  budget.characters ??= 20000;
  if (budget.nodes-- <= 0) return '[omitted: preview node limit]';
  if (budget.characters<=0) return '[omitted: preview text budget]';
  if (typeof value === 'string') {
   const retained = Math.min(value.length,2000,budget.characters);
   budget.characters -= retained;
   return retained<value.length ? `${value.slice(0,retained)} [omitted ${value.length-retained} characters]` : value;
  }
  if (value === null || typeof value !== 'object') return value;
  if (depth >= 7) return '[omitted: preview depth limit]';
  if (Array.isArray(value)) {
   const items = value.slice(0,20).map(item => bounded(item,budget,depth+1));
   if (value.length > 20) items.push(`[omitted ${value.length-20} array entries]`);
   return items;
  }
  const entries = Object.entries(value), result = Object.create(null);
  for (const [index,[key,item]] of entries.slice(0,40).entries()) {
   const retainedKey = key.length>200 ? `${key.slice(0,200)} [omitted ${key.length-200} key characters; field ${index}]` : key;
   budget.characters -= retainedKey.length;
   result[retainedKey] = bounded(item,budget,depth+1);
  }
  if (entries.length > 40) result['[omitted keys]'] = entries.length-40;
  return result;
 }
 // Activity cards re-render on every poll, so each card's raw preview gets a small budget.
 function raw(value, heading = 'Raw record · bounded preview', key = '', budget = {nodes:500,characters:20000}) {
  return `<details class="instance-raw" data-key="${e(key || heading)}"><summary>${e(heading)}</summary><p>Preview: arrays ≤20, fields ≤40, depth ≤7, ≤${budget.nodes} values, text ≤2000 characters per value, ${budget.characters}-character text budget; omissions are explicit. Complete evidence remains in the full ledger.</p><pre>${C().esc(JSON.stringify(bounded(value,{...budget}),null,2) ?? 'Unknown')}</pre></details>`;
 }
 function text(value) { return typeof value === 'string' ? value : exists(value) ? JSON.stringify(bounded(value,{nodes:40})) : ''; }
 function summary(event) {
  const p = object(event.payload), result = object(p.result);
  if (event.kind === 'decision_selected') return text(object(p.action).description) || text(p.target) || 'An action candidate was selected; dispatch is not recorded in this event.';
  if (event.kind === 'structured_action_intent') return text(p.reason) || text(object(p.input).verb) || 'A structured action was requested.';
  if (event.kind === 'subgoal_plan_updated' || event.kind === 'subgoal_plan_recalculation_prepared') return text(object(p.plan).reason);
  if (event.kind === 'provider_request_started') return `Provider request started (${p.purpose || 'purpose unknown'}).`;
  if (event.kind === 'provider_request_finished') return text(p.message) || `Provider request finished (${p.status || 'status unknown'}).`;
  if (event.kind === 'structured_decision_context') {
   const observation = object(object(p.context).observation ?? p.full_observation);
   return `Retained observation ${observation.observation_id ?? 'ID unknown'} · revision ${observation.revision ?? 'unknown'} for action selection.`;
  }
  if (event.kind === 'intervention') return `Operator requested ${p.command || 'an unknown command'}${p.reason?`: ${text(p.reason)}`:'.'}`;
  if (event.kind === 'launch_approved') return 'Explicit launch accepted. This record does not confirm game startup.';
  if (event.kind === 'player_launched') return 'Player launch returned; gameplay readiness is recorded separately.';
  if (event.kind === 'session_running') return 'Controller recorded the instance running.';
  if (event.kind === 'human_control_started') return 'Worker confirmed human desktop control; bot inputs are paused.';
  if (event.kind === 'human_control_finished') return 'Worker confirmed desktop access closed. Bot resume is a separate operation.';
  if (event.kind === 'subgoals_verified') return 'Controller recorded local subgoal verification. Overall objective acceptance remains separate.';
  if (event.kind === 'monitor_observation') return 'Monitor-only diagnostics. This is not a model observation.';
  return text(p.reason || p.error || p.message || result.reason || p.goal || p.actual);
 }
 function relatedLinks(event, events, run) {
  const own = correlation(event), matches = events.filter(other => {
   if (other === event) return false;
   const c = correlation(other);
   if ((own.run ?? run?.id) !== (c.run ?? run?.id)) return false;
   if (exists(own.player) && exists(c.player) && own.player !== c.player) return false;
   return (exists(own.intent) && own.intent === other.id) ||
    (exists(c.intent) && c.intent === event.id) ||
    (exists(own.request) && own.request === c.request) ||
    (exists(own.action) && own.action === c.action) ||
    (exists(own.observation) && own.observation === c.observation);
  });
  return matches.length ? `<nav class="activity-related" aria-label="Correlated evidence">Related records: ${matches.slice(0,4).map(other => `<a href="#${anchor(other,run)}" data-activity-anchor="${anchor(other,run)}">Sequence ${e(other.seq ?? 'unknown')}</a>`).join(' · ')}${matches.length>4?` · ${matches.length-4} more correlated records in this page`:''}</nav>` : '';
 }
 function card(event, events, run) {
  const kind = category(event), c = correlation(event), eventStatus = status(event), p = object(event.payload);
  const evidence = Object.entries(c).filter(([,value]) => exists(value));
  return `<li class="activity-card" id="${anchor(event,run)}" data-key="${anchor(event,run)}" data-seq="${e(event.seq ?? '')}" data-category="${kind}" data-status="${eventStatus}" tabindex="-1"><header><strong>${e(labels[kind])}</strong><span>${e(attribution(event,run))}</span><time>${e(exists(event.timestamp)?C().time(event.timestamp):'Unknown time')}</time></header><h3>${e(event.kind || event.type || 'Unknown event type')}</h3><p class="activity-status">Recorded status: <strong>${eventStatus}</strong>${eventStatus==='completed'&&['action_result','structured_action_outcome','structured_tool_result'].includes(event.kind)?' · Input/action completion is not proof of gameplay success.':''}</p>${summary(event)?`<p class="activity-summary">${e(summary(event))}</p>`:''}${event.kind==='agent_turn_completed'&&object(p.result).status==='dispatched'?`<p>Dispatch recorded; returned action status: ${e(resultStatus(object(object(p.result).result).status))}. Gameplay effect requires observation evidence.</p>`:''}${evidence.length?`<dl class="activity-correlation">${evidence.map(([key,value])=>`<dt>${e(key)}</dt><dd><code>${e(value)}</code></dd>`).join('')}</dl>`:'<p>Identity/correlation not recorded.</p>'}${relatedLinks(event,events,run)}${raw(event,'Raw event · bounded preview',`${anchor(event,run)}-raw`,{nodes:40,characters:2000})}</li>`;
 }
 function activityCardsView(input, run) {
  const p = page(input), first = p.events[0]?.seq, last = p.events.at(-1)?.seq;
  return `<section class="instance-activity" aria-label="Agent activity" data-first-seq="${e(first ?? '')}" data-last-seq="${e(last ?? '')}" data-older-count="${e(p.older ?? '')}"><div class="activity-page-meta">${p.events.length} retained records shown (latest 100 maximum) · Older records: ${p.older===null?'unknown':p.older}${p.total!==null?` · ${p.total} total`:''}. Older history is outside this rendered page.</div>${p.events.length?`<ol class="activity-cards">${p.events.map(event=>card(event,p.events,run)).join('')}</ol>`:'<p>No retained activity records. Activity state is unknown.</p>'}<button type="button" data-instance-action="activity-live-edge" class="activity-new" hidden>New activity · return to live edge</button></section>`;
 }
 function recentActivityView(input, run) {
  const events = page(input).events.filter(event=>operational.has(event.kind)).slice(-8).reverse();
  return `<section class="recent-activity" aria-label="Recent operational activity"><h3>Recent activity</h3>${events.length?`<ul>${events.map(event=>`<li><a href="#${anchor(event,run)}" data-activity-anchor="${anchor(event,run)}"><time>${e(exists(event.timestamp)?C().time(event.timestamp):'Unknown time')}</time> ${e(event.kind)} · ${e(attribution(event,run))}</a>${summary(event)?`<p>${e(summary(event))}</p>`:''}</li>`).join('')}</ul>`:'<p>No retained operational events in this activity page.</p>'}</section>`;
 }
 function group(label, value, key) {
  return `<section class="observation-group" data-key="world-${e(key)}"><h4>${e(label)}</h4>${value===undefined||value===null?'<p>Unknown · not retained in this observation.</p>':Array.isArray(value)&&!value.length?'<p>Observed empty array (0 entries).</p>':`<pre>${C().esc(JSON.stringify(bounded(value,{nodes:180,characters:8000}),null,2))}</pre>`}${Array.isArray(value)&&value.length>20?`<p>${value.length-20} entries omitted from this group preview.</p>`:''}</section>`;
 }
 function observationView(observation, run, playerID, heading, key) {
  const o = object(observation), state = object(o.state), player = object(state.player);
  if (!Object.keys(o).length) return `<section class="observation-snapshot" data-key="${key}"><h3>${e(heading)}</h3><p>Unknown · no retained structured observation for this player.</p></section>`;
  const identityMismatch = (exists(o.run_id)&&o.run_id!==run?.id)||(exists(o.player_id)&&o.player_id!==playerID);
  const timestamp = typeof o.timestamp==='number'&&Number.isFinite(o.timestamp) ? o.timestamp : null;
  const now = typeof C().nowSeconds==='number'&&Number.isFinite(C().nowSeconds) ? C().nowSeconds : null;
  const age = now!==null&&timestamp!==null ? now-timestamp : null;
  const stale = identityMismatch||age!==null&&(age>5||age < -1);
  const freshness = stale?'Stale / identity or timestamp outside freshness window':age===null?'Freshness unknown · clock or timestamp missing':'Fresh at render time (≤5s)';
  const partial = o.complete===false||state.complete===false||o.context_truncated===true;
  const coverage = partial?'Partial / truncated':o.complete===true?'Complete observed coverage':'Coverage unknown';
  const entities = Array.isArray(o.entities)?o.entities:null;
  const ui = entities?entities.filter(entity=>entity?.kind==='ui'):undefined;
  const playerState = Object.fromEntries(Object.entries(player).filter(([field])=>!['inventory','hotbar'].includes(field)));
  return `<section class="observation-snapshot" data-key="${key}"><h3>${e(heading)}</h3><dl class="observation-meta"><dt>Observation ID</dt><dd><code>${e(o.observation_id ?? 'Unknown')}</code></dd><dt>Revision</dt><dd>${e(o.revision ?? 'Unknown')}</dd><dt>Timestamp</dt><dd>${e(timestamp===null?'Unknown':C().date(timestamp))}</dd><dt>Age</dt><dd>${age===null?'Unknown':`<time data-age="${e(timestamp)}">${Math.max(0,Math.round(age))}s at render time</time>`}</dd><dt>Freshness</dt><dd data-stale="${stale?'true':age===null?'unknown':'false'}" ${!stale&&age!==null?`data-stale-at="${e(timestamp+5)}" data-stale-text="Stale · no newer observation within 5s"`:''}>${e(freshness)}</dd><dt>Coverage</dt><dd>${e(coverage)} · coverage is not goal success or whole-game knowledge.</dd><dt>Readiness</dt><dd>${o.ready===true?'Observed ready':o.ready===false?'Observed not ready':'Unknown'}</dd><dt>Scope</dt><dd>${e(o.scope ?? 'Unknown')}</dd><dt>Binding</dt><dd>${e(o.run_id ?? 'Unknown run')} / ${e(o.player_id ?? 'Unknown player')} / ${e(o.generation ?? 'Unknown generation')}</dd></dl>${identityMismatch?'<p>Retained observation identity differs from the selected run/player. Do not treat it as current state.</p>':''}${group('Player state / position',Object.keys(player).length?playerState:undefined,`${key}-player`)}${group('Inventory',player.inventory,`${key}-inventory`)}${group('Hotbar',player.hotbar,`${key}-hotbar`)}${group('Visible / nearby entities and supported interactions',entities??undefined,`${key}-entities`)}${group('Objectives / observed progress',state.objectives,`${key}-objectives`)}${group('Observed completed quests',state.completed_quests,`${key}-quests`)}${group('Visible UI / modal controls',ui,`${key}-ui`)}<p>UI is observed controls with kind “ui”; modal state is not inferred from labels.</p>${group('Active action',o.active_action,`${key}-action`)}${group('Advertised inputs',o.inputs,`${key}-inputs`)}${raw(o,'Raw structured observation · bounded preview',`${key}-raw`)}</section>`;
 }
 function observedWorldView(run, playerID, selectedDecision) {
  const ps = object(object(run?.players)[playerID]);
  let selected = '';
  if (selectedDecision) {
   const event = object(selectedDecision), p = object(event.payload), c = correlation(event);
   const retained = ['structured_decision_context','subgoal_planning_context'].includes(event.kind) ? object(p.context).observation ?? p.full_observation : undefined;
   const referenced = exists(c.observation);
   const matches = (!exists(event.run_id)||event.run_id===run?.id)&&(!exists(c.player)||c.player===playerID);
   if (!matches) selected = '<section><h3>Selected decision context</h3><p>Unavailable · this event belongs to a different run/player.</p></section>';
   else if (retained && typeof retained==='object' && !Array.isArray(retained)) selected = observationView(retained,run,playerID,'State retained for selected decision','decision-observation');
   else if (referenced) selected = `<section><h3>Selected decision context</h3><p>Recorded observation reference: <code>${e(c.observation)}</code>. Decision-time context is not retained in this event. Latest state below is not a reconstruction of the decision input.</p></section>`;
   else selected = '<section><h3>Selected decision context</h3><p>Unknown · this event retains neither decision context nor an observation reference.</p></section>';
  }
  return `<section class="observed-world" aria-label="Observed world state"><p>Player-scoped retained observations. Camera freshness is separate; rendering does not request a new observation.</p>${selected}${observationView(ps.structured_context,run,playerID,'Latest observed state','latest-observation')}<details class="monitor-diagnostics" data-key="monitor-diagnostics"><summary>Monitor-only diagnostics · not model input</summary><p>Worker status and telemetry below are privileged monitoring records, not facts provided to the model.</p>${raw({telemetry:ps.telemetry??null,worker_status:ps.worker_status??null},'Monitor record · bounded preview','monitor-record')}</details></section>`;
 }
 // Parent calls before/after patching the same scroller; no listeners, globals, timers or I/O.
 function activityScrollSnapshot(node) {
  if (!node) return null;
  const cards = Array.from(node.querySelectorAll('.activity-card'));
  const top = node.getBoundingClientRect().top;
  const visible = cards.find(card=>card.getBoundingClientRect().bottom>=top);
  return {scrollTop:node.scrollTop,liveEdge:node.scrollHeight-node.clientHeight-node.scrollTop<=32,
   lastSeq:Number(node.querySelector('.instance-activity')?.dataset.lastSeq || node.dataset.lastSeq || 0),
   anchorID:visible?.id,anchorOffset:visible?visible.getBoundingClientRect().top-top:0,
   hasUnread:node.querySelector('.activity-new')?.hidden===false};
 }
 function restoreActivityScroll(node, snapshot) {
  if (!node || !snapshot) return;
  const last = Number(node.querySelector('.instance-activity')?.dataset.lastSeq || node.dataset.lastSeq || 0);
  if (snapshot.liveEdge) node.scrollTop = Math.max(0,node.scrollHeight-node.clientHeight);
  else {
   node.scrollTop = snapshot.scrollTop;
   const retained = Array.from(node.querySelectorAll('.activity-card')).find(card=>card.id===snapshot.anchorID);
   if (retained) node.scrollTop += retained.getBoundingClientRect().top-node.getBoundingClientRect().top-snapshot.anchorOffset;
  }
  const indicator = node.querySelector('.activity-new');
  if (indicator) indicator.hidden = snapshot.liveEdge || (!snapshot.hasUnread && last<=snapshot.lastSeq);
 }
 Object.assign(globalThis,{activityCardsView,observedWorldView,recentActivityView,activityScrollSnapshot,restoreActivityScroll});
})();
