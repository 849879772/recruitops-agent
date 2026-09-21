'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const api = require('../../packages/desktop_filler/index.cjs');
const bundle = api.loadBundledFiller();
const route = { instanceId: 'fixture', tabId: 'tab', frameId: 'top', documentId: 'document', profileVersion: '1', href: 'https://fixture.invalid/form' };
const scanId = '00000000-0000-0000-0000-000000000000';

test('frame aggregation retains failures and disambiguates identical local field IDs', () => {
  const child = { ...route, frameId: 'child', allowSubframe: true };
  const scans = [route, child].map(r => ({ route: r, status: 'scanned', scan: { ok: true, scanId, route: r, matches: [{ fieldId: 'rf-1' }] } }));
  const result = api.aggregateFrameScans([...scans, { route: { ...route, frameId: 'restricted' }, status: 'sandboxed' }]);
  assert.equal(result.ok, false); assert.equal(result.partial, true);
  assert.equal(result.failures[0].code, 'filler_frame_sandboxed');
  assert.notEqual(result.fields[0].selectionId, result.fields[1].selectionId);
  assert.equal(api.aggregateFrameScans([]).ok, false);
  assert.throws(() => api.aggregateFrameScans([scans[0], scans[0]]), /frame_results_invalid/);
  assert.throws(() => api.aggregateFrameScans([{ ...scans[0], route: child }]), /frame_results_invalid/);
  for (const status of ['blocked', 'detached', 'unreachable']) {
    const failed = api.aggregateFrameScans([{ route, status }]);
    assert.equal(failed.ok, false); assert.equal(failed.partial, false);
    assert.equal(failed.failures[0].code, 'filler_frame_' + status);
  }
});

test('reject malformed routes, arbitrary request scripts, paths and unsupported attachments', () => {
  for (const invalid of [{}, { ...route, href: 'file:///private' }, { ...route, allowSubframe: 'yes' }, { ...route, script: 'x' }]) {
    assert.throws(() => api.buildScanScript(bundle, {}, invalid), /frame_context_invalid/);
  }
  assert.throws(() => api.buildPrepareScript(bundle, { scanId, sectionIds: ['x'], confirmed: true, script: 'x' }), /confirmation_required/);
  const request = { scanId, fieldId: 'rf-1', attachment: { id: 'file', name: 'synthetic.pdf', size: 4, mime: 'application/pdf' }, confirmed: true };
  for (const attachment of [{ ...request.attachment, name: '../private.pdf' }, { ...request.attachment, size: 30 * 1024 * 1024 },
    { ...request.attachment, path: '/private' }, { ...request.attachment, name: 'script.exe' }, { ...request.attachment, mime: 'text/javascript' }]) {
    assert.throws(() => api.buildUploadScript(bundle, { ...request, attachment }), /attachment_invalid/);
  }
});
