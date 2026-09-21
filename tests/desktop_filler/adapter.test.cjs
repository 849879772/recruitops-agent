'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const vm = require('node:vm');
const api = require('../../packages/desktop_filler/index.cjs');

function fixture(t) {
  const folder = fs.mkdtempSync(path.join(os.tmpdir(), 'public-filler-test-'));
  t.after(() => fs.rmSync(folder, { recursive: true, force: true }));
  fs.writeFileSync(path.join(folder, 'core.js'), 'globalThis.fixtureCore=true;');
  fs.writeFileSync(path.join(folder, 'repeater-engine.js'), 'globalThis.fixtureRepeater=true;');
  fs.writeFileSync(path.join(folder, 'content.js'), `
    const originals=new Map();
    chrome.runtime.onMessage.addListener((m,s,reply)=>{
      commands.push(m.type);
      if(m.type==='RESUME_SCAN') reply({ok:true,totalFields:fields.length,emptyFields:fields.length,matches:fields.map(e=>({fieldId:e.marker,key:'name',label:e.label,controlKind:e.type,value:m.resume.name})),learnableFields:[{value:'must-not-return'}]});
      if(m.type==='RESUME_FILL') { const a=m.assignments[0],e=fields.find(e=>e.marker===a.fieldId); originals.set(e,e.value); e.value=a.value; reply({ok:true,filled:1,results:[{expected:'must-not-return',actual:'must-not-return'}]}); }
      if(m.type==='RESUME_UNDO') { for(const [e,v] of originals)e.value=v; reply({ok:true,restored:originals.size,failed:[]}); originals.clear(); }
    });`);
  return folder;
}

function element(id, extras = {}) {
  return Object.assign({ marker:id, label:'Name', tagName:'INPUT', type:'text', value:'', checked:false,
    id, name:'candidateName', labels:[{textContent:'Name'}], isConnected:true, disabled:false,
    closest(){return null;},getClientRects(){return [{}];},
    getAttribute(name) { return name==='data-local-resume-field-id'?this.marker:(this[name] ?? null); },
  }, extras);
}

function world(fields) {
  const state = { fields, commands:[],location:{href:'https://fixture.example.test/form'},CSS:{escape:s=>s},
    getComputedStyle(){return {display:'block',visibility:'visible',opacity:'1'};},
    setTimeout,clearTimeout,
    document:{ querySelector(selector) {return fields.find(e=>e.isConnected&&selector.includes('"'+e.marker+'"'))||null;},getElementById(){return null;} } };
  const context=vm.createContext(state);
  vm.runInContext('globalThis.top=globalThis;globalThis.self=globalThis;',context);
  return context;
}

test('loads only fixed source files, stable hashes, no data auto-read', t => {
  const folder=fixture(t);
  fs.writeFileSync(path.join(folder,'background.js'),'throw new Error("not loaded")');
  fs.writeFileSync(path.join(folder,'resume-data.js'),'throw new Error("not loaded")');
  const bundle=api.loadLocalFiller(folder);
  assert.deepEqual(bundle.files.map(f=>f.name),api.SOURCE_FILES);
  assert.equal(bundle.hash,api.loadLocalFiller(folder).hash);
  assert.equal(bundle.source.includes('not loaded'),false);
  assert.ok(Object.isFrozen(bundle));
  fs.appendFileSync(path.join(folder,'core.js'),'\n// changed');
  assert.notEqual(bundle.hash,api.loadLocalFiller(folder).hash);
  assert.throws(()=>api.buildScanScript({...bundle},{name:'Synthetic'}),/bundle_required/);
});

test('rejects symlinks, missing, oversize and invalid UTF8 sources without leaking paths', t => {
  const folder=fixture(t);
  const filename=path.join(folder,'core.js');
  fs.writeFileSync(filename,Buffer.alloc(2*1024*1024+1));
  assert.throws(()=>api.loadLocalFiller(folder),/^Error: filler_source_size_invalid$/);
  fs.writeFileSync(filename,Buffer.from([0xff]));
  assert.throws(()=>api.loadLocalFiller(folder),/^Error: filler_source_unreadable$/);
  fs.unlinkSync(filename);
  assert.throws(()=>api.loadLocalFiller(folder),/^Error: filler_source_unreadable$/);
  const link=folder+'-link';
  try { fs.symlinkSync(folder,link,process.platform==='win32'?'junction':'dir'); }
  catch(error) { if(error.code==='EPERM') return; throw error; }
  t.after(()=>fs.unlinkSync(link));
  assert.throws(()=>api.loadLocalFiller(link),/symlink_rejected/);
});

test('JSON and actual legacy DEFAULT_RESUME imports, bounded no require/eval/process', () => {
  assert.deepEqual(api.parseProfileJson('{"name":"Synthetic"}'),{name:'Synthetic'});
  assert.deepEqual(api.parseLegacyProfile('globalThis.DEFAULT_RESUME={name:"Synthetic"};'),{name:'Synthetic'});
  assert.deepEqual(api.parseLegacyProfile('globalThis.LOCAL_RESUME_DATA={name:"Synthetic"};'),{name:'Synthetic'});
  for(const text of ['while(true){}','require("node:fs")','process.env','eval("1")',
    'globalThis.DEFAULT_RESUME={get name(){while(true){}}};',
    'globalThis.DEFAULT_RESUME={};Promise.resolve().then(()=>{while(true){}});']) {
    assert.throws(()=>api.parseLegacyProfile(text),/^Error: filler_legacy_profile_invalid$/);
  }
  assert.throws(()=>api.parseProfileJson('{"__proto__":{"polluted":true}}'),/profile_invalid/);
  assert.throws(()=>api.parseProfileJson('x'.repeat(1024*1024+1)),/size_invalid/);
});

test('scan only previews, confirmed selected IDs fill, same-document undo restores', async t => {
  const bundle=api.loadLocalFiller(fixture(t));
  const fields=[element('one'),element('two')], context=world(fields);
  const scan=await vm.runInContext(api.buildScanScript(bundle,{name:'Synthetic'}),context);
  assert.deepEqual(context.commands,['RESUME_SCAN']);
  assert.equal(fields[0].value,'');
  assert.equal(scan.learnableFields,undefined);
  const request={scanId:scan.scanId,fieldIds:['one'],confirmed:true};
  assert.throws(()=>api.buildFillScript(bundle,{...request,confirmed:false}),/confirmation/);
  assert.throws(()=>api.buildFillScript(bundle,{...request,script:'arbitrary'}),/confirmation/);
  const result=await vm.runInContext(api.buildFillScript(bundle,request),context);
  assert.equal(result.filled,1); assert.equal(fields[0].value,'Synthetic'); assert.equal(fields[1].value,'');
  assert.equal(JSON.stringify(result).includes('must-not-return'),false);
  await assert.rejects(vm.runInContext(api.buildFillScript(bundle,request),context),/stale_scan/);
  const undone=await vm.runInContext(api.buildUndoScript(bundle),context);
  assert.equal(undone.restored,1); assert.equal(fields[0].value,'');
});

for (const extras of [{type:'password'},{type:'hidden'},{type:'submit'},{type:'file'},
  {type:'reset'},{tagName:'BUTTON'},{name:'otp'},{id:'captcha'},{'aria-label':'验证码'},
  {placeholder:'Password'},{autocomplete:'one-time-code'},{labels:[{textContent:'短信验证'}]}]) {
  test('sensitive control never appears in preview '+JSON.stringify(extras), async t => {
    const bundle=api.loadLocalFiller(fixture(t)), context=world([element('one',extras)]);
    const scan=await vm.runInContext(api.buildScanScript(bundle,{name:'Synthetic'}),context);
    assert.equal(scan.matches.length,0); assert.equal(scan.blockedCount,1);
  });
}

for(const change of ['replace','type','label','value','navigation']) {
  test('changed DOM or navigation requires rescan: '+change,async t=>{
    const bundle=api.loadLocalFiller(fixture(t)), fields=[element('one')],context=world(fields);
    const scan=await vm.runInContext(api.buildScanScript(bundle,{name:'Synthetic'}),context);
    if(change==='replace'){fields[0].isConnected=false;fields.push(element('one'));}
    if(change==='type')fields[0].type='password';
    if(change==='label')fields[0].labels[0].textContent='Other meaning';
    if(change==='value')fields[0].value='User edit';
    if(change==='navigation')context.location.href+='?changed';
    await assert.rejects(vm.runInContext(api.buildFillScript(bundle,{scanId:scan.scanId,fieldIds:['one'],confirmed:true}),context),/changed/);
    assert.deepEqual(context.commands,['RESUME_SCAN']);
  });
}

test('undo cannot act on a replacement document node',async t=>{
  const bundle=api.loadLocalFiller(fixture(t)), fields=[element('one')],context=world(fields);
  const scan=await vm.runInContext(api.buildScanScript(bundle,{name:'Synthetic'}),context);
  await vm.runInContext(api.buildFillScript(bundle,{scanId:scan.scanId,fieldIds:['one'],confirmed:true}),context);
  fields[0].isConnected=false; fields.push(element('one'));
  await assert.rejects(vm.runInContext(api.buildUndoScript(bundle),context),/undo_document_changed/);
});

test('preview expires after 120 seconds and cannot fill a subframe',async t=>{
  const bundle=api.loadLocalFiller(fixture(t)),context=world([element('one')]);
  let now=1000;
  context.Date={now:()=>now};
  const scan=await vm.runInContext(api.buildScanScript(bundle,{name:'Synthetic'}),context);
  assert.equal(scan.issuedAt,1000);
  now+=120001;
  await assert.rejects(vm.runInContext(api.buildFillScript(bundle,{scanId:scan.scanId,fieldIds:['one'],confirmed:true}),context),/expired/);
  vm.runInContext('globalThis.top={};',context);
  await assert.rejects(vm.runInContext(api.buildScanScript(bundle,{name:'Synthetic'}),context),/top_frame/);
});

for(const change of ['hidden','inert','aria-hidden','css','label']) {
  test('hidden or semantically changed fields require rescan: '+change,async t=>{
    const bundle=api.loadLocalFiller(fixture(t)),fields=[element('one')],context=world(fields);
    const scan=await vm.runInContext(api.buildScanScript(bundle,{name:'Synthetic'}),context);
    if(change==='css')context.getComputedStyle=()=>({display:'none',visibility:'visible'});
    else if(change==='label')fields[0]['aria-label']='OTP';
    else fields[0].closest=()=>({});
    await assert.rejects(vm.runInContext(api.buildFillScript(bundle,{scanId:scan.scanId,fieldIds:['one'],confirmed:true}),context),/changed/);
  });
}

for(const change of ['deadline','navigation','subframe']) {
  test('checks operation bounds between each field: '+change,async t=>{
    const bundle=api.loadLocalFiller(fixture(t)),fields=[element('one'),element('two')],context=world(fields);
    let now=1000;
    context.Date={now:()=>now};
    let value='';
    Object.defineProperty(fields[0],'value',{get(){return value;},set(v){
      value=v;
      if(change==='deadline')now+=60001;
      if(change==='navigation')context.location.href+='?changed';
      if(change==='subframe')vm.runInContext('globalThis.top={};',context);
    }});
    const scan=await vm.runInContext(api.buildScanScript(bundle,{name:'Synthetic'}),context);
    await assert.rejects(vm.runInContext(api.buildFillScript(bundle,{scanId:scan.scanId,fieldIds:['one','two'],confirmed:true}),context),/deadline|navigation/);
    assert.equal(fields[0].value,'Synthetic');assert.equal(fields[1].value,'');
  });
}

test('repeated scan in same world keeps undo and does not reload engine',async t=>{
  const bundle=api.loadLocalFiller(fixture(t)),fields=[element('one')],context=world(fields);
  const scan=await vm.runInContext(api.buildScanScript(bundle,{name:'Synthetic'}),context);
  await vm.runInContext(api.buildFillScript(bundle,{scanId:scan.scanId,fieldIds:['one'],confirmed:true}),context);
  const next=await vm.runInContext(api.buildScanScript(bundle,{name:'Synthetic'}),context);
  assert.notEqual(next.scanId,scan.scanId);
  const undo=await vm.runInContext(api.buildUndoScript(bundle),context);
  assert.equal(undo.restored,1);assert.equal(fields[0].value,'');
});

test('SPA navigation rejects old fill/undo but rescan resets engine and old undo',async t=>{
  const bundle=api.loadLocalFiller(fixture(t)),fields=[element('one')],context=world(fields);
  const first=await vm.runInContext(api.buildScanScript(bundle,{name:'First Synthetic'}),context);
  const oldRequest={scanId:first.scanId,fieldIds:['one'],confirmed:true};
  await vm.runInContext(api.buildFillScript(bundle,oldRequest),context);
  context.location.href+='#new-route';
  await assert.rejects(vm.runInContext(api.buildFillScript(bundle,oldRequest),context),/navigation_changed/);
  await assert.rejects(vm.runInContext(api.buildUndoScript(bundle),context),/navigation_changed/);
  const second=await vm.runInContext(api.buildScanScript(bundle,{name:'Second Synthetic'}),context);
  assert.notEqual(first.scanId,second.scanId);
  await assert.rejects(vm.runInContext(api.buildFillScript(bundle,oldRequest),context),/stale_scan/);
  await assert.rejects(vm.runInContext(api.buildUndoScript(bundle),context),/nothing_to_undo/);
  await vm.runInContext(api.buildFillScript(bundle,{scanId:second.scanId,fieldIds:['one'],confirmed:true}),context);
  assert.equal(fields[0].value,'Second Synthetic');
  await vm.runInContext(api.buildUndoScript(bundle),context);
  assert.equal(fields[0].value,'First Synthetic');
});
