import type { WebContents, WebFrameMain } from 'electron';

const FILLER_WORLD_NAME = 'recruitops.desktop.filler.v1';
const CONTEXT_EVENT_TIMEOUT_MS = 1000;
const ATTACH_PROTOCOL_VERSION = '1.3';
let objectGroupSequence = 0;

type DebuggerPort = WebContents['debugger'];
type FrameIdentity = {
  processId: number;
  routingId: number;
  frameTreeNodeId: number;
  frameToken: string;
  url: string;
  origin: string;
};
type FrameSnapshot = {
  wc: WebContents;
  frame: WebFrameMain;
  main: FrameIdentity;
  target: FrameIdentity;
  isMainFrame: boolean;
};
type ProtocolFrame = { id: string; url: string; securityOrigin: string; loaderId: string; root: boolean };
type RuntimeContext = {
  id: number;
  name: string;
  origin: string;
  auxData?: { frameId?: string; isDefault?: boolean; type?: string };
};
type ContextWaiter = {
  resolve: (context: RuntimeContext) => void;
  reject: (error: Error) => void;
  timer: ReturnType<typeof setTimeout>;
};
type DebuggerState = {
  debugger: DebuggerPort;
  tail: Promise<void>;
  activeLeases: number;
  ownsAttachment: boolean;
  ownedGeneration: number;
  detachGeneration: number;
  runtimeEnabledGeneration: number;
  contexts: Map<number, RuntimeContext>;
  waiters: Map<number, Set<ContextWaiter>>;
  onMessage: (event: unknown, method: string, params: any, sessionId: string) => void;
  onDetach: () => void;
  onDestroyed: () => void;
};
type ContextBinding = { frameId: string; loaderId: string; context: RuntimeContext };
type SessionLease = {
  debugger: DebuggerPort;
  state: DebuggerState;
  binding: ContextBinding;
  release: () => Promise<void>;
};

const debuggerStates = new WeakMap<WebContents, DebuggerState>();

function fail(code: string): never {
  throw new Error(code);
}

function identity(frame: WebFrameMain): FrameIdentity {
  if (frame.isDestroyed() || frame.detached) fail('filler_executor_frame_unavailable');
  const value = {
    processId: frame.processId,
    routingId: frame.routingId,
    frameTreeNodeId: frame.frameTreeNodeId,
    frameToken: frame.frameToken,
    url: frame.url,
    origin: frame.origin,
  };
  if (!Number.isSafeInteger(value.processId) || !Number.isSafeInteger(value.routingId)
      || !Number.isSafeInteger(value.frameTreeNodeId) || !value.frameToken) {
    fail('filler_executor_frame_identity_unavailable');
  }
  return value;
}

function sameFrame(a: FrameIdentity, b: FrameIdentity) {
  return a.processId === b.processId && a.routingId === b.routingId
    && a.frameTreeNodeId === b.frameTreeNodeId && a.frameToken === b.frameToken;
}

function subtreeMatches(main: WebFrameMain, target: FrameIdentity) {
  return main.framesInSubtree.filter(frame => {
    try { return sameFrame(identity(frame), target); } catch { return false; }
  });
}

function captureFrame(wc: WebContents, frame: WebFrameMain): FrameSnapshot {
  if (wc.isDestroyed()) fail('filler_executor_webcontents_unavailable');
  const main = identity(wc.mainFrame);
  const target = identity(frame);
  if (wc.getURL() !== main.url) fail('filler_executor_main_document_changed');
  const isMainFrame = sameFrame(main, target);
  if (!isMainFrame) {
    const top = frame.top;
    if (!top || !sameFrame(identity(top), main)) fail('filler_executor_frame_not_in_webcontents');
  }
  if (subtreeMatches(wc.mainFrame, target).length !== 1) fail('filler_executor_frame_not_unique');
  return { wc, frame, main, target, isMainFrame };
}

function assertFrameCurrent(snapshot: FrameSnapshot) {
  const { wc, frame, main, target, isMainFrame } = snapshot;
  if (wc.isDestroyed()) fail('filler_executor_webcontents_unavailable');
  const currentMain = identity(wc.mainFrame);
  const currentTarget = identity(frame);
  if (!sameFrame(currentMain, main) || currentMain.url !== main.url || wc.getURL() !== main.url
      || !sameFrame(currentTarget, target) || currentTarget.url !== target.url || currentTarget.origin !== target.origin) {
    fail('filler_executor_document_changed');
  }
  if (!isMainFrame) {
    const top = frame.top;
    if (!top || !sameFrame(identity(top), main)) fail('filler_executor_frame_detached');
  }
  if (subtreeMatches(wc.mainFrame, target).length !== 1) fail('filler_executor_frame_detached');
}

function collectProtocolFrames(tree: any) {
  const frames: ProtocolFrame[] = [];
  const visit = (entry: any, root: boolean) => {
    const frame = entry?.frame;
    if (!frame || typeof frame.id !== 'string' || typeof frame.url !== 'string'
        || typeof frame.securityOrigin !== 'string' || typeof frame.loaderId !== 'string' || !frame.loaderId) {
      fail('filler_executor_protocol_frame_incomplete');
    }
    frames.push({ id: frame.id, url: frame.url, securityOrigin: frame.securityOrigin, loaderId: frame.loaderId, root });
    for (const child of entry.childFrames || []) visit(child, false);
  };
  visit(tree?.frameTree, true);
  return frames;
}

function protocolFrameFor(snapshot: FrameSnapshot, tree: any): ProtocolFrame {
  const matches = collectProtocolFrames(tree).filter(frame => frame.url === snapshot.target.url);
  if (matches.length !== 1) fail('filler_executor_protocol_frame_ambiguous');
  const frame = matches[0];
  if (frame.securityOrigin !== snapshot.target.origin || frame.root !== snapshot.isMainFrame) {
    fail('filler_executor_protocol_frame_mismatch');
  }
  return frame;
}

function removeWaiter(state: DebuggerState, id: number, waiter: ContextWaiter) {
  clearTimeout(waiter.timer);
  const group = state.waiters.get(id);
  group?.delete(waiter);
  if (!group?.size) state.waiters.delete(id);
}

function rejectWaiters(state: DebuggerState, error: Error, id?: number) {
  const groups = id === undefined ? [...state.waiters.entries()] : [[id, state.waiters.get(id)]] as const;
  for (const [contextId, group] of groups) {
    if (!group) continue;
    for (const waiter of group) {
      removeWaiter(state, contextId, waiter);
      waiter.reject(error);
    }
  }
}

function observeContext(state: DebuggerState, context: RuntimeContext) {
  state.contexts.set(context.id, context);
  const group = state.waiters.get(context.id);
  if (!group) return;
  for (const waiter of [...group]) {
    removeWaiter(state, context.id, waiter);
    waiter.resolve(context);
  }
}

function stateFor(wc: WebContents): DebuggerState {
  const existing = debuggerStates.get(wc);
  if (existing) return existing;
  const debug = wc.debugger;
  let state!: DebuggerState;
  const onMessage = (_event: unknown, method: string, params: any, sessionId: string) => {
    if (sessionId) return;
    if (method === 'Runtime.executionContextCreated') {
      const value = params?.context;
      if (typeof value?.id !== 'number') return;
      const context: RuntimeContext = {
        id: value.id,
        name: typeof value.name === 'string' ? value.name : '',
        origin: typeof value.origin === 'string' ? value.origin : '',
        auxData: value.auxData && typeof value.auxData === 'object' ? value.auxData : undefined,
      };
      observeContext(state, context);
    } else if (method === 'Runtime.executionContextDestroyed') {
      const id = params?.executionContextId;
      if (typeof id === 'number') {
        state.contexts.delete(id);
        rejectWaiters(state, new Error('filler_executor_context_destroyed'), id);
      }
    } else if (method === 'Runtime.executionContextsCleared') {
      state.contexts.clear();
      rejectWaiters(state, new Error('filler_executor_contexts_cleared'));
    }
  };
  const onDetach = () => {
    state.detachGeneration++;
    state.ownsAttachment = false;
    state.runtimeEnabledGeneration = -1;
    state.contexts.clear();
    rejectWaiters(state, new Error('filler_executor_debugger_detached'));
  };
  const onDestroyed = () => {
    state.contexts.clear();
    rejectWaiters(state, new Error('filler_executor_webcontents_unavailable'));
    debug.off('message', onMessage);
    debug.off('detach', onDetach);
    debuggerStates.delete(wc);
  };
  state = {
    debugger: debug,
    tail: Promise.resolve(),
    activeLeases: 0,
    ownsAttachment: false,
    ownedGeneration: 0,
    detachGeneration: 0,
    runtimeEnabledGeneration: -1,
    contexts: new Map(),
    waiters: new Map(),
    onMessage,
    onDetach,
    onDestroyed,
  };
  debug.on('message', onMessage);
  debug.on('detach', onDetach);
  wc.once('destroyed', onDestroyed);
  debuggerStates.set(wc, state);
  return state;
}

function waitForContext(state: DebuggerState, id: number): Promise<RuntimeContext> {
  const current = state.contexts.get(id);
  if (current) return Promise.resolve(current);
  return new Promise((resolve, reject) => {
    const waiter: ContextWaiter = {
      resolve,
      reject,
      timer: setTimeout(() => {
        removeWaiter(state, id, waiter);
        reject(new Error('filler_executor_context_unproven'));
      }, CONTEXT_EVENT_TIMEOUT_MS),
    };
    const group = state.waiters.get(id) || new Set<ContextWaiter>();
    group.add(waiter);
    state.waiters.set(id, group);
  });
}

function assertContext(state: DebuggerState, binding: ContextBinding) {
  const { context, frameId } = binding;
  if (state.contexts.get(context.id) !== context || context.name !== FILLER_WORLD_NAME
      || context.auxData?.frameId !== frameId
      || context.auxData?.isDefault !== false || context.auxData?.type !== 'isolated') {
    fail('filler_executor_context_mismatch');
  }
}

function assertContextTarget(binding: ContextBinding, snapshot: FrameSnapshot) {
  if (binding.context.origin !== snapshot.target.origin) fail('filler_executor_context_origin_mismatch');
}

function assertValid(valid: () => boolean) {
  let result = false;
  try { result = valid() === true; } catch { result = false; }
  if (!result) fail('filler_executor_operation_invalid');
}

async function withSetupLock<T>(state: DebuggerState, work: () => Promise<T>): Promise<T> {
  const previous = state.tail;
  let release!: () => void;
  state.tail = new Promise<void>(resolve => { release = resolve; });
  await previous;
  try {
    return await work();
  } finally {
    release();
  }
}

function closeOwnedAttachment(state: DebuggerState) {
  if (state.activeLeases === 0 && state.ownsAttachment
      && state.ownedGeneration === state.detachGeneration && state.debugger.isAttached()) {
    state.debugger.detach();
    state.ownsAttachment = false;
  }
}

async function releaseLease(state: DebuggerState) {
  await withSetupLock(state, async () => {
    if (state.activeLeases > 0) state.activeLeases--;
    closeOwnedAttachment(state);
  });
}

async function acquireContext(wc: WebContents, snapshot: FrameSnapshot): Promise<SessionLease> {
  const state = stateFor(wc);
  return withSetupLock(state, async () => {
    if (wc.isDestroyed()) fail('filler_executor_webcontents_unavailable');
    const debug = state.debugger;
    if (!debug.isAttached()) {
      debug.attach(ATTACH_PROTOCOL_VERSION);
      state.ownsAttachment = true;
      state.ownedGeneration = state.detachGeneration;
    }
    state.activeLeases++;
    try {
      assertFrameCurrent(snapshot);
      const binding = await ensureContext(debug, state, snapshot);
      let released = false;
      return {
        debugger: debug,
        state,
        binding,
        release: async () => {
          if (released) return;
          released = true;
          await releaseLease(state);
        },
      };
    } catch (error) {
      state.activeLeases--;
      closeOwnedAttachment(state);
      throw error;
    }
  });
}

async function currentProtocolFrame(debug: DebuggerPort, snapshot: FrameSnapshot) {
  assertFrameCurrent(snapshot);
  const tree = await debug.sendCommand('Page.getFrameTree');
  const frame = protocolFrameFor(snapshot, tree);
  assertFrameCurrent(snapshot);
  return frame;
}

async function ensureContext(debug: DebuggerPort, state: DebuggerState, snapshot: FrameSnapshot): Promise<ContextBinding> {
  assertFrameCurrent(snapshot);
  await debug.sendCommand('Page.enable');
  if (state.runtimeEnabledGeneration !== state.detachGeneration) {
    const generation = state.detachGeneration;
    await debug.sendCommand('Runtime.enable');
    if (generation !== state.detachGeneration) fail('filler_executor_debugger_detached');
    state.runtimeEnabledGeneration = generation;
  }
  const initialFrame = await currentProtocolFrame(debug, snapshot);
  const created = await debug.sendCommand('Page.createIsolatedWorld', {
    frameId: initialFrame.id,
    worldName: FILLER_WORLD_NAME,
    grantUniveralAccess: false,
  });
  const id = created?.executionContextId;
  if (!Number.isSafeInteger(id) || id <= 0) fail('filler_executor_context_id_missing');
  const context = await waitForContext(state, id);
  const binding = { frameId: initialFrame.id, loaderId: initialFrame.loaderId, context };
  if (context.name !== FILLER_WORLD_NAME || context.origin !== snapshot.target.origin
      || context.auxData?.frameId !== initialFrame.id || context.auxData?.isDefault !== false
      || context.auxData?.type !== 'isolated') fail('filler_executor_context_mismatch');
  assertContext(state, binding);
  assertContextTarget(binding, snapshot);
  const currentFrame = await currentProtocolFrame(debug, snapshot);
  if (currentFrame.id !== binding.frameId || currentFrame.loaderId !== binding.loaderId) {
    fail('filler_executor_document_changed');
  }
  assertContext(state, binding);
  return binding;
}

function nextObjectGroup() {
  objectGroupSequence = objectGroupSequence >= Number.MAX_SAFE_INTEGER ? 1 : objectGroupSequence + 1;
  return `${FILLER_WORLD_NAME}.upload.${objectGroupSequence}`;
}

function resultValue(response: any) {
  if (response?.exceptionDetails) fail('filler_executor_script_failed');
  const result = response?.result;
  if (result && Object.prototype.hasOwnProperty.call(result, 'value')) return result.value;
  if (result?.type === 'undefined') return undefined;
  fail('filler_executor_result_not_by_value');
}

async function assertSameDocument(debug: DebuggerPort, state: DebuggerState, snapshot: FrameSnapshot, binding: ContextBinding) {
  assertFrameCurrent(snapshot);
  assertContext(state, binding);
  const currentFrame = await currentProtocolFrame(debug, snapshot);
  if (currentFrame.id !== binding.frameId || currentFrame.loaderId !== binding.loaderId) {
    fail('filler_executor_document_changed');
  }
  assertContext(state, binding);
}

export class FillerFrameExecutor {
  async execute(wc: WebContents, frame: WebFrameMain, code: string): Promise<unknown> {
    const snapshot = captureFrame(wc, frame);
    const lease = await acquireContext(wc, snapshot);
    try {
      assertFrameCurrent(snapshot);
      assertContext(lease.state, lease.binding);
      assertContextTarget(lease.binding, snapshot);
      const response = await lease.debugger.sendCommand('Runtime.evaluate', {
        expression: code,
        contextId: lease.binding.context.id,
        returnByValue: true,
        awaitPromise: true,
      });
      const value = resultValue(response);
      await assertSameDocument(lease.debugger, lease.state, snapshot, lease.binding);
      return value;
    } finally {
      await lease.release();
    }
  }

  async assignFile(wc: WebContents, frame: WebFrameMain, code: string, file: string, valid: () => boolean): Promise<void> {
    assertValid(valid);
    if (typeof file !== 'string' || !file) fail('filler_executor_file_missing');
    const snapshot = captureFrame(wc, frame);
    const lease = await acquireContext(wc, snapshot);
    try {
      assertValid(valid);
      const debug = lease.debugger;
      const state = lease.state;
      const binding = lease.binding;
      await debug.sendCommand('DOM.enable');
      assertValid(valid);
      const objectGroup = nextObjectGroup();
      try {
        const response = await debug.sendCommand('Runtime.evaluate', {
          expression: code,
          contextId: binding.context.id,
          objectGroup,
          returnByValue: false,
          awaitPromise: true,
        });
        if (response?.exceptionDetails) fail('filler_executor_script_failed');
        const object = response?.result;
        if (object?.type !== 'object' || object.subtype !== 'node'
            || object.className !== 'HTMLInputElement' || typeof object.objectId !== 'string') {
          fail('filler_executor_upload_target_invalid');
        }
        assertValid(valid);
        assertFrameCurrent(snapshot);
        assertContext(state, binding);
        const target = await debug.sendCommand('Runtime.callFunctionOn', {
          objectId: object.objectId,
          functionDeclaration: 'function () { return this instanceof HTMLInputElement && this.localName === "input" && this.type === "file" && this.isConnected && this.ownerDocument === document && this.ownerDocument.defaultView === window; }',
          returnByValue: true,
          awaitPromise: false,
        });
        if (target?.exceptionDetails || target?.result?.value !== true) fail('filler_executor_upload_document_mismatch');
        await assertSameDocument(debug, state, snapshot, binding);
        assertValid(valid);
        await debug.sendCommand('DOM.setFileInputFiles', { files: [file], objectId: object.objectId });
        await assertSameDocument(debug, state, snapshot, binding);
        assertValid(valid);
      } finally {
        try { await debug.sendCommand('Runtime.releaseObjectGroup', { objectGroup }); } catch { /* Context teardown releases it as well. */ }
      }
    } finally {
      await lease.release();
    }
  }
}
