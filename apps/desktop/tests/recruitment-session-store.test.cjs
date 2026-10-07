const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const {EventEmitter} = require('node:events');
const {randomBytes, createCipheriv, createDecipheriv} = require('node:crypto');
const {RecruitmentSessionStore} = require('../dist/recruitment-session-store');
const DAY = 86400000;
const identity = c => JSON.stringify([c.domain, c.path || '/', c.name]);
const cookie = (extra={}) => ({domain:'login.example.test',hostOnly:true,name:'sid',value:'synthetic-secret',
  path:'/',sameSite:'lax',secure:true,httpOnly:true,session:true,...extra});
function fixture(t) {
  const directory=fs.mkdtempSync(path.join(os.tmpdir(),'recruitops-session-unit-'));
  t.after(()=>fs.rmSync(directory,{recursive:true,force:true}));
  const secret=randomBytes(32);
  const protection={available:()=>true,
    protect:text=>{const iv=randomBytes(12),cipher=createCipheriv('aes-256-gcm',secret,iv);
      return Buffer.concat([iv,cipher.update(text),cipher.final(),cipher.getAuthTag()]);},
    unprotect:data=>{const decipher=createDecipheriv('aes-256-gcm',secret,data.subarray(0,12));
      decipher.setAuthTag(data.subarray(-16));return Buffer.concat([decipher.update(data.subarray(12,-16)),decipher.final()]).toString();}};
  // Deliberately use independent cookie stores to emulate Chromium dropping
  // session cookies at process exit, not merely closing a page.
  const session=(initial=[])=>{
    const cookies=new EventEmitter(),values=new Map(initial.map(c=>[identity(c),c]));
    cookies.get=async()=>[...values.values()];cookies.flushStore=async()=>{cookies.flushed=(cookies.flushed||0)+1;};
    cookies.set=async details=>{const c=cookie({...details,domain:details.domain||new URL(details.url).hostname,
      hostOnly:!details.domain,session:details.expirationDate===undefined});delete c.url;values.set(identity(c),c);cookies.emit('changed',{},c,'explicit',false);};
    cookies.delete=c=>{values.delete(identity(c));cookies.emit('changed',{},c,'explicit',true);};
    return {cookies,flushStorageData(){this.flushed=(this.flushed||0)+1;}};
  };
  return {directory,protection,session,filename:path.join(directory,'recruitment-session/state.bin')};
}
test('encrypted recovery preserves hostOnly/domain, HttpOnly/SameSite and session semantics',async t=>{
  const f=fixture(t),original=cookie(),parent=cookie({domain:'.example.test',hostOnly:false,name:'parent',sameSite:'strict'});
  const s=f.session([original,parent,cookie({name:'persistent',session:false,expirationDate:1000})]);
  const store=new RecruitmentSessionStore(f.directory,s,f.protection,()=>100);await store.start();store.stop();
  assert.equal(fs.readFileSync(f.filename).includes(Buffer.from('synthetic-secret')),false);
  const fresh=f.session(),restored=new RecruitmentSessionStore(f.directory,fresh,f.protection,()=>200);await restored.start();t.after(()=>restored.stop());
  assert.deepEqual(await fresh.cookies.get({}),[original,parent]);
  assert.deepEqual(restored.status(),{code:'ready',restored:2});
  assert.ok(fresh.flushed);assert.ok(fresh.cookies.flushed);
  assert.deepEqual(fs.readdirSync(path.dirname(f.filename)),['state.bin']);
});
test('logout deletion and transition to persistent cookie invalidate recovery immediately',async t=>{
  const f=fixture(t),c=cookie(),s=f.session([c]),store=new RecruitmentSessionStore(f.directory,s,f.protection,()=>100);
  await store.start();s.cookies.delete(c);
  const afterLogout=f.session(),next=new RecruitmentSessionStore(f.directory,afterLogout,f.protection,()=>101);await next.start();next.stop();
  assert.deepEqual(await afterLogout.cookies.get({}),[]);
  await s.cookies.set({url:'https://login.example.test',name:'sid',value:'old'});await store.flush();
  await s.cookies.set({url:'https://login.example.test',name:'sid',value:'new',expirationDate:999});
  const afterPersistent=f.session(),last=new RecruitmentSessionStore(f.directory,afterPersistent,f.protection,()=>102);
  await last.start();assert.deepEqual(await afterPersistent.cookies.get({}),[]);last.stop();store.stop();
});
test('restarts do not extend seven-day recovery and newer native cookie is not overwritten',async t=>{
  const f=fixture(t),c=cookie(),store=new RecruitmentSessionStore(f.directory,f.session([c]),f.protection,()=>100);
  await store.start();store.stop();
  const restart=new RecruitmentSessionStore(f.directory,f.session(),f.protection,()=>100+6*DAY);await restart.start();restart.stop();
  const expired=f.session(),final=new RecruitmentSessionStore(f.directory,expired,f.protection,()=>101+7*DAY);
  await final.start();assert.deepEqual(await expired.cookies.get({}),[]);final.stop();
  const newer=cookie({value:'fresh',session:false,expirationDate:900000});
  const first=new RecruitmentSessionStore(f.directory,f.session([c]),f.protection,()=>100);await first.start();first.stop();
  const native=f.session([newer]),second=new RecruitmentSessionStore(f.directory,native,f.protection,()=>200);
  await second.start();assert.deepEqual(await native.cookies.get({}),[newer]);second.stop();
});
test('profile transplant/corruption/unavailable encryption never restore or export plaintext',async t=>{
  const f=fixture(t),store=new RecruitmentSessionStore(f.directory,f.session([cookie()]),f.protection,()=>100);await store.start();store.stop();
  const other=path.join(f.directory,'different-profile');fs.mkdirSync(path.join(other,'recruitment-session'),{recursive:true});
  fs.copyFileSync(f.filename,path.join(other,'recruitment-session/state.bin'));
  const migrated=f.session(),m=new RecruitmentSessionStore(other,migrated,f.protection,()=>200);await m.start();m.stop();
  assert.deepEqual(await migrated.cookies.get({}),[]);
  fs.writeFileSync(f.filename,'corrupt encrypted state');
  const corrupted=f.session(),r=new RecruitmentSessionStore(f.directory,corrupted,f.protection,()=>200);await r.start();r.stop();
  assert.deepEqual(await corrupted.cookies.get({}),[]);
  const disabled=new RecruitmentSessionStore(f.directory,f.session([cookie()]),{...f.protection,available:()=>false},()=>200);
  await disabled.start();assert.equal(disabled.status().code,'native_only');assert.equal(fs.existsSync(f.filename),false);disabled.stop();
});
test('changed cookies are flushed on debounce, repeated saves keep only one file',async t=>{
  const f=fixture(t),s=f.session(),store=new RecruitmentSessionStore(f.directory,s,f.protection,()=>100);await store.start();t.after(()=>store.stop());
  await s.cookies.set({url:'https://login.example.test',name:'sid',value:'changed'});
  await new Promise(resolve=>setTimeout(resolve,1100));
  for(let i=0;i<10;i++)await store.flush();
  assert.deepEqual(fs.readdirSync(path.dirname(f.filename)),['state.bin']);
  assert.ok(s.cookies.flushed>=3);
  const restored=f.session(),next=new RecruitmentSessionStore(f.directory,restored,f.protection,()=>200);await next.start();next.stop();
  assert.equal((await restored.cookies.get({}))[0].value,'changed');
});
test('overwrite removal followed by unchanged insertion cannot roll the TTL forward',async t=>{
  const f=fixture(t),c=cookie(),s=f.session([c]);let time=100;
  const store=new RecruitmentSessionStore(f.directory,s,f.protection,()=>time);await store.start();
  time+=6*DAY;
  s.cookies.emit('changed',{},c,'overwrite',true);
  assert.deepEqual(JSON.parse(f.protection.unprotect(fs.readFileSync(f.filename))).cookies,[]);
  s.cookies.emit('changed',{},c,'inserted-no-change-overwrite',false);
  await store.flush();store.stop();
  const fresh=f.session(),next=new RecruitmentSessionStore(f.directory,fresh,f.protection,()=>101+7*DAY);
  await next.start();assert.deepEqual(await fresh.cookies.get({}),[]);next.stop();
});
test('native persistent changes do not repeatedly encrypt unrelated session recovery',async t=>{
  const f=fixture(t),s=f.session([cookie()]);let writes=0;
  const store=new RecruitmentSessionStore(f.directory,s,{...f.protection,protect:text=>{writes++;return f.protection.protect(text);}},()=>100);
  await store.start();const baseline=writes;
  for(let i=0;i<20;i++)s.cookies.emit('changed',{},cookie({name:'tracking',session:false,expirationDate:9000}),'explicit',false);
  assert.equal(writes,baseline);store.stop();
});
