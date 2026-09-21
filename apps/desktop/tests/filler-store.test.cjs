const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const {FillerStore} = require('../dist/filler-store');
const base = path.resolve(__dirname, '../../../.desktop-runtime-tests');
fs.mkdirSync(base, {recursive:true});
const io = p => path.toNamespacedPath(p);

function fixture(t) {
  const root = fs.mkdtempSync(path.join(base, 'filler-store-'));
  const key = crypto.randomBytes(32);
  const protection = {
    available: () => true,
    protect(bytes) {
      const iv = crypto.randomBytes(12);
      const cipher = crypto.createCipheriv('aes-256-gcm', key, iv);
      const body = Buffer.concat([cipher.update(bytes), cipher.final()]);
      return Buffer.concat([iv, cipher.getAuthTag(), body]);
    },
    unprotect(bytes) {
      const cipher = crypto.createDecipheriv('aes-256-gcm', key, bytes.subarray(0,12));
      cipher.setAuthTag(bytes.subarray(12,28));
      return Buffer.concat([cipher.update(bytes.subarray(28)),cipher.final()]);
    }
  };
  const options = {instanceRoot:root, instanceId:'a'.repeat(32), protection};
  const file = name => path.join(root, 'resume-filler', name);
  t.after(() => {
    assert.ok(root.startsWith(base + path.sep));
    fs.rmSync(io(root), {recursive:true,force:true});
  });
  return {root, options, protection, file, store:new FillerStore(options)};
}
function selected(f, name='synthetic.pdf', content='%PDF-1.7 synthetic-only') {
  const file=path.join(f.root,name); fs.writeFileSync(io(file),content); return file;
}

test('encrypted versioned profile/settings survive restart, with one bounded backup', t => {
  const f=fixture(t);
  assert.deepEqual(f.store.snapshot().profile,{});
  f.store.importJson(JSON.stringify({basic:{fullName:'匿名测试',email:'synthetic@example.invalid'}}));
  f.store.saveSettings({overwrite:false});
  f.store.saveProfile({basic:{fullName:'Synthetic Revision'}});
  const restored=new FillerStore(f.options);
  assert.equal(restored.snapshot().revision,3);
  assert.deepEqual(restored.snapshot().settings,{overwrite:false});
  assert.equal(restored.snapshot().profile.basic.fullName,'Synthetic Revision');
  assert.equal(fs.readdirSync(f.file('')).filter(n=>n.includes('backup')).length,1);
  for(const name of ['state.bin','state.backup.bin']) {
    const raw=fs.readFileSync(f.file(name));
    assert.equal(raw.includes(Buffer.from('Synthetic')),false);
    assert.equal(raw.includes(Buffer.from('匿名测试')),false);
  }
  const snap=restored.snapshot(); snap.profile.basic.fullName='mutated';
  assert.equal(restored.snapshot().profile.basic.fullName,'Synthetic Revision');
});

test('invalid imports and credential fields never replace existing resume', t => {
  const f=fixture(t); f.store.saveProfile({basic:{fullName:'Keep'}});
  const before=fs.readFileSync(f.file('state.bin'));
  for(const text of ['[]','null','{','{"__proto__":{}}','{"basic":{"api_key":"secret"}}','{"cookies":[]}', '"'+ 'x'.repeat(1024*1024)+'"']) {
    assert.throws(()=>f.store.importJson(text),/filler_/);
    assert.deepEqual(fs.readFileSync(f.file('state.bin')),before);
  }
  const cyclic={}; cyclic.self=cyclic;
  assert.throws(()=>f.store.saveProfile(cyclic),/profile_invalid/);
});

test('export includes selected resume fields only; no storage metadata or settings', t => {
  const f=fixture(t);
  f.store.saveProfile({basic:{fullName:'Synthetic'},education:[{school:'Example'}]});
  f.store.saveSettings({overwrite:true});
  f.store.replaceAttachment(selected(f));
  assert.deepEqual(JSON.parse(f.store.exportJson(['basic'])),{basic:{fullName:'Synthetic'}});
  assert.deepEqual(JSON.parse(f.store.exportJson([])),{});
  assert.throws(()=>f.store.exportJson(['settings']),/selection_invalid/);
  const text=f.store.exportJson();
  assert.equal(text.includes('attachment'),false); assert.equal(text.includes(f.root),false);
});

test('instances isolate records and ciphertext cannot be transplanted by identity', t => {
  const f=fixture(t); f.store.saveProfile({basic:{fullName:'One'}});
  const root=path.join(f.root,'other'); fs.mkdirSync(root);
  const other=new FillerStore({...f.options,instanceRoot:root,instanceId:'b'.repeat(32)});
  assert.deepEqual(other.snapshot().profile,{});
  fs.copyFileSync(f.file('state.bin'),path.join(root,'resume-filler/state.bin'));
  assert.throws(()=>new FillerStore({...f.options,instanceRoot:root,instanceId:'b'.repeat(32)}),/instance_mismatch/);
  assert.equal(f.store.snapshot().profile.basic.fullName,'One');
});

test('native protection required; failed decrypt never resets or falls back', t => {
  const f=fixture(t); f.store.saveProfile({basic:{fullName:'Keep'}});
  const before=fs.readFileSync(f.file('state.bin'));
  assert.throws(()=>new FillerStore({...f.options,protection:{...f.protection,available:()=>false}}),/protection_unavailable/);
  assert.throws(()=>new FillerStore({...f.options,protection:{...f.protection,unprotect(){throw Error('secret');}}}),/^FillerStoreError: filler_decryption_failed$/);
  f.protection.protect=bytes=>bytes;
  assert.throws(()=>f.store.saveProfile({basic:{fullName:'No'}}),/protection_failed/);
  assert.deepEqual(fs.readFileSync(f.file('state.bin')),before);
});

test('atomic rename failure retains previous state and removes partial file', t => {
  const f=fixture(t); f.store.saveProfile({basic:{fullName:'Keep'}});
  const before=fs.readFileSync(f.file('state.bin'));
  const rename=fs.renameSync;
  fs.renameSync=(from,to)=>{ if(to===io(f.file('state.bin'))) throw Error('synthetic rename failure'); return rename(from,to); };
  try { assert.throws(()=>f.store.saveProfile({basic:{fullName:'No'}}),/synthetic rename/); }
  finally { fs.renameSync=rename; }
  assert.deepEqual(fs.readFileSync(f.file('state.bin')),before);
  assert.equal(f.store.snapshot().profile.basic.fullName,'Keep');
  assert.equal(fs.readdirSync(f.file('')).some(n=>n.endsWith('.partial')),false);
});

test('corrupt current state requires explicit backup recovery, preserves revision', t => {
  const f=fixture(t); f.store.saveProfile({basic:{fullName:'First'}}); f.store.saveProfile({basic:{fullName:'Second'}});
  fs.writeFileSync(f.file('state.bin'),'damaged');
  assert.throws(()=>new FillerStore(f.options),/decryption_failed/);
  const restored=FillerStore.recoverBackup(f.options);
  assert.equal(restored.snapshot().profile.basic.fullName,'First');
  assert.equal(restored.snapshot().revision,2);
});

test('schema1 migrates in memory to one personal profile and writes schema3 only on explicit save', t => {
  const f=fixture(t);
  const legacy={schema:1,instanceId:f.options.instanceId,revision:4,profile:{basic:{fullName:'Old'}},settings:{overwrite:false},attachment:null};
  const encrypted=f.protection.protect(Buffer.from(JSON.stringify(legacy)));
  fs.writeFileSync(f.file('state.bin'),encrypted);
  const restored=new FillerStore(f.options);
  assert.equal(restored.snapshot().schema,3); assert.equal(restored.snapshot().revision,4);
  assert.equal(restored.snapshot().personalProfiles.length,1);assert.equal(restored.snapshot().personalProfiles[0].name,'个人资料');
  assert.deepEqual(fs.readFileSync(f.file('state.bin')),encrypted);
  restored.saveSettings({overwrite:true});
  assert.equal(JSON.parse(f.protection.unprotect(fs.readFileSync(f.file('state.bin')))).schema,3);
  const newer={...legacy,schema:99}; fs.writeFileSync(f.file('state.bin'),f.protection.protect(Buffer.from(JSON.stringify(newer))));
  assert.throws(()=>new FillerStore(f.options),/schema_unsupported/);
});

test('schema2 preserves personal and demo slots and legacy attachment until the next explicit write',async t=>{
  const f=fixture(t);
  const attachmentId=crypto.randomUUID(),attachmentBytes=Buffer.from('synthetic legacy attachment');
  const legacyAttachment={id:attachmentId,name:'legacy.pdf',type:'pdf',size:attachmentBytes.length,
    sha256:crypto.createHash('sha256').update(attachmentBytes).digest('hex')};
  fs.writeFileSync(f.file('attachments/'+attachmentId+'.bin'),f.protection.protect(attachmentBytes));
  const legacy={schema:2,instanceId:f.options.instanceId,revision:8,mode:'demo',
    personal:{profile:{basic:{fullName:'Legacy Personal'}},settings:{answer:'personal'},attachment:legacyAttachment},
    demo:{profile:{basic:{fullName:'Legacy Demo'}},settings:{answer:'demo'},attachment:null}};
  const encrypted=f.protection.protect(Buffer.from(JSON.stringify(legacy)));
  fs.writeFileSync(f.file('state.bin'),encrypted);
  const restored=new FillerStore(f.options);
  assert.equal(restored.snapshot().mode,'demo');assert.equal(restored.snapshot().profile.basic.fullName,'Legacy Demo');
  assert.deepEqual(fs.readFileSync(f.file('state.bin')),encrypted);
  restored.setMode('personal');
  assert.equal(restored.snapshot().profile.basic.fullName,'Legacy Personal');
  assert.equal(restored.snapshot().attachment.id,attachmentId);
  await restored.withAttachment(async file=>assert.deepEqual(fs.readFileSync(io(file)),attachmentBytes));
  assert.equal(JSON.parse(f.protection.unprotect(fs.readFileSync(f.file('state.bin')))).schema,3);
});

test('named profiles isolate custom answers, settings, encrypted attachments, and revision guards',async t=>{
  const f=fixture(t);
  f.store.saveProfile({basic:{fullName:'Primary Synthetic'}});
  f.store.saveSettings({answer:'primary',customAnswers:[{id:'primary-answer',label:'Synthetic question',value:'Primary answer'}]});
  f.store.replaceAttachment(selected(f,'primary.pdf','synthetic-primary'));
  const primary=f.store.snapshot(),primaryId=primary.activeProfileId,primaryAttachment=primary.attachment;
  const created=f.store.createProfile('  Research  ',primary.revision),researchId=created.activeProfileId;
  assert.notEqual(researchId,primaryId);assert.equal(created.activeProfileName,'Research');
  assert.deepEqual(created.profile,{});assert.deepEqual(created.settings,{});assert.equal(created.attachment,null);
  f.store.saveSettings({answer:'research',customAnswers:[{id:'research-answer',label:'Synthetic question',value:'Research answer'}]});
  const stored=fs.readFileSync(f.file('state.bin'));
  assert.equal(stored.includes(Buffer.from('Research')),false);
  const namedAttachment=f.store.replaceAttachment(selected(f,'research.docx','synthetic-research')).attachment;
  assert.notEqual(namedAttachment.id,primaryAttachment.id);
  const renamed=f.store.renameProfile(researchId,'Research Resume',f.store.snapshot().revision);
  assert.equal(renamed.activeProfileName,'Research Resume');
  assert.throws(()=>f.store.renameProfile(researchId,' 个人资料 ',renamed.revision),/profile_name_conflict/);
  const reopened=new FillerStore(f.options);
  assert.equal(reopened.snapshot().activeProfileId,researchId);assert.equal(reopened.snapshot().attachment.id,namedAttachment.id);
  assert.equal(reopened.snapshot().settings.answer,'research');assert.equal(reopened.snapshot().settings.customAnswers[0].value,'Research answer');
  await reopened.withAttachment(async file=>assert.equal(fs.readFileSync(io(file),'utf8'),'synthetic-research'));
  const selectedPrimary=reopened.selectProfile(primaryId,reopened.snapshot().revision);
  assert.equal(selectedPrimary.profile.basic.fullName,'Primary Synthetic');assert.equal(selectedPrimary.settings.answer,'primary');
  assert.equal(selectedPrimary.settings.customAnswers[0].value,'Primary answer');
  assert.equal(selectedPrimary.attachment.id,primaryAttachment.id);
  await reopened.withAttachment(async file=>assert.equal(fs.readFileSync(io(file),'utf8'),'synthetic-primary'));
  const beforeStale=new FillerStore(f.options);
  reopened.renameProfile(primaryId,'Primary',selectedPrimary.revision);
  assert.throws(()=>beforeStale.selectProfile(researchId,beforeStale.snapshot().revision),/stale_revision/);
  const afterDelete=reopened.deleteProfile(researchId,reopened.snapshot().revision);
  assert.equal(afterDelete.activeProfileId,primaryId);assert.equal(afterDelete.attachment.id,primaryAttachment.id);
  assert.throws(()=>reopened.deleteProfile(primaryId,afterDelete.revision),/profile_last_required/);
});

test('stale store and expected revision reject writes instead of losing edits', t => {
  const f=fixture(t); const stale=new FillerStore(f.options);
  f.store.saveProfile({basic:{fullName:'Newest'}});
  assert.throws(()=>stale.saveProfile({basic:{fullName:'Old'}}),/stale_revision/);
  assert.throws(()=>f.store.saveProfile({},0),/stale_revision/);
});

test('attachment privately copied, decrypted only during callback, cleaned even on error', async t => {
  const f=fixture(t); const source=selected(f);
  const snap=f.store.replaceAttachment(source); fs.unlinkSync(source);
  assert.equal(snap.attachment.type,'pdf'); assert.equal(snap.attachment.name,'synthetic.pdf');
  const stored=fs.readFileSync(f.file('attachments/'+snap.attachment.id+'.bin'));
  assert.equal(stored.includes(Buffer.from('%PDF')),false);
  let temporary;
  await assert.rejects(f.store.withAttachment(async (file,meta)=>{
    temporary=file; assert.equal(meta.sha256,snap.attachment.sha256);
    assert.match(fs.readFileSync(io(file),'utf8'),/^%PDF/);
    new FillerStore(f.options); assert.ok(fs.existsSync(io(file)));
    throw Error('synthetic upload failure');
  }),/synthetic upload/);
  assert.equal(fs.existsSync(io(temporary)),false);
  await new FillerStore(f.options).withAttachment(async file=>assert.match(fs.readFileSync(io(file),'utf8'),/^%PDF/));
});

test('failed attachment replacement preserves original, clear remains instance-local', async t => {
  const f=fixture(t); f.store.replaceAttachment(selected(f));
  const original=f.store.snapshot().attachment;
  assert.throws(()=>f.store.replaceAttachment(selected(f,'bad.exe')),/attachment_invalid/);
  const protect=f.protection.protect;
  f.protection.protect=()=>{throw Error('synthetic protection failure');};
  assert.throws(()=>f.store.replaceAttachment(selected(f,'replacement.docx')),/protection_failed/);
  f.protection.protect=protect;
  assert.deepEqual(f.store.snapshot().attachment,original);
  f.store.clearAttachment();
  assert.equal(new FillerStore(f.options).snapshot().attachment,null);
  await assert.rejects(f.store.withAttachment(async()=>{}),/attachment_missing/);
});

test('demo roundtrip retains personal resume/settings/attachment but never exposes attachment in demo', async t => {
  const f=fixture(t); f.store.saveProfile({basic:{fullName:'Personal Synthetic'}});
  f.store.saveSettings({overwrite:true}); f.store.replaceAttachment(selected(f));
  const personal=f.store.snapshot(); f.store.setMode('demo');
  const restored=new FillerStore(f.options);
  assert.equal(restored.snapshot().mode,'demo'); assert.equal(restored.snapshot().attachment,null);
  assert.equal(restored.exportJson().includes('Personal Synthetic'),false);
  await assert.rejects(restored.withAttachment(async()=>{}),/attachment_missing/);
  assert.throws(()=>restored.replaceAttachment(selected(f)),/demo_attachment_forbidden/);
  restored.saveProfile({basic:{fullName:'Edited Demo'}}); restored.setMode('personal');
  const actual=restored.snapshot();
  assert.deepEqual(actual.profile,personal.profile); assert.deepEqual(actual.settings,personal.settings);
  assert.deepEqual(actual.attachment,personal.attachment);
});

test('personal and demo modes keep separate custom-answer settings across reopen', t => {
  const f=fixture(t),personal=[{id:'personal-answer',origin:'*',pathname:'*',label:'Synthetic question',value:'Personal answer'}];
  f.store.saveProfile({basic:{fullName:'Personal Synthetic'}});f.store.saveSettings({customAnswers:personal});
  f.store.setMode('demo');f.store.saveSettings({customAnswers:[{id:'demo-answer',origin:'*',pathname:'*',label:'Synthetic question',value:'Demo answer'}]});
  let restored=new FillerStore(f.options);
  assert.equal(restored.snapshot().mode,'demo');assert.equal(restored.snapshot().settings.customAnswers[0].value,'Demo answer');
  restored.setMode('personal');
  assert.deepEqual(restored.snapshot().settings.customAnswers,personal);
  restored.setMode('demo');restored=new FillerStore(f.options);
  assert.equal(restored.snapshot().settings.customAnswers[0].value,'Demo answer');
});

test('crash leftovers cleaned only in owned temporary namespace', t => {
  const f=fixture(t); const name='upload-'+crypto.randomUUID()+'.pdf';
  fs.writeFileSync(f.file('temporary/'+name),'synthetic plaintext');
  fs.writeFileSync(f.file('temporary/unrelated.txt'),'keep');
  new FillerStore(f.options);
  assert.equal(fs.existsSync(f.file('temporary/'+name)),false);
  assert.equal(fs.readFileSync(f.file('temporary/unrelated.txt'),'utf8'),'keep');
});

test('Chinese spaces and real long path store persist across restart', t => {
  const f=fixture(t); const root=path.join(f.root,'匿名 space',...Array(8).fill('synthetic-long-directory'));
  fs.mkdirSync(io(root),{recursive:true}); assert.ok(root.length>260);
  const options={...f.options,instanceRoot:root}; const store=new FillerStore(options);
  store.saveProfile({basic:{fullName:'匿名'}});
  assert.equal(new FillerStore(options).snapshot().profile.basic.fullName,'匿名');
});

test('junction/symlink storage directory refused without deleting target', t => {
  const f=fixture(t); const other=path.join(f.root,'linked-instance'); fs.mkdirSync(other);
  const target=path.join(f.root,'target'); fs.mkdirSync(target);
  fs.symlinkSync(target,path.join(other,'resume-filler'),process.platform==='win32'?'junction':'dir');
  assert.throws(()=>new FillerStore({...f.options,instanceRoot:other}),/link_forbidden/);
  assert.ok(fs.existsSync(target));
});

test('bounded backups retain only recovery attachment after pruning on restart', t => {
  const f=fixture(t);
  for(let i=0;i<5;i++) f.store.replaceAttachment(selected(f,'synthetic.pdf','synthetic-'+i));
  new FillerStore(f.options);
  assert.equal(fs.readdirSync(f.file('attachments')).length,2);
});

test('attachment checksum detects substituted ciphertext before any plaintext lease', async t => {
  const f=fixture(t); f.store.replaceAttachment(selected(f));
  const meta=f.store.snapshot().attachment;
  fs.writeFileSync(f.file('attachments/'+meta.id+'.bin'),f.protection.protect(Buffer.from('substituted')));
  let called=false;
  await assert.rejects(f.store.withAttachment(async()=>{called=true;}),/attachment_corrupt/);
  assert.equal(called,false);
  assert.deepEqual(fs.readdirSync(f.file('temporary')),[]);
});

test('metadata commit failure leaves old attachment and no replacement orphan', async t => {
  const f=fixture(t); f.store.replaceAttachment(selected(f));
  const before=f.store.snapshot().attachment;
  const names=fs.readdirSync(f.file('attachments'));
  const rename=fs.renameSync;
  fs.renameSync=(from,to)=>{if(to===io(f.file('state.bin')))throw Error('synthetic failure');return rename(from,to);};
  try { assert.throws(()=>f.store.replaceAttachment(selected(f,'second.doc','new-synthetic'))); }
  finally {fs.renameSync=rename;}
  assert.deepEqual(f.store.snapshot().attachment,before);
  assert.deepEqual(fs.readdirSync(f.file('attachments')),names);
  await f.store.withAttachment(async file=>assert.match(fs.readFileSync(io(file),'utf8'),/^%PDF/));
});

test('oversized attachment is refused before encryption and old attachment preserved', t => {
  const f=fixture(t); f.store.replaceAttachment(selected(f));
  const old=f.store.snapshot().attachment;
  const large=path.join(f.root,'oversized.pdf'); const fd=fs.openSync(large,'w');
  fs.ftruncateSync(fd,20*1024*1024+1);fs.closeSync(fd);
  assert.throws(()=>f.store.replaceAttachment(large),/file_size/);
  assert.deepEqual(f.store.snapshot().attachment,old);
});

test('crash cleanup refuses linked upload leftovers rather than touching outside file', t => {
  const f=fixture(t);const target=path.join(f.root,'unrelated-directory');fs.mkdirSync(target);
  fs.writeFileSync(path.join(target,'keep'),'keep');
  const link=f.file('temporary/upload-'+crypto.randomUUID()+'.pdf');
  fs.symlinkSync(target,link,process.platform==='win32'?'junction':'dir');
  assert.throws(()=>new FillerStore(f.options),/link_forbidden/);
  assert.equal(fs.readFileSync(path.join(target,'keep'),'utf8'),'keep');
});

test('corrupted backup cannot be used as an implicit plaintext recovery', t => {
  const f=fixture(t); f.store.saveProfile({basic:{fullName:'one'}}); f.store.saveProfile({basic:{fullName:'two'}});
  const before=fs.readFileSync(f.file('state.bin'));
  fs.writeFileSync(f.file('state.backup.bin'),'plaintext is not recovery');
  assert.throws(()=>FillerStore.recoverBackup(f.options),/decryption_failed/);
  assert.deepEqual(fs.readFileSync(f.file('state.bin')),before);
});

test('standalone packaged dist module works without repository adapter resolution', t => {
  const f=fixture(t);
  const directory=path.join(f.root,'packaged/resources/app.asar/dist');
  fs.mkdirSync(directory,{recursive:true});
  const isolated=path.join(directory,'filler-store.js');
  fs.copyFileSync(require.resolve('../dist/filler-store'),isolated);
  const dependencies=new Set(Object.keys(require.cache));
  const {FillerStore:PackagedStore}=require(isolated);
  const store=new PackagedStore(f.options);
  store.importJson('{"basic":{"fullName":"Packaged Synthetic"},"education":[{"school":"Example"}]}');
  assert.equal(new PackagedStore(f.options).snapshot().profile.basic.fullName,'Packaged Synthetic');
  assert.deepEqual(Object.keys(require.cache).filter(name=>!dependencies.has(name)),[isolated]);
});

test('local JSON validator bounds complexity and rejects non-JSON values without executing hooks', t => {
  const f=fixture(t); f.store.saveProfile({basic:{fullName:'Keep'}});
  const before=fs.readFileSync(f.file('state.bin'));
  let deep={};for(let i=0;i<25;i++)deep={nested:deep};
  let called=false;
  const getter={};Object.defineProperty(getter,'field',{enumerable:true,get(){called=true;return 'bad';}});
  for(const invalid of [deep,{items:Array(20001).fill(0)},{value:Infinity},{value:undefined},{value:()=>{}},
    {value:new Date()},{toJSON(){called=true;return {};}},getter,JSON.parse('{"nested":{"constructor":{}}}')]) {
    assert.throws(()=>f.store.saveProfile(invalid),/filler_profile_invalid/);
    assert.deepEqual(fs.readFileSync(f.file('state.bin')),before);
  }
  assert.equal(called,false);
});
