const {test}=require('node:test');
const assert=require('node:assert/strict');
const {scopedChanges}=require('../packaging/refresh-runtime.cjs');
const config='app/packages/config.py';
const capabilities='app/packages/desktop_runtime/capabilities.py';
const instance='app/packages/desktop_runtime/instance.py';
const fixture=()=>({schema:1,entrypoints:{python:'python/python.exe'},files:{[config]:'old',[capabilities]:'old',[instance]:'old','python/python.exe':'native'}});
test('mail refresh permits exactly two named Python files and unchanged metadata',()=>{
  const before=fixture(),after=fixture();after.files[config]='new';after.files[capabilities]='new';
  assert.deepEqual(scopedChanges(before,after,'mail-independent'),[config,capabilities]);
  assert.throws(()=>scopedChanges(before,after,'instance-long-path'),/scope_mismatch/);
  after.files[instance]='new';assert.throws(()=>scopedChanges(before,after,'mail-independent'),/scope_mismatch/);
});
test('missing change, native drift, additions, deletions and unknown scope fail closed',()=>{
  for(const mutate of [a=>{},a=>{a.files[config]='new';},a=>{a.files['python/python.exe']='new';}]) {
    const after=fixture();mutate(after);assert.throws(()=>scopedChanges(fixture(),after,'mail-independent'),/scope_mismatch/);
  }
  for(const mutate of [a=>{a.files['app/new.py']='new';},a=>{delete a.files[instance];}]) {
    const after=fixture();mutate(after);assert.throws(()=>scopedChanges(fixture(),after,'mail-independent'),/inventory_change/);
  }
  assert.throws(()=>scopedChanges(fixture(),fixture(),'all'),/unknown_runtime_refresh_scope/);
});
test('resource metadata cannot piggyback on scoped refresh; instance fix remains supported',()=>{
  const after=fixture();after.entrypoints.python='other.exe';
  assert.throws(()=>scopedChanges(fixture(),after,'mail-independent'),/metadata_change/);
  const longPath=fixture();longPath.files[instance]='new';
  assert.deepEqual(scopedChanges(fixture(),longPath,'instance-long-path'),[instance]);
});
