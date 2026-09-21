const { test } = require('node:test');
const assert = require('node:assert/strict');
const { EventEmitter } = require('node:events');
const { FillerFrameExecutor } = require('../dist/filler-frame-executor');

const WORLD = 'recruitops.desktop.filler.v1';
const NOT_HANDLED = Symbol('not handled');

function nativeFrame(id, url = 'https://form.example.test/apply') {
  const frame = {
    processId: id + 10,
    routingId: id + 20,
    frameTreeNodeId: id + 30,
    frameToken: `native-frame-${id}`,
    url,
    origin: new URL(url).origin,
    detached: false,
    parent: null,
    top: null,
    frames: [],
    framesInSubtree: [],
    isDestroyed: () => false,
  };
  frame.top = frame;
  frame.framesInSubtree = [frame];
  return frame;
}

function protocolFrame(id, url, parentId) {
  return {
    id,
    url,
    securityOrigin: new URL(url).origin,
    loaderId: `loader-${id}-1`,
    ...(parentId ? { parentId } : {}),
  };
}

function protocolTree(main, children = []) {
  const root = { frame: protocolFrame('cdp-main', main.url) };
  root.childFrames = children.map((child, index) => ({
    frame: protocolFrame(`cdp-child-${index + 1}`, child.url, root.frame.id),
  }));
  return { frameTree: root };
}

function frameFixture(childUrls = [], options = {}) {
  const main = nativeFrame(1);
  const children = childUrls.map((url, index) => {
    const child = nativeFrame(index + 2, url);
    child.parent = main;
    child.top = main;
    return child;
  });
  main.frames = children;
  main.framesInSubtree = [main, ...children];
  let tree = protocolTree(main, children);
  const debug = new MockDebugger({
    getTree: () => tree,
    context: options.context,
    suppressContextEvents: options.suppressContextEvents,
    onCommand: options.onCommand,
    initiallyAttached: options.initiallyAttached,
  });
  const wc = Object.assign(new EventEmitter(), {
    id: 7,
    debugger: debug,
    mainFrame: main,
    url: main.url,
    isDestroyed: () => false,
    isLoadingMainFrame: () => false,
    getURL() { return this.url; },
    electronWorldCalls: 0,
    async executeJavaScriptInIsolatedWorld() {
      this.electronWorldCalls++;
      throw new Error('filler must use its shared named CDP world');
    },
  });
  return {
    wc, main, children, debug,
    setTree(next) { tree = next; },
    treeWithLoader(frameId, loaderId) {
      const next = structuredClone(tree);
      const visit = entry => {
        if (entry.frame.id === frameId) entry.frame.loaderId = loaderId;
        for (const child of entry.childFrames || []) visit(child);
      };
      visit(next.frameTree);
      return next;
    },
  };
}

class MockDebugger extends EventEmitter {
  constructor(options = {}) {
    super();
    this.attached = !!options.initiallyAttached;
    this.getTree = options.getTree || (() => ({ frameTree: { frame: protocolFrame('cdp-main', 'https://form.example.test/apply') } }));
    this.contextOptions = options.context || {};
    this.suppressContextEvents = !!options.suppressContextEvents;
    this.onCommand = options.onCommand;
    this.attachCalls = 0;
    this.detachCalls = 0;
    this.commands = [];
    this.nextContextId = 700;
    this.contexts = new Map();
  }

  isAttached() { return this.attached; }

  attach(version) {
    assert.equal(version, '1.3');
    assert.equal(this.attached, false);
    this.attachCalls++;
    this.attached = true;
  }

  detach() {
    assert.equal(this.attached, true);
    this.detachCalls++;
    this.attached = false;
    this.emit('detach', {}, 'target closed');
  }

  externallyReplaceConnection() {
    if (this.attached) {
      this.attached = false;
      this.emit('detach', {}, 'replaced by DevTools');
    }
    this.attached = true;
  }

  emitContext(context) {
    this.emit('message', {}, 'Runtime.executionContextCreated', { context }, '');
  }

  findProtocolFrame(id) {
    let found;
    const visit = entry => {
      if (entry.frame.id === id) found = entry.frame;
      for (const child of entry.childFrames || []) visit(child);
    };
    visit(this.getTree().frameTree);
    return found;
  }

  async sendCommand(method, params = {}) {
    assert.equal(this.attached, true);
    this.commands.push({ method, params });
    const handled = this.onCommand && await this.onCommand(method, params, this);
    if (handled !== undefined && handled !== NOT_HANDLED) return handled;
    switch (method) {
      case 'Page.enable':
      case 'DOM.enable':
      case 'Runtime.releaseObjectGroup':
      case 'DOM.setFileInputFiles':
        return {};
      case 'Runtime.enable':
        if (!this.suppressContextEvents) for (const context of this.contexts.values()) this.emitContext(context);
        return {};
      case 'Page.getFrameTree':
        return this.getTree();
      case 'Page.createIsolatedWorld': {
        assert.equal(params.worldName, WORLD);
        assert.equal(params.grantUniveralAccess, false);
        const frame = this.findProtocolFrame(params.frameId);
        if (!frame) throw new Error('frame id not found');
        const key = `${frame.id}:${frame.loaderId}:${params.worldName}`;
        let context = this.contexts.get(key);
        const isNewContext = !context;
        if (!context) {
          context = {
            id: this.nextContextId++,
            name: this.contextOptions.name || params.worldName,
            origin: this.contextOptions.origin || frame.securityOrigin,
            auxData: {
              frameId: this.contextOptions.frameId || frame.id,
              isDefault: this.contextOptions.isDefault ?? false,
              type: this.contextOptions.type || 'isolated',
            },
          };
          this.contexts.set(key, context);
        }
        if (!this.suppressContextEvents && isNewContext) this.emitContext(context);
        return { executionContextId: context.id };
      }
      case 'Runtime.evaluate':
        if (params.returnByValue === false) {
          return { result: { type: 'object', subtype: 'node', className: 'HTMLInputElement', objectId: 'remote-input-41' } };
        }
        return { result: { type: 'object', value: { ok: true, code: 'fixture' } } };
      case 'Runtime.callFunctionOn':
        return { result: { type: 'boolean', value: true } };
      default:
        throw new Error(`unexpected CDP command: ${method}`);
    }
  }
}

test('execute uses one named CDP isolated context for top-frame calls', async () => {
  const { wc, main, debug } = frameFixture();
  const executor = new FillerFrameExecutor();
  const first = await executor.execute(wc, main, 'scan');
  const second = await executor.execute(wc, main, 'fill');
  const evaluations = debug.commands.filter(item => item.method === 'Runtime.evaluate');
  const worlds = debug.commands.filter(item => item.method === 'Page.createIsolatedWorld');

  assert.deepEqual(first, { ok: true, code: 'fixture' });
  assert.deepEqual(second, first);
  assert.equal(evaluations[0].params.expression, 'scan');
  assert.equal(evaluations[0].params.returnByValue, true);
  assert.equal(evaluations[0].params.awaitPromise, true);
  assert.equal(evaluations[0].params.contextId, evaluations[1].params.contextId);
  assert.equal(worlds.length, 2);
  assert.equal(worlds[0].params.worldName, WORLD);
  assert.equal(wc.electronWorldCalls, 0);
  assert.equal(debug.attachCalls, 2);
  assert.equal(debug.detachCalls, 2);
  assert.equal(debug.isAttached(), false);
});

test('child frame execution binds the unique URL and origin to its CDP frame id', async () => {
  const { wc, children, debug } = frameFixture(['https://identity.example.test/form']);
  await new FillerFrameExecutor().execute(wc, children[0], 'child-scan');
  const world = debug.commands.find(item => item.method === 'Page.createIsolatedWorld');
  const evaluation = debug.commands.find(item => item.method === 'Runtime.evaluate');

  assert.equal(world.params.frameId, 'cdp-child-1');
  assert.equal(evaluation.params.expression, 'child-scan');
  assert.equal(evaluation.params.contextId, 700);
});

test('duplicate frame URLs are rejected before creating or evaluating a context', async () => {
  const url = 'https://identity.example.test/form';
  const { wc, children, debug } = frameFixture([url, url]);
  await assert.rejects(new FillerFrameExecutor().execute(wc, children[0], 'must-not-run'), /frame_ambiguous/);
  assert.equal(debug.commands.some(item => item.method === 'Page.createIsolatedWorld'), false);
  assert.equal(debug.commands.some(item => item.method === 'Runtime.evaluate'), false);
});

test('execution context must match the expected name, origin, frame id and isolated type', async () => {
  const { wc, main, debug } = frameFixture([], { context: { name: 'other-world' } });
  await assert.rejects(new FillerFrameExecutor().execute(wc, main, 'must-not-run'), /context_mismatch/);
  assert.equal(debug.commands.some(item => item.method === 'Runtime.evaluate'), false);
});

test('missing executionContextCreated evidence fails closed', async () => {
  const { wc, main, debug } = frameFixture([], { suppressContextEvents: true });
  await assert.rejects(new FillerFrameExecutor().execute(wc, main, 'must-not-run'), /context_unproven/);
  assert.equal(debug.commands.some(item => item.method === 'Runtime.evaluate'), false);
});

test('assignFile passes the original remote input objectId and releases its object group', async () => {
  const { wc, main, debug } = frameFixture();
  await new FillerFrameExecutor().assignFile(wc, main, 'claim-input', 'C:\\mock\\resume.pdf', () => true);
  const evaluation = debug.commands.find(item => item.method === 'Runtime.evaluate');
  const setFiles = debug.commands.find(item => item.method === 'DOM.setFileInputFiles');
  const verification = debug.commands.find(item => item.method === 'Runtime.callFunctionOn');
  const release = debug.commands.find(item => item.method === 'Runtime.releaseObjectGroup');

  assert.equal(evaluation.params.expression, 'claim-input');
  assert.equal(evaluation.params.returnByValue, false);
  assert.equal(evaluation.params.objectGroup, release.params.objectGroup);
  assert.equal(verification.params.objectId, 'remote-input-41');
  assert.equal(setFiles.params.objectId, 'remote-input-41');
  assert.deepEqual(setFiles.params.files, ['C:\\mock\\resume.pdf']);
  assert.equal(Object.hasOwn(setFiles.params, 'nodeId'), false);
  assert.equal(debug.commands.some(item => item.method === 'DOM.querySelector'), false);
  assert.equal(debug.commands.some(item => item.method === 'DOM.getDocument'), false);
});

test('assignFile rejects a non-input remote object and releases it without touching a file', async () => {
  const { wc, main, debug } = frameFixture([], {
    onCommand(method) {
      if (method === 'Runtime.evaluate') {
        return { result: { type: 'object', subtype: 'node', className: 'HTMLDivElement', objectId: 'wrong-object' } };
      }
      return NOT_HANDLED;
    },
  });
  await assert.rejects(new FillerFrameExecutor().assignFile(wc, main, 'wrong-target', 'C:\\mock\\resume.pdf', () => true), /upload_target_invalid/);
  assert.equal(debug.commands.some(item => item.method === 'DOM.setFileInputFiles'), false);
  assert.equal(debug.commands.some(item => item.method === 'Runtime.releaseObjectGroup'), true);
});

test('assignFile rejects an input not bound to the target document', async () => {
  const { wc, main, debug } = frameFixture([], {
    onCommand(method) {
      if (method === 'Runtime.callFunctionOn') return { result: { type: 'boolean', value: false } };
      return NOT_HANDLED;
    },
  });
  await assert.rejects(new FillerFrameExecutor().assignFile(wc, main, 'foreign-input', 'C:\\mock\\resume.pdf', () => true), /upload_document_mismatch/);
  assert.equal(debug.commands.some(item => item.method === 'DOM.setFileInputFiles'), false);
  assert.equal(debug.commands.some(item => item.method === 'Runtime.releaseObjectGroup'), true);
});

test('valid callback is required before debugger access and again before assignment', async () => {
  const invalid = frameFixture();
  await assert.rejects(new FillerFrameExecutor().assignFile(invalid.wc, invalid.main, 'claim', 'C:\\mock\\resume.pdf', () => false), /operation_invalid/);
  assert.equal(invalid.debug.attachCalls, 0);

  let current = true;
  const changing = frameFixture([], {
    onCommand(method) {
      if (method === 'Runtime.callFunctionOn') current = false;
      return NOT_HANDLED;
    },
  });
  await assert.rejects(new FillerFrameExecutor().assignFile(changing.wc, changing.main, 'claim', 'C:\\mock\\resume.pdf', () => current), /operation_invalid/);
  assert.equal(changing.debug.commands.some(item => item.method === 'DOM.setFileInputFiles'), false);
  assert.equal(changing.debug.commands.some(item => item.method === 'Runtime.releaseObjectGroup'), true);
});

test('same-URL navigation to a new document loader aborts before file assignment', async () => {
  let fixture;
  fixture = frameFixture([], {
    onCommand(method, _params, debug) {
      if (method === 'Runtime.callFunctionOn') {
        fixture.setTree(fixture.treeWithLoader('cdp-main', 'loader-main-after-navigation'));
        return { result: { type: 'boolean', value: true } };
      }
      return NOT_HANDLED;
    },
  });
  await assert.rejects(new FillerFrameExecutor().assignFile(fixture.wc, fixture.main, 'claim', 'C:\\mock\\resume.pdf', () => true), /document_changed/);
  assert.equal(fixture.debug.commands.some(item => item.method === 'DOM.setFileInputFiles'), false);
  assert.equal(fixture.debug.commands.some(item => item.method === 'Runtime.releaseObjectGroup'), true);
});

test('cancel evaluation runs while fill awaits a promise and the owned debugger is refcounted', { timeout: 3000 }, async () => {
  let resumeFill;
  let markFillStarted;
  let markCancelStarted;
  const fillStarted = new Promise(resolve => { markFillStarted = resolve; });
  const cancelStarted = new Promise(resolve => { markCancelStarted = resolve; });
  const fixture = frameFixture([], {
    onCommand: (method, params) => {
      if (method !== 'Runtime.evaluate') return NOT_HANDLED;
      if (params.expression === 'fill') {
        markFillStarted();
        return new Promise(resolve => {
          resumeFill = () => resolve({ result: { type: 'object', value: { ok: true } } });
        });
      }
      if (params.expression === 'cancel') {
        markCancelStarted();
        return { result: { type: 'object', value: { ok: true } } };
      }
      return NOT_HANDLED;
    },
  });
  const executor = new FillerFrameExecutor();
  const fill = executor.execute(fixture.wc, fixture.main, 'fill');
  await fillStarted;
  assert.equal(fixture.debug.isAttached(), true);
  assert.equal(fixture.debug.attachCalls, 1);

  const cancel = executor.execute(fixture.wc, fixture.main, 'cancel');
  await cancelStarted;
  await cancel;
  assert.equal(fixture.debug.isAttached(), true);
  assert.equal(fixture.debug.detachCalls, 0);
  assert.deepEqual(fixture.debug.commands.filter(item => item.method === 'Runtime.evaluate').map(item => item.params.expression), ['fill', 'cancel']);

  resumeFill();
  await fill;
  assert.equal(fixture.debug.detachCalls, 1);
  assert.equal(fixture.debug.isAttached(), false);
});

test('a pre-attached debugger remains owned by its external caller', async () => {
  const { wc, main, debug } = frameFixture([], { initiallyAttached: true });
  await new FillerFrameExecutor().execute(wc, main, 'scan');
  assert.equal(debug.attachCalls, 0);
  assert.equal(debug.detachCalls, 0);
  assert.equal(debug.isAttached(), true);
});

test('executor does not detach a replacement connection after an external detach', async () => {
  let fixture;
  fixture = frameFixture([], {
    onCommand(method, _params, debug) {
      if (method === 'Runtime.evaluate') debug.externallyReplaceConnection();
      return NOT_HANDLED;
    },
  });
  await assert.rejects(new FillerFrameExecutor().execute(fixture.wc, fixture.main, 'scan'), /context_mismatch/);
  assert.equal(fixture.debug.attachCalls, 1);
  assert.equal(fixture.debug.detachCalls, 0);
  assert.equal(fixture.debug.isAttached(), true);
});
