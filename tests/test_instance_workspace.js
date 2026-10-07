'use strict';
// Isolated recorded-shape UI checks. node tests/test_instance_workspace.js; no SDK/API/game/worker.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const root = path.resolve(__dirname,'..');
const source = fs.readFileSync(path.join(root,'ashad/static/instance-workspace.js'),'utf8');
let helperCalls = 0;
const esc = value => String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const never = () => { throw new Error('Render must never perform operations'); };
const context = vm.createContext({console, fetch:never, api:never, SDK:never, document: new Proxy({}, {get:never}),
 ashadInstanceContext:{esc:value=>{helperCalls++;return esc(value);},arr:value=>Array.isArray(value)?value:[],
  date:value=>`date:${value}`,time:value=>`time:${value}`,nowSeconds:1000,
  state:new Proxy({}, {get:never}),ago:never,title:never,number:never,notice:never}});
vm.runInContext(source,context,{filename:'instance-workspace.js'});
const {activityCardsView,observedWorldView,recentActivityView,activityScrollSnapshot,restoreActivityScroll}=context;
const observation={schema_version:1,run_id:'run',player_id:'p',observation_id:'g:obs:42:epoch',generation:'g',revision:42,timestamp:999,
 world_id:'world',ready:true,complete:true,scope:'own_inventory_visible_ui_and_visible_world',
 state:{world_id:'world',ready:true,complete:true,player:{position:[20,30],inventory:[{slot:0,item_id:'wood',count:2},{slot:1,empty:true}],hotbar:[],stats:{energy:80},closest_target:'g:target'},
  objectives:[{id:'wheat',name:'Harvest wheat',description:'Gather',requirements:[{description:'Harvest 10 wheat',progress:'2/10'}]}],completed_quests:[],tiles:[]},
 entities:[{id:'g:target',version:4,kind:'crafter',label:'Workbench',position:[30,40],state:{is_crafting:false},actions:['move_to','interact']},
  {id:'g:ui',version:3,kind:'ui',label:'Confirm',parent:'World modal',rect:[0,0,100,30],state:{focused:true,control_type:'Button'},actions:['ui_click']}],
 active_action:{},inputs:['interact','inventory','ui_accept']};
const run={id:'run',agent_id:'agent',agent_profile:{name:'Configured agent'},plan:{players:[{id:'p',name:'Player One'},{id:'other',name:'Other player'}]},
 players:{p:{structured_context:observation,telemetry:{secret_monitor_fact:'MONITOR_ONLY',sequence:10},worker_status:{game_running:true}}}};
const event=(kind,payload={},seq=1,id=`evt_${seq}`,runID='run')=>({id,version:1,run_id:runID,seq,timestamp:990+seq,kind,payload});
const fixtures=[
 event('structured_decision_context',{player_id:'p',context:{player:{id:'p'},observation},full_observation:observation,source_revision:'source-1'},1),
 event('subgoal_planning_context',{player_id:'p',revision:1,context:{player:{id:'p'},observation}},2),
 event('provider_request_started',{id:'request_1',purpose:'player',status:'requesting',run_id:'run',question_names:['target']},3),
 event('provider_request_finished',{id:'request_1',purpose:'player',status:'completed',run_id:'run'},4),
 event('decision_selected',{player_id:'p',observation_id:observation.observation_id,target:'Workbench',action:{tool:'perform_action',args:{verb:'interact',target_id:'g:target',target_version:4,parameters:{}},description:'Interact with highlighted Workbench'}},5),
 event('structured_action_intent',{player_id:'p',action_id:'game_1',source_observation:observation.observation_id,reason:'Interact',input:{action_id:'game_1',observation_id:observation.observation_id,verb:'interact',target_id:'g:target',target_version:4,parameters:{}}},6),
 event('action_result',{player_id:'p',action_id:'game_1',intent_event_id:'evt_6',result:{action_id:'game_1',status:'accepted'},mode:'structured',retries:0},7),
 event('structured_action_outcome',{player_id:'p',result:{action_id:'game_1',status:'completed',reason:'Normal input delivered; verify gameplay effect in the next observation'}},8),
 event('structured_tool_result',{player_id:'p',tool:'wait_for_change',result:{status:'waiting',until:1010}},9),
 event('agent_turn_failed',{player_id:'p',category:'provider_failure',provider_status:'provider_failure',error:'Connection failed'},10),
 event('run_paused',{category:'manual_pause',reason:'You paused the bots.',players:['p'],world_may_continue:true},11),
 event('monitor_observation',{player_id:'p',source:'read_only_diagnostics',telemetry:{secret_monitor_fact:'MONITOR_ONLY'}},12),
 event('new_future_event',{reason:'I am thinking about acting successfully.'},13),
 event('subgoals_verified',{player_id:'p',goal_ids:['leaf-1'],evidence:{observation_id:observation.observation_id,timestamp:999,world_id:'world',generation:'g'},full_observation:observation},14),
 event('agent_turn_completed',{player_id:'p',duration_seconds:1,result:{status:'dispatched',result:{status:'accepted'},observation_id:observation.observation_id}},15)
];
const original = JSON.stringify({run,fixtures});
const html = activityCardsView(fixtures,run);
assert.equal(JSON.stringify({run,fixtures}),original,'views must not mutate retained records');
assert.equal(activityCardsView(fixtures,run),html,'same records and render clock produce identical HTML');
for (const category of ['observing','planning','selecting','acting','checking','waiting','blocked','human','unknown']) assert.ok(html.includes(`data-category="${category}"`),category);
assert.ok(html.includes('Input/action completion is not proof of gameplay success.'));
assert.ok(html.includes('Dispatch recorded; returned action status: accepted.'));
assert.ok(!html.includes('Configured agent'),'missing event agent/player identity is never filled from run profile');
const cardFor=(output,seq)=>output.match(new RegExp(`<li class="activity-card"[^>]*data-seq="${seq}"[\\s\\S]*?</li>`))?.[0];
assert.ok(cardFor(html,3).includes('Instance activity'),'provider boundary has no player attribution');
assert.ok(cardFor(html,5).includes('data-status="unknown"'),'candidate selection is not dispatched/requested');
assert.ok(cardFor(html,6).includes('data-status="requested"'));
assert.ok(cardFor(html,12).includes('data-category="unknown"'));
assert.ok(cardFor(html,12).includes('Monitor-only diagnostics'));
assert.ok(cardFor(html,13).includes('data-category="unknown"'),'prose must not classify unknown records');
assert.ok(cardFor(html,8).includes('game_1'),'outcome correlation uses result.action_id');
assert.ok(cardFor(html,15).includes('data-category="acting"'),'turn completion with dispatch is not fabricated outcome verification');
assert.ok(activityCardsView([event('agent_turn_completed',{player_id:'p',result:null},1)],run).includes('data-category="unknown"'));
assert.ok(activityCardsView([event('provider_request_started',{id:'r'},1)],run).includes('data-category="unknown"'),'missing request purpose never invents selection phase');
assert.ok(cardFor(html,7).includes('Related records:'));
assert.ok(cardFor(html,3).includes('Sequence 4'),'provider requests correlate through diagnostic.id');
assert.ok(cardFor(html,5).includes('Sequence 1'),'observations correlate by exact retained ID');
const cross=[fixtures[5],event('action_result',{player_id:'other',action_id:'game_1',result:{status:'completed'}},16),
 event('action_result',{player_id:'p',action_id:'game_1',result:{status:'completed'}},17,'evt_foreign','foreign')];
assert.ok(!cardFor(activityCardsView(cross,run),6).includes('Related records:'),'never correlate across run/player identity');
for(const status of ['requested','dispatched','accepted','running','completed','rejected','cancelled','unknown','failed','expired']) {
 assert.ok(activityCardsView([event('action_result',{player_id:'p',result:{status}},1)],run).includes(`data-status="${status}"`));
}
assert.ok(activityCardsView([event('provider_request_finished',{id:'r',status:'request_cancelled'},1)],run).includes('data-status="cancelled"'));
assert.ok(activityCardsView([event('provider_request_finished',{id:'r',status:'provider_failure'},1)],run).includes('data-category="blocked"'));
assert.ok(activityCardsView([{id:'old',seq:1,type:'acting',payload:{reason:'thinking'}}],run).includes('data-category="unknown"'));
assert.ok(activityCardsView([],run).includes('Activity state is unknown'));
for(const unknownType of ['__proto__','constructor','toString',['decision_selected']]) assert.ok(activityCardsView([event(unknownType,{},1)],run).includes('data-category="unknown"'),'only literal known event kinds classify');

const long=Array.from({length:150},(_,i)=>event('run_paused',{reason:`pause_${i+1}`},i+1));
const boundedHTML=activityCardsView({events:long,total:500,older_count:350},run);
assert.equal((boundedHTML.match(/class="activity-card"/g)||[]).length,100);
const unreadOld=long.map((item,index)=>index<50?{...item,get payload(){throw new Error('Older payload must not be rendered');}}:item);
assert.equal((activityCardsView(unreadOld,run).match(/class="activity-card"/g)||[]).length,100,'bounded views never inspect older payloads');
assert.ok(boundedHTML.includes('data-first-seq="51"'));
assert.ok(boundedHTML.includes('data-last-seq="150"'));
assert.ok(boundedHTML.includes('Older records: 400'));
assert.ok(activityCardsView({events:long,total:500},run).includes('Older records: 400'));
assert.ok(activityCardsView([],run).includes('Older records: unknown'));
const anchorOf=(output,seq)=>cardFor(output,seq).match(/id="([^"]+)"/)[1];
const stable=anchorOf(activityCardsView([long[99]],run),100);
assert.equal(stable,anchorOf(boundedHTML,100));
assert.equal(stable,anchorOf(activityCardsView(long.slice(20).reverse(),run),100));
const recent=recentActivityView({events:long,total:500},run);
assert.equal((recent.match(/data-activity-anchor=/g)||[]).length,8);
assert.ok(recent.includes(anchorOf(boundedHTML,150)),'recent evidence uses same card anchor');
assert.ok(!recentActivityView(fixtures.slice(0,9),run).includes('decision_selected'),'recent operational list is not a competing action feed');
assert.ok(recentActivityView([],run).includes('No retained operational events'));

const world=observedWorldView(run,'p');
for(const section of ['Latest observed state','Revision','date:999','1s at render time','Fresh at render time','Complete observed coverage','Player state / position','Inventory','Hotbar',
 'Visible / nearby entities','Objectives / observed progress','Visible UI / modal controls','Active action','Raw structured observation','Monitor-only diagnostics']) assert.ok(world.includes(section),section);
assert.ok(world.includes('coverage is not goal success or whole-game knowledge'));
assert.ok(world.includes('Observed empty array (0 entries)'));
assert.ok(world.indexOf('MONITOR_ONLY')>world.indexOf('Monitor-only diagnostics'),'monitor cannot leak into model snapshot groups');
assert.ok(observedWorldView({...run,players:{}},'p').includes('no retained structured observation'));
assert.ok(observedWorldView(run,'missing').includes('no retained structured observation'));
const changeObservation=changes=>({...run,players:{p:{structured_context:{...observation,...changes}}}});
assert.ok(observedWorldView(changeObservation({timestamp:990}),'p').includes('data-stale="true"'));
assert.ok(observedWorldView(changeObservation({timestamp:1003}),'p').includes('data-stale="true"'));
assert.ok(observedWorldView(changeObservation({timestamp:undefined,complete:undefined,ready:undefined}),'p').includes('Freshness unknown'));
assert.ok(observedWorldView(changeObservation({complete:false}),'p').includes('Partial / truncated'));
assert.ok(observedWorldView(changeObservation({context_truncated:true}),'p').includes('Partial / truncated'));
assert.ok(observedWorldView(changeObservation({state:{...observation.state,complete:false}}),'p').includes('Partial / truncated'));
assert.ok(observedWorldView(changeObservation({player_id:'other'}),'p').includes('identity differs'));
assert.ok(observedWorldView(changeObservation({ready:false}),'p').includes('Observed not ready'));
assert.ok(observedWorldView(changeObservation({state:{}}),'p').includes('Unknown · not retained'));
const decisionWorld=observedWorldView(run,'p',fixtures[4]);
assert.ok(decisionWorld.includes('Decision-time context is not retained in this event'));
assert.ok(!decisionWorld.includes('State retained for selected decision'),'matching latest observation ID does not manufacture retained decision input');
assert.ok(observedWorldView(run,'p',fixtures[0]).includes('State retained for selected decision'));
assert.ok(observedWorldView(run,'p',fixtures[1]).includes('State retained for selected decision'));
assert.ok(observedWorldView(run,'p',event('decision_selected',{player_id:'other',observation_id:observation.observation_id})).includes('different run/player'));
assert.ok(observedWorldView(run,'p',event('decision_selected',{reason:`Selected on ${observation.observation_id}`})).includes('neither decision context nor an observation reference'));
assert.ok(!observedWorldView(run,'p',event('monitor_observation',{context:{observation}})).includes('State retained for selected decision'));
const oldClock=context.ashadInstanceContext.nowSeconds;
delete context.ashadInstanceContext.nowSeconds;
assert.ok(observedWorldView(run,'p').includes('Freshness unknown'));
context.ashadInstanceContext.nowSeconds=oldClock;

const evil='<img src=x onerror="alert(1)"><script>alert(2)</script>&\'"';
const hostile=event('decision_selected',{player_id:evil,observation_id:evil,target:evil,action:{description:evil}},1,evil,evil);
const evilRun={id:evil,plan:{players:[{id:evil,name:evil}]},players:{[evil]:{structured_context:{...observation,run_id:evil,player_id:evil,observation_id:evil,state:{player:{inventory:[{item_id:evil}],hotbar:[]},objectives:[{name:evil}]},entities:[{kind:'ui',label:evil,state:{text:evil}}]}}}};
for(const output of [activityCardsView([hostile],evilRun),activityCardsView([event(evil,{reason:evil},1,evil)],run),observedWorldView(evilRun,evil),recentActivityView([event('goal_changed',{player_id:evil,goal:evil},1,evil,evil)],evilRun)]) {
 assert.ok(!output.includes('<img'));
 assert.ok(!output.includes('<script'));
 assert.ok(output.includes('&lt;'));
}
assert.ok(/^activity-[a-z0-9-]+$/.test(anchorOf(activityCardsView([hostile],evilRun),1)));
const enormous=Array.from({length:600},(_,i)=>({id:`item-${i}`,item_id:'a'.repeat(6000)}));
const hugeWorld=observedWorldView(changeObservation({entities:enormous,state:{player:{inventory:enormous,hotbar:[]},objectives:enormous}}),'p');
assert.ok(hugeWorld.includes('580 entries omitted'));
assert.ok(hugeWorld.includes('omitted 580 array entries'));
assert.ok(hugeWorld.includes('omitted 4000 characters'));
assert.ok(hugeWorld.length<400000,'bounded raw and group arrays prevent megabyte DOM growth');
const hugeID='e'.repeat(100000);
const hugeEvent=activityCardsView([event('new_kind',{reason:'x'.repeat(100000),rows:enormous},1,hugeID)],run);
assert.ok(hugeEvent.length<100000,'long IDs, prose, raw arrays have bounded display');
assert.ok(hugeEvent.includes('omitted'));
const giantKey=activityCardsView([event('new_kind',{['k'.repeat(100000)]:evil},1)],run);
assert.ok(giantKey.length<20000,'malicious raw field names are bounded');
assert.ok(giantKey.includes('omitted 99800 key characters'));
const legacy={kind:'old_unattributed',timestamp:1,payload:{reason:'Old record without IDs'}};
assert.equal(anchorOf(activityCardsView([legacy],run),''),anchorOf(activityCardsView([event('run_paused',{},2),legacy],run),''),'legacy anchors are not page indices');
assert.ok(helperCalls>0);

// Minimal scroller doubles verify live-edge following, no jump when reading, and retained-anchor correction.
function scroller({scrollTop=0,height=500,client=100,last=3,cardTop=0,cardBottom=50}={}) {
 const indicator={hidden:true}, card={id:'stable-card',getBoundingClientRect:()=>({top:cardTop-scroll.scrollTop,bottom:cardBottom-scroll.scrollTop})};
 const scroll={scrollTop,scrollHeight:height,clientHeight:client,dataset:{lastSeq:String(last)},
  getBoundingClientRect:()=>({top:0}),querySelectorAll:()=>[card],querySelector:selector=>selector==='.activity-new'?indicator:null};
 return {scroll,indicator,card};
}
const live=scroller({scrollTop:400});
const liveSnapshot=activityScrollSnapshot(live.scroll);
assert.equal(liveSnapshot.liveEdge,true);
live.scroll.scrollHeight=600;live.scroll.dataset.lastSeq='4';restoreActivityScroll(live.scroll,liveSnapshot);
assert.equal(live.scroll.scrollTop,500);assert.equal(live.indicator.hidden,true);
const reading=scroller({scrollTop:40,cardTop:40,cardBottom:80});
const readingSnapshot=activityScrollSnapshot(reading.scroll);
assert.equal(readingSnapshot.liveEdge,false);
reading.scroll.scrollHeight=600;reading.scroll.dataset.lastSeq='4';restoreActivityScroll(reading.scroll,readingSnapshot);
assert.equal(reading.scroll.scrollTop,40);assert.equal(reading.indicator.hidden,false);
const unreadSnapshot=activityScrollSnapshot(reading.scroll);
reading.indicator.hidden=true; // parent patch restores initial hidden attribute
restoreActivityScroll(reading.scroll,unreadSnapshot);
assert.equal(reading.indicator.hidden,false,'unread indicator persists through quiet polls');
reading.card.getBoundingClientRect=()=>({top:60-reading.scroll.scrollTop,bottom:100-reading.scroll.scrollTop});
restoreActivityScroll(reading.scroll,readingSnapshot);
assert.equal(reading.scroll.scrollTop,60,'load-older patch preserves the retained reading anchor');
assert.equal(activityScrollSnapshot(null),null);restoreActivityScroll(null,null);
console.log('PASS instance workspace: real event categories/status/correlation, bounded history/arrays, deterministic inspector, XSS, no operations, scroll preservation');
