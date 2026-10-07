'use strict';
// Actual renderer route/refresh/poll functions + actual workspace extension; no native/browser acceptance claim.
const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const renderer=fs.readFileSync(path.join(__dirname,'../ashad/static/app.js'),'utf8');
const extension=fs.readFileSync(path.join(__dirname,'../ashad/static/workspace.js'),'utf8');
const realEsc=renderer.split('\n').find(line=>line.startsWith('const esc ='));
function deferred(){let resolve;const promise=new Promise(done=>resolve=done);return {promise,resolve};}
function harness(){
 const dialog={open:false,close(){this.open=false;}},listeners={},calls=[];
 const state={data:{projects:[{id:'project',name:'Game'}],builds:[],agents:[],runs:[],instance_slot:{available:true}},parts:[],route:'projects',renderEpoch:0,dataRevision:0};
 const history={state:null,replaceState(entry,_title,url){this.state=entry;calls.push(['replace',url]);},back(){calls.push(['back']);},go(delta){calls.push(['go',delta]);}};
 const context=vm.createContext({state,location:{hash:''},history,
  document:{title:'',hidden:false,body:{classList:{remove(){}}},querySelector:()=>null,addEventListener(name,fn){listeners[name]=fn;}},
  window:{scrollTo(){},confirm:()=>false},$:(selector)=>selector==='#dialog'?dialog:null,
  arr:value=>Array.isArray(value)?value:[],title:String,
  heading:(...args)=>args.join(' '),section:(...args)=>args.join(' '),notice:String,modal(){},
  errorMessage:String,toast(){},markDraftDirty(value){state.dirtyDraft=value;},
  allowDraftNavigation(){calls.push(['legacy-guard']);return true;},navigate(){},
  api:async()=>state.data,loadRun:async()=>{calls.push(['load-run']);state.run={plan:{title:'Retained instance'}};},loadEvents:async()=>{},loadUsage:async()=>{},refreshCodexStatus:async()=>{},
  patchHTML(){},updateStatusUI(){},shell(content,background){calls.push(['shell',content,background]);},
  runView:()=> 'Retained run',settingsView:()=>'',usageView:()=>'',
 });
 context.esc=vm.runInContext(realEsc.replace('const esc = ',''),context);
 vm.runInContext(renderer.slice(renderer.indexOf('async function refresh()'),renderer.indexOf('function heading(')),context);
 vm.runInContext(renderer.slice(renderer.indexOf('function render(background='),renderer.indexOf("document.addEventListener('click'")),context);
 vm.runInContext(renderer.slice(renderer.indexOf('async function poll()'),renderer.indexOf('setInterval(poll,')),context);
 context.ashadWorkspaceContext={state,api:context.api,esc:context.esc,arr:context.arr,heading:context.heading,section:context.section,notice:context.notice,modal:context.modal,markDraftDirty:context.markDraftDirty,navigate:context.navigate,refresh:context.refresh,render:context.render,patchHTML:context.patchHTML,errorMessage:context.errorMessage,toast:context.toast};
 vm.runInContext(extension,context);
 return {context,state,calls,listeners,dialog,history};
}
// Loads the complete production renderer and extension in index.html order, then fires DOMContentLoaded.
function boot(data,hash=''){
 const listeners={},windowListeners={},stub=()=>({innerHTML:'',open:false,addEventListener(){},setAttribute(){},querySelector:()=>null,classList:{remove(){},toggle(){},contains:()=>false}});
 const app=stub(),dialog=stub(),replaced=[];
 const document={hidden:false,title:'',body:stub(),addEventListener(name,fn){listeners[name]=fn;},querySelector:selector=>selector==='#app'?app:selector==='#dialog'?dialog:null,querySelectorAll:()=>[]};
 const context=vm.createContext({document,console,setTimeout,setInterval(){},
  location:{hash,origin:'http://127.0.0.1'},
  window:{addEventListener(name,fn){windowListeners[name]=fn;},scrollTo(){},matchMedia:()=>({matches:false})},
  history:{state:null,replaceState(entry,_title,url){this.state=entry;context.location.hash=url;replaced.push(url);},back(){},go(){}},
  fetch:async url=>({ok:true,json:async()=>structuredClone(data)})});
 vm.runInContext(renderer,context);vm.runInContext(extension,context);
 return {context,app,listeners,replaced,async load(){await listeners.DOMContentLoaded();},async go(next){context.location.hash=next;await windowListeners.hashchange();}};
}
const projectData=()=>({projects:[{id:'p',name:'Game'}],builds:[{id:'b',project_id:'p'}],agents:[{id:'a1',name:'First',build_id:'b',project_id:'p',model:'m',status:'ready'},{id:'a2',name:'Chosen',build_id:'b',project_id:'p',model:'m',status:'ready'},{id:'orphan-agent',name:'Orphan',build_id:'unregistered',model:'m',status:'ready'}],runs:[],instance_slot:{available:true},ai:{},settings:{}});
test('production renderer boots after the deferred extension and renders workspace navigation',async()=>{
 const index=fs.readFileSync(path.join(__dirname,'../ashad/static/index.html'),'utf8');
 assert.ok(index.indexOf('app.js')<index.indexOf('workspace.js'));
 assert.match(index,/workspace\.css/);
 const b=boot(projectData());assert.equal(b.app.innerHTML,'','renderer waits for DOMContentLoaded');
 await b.load();
 assert.match(b.app.innerHTML,/id="workspace-navigation"/);assert.match(b.app.innerHTML,/Game projects/);assert.doesNotMatch(b.app.innerHTML,/nav-group/);
});
test('legacy new-instance links open the project composer with that agent; unassigned agents get grouping guidance',async()=>{
 const b=boot(projectData());await b.load();
 await b.go('#/new/a2');
 assert.equal(b.replaced.at(-1),'#/projects/p/new');assert.match(b.app.innerHTML,/<option value="a2" selected/);assert.doesNotMatch(b.app.innerHTML,/id="instance-form"/);
 await b.go('#/new/orphan-agent');
 assert.equal(b.replaced.at(-1),'#/history/unassigned');assert.match(b.app.innerHTML,/Review project grouping/);
 for(const legacy of ['#/sessions','#/agents','#/agents/a1','#/new']){await b.go(legacy);assert.match(b.app.innerHTML,/Game projects/,legacy);assert.doesNotMatch(b.app.innerHTML,/Tune agent|Session control/);}
});
test('default project, real unassigned project and independent unassigned history routes use extension',async()=>{
 const {context,state,calls}=harness();
 await context.route();assert.equal(state.route,'projects');assert.match(calls.at(-1)[1],/Game projects/);
 state.data.projects.push({id:'unassigned',name:'Real project'});
 context.location.hash='#/projects/unassigned';await context.route();assert.match(calls.at(-1)[1],/Real project/);
 context.location.hash='#/history/unassigned';await context.route();assert.match(calls.at(-1)[1],/Unassigned history/);
});
test('workspace navigation guard runs before legacy guard and dialog close; retained run path unchanged',async()=>{
 const {context,state,calls,dialog}=harness();
 await context.route();state.parts=['projects','project'];context.location.hash='#/usage';dialog.open=true;context.history.state=null;
 const before=calls.length;context.workspaceBeforeNavigate=async()=>false;await context.route();
 assert.equal(dialog.open,true);assert.equal(state.parts.join('/'),'projects/project');
 assert.ok(!calls.slice(before).some(call=>call[0]==='legacy-guard'));
 context.workspaceBeforeNavigate=async()=>true;context.location.hash='#/runs/retained';await context.route();
 assert.equal(dialog.open,false);assert.equal(state.route,'runs');assert.equal(state.parts[1],'retained');
 assert.ok(calls.some(call=>call[0]==='load-run'));assert.match(calls.at(-1)[1],/Retained run/);
});
test('a blocked hashchange steps back instead of duplicating the entry, and pending prompts never stack',async()=>{
 const {context,state,calls}=harness();
 context.location.hash='#/projects/project';await context.route();
 const routed=context.history.state.ashadIndex;
 const prompt=deferred();let prompts=0;context.workspaceBeforeNavigate=()=>{prompts++;return prompt.promise;};
 const before=calls.length;context.history.state=null;context.location.hash='#/usage';const pending=context.route();
 context.location.hash='#/settings';await context.route();
 assert.equal(prompts,1,'second hashchange does not open a second confirmation');
 prompt.resolve(false);await pending;
 assert.deepEqual(calls.filter(call=>['back','go'].includes(call[0])),[['go',-2]],'both pushed entries are undone');
 assert.ok(!calls.slice(before).some(call=>call[0]==='replace'),'no duplicate entry is written');
 context.history.state={ashadIndex:routed};context.location.hash='#/projects/project';const shells=calls.filter(call=>call[0]==='shell').length;await context.route();
 assert.equal(calls.filter(call=>call[0]==='shell').length,shells,'arriving back at the current entry does not re-render');
 assert.equal(state.parts.join('/'),'projects/project');
 context.workspaceBeforeNavigate=async()=>false;context.history.state={ashadIndex:routed-1};context.location.hash='#/history/unassigned';await context.route();
 assert.deepEqual(calls.at(-1),['go',1],'blocked Back returns forward to the current entry');
});
test('project polling builds the sidebar once through render; non-rendering polls patch only the sidebar',async()=>{
 const {context,state,calls}=harness();let sidebar=0;
 state.route='projects';state.parts=['projects','project'];context.workspaceUpdateSidebar=()=>sidebar++;
 await context.poll();assert.equal(sidebar,0);assert.equal(calls.filter(call=>call[0]==='shell').length,1);assert.equal(calls.at(-1)[2],true);
 state.route='settings';state.parts=['settings','operation'];await context.poll();
 assert.equal(sidebar,1);assert.equal(calls.filter(call=>call[0]==='shell').length,1);
});
