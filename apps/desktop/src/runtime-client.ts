import { spawn, ChildProcessWithoutNullStreams } from 'node:child_process';
import { randomBytes } from 'node:crypto';
import http from 'node:http';
import path from 'node:path';
import fs from 'node:fs';

export type RuntimeState = { status: string; stage: string; code?: string; instanceId?: string; writes: boolean; websocket: boolean };
export type Launch = { executable: string; args: string[]; cwd: string; env: NodeJS.ProcessEnv; expectedInstance?: string; desktop?: boolean };

// No external origin, inherited credential, developer PATH or process ID is accepted.
export function runtimeLaunch(repo: string, env: NodeJS.ProcessEnv): Launch | undefined {
  if (!env.RECRUITOPS_DESKTOP_RUNTIME_RESOURCES) return;
  const resources = fs.realpathSync(env.RECRUITOPS_DESKTOP_RUNTIME_RESOURCES);
  const root = fs.realpathSync(repo);
  const instance = path.resolve(env.RECRUITOPS_DESKTOP_RUNTIME_INSTANCE || path.join(root, '.desktop-runtime-tests', 'shell-instance'));
  const relative = path.relative(path.join(root, '.desktop-runtime-tests'), instance);
  if (!relative || relative.startsWith('..') || path.isAbsolute(relative)) throw new Error('runtime_instance_outside_isolation');
  // Check existing ancestors as well as lexical paths, including Windows junctions.
  let ancestor = instance;
  while (!fs.existsSync(ancestor)) ancestor = path.dirname(ancestor);
  const resolved = path.relative(root, fs.realpathSync(ancestor));
  if (resolved.startsWith('..') || path.isAbsolute(resolved)) throw new Error('runtime_instance_link_escape');
  const expectedInstance = env.RECRUITOPS_DESKTOP_ENABLE_WRITES_FOR_INSTANCE;
  if (expectedInstance && !/^[a-zA-Z0-9_-]{1,128}$/.test(expectedInstance)) throw new Error('invalid_instance_opt_in');
  const childEnv: NodeJS.ProcessEnv = {};
  for (const key of ['SystemRoot', 'WINDIR', 'TEMP', 'TMP']) if (env[key]) childEnv[key] = env[key];
  childEnv.PYTHONUTF8 = '1'; childEnv.PYTHONNOUSERSITE = '1';
  childEnv.NO_PROXY = childEnv.no_proxy = '127.0.0.1,localhost,::1';
  return { executable: path.join(root, '.venv-desktop-tests', 'Scripts', 'python.exe'),
    args: ['-m', 'packages.desktop_runtime', '--resources', resources, '--instance', instance, '--start',
      ...(expectedInstance ? ['--enable-writes-for-instance', expectedInstance] : [])],
    cwd: root, env: childEnv, expectedInstance };
}

export class OwnedRuntime {
  state: RuntimeState = { status: 'offline', stage: 'preflight', writes: false, websocket: false };
  origin?: string;
  private token = randomBytes(32).toString('hex');
  private child?: ChildProcessWithoutNullStreams;
  private sequence = 0;
  private runId?: string;
  private buffer = '';
  private ended = false;
  private stopPromise?: Promise<void>;
  private startupTimer?: NodeJS.Timeout;
  constructor(private changed: () => void, private launch: Launch) {}
  start() {
    if (this.child) throw new Error('runtime_already_started');
    this.state.status = 'starting'; this.changed();
    this.child = spawn(this.launch.executable, this.launch.args, { cwd: this.launch.cwd,
      env: { ...this.launch.env, RECRUITOPS_DESKTOP_SHELL_TOKEN: this.token }, windowsHide: true, shell: false, stdio: 'pipe' });
    this.startupTimer = setTimeout(() => this.fail('runtime_startup_timeout'), 300000);
    this.child.stdout.setEncoding('utf8');
    this.child.stdout.on('data', (data: string) => {
      this.buffer += data;
      if (this.buffer.length > 65536) return this.fail('runtime_protocol_limit');
      let index: number;
      while ((index = this.buffer.indexOf('\n')) >= 0) {
        const line = this.buffer.slice(0, index); this.buffer = this.buffer.slice(index + 1);
        try { this.accept(JSON.parse(line)); } catch { this.fail('runtime_protocol_invalid'); }
      }
    });
    // Arbitrary child output may contain credentials. Never publish it to a renderer.
    this.child.stderr.resume();
    this.child.stdin.on('error', () => {});
    this.child.on('error', () => { this.ended = true; this.fail('runtime_launch_failed'); });
    this.child.on('exit', () => {
      this.ended = true; clearTimeout(this.startupTimer); this.origin = undefined;
      this.state.writes = false; this.state.websocket = false;
      if (this.state.status !== 'failed') this.state.status = this.stopPromise ? 'stopped' : 'failed';
      if (this.state.status === 'failed' && !this.state.code) this.state.code = 'runtime_exited';
      this.changed();
    });
  }
  private accept(event: Record<string, unknown>) {
    if (this.state.status === 'failed' || this.stopPromise) return;
    if (event.protocol !== 1 || !Number.isSafeInteger(event.sequence) || Number(event.sequence) <= this.sequence ||
        typeof event.run_id !== 'string' || !/^[a-zA-Z0-9_-]{1,128}$/.test(event.run_id) ||
        typeof event.event !== 'string' || typeof event.stage !== 'string') throw new Error('protocol');
    if (this.runId && event.run_id !== this.runId) throw new Error('run_identity');
    this.runId = event.run_id;
    const previouslyBoundInstance = this.state.instanceId;
    if (event.instance_id !== undefined) {
      if (typeof event.instance_id !== 'string' || !/^[a-zA-Z0-9_-]{1,128}$/.test(event.instance_id) ||
          this.state.instanceId && event.instance_id !== this.state.instanceId) throw new Error('identity');
      this.state.instanceId = event.instance_id;
    } else if (this.state.instanceId) throw new Error('missing_identity');
    this.sequence = Number(event.sequence);
    this.state.stage = /^[a-z_]{1,40}$/.test(event.stage) ? event.stage : 'runtime';
    if (event.event === 'failed') return this.fail(typeof event.code === 'string' && /^[a-z0-9_]{1,100}$/.test(event.code) ? event.code : 'runtime_failed');
    if (event.event === 'ready' && event.stage === 'runtime') {
      if (typeof event.instance_id !== 'string') throw new Error('missing_identity');
      const url = new URL(String(event.api_url));
      if (url.protocol !== 'http:' || url.hostname !== '127.0.0.1' || Number(url.port) < 49152 || Number(url.port) > 65535 || url.pathname !== '/' || url.username || url.password || url.search || url.hash) throw new Error('origin');
      const desktopOwned = this.launch.desktop === true && previouslyBoundInstance === event.instance_id;
      if (this.launch.desktop && (!desktopOwned || event.writes !== true)) throw new Error('desktop_identity');
      if (event.writes !== false && !(event.writes === true && (desktopOwned || this.launch.expectedInstance === event.instance_id))) throw new Error('write_opt_in');
      void this.authenticate(url.origin, event.instance_id, event.writes === true, event.websocket === true);
    }
    this.changed();
  }
  private async authenticate(origin: string, id: string, writes: boolean, websocket: boolean) {
    try {
      const result = await this.requestAt(origin, '/desktop-runtime/ready');
      if (result.status !== 'ready' || result.instance_id !== id || result.run_id !== this.runId || result.writes !== writes || result.websocket !== websocket) throw new Error('identity');
      if (this.ended || this.stopPromise || this.state.status === 'failed') return;
      this.origin = origin; this.state = { status: 'ready', stage: 'runtime', instanceId: id, writes, websocket };
      clearTimeout(this.startupTimer); this.changed();
    } catch { this.fail('runtime_identity_failed'); }
  }
  private requestAt(origin: string, route: string): Promise<Record<string, unknown>> {
    return new Promise((resolve, reject) => {
      const req = http.get(origin + route, { headers: { Authorization: this.authorization() }, timeout: 5000 }, res => {
        let body = ''; res.setEncoding('utf8');
        res.on('data', data => { body += data; if (body.length > 65536) req.destroy(new Error('limit')); });
        res.on('error', reject);
        res.on('end', () => { try { if (res.statusCode !== 200) throw new Error('status'); resolve(JSON.parse(body)); } catch (error) { reject(error); } });
      });
      req.on('error', reject); req.on('timeout', () => req.destroy(new Error('timeout')));
    });
  }
  authorization() { return 'Bearer ' + this.token; }
  allows(url: string, method: string) {
    if (!this.origin || this.state.status !== 'ready') return false;
    try {
      const target = new URL(url);
      if (target.username || target.password) return false;
      if (target.protocol === 'ws:') return this.state.websocket && target.origin === this.origin.replace('http:', 'ws:');
      const configurationRead = method === 'POST' && target.pathname === '/api/local-ui/configuration/read' && !target.search && !target.hash;
      return target.origin === this.origin && (this.state.writes || ['GET', 'HEAD', 'OPTIONS'].includes(method) || configurationRead);
    } catch { return false; }
  }
  private fail(code: string) {
    this.origin = undefined; this.state = { ...this.state, status: 'failed', code, writes: false, websocket: false };
    clearTimeout(this.startupTimer); this.changed(); void this.stop();
  }
  stop(): Promise<void> {
    if (this.stopPromise) return this.stopPromise;
    if (!this.child || this.ended) return Promise.resolve();
    const child = this.child;
    this.origin = undefined; this.state.writes = false; this.state.websocket = false;
    if (this.state.status !== 'failed') this.state.status = 'stopping';
    this.stopPromise = new Promise(resolve => {
      const timer = setTimeout(() => child.kill(), 45000);
      child.once('exit', () => { clearTimeout(timer); resolve(); });
      child.once('error', () => { clearTimeout(timer); resolve(); });
      child.stdin.end('{"command":"stop"}\n');
    });
    this.changed(); return this.stopPromise;
  }
}
