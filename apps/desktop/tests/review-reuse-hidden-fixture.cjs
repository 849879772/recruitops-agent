'use strict';
const {BrowserWindow} = require('electron');
BrowserWindow.prototype.show = function () {};
BrowserWindow.prototype.focus = function () {};
// This isolated runtime fixture does not implement the production bridge WS.
// Bridge auth/cancel/reconnect is covered by bridge-offline.test.cjs instead.
require('../dist/bridge-client').DesktopBridge.prototype.connect = function () {};
globalThis.reuseFixture = {...require('../dist/main'), ...require('../dist/review-vision'),
  pixelsHash: image => require('node:crypto').createHash('sha256').update(image.toPNG()).digest('hex'), visionRequests: []};
const originalFetch = globalThis.fetch;
globalThis.fetch = async (url, request) => {
  if (!String(url).endsWith('/api/browser/vision')) return originalFetch(url, request);
  const payload = JSON.parse(request.body);
  globalThis.reuseFixture.visionRequests.push({operationId: payload.operation_id, imageCount: payload.image_data_urls.length});
  await globalThis.reuseFixture.onVisionRequest?.(request);
  return {ok: true, json: async () => ({text: 'Software engineer 笔试中', confidence: .95,
    model: 'synthetic-local-fixture', image_sha256: 'a'.repeat(64)})};
};
