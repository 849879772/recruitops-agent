const { test } = require('node:test');
const assert = require('node:assert/strict');
const http = require('node:http');
const { createHmac, randomInt } = require('node:crypto');
const { WebSocketServer } = require('ws');
const { DesktopBridge, reviewCommand } = require('../dist/bridge-client');
const wait = async predicate => { for (let n = 0; n < 150; n++) { if (predicate()) return; await new Promise(r => setTimeout(r, 20)); } throw new Error('timeout'); };
const dispatch = { protocol_version: 1, type: 'operation.dispatch', device_id: 'desktop-fixture', sequence: 1, operation_id: 'op-1', payload: {
  operation_id: 'op-1', operation: 'observe_application_status_page', command: { action: 'observe_application_page', selector_key: 'application_page', params: {},
    page_url: 'https://ats.example/applications', origin: 'https://ats.example', application_id: 'app-1', application_ids: ['app-1'] } } };
test('only fixed read operations with trusted operation/application binding are accepted', () => {
  assert.deepEqual(reviewCommand(dispatch), { url: 'https://ats.example/applications', ids: ['app-1'] });
  const bad = structuredClone(dispatch); bad.payload.command.action = 'execute_javascript';
  assert.throws(() => reviewCommand(bad), /ACTION_NOT_ALLOWED/);
  bad.payload.command.action = 'observe_application_page'; bad.payload.operation_id = 'other';
  assert.throws(() => reviewCommand(bad), /COMMAND_INVALID/);
});
test('owned WS HMAC handshake, ack/progress/result, duplicate dispatch and cancel', async () => {
  const server = http.createServer();
  const wss = new WebSocketServer({ noServer: true });
  let socket, origin, calls = 0, cancelled = false;
  const messages = [];
  const token = 'f'.repeat(64);
  server.on('upgrade', (req, raw, head) => {
    assert.equal(req.url, '/browser-bridge'); assert.equal(req.headers.authorization, 'Bearer ' + token); assert.equal(req.headers.origin, origin);
    wss.handleUpgrade(req, raw, head, ws => { socket = ws; wss.emit('connection', ws); });
  });
  wss.on('connection', ws => {
    const challenge = 'fixture-challenge-1234567890';
    ws.send(JSON.stringify({ protocol_version: 1, type: 'challenge', challenge }));
    ws.on('message', data => {
      const message = JSON.parse(data); messages.push(message);
      if (message.type === 'auth') {
        assert.equal(message.signature, createHmac('sha256', token).update(`recruitops-browser-bridge-v1\ndesktop-fixture\n${challenge}`).digest('hex'));
      }
      if (message.type === 'heartbeat') ws.send(JSON.stringify({ protocol_version: 1, type: 'heartbeat', ack: true }));
    });
  });
  for (let n = 0; n < 30; n++) {
    try { await new Promise((resolve, reject) => { const error = e => { server.removeListener('listening', resolve); reject(e); }; server.once('error', error); server.listen(randomInt(49152,65536), '127.0.0.1', () => { server.removeListener('error', error); resolve(); }); }); break; }
    catch (error) { if (error.code !== 'EADDRINUSE') throw error; }
  }
  origin = `http://127.0.0.1:${server.address().port}`;
  const runtime = { origin, state: { websocket: true }, authorization: () => 'Bearer ' + token, allows: target => target === origin.replace('http:', 'ws:') + '/browser-bridge' };
  const bridge = new DesktopBridge(runtime, 'desktop-fixture', async (_url, id, ids, signal) => {
    calls++;
    if (id === 'op-cancel') return new Promise(resolve => signal.addEventListener('abort', () => { cancelled = true; resolve({}); }));
    assert.deepEqual(ids, ['app-1']);
    return { type: 'result', operation_id: id, event_id: 'result-' + id, status: 'SUCCEEDED', result: {
      evidence_only: true, database_updated: false, application_ids: ids, page_url: 'https://ats.example/applications'
    } };
  }, () => {});
  try {
    bridge.connect(); await wait(() => bridge.status === 'connected');
    socket.send(JSON.stringify(dispatch)); await wait(() => messages.some(m => m.type === 'result'));
    assert.ok(messages.some(m => m.type === 'ack' && m.sequence === 1));
    assert.ok(messages.some(m => m.type === 'progress' && m.status === 'NAVIGATING'));
    socket.send(JSON.stringify(dispatch)); await wait(() => messages.filter(m => m.type === 'result').length === 2); assert.equal(calls, 1);
    const cancelWork = structuredClone(dispatch); cancelWork.operation_id = cancelWork.payload.operation_id = 'op-cancel'; cancelWork.sequence = 2;
    socket.send(JSON.stringify(cancelWork)); await wait(() => calls === 2);
    socket.send(JSON.stringify({ protocol_version: 1, type: 'operation.cancel', operation_id: 'op-cancel', device_id: 'desktop-fixture', sequence: 3 }));
    await wait(() => cancelled); await wait(() => messages.some(m => m.type === 'cancel'));
    assert.equal(messages.filter(m => m.type === 'result' && m.operation_id === 'op-cancel').length, 0);
    socket.send(JSON.stringify({ ...dispatch, device_id: 'foreign-device', sequence: 4 }));
    await wait(() => bridge.status === 'disconnected'); assert.equal(calls, 2);
  } finally {
    bridge.stop(); for (const client of wss.clients) client.terminate();
    await new Promise(resolve => wss.close(resolve)); await new Promise(resolve => server.close(resolve));
  }
});
