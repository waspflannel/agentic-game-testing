'use strict';
// Test actual renderer bridge helpers, without Electron/native dialogs or a game.
const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync(require('node:path').join(__dirname, '../ashad/static/app.js'), 'utf8');
const helpers = source.slice(source.indexOf('function markDraftDirty('), source.indexOf('const activeStates ='));
function context() {
 const calls=[];
 const app=vm.createContext({state:{parts:['new','agent'],dirtyDraft:false},
  window:{ashadDesktop:{setDirtyDraft:async value=>calls.push(value)},confirm:()=>false},
  Event:class {constructor(type){this.type=type;}}, $:()=>app.form,
  toast(){throw Error('Unexpected bridge failure');},errorMessage:String});
 vm.runInContext(helpers,app);
 return {app,calls};
}
test('draft bridge sends only a boolean; Cancel navigation retains draft and accepts same route', async()=>{
 const {app,calls}=context();
 app.markDraftDirty(true);
 assert.equal(app.allowDraftNavigation('new/agent'),true);
 assert.equal(app.allowDraftNavigation('usage'),false);
 assert.equal(app.state.dirtyDraft,true);
 assert.deepEqual(calls,[true]);
 app.window.confirm=()=>true;
 assert.equal(app.allowDraftNavigation('usage'),true);
 assert.equal(app.state.dirtyDraft,false);
 assert.deepEqual(calls,[true,false]);
});
test('native file cancel does nothing; confirmed existing form receives path only, no import',async()=>{
 const {app}=context();
 const field={value:'previous',dispatchEvent(){this.changed=true;},focus(){this.focused=true;}};
 app.form={isConnected:true,querySelector:()=>field};
 const trigger={closest:()=>app.form,dataset:{kind:'folder'}};
 app.window.ashadDesktop.chooseBuild=async kind=>{assert.equal(kind,'folder');return null;};
 await app.chooseBuildManifest(trigger);
 assert.equal(field.value,'previous');assert.equal(field.changed,undefined);
 app.window.ashadDesktop.chooseBuild=async()=> 'C:\\selected game\\manifest.json';
 await app.chooseBuildManifest(trigger);
 assert.equal(field.value,'C:\\selected game\\manifest.json');assert.equal(field.changed,true);assert.equal(field.focused,true);
});
test('late native dialog cannot change another form or a disconnected page',async()=>{
 const {app}=context();let resolve;
 app.window.ashadDesktop.chooseBuild=()=>new Promise(done=>{resolve=done;});
 const field={value:'retained'};
 const original={isConnected:true,querySelector:()=>field};app.form=original;
 const pending=app.chooseBuildManifest({closest:()=>original,dataset:{}});
 app.form={};resolve('C:\\late\\manifest.json');await pending;
 assert.equal(field.value,'retained');
});
test('typing sends the dirty bridge only on changes and a rejecting bridge warns once',async()=>{
 const {app,calls}=context();let toasts=0;app.toast=()=>{toasts++;};
 for(let i=0;i<20;i++)app.markDraftDirty(true);
 assert.deepEqual(calls,[true]);
 app.window.ashadDesktop.setDirtyDraft=async()=>{throw new Error('Bridge rejected');};
 for(let i=0;i<20;i++){app.markDraftDirty(i%2===0?false:true);}
 await new Promise(setImmediate);
 assert.equal(toasts,1);
});
