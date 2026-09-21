'use strict';
const fs = require('node:fs');
const path = require('node:path');
const { createHash } = require('node:crypto');

const entrypoints = ['python','codex','node','chromium','postgres','initdb','psql','pg_dump','pg_restore','pg_ctl','pg_config','vector_dll','vector_control','vector_sql','api_bootstrap','migration_script'];
const essentialSources = ['packages/desktop_runtime/__main__.py','packages/desktop_runtime/api_bootstrap.py','apps/api/main.py','apps/web/index.html','apps/web/app.js','scripts/apply_migrations.py'];
function fail(code) { throw new Error(code); }
function inside(root, relative) {
  if (typeof relative !== 'string' || relative.includes('\\') || relative.includes(':') || relative.startsWith('/') || relative.split('/').some(p => !p || p === '.' || p === '..' || /[. ]$/.test(p) || /^(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\.|$)/i.test(p))) fail('packaged_resource_path_invalid');
  return path.join(root, ...relative.split('/'));
}
function noLinks(filename) {
  const absolute = path.resolve(filename);
  let current = path.parse(absolute).root;
  for (const part of absolute.slice(current.length).split(path.sep).filter(Boolean)) {
    current = path.join(current,part);
    if (fs.existsSync(current) && fs.lstatSync(current).isSymbolicLink()) fail('packaged_resource_link_rejected');
  }
}
function walk(root, relative = '') {
  noLinks(root);
  return fs.readdirSync(path.join(root,relative),{withFileTypes:true}).flatMap(item => {
    const name = relative ? `${relative}/${item.name}` : item.name;
    if (item.isSymbolicLink()) fail('packaged_resource_link_rejected');
    if (item.isDirectory()) return walk(root,name);
    if (!item.isFile()) fail('packaged_resource_type_invalid');
    return [name];
  });
}
async function hash(filename) {
  const digest = createHash('sha256');
  for await (const chunk of fs.createReadStream(filename)) digest.update(chunk);
  return digest.digest('hex');
}
function validateNativePathBudget(root, manifest) {
  const maximum = Math.max(root.length, ...Object.keys(manifest.files).map(relative => inside(root,relative).length));
  if (maximum > 259) throw new Error(`package_native_path_too_long: maximum ${maximum} characters exceeds 259; choose a shorter package output path`);
  return maximum;
}
function sourceInventory(repo) {
  const paths = [];
  const sourceExtension = /\.(py|sql|json|ya?ml|txt|md|[cm]?js|html|css|j2|jinja2|toml|svg|png|ico|woff2?)$/i;
  for (const directory of ['packages','apps/api','apps/web','migrations','.agents/skills']) {
    for (const relative of walk(path.join(repo,directory))) {
      // Electron-only adapter is separately sealed, never Python application data.
      if (directory === 'packages' && relative.startsWith('desktop_filler/')) continue;
      if (relative.split('/').some(part => ['__pycache__','node_modules','.git','data','.cache'].includes(part))) continue;
      if (sourceExtension.test(relative)) paths.push(`${directory}/${relative}`);
    }
  }
  for (const relative of walk(path.join(repo,'config'))) if (relative.includes('.example.') || relative === 'README.md') paths.push(`config/${relative}`);
  for (const relative of ['scripts/apply_migrations.py','scripts/run_mcp_server.py','pyproject.toml','LICENSE']) if (fs.existsSync(path.join(repo,relative))) paths.push(relative);
  return [...new Set(paths)].sort();
}
async function validateRuntime(root, options = {}) {
  noLinks(root);
  let manifest;
  try { manifest = JSON.parse(fs.readFileSync(path.join(root,'runtime-manifest.json'),'utf8')); }
  catch { fail('packaged_runtime_manifest_missing_or_invalid'); }
  if (manifest?.schema !== 1 || manifest.platform !== 'windows-x64' || manifest.postgres_major !== 16) fail('packaged_runtime_manifest_unsupported');
  if (!manifest.files || !manifest.entrypoints || !manifest.components) fail('packaged_runtime_manifest_incomplete');
  for (const key of entrypoints) if (typeof manifest.entrypoints[key] !== 'string' || !Object.hasOwn(manifest.files,manifest.entrypoints[key])) fail('packaged_runtime_entrypoint_missing');
  if (manifest.entrypoints.api_bootstrap !== 'app/packages/desktop_runtime/api_bootstrap.py' || manifest.entrypoints.migration_script !== 'app/scripts/apply_migrations.py') fail('packaged_application_layout_invalid');
  for (const key of ['python','codex','node','chromium','postgres','pgvector','application']) {
    const component = manifest.components[key];
    if (!component || !/^\d+\.\d+(?:\.\d+){0,2}(?:[-+][A-Za-z0-9.]+)?$/.test(component.version) || !/^https:\/\//.test(component.source) || !Object.hasOwn(manifest.files,component.license_file)) fail('packaged_component_metadata_missing');
  }
  const required = options.repository ? sourceInventory(options.repository) : essentialSources;
  for (const relative of required) if (!Object.hasOwn(manifest.files,`app/${relative}`)) fail('packaged_application_source_missing');
  if (options.repository) for (const name of Object.keys(manifest.files)) {
    if (name.startsWith('app/') && !required.includes(name.slice(4))) fail('packaged_application_unexpected_file');
  }
  if (!Object.keys(manifest.files).some(name => /^app\/migrations\/[^/]+\.sql$/.test(name))) fail('packaged_migrations_missing');
  const actual = walk(root);
  for (const relative of actual) if (relative !== 'runtime-manifest.json' && !Object.hasOwn(manifest.files,relative)) fail('packaged_untracked_resource');
  for (const [relative,digest] of Object.entries(manifest.files)) {
    const filename = inside(root,relative);
    if (!/^[a-f0-9]{64}$/.test(digest) || !fs.existsSync(filename)) fail('packaged_resource_missing_or_hash_invalid');
    if (await hash(filename) !== digest) fail('packaged_resource_hash_mismatch');
  }
  if (options.repository) for (const relative of required) {
    if (await hash(path.join(options.repository,relative)) !== manifest.files[`app/${relative}`]) fail('packaged_application_source_stale');
  }
  const executable = inside(root,manifest.entrypoints.python);
  if (path.extname(executable).toLowerCase() !== '.exe') fail('packaged_python_entrypoint_invalid');
  const fd = fs.openSync(executable,'r');
  try {
    const header = Buffer.alloc(64); if (fs.readSync(fd,header,0,64,0) !== 64 || header.toString('ascii',0,2) !== 'MZ') fail('packaged_python_not_x64');
    const pe = Buffer.alloc(6); if (fs.readSync(fd,pe,0,6,header.readUInt32LE(60)) !== 6 || pe.toString('ascii',0,4) !== 'PE\0\0' || pe.readUInt16LE(4) !== 0x8664) fail('packaged_python_not_x64');
  } finally { fs.closeSync(fd); }
  return { manifest, executable, application: path.join(root,'app') };
}
async function stageResources(repo, runtime, destination) {
  if (fs.existsSync(destination)) fail('package_staging_already_exists');
  await validateRuntime(runtime,{repository:repo});
  fs.mkdirSync(destination,{recursive:true});
  const target = path.join(destination,'desktop-runtime');
  fs.cpSync(runtime,target,{recursive:true,errorOnExist:true,force:false});
  await validateRuntime(target,{repository:repo});
  const adapter = path.join(destination,'desktop-browser'); fs.mkdirSync(adapter);
  for (const name of ['index.cjs','index.d.cts','NOTICE']) fs.copyFileSync(path.join(repo,'packages/desktop_browser',name),path.join(adapter,name),fs.constants.COPYFILE_EXCL);
  fs.copyFileSync(path.join(repo,'LICENSE'),path.join(adapter,'LICENSE'),fs.constants.COPYFILE_EXCL);
  require(path.join(repo,'packages/desktop_browser/index.cjs')).packageObservationResources(path.join(adapter,'resources'));
  fs.copyFileSync(path.join(__dirname,'desktop-bootstrap.py'),path.join(destination,'desktop-bootstrap.py'),fs.constants.COPYFILE_EXCL);
  const filler=stageFiller(repo,destination);
  return [target,adapter,filler,path.join(destination,'desktop-bootstrap.py')];
}
const FILLER_FILES=['NOTICE','browser-engine/content.js','browser-engine/core.js','browser-engine/provenance.json','browser-engine/repeater-engine.js','index.cjs','index.d.cts'];
function validateFiller(root) {
  noLinks(root);
  const manifestPath=path.join(root,'manifest.json');noLinks(manifestPath);
  const manifest=JSON.parse(fs.readFileSync(manifestPath,'utf8'));
  const names=FILLER_FILES;
  if(manifest.schema!==1 || JSON.stringify(Object.keys(manifest.files||{}).sort())!==JSON.stringify(names)) fail('filler_manifest_invalid');
  if(JSON.stringify(walk(root).sort())!==JSON.stringify([...names,'manifest.json'].sort())) fail('filler_unexpected_resource');
  for(const name of names) {
    const filename=path.join(root,name);noLinks(filename);
    if(createHash('sha256').update(fs.readFileSync(filename)).digest('hex')!==manifest.files[name]) fail('filler_hash_mismatch');
  }
  return manifest;
}
function stageFiller(repo,destination) {
  const root=path.join(destination,'desktop-filler');
  fs.mkdirSync(root);
  const manifest={schema:1,files:{}};
  for(const name of FILLER_FILES) {
    const source=path.join(repo,'packages/desktop_filler',name);noLinks(source);
    fs.mkdirSync(path.dirname(path.join(root,name)),{recursive:true});
    fs.copyFileSync(source,path.join(root,name),fs.constants.COPYFILE_EXCL);
    manifest.files[name]=createHash('sha256').update(fs.readFileSync(source)).digest('hex');
  }
  fs.writeFileSync(path.join(root,'manifest.json'),JSON.stringify(manifest,null,2),{flag:'wx'});
  validateFiller(root);return root;
}
module.exports = { inside, noLinks, hash, sourceInventory, validateRuntime, stageResources, validateNativePathBudget, validateFiller, stageFiller, FILLER_FILES };
