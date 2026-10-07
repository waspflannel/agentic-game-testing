'use strict';
// Exercise the actual async dashboard loaders with controlled HTTP responses.
// No browser, game, controller, or provider service is started.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const {test} = require('node:test');

const source = fs.readFileSync(path.join(__dirname, '../ashad/static/app.js'), 'utf8');
const loaders = source.slice(source.indexOf('async function loadEvents('), source.indexOf('function controlDialog('));
const routing = source.slice(source.indexOf('async function route()'), source.indexOf("document.addEventListener('click'"));
const feedSource = source.slice(source.indexOf('const feedStaleSeconds'), source.indexOf('\n', source.indexOf('function feed(')));
const timeHelpers = source.split('\n').filter(line=>/^const (dateObj|ago) =/.test(line)).join('\n');

test('live camera URL stays connected through polling and follows player ownership', () => {
 const app=vm.createContext({
  state:{run:{id:'run',state:'Running'}}, esc:String, icon:()=>'', badge:()=>'',
  active:r=>r.state==='Running', agentDiagnostics:()=>'', button:()=>'', Date,
 });
 vm.runInContext(timeHelpers+'\n'+feedSource,app);
 const player={id:'player',name:'Bot',role:'host',initial:true};
 const status={launched:true,epoch:'epoch',feed_captured_at:1};
 const url=html=>html.match(/<img[^>]*src="([^"]+)"/)[1];
 const first=url(app.feed(player,status));
 assert.equal(first,'/api/runs/run/players/player/live?epoch=epoch');
 assert.equal(url(app.feed(player,{...status,feed_captured_at:2})),first);
 assert.notEqual(url(app.feed(player,{...status,epoch:'new-owner'})),first);
 app.state.run.state='Ended';
 assert.doesNotMatch(app.feed(player,status),/<img/);
});

test('camera overlay shows capture age, turns stale without new captures, and never claims live without a capture time', () => {
 const app=vm.createContext({state:{run:{id:'run',state:'Running'}},esc:String,icon:()=>'',badge:()=>'',active:r=>r.state==='Running',agentDiagnostics:()=>'',button:()=>'',Date});
 vm.runInContext(timeHelpers+'\n'+feedSource,app);
 const player={id:'player',initial:true},now=Date.now()/1000,overlay=html=>html.match(/<div class="feed-overlays">([^]*?)<\/div>/)[1];
 const fresh=overlay(app.feed(player,{launched:true,feed_captured_at:now-2}));
 assert.match(fresh,/LIVE VIEW/);assert.match(fresh,new RegExp(`data-age="${now-2}"`));assert.match(fresh,/data-stale-at="[\d.]+" data-stale-text="STALE · capture stopped"/);
 assert.equal(Number(fresh.match(/data-stale-at="([\d.]+)"/)[1]),now-2+20);
 const stalled=overlay(app.feed(player,{launched:true,feed_captured_at:now-60}));
 assert.match(stalled,/STALE · capture stopped/);assert.doesNotMatch(stalled,/LIVE VIEW/);
 const unknown=overlay(app.feed(player,{launched:true}));
 assert.match(unknown,/NO CAPTURE TIME/);assert.doesNotMatch(unknown,/LIVE/);
 app.state.run.state='Ended';assert.match(overlay(app.feed(player,{launched:true,feed_captured_at:now})),/OFFLINE/);
});

test('the 1s ticker ages capture/observation times and flips expired freshness labels to stale', () => {
 const fresh={dataset:{staleAt:String(Date.now()/1000+60),staleText:'STALE'},textContent:'LIVE VIEW'};
 const expired={dataset:{staleAt:String(Date.now()/1000-1),staleText:'STALE · capture stopped'},textContent:'LIVE VIEW'};
 const age={dataset:{age:String(Date.now()/1000-90)},textContent:'0s ago'};
 let tick;const app=vm.createContext({Date,isNaN,Number,setInterval:fn=>{tick=fn;},countdown:String,
  $$:selector=>selector==='[data-stale-at]'?[fresh,expired]:selector==='[data-age]'?[age]:[]});
 vm.runInContext(timeHelpers+'\n'+source.split('\n').find(line=>line.startsWith('setInterval(()=>{')),app);
 tick();
 assert.equal(fresh.textContent,'LIVE VIEW');assert.equal(expired.textContent,'STALE · capture stopped');assert.equal(expired.dataset.stale,'true');
 assert.equal(age.textContent,'1m ago');
});

test('a failed live image skipped while hidden is retried when the page becomes visible', () => {
 const listeners={},failed={hidden:true,src:'',getAttribute:()=>'/live?epoch=e',closest:()=>null},inClosedDialog={hidden:true,src:'',getAttribute:()=>'/live',closest:()=>({open:false})};
 const app=vm.createContext({document:{hidden:true,addEventListener:(name,fn)=>{listeners[name]=fn;}},$$:selector=>selector==='img[data-live][hidden]'?[failed,inClosedDialog]:[]});
 const start=source.indexOf("document.addEventListener('visibilitychange'");
 vm.runInContext(source.slice(start,source.indexOf('\n',start)),app);
 listeners.visibilitychange();assert.equal(failed.src,'');
 app.document.hidden=false;listeners.visibilitychange();
 assert.equal(failed.src,'/live?epoch=e');assert.equal(inClosedDialog.src,'');
});

function deferred() {
 let resolve;
 const promise = new Promise(done => { resolve = done; });
 return {promise, resolve};
}

function dashboard(api) {
 const context = vm.createContext({
  api, structuredClone, clone:structuredClone,
  state:{parts:['runs', 'old', 'timeline'], renderEpoch:0, search:'', events:[], data:{runs:[], scenarios:[]}},
  location:{hash:''},
  document:{body:{classList:{remove(){}}}},
  $:() => ({open:false}),
  arr:value => Array.isArray(value) ? value : [],
  render(){}, toast(){},
 });
 vm.runInContext(source.slice(source.indexOf('function markDraftDirty('), source.indexOf('const activeStates =')) + loaders + routing, context);
 return context;
}

test('late ledger responses cannot replace a newer route or search', async () => {
 for (const change of ['route', 'search']) {
  const response = deferred();
  const app = dashboard(() => response.promise);
  const pending = app.loadEvents();
  if (change === 'route') {
   app.state.parts = ['runs', 'new', 'timeline'];
   app.state.renderEpoch++;
  } else {
   app.state.search = 'new query';
  }
  app.state.events = [{id:'current'}];
  response.resolve({events:[{id:'stale'}], next:1, total:1});
  await pending;
  assert.equal(app.state.events[0].id, 'current');
 }
});

test('late live responses cannot overwrite a newly selected instance', async () => {
 const oldRun=deferred(),requested=deferred();
 const app=dashboard(async url=>{if(url==='/api/runs/old'){requested.resolve();return oldRun.promise;}return {id:'new'};});
 app.location.hash='#/runs/old';const pending=app.route();await requested.promise;
 app.location.hash='#/runs/new';await app.route();oldRun.resolve({id:'old'});await pending;
 assert.equal(app.state.run.id,'new');
});

function controls() {
 const app = vm.createContext({
  URL,location:{origin:'http://127.0.0.1:8740'},
  state:{run:{id:'run',state:'Investigating',investigation_deadline:100,
   plan:{players:[{id:'bot',name:'Solo bot'}]},
   pause:{reason:'Action timed out <script>',players:['bot'],recovery:'Check the blocked path, then resume.'}}},
  arr:value=>Array.isArray(value)?value:[], title:String, icon:()=>'',
  ago:value=>`age:${value}`, time:value=>`time:${value}`, number:String,
  countdown:()=> '01:00', active:()=>true, button:()=>'',
  api:async (...args)=>{app.request=args;}, loadRun:async()=>{},render(){},toast(){},
  modal:(...args)=>{app.dialog=args;},refresh:async()=>{},
 });
 vm.runInContext(source.split('\n').find(line=>line.startsWith('const esc =')),app);
 vm.runInContext(source.slice(source.indexOf('function dialogField('),source.indexOf('function entityModal(')),app);
 vm.runInContext(source.split('\n').find(line=>line.startsWith('function safeUrl(')),app);
 vm.runInContext(source.slice(source.indexOf('function pauseNotice('),source.indexOf('function updateStatusUI(')),app);
 vm.runInContext(source.slice(source.indexOf('async function startTakeover('),source.indexOf('const actions=')),app);
 return app;
}

test('termination is a confirmation with no text input and resume instructions are optional', async () => {
 const app=controls();
 app.controlDialog('terminate');
 assert.doesNotMatch(app.dialog[1],/<(?:textarea|input)\b/);
 await app.dialog[3]({}, {dataset:{requestId:'terminate-once'}});
 assert.equal(app.request[2].command,'terminate');
 assert.equal(app.request[2].command_id,'terminate-once');
 assert.equal(app.request[2].reason,'');
 app.controlDialog('resume');
 assert.match(app.dialog[1],/Recovery instructions \(optional\)/);
 assert.doesNotMatch(app.dialog[1],/\brequired\b/);
 await app.dialog[3]({}, {dataset:{requestId:'resume-once'}});
 assert.equal(app.request[2].reason,'');
 await app.dialog[3]({reason:'Try a different path'}, {dataset:{requestId:'resume-again'}});
 assert.equal(app.request[2].reason,'Try a different path');
});

test('pause warning names the trigger, affected bot, recovery and expiry', () => {
 const app=controls(),html=app.pauseNotice(app.state.run);
 assert.match(html,/Warning: bots paused/);
 assert.match(html,/Action timed out &lt;script&gt;/);
 assert.match(html,/Affected: Solo bot/);
 assert.match(html,/Check the blocked path, then resume/);
 assert.match(html,/Automatic termination in/);
 app.state.run.state='Running';
 assert.equal(app.pauseNotice(app.state.run),'');
});

test('takeover controls appear only for launched live players and show who has control', () => {
 const app=controls();
 Object.assign(app,{badge:()=>'',agentDiagnostics:()=>'',Date,
  active:r=>r.state!=='Ended',button:(label,action,_icon,_class,attrs)=>`<button data-action="${action}" ${attrs}>${label}</button>`});
 vm.runInContext(timeHelpers+'\n'+feedSource,app);
 const player={id:'bot',name:'Solo <bot>',initial:true};
 for(const state of ['Running','Paused','Investigating']){
  app.state.run.state=state;
  assert.match(app.feed(player,{launched:true}),/data-action="takeover"/);
 }
 assert.doesNotMatch(app.feed(player,{launched:false}),/data-action="takeover"/);
 app.state.run.human_control={player_id:'bot',status:'active'};
 assert.match(app.feed(player,{launched:true}),/Reopen desktop/);
 assert.match(app.pauseNotice(app.state.run),/Manual control active/);
 assert.match(app.pauseNotice(app.state.run),/Closing the desktop does not resume the bots/);
 app.state.run.human_control={player_id:'other'};
 assert.match(app.feed(player,{launched:true}),/disabled title="Finish the current takeover first"/);
 app.state.run.state='Ended';
 assert.doesNotMatch(app.feed(player,{launched:true}),/data-action="takeover"/);
});

test('unconfirmed desktop ownership keeps handback available and shows the actual failure and deadline', () => {
 const app=controls();
 app.button=(label,action)=>`<button data-action="${action}">${label}</button>`;
 app.state.run.human_control={player_id:'bot',status:'unknown'};
 const html=app.pauseNotice(app.state.run);
 assert.match(html,/Desktop control unconfirmed/);
 assert.doesNotMatch(html,/Manual control active/);
 assert.match(html,/Action timed out &lt;script&gt;/);
 assert.match(html,/Check the blocked path, then resume/);
 assert.match(html,/Automatic termination in/);
 assert.match(html,/data-action="finish-takeover"/);
 assert.match(html,/data-action="takeover"/);
 app.state.run.human_control.status='opening';
 assert.match(app.pauseNotice(app.state.run),/Preparing manual control/);
});

test('takeover waits for paused state before offering an explicit desktop link and a separate password', async () => {
 const app=controls(),calls=[];
 Object.assign(app.state,{renderEpoch:1,parts:['runs','run'],route:'runs'});
 app.api=async(...args)=>{calls.push(args);return {desktop_url:'http://127.0.0.1:6080/vnc.html?resize=scale',password:'local<secret>"'};};
 app.loadRun=async()=>calls.push('loaded');
 app.render=()=>calls.push('rendered');
 await app.startTakeover('bot');
 assert.equal(calls[0][0],'/api/runs/run/players/bot/takeover');
 assert.equal(calls[0][1],'POST');
 assert.deepEqual(calls.slice(1),['loaded','rendered']);
 const html=app.dialog[1];
 assert.match(html,/All bots are paused/);
 assert.match(html,/Copy the desktop password/);
 assert.match(html,/value="local&lt;secret&gt;&quot;" readonly/);
 assert.match(html,/href="http:\/\/127\.0\.0\.1:6080\/vnc\.html\?resize=scale" target="_blank" rel="noopener noreferrer"/);
 assert.doesNotMatch(html,/href="[^"\n]*(?:password|secret)/);
 assert.match(html,/Closing the desktop or this dialog leaves all bots paused/);
 assert.equal(app.state.password,undefined);
});

test('takeover cannot expose unsafe desktop links or open a stale dialog after navigation', async () => {
 for(const desktop_url of ['javascript:alert(1)','http://name:secret@localhost/vnc.html','http://localhost/vnc.html?password=secret',null]){
  const app=controls();Object.assign(app.state,{renderEpoch:1,parts:['runs','run'],route:'runs'});
  app.api=async()=>({desktop_url,password:'secret'});
  await assert.rejects(app.startTakeover('bot'),/desktop address is invalid/);
  assert.equal(app.dialog,undefined);
 }
 const app=controls(),response=deferred();
 Object.assign(app.state,{renderEpoch:1,parts:['runs','run'],route:'runs'});
 app.api=()=>response.promise;
 const pending=app.startTakeover('bot');
 app.state.renderEpoch++;app.state.parts=['runs','another'];
 response.resolve({desktop_url:'http://127.0.0.1:6080/vnc.html',password:'secret'});
 await pending;
 assert.equal(app.dialog,undefined);
});

test('handback resumes without instructions, can leave bots paused, and replaces generic resume', async () => {
 const app=controls();Object.assign(app.state,{parts:['runs','run'],route:'runs'});
 app.state.run.human_control={player_id:'bot'};
 app.controlDialog('resume');
 assert.equal(app.dialog[0],'Hand back to bots');
 assert.match(app.dialog[1],/Recovery instructions \(optional\)/);
 assert.doesNotMatch(app.dialog[1],/\brequired\b/);
 const submit=app.dialog[3];
 await submit({});
 assert.equal(app.request[0],'/api/runs/run/takeover/finish');
 assert.equal(app.request[2].resume,true);
 assert.equal(app.request[2].reason,'');
 app.state.run={id:'another'};
 await submit({after:'paused',reason:'Bridge crossed'});
 assert.equal(app.request[0],'/api/runs/run/takeover/finish');
 assert.equal(app.request[2].resume,false);
 assert.equal(app.request[2].reason,'Bridge crossed');
});

test('live diagnostics expose the failing operation, last error and observation freshness', () => {
 const app=controls();
 const html=app.agentDiagnostics({id:'bot',name:'Solo bot'}, {
  activity:'Moving to target',invalid_actions:3,
  agent_status:{phase:'error',detail:'Decisions request failed',updated_at:10,
   last_error:'HTTP 429 <quota>',action_id:'action-7',completed_at:8,wait_until:12},
  structured_context:{timestamp:9,revision:4},
 });
 assert.match(html,/Decisions request failed/);
 assert.match(html,/HTTP 429 &lt;quota&gt;/);
 assert.match(html,/Last activity: Moving to target/);
 assert.match(html,/data-age="9"/);
 assert.match(html,/revision 4/);
 assert.match(html,/action-7/);
 assert.match(html,/Rejected actions<\/dt><dd>3/);
});

test('historical Decisions player requests count as gameplay with retained API charges', () => {
 const app=usageDashboard([{purpose:'player',model:'gpt-6-luna',usage:{input_tokens:42},estimated_cost_usd:0.0000042,latency_seconds:0.2}]);
 const html=app.usageView();
 assert.match(html,/<h3>gameplay<\/h3><strong>1<\/strong>/);
 assert.match(html,/<th>Input tokens<\/th>/);
 assert.match(html,/0.20s/);
 assert.match(html,/<th>Output tokens<\/th>/);
 assert.match(html,/Recorded API cost/);
 assert.equal(app.apiEstimate(app.state.usage[0]),0.0000042);
});

function usageDashboard(usage=[],runs=[]) {
 const app=vm.createContext({state:{usage,data:{runs,settings:{},ai:{}}},notice:copy=>copy,
  arr:value=>Array.isArray(value)?value:[],heading:()=>'',number:value=>value==null?'Unknown':value.toLocaleString('en-US'),title:String,
  hardwareView:()=>'',icon:()=>'',date:()=>'',money:value=>value==null?'Unknown':`$${value}`,empty:(title,copy)=>`${title} ${copy}`,
 });
 vm.runInContext(source.split('\n').find(line=>line.startsWith('const esc =')),app);
 vm.runInContext(source.slice(source.indexOf('function subscriptionUsage('),source.indexOf('function section(')),app);
 vm.runInContext(source.slice(source.indexOf('function usageView('),source.indexOf('function modal(')),app);
 return app;
}

function subscriptionRow(overrides={}){return {api:'codex',billing:'subscription',model:'gpt-6-luna',usage_scope:'selection_total',usage:{input_tokens:1000,cached_input_tokens:300,cache_write_input_tokens:100,output_tokens:200,reasoning_output_tokens:80,total_tokens:1200},...overrides};}

test('API equivalents price cached reads/writes and output once for each supported model', () => {
 const app=usageDashboard();
 for(const [model,expected] of [['gpt-6-luna',.0001755],['gpt-6-sol',.00351],['gpt-6.1-sol',.00348],['gpt-6-astra',.01755]]){
  const row=subscriptionRow({model});
  assert.ok(Math.abs(app.apiEstimate(row)-expected)<1e-12,model);
  assert.equal(app.usageTokens(row).total,1200);
  assert.equal(app.usageTokens(row).reasoning,80);
 }
 assert.equal(app.apiEstimate(subscriptionRow({model:'gpt-6-sol-unknown'})),null);
 assert.equal(app.estimateMoney(.0000001),'<$0.000001');
 assert.equal(app.estimateMoney(.000004),'$0.000004');
});

test('snake and camel usage normalize identically without adding reasoning to total', () => {
 const app=usageDashboard(),snake=subscriptionRow();
 const camel=subscriptionRow({usage:{inputTokens:1000,cachedInputTokens:300,cacheWriteInputTokens:100,outputTokens:200,reasoningOutputTokens:80,totalTokens:1200}});
 assert.deepEqual(app.usageTokens(snake),app.usageTokens(camel));
 assert.equal(app.apiEstimate(snake),app.apiEstimate(camel));
});

test('missing usage and incomplete cache breakdown keep known subtotals and explicit uncertainty', () => {
 const app=usageDashboard(),good=subscriptionRow();
 const missing=subscriptionRow({usage:null}),noCache=subscriptionRow({usage:{input_tokens:40,output_tokens:6}});
 assert.equal(app.apiEstimate(noCache),null);
 assert.equal(app.apiEstimate(missing),null);
 const summary=app.usageSummary([good,missing,noCache]);
 assert.equal(app.summaryValue(summary,'input'),'1,040 + unknown');
 assert.equal(app.summaryValue(summary,'output'),'206 + unknown');
 assert.match(app.summaryValue(summary,'cost'),/\$0\.000176 \+ unknown/);
 assert.equal(app.summaryValue(app.usageSummary([missing]),'cost'),'Unknown');
 assert.equal(app.apiEstimate(subscriptionRow({usage:{...good.usage,cached_input_tokens:1100}})),null);
 assert.equal(app.usageTokens(subscriptionRow({usage:{input_tokens:-1,output_tokens:NaN}})).input,null);
});

test('run usage summarizes every request but renders a bounded ledger on each poll', () => {
 const app=usageDashboard(),rows=Array.from({length:10000},(_,i)=>subscriptionRow({id:`u${i}`,run_id:'r',created_at:i}));
 const html=app.runUsageView({usage:rows});
 assert.match(html,/10000 recorded requests/);
 assert.equal((html.match(/Request details/g)||[]).length,50);
 assert.match(html,/9950 older requests — see <a href="#\/usage">Usage<\/a>/);
 assert.match(html,/&quot;id&quot;: &quot;u0&quot;/);assert.doesNotMatch(html,/&quot;u50&quot;/);
 assert.ok(html.length<100000,`bounded run usage view: ${html.length}`);
});

test('pricing source is selectable text, never a link the desktop shell would block', () => {
 const app=usageDashboard();
 assert.doesNotMatch(app.pricingNote(),/<a /);
 assert.match(app.pricingNote(),/https:\/\/developers\.openai\.com\/api\/docs\/pricing/);
});

test('Usage page without loaded usage rows reports unavailable instead of an empty ledger', () => {
 const app=usageDashboard();app.state.usage=null;
 assert.match(app.usageView(),/Usage records are not loaded/);assert.doesNotMatch(app.usageView(),/No usage yet/);
});

test('old Codex last-request records retain reported amounts with a partial-scope notice', () => {
 const app=usageDashboard(),old=subscriptionRow({usage_scope:undefined});
 assert.equal(app.apiEstimate(old),app.apiEstimate(subscriptionRow()));
 assert.equal(app.usageSummary([subscriptionRow({usage_scope:'tuning_total'})]).partial,false);
 const html=app.runUsageView({usage:[old]});
 assert.match(html,/\(partial\)/);
 assert.match(html,/older records may omit internal requests/);
 assert.match(html,/Equivalent API cost, not a subscription charge/);
 assert.match(html,/Request details/);
});

test('identically named Luna and Sol instances keep $10 and $20 costs with $30 grand total', () => {
 const runs=[{id:'luna-run',model:'gpt-6-luna',plan:{title:'Same scenario'}},{id:'sol-run',model:'gpt-6-sol',plan:{title:'Same scenario'}}];
 const tokenUsage=input=>({input_tokens:input,cached_input_tokens:0,cache_write_input_tokens:0,output_tokens:0,reasoning_output_tokens:0});
 const rows=[subscriptionRow({run_id:'luna-run',usage:tokenUsage(100000000)}),subscriptionRow({run_id:'sol-run',model:'gpt-6-sol',usage:tokenUsage(10000000)}),subscriptionRow({purpose:'planning',usage:tokenUsage(990000000)})];
 const app=usageDashboard(rows,runs),html=app.usageView();
 assert.match(html,/luna-run[^]*?\$10\.00/);
 assert.match(html,/sol-run[^]*?\$20\.00/);
 assert.match(html,/<tfoot>[^]*?All instances[^]*?\$30\.00/);
 assert.match(html,/Unassigned usage · 1 requests \(excluded from instance total\)/);
 assert.equal((app.instanceUsageView(rows,runs).match(/Same scenario/g)||[]).length,2);
 assert.doesNotMatch(app.instanceUsageView(rows,runs),/\$129\.00/);
});

test('completed planning usage is not partial while unknown scopes remain partial',()=>{
 const app=usageDashboard();
 const completed=subscriptionRow({purpose:'planning',usage_scope:'planning_total'});
 assert.equal(app.usageSummary([completed]).partial,false);
 assert.equal(app.usageSummary([subscriptionRow({usage_scope:'unknown'})]).partial,true);
});

test('live run displays dominant computer and inspector tokens using its frozen model', () => {
 const app=usageDashboard();
 Object.assign(app,{heading:(_label,title,copy)=>`${title} ${copy}`,feed:()=>'<video>camera</video>',progressView:()=>'',timelineView:()=>'',pauseNotice:()=>'',badge:()=>'',button:()=>'',active:()=>true});
 app.state.run={id:'run',model:'gpt-6-sol',plan:{title:'Live',model:'gpt-6-luna',players:[{id:'bot'}]},usage:[subscriptionRow({model:'gpt-6-sol'})]};
 app.state.parts=['runs','run'];
 Object.assign(app,{date:()=>'',time:()=>'',Date});
 const start=source.indexOf('function instanceLiveView(');
 vm.runInContext(source.slice(start,source.indexOf('function countdown(',start)),app);
 const html=app.runView();
 assert.ok(html.indexOf('<video>')<html.indexOf('API price estimate'));
 assert.match(html,/Bot computer[^]*?Agent activity[^]*?Instance inspector/);
 assert.match(html,/1,000/);
 assert.match(html,/200/);
 assert.match(html,/gpt-6-sol · Started/);
 assert.doesNotMatch(html,/<select[^>]*(name="model"|id="model")/);
 assert.match(html,/<select id="inspected-player"/);
});

test('Usage polling fetches /api/usage, renders updated totals and cannot rerender a newer route', async () => {
 let renders=0,usageResponse=deferred();const requests=[];
 const app=vm.createContext({state:{data:{},route:'usage',parts:['usage'],renderEpoch:1,polling:false},arr:value=>Array.isArray(value)?value:[],
  document:{hidden:false},$:()=>({open:false}),refresh:async()=>{},render:()=>renders++,updateStatusUI(){},
  api:url=>{requests.push(url);return usageResponse.promise;}});
 vm.runInContext(source.slice(source.indexOf('async function loadUsage()'),source.indexOf('async function startTakeover(')),app);
 vm.runInContext(source.slice(source.indexOf('async function poll()'),source.indexOf('setInterval(poll,')),app);
 const pending=app.poll();await new Promise(setImmediate);usageResponse.resolve({usage:[{id:'row'}]});await pending;
 assert.deepEqual(requests,['/api/usage']);assert.equal(app.state.usage[0].id,'row');assert.equal(renders,1);
 usageResponse=deferred();
 const late=app.poll();await new Promise(setImmediate);app.state.renderEpoch++;app.state.route='projects';usageResponse.resolve({usage:[{id:'late'}]});await late;
 assert.equal(renders,1);assert.equal(app.state.usage[0].id,'row');
});

test('subscription usage remains separate from historical metered cost', () => {
 const app=vm.createContext({arr:value=>value,money:value=>`$${value.toFixed(2)}`});
 vm.runInContext(source.slice(source.indexOf('function subscriptionUsage('),source.indexOf('function section(')),app);
 const subscription={api:'codex',billing:'subscription',estimated_cost_usd:null};
 assert.equal(app.totalCost([subscription]),'Subscription');
 assert.equal(app.totalCost([subscription,{api:'decisions',estimated_cost_usd:1.25}]),'Subscription + $1.25 API');
 assert.equal(app.totalCost([{api:'decisions',estimated_cost_usd:1.25}]),'$1.25');
 assert.equal(app.totalCost([subscription,{api:'decisions',estimated_cost_usd:null}]),'Subscription + Unknown API cost');
});

function codexSettings(api) {
 const app=vm.createContext({
  URL,api,
  state:{authEpoch:0,renderEpoch:0,polling:false,codexLogin:null,data:{ai:{configured:false}},route:'settings',parts:['settings','ai']},
  section:(_name,_copy,body)=>body,icon:()=>'',badge:()=>'',title:String,
  button:(label,action)=>`<button data-action="${action}">${label}</button>`,
  render(){},toast(){},updateStatusUI(){},document:{hidden:false},$:()=>({open:false}),
 });
 vm.runInContext(source.split('\n').find(line=>line.startsWith('const esc =')),app);
 vm.runInContext(source.slice(source.indexOf('function chatgptConnected('),source.indexOf('function settingsView(')),app);
 vm.runInContext(source.slice(source.indexOf('async function poll()'),source.indexOf('setInterval(poll,')),app);
 return app;
}

test('ChatGPT login offers only an official HTTPS link and clears after sign-in', async () => {
 const calls=[];
 const app=codexSettings(async path=>{
  calls.push(path);
  return path.endsWith('/login')?{auth_url:'https://auth.openai.com/oauth/authorize?state=test',login_id:'login-1'}:{configured:true,auth_method:'chatgpt',api:'codex'};
 });
 assert.equal(app.chatgptConnected({configured:true,auth_method:'apikey'}),false);
 for(const url of ['javascript:alert(1)','http://auth.openai.com/login','https://auth.openai.com.evil.test/login','https://evil.test@auth.openai.com/login'])assert.throws(()=>app.codexAuthUrl(url));
 await app.startCodexLogin();
 const html=app.codexSettingsView();
 assert.match(html,/https:\/\/auth\.openai\.com\/oauth\/authorize/);
 assert.match(html,/target="_blank" rel="noopener noreferrer"/);
 assert.match(html,/Complete sign-in in your browser/);
 assert.match(html,/Cancel sign-in/);
 assert.doesNotMatch(html,/<(?:input|textarea)\b|API key/);
 await app.refreshCodexStatus();
 assert.equal(app.state.codexLogin,null);
 assert.equal(app.chatgptConnected(app.state.data.ai),true);
 assert.deepEqual(calls,['/api/settings/codex/login','/api/settings/codex/status']);
});

test('invalid login destinations cancel the pending login and never appear as links', async () => {
 const calls=[];
 const app=codexSettings(async path=>{calls.push(path);return {auth_url:'https://untrusted.test/login',login_id:'bad'};});
 await assert.rejects(app.startCodexLogin(),/unrecognized login URL/);
 assert.equal(app.state.codexLogin,null);
 assert.equal(calls.at(-1),'/api/settings/codex/login/cancel');
});

test('late sign-in checks cannot undo logout and automatic checks stop after five minutes', async () => {
 const response=deferred();
 const app=codexSettings(()=>response.promise);
 const pending=app.refreshCodexStatus();
 app.state.authEpoch++;
 response.resolve({configured:true,auth_method:'chatgpt'});
 await pending;
 assert.equal(app.state.data.ai.configured,false);
 let polls=0;
 app.api=async()=>{polls++;return {configured:false};};
 app.state.codexLogin={auth_url:'https://auth.openai.com/login',pollUntil:Date.now()-1};
 await app.poll();
 assert.equal(polls,0);
 assert.match(app.codexSettingsView(),/Automatic checks have stopped/);
 app.state.codexLogin.pollUntil=Date.now()+300000;
 await app.poll();
 assert.equal(polls,1);
});

test('operating settings render a disabled fixed cap and explain retained legacy values',()=>{
 const app=vm.createContext({state:{parts:['settings','operation'],data:{settings:{concurrent_sessions:1,saved_concurrent_sessions:12}}},
 heading:()=>'',section:(name,copy,body)=>`${copy}${body}`,hardwareView:()=>'',icon:()=>'',esc:value=>String(value??'')});
 vm.runInContext(source.slice(source.indexOf('function settingsView('),source.indexOf('function hardwareView(')),app);
 const html=app.settingsView();assert.match(html,/<input id="concurrency"[^>]*value="1"[^>]*disabled/);
 assert.doesNotMatch(html,/name="concurrent_sessions"/);assert.match(html,/legacy value: 12 \(not enforced\)/i);
});

test('polling cannot erase an agent saved while its bootstrap request was pending', async () => {
 const response=deferred(),app=vm.createContext({api:()=>response.promise,state:{data:{agents:[]},dataRevision:0}});
 vm.runInContext(source.slice(source.indexOf('async function refresh()'),source.indexOf('function navigate(')),app);
 const pending=app.refresh();app.state.dataRevision++;app.state.data.agents=[{id:'new-profile'}];
 response.resolve({agents:[]});await pending;assert.equal(app.state.data.agents[0].id,'new-profile');
});

function goalDashboard() {
 const app=vm.createContext({icon:()=>''});
 const helpers=['esc','arr','title','known','dateObj','date','duration','button'];
 vm.runInContext(source.split('\n').filter(line=>helpers.some(name=>line.startsWith(`const ${name} =`))).join('\n'),app);
 vm.runInContext(source.slice(source.indexOf('const goalRecalculations ='),source.indexOf('function eventView(')),app);
 return app;
}

function plannedRun() {
 return {plan:{primary_goal:'Legacy objective'},subgoal_plan:{primary_goal:'Build a raft',root_id:'root',active_goal_id:'timber',status:'active',reason:'Gather materials first',goals:[
  {id:'root',parent_id:null,objective:'Build a raft',status:'active',completion:{kind:'observation',description:'A working raft is visible.'}},
  {id:'materials',parent_id:'root',objective:'Find materials',status:'active',completion:{description:'Wood and rope are in inventory.'}},
  {id:'timber',parent_id:'materials',objective:'Gather timber',status:'active',completion:{description:'Inventory contains 5 timber.'}},
  {id:'rope',parent_id:'materials',objective:'Gather rope',status:'blocked',completion:{description:'Inventory contains rope.'},blocked_reason:'The store is closed.'},
  {id:'guide',parent_id:'root',objective:'Read the guide',status:'completed',completion:{description:'The guide has been read.'}},
  {id:'launch',parent_id:'root',objective:'Launch the raft',status:'pending',completion:{description:'The raft is afloat.'}},
 ]}};
}

test('goal sidebar renders nested semantic checklists and follows only the selected active leaf', () => {
 const app=goalDashboard(),run=plannedRun(),html=app.progressView(run);
 assert.match(html,/<h2>Primary objective<\/h2>[^]*?<p class="objective-copy">Build a raft<\/p>/);
 assert.match(html,/<h3>Currently chasing<\/h3><p role="status">Gather timber/);
 const ancestry=html.match(/<ol aria-label="Goal ancestry">([^]*?)<\/ol>/)[1];
 assert.ok(ancestry.indexOf('Build a raft')<ancestry.indexOf('Find materials'));
 assert.ok(ancestry.indexOf('Find materials')<ancestry.indexOf('Gather timber'));
 const current=[...html.matchAll(/<li class="goal-node"[^>]*aria-current="step"/g)];
 assert.equal(current.length,1);assert.match(current[0][0],/data-key="goal-timber"/);
 let depth=0;const levels=[];
 for(const tag of html.match(/<\/?ul\b[^>]*>|<li class="goal-node"[^>]*>/g)){
  if(tag.startsWith('</ul'))depth--;
  else if(tag.startsWith('<ul'))depth++;
  else levels.push([tag.match(/data-key="goal-([^"]+)"/)[1],depth]);
 }
 assert.deepEqual(levels,[['root',1],['materials',2],['timber',3],['rope',3],['guide',2],['launch',2]]);
 for(const status of ['Pending','Active','Completed','Blocked'])assert.match(html,new RegExp(`class="goal-state">${status}<`));
 assert.match(html,/<strong>Complete when:<\/strong> Inventory contains 5 timber\./);
 assert.match(html,/Overall success requires verified game evidence, not just completed subgoals/);
 assert.doesNotMatch(html,/<(?:input|button|select|textarea)\b|role="checkbox"/);
 run.subgoal_plan.active_goal_id='root';
 assert.match(app.progressView(run),/<h3>Currently chasing<\/h3><p role="status">Build a raft/);
});

test('blocked goals explain their condition and evidence while escaping every planner field', () => {
 const app=goalDashboard(),run=plannedRun(),plan=run.subgoal_plan;
 const unsafe='<img src=x onerror="boom">&\'';
 const escaped='&lt;img src=x onerror=&quot;boom&quot;&gt;&amp;&#39;';
 plan.primary_goal=unsafe;plan.status='blocked';plan.reason=`Planner ${unsafe}`;plan.active_goal_id=null;
 const goal=plan.goals[3];goal.id=`rope" data-injected="${unsafe}`;
 Object.assign(goal,{objective:`Goal ${unsafe}`,blocked_reason:`Blocked ${unsafe}`,completion:{kind:'<manual_check>',description:`Condition ${unsafe}`},
  evidence:[{source:'observation',path:`state.${unsafe}`,quote:`Quote ${unsafe}`},{source:'controls',path:'guide',quote:'Press E'},{source:'objective',path:'primary_goal',quote:'Build a raft'},{source:unsafe,path:'extra',quote:'Extra evidence'},{source:'observation',quote:'Hidden fifth evidence'}],
  completion_evidence:{observation_id:'obs<7>',timestamp:'<now>',detail:unsafe},
 });
 const html=app.progressView(run);
 assert.match(html,/No active goal — planner is blocked/);
 assert.ok(html.includes(`<strong>Planner blocked:</strong> Planner ${escaped}`));
 assert.ok(html.includes(`<strong>Blocked reason:</strong> Blocked ${escaped}`));
 for(const label of ['Goal','Condition','Quote'])assert.ok(html.includes(`${label} ${escaped}`),label);
 assert.ok(html.includes(`<code>state.${escaped}</code>`));
 assert.match(html,/Completion check: &lt;Manual Check&gt;/);
 for(const source of ['Observation','Controls','Objective'])assert.ok(html.includes(`<strong>${source}</strong>`));
 assert.match(html,/&lt;Img Src=X Onerror=&quot;Boom&quot;&gt;&amp;&#39;/);
 assert.match(html,/Completion evidence/);assert.match(html,/obs&lt;7&gt;/);assert.match(html,/&lt;now&gt;/);
 assert.match(html,/data-key="goal-rope&quot; data-injected=&quot;/);
 assert.doesNotMatch(html,/<img\b| data-injected="|Hidden fifth evidence/);
 assert.match(html,/Additional evidence omitted/);
});

test('legacy runs have neutral missing-plan copy while pending plans await observations', () => {
 const app=goalDashboard();
 for(const subgoal_plan of [undefined,null,[],false,'legacy']){
  const html=app.progressView({subgoal_plan,plan:{primary_goal:'Reach the <shore>'},progress:{stage:'Observed <beach>'}});
  assert.match(html,/Reach the &lt;shore&gt;/);assert.match(html,/No generated subgoals yet/);
  assert.match(html,/Latest stage<\/dt><dd>Observed &lt;beach&gt;/);
  assert.doesNotMatch(html,/Awaiting observations|class="goal-node"|role="progressbar"|\d+%/);
 }
 for(const subgoal_plan of [{},{status:'pending',goals:[]}]){
  const html=app.progressView({subgoal_plan});
  assert.match(html,/Awaiting observations and planning/);assert.doesNotMatch(html,/class="goal-node"/);
 }
 const html=app.progressView({plan:{players:[{goal:'Legacy player goal'}]},progress:{completed_quests:['Observed quest']}});
 assert.match(html,/Legacy player goal/);assert.match(html,/Latest stage<\/dt><dd>Observed quest/);
 const run=plannedRun();run.subgoal_plan.active_goal_id=null;
 assert.match(app.progressView(run),/<h3>Currently chasing<\/h3><p role="status">Awaiting observations and planning/);
 assert.doesNotMatch(app.progressView(run),/aria-current="step"/);
 Object.assign(run.subgoal_plan,{status:'pending',active_goal_id:'root',reason:'Waiting for a fresh game observation.',goals:[{...run.subgoal_plan.goals[0],status:'pending'}]});
 const pending=app.progressView(run);
 assert.match(pending,/class="goal-state">Pending/);assert.match(pending,/Waiting for a fresh game observation/);
 assert.doesNotMatch(pending,/class="goal-state">Completed/);
});

test('corrupt cycles, missing and duplicate IDs do not fabricate an active goal or duplicate nodes', () => {
 const app=goalDashboard(),run={subgoal_plan:{root_id:'root',status:'active',active_goal_id:'missing',goals:[
  null,{}, {id:'root',parent_id:'cycle',objective:'Primary',status:'active'},
  {id:'cycle',parent_id:'root',objective:'Cycle',status:'<img>'},
  {id:'orphan',parent_id:'absent',objective:'Orphan',status:'pending'},
  {id:'root',parent_id:null,objective:'Duplicate root',status:'completed'},
 ]}};
 const before=JSON.stringify(run),html=app.progressView(run);
 assert.match(html,/Recorded active goal is unavailable; awaiting planning/);
 assert.match(html,/Unlinked goals/);assert.match(html,/missing, duplicated, or cyclic/);
 assert.equal((html.match(/class="goal-node"/g)||[]).length,3);
 assert.match(html,/data-status="unknown"[^]*?class="goal-state">Unknown/);
 assert.doesNotMatch(html,/Duplicate root|<img>|aria-current="step"/);
 assert.equal(JSON.stringify(run),before);
});

test('hierarchy supports all 64 goals and eight levels while bounding corrupt overflow', () => {
 const app=goalDashboard();
 for(const deep of [false,true])for(const overflow of [false,true]){
  const limit=deep?8:64,count=limit+(overflow?1:0);
  const goals=Array.from({length:count},(_,i)=>({id:i?`g${i}`:'root',parent_id:i?(deep&&i>1?`g${i-1}`:'root'):null,objective:`Goal ${i}`,status:'active'}));
  const run={subgoal_plan:{root_id:'root',active_goal_id:`g${count-1}`,status:'active',goals}};
  const html=app.progressView(run);
  assert.equal((html.match(/class="goal-node"/g)||[]).length,limit);
  assert.ok(html.includes(`<h3>Currently chasing</h3><p role="status">Goal ${count-1}`));
  if(overflow)assert.match(html,/Hierarchy display limited to 64 goals and 8 levels/);
  else assert.doesNotMatch(html,/Hierarchy display limited|partial ancestry/);
  if(deep&&overflow)assert.match(html,/partial ancestry/);
 }
});

test('retired descendants retain blocked status and never imply independent verification', () => {
 const app=goalDashboard(),run=plannedRun();
 Object.assign(run.subgoal_plan,{status:'completed',active_goal_id:null,goals:[
  {...run.subgoal_plan.goals[0],status:'completed',completion_evidence:{observation_id:'obs-8',timestamp:8}},
  {...run.subgoal_plan.goals[1],status:'blocked',retired:true,blocked_reason:'No longer needed: the parent goal was verified directly.'},
 ]});
 const html=app.progressView(run);
 assert.match(html,/No active goal — planner reports completion/);
 assert.match(html,/No longer needed · not verified/);
 assert.match(html,/<strong>Blocked reason:<\/strong> No longer needed: the parent goal was verified directly\./);
 assert.equal((html.match(/aria-hidden="true">☑/g)||[]).length,1);
 const retired=html.slice(html.indexOf('data-key="goal-materials"'));
 assert.match(retired,/class="goal-state">Blocked/);assert.doesNotMatch(retired,/class="goal-state">Completed/);
 assert.match(retired,/No completion evidence recorded yet/);
});

function recalculatableRun() {
 const run=plannedRun();
 return {...run,id:'run-one',state:'Investigating',model:'gpt-6-sol',agent_profile:{id:'agent-one'},
  plan:{...run.plan,players:[{id:'bot'}]},players:{bot:{launched:true}},
  subgoal_plan:{...run.subgoal_plan,player_id:'bot',status:'blocked',active_goal_id:null}};
}

function recalculationDashboard(api) {
 const app=goalDashboard(),run=recalculatableRun();let ids=0;
 const node=()=>({attributes:{},textContent:'',setAttribute(name,value){this.attributes[name]=value;}});
 const dialog={open:false,listeners:[],
  set innerHTML(html){this.html=html;app.form={...node(),dataset:{},submit:{disabled:false,textContent:'Recalculate goals'},cancel:{dataset:{action:'close-dialog'},textContent:'Cancel',closest:()=>app.form},status:node(),reportValidity:()=>true};},
  showModal(){this.open=true;},
  close(){this.open=false;for(const listener of this.listeners.splice(0))listener();},
  addEventListener(_event,listener){this.listeners.push(listener);},
 };
 Object.assign(app,{api,dialog,requests:[],renders:[],notifications:[],focusCount:0,
  state:{run,parts:['runs',run.id],route:'runs',renderEpoch:1,dataRevision:0,data:{runs:[run]}},
  crypto:{randomUUID:()=>`client-command-${++ids}`},FormData:class {constructor(){return [];}},
  errorMessage:error=>error.message,stopCameras(){},bindImages(){},
  $:(selector,root)=>selector==='#dialog'?dialog:selector==='#modal-form'?app.form:selector==='[data-action="recalculate-goals"]'?app.trigger:selector==='[data-action="close-dialog"]'?root.cancel:selector==='[data-recalculate-status]'?root.status:root.submit,
  render:background=>app.renders.push(background),toast:(message,error)=>app.notifications.push({message,error}),
 });
 app.trigger={isConnected:true,focus:()=>app.focusCount++};
 vm.runInContext(source.slice(source.indexOf('function modal('),source.indexOf('function dialogField(')),app);
 vm.runInContext(source.slice(source.indexOf('function recalculateGoalsDialog('),source.indexOf('const actions=')),app);
 vm.runInContext(source.slice(source.indexOf('async function loadRun('),source.indexOf('async function startTakeover(')),app);
 app.open=()=>app.recalculateGoalsDialog(app.trigger);
 app.submit=()=>app.form.onsubmit({preventDefault(){}});
 return app;
}

test('Recalculate goals appears once only for launched single-player planned runs without another owner', () => {
 const app=goalDashboard(),run=recalculatableRun();
 for(const state of ['Starting','Running','Paused','Investigating']){
  run.state=state;
  const html=app.progressView(run);
  assert.equal(app.canRecalculateGoals(run),true);
  assert.equal((html.match(/data-action="recalculate-goals"/g)||[]).length,1);
  assert.match(html,/Currently chasing[^]*?Recalculate goals/);
  assert.doesNotMatch(html,/<button[^>]*\bid=/);
 }
 const invalid=[...['Queued','Stopping','Ended','Completed','Failed','Terminated','unknown'].map(state=>({...run,state})),
  {...run,players:{bot:{launched:false}}},{...run,players:{}},{...run,plan:{players:[]}},
  {...run,plan:{players:[{id:'bot'},{id:'other'}]}},{...run,human_control:{player_id:'bot',status:'active'}},
  {...run,human_control:{player_id:'bot',status:'unknown'}},{...run,external_control:true},
  {...run,subgoal_plan:{...run.subgoal_plan,player_id:'other'}},
  ...[undefined,null,false,{},[], 'legacy'].map(subgoal_plan=>({...run,subgoal_plan}))];
 for(const unavailable of invalid){
  assert.equal(app.canRecalculateGoals(unavailable),false,JSON.stringify(unavailable));
  assert.doesNotMatch(app.progressView(unavailable),/data-action="recalculate-goals"/);
 }
 run.goal_recalculation={status:'planning'};
 assert.match(app.progressView(run),/data-action="recalculate-goals"[^>]*disabled aria-busy="true"/);
 assert.match(app.progressView(run),/Holding inputs and replanning from fresh game state/);
});

test('recalculation confirmation explains fresh replanning without completion or resume; cancel sends nothing and restores focus', () => {
 const requests=[],app=recalculationDashboard((...args)=>requests.push(args));
 app.open();
 assert.equal(app.dialog.open,true);
 assert.match(app.dialog.html,/fresh game-state JSON/);
 assert.match(app.dialog.html,/unchanged primary objective, model, and agent profile/);
 assert.match(app.dialog.html,/Retire the old unfinished checklist, not mark it completed/);
 assert.match(app.dialog.html,/previous plan and its evidence remain in the action ledger/);
 assert.match(app.dialog.html,/Inputs remain paused/);
 assert.match(app.dialog.html,/Resume inputs separately/);
 assert.match(app.dialog.html,/data-action="close-dialog"[^>]*>Cancel/);
 assert.doesNotMatch(app.dialog.html,/<(?:input|textarea|select)\b/);
 app.dialog.close();
 assert.equal(app.focusCount,1);assert.equal(requests.length,0);
 for(const change of [{human_control:{}},{external_control:true},{goal_recalculation:{status:'planning'}},{state:'Ended'}]){
  app.state.run={...recalculatableRun(),...change};app.open();assert.equal(app.dialog.open,false);
 }
});

test('recalculation holds duplicate submits, shows loading, applies returned paused run and never resumes or launches', async () => {
 const response=deferred(),requests=[],app=recalculationDashboard((...args)=>{requests.push(args);return response.promise;});
 const original=app.state.run,before=JSON.stringify(original);
 app.open();const form=app.form,pending=app.submit();await app.submit();app.open();
 assert.equal(app.form,form);assert.equal(requests.length,1);
 assert.equal(form.submit.disabled,true);assert.equal(form.attributes['aria-busy'],'true');
 assert.equal(form.submit.textContent,'Recalculating goals…');
 assert.match(form.status.textContent,/Holding inputs and replanning/);
 assert.match(app.progressView(app.state.run),/disabled aria-busy="true"/);
 assert.equal(JSON.stringify(original),before);
 const updated={...original,state:'Paused',subgoal_plan:{...original.subgoal_plan,reason:'Replanned from fresh JSON'}};
 response.resolve(updated);await pending;
 assert.equal(app.state.run,updated);assert.equal(app.state.data.runs[0],updated);
 assert.equal(app.dialog.open,false);assert.equal(app.focusCount,1);
 assert.equal(form.submit.disabled,false);assert.equal(form.attributes['aria-busy'],'false');
 assert.ok(app.renders.length>=2);assert.ok(app.renders.every(background=>background===true));
 assert.match(app.progressView(updated),/Goals recalculated\. Inputs remain paused/);
 assert.doesNotMatch(app.progressView({...updated,state:'Running'}),/Inputs remain paused/);
 assert.deepEqual(JSON.parse(JSON.stringify(requests)),[['/api/runs/run-one/recalculate-goals','POST',{command_id:'client-command-1'}]]);
 app.open();await app.submit();
 assert.equal(requests[1][2].command_id,'client-command-2','a new confirmed operation gets a new id after success');
 assert.ok(requests.every(([url])=>url.endsWith('/recalculate-goals')),'no resume, launch or extra request');
});

test('failed recalculation shows escaped errors and retries the same client id even after closing its dialog', async () => {
 const requests=[],app=recalculationDashboard(async(...args)=>{
  requests.push(args);if(requests.length<3)throw new Error('Planner unavailable <quota>');
  return {...app.state.run,state:'Paused'};
 });
 app.open();const original=app.state.run;
 await app.submit();
 assert.equal(app.state.run,original);assert.equal(app.dialog.open,true);
 assert.equal(app.form.submit.disabled,false);assert.equal(app.form.attributes['aria-busy'],'false');
 assert.equal(app.form.status.attributes.role,'alert');assert.match(app.form.status.textContent,/Planner unavailable <quota>/);
 assert.match(app.progressView(original),/role="alert">Planner unavailable &lt;quota&gt;/);
 assert.match(app.notifications[0].message,/Planner unavailable/);
 await app.submit();app.dialog.close();app.open();
 assert.match(app.dialog.html,/Planner unavailable &lt;quota&gt;/);
 assert.equal(app.form.dataset.requestId,'client-command-2');
 await app.submit();
 assert.deepEqual(requests.map(([,method,body])=>[method,body.command_id]),Array(3).fill(['POST','client-command-1']));
 assert.equal(app.dialog.open,false);assert.equal(app.state.run.state,'Paused');
 assert.doesNotMatch(app.progressView(app.state.run),/Planner unavailable/);
});

test('late recalculation success or failure cannot replace another instance, close its modal or steal its focus', async () => {
 for(const failed of [false,true]){
  const response=deferred(),app=recalculationDashboard(async()=>{await response.promise;if(failed)throw new Error('Old request failed');return {...oldRun,state:'Paused'};});
  const oldRun=app.state.run;app.open();const pending=app.submit(),renderCount=app.renders.length;
  app.state.renderEpoch++;app.state.parts=['runs','other'];app.state.run={...recalculatableRun(),id:'other'};
  app.dialog.close();app.open();const newForm=app.form,newRun=app.state.run;
  response.resolve();await pending;
  assert.equal(app.state.run,newRun);assert.equal(app.form,newForm);assert.equal(app.dialog.open,true);
  assert.equal(app.focusCount,0);assert.equal(app.renders.length,renderCount);assert.equal(app.notifications.length,0);
 }
});

test('stale recalculation confirmation sends nothing and a late result cannot overwrite a revisited instance', async () => {
 const response=deferred(),requests=[],app=recalculationDashboard((...args)=>{requests.push(args);return response.promise;});
 app.open();app.state.renderEpoch++;await app.submit();assert.equal(requests.length,0);
 app.open();const pending=app.submit();
 app.state.renderEpoch++;app.dialog.close();
 app.state.renderEpoch++;const latest={...app.state.run,state:'Paused',marker:'revisited'};app.state.run=latest;
 response.resolve({...app.state.run,marker:'old response'});await pending;
 assert.equal(app.state.run,latest);assert.equal(app.focusCount,0);
});

test('a GET started before recalculation cannot erase its returned paused plan', async () => {
 const response=deferred(),app=recalculationDashboard(async(_url,method)=>method==='POST'?{...app.state.run,state:'Paused'}:response.promise);
 const original=app.state.run,loading=app.loadRun();
 app.open();await app.submit();assert.equal(app.state.run.state,'Paused');
 response.resolve(original);await loading;assert.equal(app.state.run.state,'Paused');
});

test('recalculation finishing during navigation does not discard the newly selected run GET', async () => {
 const recalculated=deferred(),loaded=deferred(),app=recalculationDashboard((_url,method)=>method==='POST'?recalculated.promise:loaded.promise);
 const original=app.state.run;app.open();const pending=app.submit();
 app.state.renderEpoch++;app.state.parts=['runs','other'];app.dialog.close();
 const loading=app.loadRun(),other={...recalculatableRun(),id:'other'};
 recalculated.resolve({...original,state:'Paused'});await pending;
 loaded.resolve(other);await loading;assert.equal(app.state.run,other);
});

test('recalculation rechecks ownership before submitting an already open confirmation', async () => {
 const requests=[],app=recalculationDashboard((...args)=>requests.push(args));
 app.open();app.state.run.human_control={player_id:'bot'};
 await app.submit();
 assert.equal(requests.length,0);assert.equal(app.dialog.open,true);
 assert.match(app.notifications[0].message,/no longer available/);
});

test('Cancel recalculation pauses once with a new command id and ignores late planning responses and navigation', async () => {
 for(const mode of ['local-success','local-error','external','navigation','cancel-retry','already-finished']){
  const planning=deferred(),cancellation=deferred(),requests=[];
  const app=recalculationDashboard(async(...args)=>{
   requests.push(args);
   if(args[0].endsWith('/control')){
    if(mode==='cancel-retry'&&requests.filter(([url])=>url.endsWith('/control')).length===1)throw new Error('Connection lost');
    return cancellation.promise;
   }
   const result=await planning.promise;
   if(mode==='local-error')throw new Error('Planning cancelled');
   return result;
  });
  const original=app.state.run;let pending;
  if(mode==='external')original.goal_recalculation={status:'planning'};
  else {
   app.open();pending=app.submit();
   assert.equal(app.form.cancel.textContent,'Cancel recalculation');
   assert.equal(app.form.cancel.dataset.action,'cancel-recalculation');
  }
  assert.match(app.progressView(original),/data-action="recalculate-goals"[^>]*disabled/);
  assert.match(app.progressView(original),/data-action="cancel-recalculation"[^>]*>Cancel recalculation/);
  if(mode==='cancel-retry'){
   await app.cancelGoalRecalculation(app.form.cancel);
   assert.match(app.progressView(original),/Could not cancel recalculation: Connection lost/);
  }
  const cancelling=app.cancelGoalRecalculation(mode==='external'?undefined:app.form.cancel);
  await app.cancelGoalRecalculation();
  assert.equal(app.dialog.open,false);
  const controls=requests.filter(([url])=>url.endsWith('/control'));
  assert.equal(controls.length,mode==='cancel-retry'?2:1);
  assert.deepEqual(JSON.parse(JSON.stringify(controls[0])),['/api/runs/run-one/control','POST',{command:'pause',command_id:mode==='external'?'client-command-1':'client-command-2',reason:'Cancel goal recalculation'}]);
  assert.ok(controls.every(([, ,body])=>body.command_id===controls[0][2].command_id));
  assert.match(app.progressView(original),/data-action="cancel-recalculation"[^>]*disabled aria-busy="true"/);
  const committedPlan={...original.subgoal_plan,reason:'Committed before cancellation'};
  const cancelled={...original,state:'Paused',goal_recalculation:{status:mode==='already-finished'?'completed':'cancelled'},subgoal_plan:mode==='already-finished'?committedPlan:original.subgoal_plan},other={...recalculatableRun(),id:'other'};
  if(mode==='navigation'){app.state.renderEpoch++;app.state.parts=['runs','other'];app.state.run=other;}
  cancellation.resolve(cancelled);await cancelling;
  if(pending){planning.resolve({...original,subgoal_plan:{...original.subgoal_plan,reason:'Stale late plan'}});await pending;}
  assert.equal(app.state.run,mode==='navigation'?other:cancelled);
  assert.equal(app.state.data.runs.find(run=>run.id===original.id),cancelled);
  assert.equal(cancelled.subgoal_plan,mode==='already-finished'?committedPlan:original.subgoal_plan);
  assert.doesNotMatch(app.progressView(cancelled),/data-action="cancel-recalculation"|Stale late plan|Connection lost/);
  if(mode==='already-finished'){
   assert.match(app.progressView(cancelled),/Recalculation already finished; inputs remain paused/);
   assert.match(app.progressView(cancelled),/Committed before cancellation/);
   assert.doesNotMatch(app.progressView(cancelled),/Recalculation cancelled/);
  } else assert.match(app.progressView(cancelled),/Recalculation cancelled\. Inputs remain paused/);
  assert.equal(app.notifications.length,0);
  assert.ok(requests.every(([url,,body])=>url.endsWith('/recalculate-goals')||body.command==='pause'),'never resumes or launches');
 }
});
