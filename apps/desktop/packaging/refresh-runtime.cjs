'use strict';
const fs=require('node:fs');
const path=require('node:path');
const {spawnSync}=require('node:child_process');
const {randomUUID}=require('node:crypto');
const {isDeepStrictEqual}=require('node:util');
const {hash,noLinks,validateRuntime,validateNativePathBudget}=require('./resources.cjs');

function scopedChanges(before,after,scope) {
  const allowed=scope==='instance-long-path'?['app/packages/desktop_runtime/instance.py']:
    scope==='mail-independent'?['app/packages/config.py','app/packages/desktop_runtime/capabilities.py']:null;
  if(!allowed) throw new Error('unknown_runtime_refresh_scope');
  const {files:oldFiles,...oldMetadata}=before;
  const {files:newFiles,...newMetadata}=after;
  if(!isDeepStrictEqual(oldMetadata,newMetadata)) throw new Error('runtime_metadata_change_rejected');
  if(!isDeepStrictEqual(Object.keys(oldFiles).sort(),Object.keys(newFiles).sort())) throw new Error('resource_inventory_change_requires_full_build');
  const changed=Object.keys(newFiles).filter(name=>oldFiles[name]!==newFiles[name]).sort();
  if(!isDeepStrictEqual(changed,allowed)) throw new Error('runtime_refresh_scope_mismatch');
  return changed;
}

async function main() {
  const [id,expectedSeal,scope='instance-long-path']=process.argv.slice(2);
  if(!/^n-[a-f0-9]{8}$/.test(id||'') || !/^[a-f0-9]{64}$/.test(expectedSeal||'')) throw new Error('candidate_and_resealed_hash_required');
  const app=path.resolve(__dirname,'..'), repo=path.resolve(app,'../..');
  const output=path.join(repo,'artifacts/desktop-builds',id);
  const source=path.join(repo,'.desktop-runtime-tests/native-runtime-v1');
  const resources=path.join(output,'recruitops-desktop-win32-x64/resources');
  const target=path.join(resources,'desktop-runtime');
  noLinks(output);noLinks(source);
  const recordPath=path.join(output,'build-record.json');
  const record=JSON.parse(fs.readFileSync(recordPath,'utf8'));
  if(record.release_accepted!==false) throw new Error('released_candidate_refused');
  for(const [relative,digest] of Object.entries(record.sha256)) {
    if(await hash(path.join(output,relative))!==digest) throw new Error('candidate_hash_drift');
  }
  if(await hash(path.join(source,'runtime-manifest.json'))!==expectedSeal) throw new Error('stage_seal_mismatch');
  const verified=await validateRuntime(source,{repository:repo});
  const before=JSON.parse(fs.readFileSync(path.join(target,'runtime-manifest.json'),'utf8'));
  const after=verified.manifest;
  const changed=scopedChanges(before,after,scope);
  validateNativePathBudget(target,after);
  // Verify every affected old file before replacing any of them.
  for(const relative of changed) {
    const destination=path.join(target,relative);noLinks(destination);
    if(await hash(destination)!==before.files[relative]) throw new Error('target_source_drift');
  }
  const backup=path.join(app,'.package-staging',`runtime-refresh-${id}-${randomUUID().slice(0,8)}`);
  fs.mkdirSync(backup,{recursive:true});
  fs.copyFileSync(recordPath,path.join(backup,'build-record.json'));
  for(const relative of [...changed,'runtime-manifest.json']) {
    const destination=path.join(target,relative);
    noLinks(destination);
    fs.copyFileSync(destination,path.join(backup,path.basename(relative)));
    fs.copyFileSync(path.join(source,relative),destination+'.refresh',fs.constants.COPYFILE_EXCL);
    fs.renameSync(destination+'.refresh',destination);
  }
  record.sha256['recruitops-desktop-win32-x64/resources/desktop-runtime/runtime-manifest.json']=expectedSeal;
  record.runtime_refresh={scope,files:changed,backup:path.relative(repo,backup),native_preflight_passed:false};
  record.label=`Scoped ${scope} runtime refresh; native preflight pending`;
  fs.writeFileSync(recordPath,JSON.stringify(record,null,2));
  const final=await validateRuntime(target,{repository:repo});
  const env={};
  for(const key of ['SystemRoot','WINDIR']) if(process.env[key]) env[key]=process.env[key];
  Object.assign(env,{HOME:backup,USERPROFILE:backup,APPDATA:backup,LOCALAPPDATA:backup,TEMP:backup,TMP:backup,NO_PROXY:'127.0.0.1,localhost,::1'});
  const result=spawnSync(final.executable,['-I','-B',path.join(resources,'desktop-bootstrap.py'),'--resources',target],
    {cwd:backup,env,windowsHide:true,shell:false,encoding:'utf8',timeout:180000,maxBuffer:262144});
  const completed=result.stdout?.split(/\r?\n/).filter(Boolean).some(line=>{
    try {const event=JSON.parse(line);return event.event==='completed'&&event.stage==='preflight'&&event.started===false;} catch {return false;}
  });
  if(result.status!==0 || !completed) throw new Error('refreshed_runtime_preflight_failed');
  record.runtime_refresh.native_preflight_passed=true;
  record.label=`Scoped ${scope} runtime refresh; native preflight passed; business acceptance not implied`;
  fs.writeFileSync(recordPath,JSON.stringify(record,null,2));
  console.log(JSON.stringify({output,changed,manifest:expectedSeal,native_preflight_passed:true}));
}
if(require.main===module) main().catch(error=>{console.error(error.message);process.exitCode=1;});
module.exports={scopedChanges};
