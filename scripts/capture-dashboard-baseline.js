'use strict';
// P0.2: fixture-only browser baseline. No controller, provider, worker or game.
// node scripts/capture-dashboard-baseline.js --check
// node scripts/capture-dashboard-baseline.js
// Open printed origin + /?fixture=<name>#/<route> with agent-browser; screenshot
// into .artifacts/electron-baseline. Stop this server when captures are finished.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const http = require('node:http');
const crypto = require('node:crypto');
const root = path.resolve(__dirname, '..');
const output = path.join(root, '.artifacts/electron-baseline');
const names = ['no-setup', 'saved-agent', 'active', 'paused', 'ended', 'missing-feed', 'provider-error'];
const timestamp = Math.floor(Date.now() / 1000);
const profile = {id:'fixture-agent', name:'FIXTURE ONLY · Saved beach agent', build_id:'fixture-build', source_revision:'fixture-revision', model:'gpt-6-luna', status:'ready', created_at:timestamp-600, instructions:'FIXTURE ONLY — synthetic instructions, never tuned by a provider.\n\nMove using directional controls. Inspect nearby targets before interacting. Close open menus before moving.', source_files:[{path:'fixture-only/controls.gd',included_lines:20,total_lines:20}], limitations:['Synthetic baseline data. No live tuning or gameplay has been verified.']};
const player = {id:'fixture-player',name:'FIXTURE bot',role:'host',initial:true,worker_id:'fixture-worker',goal:'FIXTURE · Inspect the beach'};
function fixture(name) {
 const run = {id:`fixture-${name}`,agent_id:profile.id,agent_profile:profile,model:profile.model,state:name==='ended'?'Ended':name==='paused'?'Paused':name==='provider-error'?'Investigating':'Running',created_at:timestamp-300,started_at:timestamp-290,ending_reason:name==='ended'?'fixture_operator_stop':undefined,world_simulating:name==='ended'?false:true,
  plan:{title:`FIXTURE · ${name}`,primary_goal:'FIXTURE · Inspect the beach',build_id:profile.build_id,players:[player],transport:{mode:'enet'},world_setup:{mode:'fresh'},limits:{duration_seconds:1800}},progress:{stage:'FIXTURE observation retained',last_progress_at:timestamp-15},
  players:{[player.id]:{phase:'ready',launched:true,epoch:'fixture-epoch',feed_captured_at:timestamp-5,activity:'FIXTURE · Nearby target observed',agent_status:{phase:'observing',detail:'FIXTURE · Reading retained structured observation',updated_at:timestamp-5,completed_at:timestamp-10},structured_context:{ready:true,complete:false,revision:7,timestamp:timestamp-5,entities:[{id:'fixture-workbench',name:'FIXTURE workbench'}],state:{player:{name:'FIXTURE bot',inventory:[]},objectives:['FIXTURE · Inspect the beach']}}}},
  events:[{id:'fixture-event-1',type:'observation',created_at:timestamp-15,title:'FIXTURE · Observation received',description:'Synthetic revision 7. No game was started.'}],
  usage:[{id:'fixture-usage',api:'codex',billing:'subscription',purpose:'gameplay',model:profile.model,usage_scope:'selection_total',created_at:timestamp-10,usage:{input_tokens:1000,cached_input_tokens:300,cache_write_input_tokens:0,output_tokens:200,reasoning_output_tokens:80,total_tokens:1200}}],
  subgoal_plan:{primary_goal:'FIXTURE · Inspect the beach',root_id:'root',player_id:player.id,active_goal_id:'inspect',status:'active',reason:'FIXTURE · Inspect only the observed nearby target.',goals:[{id:'root',parent_id:null,objective:'FIXTURE · Inspect the beach',status:'active',completion:{kind:'manual',description:'Operator checks retained evidence.'}},{id:'inspect',parent_id:'root',objective:'FIXTURE · Inspect workbench',status:'active',completion:{description:'A workbench interaction is observed.'}}]}};
 if(name==='paused'||name==='provider-error')run.pause={reason:name==='paused'?'FIXTURE · Paused by operator':'FIXTURE · Provider request failed (HTTP 429 quota)',players:[player.id],recovery:'FIXTURE · Inspect diagnostics before resuming. No real service is connected.'};
 if(name==='provider-error')Object.assign(run.players[player.id].agent_status,{phase:'error',detail:'FIXTURE · Action selection request failed',last_error:'FIXTURE HTTP 429 · Provider quota exhausted; no real AI request was made.',error_at:timestamp-5});
 if(name==='ended'){run.players[player.id].phase='ended';run.players[player.id].agent_status={phase:'ended',detail:'FIXTURE · Operator stopped the synthetic instance'};run.events.push({id:'fixture-event-2',type:'ended',created_at:timestamp,title:'FIXTURE · Instance ended',description:'Synthetic teardown confirmed; history only.'});}
 const empty=name==='no-setup',hasRun=!['no-setup','saved-agent'].includes(name);
 const data={agents:empty?[]:[profile],runs:hasRun?[run]:[],usage:hasRun?run.usage:[],builds:empty?[]:[{id:'fixture-build',name:'FIXTURE game build',source_revision:'fixture-revision'}],workers:empty?[]:[{id:'fixture-worker',name:'FIXTURE desktop (not connected)',enabled:true}],accounts:[],leases:[],settings:{concurrent_sessions:1,duration_seconds:1800,hardware:{}},ai:{configured:!empty,auth_method:empty?null:'chatgpt',status:empty?'not_connected':'ready',message:'FIXTURE ONLY · No account or provider connected',model:profile.model,available_models:empty?[]:[profile.model]},model_options:[{id:profile.model,label:'GPT-6 Luna (fixture)'}]};
 return {data,run,route:empty?'sessions':name==='saved-agent'?`agents/${profile.id}`:`runs/${run.id}`};
}
const assets = new Map(['index.html','app.js','style.css','favicon.svg'].map(name=>[name,fs.readFileSync(path.join(root,'ashad/static',name))]));
const frame = '<svg xmlns="http://www.w3.org/2000/svg" width="960" height="540" viewBox="0 0 960 540"><rect width="960" height="540" fill="#12212b"/><rect x="25" y="25" width="910" height="490" rx="12" fill="none" stroke="#59d8b0" stroke-width="3"/><g fill="#c6f7e7" font-family="sans-serif" text-anchor="middle"><text x="480" y="210" font-size="42">FIXTURE ONLY</text><text x="480" y="275" font-size="28">Synthetic camera frame · NOT gameplay</text><text x="480" y="330" font-size="20">No controller · No AI requests · No worker</text></g></svg>';
const checking = process.argv.includes('--check');
if(!checking)fs.mkdirSync(output,{recursive:true});
const server = http.createServer((req,res)=>{
 const url=new URL(req.url,'http://127.0.0.1');
 const selected=/(?:^|;\s*)baseline=([^;]+)/.exec(req.headers.cookie||'')?.[1]||'no-setup';
 const name=url.searchParams.get('fixture')||selected;
 function send(status,type,body){
  res.writeHead(status,{'Content-Type':type,'Cache-Control':'no-store','Content-Security-Policy':"default-src 'self'; connect-src 'self'; img-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; object-src 'none'; frame-src 'none'; base-uri 'none'; form-action 'none'"});res.end(body);
  if(!checking)fs.appendFileSync(path.join(output,'requests.jsonl'),JSON.stringify({method:req.method,path:url.pathname,fixture:name,status})+'\n');
 }
 if(req.method!=='GET')return send(405,'application/json',JSON.stringify({detail:'FIXTURE ONLY: mutations disabled'}));
 if(!names.includes(name))return send(400,'text/plain','Unknown fixture');
 const {data,run}=fixture(name);
 if(url.pathname==='/'||url.pathname==='/static/index.html'){
  res.setHeader('Set-Cookie',`baseline=${name}; SameSite=Strict; Path=/`);
  const banner=`<div id="fixture-banner" style="padding:9px;background:#ffe08a;color:#171717;text-align:center;font:700 14px sans-serif">FIXTURE ONLY · ${name} · No controller / AI / gameplay · P0.2 baseline</div>`;
  return send(200,'text/html; charset=utf-8',assets.get('index.html').toString().replace('</body>',banner+'</body>'));
 }
 const asset=url.pathname.startsWith('/static/')?url.pathname.slice(8):'';
 if(assets.has(asset)&&asset!=='index.html')return send(200,asset.endsWith('.js')?'text/javascript':asset.endsWith('.css')?'text/css':'image/svg+xml',assets.get(asset));
 if(url.pathname==='/api/bootstrap')return send(200,'application/json',JSON.stringify(data));
 if(url.pathname===`/api/runs/${run.id}`)return send(200,'application/json',JSON.stringify(run));
 if(url.pathname===`/api/runs/${run.id}/players/${player.id}/live`)return name==='missing-feed'?send(503,'text/plain','FIXTURE missing feed'):send(200,'image/svg+xml',frame);
 return send(404,'text/plain','FIXTURE ONLY: route not allowed');
});
server.listen(0,'127.0.0.1',async()=>{
 const origin=`http://127.0.0.1:${server.address().port}`;
 if(checking){
  try{
   for(const name of names){
    const headers={Cookie:`baseline=${name}`};
    assert.equal((await fetch(`${origin}/?fixture=${name}`)).status,200);
    const data=await (await fetch(`${origin}/api/bootstrap`,{headers})).json();
    assert.equal(data.agents.length,name==='no-setup'?0:1);
    if(data.runs.length){const run=await (await fetch(`${origin}/api/runs/fixture-${name}`,{headers})).json();assert.equal(run.state,fixture(name).run.state);assert.equal((await fetch(`${origin}/api/runs/fixture-${name}/players/fixture-player/live`,{headers})).status,name==='missing-feed'?503:200);}
   }
   assert.equal((await fetch(`${origin}/api/instances`,{method:'POST'})).status,405);
   assert.equal((await fetch(`${origin}/api/agents/tune`,{method:'POST'})).status,405);
   assert.equal((await fetch(`${origin}/README.md`)).status,404);
   assert.equal((await fetch(`${origin}/?fixture=invalid`)).status,400);
   console.log('PASS: seven fixture routes, synthetic feeds, unknown route/fixture rejection, launch/tune mutation rejection');
  }catch(error){console.error(error);process.exitCode=1;}finally{server.close();server.closeAllConnections();}
 }else{
  const manifest={origin,pid:process.pid,created_at:new Date().toISOString(),fixture_only:true,source_sha256:Object.fromEntries([...assets].map(([name,body])=>[name,crypto.createHash('sha256').update(body).digest('hex')])),states:names.map(name=>({name,url:`${origin}/?fixture=${name}#/${fixture(name).route}`}))};
  fs.writeFileSync(path.join(output,'server.json'),JSON.stringify(manifest,null,2)+'\n');
  console.log(JSON.stringify(manifest,null,2));
 }
});
