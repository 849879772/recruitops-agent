'use strict';
const fs = require('node:fs');
const path = require('node:path');
const { randomUUID } = require('node:crypto');
const { stageResources, hash, validateRuntime, noLinks, validateNativePathBudget, validateFiller, stageFiller } = require('./resources.cjs');
const { spawn } = require('node:child_process');

async function preflight(stage, cwd) {
  const { validateRuntime } = require('./resources.cjs');
  const root=path.join(stage,'desktop-runtime');
  const verified=await validateRuntime(root);
  const env={};
  for(const key of ['SystemRoot','WINDIR']) if(process.env[key]) env[key]=process.env[key];
  Object.assign(env,{HOME:cwd,USERPROFILE:cwd,APPDATA:cwd,LOCALAPPDATA:cwd,TEMP:cwd,TMP:cwd,NO_PROXY:'127.0.0.1,localhost,::1'});
  await new Promise((resolve,reject)=>{
    const child=spawn(verified.executable,['-I','-B',path.join(stage,'desktop-bootstrap.py'),'--resources',root],{cwd,env,windowsHide:true,shell:false,stdio:['ignore','pipe','pipe']});
    let completed=false,buffer='';
    const timer=setTimeout(()=>{child.kill();reject(new Error('package_native_preflight_timeout'));},120000);
    child.stdout.setEncoding('utf8');child.stderr.resume();
    child.stdout.on('data',data=>{
      buffer+=data;
      if(buffer.length>65536) {child.kill();return;}
      let at;
      while((at=buffer.indexOf('\n'))>=0) {
        const line=buffer.slice(0,at);buffer=buffer.slice(at+1);
        try {const event=JSON.parse(line);if(event.protocol===1&&event.event==='completed'&&event.stage==='preflight'&&event.started===false) completed=true;} catch {}
      }
    });
    child.once('error',()=>{clearTimeout(timer);reject(new Error('package_native_python_launch_failed'));});
    child.once('exit',code=>{clearTimeout(timer);code===0&&completed?resolve():reject(new Error('package_native_preflight_failed'));});
  });
}

async function main() {
  const app = path.resolve(__dirname,'..');
  const repo = path.resolve(app,'../..');
  const runtime = process.env.RECRUITOPS_DESKTOP_PACKAGE_RUNTIME;
  if (!runtime) throw new Error('package_native_runtime_required');
  const relativeRuntime=path.relative(repo,path.resolve(runtime));
  if(!relativeRuntime || relativeRuntime.startsWith('..') || path.isAbsolute(relativeRuntime)) throw new Error('package_runtime_outside_checkout');
  // Embedded Python and native tools still encounter MAX_PATH on some hosts.
  const build = `n-${randomUUID().slice(0,8)}`;
  const outputRoot = process.env.RECRUITOPS_DESKTOP_PACKAGE_OUTPUT_ROOT;
  if(outputRoot && !path.isAbsolute(outputRoot)) throw new Error('package_output_root_must_be_absolute');
  const output = path.join(outputRoot || path.join(repo,'artifacts','desktop-builds'),build);
  console.log('New candidate output:',output);
  const packageName=JSON.parse(fs.readFileSync(path.join(app,'package.json'),'utf8')).name;
  const stageRoot = path.join(app,'.package-staging');
  const reuse = process.env.RECRUITOPS_DESKTOP_PACKAGE_REUSE_STAGE;
  const stage = reuse ? path.resolve(reuse) : path.join(stageRoot,build);
  if (fs.existsSync(output)) throw new Error('package_output_already_exists');
  let resources;
  if (reuse) {
    if(path.dirname(stage)!==stageRoot) throw new Error('package_reuse_stage_outside_checkout');
    noLinks(stage);
    await validateRuntime(path.join(stage,'desktop-runtime'),{repository:repo});
    for(const name of ['index.cjs','index.d.cts','NOTICE']) {
      if(await hash(path.join(stage,'desktop-browser',name))!==await hash(path.join(repo,'packages/desktop_browser',name))) throw new Error('package_reuse_adapter_stale');
    }
    require(path.join(stage,'desktop-browser/index.cjs')).loadObservationResources();
    if(await hash(path.join(stage,'desktop-bootstrap.py'))!==await hash(path.join(__dirname,'desktop-bootstrap.py'))) throw new Error('package_reuse_bootstrap_stale');
    if(!fs.existsSync(path.join(stage,'desktop-filler'))) stageFiller(repo,stage);
    validateFiller(path.join(stage,'desktop-filler'));
    for(const name of require('./resources.cjs').FILLER_FILES) if(await hash(path.join(stage,'desktop-filler',name))!==await hash(path.join(repo,'packages/desktop_filler',name))) throw new Error('package_reuse_filler_stale');
    resources=['desktop-runtime','desktop-browser','desktop-filler','desktop-bootstrap.py'].map(name=>path.join(stage,name));
  } else resources = await stageResources(repo,path.resolve(runtime),stage);
  const maximumPath=validateNativePathBudget(path.join(output,`${packageName}-win32-x64/resources/desktop-runtime`),
    JSON.parse(fs.readFileSync(path.join(stage,'desktop-runtime/runtime-manifest.json'),'utf8')));
  console.log('Maximum final native resource path:',maximumPath);
  const preflightHome=path.join(stage,'preflight-home');fs.mkdirSync(preflightHome,{recursive:true});
  console.log('Staged resources verified; native preflight');
  await preflight(stage,preflightHome);
  process.env.RECRUITOPS_DESKTOP_PACKAGE_STAGE = stage;
  Object.assign(process.env,{HTTP_PROXY:'http://127.0.0.1:10808',HTTPS_PROXY:'http://127.0.0.1:10808',
    NO_PROXY:'localhost,127.0.0.1,::1',ELECTRON_GET_USE_PROXY:'1',GLOBAL_AGENT_HTTP_PROXY:'http://127.0.0.1:10808',
    GLOBAL_AGENT_NO_PROXY:'localhost,127.0.0.1,::1',electron_config_cache:path.join(app,'.cache/electron')});
  fs.mkdirSync(output,{recursive:true});
  console.log('Packaging verified snapshot:',output);
  await require('@electron-forge/core').api.package({dir:app,outDir:output,platform:'win32',arch:'x64',interactive:false});
  const results=[{packagedPath:path.join(output,`${packageName}-win32-x64`)}];
  if(!fs.existsSync(path.join(results[0].packagedPath,'RecruitOps-Desktop-Preview.exe'))) throw new Error('candidate_package_output_missing');
  console.log('Final-location native resource preflight');
  await preflight(path.join(results[0].packagedPath,'resources'),preflightHome);
  validateFiller(path.join(results[0].packagedPath,'resources/desktop-filler'));
  fs.copyFileSync(path.join(__dirname,'README-PORTABLE.txt'),path.join(results[0].packagedPath,'README-PORTABLE.txt'),fs.constants.COPYFILE_EXCL);
  const hashes={};
  for(const result of results) for(const name of ['RecruitOps-Desktop-Preview.exe','resources/app.asar','resources/desktop-runtime/runtime-manifest.json','resources/desktop-filler/manifest.json']) {
    hashes[`${path.basename(result.packagedPath)}/${name}`]=await hash(path.join(result.packagedPath,name));
  }
  fs.writeFileSync(path.join(output,'build-record.json'),JSON.stringify({schema:1,release_accepted:false,
    maximum_resource_path:maximumPath,
    label:'Unsigned native integration candidate; acceptance pending',resources:resources.map(p=>path.basename(p)),
    packages:results.map(r=>path.basename(r.packagedPath)),sha256:hashes},null,2),{flag:'wx'});
  console.log(JSON.stringify({output,release_accepted:false}));
}
main().catch(error=>{ console.error(error.message); process.exitCode=1; });
