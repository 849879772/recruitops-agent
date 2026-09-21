'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const vm = require('node:vm');
const root = path.join(__dirname, 'fixtures', 'filler-parity');
const read = name => fs.readFileSync(path.join(root, name), 'utf8');
const audit = JSON.parse(read('audit.json'));
const scenarios = JSON.parse(read('scenarios.json'));
const profile = JSON.parse(read('profile.json'));
const adapter = require('../../../packages/desktop_filler/index.cjs');

test('FP-BASE: anonymous profile uses public parser and preserves three records', () => {
  assert.deepEqual(adapter.parseProfileJson(read('profile.json')), profile);
  assert.equal(profile.education.length, 3);
  assert.equal(profile.basic.fullName, 'Synthetic Candidate');
  assert.equal(profile.basic.email, 'candidate@example.test');
  assert.equal(profile.basic.phone, '00000000000');
  assert.throws(() => adapter.parseProfileJson('{"__proto__":{}}'), /profile_invalid/);
  assert.throws(() => adapter.parseProfileJson('{'), /profile_invalid/);
});

test('FP-AUDIT: pinned baseline and field inventory are explicit, not a license grant', () => {
  assert.equal(audit.version, '0.11.5');
  assert.equal(audit.redistribution, 'unconfirmed-do-not-bundle');
  assert.equal(Object.keys(audit.hashes).length, 8);
  for (const digest of Object.values(audit.hashes)) assert.match(digest, /^[a-f0-9]{64}$/);
  assert.equal(Object.values(audit.profileFields).flat().length, 65);
  assert.equal(audit.repeaterSections.length, 11);
});

test('FP-PRIVACY: public fixture artifacts contain only synthetic content and reserved origins', () => {
  const files = fs.readdirSync(root);
  assert.deepEqual(files.sort(), ['audit.json', 'controls.html', 'frames.html', 'profile.json', 'scenarios.json']);
  for (const file of files) {
    const text = read(file);
    assert.doesNotMatch(text, /\b[A-Za-z]:[\\/]|file:\/\/|\\\\[^\s]+|Bearer\s+\S+|-----BEGIN|data:application\/pdf/i, file);
    assert.doesNotMatch(text, /localhost|127\.0\.0\.1|:8012|:5433/, file);
    for (const match of text.matchAll(/https?:\/\/[^\s"<>]+/g)) {
      assert.ok(new URL(match[0]).hostname.endsWith('.example.test'), file);
    }
  }
});

test('FP-CONTROLS: authored form exposes safety and routing targets without external scripts', () => {
  const controls = read('controls.html');
  for (const id of ['name','summary','consent','degree','date','city','password','otp','hidden','disabled','resume','portrait','education','add-education']) {
    assert.ok(controls.includes(`id="${id}"`), id);
  }
  assert.match(controls, /event\.preventDefault\(\)/);
  assert.doesNotMatch(controls, /fetch\(|XMLHttpRequest|<script[^>]+src=/);
  for (const id of ['same-origin','cross-origin','nested','restricted']) assert.ok(read('frames.html').includes(`id="${id}"`));
  assert.match(read('frames.html'), /sandbox srcdoc/);
});

test('FP-SPEC: each T04-T16 task has distinct positive, rejection and recovery oracles', () => {
  assert.equal(scenarios.evidenceLevel, 'acceptance-specification-not-product-pass');
  assert.equal(scenarios.cases.length, 39);
  assert.equal(new Set(scenarios.cases.map(item => item.id)).size, 39);
  for (let n = 4; n <= 16; n++) {
    const task = `T${String(n).padStart(2, '0')}`;
    const cases = scenarios.cases.filter(item => item.task === task);
    assert.deepEqual(cases.map(item => item.kind).sort(), ['negative','positive','recovery']);
    for (const item of cases) {
      assert.match(item.id, new RegExp(`^FP-${task}-[PNR]$`));
      for (const key of ['given','action','expect']) assert.ok(item[key].length > 15, `${item.id}:${key}`);
    }
  }
});

// These are intentionally TODO, not mocked product successes. Owners attach
// actual service/Electron assertions to these IDs after the T03 contract lands.
for (const item of scenarios.cases) test.todo(`${item.id}: ${item.expect}`);

const localSource = process.env.RECRUITOPS_TEST_LOCAL_FILLER_DIR;
test('FP-LOCAL-AUDIT: explicit source opt-in matches hashes and complete editor field inventory', {skip: !localSource}, () => {
  const source = {};
  for (const [name, digest] of Object.entries(audit.hashes)) {
    // Fixed allowlist only. Never resolve manifest scripts or read default data.
    let bytes;
    try { bytes = fs.readFileSync(path.join(localSource, name)); }
    catch { assert.fail(`authorized source unavailable: ${name}`); }
    assert.equal(crypto.createHash('sha256').update(bytes).digest('hex'), digest, `source drift: ${name}`);
    source[name] = bytes.toString('utf8');
  }
  assert.equal(JSON.parse(source['manifest.json']).version, audit.version);
  const basic = [...source['sidepanel.html'].matchAll(/name="basic\.([^"]+)"/g)].map(m => m[1]);
  assert.deepEqual(basic, audit.profileFields.basic);
  const scalar = [...source['sidepanel.html'].matchAll(/(?:textarea|input) name="([^".]+)"/g)].map(m => m[1]);
  assert.deepEqual(scalar, audit.profileFields.scalar);
  for (const group of ['education','projects','awards','publications','certificates']) {
    const keys = [...source['sidepanel.js'].matchAll(/data-path="([a-z]+)\.\$\{index\}\.([^"]+)"/g)]
      .filter(m => m[1] === group).map(m => m[2]);
    assert.deepEqual(keys, audit.profileFields[group]);
  }
});

test('FP-LOCAL-RULES: opt-in original pure rules route duplicate names and count anonymous experiences', {skip: !localSource}, () => {
  const context = vm.createContext({}, {codeGeneration:{strings:false, wasm:false}});
  for (const name of ['core.js','repeater-engine.js','frame-routing.js']) {
    let source;
    try { source = fs.readFileSync(path.join(localSource, name)); }
    catch { assert.fail(`authorized source unavailable: ${name}`); }
    assert.equal(crypto.createHash('sha256').update(source).digest('hex'), audit.hashes[name], `source drift: ${name}`);
    vm.runInContext(source.toString('utf8'), context, {timeout:1000, filename:name});
  }
  context.profile = JSON.parse(JSON.stringify(profile));
  const result = JSON.parse(vm.runInContext(`JSON.stringify({
    counts: ResumeRepeaterEngine.desiredCounts(profile),
    routed: ResumeFrameRouting.buildFrameRequests([0, 2], {type:'RESUME_FILL', assignments:[
      {frameId:0,fieldId:'0:name',value:'Synthetic A'},
      {frameId:2,fieldId:'2:name',value:'Synthetic B'}
    ]}),
    fullName: ResumeFillerCore.buildResumeValues(profile).fullName
  })`, context, {timeout:1000}));
  assert.equal(result.counts.education, 3);
  assert.equal(result.fullName, profile.basic.fullName);
  assert.deepEqual(result.routed.map(item => item.frameId), [0, 2]);
  assert.deepEqual(result.routed.map(item => item.message.assignments[0].fieldId), ['name','name']);
  assert.deepEqual(result.routed.map(item => item.message.assignments[0].value), ['Synthetic A','Synthetic B']);
});
