'use strict';
// Native workspace extension. The legacy renderer owns API, shell and lifecycle controls.
(() => {
 let context, bound=false, setup=null, migration=null, opener=null, underlyingDirty=false;
 const drafts=new Map(), checks=new Map(), attempts=new Map(), historyPages=new Map();
 const storageKey='ashad.workspace.pending.v1';
 const C=()=>context || globalThis.ashadWorkspaceContext;
 const data=()=>C().state.data || {};
 const list=value=>C().arr(value);
 const e=value=>C().esc(value);
 const url=value=>encodeURIComponent(value);
 const invalid=(field,message)=>Object.assign(new Error(message),{field});
 const action=(label,name,project='',extra='')=>`<button type="button" class="button ghost small" data-w-action="${name}" data-project="${e(project)}" ${extra}>${e(label)}</button>`;
 const link=(route,label,current=false)=>`<a href="#/${route}" ${current?'aria-current="page"':''}>${e(label)}</a>`;
 const project=id=>list(data().projects).find(item=>item.id===id);
 const builds=id=>list(data().builds).filter(item=>item.project_id===id);
 function membership(item){
  const direct=item.project_id,build=list(data().builds).find(b=>b.id===item.build_id);
  if(direct)return project(direct)&&(!build?.project_id||build.project_id===direct)?direct:null;
  return build?.project_id&&project(build.project_id)?build.project_id:null;
 }
 const profiles=id=>list(data().agents).filter(item=>membership(item)===id&&builds(id).some(b=>b.id===item.build_id)&&['ready','completed',undefined].includes(item.status));
 const runs=id=>list(data().runs).filter(item=>membership(item)===id);
 function status(run){
  const owner=list(data().instance_slot?.owners).find(item=>item.run_id===run.id);
  const players=Object.values(run.players || {});
  if(players.some(p=>p.teardown_confirmed===false||p.phase==='teardown_unknown')||owner?.teardown_unconfirmed||String(owner?.reason||'').includes('teardown'))return 'Teardown unconfirmed';
  const value=String(run.state||'unknown').toLowerCase();
  if(['paused','investigating'].includes(value)||run.human_control||run.external_control)return 'Paused / inputs held';
  if(['queued','starting','running','stopping'].includes(value))return `Active · ${value}`;
  return ['ended','completed','failed','cancelled','terminated'].includes(value)?`Ended · ${value}`:'Unknown';
 }
 function thread(run){
  const current=C().state.route==='runs'&&C().state.parts[1]===run.id;
  return `<li data-key="thread-${e(run.id)}"><a class="workspace-thread" href="#/runs/${url(run.id)}" ${current?'aria-current="page"':''} title="${e(run.plan?.primary_goal||run.title||run.id)}"><span>${e(run.plan?.primary_goal||run.plan?.title||run.title||run.id)}</span><small>${e(status(run))}</small></a></li>`;
 }
 function sidebarThreads(items,id){
  const selected=C().state.route==='runs'?C().state.parts[1]:null,owners=new Set(list(data().instance_slot?.owners).map(owner=>owner.run_id));
  const visible=items.filter((run,index)=>index<30||run.id===selected||owners.has(run.id));
  return visible.map(thread).join('')+(items.length>visible.length?`<li>${link(id===null?'history/unassigned':`projects/${url(id)}`,`View all ${items.length} instances`)}</li>`:'');
 }
 function historyView(items,id){
  const page=Math.min(historyPages.get(id)||0,Math.max(0,Math.ceil(items.length/60)-1)),start=page*60;
  return `<ul class="workspace-history">${items.slice(start,start+60).map(thread).join('')||'<li>No instances.</li>'}</ul>${items.length>60?`<div class="form-actions">${action('Previous history page','history-prev',id,page===0?'disabled':'')}<span>${start+1}–${Math.min(start+60,items.length)} of ${items.length}</span>${action('Next history page','history-next',id,start+60>=items.length?'disabled':'')}</div>`:''}`;
 }
 function workspaceSidebarView(){
  const state=C().state,currentProject=state.route==='projects'?state.parts[1]:membership(list(data().runs).find(r=>r.id===state.parts[1])||{});
  const unassigned=runs(null);
  return `<div id="workspace-navigation" class="workspace-navigation"><div class="workspace-nav-title">${link('projects','Projects',state.route==='projects'&&!state.parts[1])}${action('+','setup','', 'aria-label="Setup new game agent"')}</div><div class="workspace-project-list">${list(data().projects).map(p=>`<details data-key="project-${e(p.id)}" ${currentProject===p.id?'open':''}><summary title="${e(p.name)}">${e(p.name)}<small>${runs(p.id).length}</small></summary><div class="workspace-project-links">${link(`projects/${url(p.id)}`,'Project overview',state.route==='projects'&&state.parts[1]===p.id&&!state.parts[2])}<ul>${sidebarThreads(runs(p.id),p.id)}</ul>${link(`projects/${url(p.id)}/new`,'+ New instance',state.route==='projects'&&state.parts[1]===p.id&&state.parts[2]==='new')}</div></details>`).join('')}<details data-key="unassigned" ${(state.route==='history'&&state.parts[1]==='unassigned')||(state.route==='runs'&&!currentProject)?'open':''}><summary>Unassigned history<small>${unassigned.length}</small></summary><div class="workspace-project-links">${link('history/unassigned','Review membership')}<ul>${sidebarThreads(unassigned,null)}</ul></div></details></div><div class="workspace-nav-system">${link('usage','Usage',state.route==='usage')}${link('settings','Settings',state.route==='settings')}</div>${slotView()}</div>`;
 }
 function workspaceUpdateSidebar(){
  const root=document.querySelector('#workspace-navigation');
  if(!root)return;
  // Patch the parent, not the draft. patchHTML preserves keyed details, focus and scroll.
  C().patchHTML(root.parentElement,workspaceSidebarView());
 }
 function slotView(slot=data().instance_slot){
  if(slot?.available!==false)return '';
  return C().notice(`Start blocked. ${e(slot.blocked_reason||'An instance owns the global slot.')} Drafts never auto-launch. ${list(slot.owners).map(owner=>owner.run_id?link(`runs/${url(owner.run_id)}`,`Return to owning instance: ${owner.run_id}`):link('settings/workers','Reconcile unresolved desktop ownership')).join(' · ')}`,'warning');
 }
 function readinessView(record){
  if(!record)return '<p class="section-note">Not inspected. Registration and an enabled environment do not prove readiness.</p>';
  const r=record.result;
  return `<div class="workspace-checks"><p class="section-note">Inspected ${e(new Date(record.at).toLocaleString())}. Results are a snapshot; Start checks again.</p><ul><li>Build: ${e(r.build_validation?.ready===true?'Validated':r.build_validation?.reason||'Unknown — not ready')}</li><li>AI connection: ${e(r.provider?.message||r.provider?.status||'Unknown — not ready')} ${r.provider?.ready===true?'':link('settings/ai','Open AI connection settings')}</li>${list(r.environments).map(env=>`<li>${e(env.name||env.id)}: ${e(env.ready===true?'Compatible and idle':env.reason||'Unknown — not ready')}${env.checks?`<details><summary>Exact environment checks</summary><pre class="json-details">${e(JSON.stringify(env.checks,null,2))}</pre></details>`:''}</li>`).join('')||'<li>No compatible desktop reported.</li>'}</ul><details><summary>Exact inspection response</summary><pre class="json-details">${e(JSON.stringify(r,null,2))}</pre></details>${slotView(r.slot)}</div>`;
 }
 const checkKey=(p,b,a='')=>`${p}/${b}/${a}`;
 function unassignedHistoryView(){return C().heading('Workspace','Unassigned history','Missing or ambiguous stable membership remains readable. Names never determine grouping.',action('Review project grouping','migration'))+historyView(runs(null),'history:unassigned');}
 function projectOverviewView(id){
  if(!id)return C().heading('Workspace','Game projects','Supported instrumented builds only. Open retained history without AI sign-in or Docker.',action('Setup new game agent','setup'))+(!list(data().projects).length?C().notice('No projects yet. Setup creates a project, validates a pinned build, prepares a compatible desktop and explicitly tunes a named configuration.'):list(data().projects).map(p=>`<article class="workspace-row">${link(`projects/${url(p.id)}`,p.name)}<code>${e(p.id)}</code><span>${builds(p.id).length} builds · ${profiles(p.id).length} profiles</span></article>`).join(''));
    const p=project(id);if(!p)return C().heading('Workspace','Project unavailable','The project ID is missing. Retained runs remain accessible in unassigned history.');
  const bs=builds(id),agents=profiles(id),recent=runs(id);
  return C().heading('Project',p.name,'Pinned build revisions, immutable saved profiles and fresh-world instances.',action(agents.length?'Add agent configuration':'Continue setup','setup',id)+` <a class="button primary small" href="#/projects/${url(id)}/new">New instance</a>`)+
   (!bs.length?C().notice('Incomplete setup: register a supported pinned build.'):!agents.length?C().notice('Incomplete setup: inspect the desktop and explicitly tune a named configuration.'):C().notice('Saved instructions are source-grounded, not a live gameplay competence test.'))+
   C().section('Build revisions','Changed artifacts require a new registration ID.',bs.map(b=>`<article class="workspace-row"><strong>${e(b.name||b.id)}</strong><code>${e(b.id)}</code><span>${e(b.source_revision||'Source revision unknown')}</span>${action('Inspect readiness','inspect',id,`data-build="${e(b.id)}"`)}</article>${readinessView(checks.get(checkKey(id,b.id)))}`).join('')||'<p>No registered build.</p>')+
   C().section('Saved configurations','Review a saved guide without tuning or launching.',agents.map(a=>`<article class="workspace-row"><strong>${e(a.name||a.id)}</strong><span>${e(a.model)} · ${e(a.build_id)}</span>${action('Review instructions','review',id,`data-agent="${e(a.id)}"`)}</article>`).join('')||'<p>No saved configurations.</p>')+
   C().section('Instance history','Opening a thread is navigation only.',historyView(recent,id));
 }
 function composer(id){
  if(!drafts.has(id))drafts.set(id,{project_id:id,agent_id:profiles(id)[0]?.id||'',primary_goal:'',transport:'enet',dirty:false,busy:false,error:''});
  return drafts.get(id);
 }
 function input(label,key,value,options={}){
  const id=`workspace-${key}`,attributes=`data-w-field="${e(key)}" id="${e(id)}" ${options.required?'required':''} ${options.max?`maxlength="${options.max}"`:''} ${options.error?`aria-invalid="true" aria-describedby="${e(id)}-error"`:''}`;
  return `<div class="field"><label for="${e(id)}">${e(label)}</label>${options.options?`<select ${attributes}>${options.options.map(([v,l,disabled])=>`<option value="${e(v)}" ${v===value?'selected':''} ${disabled?'disabled':''}>${e(l)}</option>`).join('')}</select>`:options.textarea?`<textarea ${attributes}>${e(value)}</textarea>`:`<input ${attributes} value="${e(value)}" ${options.readonly?'readonly':''}>`}${options.error?`<small id="${e(id)}-error" class="danger-text">${e(options.error)}</small>`:''}</div>`;
 }
 function preparedIntentView(request,id){
  if(!request)return '';
  return `<details class="workspace-checks" open><summary>Prepared original launch intent</summary><dl class="definition-list"><dt>Configuration ID</dt><dd>${e(request.agent_id)}</dd><dt>Original goal</dt><dd class="objective-copy">${e(request.primary_goal)}</dd><dt>Transport</dt><dd>${e(request.transport)}</dd><dt>Request ID</dt><dd><code>${e(request.request_id)}</code></dd></dl><p class="section-note">Lookup is read-only. Restore asks before replacing this draft; nothing launches until you explicitly click Start again.</p>${action('Restore original intent…','restore-start',id)}</details>`;
 }
 function preparedIntent(saved,key,id){
  const request=saved.request;
  if(saved.status!=='prepared'||!request||request.request_id!==key.id||request.project_id&&request.project_id!==id||typeof request.agent_id!=='string'||typeof request.primary_goal!=='string'||!['enet','steam'].includes(request.transport))return null;
  return {agent_id:request.agent_id,project_id:request.project_id||id,primary_goal:request.primary_goal,transport:request.transport,request_id:request.request_id};
 }
 function projectComposerView(id){
  const p=project(id),agents=profiles(id),d=drafts.get(id)||{agent_id:agents[0]?.id||'',primary_goal:'',transport:'enet'},a=agents.find(a=>a.id===d.agent_id);
  if(!p)return projectOverviewView(id);
  if(!agents.length)return C().heading('New instance',p.name,'A complete saved profile is required.',action('Continue setup','setup',id));
  const inspected=checks.get(checkKey(id,a?.build_id,a?.id))?.result,steamReady=inspected?.steam?.ready===true;
  return C().heading('New instance',p.name,'One selected profile, one goal, one fresh world.')+slotView()+(d.reconciledRun?C().notice(`An earlier start was accepted. ${link(`runs/${url(d.reconciledRun)}`,'Review earlier accepted instance')}. Your changed goal draft is retained; starting it requires another explicit click.`):'')+`<form data-workspace="composer" data-project="${e(id)}" class="instance-form" aria-busy="${Boolean(d.busy)}"><fieldset ${d.busy?'disabled':''}><div class="field-grid">${input('Agent configuration','agent_id',d.agent_id,{required:true,error:d.errorField==='agent_id'?d.error:'',options:agents.map(a=>[a.id,`${a.name||a.id} · ${a.model}`])})}${input('Primary goal','primary_goal',d.primary_goal,{textarea:true,required:true,max:4000,error:d.errorField==='primary_goal'?d.error:''})}${input('Transport','transport',d.transport,{error:d.errorField==='transport'?d.error:'',options:[['enet','ENet'],['steam',steamReady?'Steam — fresh licensed identity verified':'Steam — unavailable until fresh licensed identity is proven',!steamReady]]})}</div><p class="section-note">${e(a?.model||'Choose a profile')} · ${e(a?.build_id||'Build unknown')} · Fresh world. Model and build are fixed by this saved profile.</p><div class="form-actions"><button class="button primary" type="submit" ${data().instance_slot?.available===false||d.busy?'disabled':''}>${d.busy?'Checking / starting…':'Start instance'}</button>${action('Check readiness','composer-inspect',id)}${attempts.has(`start:${id}`)?action('Reconcile pending start','reconcile-start',id):''}</div></fieldset>${d.error?`<p class="danger-text" role="alert">${e(d.error)}</p>`:''}${preparedIntentView(d.preparedRequest,id)}${readinessView(checks.get(checkKey(id,a?.build_id,a?.id)))}</form>`;
 }
 // Legacy #/new/<agent> links open the project composer; agents without stable membership need explicit grouping.
 function workspaceNewInstanceRoute(agentId){
  const a=list(data().agents).find(a=>a.id===agentId),id=a&&membership(a);
  if(!a)return 'projects';
  if(!id||!profiles(id).some(p=>p.id===agentId))return 'history/unassigned';
  const d=composer(id);if(!d.busy)d.agent_id=agentId;
  return `projects/${url(id)}/new`;
 }
 function buildIdentityView(build){
  if(!build)return '';
  return `<details class="workspace-checks"><summary>Pinned build identity · ${e(build.id)}</summary><dl class="definition-list"><dt>Revision</dt><dd>${e(build.source_revision||'Unknown')}</dd><dt>Artifact SHA-256</dt><dd><code>${e(build.artifact_sha256||'Unknown — inspection required')}</code></dd><dt>Engine SHA-256</dt><dd><code>${e(build.engine_sha256||'Unknown — inspection required')}</code></dd><dt>Manifest</dt><dd>${e(build.manifest_path||'Unknown')}</dd></dl><p class="section-note">Validated registration is immutable metadata, not deployment or desktop readiness.</p></details>`;
 }
 function newSetup(id){const bs=builds(id);return {project_id:id||'',project_name:'',build_id:bs[0]?.id||'',manifest_path:'',build_name:'',name:'',model:data().ai?.model||'',stage:id?'build':'project',agent_id:'',dirty:false,busy:false,error:''};}
 function setupView(s){
  const p=project(s.project_id),bs=builds(s.project_id),a=list(data().agents).find(a=>a.id===s.agent_id),record=checks.get(checkKey(s.project_id,s.build_id));
  let body='';
  if(s.stage==='project')body=input('Project name','project_name',s.project_name,{required:true,max:200,error:s.errorField==='project_name'?s.error:''})+'<p class="section-note">One project per game. Duplicate names remain separate IDs. Explicit Create is the only save.</p><button class="button primary" type="submit">Create project</button>';
  if(s.stage==='build')body=input('Registered build revision','build_id',s.build_id,{options:[['','Select or register a build'],...bs.map(b=>[b.id,`${b.name||b.id} · ${b.id}`])]})+input('Manifest path','manifest_path',s.manifest_path,{readonly:Boolean(globalThis.window?.ashadDesktop?.chooseBuild),error:s.errorField==='manifest_path'?s.error:''})+`<div class="actions">${action('Choose supported manifest…','pick','', 'data-kind="manifest"')}${action('Choose supported build folder…','pick','', 'data-kind="folder"')}</div>`+input('Build revision name','build_name',s.build_name,{max:200})+'<p class="section-note">Selection returns a path only, not an import. Register validates source, artifacts and structured interface; arbitrary games are not supported. Changed artifacts need a new build ID.</p>'+action('Register selected pinned build','register')+`<button class="button primary" type="submit" ${!s.build_id?'disabled':''}>Continue to inspection</button>`;
  if(s.stage==='inspect')body=readinessView(record)+`<div class="actions">${action('Inspect compatibility and readiness','setup-inspect')}${action('Prepare supported desktop…','prepare')}<button class="button primary" type="submit" ${!setupReady(record?.result)?'disabled':''}>Configure agent</button></div><p class="section-note">Docker Desktop in Linux-container mode, the supported worker image and the selected validated build are required. Tuning does not launch a game.</p>`;
  if(s.stage==='configure'){
   const available=data().ai?.available_models;
   body=input('Configuration name','name',s.name,{required:true,max:200,error:s.errorField==='name'?s.error:''})+input('Account-supported model','model',s.model,{required:true,error:s.errorField==='model'?s.error:'',options:[['','Choose a model'],...list(data().model_options).map(m=>[m.id,m.label||m.id,Array.isArray(available)&&!available.includes(m.id)])]})+'<p class="section-note">Explicit tuning reads supported source to save immutable instructions. No gameplay, weight training or model fallback.</p><a href="#/settings/ai">AI connection settings — explicit supported external sign-in</a><button class="button primary" type="submit">Tune and save named profile</button>';
  }
  if(s.stage==='review')body=a?`<dl class="definition-list"><dt>Configuration</dt><dd>${e(a.name||a.id)}</dd><dt>Build</dt><dd>${e(a.build_id)}</dd><dt>Model</dt><dd>${e(a.model)}</dd></dl><h3>Saved instructions</h3><pre class="json-details workspace-instructions">${e(a.instructions)}</pre><ul>${list(a.limitations).map(item=>`<li>${e(item)}</li>`).join('')}</ul><p class="section-note">Already saved. Closing or finishing retains this profile. Source analysis is not a live gameplay test.</p><div class="actions">${action('Finish setup','finish')}${action('New instance','setup-new')}${action('Explicit retune as a new profile','retune')}</div>`:'<p>Saved profile unavailable. Return to the project to inspect retained profiles.</p>';
  return `<div id="workspace-setup" data-key="workspace-setup"><p class="section-note">${e(p?.name||'New game project')} · ${e(s.stage)}</p><ol class="workspace-steps" aria-label="Setup stages">${['project','build','inspect','configure','review'].map(stage=>`<li ${stage===s.stage?'aria-current="step"':''}>${e(stage)}</li>`).join('')}</ol>${buildIdentityView(bs.find(build=>build.id===s.build_id))}<fieldset ${s.busy?'disabled':''}>${body}${s.stage!=='review'&&attempts.get(`tune:${s.project_id}`)?.tried?action('Reconcile saved/pending tuning','reconcile-tune'):''}</fieldset>${s.busy?'<p role="status">Working… Wait for the real result; no game is launched.</p>':''}${s.error?`<p class="danger-text" role="alert">${e(s.error)}</p><p><a href="#/settings/ai">AI settings</a> · <a href="#/settings/workers">Desktop prerequisites</a></p>`:''}<div class="form-actions">${s.stage!=='project'&&s.stage!=='review'?action('Back','back'):''}${action('Cancel','close')}</div></div>`;
 }
 function showSetup(){
  if(!setup)return;
  const form=document.querySelector('#modal-form'),root=document.querySelector('#workspace-setup');
  if(root&&form){C().patchHTML(form.querySelector('.dialog-body'),setupView(setup));return;}
  C().modal('Setup new game agent',setupView(setup),null,null);
  const dialog=document.querySelector('#dialog');
  const close=dialog?.querySelector('[data-action="close-dialog"]');if(close){close.removeAttribute('data-action');close.dataset.wAction='close';}
  const next=document.querySelector('#modal-form');if(next){next.dataset.workspace='setup';next.onsubmit=null;}
 }
 async function openProjectSetup(id=null){
  initializeWorkspace(C());
  if(setup?.busy||migration?.busy)return;
  const current=C().state.parts,d=current[0]==='projects'&&current[2]==='new'?drafts.get(current[1]):null;
  if(d?.busy)return false;
  if(d?.dirty&&!document.querySelector('#workspace-setup')){if(!await workspaceConfirm('Leave the unsent goal draft and open setup? Its values stay in memory and it will never auto-launch.','Open setup'))return false;d.dirty=false;C().markDraftDirty(false);}
  if(id&&!project(id))throw new Error('Project unavailable.');
  if(setup&&setup.project_id!==(id||'')&&setup.dirty&&!await workspaceConfirm('Leave these setup changes? Saved projects and profiles are retained.','Leave setup'))return;
  if(!document.querySelector('#workspace-setup')&&!document.querySelector('#workspace-migration'))underlyingDirty=Boolean(C().state.dirtyDraft);
  opener=document.activeElement;
  if(!setup||setup.project_id!==(id||'')||(setup.stage==='review'&&!document.querySelector('#workspace-setup')))setup=newSetup(id);
  showSetup();return true;
 }
 function tunable(r){return r?.build_validation?.ready===true&&r.provider?.ready===true;}
 function setupReady(r){return tunable(r)&&list(r.environments).some(env=>env.ready===true);}
 function launchReady(r,transport){
  if(r?.ready!==true||!tunable(r)||r.slot?.available!==true||!list(r.environments).some(env=>env.ready===true))return false;
  return transport!=='steam'||r.steam?.ready===true||list(r.environments).some(env=>env.ready===true&&env.steam?.ready===true);
 }
 async function inspect(p,b,a=''){
  if(!p||!b)throw new Error('Choose a registered pinned build first.');
  const result=await C().api(`/api/projects/${url(p)}/readiness?build_id=${url(b)}${a?`&agent_id=${url(a)}`:''}`);
  const record={at:Date.now(),result};checks.set(checkKey(p,b,a),record);return result;
 }
 function remember(kind,item){const state=C().state;state.dataRevision++;state.data[kind]=[item,...list(data()[kind]).filter(other=>other.id!==item.id)];}
 function saveAttempts(){
  try{const records=[...attempts.entries()].slice(-24);globalThis.sessionStorage?.setItem(storageKey,JSON.stringify(records));}catch{/* Storage is optional; never store DOM values, paths, goals or credentials. */}
 }
 async function payloadSignature(payload){
  const encoded=new TextEncoder().encode(JSON.stringify(payload)),digest=await crypto.subtle.digest('SHA-256',encoded);
  return Array.from(new Uint8Array(digest),v=>v.toString(16).padStart(2,'0')).join('');
 }
 async function attempt(scope,payload,fresh=false){
  const signature=await payloadSignature(payload);
  let item=attempts.get(scope);
  if(fresh||item?.signature!==signature){item={id:crypto.randomUUID(),signature,tried:false};attempts.set(scope,item);}
  if(attempts.size>24)attempts.delete(attempts.keys().next().value);
  saveAttempts();return item;
 }
 function sent(item){item.tried=true;saveAttempts();}
 async function setupOperation(operation){
  const s=setup;if(!s||s.busy)return;
  s.busy=true;s.error='';C().markDraftDirty(true);showSetup();
  try{await operation(s);}catch(error){s.error=C().errorMessage(error);s.errorField=error.field||'';}finally{s.busy=false;if(setup===s){C().markDraftDirty(s.dirty||underlyingDirty);if(document.querySelector('#workspace-setup')){showSetup();if(s.errorField)document.querySelector(`#workspace-${s.errorField}`)?.focus();}}}
 }
 async function create(s){
  const name=s.project_name.trim();if(!name||name.length>200)throw invalid('project_name','Enter a project name, up to 200 characters.');
  const payload={name},key=await attempt('create',payload);sent(key);
  const p=await C().api('/api/projects','POST',{...payload,request_id:key.id});remember('projects',p);s.project_id=p.id;s.stage='build';s.dirty=false;attempts.delete('create');saveAttempts();
 }
 async function register(s){
  if(!s.manifest_path)throw invalid('manifest_path','Choose a supported manifest or folder path first.');
  const payload={project_id:s.project_id,name:s.build_name.trim()||'Pinned revision',manifest_path:s.manifest_path};
  const key=await attempt(`build:${s.project_id}`,payload);sent(key);
  const b=await C().api('/api/builds','POST',{...payload,id:`build_${key.id}`});remember('builds',b);s.build_id=b.id;s.manifest_path='';s.stage='inspect';s.dirty=false;
 }
 async function tune(s){
  if(!s.name.trim()||!s.model)throw invalid(s.name.trim()?'model':'name','Enter a configuration name and select an account-supported model.');
  const payload={project_id:s.project_id,build_id:s.build_id,name:s.name.trim(),model:s.model},key=await attempt(`tune:${s.project_id}`,payload);
  let a;
  if(key.tried){const saved=await C().api(`/api/agents/tuning/${url(key.id)}`);if(saved.status==='completed')a=saved.agent;else if(saved.status==='in_progress')throw new Error('This tuning attempt is still in progress. Wait, then retry to reconcile; no second inference was started.');}
  if(!a){const r=await inspect(s.project_id,s.build_id);if(!setupReady(r))throw new Error(r.provider?.message||r.build_validation?.reason||'Build, provider or compatible desktop not ready. Inspect checks and Settings.');
   if(Array.isArray(data().ai?.available_models)&&!data().ai.available_models.includes(s.model))throw invalid('model','This model is unavailable for the connected account. Choose another supported model explicitly.');
   sent(key);a=await C().api('/api/agents/tune','POST',{...payload,request_id:key.id});}
  remember('agents',a);attempts.delete(`tune:${s.project_id}`);saveAttempts();s.agent_id=a.id;s.stage='review';s.dirty=false;
 }
 async function pick(s,kind){
  const bridge=globalThis.window?.ashadDesktop;
  if(!bridge?.chooseBuild)throw new Error('Native file selection is unavailable. Reload ASHAD and try again.');
  const path=await bridge.chooseBuild(kind);
  if(!path||setup!==s||!document.querySelector('#workspace-setup'))return;
  s.manifest_path=path;s.dirty=true;C().markDraftDirty(true);showSetup();
 }
 async function prepare(s){
  if(!await workspaceConfirm('Prepare an ASHAD-managed Docker desktop for this pinned build? This can create/start a managed container, mount the selected build read-only and create or reuse persistent account volumes and backend worker credentials. Docker permissions are required. Existing active or unconfirmed desktops must not be repurposed. No game is launched and no system software is silently installed.','Prepare desktop'))return;
  const payload={build_id:s.build_id},key=await attempt(`prepare:${s.project_id}`,payload);sent(key);
  await C().api(`/api/projects/${url(s.project_id)}/environments/prepare`,'POST',{...payload,request_id:key.id,confirmed:true});
  // A completed prepare is not retried; the next explicit click is a new request the backend may reconcile.
  attempts.delete(`prepare:${s.project_id}`);saveAttempts();
  await inspect(s.project_id,s.build_id);
 }
 async function closeSetup(){
  const owner=document.querySelector('#workspace-setup')?setup:document.querySelector('#workspace-migration')?migration:null;
  if(!owner)return true;
  if(owner.busy){await workspaceConfirm('An operation is in progress. Wait for its result before closing setup.','Keep waiting',true);return false;}
  if(owner.dirty&&!await workspaceConfirm('Leave unsaved setup changes? Saved projects, pinned builds and profiles are retained.','Leave setup'))return false;
  owner.dirty=false;document.querySelector('#dialog')?.close();C().markDraftDirty(underlyingDirty);if(opener?.isConnected)opener.focus();return true;
 }
 function workspaceConfirm(copy,label,cancelOnly=false){
  return new Promise(resolve=>{
   const dialog=document.createElement('dialog');dialog.className='workspace-confirm';dialog.setAttribute('aria-labelledby','workspace-confirm-title');
   dialog.innerHTML=`<h2 id="workspace-confirm-title">Confirm operation</h2><p>${e(copy)}</p><div class="dialog-actions"><button type="button" data-answer="cancel" autofocus>Cancel</button>${cancelOnly?'':`<button type="button" class="button primary" data-answer="yes">${e(label)}</button>`}</div>`;
   const done=value=>{dialog.close();dialog.remove();resolve(value);};
   dialog.addEventListener('click',event=>{const answer=event.target.closest('[data-answer]');if(answer)done(answer.dataset.answer==='yes');});
   dialog.addEventListener('cancel',event=>{event.preventDefault();done(false);});document.body.append(dialog);dialog.showModal();dialog.querySelector('[data-answer="cancel"]').focus();
  });
 }
 async function workspaceBeforeNavigate(next){
  if(next===C().state.parts.join('/'))return true;
  if(document.querySelector('#workspace-setup')||document.querySelector('#workspace-migration'))return closeSetup();
  const current=C().state.parts,id=current[1],d=drafts.get(id);
  if(current[0]==='projects'&&current[2]==='new'&&d?.busy)return false;
  if(current[0]==='projects'&&current[2]==='new'&&d?.dirty){if(!await workspaceConfirm('Leave this unsent goal draft? It will not be launched automatically.','Leave draft'))return false;d.dirty=false;C().markDraftDirty(false);}
  return true;
 }
 async function start(id,reconcileOnly=false){
  const d=composer(id);if(d.busy)return;
  const epoch=C().state.renderEpoch;d.busy=true;d.error='';C().markDraftDirty(true);C().render(true);
  try{
   let key=attempts.get(`start:${id}`),run;
   if(reconcileOnly){
    if(!key)throw new Error('No pending start request.');
    const saved=await C().api(`/api/instances/requests/${url(key.id)}`);d.preparedRequest=preparedIntent(saved,key,id);
    if(saved.status!=='accepted'||!saved.run?.id)throw new Error(`Start status: ${saved.status}. Retain the goal and retry the same attempt; nothing was launched by this check.`);
    const current={agent_id:d.agent_id,project_id:id,primary_goal:d.primary_goal.trim(),transport:d.transport};
    if(current.primary_goal&&key.signature!==await payloadSignature(current)){remember('runs',saved.run);attempts.delete(`start:${id}`);saveAttempts();d.reconciledRun=saved.run.id;d.dirty=true;return;}
    run=saved.run;
   }
   else{
    const a=profiles(id).find(a=>a.id===d.agent_id);if(!a)throw invalid('agent_id','Choose a saved configuration in this project.');
    if(!d.primary_goal.trim())throw invalid('primary_goal','Enter a primary goal.');
    const payload={agent_id:a.id,project_id:id,primary_goal:d.primary_goal.trim(),transport:d.transport};
    // Editing a lost start cannot discard its key before GET establishes its outcome.
    if(key?.tried){
     const saved=await C().api(`/api/instances/requests/${url(key.id)}`),changed=key.signature!==await payloadSignature(payload);d.preparedRequest=preparedIntent(saved,key,id);
     if(saved.status==='accepted'){
      if(!saved.run?.id)throw new Error('Accepted start has no retained run identity. Reconcile this request before retrying.');
      if(changed){remember('runs',saved.run);attempts.delete(`start:${id}`);saveAttempts();d.reconciledRun=saved.run.id;d.dirty=true;return;}
      run=saved.run;
     }else if(saved.status==='in_progress'||(changed&&saved.status==='prepared'))throw new Error(`Earlier start status: ${saved.status}. Its original request ID is retained. Check again, or retry the original intent when prepared; changed inputs cannot create another start yet.`);
     else if(saved.status!=='unknown'&&saved.status!=='prepared')throw new Error(`Earlier start status: ${saved.status}. Reconcile before changing its request ID.`);
    }
    if(!run)key=await attempt(`start:${id}`,payload);

    if(!run){if(data().instance_slot?.available===false)throw new Error(`${data().instance_slot.blocked_reason||'Another instance owns the slot.'} This draft is retained and never auto-launched.`);
     const r=await inspect(id,a.build_id,a.id);if(!launchReady(r,d.transport))throw d.transport==='steam'&&launchReady(r,'enet')?invalid('transport','Steam requires fresh verified licensed identity on a compatible idle desktop. Review exact checks; no ENet fallback.'):new Error(`Not ready to start${d.transport==='steam'?' with Steam':''}. Review exact provider, build, desktop and slot checks; no ENet fallback.`);
     sent(key);run=await C().api('/api/instances','POST',{...payload,request_id:key.id});}
   }
   if(!run?.id)throw new Error('Accepted start has no retained run identity. Reconcile this request before retrying.');
   remember('runs',run);attempts.delete(`start:${id}`);saveAttempts();d.dirty=false;
   if(epoch===C().state.renderEpoch&&C().state.parts.join('/')===`projects/${id}/new`){C().markDraftDirty(false);C().navigate(`runs/${run.id}`);}
  }catch(error){d.error=C().errorMessage(error);d.errorField=error.field||'';if(error.code==='active_instance_conflict')data().instance_slot={available:false,owners:list(error.owning_instances),limit:1};}
  finally{d.busy=false;if(epoch===C().state.renderEpoch&&C().state.parts.join('/')===`projects/${id}/new`){C().markDraftDirty(d.dirty);C().render(true);if(d.errorField)document.querySelector(`#workspace-${d.errorField}`)?.focus();}}
 }
 function migrationBuildIds(){
  const ids=new Set(list(data().builds).map(b=>b.id));
  for(const a of list(data().agents))if(a.build_id)ids.add(a.build_id);
  for(const r of list(data().runs))for(const id of [r.build_id,r.plan?.build_id,r.approved_plan?.build_id,r.build?.id,r.agent_profile?.build_id])if(typeof id==='string'&&id)ids.add(id);
  return [...ids];
 }
 function migrationView(){
  const m=migration,groups=[...list(data().projects),...m.newGroups];
  return `<div id="workspace-migration" aria-busy="${m.busy}"><fieldset ${m.busy?'disabled':''}><p class="section-note">Assign stable build IDs explicitly. Duplicate project names never merge. Leave ambiguous builds unassigned; frozen evidence is not rewritten.</p>${migrationBuildIds().map(id=>input(id,`map:${id}`,m.mapping[id]||'',{options:[['','Keep unassigned'],...groups.map(p=>[p.id,`${p.name} · ${p.id}`])]})).join('')}${input('New project group name','group_name',m.group_name,{max:200})}${action('Add separate group','migration-add')}${action('Preview grouping','migration-preview')}${m.preview?`<details open><summary>Reviewed preview</summary><pre class="json-details">${e(JSON.stringify(m.preview,null,2))}</pre></details>${input('Fresh absolute backup directory','backup_dir',m.backup_dir)}<label class="checkbox-row"><input type="checkbox" data-w-field="confirmed" ${m.confirmed?'checked':''}> I reviewed this preview and backup scope and confirm metadata/evidence backup and grouping.</label>${action('Back up and apply reviewed grouping','migration-apply','',list(m.preview.blockers).length||!m.confirmed||m.applied?'disabled':'')}`:''}${m.error?`<p class="danger-text" role="alert">${e(m.error)}</p>`:''}${m.busy?'<p role="status">Waiting for the real operation result…</p>':''}</fieldset>${action('Cancel','close')}</div>`;
 }
 function showMigration(){
  const root=document.querySelector('#workspace-migration');if(root){C().patchHTML(root.parentElement,migrationView());return;}
  C().modal('Explicit project membership grouping',migrationView(),null,null);
  const form=document.querySelector('#modal-form');if(form){form.onsubmit=null;form.dataset.workspace='migration';}
  const close=document.querySelector('#dialog [data-action="close-dialog"]');if(close){close.removeAttribute('data-action');close.dataset.wAction='close';}
 }
 function migrationGroups(){const m=migration,groups=[...list(data().projects),...m.newGroups];return groups.map(p=>({project_id:p.id,name:p.name,build_ids:Object.keys(m.mapping).filter(id=>m.mapping[id]===p.id)})).filter(g=>g.build_ids.length);}
 async function migrationOperation(operation){if(!migration||migration.busy)return;const m=migration;m.busy=true;m.error='';C().markDraftDirty(true);showMigration();try{await operation(m);}catch(error){m.error=C().errorMessage(error);}finally{m.busy=false;if(document.querySelector('#workspace-migration'))showMigration();C().markDraftDirty(m.dirty||underlyingDirty);}}
 async function workspaceDispatch(name,el={dataset:{}}){
  const id=el.dataset.project||C().state.parts[1];
  if(name==='history-next'||name==='history-prev'){historyPages.set(id,Math.max(0,(historyPages.get(id)||0)+(name==='history-next'?1:-1)));C().render(true);return;}
  if(name==='setup')return openProjectSetup(el.dataset.project||null);
  if(name==='close')return closeSetup();
  if(name==='inspect'){const epoch=C().state.renderEpoch;try{await inspect(id,el.dataset.build);}catch(error){checks.set(checkKey(id,el.dataset.build),{at:Date.now(),result:{build_validation:{ready:false,reason:C().errorMessage(error)}}});}if(epoch===C().state.renderEpoch)C().render(true);return;}
  if(name==='review'){if(!await openProjectSetup(id))return;setup.agent_id=el.dataset.agent;setup.stage='review';setup.dirty=false;showSetup();return;}
  if(name==='composer-inspect'){const d=composer(id),a=profiles(id).find(a=>a.id===d.agent_id),epoch=C().state.renderEpoch;try{await inspect(id,a?.build_id,a?.id);}catch(error){d.error=C().errorMessage(error);}if(epoch===C().state.renderEpoch)C().render(true);return;}
  if(name==='reconcile-start')return start(id,true);
  if(name==='restore-start'){
   const d=composer(id),key=attempts.get(`start:${id}`),original=d.preparedRequest,epoch=C().state.renderEpoch;
   if(d.busy||!original||!key)return;
   const payload={agent_id:original.agent_id,project_id:id,primary_goal:original.primary_goal.trim(),transport:original.transport};
   if(original.request_id!==key.id||key.signature!==await payloadSignature(payload)){d.error='The returned original intent does not match this pending request. Reconcile it again before restoring.';C().render(true);return;}
   if(!await workspaceConfirm('Replace the current goal, configuration and transport with the prepared original intent? The changed draft stays untouched if you cancel. Restoring does not launch; a separate Start click retries the original request ID.','Restore original intent'))return;
   if(epoch!==C().state.renderEpoch||C().state.parts.join('/')!==`projects/${id}/new`)return;
   Object.assign(d,{agent_id:payload.agent_id,primary_goal:payload.primary_goal,transport:payload.transport,dirty:true,error:'',errorField:''});C().markDraftDirty(true);C().render(true);return;
  }
  if(name==='migration'){if(setup?.busy||migration?.busy)return;if(document.querySelector('#workspace-setup')&&!await closeSetup())return;underlyingDirty=Boolean(C().state.dirtyDraft);opener=document.activeElement;migration={mapping:{},newGroups:[],group_name:'',backup_dir:'',confirmed:false,preview:null,busy:false,dirty:false,error:''};showMigration();return;}
  if(name==='migration-add'){const m=migration;if(m.busy||!m.group_name.trim())return;m.newGroups.push({id:`project_${crypto.randomUUID()}`,name:m.group_name.trim()});m.group_name='';m.preview=null;m.dirty=true;showMigration();return;}
  if(name==='migration-preview')return migrationOperation(async m=>{m.preview=await C().api('/api/projects/migration/preview','POST',{groups:migrationGroups()});m.confirmed=false;});
  if(name==='migration-apply')return migrationOperation(async m=>{if(m.applied)return;if(!m.preview||!m.confirmed||list(m.preview.blockers).length)throw new Error('Review a valid preview and explicitly confirm backup first.');await C().api('/api/projects/migration/apply','POST',{groups:migrationGroups(),preview_token:m.preview.preview_token,backup_dir:m.backup_dir,confirmed:true});m.dirty=false;m.applied=true;try{await C().refresh();}catch{m.error='Applied. Refreshing the workspace failed; reload to see the new grouping. Do not apply again.';return;}document.querySelector('#dialog')?.close();C().render(true);});
  if(!setup||setup.busy)return;
  if(name==='pick')return setupOperation(s=>pick(s,el.dataset.kind||'manifest'));
  if(name==='register')return setupOperation(register);
  if(name==='reconcile-tune')return setupOperation(async s=>{const key=attempts.get(`tune:${s.project_id}`);if(!key)return;const result=await C().api(`/api/agents/tuning/${url(key.id)}`);if(result.status!=='completed'||!result.agent)throw new Error(`Tuning status: ${result.status}. No additional inference was started. Retain the fields and retry the same attempt if needed.`);remember('agents',result.agent);attempts.delete(`tune:${s.project_id}`);saveAttempts();s.agent_id=result.agent.id;s.build_id=result.agent.build_id;s.name=result.agent.name;s.model=result.agent.model;s.stage='review';s.dirty=false;});
  if(name==='setup-inspect')return setupOperation(s=>inspect(s.project_id,s.build_id));
  if(name==='prepare')return setupOperation(prepare);
  if(name==='back'){setup.stage={build:'project',inspect:'build',configure:'inspect'}[setup.stage]||'build';if(setup.project_id&&setup.stage==='project')setup.stage='build';showSetup();return;}
  if(name==='retune'){setup.agent_id='';setup.stage='configure';attempts.delete(`tune:${setup.project_id}`);saveAttempts();showSetup();return;}
  if(name==='finish'||name==='setup-new'){const p=setup.project_id;setup.dirty=false;await closeSetup();C().navigate(`projects/${p}${name==='setup-new'?'/new':''}`);}
 }
 function workspaceInput(el){
  const key=el.dataset.wField;if(!key)return;
  if(el.closest('#workspace-migration')){const m=migration;if(m.busy)return;if(key.startsWith('map:')){m.mapping[key.slice(4)]=el.value;m.preview=null;m.confirmed=false;}else m[key]=el.type==='checkbox'?el.checked:el.value;m.dirty=true;C().markDraftDirty(true);if(el.tagName==='SELECT'||key==='confirmed')showMigration();return;}
  const form=el.closest('[data-workspace]');if(!form)return;
  const d=form.dataset.workspace==='setup'?setup:composer(form.dataset.project);if(!d||d.busy)return;
  if(!['project_name','build_id','manifest_path','build_name','name','model','agent_id','primary_goal','transport'].includes(key))return;
  d[key]=el.value;d.dirty=true;d.error='';C().markDraftDirty(true);
  if(el.tagName==='SELECT'){if(form.dataset.workspace==='setup')showSetup();else C().render(true);}
 }
 async function workspaceSubmit(form){
  if(!form.reportValidity())return;
  if(form.dataset.workspace==='composer')return start(form.dataset.project);
  if(form.dataset.workspace!=='setup'||!setup)return;
  if(setup.stage==='project')return setupOperation(create);
  if(setup.stage==='build'){if(!builds(setup.project_id).some(b=>b.id===setup.build_id))return;setup.stage='inspect';showSetup();return;}
  if(setup.stage==='inspect'){if(setupReady(checks.get(checkKey(setup.project_id,setup.build_id))?.result)){setup.stage='configure';showSetup();}return;}
  if(setup.stage==='configure')return setupOperation(tune);
 }
 function initializeWorkspace(shared){
  if(shared)context=shared;if(!C()||bound)return;
  bound=true;
  try{const raw=globalThis.sessionStorage?.getItem(storageKey);if(raw&&raw.length<12000)for(const [scope,item] of JSON.parse(raw).slice(-24)){if(typeof scope==='string'&&scope.length<140&&/^[\w-]{1,100}$/.test(item.id)&&/^[a-f0-9]{64}$/.test(item.signature))attempts.set(scope,{id:item.id,signature:item.signature,tried:Boolean(item.tried)});}}catch{/* Corrupt/blocked session storage cannot prevent reading history. */}
  document.addEventListener('click',async event=>{const el=event.target.closest('[data-w-action]');if(!el||el.disabled)return;event.preventDefault();try{await workspaceDispatch(el.dataset.wAction,el);}catch(error){C().toast?.(C().errorMessage(error),true);}});
  document.addEventListener('input',event=>workspaceInput(event.target));
  document.addEventListener('change',event=>{if(event.target.tagName==='SELECT'||event.target.type==='checkbox')workspaceInput(event.target);});
  document.addEventListener('submit',async event=>{if(!event.target.dataset.workspace)return;event.preventDefault();await workspaceSubmit(event.target);});
  document.querySelector('#dialog')?.addEventListener('cancel',event=>{if(document.querySelector('#workspace-setup')||document.querySelector('#workspace-migration')){event.preventDefault();closeSetup();}});
 }
 Object.assign(globalThis,{initializeWorkspace,workspaceNewInstanceRoute,workspaceSidebarView,workspaceUpdateSidebar,projectOverviewView,unassignedHistoryView,projectComposerView,openProjectSetup,workspaceBeforeNavigate,workspaceDispatch,workspaceInput,workspaceSubmit});
 if(globalThis.ashadWorkspaceContext)initializeWorkspace(globalThis.ashadWorkspaceContext);
})();
