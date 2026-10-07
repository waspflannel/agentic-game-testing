'use strict';
const test=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const source=fs.readFileSync('ashad/static/app.js','utf8');
const esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
function renderer(){
 const run={id:'r',plan:{players:[{id:'p',name:'Recorded player'}]},players:{p:{}},events:[],events_page:{total:0}};
 const app=vm.createContext({state:{run,parts:['runs','r'],renderEpoch:1},esc,arr:v=>Array.isArray(v)?v:[],date:()=>'',time:()=>'',Date,button:(label,action)=>`<button data-action="${action}">${label}</button>`,feed:()=>'<video>retained camera</video>',runUsageView:()=>'<p>Unknown tokens · API equivalent</p>',progressView:()=>'<p>Currently chasing · unknown</p>',errorMessage:e=>e.message,render:()=>{}});
 vm.runInContext(fs.readFileSync('ashad/static/instance-workspace.js','utf8'),app);
 const start=source.indexOf('function instanceLiveView('),end=source.indexOf('function runView(',start);
 vm.runInContext(source.slice(start,end),app);
 return app;
}
test('actual instance renderer composes computer activity inspector with unknown retained context',()=>{
 const app=renderer(),html=app.instanceLiveView(app.state.run,app.state.run.plan.players);
 assert.ok(html.indexOf('<video>')<html.indexOf('Unknown tokens'));
 assert.match(html,/Bot computer[^]*?Agent activity[^]*?Instance inspector/);
 assert.match(html,/No retained activity records/);assert.match(html,/Unknown/);assert.match(html,/Monitor-only diagnostics/);
 assert.equal(app.state.run.events.length,0);
});
test('actual integration renders bounded latest100 and selects only real retained decisions',()=>{
 const app=renderer();app.state.run.events=Array.from({length:200},(_,i)=>({id:`e${i}`,seq:i+1,kind:'decision_selected',payload:{player_id:'p',observation_id:`o${i}`}}));app.state.run.events_page.total=2000;app.state.inspectedDecision='e199';
 const html=app.instanceLiveView(app.state.run,app.state.run.plan.players);
 assert.equal((html.match(/class="activity-card"/g)||[]).length,100);
 assert.match(html,/Load older activity/);assert.match(html,/Recorded observation reference/);assert.match(html,/not a reconstruction/);assert.doesNotMatch(html,/value="e99"/);
});
test('older page uses first visible sequence and late route responses cannot replace another run',async()=>{
 const app=renderer();app.state.run.events=Array.from({length:200},(_,i)=>({id:`e${i}`,seq:i+1,kind:'old'}));let resolve,url;
 app.api=value=>{url=value;return new Promise(r=>resolve=r);};const task=app.olderActivity();assert.match(url,/before=101&limit=100/);
 app.state.renderEpoch++;app.state.run={id:'other'};resolve({events:[{seq:100}],has_more:true});await task;assert.equal(app.state.activityPage,undefined);
});
test('late history response cannot replace an explicit return to latest',async()=>{
 const app=renderer();app.state.run.events=[{id:'initial',seq:161}];let resolve;app.api=()=>new Promise(r=>resolve=r);const task=app.olderActivity();
 app.state.activityEpoch++;app.state.activityPage=null;resolve({events:[{id:'older',seq:61}]});await task;assert.equal(app.state.activityPage,null);
});
test('older page replaces rather than accumulates history and retains exact run identity',async()=>{
 const app=renderer();app.state.run.events=[{seq:101,id:'e101'}];app.api=async()=>({events:[{seq:100,id:'e100'}],has_more:false,total:101,before:100,next:100});await app.olderActivity();
 assert.equal(app.state.activityPage.runId,'r');assert.equal(app.state.activityPage.events.length,1);assert.equal(app.state.activityPage.events[0].seq,100);
});
test('sparse history counts exclude newer records and frozen page reports new live evidence',()=>{
 const app=renderer();app.state.run.events_page={total:270,recent_100_older_count:170};
 app.state.activityPage={runId:'r',events:[{id:'old',seq:1000,kind:'decision_selected',payload:{observation_id:'older'}}],total:260,older_count:60,newer_count:100,has_more:true};app.state.inspectedDecision='not-in-page';
 const html=app.instanceLiveView(app.state.run,app.state.run.plan.players);
 assert.match(html,/Older records: 60/);assert.match(html,/110 newer records/);assert.match(html,/Previously selected decision is outside this page/);assert.match(html,/data-scope="r:history:1000"/);
 app.state.activityPage=null;assert.match(app.instanceLiveView(app.state.run,app.state.run.plan.players),/Older records: 170/);
});
test('production click handler awaits real older-page action and re-enables native button',async()=>{
 const app=renderer();app.state.run.events=[{id:'initial',seq:161}];let click;
 app.document={addEventListener:(kind,handler)=>{if(kind==='click')click=handler;}};app.actions={'older-activity':app.olderActivity};app.toast=()=>assert.fail('Unexpected error toast');
 const start=source.indexOf("document.addEventListener('click'"),end=source.indexOf("document.addEventListener('input'",start);vm.runInContext(source.slice(start,end),app);
 const element={dataset:{action:'older-activity'},disabled:false};app.api=async url=>{assert.match(url,/before=161&limit=100/);return {events:[{id:'older',seq:61}],has_more:true,older_count:60,newer_count:100,total:260};};
 let prevented=false;await click({target:{closest:selector=>selector==='[data-action]'?element:null},preventDefault:()=>{prevented=true;}});
 assert.equal(prevented,true);assert.equal(element.disabled,false);assert.equal(app.state.activityPage.events[0].seq,61);
});
test('scroll ownership scopes distinguish runs and explicit pages, quiet polls preserve only same scope',()=>{
 assert.match(source,/scrollScope===currentScroll.dataset.scope/);assert.match(source,/scope.includes\(':history:'\)\?0:currentScroll.scrollHeight/);
 const app=renderer();assert.match(app.instanceLiveView(app.state.run,app.state.run.plan.players),/data-scope="r:latest"/);
 app.state.run.id='second';assert.match(app.instanceLiveView(app.state.run,app.state.run.plan.players),/data-scope="second:latest"/);
});
test('legacy full-ledger pagination keeps at most 200 records instead of accumulating history',async()=>{
 const app=renderer();app.state.search='';app.state.events=[];let page=0;
 app.api=async()=>{const first=page++*200;return {events:Array.from({length:200},(_,i)=>({seq:first+i+1})),next:first+200,total:2000,has_more:true};};
 const start=source.indexOf('async function loadEvents('),end=source.indexOf('async function loadRun(',start);vm.runInContext(source.slice(start,end),app);
 await app.loadEvents();for(let i=0;i<6;i++)await app.loadEvents(true);
 assert.equal(app.state.events.length,200);assert.equal(app.state.events[0].seq,1201);assert.equal(app.state.eventNext,1400);
});
test('production deferred module order and evidence click avoid route hash mutation',()=>{
 const html=fs.readFileSync('ashad/static/index.html','utf8');assert.ok(html.indexOf('/static/app.js')<html.indexOf('/static/instance-workspace.js'));assert.match(html,/instance-workspace.js" defer/);
 assert.match(source,/activityScrollSnapshot\?\.\(scrollNode\)/);assert.match(source,/restoreActivityScroll\?\.\(currentScroll,scrollSnapshot\)/);
 const handler=source.slice(source.indexOf("document.addEventListener('click'"),source.indexOf("document.addEventListener('change'"));assert.match(handler,/evidence\)\{event.preventDefault\(\)/);assert.match(handler,/getElementById\(evidence.dataset.activityAnchor\)/);assert.doesNotMatch(handler,/location.hash\s*=/);
});
