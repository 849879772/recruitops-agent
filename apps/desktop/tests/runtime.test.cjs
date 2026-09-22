const { test } = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');
const { OwnedRuntime, runtimeLaunch } = require('../dist/runtime-client');
const repo = path.resolve(__dirname, '../../..');
const wait = async predicate => { for (let i = 0; i < 150; i++) { if (predicate()) return; await new Promise(r => setTimeout(r, 50)); } throw new Error('fixture timeout'); };
function fixture(mode, expectedInstance, desktop = false) {
  return new OwnedRuntime(() => {}, { executable: path.join(repo, '.venv-desktop-tests/Scripts/python.exe'),
    args: [path.join(__dirname, 'runtime_fixture.py'), mode], cwd: repo,
    env: { SystemRoot: process.env.SystemRoot, PYTHONUTF8: '1' }, expectedInstance, desktop });
}
test('controlled desktop handshake binds writable readiness to the previously opened instance',async()=>{
  const runtime=fixture('desktop',undefined,true);runtime.start();
  try {await wait(()=>runtime.state.status==='ready');assert.equal(runtime.state.writes,true);assert.equal(runtime.state.instanceId,'fixture-instance');}
  finally {await runtime.stop();}
  const unbound=fixture('desktop',undefined,true);
  assert.throws(()=>unbound.accept({protocol:1,sequence:1,run_id:'run',instance_id:'unbound',event:'ready',stage:'runtime',api_url:'http://127.0.0.1:55001',writes:true}),/desktop_identity/);
});
test('runtime config ignores ambient API/credentials and rejects instance escape', () => {
  assert.equal(runtimeLaunch(repo, { RECRUITOPS_DESKTOP_API_ORIGIN: 'http://127.0.0.1:8012' }), undefined);
  assert.throws(() => runtimeLaunch(repo, { RECRUITOPS_DESKTOP_RUNTIME_RESOURCES: repo, RECRUITOPS_DESKTOP_RUNTIME_INSTANCE: path.dirname(repo) }));
  const launch = runtimeLaunch(repo, { RECRUITOPS_DESKTOP_RUNTIME_RESOURCES: repo, DEEPSEEK_API_KEY: 'must-not-pass', DATABASE_URL: 'must-not-pass', PATH: 'must-not-pass' });
  assert.equal(launch.env.DEEPSEEK_API_KEY, undefined); assert.equal(launch.env.PATH, undefined); assert.equal(launch.env.DATABASE_URL, undefined);
});
test('owned read-only runtime handshake, request policy and graceful stop', async () => {
  const runtime = fixture('read-only'); runtime.start();
  try {
    await wait(() => runtime.state.status === 'ready');
    assert.ok(runtime.allows(runtime.origin + '/api/read', 'GET'));
    assert.equal(runtime.allows(runtime.origin + '/api/save', 'POST'), false);
    assert.equal(runtime.allows(runtime.origin + '/api/local-ui/configuration/read', 'POST'), true);
    for (const route of ['/api/local-ui/configuration/save','/api/local-ui/configuration/read/','/api/local-ui/configuration/read?save=1','/api/local-ui/configuration/read#fragment','/api/local-ui/configuration/%72ead','/api/local-ui/configuration/model/test']) {
      assert.equal(runtime.allows(runtime.origin + route, 'POST'),false,route);
    }
    assert.equal(runtime.allows(runtime.origin + '/api/local-ui/configuration/read','PUT'),false);
    assert.equal(runtime.allows('https://ats.example/api/local-ui/configuration/read','POST'),false);
    assert.equal(runtime.allows(runtime.origin.replace('http:', 'ws:') + '/ws', 'GET'), false);
    assert.equal(runtime.allows('http://127.0.0.1:8012/', 'GET'), false);
    assert.equal(runtime.allows('http://127.0.0.1:5433/', 'GET'), false);
    assert.equal(JSON.stringify(runtime.state).includes(runtime.authorization()), false);
  } finally { await runtime.stop(); }
  assert.equal(runtime.state.status, 'stopped'); assert.equal(runtime.origin, undefined);
});
test('runtime reports bounded active background tasks', async () => {
  const runtime = fixture('active-task', undefined, true); runtime.start();
  try {
    await wait(() => runtime.state.status === 'ready');
    assert.deepEqual(await runtime.activity(), { activeTasks: [{ runId: 'active-run-1234567890', currentStep: 'discovery' }] });
  } finally { await runtime.stop(); }
});
test('instance-specific write opt-in permits exact owned HTTP and WS only', async () => {
  const runtime = fixture('writes', 'fixture-instance'); runtime.start();
  try {
    await wait(() => runtime.state.status === 'ready');
    assert.ok(runtime.allows(runtime.origin + '/api/save', 'POST'));
    assert.ok(runtime.allows(runtime.origin.replace('http:', 'ws:') + '/ws', 'GET'));
    assert.equal(runtime.allows('https://ats.example/', 'POST'), false);
  } finally { await runtime.stop(); }
});
for (const mode of ['writes', 'identity-failure', 'crash']) test(`runtime rejects ${mode} without valid ownership/opt-in`, async () => {
  const runtime = fixture(mode); runtime.start();
  try { await wait(() => runtime.state.status === 'failed'); assert.equal(runtime.origin, undefined); assert.equal(runtime.state.writes, false); }
  finally { await runtime.stop(); }
});
test('actual Python runtime preflight failure is surfaced, not replaced by a fixture service', async () => {
  const runtime = new OwnedRuntime(() => {}, runtimeLaunch(repo, { RECRUITOPS_DESKTOP_RUNTIME_RESOURCES: repo, SystemRoot: process.env.SystemRoot }));
  runtime.start();
  try {
    await wait(() => runtime.state.status === 'failed');
    assert.equal(runtime.state.code, 'manifest_unavailable');
    assert.equal(runtime.origin, undefined);
  } finally { await runtime.stop(); }
});
test('missing interpreter resolves shutdown without hanging', async () => {
  const runtime = new OwnedRuntime(() => {}, { executable: path.join(__dirname, 'absent-interpreter.exe'), args: [], cwd: repo, env: {} });
  runtime.start();
  await wait(() => runtime.state.status === 'failed');
  await runtime.stop(); assert.equal(runtime.state.code, 'runtime_launch_failed');
});
test('JSONL refuses foreign origins, changed identity and unrequested writes before HTTP', () => {
  for (const url of ['http://127.0.0.1:8012', 'http://127.0.0.1:5433', 'http://localhost:50000', 'http://127.0.0.1:50000/path', 'https://example.com']) {
    const runtime = fixture('read-only');
    assert.throws(() => runtime.accept({ protocol: 1, sequence: 1, run_id: 'run', instance_id: 'instance', event: 'ready', stage: 'runtime', api_url: url, writes: false }));
  }
  const runtime = fixture('read-only');
  runtime.accept({ protocol: 1, sequence: 1, run_id: 'run', instance_id: 'one', event: 'opened', stage: 'instance' });
  assert.throws(() => runtime.accept({ protocol: 1, sequence: 2, run_id: 'run', instance_id: 'two', event: 'opened', stage: 'instance' }));
});
