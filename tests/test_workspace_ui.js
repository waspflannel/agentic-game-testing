'use strict';
// Explicit synthetic DOM/API fixture. No controller, credentials, Docker, AI or game.
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const {test}=require('node:test');
const {webcrypto,randomUUID}=require('node:crypto');
const source=fs.readFileSync(path.join(__dirname,'../ashad/static/workspace.js'),'utf8');
const esc=value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const plain=value=>JSON.parse(JSON.stringify(value));
const ready={project_id:'p1',build_id:'b1',provider:{ready:true,status:'ready'},build_validation:{ready:true},environments:[{id:'desktop',ready:true,checks:{artifact_sha256:'matched'}}],slot:{available:true,owners:[],limit:1},ready:true};
function deferred(){let resolve;const promise=new Promise(done=>resolve=done);return {promise,resolve};}
function fixture(options={}){
 const listeners={},calls=[],storage=options.storage||new Map();
 const state={dataRevision:0,renderEpoch:0,route:'projects',parts:['projects','p1'],dirtyDraft:false,data:{projects:[{id:'p1',name:'Same name'},{id:'p2',name:'Same name'}],builds:[{id:'b1',name:'Build',project_id:'p1'},{id:'b2',name:'Build',project_id:'p2'}],agents:[{id:'a1',name:'Scout',build_id:'b1',project_id:'p1',model:'model-a',instructions:'Use <safe> controls',status:'ready'},{id:'a2',name:'Scout',build_id:'b2',project_id:'p2',model:'model-b',instructions:'Other',status:'ready'}],runs:[],model_options:[{id:'model-a',label:'Model A'},{id:'model-b',label:'Model B'}],ai:{model:'model-a',available_models:['model-a','model-b']},instance_slot:{available:true,owners:[],limit:1}},...options.state};
 let modalHTML='',mainHTML='',form=null,composerForm=null,confirmations=[],confirmationWaiters=[],renders=0,sidebarPatches=0;
 const active={isConnected:true,focus(){document.activeElement=this;}};
 const body={append(node){confirmations.push(node);confirmationWaiters.shift()?.(node);}};
 const dialog={open:false,dataset:{},querySelector(selector){return selector.includes('close-dialog')?{dataset:{},removeAttribute(){}}:null;},addEventListener(name,fn){listeners[`dialog:${name}`]=fn;},close(){this.open=false;modalHTML='';}};
 function makeForm(kind,id){return {dataset:{workspace:kind,project:id},reportValidity(){return true;},querySelector(selector){if(selector==='.dialog-body')return {kind:'dialog-body'};return null;}};}
 const navParent={kind:'nav',scrollTop:27},nav={parentElement:navParent};
 const document={activeElement:active,body,addEventListener(name,fn){listeners[name]=fn;},querySelector(selector){
  if(selector==='#dialog')return dialog;
  if(selector==='#modal-form')return form;
  if(selector==='#workspace-setup')return modalHTML.includes('id="workspace-setup"')?{parentElement:{kind:'dialog-body'}}:null;
  if(selector==='#workspace-migration')return modalHTML.includes('id="workspace-migration"')?{parentElement:{kind:'dialog-body'}}:null;
  if(selector==='#workspace-navigation')return nav;
  return null;
 },createElement(tag){assert.equal(tag,'dialog');const events={};const cancel={focus(){document.activeElement=cancel;}};return {innerHTML:'',setAttribute(){},addEventListener(name,fn){events[name]=fn;},querySelector(){return cancel;},showModal(){this.open=true;},close(){this.open=false;},remove(){this.removed=true;},answer(yes){events.click({target:{closest(){return {dataset:{answer:yes?'yes':'cancel'}};}}});},escape(){events.cancel({preventDefault(){}});}};}};
 const sandbox=vm.createContext({document,window:{ashadDesktop:{chooseBuild:async()=>options.pickerResult??null}},sessionStorage:{getItem:key=>storage.get(key),setItem:(key,value)=>storage.set(key,value)},crypto:{subtle:webcrypto.subtle,randomUUID},TextEncoder,Uint8Array,Date,console});
 const shared={state,arr:v=>Array.isArray(v)?v:[],esc,heading:(_a,b,c,actions='')=>`<h1>${esc(b)}</h1><p>${esc(c)}</p>${actions}`,section:(a,b,c)=>`<section><h2>${a}</h2><p>${b}</p>${c}</section>`,notice:(copy,type='')=>`<div class="notice ${type}">${copy}</div>`,errorMessage:error=>error.detail||error.message||String(error),toast(){},markDraftDirty(value){state.dirtyDraft=Boolean(value);},navigate(route){state.destination=route;},refresh:async()=>state.data,api:async (url,method='GET',payload)=>{calls.push({url,method,payload:plain(payload||{})});if(options.api)return options.api(url,method,payload);if(url.includes('/readiness'))return plain(ready);throw new Error(`Unexpected API: ${method} ${url}`);},modal(_name,html){modalHTML=html;dialog.open=true;form=makeForm('setup');},patchHTML(root,html){if(root.kind==='nav'){sidebarPatches++;return;}modalHTML=html;},render(){renders++;if(state.parts[2]==='new'){mainHTML=sandbox.projectComposerView(state.parts[1]);composerForm=makeForm('composer',state.parts[1]);}}};
 sandbox.ashadWorkspaceContext=shared;vm.runInContext(source,sandbox);
 function field(key,value,kind='setup'){
  const html=kind==='setup'?modalHTML:kind==='migration'?modalHTML:mainHTML;
  assert.ok(html.includes(`data-w-field="${key}"`),`Actual rendered field ${key} exists`);
  const select=html.includes(`<select data-w-field="${key}"`),node={dataset:{wField:key},value,tagName:select?'SELECT':'INPUT',type:key==='confirmed'?'checkbox':'text',checked:Boolean(value),closest(selector){if(selector==='#workspace-migration')return kind==='migration'?{}:null;return kind==='setup'?form:composerForm;}};
  sandbox.workspaceInput(node);return node;
 }
 return {app:sandbox,state,calls,storage,document,listeners,dialog,field,get html(){return modalHTML;},get main(){return mainHTML;},get form(){return form;},get composerForm(){return composerForm;},get confirmations(){return confirmations;},get renders(){return renders;},get sidebarPatches(){return sidebarPatches;},showComposer(id='p1'){state.route='projects';state.parts=['projects',id,'new'];shared.render();return composerForm;},nextConfirmation(){return confirmations.at(-1)?.open?Promise.resolve(confirmations.at(-1)):new Promise(done=>confirmationWaiters.push(done));},answer(yes){assert.ok(confirmations.length);confirmations.at(-1).answer(yes);},dispatch(name,dataset={}){return sandbox.workspaceDispatch(name,{dataset});}};
}
async function configure(f){await f.app.openProjectSetup('p1');await f.app.workspaceSubmit(f.form);await f.dispatch('setup-inspect');await f.app.workspaceSubmit(f.form);assert.match(f.html,/Tune and save named profile/);f.field('name','Named scout');}

test('all rendering is read-only, escaped, and project names never group IDs',()=>{
 const f=fixture();f.state.data.runs=[{id:'r1',build_id:'b1',state:'Running',title:'<script>'},{id:'r2',project_id:'p2',build_id:'b2',state:'Ended'},{id:'orphan',state:'Ended',plan:{build_id:'b1'}},{id:'bad',project_id:'p1',build_id:'b2',state:'Ended'}];
 const before=JSON.stringify(f.state);const sidebar=f.app.workspaceSidebarView();
 assert.match(sidebar,/project-p1/);assert.match(sidebar,/project-p2/);assert.match(sidebar,/&lt;script&gt;/);assert.match(sidebar,/#\/runs\/orphan/);assert.match(sidebar,/Unassigned history/);
 assert.ok(sidebar.indexOf('thread-r1')<sidebar.indexOf('project-p2'));
 const unassigned=sidebar.slice(sidebar.indexOf('data-key="unassigned"'));assert.match(unassigned,/thread-orphan/);assert.match(unassigned,/thread-bad/);
 f.app.projectOverviewView();f.app.projectOverviewView('p1');f.app.projectComposerView('p1');f.app.projectOverviewView('unassigned');
 assert.equal(f.calls.length,0);assert.equal(JSON.stringify(f.state),before);assert.equal(f.storage.size,0);
});
test('actual statuses preserve paused, ended and unconfirmed ownership',()=>{
 const f=fixture();f.state.data.runs=[{id:'paused',project_id:'p1',state:'Paused'},{id:'ended',project_id:'p1',state:'Ended'},{id:'unconfirmed',project_id:'p1',state:'Ended',players:{bot:{teardown_confirmed:false}}}];
 const html=f.app.workspaceSidebarView();assert.match(html,/Paused \/ inputs held/);assert.match(html,/Ended · ended/);assert.match(html,/Teardown unconfirmed/);
});
test('native picker cancellation is a no-op and selection is never registration',async()=>{
 const f=fixture();await f.app.openProjectSetup('p1');const before=f.html;await f.dispatch('pick',{kind:'folder'});assert.equal(f.calls.length,0);assert.equal(f.state.dirtyDraft,false);assert.equal(f.html,before);
 const g=fixture({pickerResult:'C:\\Supported\\manifest.json'});await g.app.openProjectSetup('p1');await g.dispatch('pick',{kind:'manifest'});assert.match(g.html,/C:\\Supported/);assert.equal(g.calls.length,0);assert.equal(g.state.dirtyDraft,true);
});
test('failed/repeated creation retains the exact ID, then creates independently',async()=>{
 let fail=true;const f=fixture({api:async(url,_method,payload)=>{assert.equal(url,'/api/projects');if(fail)throw new Error('Lost create response');return {id:randomUUID(),name:payload.name};}});
 await f.app.openProjectSetup();f.field('project_name','Duplicate');await f.app.workspaceSubmit(f.form);assert.match(f.html,/Lost create response/);assert.equal(f.state.dirtyDraft,true);
 fail=false;await f.app.workspaceSubmit(f.form);assert.equal(f.calls[0].payload.request_id,f.calls[1].payload.request_id);assert.match(f.html,/Continue to inspection/);
 await f.app.openProjectSetup();f.field('project_name','Duplicate');await f.app.workspaceSubmit(f.form);assert.notEqual(f.calls[2].payload.request_id,f.calls[1].payload.request_id);
});
test('registration retries stable identity and changed revision inputs allocate a new ID',async()=>{
 let fail=true;const f=fixture({api:async(url,_method,payload)=>{assert.equal(url,'/api/builds');if(fail)throw new Error('Validation failed');return {id:payload.id,name:payload.name,project_id:payload.project_id};}});
 await f.app.openProjectSetup('p1');f.field('manifest_path','C:\\build\\manifest.json');f.field('build_name','Revision A');await f.dispatch('register');await f.dispatch('register');assert.equal(f.calls[0].payload.id,f.calls[1].payload.id);assert.match(f.html,/Validation failed/);
 f.field('manifest_path','C:\\build\\new.json');fail=false;await f.dispatch('register');assert.notEqual(f.calls[2].payload.id,f.calls[1].payload.id);assert.equal(f.calls[2].payload.project_id,'p1');assert.match(f.html,/Inspect compatibility/);
});
test('existing stages reuse builds without automatic inspection, tuning or start',async()=>{
 const f=fixture();await f.app.openProjectSetup('p1');assert.match(f.html,/b1/);assert.doesNotMatch(f.html,/b2/);assert.equal(f.calls.length,0);await f.app.workspaceSubmit(f.form);assert.equal(f.calls.length,0);assert.match(f.html,/Not inspected/);
});
test('compatible desktop is required; registration and enabled metadata are insufficient',async()=>{
 const f=fixture({api:async()=>({...ready,environments:[{id:'unknown',ready:false,reason:'Artifact hash absent'}],ready:false})});await f.app.openProjectSetup('p1');await f.app.workspaceSubmit(f.form);await f.dispatch('setup-inspect');await f.app.workspaceSubmit(f.form);assert.match(f.html,/Artifact hash absent/);assert.doesNotMatch(f.html,/Tune and save named profile/);assert.ok(f.calls.every(c=>c.method==='GET'));
});
test('preparation requires Cancel-default explicit confirmation and stable retry key',async()=>{
 const f=fixture({api:async(url)=>{if(url.includes('/prepare'))throw new Error('Docker Desktop is unavailable; install prerequisites');return ready;}});await f.app.openProjectSetup('p1');await f.app.workspaceSubmit(f.form);
 let pending=f.dispatch('prepare');assert.match(f.confirmations.at(-1).innerHTML,/autofocus/);assert.match(f.confirmations.at(-1).innerHTML,/read-only/);assert.equal(f.calls.length,0);f.answer(false);await pending;assert.equal(f.calls.length,0);
 pending=f.dispatch('prepare');f.answer(true);await pending;assert.match(f.html,/Docker Desktop is unavailable/);pending=f.dispatch('prepare');f.answer(true);await pending;assert.equal(f.calls[0].payload.request_id,f.calls[1].payload.request_id);assert.equal(f.calls[0].payload.confirmed,true);assert.ok(!f.calls.some(c=>c.url==='/api/instances'));
});
test('tuning is explicit, lost response reconciles saved profile before repeat inference',async()=>{
 let tuned=false;const f=fixture({api:async(url,method,payload)=>{if(url.includes('/readiness'))return ready;if(url==='/api/agents/tune'){tuned=true;throw new Error('Lost tuning response');}if(url.includes('/tuning/'))return {status:'completed',agent:{id:'saved',project_id:'p1',build_id:'b1',model:'model-a',name:'Named scout',instructions:'Saved <guide>'}};throw new Error(url);}});
 await configure(f);assert.equal(tuned,false);await f.app.workspaceSubmit(f.form);assert.match(f.html,/Lost tuning response/);assert.equal(f.state.dirtyDraft,true);await f.app.workspaceSubmit(f.form);assert.equal(f.calls.filter(c=>c.url==='/api/agents/tune').length,1);assert.match(f.html,/Saved &lt;guide&gt;/);assert.equal(f.state.data.agents.find(a=>a.id==='saved').name,'Named scout');assert.equal(f.state.dirtyDraft,false);assert.equal(f.calls.some(c=>c.url==='/api/instances'),false);
});
test('failed/in-progress tuning retains key, changed inputs and explicit retune get new IDs',async()=>{
 let progress=false;const f=fixture({api:async(url,method,payload)=>{if(url.includes('/readiness'))return ready;if(url.includes('/tuning/'))return {status:progress?'in_progress':'unknown'};if(url==='/api/agents/tune')throw new Error('Recoverable model error');throw new Error(url);}});
 await configure(f);await f.app.workspaceSubmit(f.form);await f.app.workspaceSubmit(f.form);let posts=f.calls.filter(c=>c.url==='/api/agents/tune');assert.equal(posts[0].payload.request_id,posts[1].payload.request_id);
 progress=true;await f.app.workspaceSubmit(f.form);assert.equal(f.calls.filter(c=>c.url==='/api/agents/tune').length,2);assert.match(f.html,/still in progress/);
 f.field('name','Second profile');progress=false;await f.app.workspaceSubmit(f.form);posts=f.calls.filter(c=>c.url==='/api/agents/tune');assert.notEqual(posts[2].payload.request_id,posts[1].payload.request_id);
 await f.dispatch('retune');await f.app.workspaceSubmit(f.form);posts=f.calls.filter(c=>c.url==='/api/agents/tune');assert.notEqual(posts[3].payload.request_id,posts[2].payload.request_id);
});
test('saved profile review and finishing never tune or launch; multiple names remain independent',async()=>{
 const f=fixture();f.state.data.agents.push({id:'a3',project_id:'p1',build_id:'b1',name:'Builder',model:'model-b',instructions:'Build'});await f.dispatch('review',{project:'p1',agent:'a1'});assert.match(f.html,/Use &lt;safe&gt; controls/);await f.dispatch('finish');assert.equal(f.calls.length,0);assert.equal(f.state.destination,'projects/p1');assert.match(f.app.projectOverviewView('p1'),/Builder/);assert.doesNotMatch(f.app.projectComposerView('p1'),/a2/);
});
test('signed-out/model-unavailable failures preserve named configuration with settings recovery',async()=>{
 const f=fixture({api:async()=>({...ready,provider:{ready:false,status:'signed_out',message:'Sign in explicitly'},ready:false})});await f.app.openProjectSetup('p1');await f.app.workspaceSubmit(f.form);await f.dispatch('setup-inspect');assert.match(f.html,/Sign in explicitly/);assert.match(f.html,/#\/settings\/ai/);assert.ok(f.calls.every(c=>c.method==='GET'));
});
test('setup Escape and navigation honor Cancel default and retain saved stages',async()=>{
 const f=fixture();await f.app.openProjectSetup();f.field('project_name','Unsaved');let prevented=false;f.listeners['dialog:cancel']({preventDefault(){prevented=true;}});f.answer(false);await new Promise(setImmediate);assert.equal(prevented,true);assert.equal(f.dialog.open,true);assert.equal(f.state.dirtyDraft,true);
 const pending=f.app.workspaceBeforeNavigate('settings/ai');f.answer(true);assert.equal(await pending,true);assert.equal(f.dialog.open,false);assert.equal(f.calls.length,0);
});
test('blocked global slot preserves typed draft with owning-thread navigation and no autostart',async()=>{
 const f=fixture();f.showComposer();f.field('primary_goal','Keep this objective','composer');f.state.data.instance_slot={available:false,owners:[{run_id:'owner'}],limit:1};await f.app.workspaceSubmit(f.composerForm);assert.match(f.main,/Keep this objective/);assert.match(f.main,/#\/runs\/owner/);assert.equal(f.calls.length,0);assert.equal(f.state.dirtyDraft,true);f.state.data.instance_slot.available=true;f.app.projectComposerView('p1');assert.equal(f.calls.length,0);
});
test('409 owning instance conflict retains goal, transport and attempted ID',async()=>{
 const f=fixture({api:async(url)=>{if(url.includes('/readiness'))return ready;if(url.includes('/requests/'))return {status:'unknown'};const error=new Error('Busy');error.code='active_instance_conflict';error.owning_instances=[{run_id:'r-owner'}];throw error;}});
 f.showComposer();f.field('primary_goal','Goal survives','composer');await f.app.workspaceSubmit(f.composerForm);assert.match(f.main,/Goal survives/);assert.match(f.main,/r-owner/);assert.equal(f.state.dirtyDraft,true);assert.equal(f.state.data.instance_slot.available,false);assert.match(f.main,/Reconcile pending start/);
});
test('lost starts reconcile accepted run before retry; goal changes get a new attempt',async()=>{
 let accepted=false;const f=fixture({api:async(url)=>{if(url.includes('/readiness'))return ready;if(url.includes('/requests/'))return accepted?{status:'accepted',run:{id:'actual-run',project_id:'p1',state:'Starting'}}:{status:'unknown'};throw new Error('Lost start response');}});
 f.showComposer();f.field('primary_goal','First goal','composer');await f.app.workspaceSubmit(f.composerForm);const first=f.calls.find(c=>c.url==='/api/instances').payload.request_id;f.field('primary_goal','Changed goal','composer');await f.app.workspaceSubmit(f.composerForm);const posts=f.calls.filter(c=>c.url==='/api/instances');assert.notEqual(first,posts[1].payload.request_id);accepted=true;await f.app.workspaceSubmit(f.composerForm);assert.equal(f.calls.filter(c=>c.url==='/api/instances').length,2);assert.equal(f.state.destination,'runs/actual-run');
});
test('changed lost-start intent reconciles old key before unknown permits a new ID',async()=>{
 const f=fixture({api:async(url)=>{if(url.includes('/readiness'))return ready;if(url.includes('/requests/'))return {status:'unknown'};throw new Error('Lost response');}});f.showComposer();f.field('primary_goal','Original intent','composer');await f.app.workspaceSubmit(f.composerForm);const first=f.calls.find(c=>c.url==='/api/instances').payload.request_id,checkpoint=f.calls.length;f.field('primary_goal','Changed intent','composer');await f.app.workspaceSubmit(f.composerForm);assert.equal(f.calls[checkpoint].url,`/api/instances/requests/${first}`);assert.equal(f.calls[checkpoint].method,'GET');assert.notEqual(f.calls.filter(c=>c.url==='/api/instances')[1].payload.request_id,first);
});
test('prepared and in-progress lost starts retain old key and block changed POSTs',async()=>{
 for(const status of ['prepared','in_progress']){
  const f=fixture({api:async(url)=>{if(url.includes('/readiness'))return ready;if(url.includes('/requests/'))return {status};throw new Error('Lost response');}});f.showComposer();f.field('primary_goal','Original intent','composer');await f.app.workspaceSubmit(f.composerForm);const first=f.calls.find(c=>c.url==='/api/instances').payload.request_id;f.field('primary_goal','Changed intent','composer');await f.app.workspaceSubmit(f.composerForm);assert.equal(f.calls.filter(c=>c.url==='/api/instances').length,1);assert.match(f.main,/Changed intent/);assert.match(f.main,/original request ID is retained/);assert.equal(f.state.dirtyDraft,true);const saved=JSON.parse([...f.storage.values()][0]).find(([scope])=>scope==='start:p1');assert.equal(saved[1].id,first);await f.dispatch('reconcile-start',{project:'p1'});assert.equal(f.calls.at(-1).url,`/api/instances/requests/${first}`);assert.equal(f.calls.filter(c=>c.url==='/api/instances').length,1);
 }
});
test('same intent never repeats an in-progress start and prepared retries keep original ID',async()=>{
 for(const status of ['prepared','in_progress']){
  const f=fixture({api:async(url)=>{if(url.includes('/readiness'))return ready;if(url.includes('/requests/'))return {status};throw new Error('Lost response');}});f.showComposer();f.field('primary_goal','Original intent','composer');await f.app.workspaceSubmit(f.composerForm);await f.app.workspaceSubmit(f.composerForm);const posts=f.calls.filter(c=>c.url==='/api/instances');assert.equal(posts.length,status==='prepared'?2:1);if(status==='prepared')assert.equal(posts[0].payload.request_id,posts[1].payload.request_id);
 }
});
test('accepted earlier start preserves changed goal with review link and no same-click POST',async()=>{
 for(const reconcileOnly of [false,true]){
  const f=fixture({api:async(url)=>{if(url.includes('/readiness'))return ready;if(url.includes('/requests/'))return {status:'accepted',run:{id:'prior-run',project_id:'p1',state:'Starting'}};throw new Error('Lost response');}});f.showComposer();f.field('primary_goal','Original intent','composer');await f.app.workspaceSubmit(f.composerForm);const first=f.calls.find(c=>c.url==='/api/instances').payload.request_id;f.field('primary_goal','Changed goal stays','composer');if(reconcileOnly)await f.dispatch('reconcile-start',{project:'p1'});else await f.app.workspaceSubmit(f.composerForm);assert.equal(f.calls.filter(c=>c.url==='/api/instances').length,1);assert.match(f.main,/Changed goal stays/);assert.match(f.main,/#\/runs\/prior-run/);assert.match(f.main,/another explicit click/);assert.equal(f.state.destination,undefined);assert.equal(f.state.dirtyDraft,true);assert.equal(f.state.data.runs[0].id,'prior-run');await f.app.workspaceSubmit(f.composerForm);const posts=f.calls.filter(c=>c.url==='/api/instances');assert.equal(posts.length,2);assert.notEqual(posts[1].payload.request_id,first);assert.equal(posts[1].payload.primary_goal,'Changed goal stays');
 }
});
test('prepared lookup exposes escaped original intent and confirmed restore never auto-retries',async()=>{
 let original,posts=0;const f=fixture({api:async(url,method,payload)=>{if(url.includes('/readiness'))return ready;if(url.includes('/requests/'))return {status:'prepared',request:{...original,untrusted_extra:'NEVER-COPY-EXTRA'}};original=plain(payload);if(++posts===1)throw new Error('Lost prepared response');return {id:'retried-original',state:'Starting',project_id:'p1'};}});f.showComposer();f.field('primary_goal','Original <script> intent','composer');await f.app.workspaceSubmit(f.composerForm);const key=original.request_id;f.field('primary_goal','Changed draft stays until confirmation','composer');await f.app.workspaceSubmit(f.composerForm);assert.match(f.main,/Prepared original launch intent/);assert.match(f.main,/Original &lt;script&gt; intent/);assert.doesNotMatch(f.main,/NEVER-COPY-EXTRA/);
 let pending=f.dispatch('restore-start',{project:'p1'});const cancelDialog=await f.nextConfirmation();assert.match(cancelDialog.innerHTML,/autofocus/);f.answer(false);await pending;assert.match(f.app.projectComposerView('p1'),/Changed draft stays until confirmation/);assert.equal(posts,1);
 pending=f.dispatch('restore-start',{project:'p1'});await f.nextConfirmation();f.answer(true);await pending;assert.match(f.main,/<textarea[^>]*>Original &lt;script&gt; intent<\/textarea>/);assert.equal(posts,1);assert.equal(f.state.dirtyDraft,true);assert.equal(f.calls.at(-1).method,'GET');await f.app.workspaceSubmit(f.composerForm);assert.equal(posts,2);assert.equal(original.request_id,key);assert.equal(original.primary_goal,'Original <script> intent');assert.equal(f.state.destination,'runs/retried-original');
});
test('renderer reload prepared recovery restores backend original fields without secret persistence',async()=>{
 const storage=new Map();let original;const f=fixture({storage,api:async(url,method,payload)=>{if(url.includes('/readiness'))return ready;original=plain(payload);throw new Error('Lost prepared response');}});f.showComposer();f.field('primary_goal','Reload original goal','composer');await f.app.workspaceSubmit(f.composerForm);const g=fixture({storage,api:async()=>({status:'prepared',request:original})});g.showComposer();await g.dispatch('reconcile-start',{project:'p1'});assert.match(g.main,/Prepared original launch intent/);const pending=g.dispatch('restore-start',{project:'p1'});await g.nextConfirmation();g.answer(true);await pending;assert.match(g.main,/<textarea[^>]*>Reload original goal<\/textarea>/);assert.ok(g.calls.every(c=>c.method==='GET'));assert.doesNotMatch([...storage.values()].join(''),/Reload original goal/);assert.equal(g.state.dirtyDraft,true);
});
test('prepared restore refuses a returned payload that does not match the pending signature',async()=>{
 let original;const f=fixture({api:async(url,method,payload)=>{if(url.includes('/readiness'))return ready;if(url.includes('/requests/'))return {status:'prepared',request:{...original,primary_goal:'Mismatched backend payload'}};original=plain(payload);throw new Error('Lost response');}});f.showComposer();f.field('primary_goal','Original intent','composer');await f.app.workspaceSubmit(f.composerForm);f.field('primary_goal','Current changed draft','composer');await f.dispatch('reconcile-start',{project:'p1'});await f.dispatch('restore-start',{project:'p1'});assert.match(f.main,/does not match this pending request/);assert.match(f.main,/Current changed draft/);assert.equal(f.calls.filter(c=>c.method==='POST').length,1);assert.equal(f.confirmations.length,0);
});
test('Steam never trusts stale account rows, App ID 480, or ENet fallback',async()=>{
 const f=fixture();f.state.data.accounts=[{readiness:'ready',steam_id:'stale',app_id:480}];f.showComposer();f.field('primary_goal','Steam goal','composer');f.field('transport','steam','composer');await f.app.workspaceSubmit(f.composerForm);assert.match(f.main,/no ENet fallback/);assert.ok(!f.calls.some(c=>c.url==='/api/instances'));
 const g=fixture({api:async(url,method,payload)=>{if(url.includes('/readiness'))return {...ready,environments:[{ready:true,steam:{ready:true,checks:{licensed:true,matching_app_id:true}}}]};if(url==='/api/instances')return {id:'steam-run',state:'Starting',project_id:'p1'};throw new Error(url);}});g.showComposer();g.field('primary_goal','Steam goal','composer');g.field('transport','steam','composer');await g.app.workspaceSubmit(g.composerForm);assert.equal(g.calls.find(c=>c.url==='/api/instances').payload.transport,'steam');
});
test('late start results save retained identity but cannot navigate another route',async()=>{
 const response=deferred(),requested=deferred();const f=fixture({api:async(url)=>{if(url.includes('/readiness'))return ready;requested.resolve();return response.promise;}});f.showComposer();f.field('primary_goal','Old draft','composer');const pending=f.app.workspaceSubmit(f.composerForm);await requested.promise;f.state.renderEpoch++;f.state.parts=['projects','p2'];f.state.route='projects';const renders=f.renders;response.resolve({id:'late',project_id:'p1',state:'Starting'});await pending;assert.equal(f.state.destination,undefined);assert.equal(f.renders,renders);assert.equal(f.state.data.runs[0].id,'late');
});
test('late picker and tuning responses cannot overwrite a replacement dialog',async()=>{
 const response=deferred(),requested=deferred();const f=fixture({api:async(url)=>{if(url.includes('/readiness'))return ready;if(url==='/api/agents/tune'){requested.resolve();return response.promise;}throw new Error(url);}});await configure(f);const pending=f.app.workspaceSubmit(f.form);await requested.promise;f.dialog.close();response.resolve({id:'late-profile',project_id:'p1',build_id:'b1',name:'Saved',instructions:'Late'});await pending;assert.equal(f.dialog.open,false);assert.equal(f.html,'');assert.equal(f.state.data.agents[0].id,'late-profile');
});
test('polling delegates keyed sidebar patch only, retaining focus/draft/disclosure container',()=>{
 const f=fixture();f.showComposer();f.field('primary_goal','Focused draft','composer');const focus=f.document.activeElement,renders=f.renders;f.state.data.runs.push({id:'new-history',project_id:'p2',state:'Ended'});f.app.workspaceUpdateSidebar();assert.equal(f.sidebarPatches,1);assert.equal(f.renders,renders);assert.equal(f.document.activeElement,focus);assert.match(f.app.projectComposerView('p1'),/Focused draft/);
});
test('pending reload metadata contains only bounded IDs/hash, never goal/path/password/credentials',async()=>{
 const storage=new Map();const f=fixture({storage,api:async(url)=>{if(url.includes('/readiness'))return ready;throw new Error('lost');}});f.showComposer();f.field('primary_goal','DO-NOT-PERSIST-THIS-OBJECTIVE','composer');await f.app.workspaceSubmit(f.composerForm);const encoded=[...storage.values()].join('');assert.doesNotMatch(encoded,/DO-NOT-PERSIST|primary_goal|password|token|manifest_path|model-a/);assert.ok(encoded.length<12000);
 const g=fixture({storage,api:async()=>({status:'accepted',run:{id:'reloaded',project_id:'p1',state:'Starting'}})});g.showComposer();assert.match(g.main,/Reconcile pending start/);await g.dispatch('reconcile-start',{project:'p1'});assert.equal(g.state.destination,'runs/reloaded');assert.ok(g.calls.every(c=>c.method==='GET'));
});
test('membership migration previews exact selectable IDs before confirmed backup/apply',async()=>{
 const f=fixture({api:async(url,method,payload)=>{if(url.endsWith('/preview'))return {preview_token:'reviewed',blockers:[],assignments:{builds:{b1:'p2'}},backup_scope:'metadata only'};if(url.endsWith('/apply'))return {applied:true};throw new Error(url);}});await f.dispatch('migration');assert.match(f.html,/Keep unassigned/);f.field('map:b1','p2','migration');await f.dispatch('migration-preview');assert.deepEqual(f.calls[0].payload.groups,[{project_id:'p2',name:'Same name',build_ids:['b1']}]);await f.dispatch('migration-apply');assert.equal(f.calls.length,1);f.field('backup_dir','C:\\SyntheticBackup','migration');f.field('confirmed',true,'migration');await f.dispatch('migration-apply');assert.equal(f.calls[1].payload.preview_token,'reviewed');assert.equal(f.calls[1].payload.confirmed,true);assert.equal(f.calls[1].payload.backup_dir,'C:\\SyntheticBackup');
});
test('late native picker results do not dirty or reopen a closed setup',async()=>{
 const response=deferred();const f=fixture({pickerResult:response.promise});await f.app.openProjectSetup('p1');const pending=f.dispatch('pick',{kind:'folder'});f.dialog.close();response.resolve('C:\\late\\manifest.json');await pending;assert.equal(f.dialog.open,false);assert.equal(f.html,'');assert.equal(f.state.dirtyDraft,false);assert.equal(f.calls.length,0);
});
test('field-local errors identify invalid configuration and manifest fields without losing drafts',async()=>{
 const f=fixture({api:async()=>{throw new Error('Pinned identity validation failed');}});await f.app.openProjectSetup('p1');f.field('manifest_path','C:\\revision\\manifest.json');await f.dispatch('register');assert.match(f.html,/aria-invalid="true" aria-describedby="workspace-manifest_path-error"/);assert.match(f.html,/C:\\revision/);
 const g=fixture({api:async(url)=>{if(url.includes('/readiness'))return ready;if(url==='/api/agents/tune')throw new Error('Model unavailable for this account');throw new Error(url);}});await configure(g);await g.app.workspaceSubmit(g.form);assert.match(g.html,/aria-describedby="workspace-model-error"/);assert.match(g.html,/Named scout/);
});
test('sidebar history is bounded, selected old threads remain visible, overview pages retain every ID',async()=>{
 const f=fixture();f.state.data.runs=Array.from({length:145},(_,index)=>({id:`history-${index}`,project_id:'p1',state:'Ended'}));f.state.route='runs';f.state.parts=['runs','history-144'];const sidebar=f.app.workspaceSidebarView();assert.equal((sidebar.match(/class="workspace-thread"/g)||[]).length,31);assert.match(sidebar,/#\/runs\/history-144/);assert.match(sidebar,/View all 145 instances/);
 const first=f.app.projectOverviewView('p1');assert.equal((first.match(/class="workspace-thread"/g)||[]).length,60);assert.doesNotMatch(first,/thread-history-60/);await f.dispatch('history-next',{project:'p1'});assert.match(f.app.projectOverviewView('p1'),/thread-history-60/);await f.dispatch('history-next',{project:'p1'});assert.match(f.app.projectOverviewView('p1'),/thread-history-144/);
});
test('real project ID unassigned does not collide with orphan-history route',()=>{
 const f=fixture();f.state.data.projects.push({id:'unassigned',name:'Real imported project'});f.state.data.runs=[{id:'mapped',project_id:'unassigned',state:'Ended'},{id:'orphan',state:'Ended'}];assert.match(f.app.workspaceSidebarView(),/#\/history\/unassigned/);assert.match(f.app.projectOverviewView('unassigned'),/Real imported project/);assert.doesNotMatch(f.app.projectOverviewView('unassigned'),/thread-orphan/);assert.match(f.app.unassignedHistoryView(),/thread-orphan/);assert.doesNotMatch(f.app.unassignedHistoryView(),/thread-mapped/);
});
test('closing setup restores underlying settings draft protection',async()=>{
 const f=fixture();f.state.route='settings';f.state.parts=['settings','ai'];f.state.dirtyDraft=true;await f.app.openProjectSetup('p1');await f.dispatch('close');assert.equal(f.state.dirtyDraft,true);assert.equal(f.dialog.open,false);
});
test('renderer reload can reconcile pending tuning without persisting its input fields',async()=>{
 const storage=new Map();const f=fixture({storage,api:async(url)=>{if(url.includes('/readiness'))return ready;throw new Error('Lost response');}});await configure(f);await f.app.workspaceSubmit(f.form);const g=fixture({storage,api:async()=>({status:'completed',agent:{id:'saved-after-reload',project_id:'p1',build_id:'b1',name:'Recovered',model:'model-a',instructions:'Recovered guide'}})});await g.app.openProjectSetup('p1');assert.match(g.html,/Reconcile saved\/pending tuning/);await g.dispatch('reconcile-tune');assert.match(g.html,/Recovered guide/);assert.equal(g.calls.length,1);assert.equal(g.calls[0].method,'GET');assert.equal(g.state.data.agents[0].id,'saved-after-reload');
});
test('changing migration grouping invalidates reviewed preview, ambiguous records stay unassigned',async()=>{
 const f=fixture({api:async()=>({preview_token:'old',blockers:[],unassigned:{runs:[{id:'orphan'}]}})});await f.dispatch('migration');f.field('map:b1','p1','migration');await f.dispatch('migration-preview');assert.match(f.html,/orphan/);f.field('map:b1','','migration');assert.doesNotMatch(f.html,/Reviewed preview/);await f.dispatch('migration-apply');assert.equal(f.calls.length,1);
});
