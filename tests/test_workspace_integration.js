'use strict';
// Actual renderer route/refresh/poll functions + actual workspace extension; no native/browser acceptance claim.
const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const renderer=fs.readFileSync(path.join(__dirname,'../ashad/static/app.js'),'utf8');
const extension=fs.readFileSync(path.join(__dirname,'../ashad/static/workspace.js'),'utf8');
function harness(){
 const dialog={open:false,close(){this.open=false;}},listeners={},calls=[];
 const state={data:{projects:[{id:'project',name:'Game'}],builds:[],agents:[],runs:[],instance_slot:{available:true}},parts:[],route:'projects',renderEpoch:0,dataRevision:0};
 const context=vm.createContext({state,location:{hash:''},history:{replaceState(...args){calls.push(['restore',...args]);}},
  document:{title:'',hidden:false,body:{classList:{remove(){}}},querySelector:()=>null,addEventListener(name,fn){listeners[name]=fn;}},
  window:{scrollTo(){},confirm:()=>false},$:(selector)=>selector==='#dialog'?dialog:null,
  arr:value=>Array.isArray(value)?value:[],esc:value=>String(value??''),title:String,
  heading:(...args)=>args.join(' '),section:(...args)=>args.join(' '),notice:String,modal(){},
  errorMessage:String,toast(){},markDraftDirty(value){state.dirtyDraft=value;},
  allowDraftNavigation(){calls.push(['legacy-guard']);return true;},navigate(){},
  api:async()=>state.data,loadRun:async()=>{calls.push(['load-run']);state.run={plan:{title:'Retained instance'}};},loadEvents:async()=>{},refreshCodexStatus:async()=>{},
  patchHTML(){},updateStatusUI(){},shell(content,background){calls.push(['shell',content,background]);},
  sessionsView:()=>'',agentsView:()=>'',draftView:()=>'',runView:()=> 'Retained run',settingsView:()=>'',usageView:()=>'',
 });
 vm.runInContext(renderer.slice(renderer.indexOf('async function refresh()'),renderer.indexOf('function heading(')),context);
 vm.runInContext(renderer.slice(renderer.indexOf('function render(background='),renderer.indexOf("document.addEventListener('click'")),context);
 vm.runInContext(renderer.slice(renderer.indexOf('async function poll()'),renderer.indexOf('setInterval(poll,')),context);
 context.ashadWorkspaceContext={state,api:context.api,esc:context.esc,arr:context.arr,heading:context.heading,section:context.section,notice:context.notice,modal:context.modal,markDraftDirty:context.markDraftDirty,navigate:context.navigate,refresh:context.refresh,render:context.render,patchHTML:context.patchHTML,errorMessage:context.errorMessage,toast:context.toast};
 vm.runInContext(extension,context);
 return {context,state,calls,listeners,dialog};
}
test('production assets load workspace after renderer and bootstrap waits for defer completion',()=>{
 const index=fs.readFileSync(path.join(__dirname,'../ashad/static/index.html'),'utf8');
 assert.ok(index.indexOf('app.js')<index.indexOf('workspace.js'));
 assert.match(index,/workspace\.css/);
 assert.match(renderer,/document\.addEventListener\('DOMContentLoaded',init,\{once:true\}\)/);
 assert.match(renderer,/globalThis\.ashadWorkspaceContext=\{state,api,esc,arr/);
 assert.doesNotMatch(renderer,/<nav aria-label="Main navigation"><div class="nav-group">/);
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
 state.parts=['projects','project'];context.location.hash='#/usage';dialog.open=true;
 context.workspaceBeforeNavigate=async()=>false;await context.route();
 assert.equal(dialog.open,true);assert.equal(state.parts.join('/'),'projects/project');
 assert.ok(calls.some(call=>call[0]==='restore'));assert.ok(!calls.some(call=>call[0]==='legacy-guard'));
 context.workspaceBeforeNavigate=async()=>true;context.location.hash='#/runs/retained';await context.route();
 assert.equal(dialog.open,false);assert.equal(state.route,'runs');assert.equal(state.parts[1],'retained');
 assert.ok(calls.some(call=>call[0]==='load-run'));assert.match(calls.at(-1)[1],/Retained run/);
});
test('background project polling patches instead of replacing draft and refresh updates navigation',async()=>{
 const {context,state,calls}=harness();let sidebar=0;
 state.route='projects';state.parts=['projects','project'];context.workspaceUpdateSidebar=()=>sidebar++;
 await context.poll();assert.equal(sidebar,1);assert.equal(calls.at(-1)[0],'shell');assert.equal(calls.at(-1)[2],true);
});
