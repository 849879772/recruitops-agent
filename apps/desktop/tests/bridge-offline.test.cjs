const { test } = require('node:test');
const assert = require('node:assert/strict');
const { EventEmitter } = require('node:events');
const { DesktopBridge, safeReviewErrorCode } = require('../dist/bridge-client');
const { ReviewObservationError, reviewDiagnosticSummary } = require('../dist/review-readiness');

const wait = async predicate => {
  for (let n = 0; n < 200; n++) {
    if (predicate()) return;
    await new Promise(resolve => setTimeout(resolve, 5));
  }
  throw new Error('offline bridge fixture timed out');
};

class MockSocket extends EventEmitter {
  readyState = 1;
  sent = [];
  send(value) { this.sent.push(JSON.parse(value)); }
  input(value) { this.emit('message', Buffer.from(JSON.stringify(value))); }
  close(code = 1000) {
    if (this.readyState === 3) return;
    this.readyState = 3;
    this.emit('close', code);
  }
  terminate() { this.close(1006); }
}

function fixture(review, options = {}) {
  const sockets = [];
  const runtime = { origin: 'http://owned-runtime.invalid', state: { websocket: true },
    authorization: () => `Bearer ${'f'.repeat(64)}`, allows: () => true };
  const bridge = new DesktopBridge(runtime, 'desktop-fixture', review, () => {}, {
    ...options, socketFactory: () => { const socket = new MockSocket(); sockets.push(socket); return socket; }
  });
  const authenticate = socket => {
    socket.input({ protocol_version: 1, type: 'challenge', challenge: 'offline-fixture-challenge-1234' });
    socket.input({ protocol_version: 1, type: 'heartbeat', ack: true });
  };
  bridge.connect();
  authenticate(sockets[0]);
  return { bridge, sockets, socket: sockets[0], authenticate };
}

function dispatch(operationId, sequence) {
  return { protocol_version: 1, type: 'operation.dispatch', device_id: 'desktop-fixture', sequence, operation_id: operationId,
    payload: { operation_id: operationId, operation: 'observe_application_status_page', command: {
      action: 'observe_application_page', selector_key: 'application_page', page_url: 'https://ats.example/applications',
      origin: 'https://ats.example', application_id: 'app-1', application_ids: ['app-1']
    } } };
}

function result(operationId) {
  return { type: 'result', operation_id: operationId, event_id: `result-${operationId}`, status: 'SUCCEEDED',
    result: { evidence_only: true, database_updated: false, application_ids: ['app-1'],
      page_url: 'https://ats.example/applications' } };
}

test('failed review transports its sanitized last observation for persisted diagnostics', async()=>{
  const summary=reviewDiagnosticSummary({result:{page:{text:'private@example.test'},application_records:[],
    diagnostics:{iframeCount:2,frameCount:3,skippedFrameCount:2}}});
  const {bridge,socket}=fixture(async()=>{throw new ReviewObservationError('browser_readiness_timeout',summary);});
  try {
    socket.input(dispatch('diagnostic-failure',1));
    await wait(()=>socket.sent.some(message=>message.type==='result'));
    const output=socket.sent.find(message=>message.type==='result');
    assert.equal(output.error_code,'DESKTOP_READINESS_TIMEOUT');
    assert.deepEqual(output.result.last_observation,summary);
    assert.equal(output.result.database_updated,false);
    assert.ok(!JSON.stringify(output).includes('private@example.test'));
  } finally {bridge.stop();}
});

test('accepts and drains a ten-operation wave with four active and FIFO queueing', async () => {
  const calls = [];
  let active = 0;
  let maxActive = 0;
  const { bridge, socket } = fixture((_url, id) => new Promise(resolve => {
    active++;
    maxActive = Math.max(maxActive, active);
    calls.push({ id, finish: () => { active--; resolve(result(id)); } });
  }));
  try {
    for (let n = 1; n <= 10; n++) socket.input(dispatch(`op-${n}`, n));
    await wait(() => calls.length === 4);
    assert.equal(socket.sent.filter(message => message.type === 'progress' && message.status === 'DISPATCHED').length, 6);
    assert.deepEqual(calls.map(call => call.id), ['op-1', 'op-2', 'op-3', 'op-4']);

    socket.input(dispatch('op-1', 11));
    assert.equal(calls.length, 4, 'an in-flight duplicate must not execute again');
    for (let n = 0; n < 10; n++) {
      calls[n].finish();
      if (n < 9) await wait(() => calls.length === Math.min(10, n + 5));
    }
    await wait(() => socket.sent.filter(message => message.type === 'result').length === 10);
    socket.input(dispatch('op-1', 12));
    assert.equal(calls.length, 10, 'a completed duplicate must not execute again');
    assert.equal(socket.sent.filter(message => message.type === 'result' && message.operation_id === 'op-1').length, 2);
    assert.equal(maxActive, 4);
  } finally { bridge.stop(); }
});

test('cancels queued work without later executing it, including a duplicate dispatch', async () => {
  let finishActive;
  const calls = [];
  const { bridge, socket } = fixture((_url, id) => {
    calls.push(id);
    return new Promise(resolve => { if (id === 'active') finishActive = () => resolve(result(id)); else resolve(result(id)); });
  }, { maxActive: 1, queueWaitMs: 300 });
  try {
    socket.input(dispatch('active', 1));
    socket.input(dispatch('queued', 2));
    socket.input({ protocol_version: 1, type: 'operation.cancel', device_id: 'desktop-fixture', sequence: 3, operation_id: 'queued' });
    await wait(() => socket.sent.some(message => message.type === 'cancel' && message.operation_id === 'queued'));
    finishActive();
    await wait(() => socket.sent.some(message => message.type === 'result' && message.operation_id === 'active'));
    socket.input(dispatch('queued', 4));
    await wait(() => socket.sent.filter(message => message.type === 'ack' && message.operation_id === 'queued').length === 2);
    assert.deepEqual(calls, ['active']);
    assert.equal(socket.sent.some(message => message.type === 'result' && message.operation_id === 'queued'), false);
  } finally { bridge.stop(); }
});

test('bounds queue wait and rejects excess work with stable codes', async () => {
  const calls = [];
  const { bridge, socket } = fixture((_url, id, _ids, signal) => {
    calls.push(id);
    return new Promise((_resolve, reject) => signal.addEventListener('abort', () => reject(new Error('browser_cancelled')), { once: true }));
  }, { maxActive: 1, maxQueued: 1, queueWaitMs: 25, operationBudgetMs: 500 });
  try {
    socket.input(dispatch('active', 1));
    socket.input(dispatch('expires', 2));
    socket.input(dispatch('rejected', 3));
    await wait(() => socket.sent.some(message => message.type === 'result' && message.operation_id === 'expires'));
    const expired = socket.sent.find(message => message.type === 'result' && message.operation_id === 'expires');
    const rejected = socket.sent.find(message => message.type === 'result' && message.operation_id === 'rejected');
    assert.equal(expired.error_code, 'DESKTOP_REVIEW_QUEUE_TIMEOUT');
    assert.equal(rejected.error_code, 'DESKTOP_REVIEW_QUEUE_FULL');
    assert.deepEqual(calls, ['active']);
  } finally { bridge.stop(); }
});

test('disconnect aborts active and queued work and replay does not call the reviewer again', async () => {
  let aborted = 0;
  const calls = [];
  const { bridge, sockets, socket, authenticate } = fixture((_url, id, _ids, signal) => {
    calls.push(id);
    return new Promise((_resolve, reject) => signal.addEventListener('abort', () => { aborted++; reject(new Error('browser_cancelled')); }, { once: true }));
  }, { maxActive: 1 });
  try {
    socket.input(dispatch('interrupted', 1));
    socket.input(dispatch('queued-on-old-socket', 2));
    socket.close(1006);
    await wait(() => aborted === 1);
    bridge.connect();
    const reconnected = sockets[1];
    authenticate(reconnected);
    reconnected.input(dispatch('interrupted', 3));
    reconnected.input(dispatch('queued-on-old-socket', 4));
    await wait(() => reconnected.sent.some(message => message.type === 'result' && message.operation_id === 'interrupted'));
    const replay = reconnected.sent.find(message => message.type === 'result' && message.operation_id === 'interrupted');
    await wait(() => reconnected.sent.some(message => message.type === 'result' && message.operation_id === 'queued-on-old-socket'));
    assert.equal(replay.error_code, 'DESKTOP_REVIEW_DISCONNECTED');
    assert.equal(reconnected.sent.find(message => message.type === 'result' && message.operation_id === 'queued-on-old-socket').error_code,
      'DESKTOP_REVIEW_DISCONNECTED');
    assert.deepEqual(calls, ['interrupted']);
  } finally { bridge.stop(); }
});

test('maps known lowercase browser errors and never forwards unknown messages', async () => {
  const { bridge, socket } = fixture(async () => { throw new Error('browser_readiness_timeout'); });
  try {
    socket.input(dispatch('timeout', 1));
    await wait(() => socket.sent.some(message => message.type === 'result' && message.operation_id === 'timeout'));
    const outcome = socket.sent.find(message => message.type === 'result' && message.operation_id === 'timeout');
    assert.equal(outcome.error_code, 'DESKTOP_READINESS_TIMEOUT');
    assert.equal(safeReviewErrorCode(new Error('private page text')), 'DESKTOP_OBSERVATION_FAILED');
    assert.equal(safeReviewErrorCode('browser_load_timeout'), 'DESKTOP_LOAD_TIMEOUT');
  } finally { bridge.stop(); }
});

test('binds the returned page evidence to the dispatched origin, URL envelope and application IDs', async () => {
  const route = 'https://ats.example/applications#/app/application_center';
  const { bridge, socket } = fixture(async (_url, id) => {
    if (id === 'route-update') return { ...result(id), result: { ...result(id).result, page_url: route,
      page: { page_url: route, origin: 'https://ats.example', path: '/applications' } } };
    if (id === 'foreign-origin') return { ...result(id), result: { ...result(id).result,
      page_url: 'https://foreign.example/applications' } };
    if (id === 'wrong-page') return { ...result(id), result: { ...result(id).result,
      page: { page_url: 'https://ats.example/other', origin: 'https://ats.example', path: '/other' } } };
    return { ...result(id), result: { ...result(id).result, application_ids: ['other-app'] } };
  });
  try {
    for (const [id, sequence] of [['route-update', 1], ['foreign-origin', 2], ['wrong-page', 3], ['wrong-binding', 4]]) {
      socket.input(dispatch(id, sequence));
    }
    await wait(() => socket.sent.filter(message => message.type === 'result').length === 4);
    const outcome = id => socket.sent.find(message => message.type === 'result' && message.operation_id === id);
    assert.equal(outcome('route-update').status, 'SUCCEEDED');
    assert.equal(outcome('route-update').result.page_url, route);
    assert.deepEqual(outcome('route-update').result.navigation_binding, {
      source: 'desktop_owned_navigation_v1', requested_page_url: 'https://ats.example/applications', observed_page_url: route,
    });
    assert.equal(outcome('foreign-origin').error_code, 'DESKTOP_NAVIGATION_CHANGED');
    assert.equal(outcome('wrong-page').error_code, 'DESKTOP_OBSERVATION_FAILED');
    assert.equal(outcome('wrong-binding').error_code, 'DESKTOP_INVALID_BINDING');
  } finally { bridge.stop(); }
});

test('applies the per-operation deadline and aborts the reviewer', async () => {
  let aborted = false;
  const { bridge, socket } = fixture((_url, _id, _ids, signal) => new Promise((_resolve, reject) => {
    signal.addEventListener('abort', () => { aborted = true; reject(new Error('browser_cancelled')); }, { once: true });
  }), { operationBudgetMs: 25 });
  try {
    socket.input(dispatch('deadline', 1));
    await wait(() => socket.sent.some(message => message.type === 'result' && message.operation_id === 'deadline'));
    const outcome = socket.sent.find(message => message.type === 'result' && message.operation_id === 'deadline');
    assert.equal(outcome.error_code, 'DESKTOP_REVIEW_TIMEOUT');
    assert.equal(aborted, true);
  } finally { bridge.stop(); }
});

test('a duplicate remains non-executable after its cached result is evicted', async () => {
  let calls = 0;
  const { bridge, socket } = fixture(async (_url, id) => { calls++; return result(id); }, { maxQueued: 300 });
  try {
    for (let n = 0; n < 260; n++) {
      const id = `history-${n}`;
      socket.input(dispatch(id, n + 1));
    }
    await wait(() => calls === 260 && socket.sent.filter(message => message.type === 'result').length === 260);
    assert.equal(calls, 260);
    socket.input(dispatch('history-0', 261));
    await wait(() => socket.sent.filter(message => message.type === 'result' && message.operation_id === 'history-0').length === 2);
    const replay = socket.sent.filter(message => message.type === 'result' && message.operation_id === 'history-0')[1];
    assert.equal(replay.error_code, 'DESKTOP_REVIEW_RESULT_EXPIRED');
    assert.equal(calls, 260);
  } finally { bridge.stop(); }
});
