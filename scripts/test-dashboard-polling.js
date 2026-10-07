// Browser regression check with mocked API responses. No game or AI is started.
// Serve ashad/ with python -m http.server and open /static/index.html first.
// Get-Content scripts/test-dashboard-polling.js -Raw | agent-browser.cmd --session agent-ui-check eval --stdin
(async()=>{
 const assert=(condition,message)=>{if(!condition)throw new Error(message);};
 const until=async(predicate)=>{
  const deadline=Date.now()+12000;
  while(!predicate()){if(Date.now()>deadline)throw new Error('Dashboard update timed out');await new Promise(r=>setTimeout(r,50));}
 };
 const navigate=async hash=>{
  if(location.hash===hash)return;
  location.hash=hash;
  await new Promise(resolve=>addEventListener('hashchange',()=>setTimeout(resolve,0),{once:true}));
 };
 const originalFetch=window.fetch,originalHash=location.hash;
 const profile={id:'fixture-agent',name:'Agronauts agent',project_id:'fixture-project',build_id:'fixture-build',source_revision:'fixture-revision',model:'gpt-6-luna',status:'ready',created_at:Date.now()/1000,instructions:'Move with the directional controls.',source_files:[{path:'scripts/ui/cutscene.gd'}],limitations:[]};
 const player={id:'polling-player',name:'Agronauts agent',role:'host',initial:true,worker_id:'fixture-worker',goal:'Explore the beach'};
 const run={id:'polling-ui-check',project_id:'fixture-project',agent_id:profile.id,agent_profile:profile,state:'Running',created_at:1,model:profile.model,
  plan:{title:'Explore the beach',primary_goal:'Explore the beach',players:[player],transport:{mode:'enet'},world_setup:{mode:'fresh'},limits:{}},events:[],progress:{},usage:[],
  players:{[player.id]:{phase:'ready',launched:true,epoch:'fixture-epoch',feed_captured_at:Date.now()/1000,activity:'Before update',
   structured_context:{ready:true,revision:1,timestamp:Date.now()/1000,entities:[],state:{}}}}};
 const ready={provider:{ready:true},build_validation:{ready:true},environments:[{id:'fixture-worker',ready:true}],slot:{available:true,owners:[],limit:1},ready:true};
 const data={projects:[{id:'fixture-project',name:'Agronauts'}],agents:[profile],runs:[],usage_count:0,builds:[{id:'fixture-build',project_id:'fixture-project',name:'Agronauts',source_revision:'fixture-revision'}],workers:[{id:'fixture-worker',name:'Desktop',enabled:true}],accounts:[],leases:[],instance_slot:{available:true,owners:[],limit:1},settings:{concurrent_sessions:1,hardware:{}},ai:{configured:true,auth_method:'chatgpt',status:'ready',model:'gpt-6-luna',available_models:['gpt-6-luna']},model_options:[{id:'gpt-6-luna',label:'GPT-6 Luna'}]};
 let polls=0,launchCalls=0,usageCalls=0;
 const json=body=>new Response(JSON.stringify(body),{headers:{'Content-Type':'application/json'}});
 window.fetch=async(input,options)=>{
  const path=new URL(input,location.href).pathname,method=options?.method||'GET';
  if(path==='/api/bootstrap'&&method==='GET'){polls++;return json(data);}
  if(path===`/api/runs/${run.id}`&&method==='GET')return json(run);
  if(path==='/api/usage'&&method==='GET'){usageCalls++;return json({usage:[]});}
  if(path==='/api/settings/codex/status'&&method==='GET')return json(data.ai);
  if(path==='/api/projects/fixture-project/readiness'&&method==='GET')return json(ready);
  if(path==='/api/instances'&&method==='POST'){
   launchCalls++;const payload=JSON.parse(options.body);
   assert(payload.agent_id===profile.id&&payload.project_id==='fixture-project'&&payload.primary_goal==='Explore the beach'&&payload.transport==='enet'&&payload.request_id,'Wrong instance inputs');
   data.runs=[run];return json(run);
  }
  throw new Error(`Unexpected request: ${method} ${path}`);
 };
 try{
  if(document.querySelector('#retry-connection'))document.querySelector('#retry-connection').click();
  await navigate('#/projects');await until(()=>polls>0&&document.querySelector('#workspace-navigation'));
  assert(!document.querySelector('a[href="#/sessions"],a[href="#/agents"],a[href="#/compare"],a[href="#/scenarios"]'),'Legacy navigation is still exposed');
  await navigate(`#/new/${profile.id}`);await until(()=>location.hash==='#/projects/fixture-project/new'&&document.querySelector('[data-workspace="composer"]'));
  assert(document.querySelector('[data-w-field="agent_id"]').value===profile.id,'Legacy link did not preselect its agent');
  const objective=document.querySelector('[data-w-field="primary_goal"]');
  objective.value='Explore the beach';objective.dispatchEvent(new Event('input',{bubbles:true}));
  objective.focus();let previous=polls;await until(()=>polls>previous);
  assert(document.activeElement===objective&&objective.value==='Explore the beach','Polling replaced the instance draft');
  document.querySelector('[data-workspace="composer"]').requestSubmit();await until(()=>location.hash===`#/runs/${run.id}`&&document.querySelector('.instance-inspector'));
  assert(launchCalls===1,'Instance was started more than once');
  const runMain=document.querySelector('main'),details=document.querySelector('.instance-inspector');
  const image=document.querySelector('img[data-feed]');details.querySelector('summary').focus();
  assert(/LIVE VIEW/.test(document.querySelector('.feed-overlays').textContent),'Fresh capture is not shown as live');
  previous=polls;await until(()=>polls>previous);
  assert(runMain===document.querySelector('main'),'Live polling replaced the main element');
  assert(details===document.querySelector('.instance-inspector')&&details.open,'Polling collapsed the inspector');
  assert(image===document.querySelector('img[data-feed]'),'Polling replaced the live image');
  const oldSrc=image.src;run.state='Investigating';run.investigation_deadline=Date.now()/1000+1800;
  run.players[player.id].activity='After update';run.players[player.id].feed_captured_at=Date.now()/1000-60;
  previous=polls;await until(()=>polls>previous);
  assert(runMain===document.querySelector('main'),'A status change replaced the main element');
  assert(image===document.querySelector('img[data-feed]')&&image.src===oldSrc,'Polling must not reconnect the live stream');
  assert(/STALE/.test(document.querySelector('.feed-overlays').textContent),'Stalled capture is still shown as live');
  assert(document.querySelector('.activity').textContent.includes('After update'),'Activity stopped updating');
  assert(document.querySelector('.investigation-banner'),'Investigation state did not render');
  assert(document.activeElement===details.querySelector('summary'),'A status change lost keyboard focus');
  await navigate('#/projects');assert(!image.hasAttribute('src'),'Leaving the camera page must disconnect the stream');
  await navigate('#/usage');await until(()=>usageCalls>0&&document.querySelector('h1')?.textContent==='Usage & capacity');
  await navigate('#/settings');assert(!document.querySelector('a[href="#/settings/fixtures"]'),'Saved starts still exposed');
  return 'PASS: legacy link redirect, project composer launch, usage fetch, stable stream with capture freshness, polling, draft/focus preservation and camera teardown';
 }finally{window.fetch=originalFetch;location.hash=originalHash||'#/projects';}
})()
