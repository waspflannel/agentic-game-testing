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
 const profile={id:'fixture-agent',name:'Agronauts agent',build_id:'fixture-build',source_revision:'fixture-revision',model:'gpt-6-luna',status:'ready',created_at:Date.now()/1000,instructions:'Controls\n\nMove with the directional controls. Close an open window before moving. Advance the cutscene to reveal its text, then advance again to continue.',source_files:[{path:'scripts/ui/cutscene.gd',included_lines:80,total_lines:80}],limitations:[]};
 const player={id:'polling-player',name:'Agronauts agent',role:'host',initial:true,worker_id:'fixture-worker',goal:'Explore the beach'};
 const run={id:'polling-ui-check',agent_id:profile.id,agent_profile:profile,state:'Running',created_at:1,model:profile.model,
  plan:{title:'Explore the beach',primary_goal:'Explore the beach',players:[player],transport:{mode:'enet'},world_setup:{mode:'fresh'},limits:{}},events:[],progress:{},
  players:{[player.id]:{phase:'ready',launched:true,epoch:'fixture-epoch',feed_captured_at:1,activity:'Before update',
   structured_context:{ready:true,revision:1,timestamp:1,entities:[],state:{}}}}};
 const data={agents:[],runs:[],usage:[],builds:[{id:'fixture-build',name:'Agronauts',source_revision:'fixture-revision'}],workers:[{id:'fixture-worker',name:'Desktop',enabled:true}],accounts:[],leases:[],settings:{concurrent_sessions:1,hardware:{}},ai:{configured:true,auth_method:'chatgpt',status:'ready',model:'gpt-6-luna',available_models:['gpt-6-luna','gpt-6-sol']},model_options:[{id:'gpt-6-luna',label:'GPT-6 Luna'},{id:'gpt-6-sol',label:'GPT-6 Sol'}]};
 let polls=0,tuneCalls=0,launchCalls=0,finishTune;
 const json=body=>new Response(JSON.stringify(body),{headers:{'Content-Type':'application/json'}});
 window.fetch=async(input,options)=>{
  const path=new URL(input,location.href).pathname,method=options?.method||'GET';
  if(path==='/api/bootstrap'&&method==='GET'){polls++;return json(data);}
  if(path===`/api/runs/${run.id}`&&method==='GET')return json(run);
  if(path==='/api/settings/codex/status'&&method==='GET')return json(data.ai);
  if(path==='/api/agents/tune'&&method==='POST'){
   tuneCalls++;const payload=JSON.parse(options.body);
   assert(payload.build_id==='fixture-build'&&payload.model==='gpt-6-luna','Wrong tuning inputs');
   await new Promise(resolve=>{finishTune=resolve;});data.agents=[profile];return json(profile);
  }
  if(path==='/api/instances'&&method==='POST'){
   launchCalls++;const payload=JSON.parse(options.body);
   assert(payload.agent_id===profile.id&&payload.primary_goal==='Explore the beach'&&payload.transport==='steam'&&payload.request_id,'Wrong instance inputs');
   assert(Object.keys(payload).length===4,'Unexpected advanced instance inputs');
   data.runs=[run];return json(run);
  }
  throw new Error(`Unexpected request: ${method} ${path}`);
 };
 try{
  if(document.querySelector('#retry-connection'))document.querySelector('#retry-connection').click();
  await navigate('#/sessions');await until(()=>polls>0&&document.querySelector('[data-action="new"]'));
  assert(!document.querySelector('a[href="#/compare"],a[href="#/scenarios"],a[href="#/balancing"],a[href="#/incidents"]'),'Deferred navigation is still exposed');
  const main=document.querySelector('main'),button=document.querySelector('[data-action="new"]');
  button.focus();let previous=polls;await until(()=>polls>previous);
  assert(main===document.querySelector('main'),'Polling replaced the main element');
  assert(document.activeElement===button,'Polling lost keyboard focus');
  await navigate('#/agents');await until(()=>document.querySelector('#tune-agent-form'));
  document.querySelector('#tune-agent-form').requestSubmit();await until(()=>tuneCalls===1);
  assert(document.querySelector('#tune-agent-form fieldset').disabled,'Tuning form must stay disabled while pending');
  previous=polls;await until(()=>polls>previous);assert(tuneCalls===1,'Polling duplicated tuning');
  finishTune();await until(()=>location.hash===`#/agents/${profile.id}`&&document.querySelector('.agent-instructions'));
  assert(document.querySelector('.agent-instructions').textContent.includes('scripts/ui/cutscene.gd'),'Source provenance is missing');
  await navigate(`#/new/${profile.id}`);
  const objective=document.querySelector('[data-path="primary_goal"]');
  objective.value='Explore the beach';objective.dispatchEvent(new Event('input',{bubbles:true}));
  const transport=document.querySelector('[data-path="transport"]');transport.value='steam';transport.dispatchEvent(new Event('change',{bubbles:true}));
  objective.focus();previous=polls;await until(()=>polls>previous);
  assert(document.activeElement===objective&&objective.value==='Explore the beach','Polling replaced the instance draft');
  assert(document.querySelectorAll('#instance-form [data-path]').length===2,'New instance must have exactly two inputs');
  document.querySelector('#instance-form').requestSubmit();await until(()=>location.hash===`#/runs/${run.id}`&&document.querySelector('.structured-inspector'));
  assert(launchCalls===1,'Instance was started more than once');
  const runMain=document.querySelector('main'),details=document.querySelector('.structured-inspector');
  const image=document.querySelector('img[data-feed]');details.open=true;details.querySelector('summary').focus();
  previous=polls;await until(()=>polls>previous);
  assert(runMain===document.querySelector('main'),'Live polling replaced the main element');
  assert(details===document.querySelector('.structured-inspector')&&details.open,'Polling collapsed context');
  assert(image===document.querySelector('img[data-feed]'),'Polling replaced the live image');
  const oldSrc=image.src;run.state='Investigating';run.investigation_deadline=Date.now()/1000+1800;
  run.players[player.id].activity='After update';run.players[player.id].feed_captured_at=2;
  run.players[player.id].structured_context.revision=2;
  previous=polls;await until(()=>polls>previous);
  assert(runMain===document.querySelector('main'),'A status change replaced the main element');
  assert(image===document.querySelector('img[data-feed]')&&image.src===oldSrc,'Polling must not reconnect the live stream');
  assert(details.open&&details.textContent.includes('revision 2'),'Context did not update in place');
  assert(document.querySelector('.activity').textContent.includes('After update'),'Activity stopped updating');
  assert(document.querySelector('.investigation-banner'),'Investigation state did not render');
  assert(document.activeElement===details.querySelector('summary'),'A status change lost keyboard focus');
  await navigate('#/sessions');assert(!image.hasAttribute('src'),'Leaving the camera page must disconnect the stream');
  await navigate(`#/agents/${profile.id}`);assert(document.querySelector('a[href="#/runs/polling-ui-check"]'),'Agent must list its instance');
  await navigate('#/usage');assert(document.querySelector('h1').textContent==='Usage & capacity','Usage page unavailable');
  await navigate('#/settings');assert(!document.querySelector('a[href="#/settings/fixtures"]'),'Saved starts still exposed');
  await navigate(`#/new/${profile.id}`);
  return 'PASS: agent tuning and instance launch, two-field setup, usage/settings, stable stream, polling, draft/focus preservation and camera teardown';
 }finally{window.fetch=originalFetch;location.hash=originalHash||'#/sessions';}
})()
