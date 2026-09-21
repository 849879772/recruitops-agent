'use strict';
const fs=require('node:fs');
const path=require('node:path');
const {spawnSync}=require('node:child_process');
const {randomUUID}=require('node:crypto');
const asar=require('@electron/asar');
const {hash,noLinks,validateRuntime}=require('./resources.cjs');

async function main() {
  const id=process.argv[2];
  if(!/^n-[a-f0-9]{8}$/.test(id||'')) throw new Error('invalid_candidate_id');
  const app=path.resolve(__dirname,'..');
  const repo=path.resolve(app,'../..');
  const output=path.join(repo,'artifacts/desktop-builds',id);
  noLinks(output);
  const recordPath=path.join(output,'build-record.json');
  const record=JSON.parse(fs.readFileSync(recordPath,'utf8'));
  if(record.release_accepted!==false || fs.existsSync(path.join(output,'acceptance'))) throw new Error('candidate_already_delivered');
  const packageName='recruitops-desktop-win32-x64';
  const resources=path.join(output,packageName,'resources');
  const archive=path.join(resources,'app.asar');
  if(fs.existsSync(archive+'.unpacked')) throw new Error('unpacked_resources_require_full_shell_packager');
  for(const [relative,digest] of Object.entries(record.sha256)) {
    if(await hash(path.join(output,relative))!==digest) throw new Error('candidate_hash_drift');
  }
  const stage=path.join(app,'.package-staging',`shell-refresh-${id}-${randomUUID().slice(0,8)}`);
  fs.mkdirSync(stage,{recursive:true});
  const shell=path.join(stage,'app');
  asar.extractAll(archive,shell);
  const files=['dist','renderer'].flatMap(dir=>fs.readdirSync(path.join(app,dir),{withFileTypes:true})
    .filter(entry=>entry.isFile()).map(entry=>`${dir}/${entry.name}`));
  for(const file of files) fs.copyFileSync(path.join(app,file),path.join(shell,file));
  const next=path.join(stage,'app.asar');
  await asar.createPackage(shell,next);
  for(const file of files) if(!asar.extractFile(next,file).equals(fs.readFileSync(path.join(app,file)))) throw new Error('shell_snapshot_mismatch');
  fs.copyFileSync(archive,path.join(stage,'previous-app.asar'));
  fs.copyFileSync(next,archive);
  record.sha256[`${packageName}/resources/app.asar`]=await hash(archive);
  record.shell_refresh={files,previous_archive:path.relative(repo,path.join(stage,'previous-app.asar'))};
  record.label='Shell refreshed; native preflight and anonymous acceptance pending';
  fs.writeFileSync(recordPath,JSON.stringify(record,null,2));
  console.log('Shell archive refreshed and hashed; native resources unchanged');
  const runtime=path.join(resources,'desktop-runtime');
  const verified=await validateRuntime(runtime,{repository:repo});
  const env={};
  for(const key of ['SystemRoot','WINDIR']) if(process.env[key]) env[key]=process.env[key];
  Object.assign(env,{HOME:stage,USERPROFILE:stage,APPDATA:stage,LOCALAPPDATA:stage,TEMP:stage,TMP:stage,NO_PROXY:'127.0.0.1,localhost,::1'});
  const result=spawnSync(verified.executable,['-I','-B',path.join(resources,'desktop-bootstrap.py'),'--resources',runtime],
    {cwd:stage,env,windowsHide:true,shell:false,encoding:'utf8',timeout:180000,maxBuffer:262144});
  const completed=result.stdout?.split(/\r?\n/).filter(Boolean).some(line=>{
    try {const event=JSON.parse(line);return event.event==='completed'&&event.stage==='preflight'&&event.started===false;} catch {return false;}
  });
  if(result.status!==0 || !completed) throw new Error('refreshed_native_preflight_failed');
  record.shell_refresh.native_preflight_passed=true;
  fs.writeFileSync(recordPath,JSON.stringify(record,null,2));
  console.log(JSON.stringify({output,app_asar_sha256:record.sha256[`${packageName}/resources/app.asar`],native_preflight_passed:true}));
}
main().catch(error=>{console.error(error.message);process.exitCode=1;});
