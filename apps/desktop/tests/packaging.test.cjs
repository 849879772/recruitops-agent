const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const crypto = require('node:crypto');
const {validateRuntime,sourceInventory,inside,stageResources,validateNativePathBudget,stageFiller,validateFiller} = require('../packaging/resources.cjs');
const {packagedRuntimeLaunch} = require('../dist/packaged-runtime');
const repo = path.resolve(__dirname,'../../..');
test('pinned Forge exposes supported package facade',()=>{
  assert.equal(typeof require('@electron-forge/core').api.package,'function');
});
test('native final output path budget accepts 259 and rejects 260 with actionable error',()=>{
  const root=path.resolve('fixture-native-root');
  const relative='a'.repeat(259-root.length-1);
  assert.equal(validateNativePathBudget(root,{files:{[relative]:'fixture'}}),259);
  assert.throws(()=>validateNativePathBudget(root,{files:{[relative+'b']:'fixture'}}),/maximum 260 characters exceeds 259; choose a shorter/);
});
function fixture(t) {
  const home = fs.mkdtempSync(path.join(os.tmpdir(),'recruitops-package-contract-'));
  t.after(()=>fs.rmSync(home,{recursive:true,force:true}));
  const root = path.join(home,'resources/desktop-runtime');fs.mkdirSync(root,{recursive:true});
  const manifest={schema:1,platform:'windows-x64',postgres_major:16,components:{},entrypoints:{},files:{}};
  const put = (name,content='fixture only, not native release') => {const filename=path.join(root,name);fs.mkdirSync(path.dirname(filename),{recursive:true});fs.writeFileSync(filename,content);manifest.files[name]=crypto.createHash('sha256').update(content).digest('hex');};
  for (const key of ['python','codex','node','chromium','postgres','initdb','psql','pg_dump','pg_restore','pg_ctl','pg_config','vector_dll','vector_control','vector_sql']) { const name=`${key}/${key}.exe`;put(name);manifest.entrypoints[key]=name; }
  // Non-executable PE-header fixture for structural validation only. Never launched.
  const pe=Buffer.alloc(134);pe.write('MZ');pe.writeUInt32LE(128,60);pe.write('PE\0\0',128);pe.writeUInt16LE(0x8664,132);put(manifest.entrypoints.python,pe);
  for(const name of ['packages/desktop_runtime/__main__.py','packages/desktop_runtime/api_bootstrap.py','apps/api/main.py','apps/web/index.html','apps/web/app.js','scripts/apply_migrations.py','migrations/001_fixture.sql']) put('app/'+name);
  manifest.entrypoints.api_bootstrap='app/packages/desktop_runtime/api_bootstrap.py';manifest.entrypoints.migration_script='app/scripts/apply_migrations.py';
  for(const key of ['python','codex','node','chromium','postgres','pgvector','application']) {put(`licenses/${key}.txt`);manifest.components[key]={version:'16.0',source:'https://fixtures.example/fixture',license_file:`licenses/${key}.txt`};}
  const save=()=>fs.writeFileSync(path.join(root,'runtime-manifest.json'),JSON.stringify(manifest));save();
  return {home,root,manifest,put,save};
}
test('application inclusion inventory includes all migrations/active sources and no local data',()=>{
  const files=sourceInventory(repo);
  for(const file of ['packages/desktop_runtime/__main__.py','apps/api/main.py','apps/web/app.js','scripts/apply_migrations.py','config/companies.example.yaml','migrations/023_automation_multiple_daily_times.sql']) assert.ok(files.includes(file),file);
  assert.ok(!files.some(file=>file.includes('.venv') || file.endsWith('.db') || file.includes('node_modules') || file.includes('.env')));
});
test('manifest paths, missing resources, extra files and drift fail before Python execution',async t=>{
  const f=fixture(t);await validateRuntime(f.root);
  for(const name of ['../x','C:/x','/x','app/../x','app\\x','app/CON.txt','app/x.']) assert.throws(()=>inside(f.root,name));
  fs.writeFileSync(path.join(f.root,'untracked.txt'),'x');await assert.rejects(validateRuntime(f.root),/untracked_resource/);fs.unlinkSync(path.join(f.root,'untracked.txt'));
  fs.writeFileSync(path.join(f.root,'app/apps/web/app.js'),'changed');await assert.rejects(validateRuntime(f.root),/hash_mismatch/);
  await assert.rejects(validateRuntime(path.join(f.home,'absent')),/manifest_missing/);
});
test('missing app source/native key and corrupt Python PE fail explicitly',async t=>{
  const f=fixture(t);delete f.manifest.entrypoints.postgres;f.save();await assert.rejects(validateRuntime(f.root),/entrypoint_missing/);
  f.manifest.entrypoints.postgres='postgres/postgres.exe';delete f.manifest.files['app/apps/api/main.py'];f.save();await assert.rejects(validateRuntime(f.root),/source_missing/);
  f.put('app/apps/api/main.py');f.put(f.manifest.entrypoints.python,'not an executable');f.save();await assert.rejects(validateRuntime(f.root),/python_not_x64/);
});
test('packaged launch uses only sealed resources, ignores dev paths and pins isolated profile',async t=>{
  const f=fixture(t);const resources=path.dirname(f.root);
  fs.copyFileSync(path.resolve(__dirname,'../packaging/desktop-bootstrap.py'),path.join(resources,'desktop-bootstrap.py'));
  const isolated=path.join(f.home,'.desktop-runtime-tests');const profile=path.join(isolated,'profile');fs.mkdirSync(profile,{recursive:true});
  const env={RECRUITOPS_DESKTOP_ISOLATION_ROOT:isolated,RECRUITOPS_DESKTOP_RUNTIME_RESOURCES:'C:/untrusted',RECRUITOPS_DESKTOP_RUNTIME_INSTANCE:'C:/untrusted',RECRUITOPS_DESKTOP_API_ORIGIN:'http://127.0.0.1:8012',PYTHONPATH:'C:/untrusted',PATH:'C:/untrusted',DEEPSEEK_API_KEY:'must-not-inherit',SystemDrive:'C:',ProgramData:'C:/ProgramData'};
  const launch=await packagedRuntimeLaunch(resources,profile,env);
  assert.ok(launch.executable.startsWith(f.root));assert.equal(launch.cwd,profile);
  assert.ok(launch.args.includes('--isolation-root'));assert.ok(launch.args.includes('-I'));
  assert.ok(launch.args.includes('--desktop'));assert.equal(launch.desktop,true);
  const readOnly=await packagedRuntimeLaunch(resources,profile,{...env,RECRUITOPS_DESKTOP_ENABLE_WRITES_FOR_INSTANCE:'untrusted'},true);
  assert.equal(readOnly.desktop,false);assert.equal(readOnly.args.includes('--desktop'),false);
  assert.equal(readOnly.args.includes('--enable-writes-for-instance'),false);
  assert.equal(launch.env.PATH,undefined);assert.equal(launch.env.PYTHONPATH,undefined);assert.equal(launch.env.DEEPSEEK_API_KEY,undefined);
  assert.equal(launch.env.SystemDrive,'C:');assert.equal(launch.env.ProgramData,'C:/ProgramData');
  assert.equal(JSON.stringify(launch).includes('.venv-desktop-tests'),false);assert.equal(JSON.stringify(launch).includes('untrusted'),false);
  assert.deepEqual((await packagedRuntimeLaunch(resources,profile,env)).args,launch.args);
  await assert.rejects(packagedRuntimeLaunch(resources,f.home,env),/profile_outside/);
  await assert.rejects(packagedRuntimeLaunch(resources,profile,{}),/isolation_root_required/);
  fs.unlinkSync(path.join(resources,'desktop-bootstrap.py'));await assert.rejects(packagedRuntimeLaunch(resources,profile,env),/bootstrap_missing/);
});
test('staging refuses old destination and incomplete application tree',async t=>{
  const f=fixture(t);await assert.rejects(stageResources(repo,f.root,f.home),/already_exists/);
  await assert.rejects(stageResources(repo,f.root,path.join(f.home,'stage')),/source_missing/);
  assert.equal(fs.existsSync(path.join(f.home,'stage')),false);
});
test('complete source contract stages adapter resources, notices and immutable bootstrap',async t=>{
  const f=fixture(t);
  const snapshot=path.join(f.home,'source');
  for(const relative of [...sourceInventory(repo),'packages/desktop_browser/index.d.cts','packages/desktop_browser/NOTICE',...require('../packaging/resources.cjs').FILLER_FILES.map(name=>'packages/desktop_filler/'+name)]) {
    const target=path.join(snapshot,relative);fs.mkdirSync(path.dirname(target),{recursive:true});fs.copyFileSync(path.join(repo,relative),target);
  }
  require(path.join(repo,'packages/desktop_browser/index.cjs')).packageObservationResources(path.join(snapshot,'packages/desktop_browser/resources'));
  // All fixture application bytes come from this public checkout; fake native bytes are never run.
  fs.rmSync(path.join(f.root,'app'),{recursive:true,force:true});
  for(const name of Object.keys(f.manifest.files)) if(name.startsWith('app/')) delete f.manifest.files[name];
  for(const relative of sourceInventory(snapshot)) f.put('app/'+relative,fs.readFileSync(path.join(snapshot,relative)));
  f.save();const stage=path.join(f.home,'stage');
  const included=await stageResources(snapshot,f.root,stage);
  assert.deepEqual(included.map(name=>path.basename(name)),['desktop-runtime','desktop-browser','desktop-filler','desktop-bootstrap.py']);
  assert.deepEqual(Object.keys(validateFiller(path.join(stage,'desktop-filler')).files),require('../packaging/resources.cjs').FILLER_FILES);
  const adapter=require(path.join(stage,'desktop-browser/index.cjs'));
  assert.equal(Object.keys(adapter.loadObservationResources()).length,4);
  assert.ok(fs.existsSync(path.join(stage,'desktop-browser/LICENSE')));
  assert.ok(fs.readFileSync(path.join(stage,'desktop-bootstrap.py'),'utf8').includes('sys.path.insert(0, str(application))'));
  f.put('app/.env','unsafe fixture sentinel');f.save();
  await assert.rejects(validateRuntime(f.root,{repository:snapshot}),/application_unexpected_file/);
});

test('filler has exact separately sealed public files and rejects extras/hash drift',t=>{
  const f=fixture(t);const target=stageFiller(repo,f.home);
  assert.equal(sourceInventory(repo).some(name=>name.startsWith('packages/desktop_filler/')),false);
  assert.deepEqual(fs.readdirSync(target).sort(),['NOTICE','browser-engine','index.cjs','index.d.cts','manifest.json']);
  assert.equal(require(path.join(target,'index.cjs')).loadDesktopFiller().files.length,3);
  validateFiller(target);
  fs.writeFileSync(path.join(target,'profile.json'),'{}');assert.throws(()=>validateFiller(target),/unexpected_resource/);
  fs.unlinkSync(path.join(target,'profile.json'));
  fs.appendFileSync(path.join(target,'index.cjs'),'\n// drift');assert.throws(()=>validateFiller(target),/hash_mismatch/);
});
