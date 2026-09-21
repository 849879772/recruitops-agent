import { createHmac, randomUUID } from 'node:crypto';
import fs from 'node:fs';
import path from 'node:path';
import { OwnedRuntime } from './runtime-client';
import { ReviewObservationError } from './review-readiness';
const WebSocket = require('ws');
type RecordValue = Record<string, any>;
const identifier = (value: unknown): value is string => typeof value === 'string' && /^[a-zA-Z0-9][a-zA-Z0-9_.:-]{0,127}$/.test(value);

export const REVIEW_OPERATION_BUDGET_MS = 38000;
export const REVIEW_QUEUE_WAIT_MS = 8000;
const MAX_SEEN_OPERATION_IDS = 4096;
export type ReviewStage = 'EXTRACTING' | 'WAITING_FOR_CONTENT' | 'VALIDATING' | 'WAITING_FOR_LOGIN' | 'STATE_UNCLEAR';
type ReviewCallback = (url: string, operationId: string, ids: string[], signal: AbortSignal, deadline: number,
  onStage?: (stage: ReviewStage) => void) => Promise<unknown>;
interface BridgeOptions {
  maxActive?: number;
  maxQueued?: number;
  queueWaitMs?: number;
  operationBudgetMs?: number;
  socketFactory?: (target: string, options: RecordValue) => any;
}
interface ReviewTask {
  id: string;
  socket: any;
  controller: AbortController;
  url: string;
  ids: string[];
  dispatchedAt: number;
  deadline: number;
  startedAt?: number;
  state: 'queued' | 'running';
  stage: string;
  queueTimer?: NodeJS.Timeout;
  operationTimer?: NodeJS.Timeout;
}

const SAFE_REVIEW_ERROR_CODES = new Set([
  'ACTION_NOT_ALLOWED', 'COMMAND_INVALID', 'DESKTOP_ADAPTER_UNAVAILABLE', 'DESKTOP_BROWSER_UNAVAILABLE',
  'DESKTOP_INVALID_BINDING', 'DESKTOP_LOAD_FAILED', 'DESKTOP_LOAD_TIMEOUT', 'DESKTOP_MANUAL_CAPTURE_UNAVAILABLE',
  'DESKTOP_NAVIGATION_CHANGED', 'DESKTOP_OBSERVATION_FAILED', 'DESKTOP_OBSERVATION_TIMEOUT',
  'DESKTOP_PAGE_LOADING', 'DESKTOP_PAYLOAD_LIMIT', 'DESKTOP_READINESS_TIMEOUT', 'DESKTOP_REVIEW_BUSY',
  'DESKTOP_REVIEW_CANCELLED', 'DESKTOP_REVIEW_DISCONNECTED', 'DESKTOP_REVIEW_QUEUE_FULL',
  'DESKTOP_REVIEW_HISTORY_FULL', 'DESKTOP_REVIEW_QUEUE_TIMEOUT', 'DESKTOP_REVIEW_RESULT_EXPIRED',
  'DESKTOP_REVIEW_TIMEOUT', 'FRAME_EVIDENCE_UNAVAILABLE', 'FRAME_NOT_ALLOWED',
  'LOGIN_REQUIRED', 'SOURCE_NOT_ALLOWED', 'STATE_UNCLEAR', 'CAPTCHA_REQUIRED'
]);
const BROWSER_ERROR_CODES: Record<string, string> = {
  browser_adapter_unavailable: 'DESKTOP_ADAPTER_UNAVAILABLE',
  browser_cancelled: 'DESKTOP_REVIEW_CANCELLED',
  browser_invalid_binding: 'DESKTOP_INVALID_BINDING',
  browser_load_failed: 'DESKTOP_LOAD_FAILED',
  browser_load_timeout: 'DESKTOP_LOAD_TIMEOUT',
  browser_navigation_changed: 'DESKTOP_NAVIGATION_CHANGED',
  browser_observation_timeout: 'DESKTOP_OBSERVATION_TIMEOUT',
  browser_page_loading: 'DESKTOP_PAGE_LOADING',
  browser_payload_limit: 'DESKTOP_PAYLOAD_LIMIT',
  browser_readiness_timeout: 'DESKTOP_READINESS_TIMEOUT',
  browser_unavailable_or_busy: 'DESKTOP_BROWSER_UNAVAILABLE',
  manual_capture_unavailable: 'DESKTOP_MANUAL_CAPTURE_UNAVAILABLE'
};

export function safeReviewErrorCode(error: unknown): string {
  const value = typeof error === 'string' ? error : error instanceof Error ? error.message : '';
  return BROWSER_ERROR_CODES[value] || (SAFE_REVIEW_ERROR_CODES.has(value) ? value : 'DESKTOP_OBSERVATION_FAILED');
}

export function desktopDeviceId(directory: string): string {
  const filename = path.join(directory, 'desktop-device.json');
  try {
    const id = JSON.parse(fs.readFileSync(filename, 'utf8')).device_id;
    if (typeof id !== 'string' || !/^desktop-[0-9a-f-]{36}$/.test(id)) throw new Error('invalid_device_identity');
    return id;
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code !== 'ENOENT') throw new Error('invalid_device_identity');
    const id = 'desktop-' + randomUUID();
    fs.writeFileSync(filename, JSON.stringify({ device_id: id }), { flag: 'wx', mode: 0o600 });
    return id;
  }
}
export function reviewCommand(message: RecordValue) {
  const payload = message.payload;
  const command = payload?.command;
  if (!payload || payload.operation_id !== message.operation_id || !command || typeof command !== 'object' || Array.isArray(command)) throw new Error('COMMAND_INVALID');
  const allowed = payload.operation === 'observe_application_status_page' && command.action === 'observe_application_page' && command.selector_key === 'application_page' ||
    payload.operation === 'review_and_update_application_status' && command.action === 'read_application_status' && command.selector_key === 'application_status';
  if (!allowed) throw new Error('ACTION_NOT_ALLOWED');
  let page: URL;
  try { page = new URL(command.page_url); } catch { throw new Error('COMMAND_INVALID'); }
  if (typeof command.page_url !== 'string' || command.page_url.length > 2048 || !['http:', 'https:'].includes(page.protocol) ||
      page.username || page.password || page.origin !== command.origin || !identifier(command.application_id) ||
      !Array.isArray(command.application_ids) || command.application_ids.length < 1 || command.application_ids.length > 100 ||
      !command.application_ids.every(identifier) || new Set(command.application_ids).size !== command.application_ids.length ||
      !command.application_ids.includes(command.application_id)) throw new Error('COMMAND_INVALID');
  return { url: command.page_url as string, ids: command.application_ids as string[] };
}

export class DesktopBridge {
  status = 'disconnected';
  private socket: any;
  private heartbeat?: NodeJS.Timeout;
  private retry?: NodeJS.Timeout;
  private timeout?: NodeJS.Timeout;
  private stopped = false;
  private attempts = 0;
  private authenticated = false;
  private lastHeartbeat = 0;
  private operations = new Map<string, ReviewTask>();
  private queue: string[] = [];
  private active = 0;
  private completed = new Map<string, RecordValue | null>();
  private seen = new Set<string>();
  constructor(private runtime: OwnedRuntime, private deviceId: string,
    private review: ReviewCallback, private changed: () => void, private options: BridgeOptions = {}) {}
  connect() {
    if (this.stopped || this.socket || !this.runtime.origin || !this.runtime.state.websocket) return;
    const origin = this.runtime.origin;
    const target = origin.replace('http:', 'ws:') + '/browser-bridge';
    if (!this.runtime.allows(target, 'GET')) return;
    const socketOptions = { headers: { Authorization: this.runtime.authorization(), Origin: origin },
      followRedirects: false, maxPayload: 262144, handshakeTimeout: 5000, perMessageDeflate: false };
    const socket = this.socket = this.options.socketFactory
      ? this.options.socketFactory(target, socketOptions)
      : new WebSocket(target, socketOptions);
    this.status = 'connecting'; this.changed();
    this.timeout = setTimeout(() => socket.terminate(), 10000);
    socket.on('message', (data: Buffer) => {
      if (socket !== this.socket) return;
      try { this.receive(JSON.parse(data.toString('utf8'))); } catch { socket.close(1008); }
    });
    socket.on('error', () => { this.status = 'connection_failed'; this.changed(); });
    socket.on('close', () => {
      if (socket !== this.socket) return;
      this.socket = undefined; this.authenticated = false;
      clearTimeout(this.timeout); clearInterval(this.heartbeat);
      this.clearSocketTasks(socket, 'DESKTOP_REVIEW_DISCONNECTED');
      this.status = 'disconnected'; this.changed();
      if (!this.stopped && this.runtime.origin) this.retry = setTimeout(() => this.connect(), Math.min(30000, 1000 * 2 ** Math.min(this.attempts++, 5)));
    });
  }
  private send(message: RecordValue, socket = this.socket) {
    if (socket?.readyState === WebSocket.OPEN) socket.send(JSON.stringify({ protocol_version: 1, ...message }));
  }
  private receive(message: RecordValue) {
    if (!message || typeof message !== 'object' || Array.isArray(message) || message.protocol_version !== 1) throw new Error('protocol');
    if (message.type === 'challenge') {
      if (this.authenticated || typeof message.challenge !== 'string' || !/^[A-Za-z0-9_-]{16,256}$/.test(message.challenge)) throw new Error('challenge');
      const signature = createHmac('sha256', this.runtime.authorization().slice(7))
        .update(`recruitops-browser-bridge-v1\n${this.deviceId}\n${message.challenge}`).digest('hex');
      this.send({ type: 'auth', device_id: this.deviceId, challenge: message.challenge, signature });
      this.authenticated = true;
      this.send({ type: 'heartbeat' });
      this.lastHeartbeat = Date.now();
      this.heartbeat = setInterval(() => {
        if (Date.now() - this.lastHeartbeat > 45000) this.socket?.terminate(); else this.send({ type: 'heartbeat' });
      }, 15000);
      return;
    }
    if (!this.authenticated) throw new Error('unauthenticated');
    if (message.type === 'heartbeat' && message.ack === true) {
      clearTimeout(this.timeout); this.lastHeartbeat = Date.now(); this.attempts = 0; this.status = 'connected'; this.changed(); return;
    }
    if (message.device_id !== this.deviceId || !identifier(message.operation_id) || !Number.isSafeInteger(message.sequence) || message.sequence <= 0) throw new Error('ownership');
    const id = message.operation_id;
    if (message.type === 'operation.cancel') {
      const task = this.operations.get(id);
      if (task) this.finishTask(task, null, false, true);
      else if (!this.completed.has(id)) {
        if (!this.seen.has(id) && this.seen.size < MAX_SEEN_OPERATION_IDS) this.seen.add(id);
        this.remember(id, null);
      }
      this.send({ type: 'cancel', operation_id: id, sequence: message.sequence, ack_id: `cancel-${message.sequence}` }); return;
    }
    if (message.type !== 'operation.dispatch') throw new Error('unsupported');
    this.send({ type: 'ack', operation_id: id, sequence: message.sequence, ack_id: `ack-${message.sequence}` });
    if (this.completed.has(id)) { const previous = this.completed.get(id); if (previous) this.send(previous); return; }
    if (this.operations.has(id)) return;
    if (this.seen.has(id)) { this.send(this.expiredResult(id)); return; }
    if (this.seen.size >= MAX_SEEN_OPERATION_IDS) { this.completeRejected(id, 'DESKTOP_REVIEW_HISTORY_FULL', false); return; }
    this.seen.add(id);
    let command: ReturnType<typeof reviewCommand>;
    try { command = reviewCommand(message); }
    catch (error) { this.completeRejected(id, error); return; }
    const task: ReviewTask = {
      id, socket: this.socket, controller: new AbortController(), url: command.url, ids: command.ids,
      dispatchedAt: Date.now(), deadline: Date.now() + (this.options.operationBudgetMs ?? REVIEW_OPERATION_BUDGET_MS),
      state: 'queued', stage: 'QUEUED'
    };
    if (this.active < (this.options.maxActive ?? 4)) {
      task.state = 'running'; this.operations.set(id, task); this.runTask(task); return;
    }
    if (this.queue.length >= (this.options.maxQueued ?? 10)) {
      this.completeRejected(id, 'DESKTOP_REVIEW_QUEUE_FULL'); return;
    }
    this.operations.set(id, task); this.queue.push(id);
    task.queueTimer = setTimeout(() => this.finishTask(task, this.failure(task, 'DESKTOP_REVIEW_QUEUE_TIMEOUT'), true, true),
      Math.max(0, this.options.queueWaitMs ?? REVIEW_QUEUE_WAIT_MS));
    this.progress(task, 'DISPATCHED', 'QUEUED');
  }

  private remember(id: string, result: RecordValue | null) {
    this.completed.delete(id); this.completed.set(id, result);
    if (this.completed.size > 256) this.completed.delete(this.completed.keys().next().value!);
  }

  private expiredResult(id: string): RecordValue {
    return { type: 'result', operation_id: id, event_id: `desktop-result-${randomUUID()}`, status: 'FAILED',
      error_code: 'DESKTOP_REVIEW_RESULT_EXPIRED', result: { evidence_only: true, database_updated: false } };
  }

  private timing(task: ReviewTask) {
    const now = Date.now();
    return { queue_wait_ms: task.state === 'queued' ? now - task.dispatchedAt : Math.max(0, (task.startedAt ?? now) - task.dispatchedAt),
      elapsed_ms: now - task.dispatchedAt };
  }

  private progress(task: ReviewTask, status: string, stage: string) {
    if (this.operations.get(task.id) !== task) return;
    task.stage = stage;
    this.send({ type: 'progress', operation_id: task.id, event_id: `desktop-${randomUUID()}`, status,
      payload: { stage, ...this.timing(task) } }, task.socket);
  }

  private failure(task: ReviewTask, code: string, error?: unknown): RecordValue {
    return { type: 'result', operation_id: task.id, event_id: `desktop-result-${randomUUID()}`, status: 'FAILED',
      error_code: safeReviewErrorCode(code), result: { evidence_only: true, database_updated: false, stage: task.stage, ...this.timing(task),
        ...(error instanceof ReviewObservationError ? {last_observation: error.lastObservation} : {}) } };
  }

  private completeRejected(id: string, error: unknown, remember = true) {
    const code = safeReviewErrorCode(error);
    const result: RecordValue = { type: 'result', operation_id: id, event_id: `desktop-result-${randomUUID()}`, status: 'FAILED',
      error_code: code, result: { evidence_only: true, database_updated: false } };
    if (remember) this.remember(id, result);
    this.send(result);
  }

  private release(task: ReviewTask) {
    clearTimeout(task.queueTimer); clearTimeout(task.operationTimer);
    if (this.operations.get(task.id) !== task) return false;
    this.operations.delete(task.id);
    if (task.state === 'running') this.active = Math.max(0, this.active - 1);
    else this.queue = this.queue.filter(id => id !== task.id);
    return true;
  }

  private finishTask(task: ReviewTask, result: RecordValue | null, send: boolean, abort: boolean) {
    if (!this.release(task)) return;
    if (abort) task.controller.abort();
    this.remember(task.id, result);
    if (send && result && !this.stopped && task.socket === this.socket) this.send(result, task.socket);
    this.pump();
  }

  private pump() {
    while (!this.stopped && this.active < (this.options.maxActive ?? 4) && this.queue.length) {
      const id = this.queue.shift()!;
      const task = this.operations.get(id);
      if (!task || task.state !== 'queued') continue;
      task.state = 'running'; clearTimeout(task.queueTimer); this.runTask(task);
    }
  }

  private async runTask(task: ReviewTask) {
    if (this.operations.get(task.id) !== task) return;
    const remaining = task.deadline - Date.now();
    if (remaining <= 0) { this.finishTask(task, this.failure(task, 'DESKTOP_REVIEW_TIMEOUT'), true, true); return; }
    task.state = 'running'; task.startedAt = Date.now(); this.active++;
    task.operationTimer = setTimeout(() => this.finishTask(task, this.failure(task, 'DESKTOP_REVIEW_TIMEOUT'), true, true), remaining);
    this.progress(task, 'NAVIGATING', 'NAVIGATING');
    try {
      const value = await this.review(task.url, task.id, task.ids, task.controller.signal, task.deadline,
        stage => {
          // Readiness detail is not a bridge state. Keep terminal states for
          // the result envelope so it can carry the evidence/error payload.
          const status = stage === 'WAITING_FOR_CONTENT' || stage === 'STATE_UNCLEAR' ? 'EXTRACTING' : stage;
          this.progress(task, status, stage);
        });
      if (this.operations.get(task.id) !== task) return;
      this.finishTask(task, this.normalizeResult(value, task), true, false);
    } catch (error) {
      if (this.operations.get(task.id) !== task) return;
      this.finishTask(task, this.failure(task, safeReviewErrorCode(error), error), true, false);
    }
  }

  private normalizeResult(value: unknown, task: ReviewTask): RecordValue {
    if (!value || typeof value !== 'object' || Array.isArray(value)) return this.failure(task, 'DESKTOP_OBSERVATION_FAILED');
    const response = value as RecordValue;
    if (response.type !== 'result' || response.operation_id !== task.id ||
        !['SUCCEEDED', 'STATE_UNCLEAR', 'FAILED'].includes(response.status) ||
        !response.result || typeof response.result !== 'object' || Array.isArray(response.result)) {
      return this.failure(task, 'DESKTOP_OBSERVATION_FAILED');
    }
    let requestedUrl: URL;
    let observedUrl: URL;
    try {
      requestedUrl = new URL(task.url);
      observedUrl = new URL(response.result.page_url);
    } catch { return this.failure(task, 'DESKTOP_OBSERVATION_FAILED'); }
    if (!['http:', 'https:'].includes(observedUrl.protocol) || observedUrl.username || observedUrl.password) {
      return this.failure(task, 'DESKTOP_OBSERVATION_FAILED');
    }
    if (observedUrl.origin !== requestedUrl.origin) return this.failure(task, 'DESKTOP_NAVIGATION_CHANGED');
    const resultIds = response.result.application_ids;
    if (!Array.isArray(resultIds) || resultIds.length !== task.ids.length ||
        resultIds.some((id: unknown, index: number) => id !== task.ids[index])) {
      return this.failure(task, 'DESKTOP_INVALID_BINDING');
    }
    if (response.result.page !== undefined) {
      const page = response.result.page;
      if (!page || typeof page !== 'object' || Array.isArray(page) || page.page_url !== observedUrl.href ||
          page.origin !== observedUrl.origin || page.path !== observedUrl.pathname) {
        return this.failure(task, 'DESKTOP_OBSERVATION_FAILED');
      }
    }
    // Preserve the actual capture URL. Bind an owned redirect to the server's
    // original target separately, without persisting query credentials.
    requestedUrl.search = '';
    const route = requestedUrl.hash.slice(1).split('?', 1)[0];
    requestedUrl.hash = /^(?:\/|!\/)[^\s#]{0,1022}$/.test(route) ? route : '';
    const result = { ...response.result };
    delete result.navigation_binding;
    if (requestedUrl.href !== observedUrl.href) result.navigation_binding = {
      source: 'desktop_owned_navigation_v1', requested_page_url: requestedUrl.href,
      observed_page_url: observedUrl.href,
    };
    const output: RecordValue = { type: 'result', operation_id: task.id,
      event_id: typeof response.event_id === 'string' && response.event_id.length <= 128 ? response.event_id : `desktop-result-${randomUUID()}`,
      status: response.status, result };
    if (response.status === 'FAILED' || response.error_code !== undefined) {
      output.error_code = safeReviewErrorCode(response.error_code);
      if (!response.error_code && response.status === 'FAILED') output.error_code = 'DESKTOP_OBSERVATION_FAILED';
    }
    return output;
  }

  private clearSocketTasks(socket: any, code: string) {
    const tasks = [...this.operations.values()].filter(task => task.socket === socket);
    const ids = new Set(tasks.map(task => task.id));
    for (const task of tasks) {
      clearTimeout(task.queueTimer); clearTimeout(task.operationTimer); task.controller.abort();
      if (task.state === 'running') this.active = Math.max(0, this.active - 1);
      this.operations.delete(task.id); this.remember(task.id, this.failure(task, code));
    }
    this.queue = this.queue.filter(id => !ids.has(id));
  }

  stop() {
    this.stopped = true; clearTimeout(this.retry); clearTimeout(this.timeout); clearInterval(this.heartbeat);
    const socket = this.socket; this.socket = undefined; this.authenticated = false;
    if (socket) this.clearSocketTasks(socket, 'DESKTOP_REVIEW_DISCONNECTED');
    this.operations.clear(); this.queue = []; this.active = 0; this.completed.clear(); this.seen.clear();
    socket?.close(1000); socket?.terminate();
    this.status = 'stopped';
  }
}
