import { app, BrowserWindow, WebContentsView, session, ipcMain, Menu, Tray, nativeImage, dialog, screen, Session, safeStorage } from 'electron';
import type { WebContents } from 'electron';
import path from 'node:path';
import fs from 'node:fs';
import { lookup } from 'node:dns/promises';
import { pathToFileURL } from 'node:url';
import { securePreferences, websiteUrl, isolatedApi, parseCommand, trustedSender, isPrivateHost } from './policy';
import { browserIntegration, BackgroundPageLease } from './browser-contract';
import { OwnedRuntime, runtimeLaunch, Launch } from './runtime-client';
import { BrowserService, BrowserAdapter } from './browser-service';
import { randomUUID } from 'node:crypto';
import { DesktopBridge, desktopDeviceId, REVIEW_OPERATION_BUDGET_MS } from './bridge-client';
import type { ReviewStage } from './bridge-client';
import { packagedRuntimeLaunch } from './packaged-runtime';
import { FillerService } from './filler-service';
import { FillerStore, FillerSnapshot } from './filler-store';
import { FillerApplicationService, PendingRegistration, recruitCompanyCacheKey } from './filler-application-service';
import type { ApplicationPage } from './filler-application-service';

// A dedicated profile is selected before any Chromium session is created.
// Portable launchers provide their own paths; direct EXE launches use a writable
// per-user default while preserving the same packaged isolation boundary.
if (app.isPackaged && !process.env.RECRUITOPS_DESKTOP_ISOLATION_ROOT) {
  process.env.RECRUITOPS_DESKTOP_ISOLATION_ROOT = path.join(
    path.dirname(process.execPath), '.data', '.desktop-runtime-tests'
  );
}
const packagedIsolationRoot = process.env.RECRUITOPS_DESKTOP_ISOLATION_ROOT;
const instanceDir = process.env.RECRUITOPS_DESKTOP_DATA_DIR
  || (app.isPackaged && packagedIsolationRoot ? path.join(packagedIsolationRoot, 'default-user') : undefined);
if (app.isPackaged && packagedIsolationRoot && instanceDir) {
  fs.mkdirSync(packagedIsolationRoot, { recursive: true });
  fs.mkdirSync(instanceDir, { recursive: true });
  process.env.RECRUITOPS_DESKTOP_DATA_DIR = instanceDir;
}
app.setPath('userData', instanceDir ? path.resolve(instanceDir) : path.join(app.getPath('appData'), 'RecruitOps-Desktop-Preview'));
const testMode = process.env.RECRUITOPS_DESKTOP_TEST === '1' && !!instanceDir && !app.isPackaged;
const fixtureOrigin = testMode ? isolatedApi(process.env.RECRUITOPS_DESKTOP_FIXTURE_ORIGIN) : undefined;
let api: string | undefined;
let configurationError = '';
if (process.env.RECRUITOPS_DESKTOP_API_ORIGIN) configurationError = 'External API addresses are disabled (including 8012/5433). Only an owned runtime may connect.';
let runtime: OwnedRuntime | undefined;
let browserService: BrowserService | undefined;
let bridge: DesktopBridge | undefined;
let runtimeStopped = false;
let runtimeRestarting = false;
let launchConfig: Launch | undefined;
let bootstrapStatus = 'offline';
let captureDraft: unknown;
let filler: FillerService | undefined;
let fillerStore: FillerStore | undefined;
let fillerStoreSnapshot: FillerSnapshot | undefined;
let fillerApplications: FillerApplicationService | undefined;
let applicationCandidates: Record<string, unknown>[] = [];
let applicationPage: ApplicationPage | undefined;
let applicationQueue: PendingRegistration[] = [];
let applicationMessage = '';
let applicationBatchResults: {index:number;status:'saved'|'queued'|'failed';error?:string;applicationId?:string}[] = [];
let fillerOpen = false;
const shellUrl = pathToFileURL(path.join(__dirname, '../renderer/index.html')).href;
let window: BrowserWindow;
let tray: Tray | undefined;
let quitting = false;
let active: number | 'workbench' | null = null;
let sequence = 0;
let notice = '';
let workbench: WebContentsView | undefined;
let workbenchError = false;
let workbenchLoading = false;
let workbenchRequested = true;
let workbenchAutoOpened = false;
const tabs = new Map<number, { view: WebContentsView; title: string; error: string; resourceWarning: string; background: boolean }>();
const stateFile = path.join(app.getPath('userData'), 'window.json');
const modeFile = path.join(app.getPath('userData'), 'desktop-mode.json');
function readOnlyPreference() {
  if (process.argv.includes('--read-only')) return true;
  try { return JSON.parse(fs.readFileSync(modeFile,'utf8')).readOnly === true; } catch { return false; }
}

function state() {
  const currentTab = typeof active === 'number' ? tabs.get(active)?.view.webContents : undefined;
  const engine = filler?.snapshot(currentTab) ?? {};
  const currentPage = currentTab && runtime?.state.instanceId ? filler?.pageContext(currentTab, runtime.state.instanceId) : undefined;
  const sameApplicationPage = !!applicationPage && !!currentPage && applicationPage.instanceId === currentPage.instanceId
    && applicationPage.tabId === currentPage.tabId && applicationPage.generation === currentPage.generation && applicationPage.url === currentPage.url;
  const profileData = fillerStoreSnapshot?.profile ?? {};
  const customAnswers = Array.isArray(fillerStoreSnapshot?.settings.customAnswers)
    ? fillerStoreSnapshot!.settings.customAnswers.filter(value => value && typeof value === 'object').slice(-200) : [];
  return { active, notice, apiConfigured: !!api, configurationError,
    runtime: runtime?.state ?? { status: bootstrapStatus, stage: 'resources', writes: false },
    browser: { ...browserIntegration, connected: !!browserService }, writesEnabled: runtime?.state.writes ?? false,
    bridge: bridge?.status ?? 'disabled',
    runtimeRestarting,
    workbenchLoading, workbenchRequested, workbenchError,
    captureDraft,
    filler: { available:!!filler, open:fillerOpen, ...engine,
      supportedActions: ['filler-open','filler-close','filler-profile-save','filler-profile-import','filler-profile-export',
        'filler-profile-create','filler-profile-select','filler-profile-rename','filler-profile-delete','filler-stop',
        'filler-attachment-select','filler-attachment-clear','filler-attachment-upload','filler-demo-enable','filler-demo-restore','filler-scan','filler-frame-allow',
        'filler-prepare','filler-fill','filler-undo','filler-custom-save','filler-custom-delete',
        'filler-application-detect','filler-application-save','filler-application-save-batch','filler-application-flush','filler-application-cancel',
        'filler-application-retry','filler-application-correct','filler-application-sync-page'],
      capabilities: { ...(engine as {capabilities?: object}).capabilities, persistentProfile:!!fillerStore, attachment:!!fillerStore,
        applications:!!fillerApplications, offlineQueue:!!fillerApplications },
      profile: { ready:Object.keys(profileData).length>0, mode:fillerStoreSnapshot?.mode ?? 'personal', data:profileData,
        version:fillerStoreSnapshot?.revision ?? 0, activeProfileId:fillerStoreSnapshot?.activeProfileId,
        activeProfileName:fillerStoreSnapshot?.activeProfileName, personalProfiles:fillerStoreSnapshot?.personalProfiles ?? [] },
      attachment: fillerStoreSnapshot?.attachment ? { ready:true, ...fillerStoreSnapshot.attachment } : { ready:false },
      customAnswers,
      application: { candidates:sameApplicationPage ? applicationCandidates : [], queue:applicationQueue, batchResults:applicationBatchResults,
        pendingCount:applicationQueue.length, message:!sameApplicationPage && applicationPage ? '页面已变化，请重新识别岗位。' : applicationMessage } },
    tabs: [...tabs].filter(([, t]) => !t.background).map(([id, t]) => ({
      id, title: t.title, url: t.view.webContents.getURL(), loading: t.view.webContents.isLoading(), error: t.error, resourceWarning: t.resourceWarning,
      back: t.view.webContents.navigationHistory.canGoBack(), forward: t.view.webContents.navigationHistory.canGoForward()
    })) };
}
function publish() { if (window && !window.isDestroyed()) window.webContents.send('desktop:state', state()); }
function refreshFillerStoreSnapshot() {
  if (!fillerStore) return;
  fillerStoreSnapshot = fillerStore.snapshot();
  const answers=Array.isArray(fillerStoreSnapshot.settings.customAnswers)?fillerStoreSnapshot.settings.customAnswers:[];
  const profile=Object.keys(fillerStoreSnapshot.profile).length?{...fillerStoreSnapshot.profile,customAnswers:answers}:undefined;
  filler?.setProfile(profile, fillerStoreSnapshot.revision);
}
async function initializeFillerInstance() {
  const id = runtime?.state.instanceId;
  if (!filler || !id || !/^[a-f0-9]{32}$/.test(id) || !safeStorage.isEncryptionAvailable()) return;
  if (fillerStore && fillerApplications) return;
  const root = app.getPath('userData');
  fillerStore = new FillerStore({ instanceRoot:root, instanceId:id, protection:{
    available:()=>safeStorage.isEncryptionAvailable(),
    protect:value=>safeStorage.encryptString(value.toString('base64')),
    unprotect:value=>Buffer.from(safeStorage.decryptString(value),'base64'),
  }, secureDirectory:directory=>{ try { fs.chmodSync(directory,0o700); } catch { /* Windows ACL remains owner scoped. */ } } });
  if (!Object.keys(fillerStore.snapshot().profile).length) {
    const isolation=process.env.RECRUITOPS_DESKTOP_ISOLATION_ROOT;
    if (isolation && path.isAbsolute(isolation) && fs.existsSync(isolation)) {
      for (const entry of fs.readdirSync(isolation,{withFileTypes:true}).slice(0,100)) {
        if (!entry.isDirectory() || entry.isSymbolicLink()) continue;
        const directory=path.join(isolation,entry.name),identity=path.join(directory,'instance.json');
        try {
          const stat=fs.lstatSync(identity);if(!stat.isFile()||stat.isSymbolicLink()||stat.size>65536)continue;
          const metadata=JSON.parse(fs.readFileSync(identity,'utf8'));
          if(metadata.instance_id!==id)continue;
          const legacy=path.join(directory,'.data','settings','filler-profile.json'),legacyStat=fs.lstatSync(legacy);
          if(!legacyStat.isFile()||legacyStat.isSymbolicLink()||legacyStat.size>1048576)break;
          fillerStore.importJson(fs.readFileSync(legacy,'utf8'));
          notice='已将当前实例原有简历闪填资料迁移到桌面版加密存储。';
          break;
        } catch { /* No valid legacy profile for this exact instance. */ }
      }
    }
  }
  refreshFillerStoreSnapshot();
  const queueStore = {
    load: async () => {
      const value=fillerStore?.snapshot().settings.applicationQueue;
      return value && typeof value==='object' && !Array.isArray(value) ? value as {instanceId:string;items:PendingRegistration[]} : undefined;
    },
    save: async (value:{instanceId:string;items:PendingRegistration[]}) => {
      const settings={...(fillerStore?.snapshot().settings || {}),applicationQueue:value};
      fillerStoreSnapshot=fillerStore!.saveSettings(settings); applicationQueue=structuredClone(value.items); publish();
    },
  };
  fillerApplications = new FillerApplicationService(id, () => runtime?.origin && runtime.state.status==='ready'
    ? { instanceId:id, origin:runtime.origin, token:runtime.authorization().replace(/^Bearer\s+/,'') } : undefined,
    () => {
      const tab=typeof active==='number'?tabs.get(active):undefined;
      return tab ? filler?.pageContext(tab.view.webContents,id) : undefined;
    }, queueStore, event => {
      if (workbench && !workbench.webContents.isDestroyed()) workbench.webContents.send('desktop:data-changed',event);
      publish();
    });
  applicationQueue=await fillerApplications.pending();
  publish();
}
function startOwnedRuntime(launch: Launch) {
  launchConfig = launch;
  workbenchAutoOpened = false;
  runtime = new OwnedRuntime(() => {
    api = runtime?.origin;
    if (!api && bridge) { bridge.stop(); bridge = undefined; }
    if (api && runtime?.state.websocket && browserService && !bridge) {
      try { bridge = new DesktopBridge(runtime, desktopDeviceId(app.getPath('userData')), reviewPage, publish); bridge.connect(); }
      catch { notice = 'Browser bridge device identity unavailable.'; }
    }
    if (!api && workbench) { window.contentView.removeChildView(workbench); workbench.webContents.close(); workbench = undefined; if (active === 'workbench') active = null; }
    // The packaged runtime publishes its origin before migrations and the API
    // listener are ready. Opening at that point can leave the first workbench
    // load failed permanently, so create/recover it only at the ready edge.
    if (api && workbenchRequested && runtime?.state.status === 'ready' && !workbenchAutoOpened) {
      workbenchAutoOpened = true;
      showWorkbench();
    }
    if (api && runtime?.state.status === 'ready') void initializeFillerInstance().catch(() => { notice='简历闪填资料存储无法初始化，请查看启动状态。'; publish(); });
    layout();
  }, launch);
  runtime.start();
}
async function changeWriteOptIn(enable: boolean) {
  if (runtimeRestarting || runtime?.state.status !== 'ready' || !runtime.state.instanceId || !launchConfig) throw new Error('Owned runtime must be ready');
  const id = runtime.state.instanceId;
  runtimeRestarting = true; publish();
  try {
    const { response } = await dialog.showMessageBox(window, { type: 'warning', buttons: ['取消', enable ? '恢复正常模式' : '重启为只读模式'], defaultId: 0, cancelId: 0,
      message: enable ? '恢复桌面正常业务模式？' : '切换为高级只读模式？',
      detail: `实例：${id}\n仅重启当前桌面后端，未保存的表单可能丢失。此选择会在当前用户空间保存。不会自动配置模型密钥或开启外部账户任务。` });
    if (response !== 1 || quitting) return;
    const args = [...launchConfig.args];
      const at = args.indexOf('--enable-writes-for-instance'); if (at >= 0) args.splice(at,2);
    const desktopAt = args.indexOf('--desktop'); if (desktopAt >= 0) args.splice(desktopAt,1);
    if (enable) args.push(...(app.isPackaged ? ['--desktop'] : ['--enable-writes-for-instance',id]));
    const next = { ...launchConfig, args, desktop:app.isPackaged && enable, expectedInstance: !app.isPackaged && enable ? id : undefined };
    if (app.isPackaged) fs.writeFileSync(modeFile,JSON.stringify({readOnly:!enable}));
    workbenchRequested = true;
    bridge?.stop(); bridge = undefined;
    await runtime.stop();
    if (!quitting) startOwnedRuntime(next);
  } finally { runtimeRestarting = false; publish(); }
}
async function applySavedConfiguration() {
  if (quitting || runtimeRestarting || runtime?.state.status !== 'ready' || !launchConfig)
    return { scheduled: false, reason: 'unavailable' };
  try {
    if ((await runtime.activity()).activeTasks.length)
      return { scheduled: false, reason: 'active_tasks' };
  } catch { return { scheduled: false, reason: 'activity_unknown' }; }
  runtimeRestarting = true;
  notice = '配置已保存，正在应用并重启本地服务…';
  publish();
  const next = launchConfig;
  setTimeout(() => {
    void (async () => {
      try {
        bridge?.stop(); bridge = undefined;
        await runtime?.stop();
        if (!quitting) startOwnedRuntime(next);
      } catch {
        notice = '配置已保存，但自动应用失败；请退出并重新打开软件。';
      } finally { runtimeRestarting = false; publish(); }
    })();
  }, 150);
  return { scheduled: true };
}
function layout() {
  const [width, height] = window.getContentSize();
  const fillerWidth = fillerOpen && typeof active === 'number'
    ? (width <= 1100 ? 360 : 420) : 0;
  for (const [id, tab] of tabs) {
    tab.view.setVisible(active === id && !tab.background);
    tab.view.setBounds({ x: 0, y: 144, width: Math.max(0, width - fillerWidth), height: Math.max(0, height - 144) });
  }
  if (workbench) {
    workbench.setVisible(active === 'workbench' && !workbenchLoading && !workbenchError && !fillerOpen);
    workbench.setBounds({ x: 0, y: 144, width, height: Math.max(0, height - 144) });
  }
  publish();
}
function denyPermissions(s: Session) {
  s.setPermissionCheckHandler(() => false);
  s.setPermissionRequestHandler((_contents, _permission, callback) => callback(false));
  s.setDevicePermissionHandler(() => false);
  s.on('will-download', (event) => { event.preventDefault(); notice = 'Downloads are disabled in this preview.'; publish(); });
}
function secureRecruitmentSession(s: Session) {
  denyPermissions(s);
  s.webRequest.onErrorOccurred(details => {
    if (!['script', 'stylesheet'].includes(details.resourceType) || details.error === 'net::ERR_ABORTED') return;
    const tab = [...tabs.values()].find(t => t.view.webContents.id === details.webContentsId);
    if (tab) {
      tab.resourceWarning = `部分页面资源未加载：${new URL(details.url).hostname}（${details.error}）。若页面功能异常，请刷新重试。`;
      publish();
    }
  });
  s.webRequest.onBeforeRequest((details, callback) => {
    void (async () => {
      try {
        const url = websiteUrl(details.url, fixtureOrigin);
        if (fixtureOrigin && new URL(url).origin === fixtureOrigin) return callback({ cancel: false });
        const addresses = await lookup(new URL(url).hostname, { all: true });
        callback({ cancel: !addresses.length || addresses.some(a => isPrivateHost(a.address)) });
      } catch { callback({ cancel: true }); }
    })();
  });
}
function guardNavigation(view: WebContentsView, allow: (url: string) => boolean, ownedOrigin?: string) {
  const allowed = (url: string, main: boolean) => allow(url) && (!main || !ownedOrigin || new URL(url).origin === ownedOrigin);
  view.webContents.on('will-navigate', (event, url) => { if (!allowed(url, true)) event.preventDefault(); });
  view.webContents.on('will-redirect', (event, url, _inPlace, main) => { if (!allowed(url, main)) event.preventDefault(); });
  view.webContents.on('will-frame-navigate', (event) => { if (!allowed(event.url, event.isMainFrame)) event.preventDefault(); });
  view.webContents.on('will-attach-webview', event => event.preventDefault());
}
function allowedSite(url: string) { try { websiteUrl(url, fixtureOrigin); return true; } catch { return false; } }
function createTab(url: string, background = false): number {
  url = websiteUrl(url, fixtureOrigin);
  if (tabs.size >= 24) throw new Error('Close a tab before opening another (limit 24)');
  const id = ++sequence;
  const view = new WebContentsView({ webPreferences: { ...securePreferences, partition: 'persist:recruitment',
    backgroundThrottling: !background, ...(background ? { offscreen: true } : {}) } });
  filler?.attach(view.webContents);
  tabs.set(id, { view, title: new URL(url).hostname, error: '', resourceWarning: '', background });
  // Offscreen rendering supplies frames even while both view and window are hidden;
  // disabling timer throttling alone does not drive requestAnimationFrame.
  // Hide before attaching so a visible workbench never exposes the review page.
  view.setVisible(false);
  window.contentView.addChildView(view);
  guardNavigation(view, allowedSite, background ? new URL(url).origin : undefined);
  view.webContents.setWindowOpenHandler(({ url: target }) => {
    if (!background && allowedSite(target)) {
      notice = 'Popup opened as an isolated tab. Opener-dependent SSO may require a supported browser.';
      try { createTab(target); } catch { notice = 'Popup blocked: tab limit reached.'; publish(); }
    }
    return { action: 'deny' };
  });
  view.webContents.on('page-title-updated', (_event, title) => { const t = tabs.get(id); if (t) t.title = title.slice(0, 100); publish(); });
  view.webContents.on('did-start-loading', () => { const tab = tabs.get(id); if (tab) { tab.error = ''; tab.resourceWarning = ''; } publish(); });
  view.webContents.on('did-stop-loading', publish);
  view.webContents.on('did-navigate', publish);
  view.webContents.on('did-navigate-in-page', publish);
  view.webContents.on('did-fail-load', (_event, code, _description, _url, main) => {
    if (main && code !== -3) { const t = tabs.get(id); if (t) t.error = `Page load failed (${code}). Login or embed compatibility is not verified.`; publish(); }
  });
  view.webContents.on('render-process-gone', () => { const t = tabs.get(id); if (t) t.error = 'Page renderer stopped. Reload to retry.'; publish(); });
  if (!background) { active = id; fillerOpen = false; }
  layout();
  void view.webContents.loadURL(url).catch(() => {});
  return id;
}
function closeTab(id: number) {
  const tab = tabs.get(id);
  if (!tab) return;
  window.contentView.removeChildView(tab.view);
  tab.view.webContents.close();
  tabs.delete(id);
  if (active === id) { active = null; showWorkbench(); }
  else layout();
}
// Main-process-only lease. Review views are hosted but never shown or focused.
export function createBackgroundPage(url: string): BackgroundPageLease {
  const id = createTab(url, true);
  return { id, purpose: 'review', close: () => closeTab(id) };
}
// Trusted backend integration can call only typed actions; recruitment pages have no IPC.
async function waitForReviewLoad(wc: WebContents, deadline: number, signal?: AbortSignal) {
  if (signal?.aborted || wc.isDestroyed()) throw new Error('browser_cancelled');
  if (!wc.isLoadingMainFrame()) return;
  const timeoutAt = Math.min(deadline, Date.now() + 20000);
  if (timeoutAt <= Date.now()) throw new Error('browser_load_timeout');
  await new Promise<void>((resolve, reject) => {
    let settled = false;
    let timer: NodeJS.Timeout;
    const stop = () => { if (!wc.isLoadingMainFrame()) finish(); };
    const failed = (_event: unknown, code: number, _description: string, _url: string, main: boolean) => {
      if (main && code !== -3) finish(new Error('browser_load_failed'));
    };
    const destroyed = () => finish(new Error('browser_cancelled'));
    const aborted = () => finish(new Error('browser_cancelled'));
    const finish = (error?: Error) => {
      if (settled) return;
      settled = true; clearTimeout(timer);
      wc.removeListener('did-stop-loading', stop); wc.removeListener('did-fail-load', failed); wc.removeListener('destroyed', destroyed);
      signal?.removeEventListener('abort', aborted);
      error ? reject(error) : resolve();
    };
    timer = setTimeout(() => finish(new Error('browser_load_timeout')), timeoutAt - Date.now());
    wc.on('did-stop-loading', stop); wc.on('did-fail-load', failed); wc.on('destroyed', destroyed);
    signal?.addEventListener('abort', aborted, { once: true });
    if (signal?.aborted || wc.isDestroyed()) destroyed();
    else stop();
  });
}

export async function reviewPage(url: string, operationId: string, applicationIds: string[], signal?: AbortSignal,
  deadline = Date.now() + REVIEW_OPERATION_BUDGET_MS, onStage?: (stage: ReviewStage) => void) {
  if (!browserService) throw new Error('browser_adapter_unavailable');
  if (signal?.aborted) throw new Error('browser_cancelled');
  // Leave a small margin to return a useful readiness error before the transport expires.
  deadline = Math.min(deadline, Date.now() + REVIEW_OPERATION_BUDGET_MS) - 100;
  const targetUrl = websiteUrl(url, fixtureOrigin);
  const ownedOrigin = new URL(targetUrl).origin;
  const lease = createBackgroundPage(targetUrl);
  const wc = tabs.get(lease.id)!.view.webContents;
  const cancel = () => lease.close();
  let pageNavigationAttempted = false;
  let foreignRedirect = false;
  const notePageNavigation = (_event: unknown, navigationUrl: string) => {
    try { if (new URL(navigationUrl).origin !== ownedOrigin) pageNavigationAttempted = true; }
    catch { pageNavigationAttempted = true; }
  };
  const noteRedirect = (_event: unknown, navigationUrl: string, _inPlace: boolean, isMainFrame: boolean) => {
    if (isMainFrame === false) return;
    try { if (new URL(navigationUrl).origin !== ownedOrigin) foreignRedirect = true; }
    catch { foreignRedirect = true; }
  };
  wc.on('will-navigate', notePageNavigation);
  wc.on('will-redirect', noteRedirect);
  signal?.addEventListener('abort', cancel, { once: true });
  try {
    try { await waitForReviewLoad(wc, deadline, signal); }
    catch (error) {
      if (signal?.aborted) throw error;
      if (pageNavigationAttempted || foreignRedirect) throw new Error('browser_navigation_changed');
      throw error;
    }
    if (pageNavigationAttempted || foreignRedirect) throw new Error('browser_navigation_changed');
    if (tabs.get(lease.id)?.error) throw new Error('browser_load_failed');
    if (signal?.aborted) throw new Error('browser_cancelled');
    const loadedUrl = new URL(wc.getURL());
    if (!['http:', 'https:'].includes(loadedUrl.protocol) || loadedUrl.username || loadedUrl.password ||
        loadedUrl.origin !== ownedOrigin) {
      throw new Error('browser_navigation_changed');
    }
    const result = await browserService.observeForReview(wc, operationId, applicationIds,
      { deadline, signal, onStage, ownedOrigin, requestedUrl: targetUrl });
    if (pageNavigationAttempted || foreignRedirect) throw new Error('browser_navigation_changed');
    return result;
  } finally {
    signal?.removeEventListener('abort', cancel);
    wc.removeListener('will-navigate', notePageNavigation);
    wc.removeListener('will-redirect', noteRedirect);
    lease.close();
  }
}
function showWorkbench() {
  fillerOpen = false;
  workbenchRequested = true;
  if (!api) { active = null; notice = '正在启动本地服务，就绪后将自动打开工作台。'; layout(); return; }
  if (!workbench) {
    workbenchError = false; workbenchLoading = true;
    const s = session.fromPartition('persist:workbench');
    denyPermissions(s);
    s.webRequest.onBeforeRequest((details, callback) => {
      callback({ cancel: !runtime?.allows(details.url, details.method) });
    });
    s.webRequest.onBeforeSendHeaders((details, callback) => {
      if (!runtime?.allows(details.url, details.method)) return callback({ cancel: true });
      const headers = { ...details.requestHeaders };
      for (const name of Object.keys(headers)) if (name.toLowerCase() === 'authorization') delete headers[name];
      headers.Authorization = runtime.authorization();
      callback({ requestHeaders: headers });
    });
    void s.setProxy({ mode: 'direct' });
    workbench = new WebContentsView({ webPreferences: { ...securePreferences, session: s, preload: path.join(__dirname, 'workbench-preload.js') } });
    window.contentView.addChildView(workbench);
    const openWebsite = (url: string) => {
      try { createTab(url); notice = ''; }
      catch { notice = '无法打开此链接：仅支持公开 HTTPS 官网地址，或已达到标签页数量上限。'; }
      publish();
    };
    workbench.webContents.setWindowOpenHandler(({ url }) => {
      openWebsite(url);
      return { action: 'deny' };
    });
    // will-frame-navigate precedes will-navigate; route only the top-level link.
    workbench.webContents.on('will-frame-navigate', event => {
      if (!event.isMainFrame) return;
      try { if (new URL(event.url).origin === api) return; } catch { /* Reject malformed destinations below. */ }
      event.preventDefault();
      openWebsite(event.url);
    });
    guardNavigation(workbench, url => { try { return new URL(url).origin === api; } catch { return false; } });
    workbench.webContents.on('did-start-loading', () => { workbenchLoading = true; layout(); });
    workbench.webContents.on('did-finish-load', () => { workbenchLoading = false; workbenchError = false; notice = ''; layout(); });
    workbench.webContents.on('did-fail-load', (_event, code, _description, _url, main) => {
      if (main && code !== -3) { workbenchError = true; workbenchLoading = false; notice = `工作台加载失败（${code}），请点击工作台重试。`; active = null; layout(); }
    });
    void workbench.webContents.loadURL(api + '/').catch(() => {});
  } else if (workbenchError) {
    workbenchError = false;
    workbench.webContents.reload();
  }
  active = 'workbench'; layout();
}

async function syncObservedApplicationStatus(wc: WebContents, context: ApplicationPage, applicationIds: string[]) {
  if (!applicationIds.length || !browserService || !fillerApplications || wc.getURL() !== context.url) return [];
  const observation = await browserService.observe(wc, randomUUID(), applicationIds);
  return fillerApplications.syncObservations(context, applicationIds, observation as Record<string, unknown>);
}

function learnedRecruitCompany(url: string): string {
  const key=recruitCompanyCacheKey(url);
  const raw=fillerStore?.snapshot().settings.recruitCompanyByHost;
  if(!key || !raw || typeof raw!=='object' || Array.isArray(raw)) return '';
  const company=(raw as Record<string,unknown>)[key];
  return typeof company==='string' && company.trim().length<=255 ? company.trim() : '';
}

function rememberRecruitCompany(url: string, company: string) {
  const key=recruitCompanyCacheKey(url), name=company.trim();
  if(!key || !name || name.length>255 || !fillerStore) return;
  const settings=fillerStore.snapshot().settings;
  const raw=settings.recruitCompanyByHost;
  const prior=raw && typeof raw==='object' && !Array.isArray(raw) ? raw as Record<string,unknown> : {};
  if(prior[key]===name) return;
  const entries=Object.entries({...prior,[key]:name}).filter(([site,value])=>
    site!=='app.mokahr.com' && typeof value==='string' && value.trim() && value.length<=255).slice(-199);
  fillerStoreSnapshot=fillerStore.saveSettings({...settings,recruitCompanyByHost:Object.fromEntries(entries)});
  refreshFillerStoreSnapshot();
}

async function execute(raw: unknown) {
  const command = parseCommand(raw);
  if (['home','select','close','capture'].includes(command.action)) fillerOpen=false;
  notice = '';
  if (command.action.startsWith('filler-')) {
    if(command.action==='filler-close') { fillerOpen=false;layout();return state(); }
    const tab=typeof active==='number'?tabs.get(active):undefined;
    if(!tab || tab.background || !filler) throw new Error('请先选择招聘官网标签页。');
    const wc=tab.view.webContents;
    if(command.action==='filler-open') fillerOpen=true;
    if(command.action==='filler-plugin' || command.action==='filler-profile') throw new Error('桌面版已内置填写引擎，请在“简历资料”中导入 JSON。');
    if(!fillerStore || !fillerStoreSnapshot) throw new Error('简历资料存储尚未就绪，请稍后重试。');
    if(filler.snapshot(wc).busy && /^(filler-profile-|filler-demo-|filler-custom-|filler-attachment-)/.test(command.action)) {
      throw new Error('填写操作尚未结束，请先停止并等待结束后再修改资料或附件。');
    }
    if(command.action==='filler-profile-save') {
      fillerStoreSnapshot=fillerStore.saveProfile(command.profile,command.expectedVersion);
      refreshFillerStoreSnapshot();
    }
    if(command.action==='filler-profile-create') {fillerStoreSnapshot=fillerStore.createProfile(command.name,command.expectedVersion);refreshFillerStoreSnapshot();}
    if(command.action==='filler-profile-select') {fillerStoreSnapshot=fillerStore.selectProfile(command.profileId,command.expectedVersion);refreshFillerStoreSnapshot();}
    if(command.action==='filler-profile-rename') {fillerStoreSnapshot=fillerStore.renameProfile(command.profileId,command.name,command.expectedVersion);refreshFillerStoreSnapshot();}
    if(command.action==='filler-profile-delete') {fillerStoreSnapshot=fillerStore.deleteProfile(command.profileId,command.expectedVersion);refreshFillerStoreSnapshot();}
    if(command.action==='filler-profile-import') {
      const selection=await dialog.showOpenDialog(window,{title:'导入简历资料 JSON',properties:['openFile'],filters:[{name:'JSON',extensions:['json']}]});
      if(!selection.canceled && selection.filePaths.length===1) {
        const filename=selection.filePaths[0],info=fs.lstatSync(filename);
        if(!info.isFile()||info.isSymbolicLink()||info.size>1048576) throw new Error('资料文件过大或不是普通文件。');
        fillerStoreSnapshot=fillerStore.importJson(fs.readFileSync(filename,'utf8'));
        refreshFillerStoreSnapshot();
      }
    }
    if(command.action==='filler-profile-export') {
      const target=await dialog.showSaveDialog(window,{title:'导出简历资料',defaultPath:'resume-profile.json',filters:[{name:'JSON',extensions:['json']}]});
      if(!target.canceled&&target.filePath) fs.writeFileSync(target.filePath,fillerStore.exportJson(),{encoding:'utf8',mode:0o600});
    }
    if(command.action==='filler-attachment-select') {
      const selection=await dialog.showOpenDialog(window,{title:'选择默认简历附件',properties:['openFile'],filters:[{name:'简历附件',extensions:['pdf','doc','docx']}]});
      if(!selection.canceled&&selection.filePaths.length===1) fillerStoreSnapshot=fillerStore.replaceAttachment(selection.filePaths[0]);
    }
    if(command.action==='filler-attachment-clear') fillerStoreSnapshot=fillerStore.clearAttachment();
    if(command.action==='filler-attachment-upload') {
      let confirmed=command.confirmed===true;
      if(!confirmed) {
        const confirmation=await dialog.showMessageBox(window,{type:'question',buttons:['取消','确认上传'],defaultId:0,cancelId:0,message:'上传默认简历到当前官网？',detail:`${wc.getURL()}\n${fillerStoreSnapshot.attachment?.name||''}\n网站接收后的文件不能通过本地撤销收回。`});
        confirmed=confirmation.response===1;
      }
      if(confirmed) {
        const result=await filler.uploadAttachment(wc,command.scanId,command.fieldId,fillerStore);
        if(!result.ok)throw new Error('网站未确认简历附件，已停止后续填写；请检查页面。');
      }
    }
    if(command.action==='filler-demo-enable') {fillerStoreSnapshot=fillerStore.setMode('demo');refreshFillerStoreSnapshot();}
    if(command.action==='filler-demo-restore') {fillerStoreSnapshot=fillerStore.setMode('personal');refreshFillerStoreSnapshot();}
    if(command.action==='filler-custom-save') {
      const settings=fillerStore.snapshot().settings;
      const prior=Array.isArray(settings.customAnswers)?settings.customAnswers.filter(value=>value&&typeof value==='object'):[];
      const origin=command.scope==='site'?new URL(wc.getURL()).origin:'*';
      const id=command.answerId||randomUUID();
      const answer={id,origin,pathname:command.scope==='site'?new URL(wc.getURL()).pathname:'*',label:command.question,value:command.answer,...(command.fieldId?{fieldId:command.fieldId}:{})};
      const next=command.answerId?prior.map(value=>(value as any).id===command.answerId?answer:value):[...prior,answer];
      if(command.answerId&&!prior.some(value=>(value as any).id===command.answerId))throw new Error('自定义答案已变化，请刷新后重试。');
      fillerStoreSnapshot=fillerStore.saveSettings({...settings,customAnswers:next.slice(-200)});
      refreshFillerStoreSnapshot();
    }
    if(command.action==='filler-custom-delete') {
      const settings=fillerStore.snapshot().settings;
      const prior=Array.isArray(settings.customAnswers)?settings.customAnswers.filter(value=>value&&typeof value==='object'):[];
      const next=prior.filter(value=>(value as any).id!==command.answerId);
      if(next.length===prior.length)throw new Error('自定义答案已变化，请刷新后重试。');
      fillerStoreSnapshot=fillerStore.saveSettings({...settings,customAnswers:next});
      refreshFillerStoreSnapshot();
    }
    if(command.action==='filler-scan') {
      const id=runtime?.state.instanceId;if(!id)throw new Error('本地实例尚未就绪。');
      const settings=fillerStore.snapshot().settings;
      const site=new URL(wc.getURL()).origin;
      const scoped=settings.allowedFrameOriginsBySite;
      const saved=scoped&&typeof scoped==='object'&&!Array.isArray(scoped)?(scoped as Record<string,unknown>)[site]:undefined;
      const origins=[...(Array.isArray(settings.allowedFrameOrigins)?settings.allowedFrameOrigins.filter(value=>typeof value==='string') as string[]:[]),
        ...(Array.isArray(saved)?saved.filter(value=>typeof value==='string') as string[]:[])];
      await filler.scan(wc,id,origins);
    }
    if(command.action==='filler-frame-allow') {
      const id=runtime?.state.instanceId;if(!id)throw new Error('本地实例尚未就绪。');
      const origin=filler.snapshot(wc).blockedFrameOrigins[0];
      if(!origin)throw new Error('当前扫描没有可授权的嵌入表单，请重新扫描。');
      websiteUrl(origin);
      const pageUrl=wc.getURL(),site=new URL(pageUrl).origin;
      const {response}=await dialog.showMessageBox(window,{type:'question',buttons:['取消','允许并重新扫描'],defaultId:0,cancelId:0,
        message:'允许读取此招聘网站的嵌入表单？',
        detail:`当前网站：${site}\n嵌入表单：${origin}\n只对当前招聘网站授权。仍需您确认后才会填写，不会自动提交。`});
      if(response!==1)return state();
      if(wc.isDestroyed()||wc.getURL()!==pageUrl||!filler.snapshot(wc).blockedFrameOrigins.includes(origin))
        throw new Error('页面已变化，请重新扫描后授权。');
      const settings=fillerStore.snapshot().settings;
      const raw=settings.allowedFrameOriginsBySite;
      const scoped=raw&&typeof raw==='object'&&!Array.isArray(raw)?raw as Record<string,unknown>:{};
      const prior=Array.isArray(scoped[site])?scoped[site].filter(value=>typeof value==='string') as string[]:[];
      if(!prior.includes(origin)&&prior.length>=8)throw new Error('当前网站的嵌入表单授权已达到上限。');
      fillerStoreSnapshot=fillerStore.saveSettings({...settings,allowedFrameOriginsBySite:{...scoped,[site]:[...new Set([...prior,origin])]}});
      refreshFillerStoreSnapshot();
      const legacy=Array.isArray(settings.allowedFrameOrigins)?settings.allowedFrameOrigins.filter(value=>typeof value==='string') as string[]:[];
      await filler.scan(wc,id,[...new Set([...legacy,...prior,origin])]);
    }
    if(command.action==='filler-fill') {
      if(typeof active!=='number' || tabs.get(active)?.view.webContents!==wc) throw new Error('标签页已变化，请重新扫描。');
      await filler.fill(wc,command.scanId,command.fieldIds);
    }
    if(command.action==='filler-prepare') await filler.prepare(wc);
    if(command.action==='filler-stop') await filler.stop(wc);
    if(command.action==='filler-undo') await filler.undo(wc);
    if(command.action==='filler-application-detect') {
      const id=runtime?.state.instanceId;if(!id)throw new Error('本地实例尚未就绪。');
      applicationCandidates=[];
      applicationBatchResults=[];
      applicationMessage='';
      await waitForReviewLoad(wc,Date.now()+12000);
      applicationPage=filler.pageContext(wc,id);
      const settings=fillerStore.snapshot().settings;
      const site=new URL(wc.getURL()).origin;
      const scoped=settings.allowedFrameOriginsBySite;
      const saved=scoped&&typeof scoped==='object'&&!Array.isArray(scoped)?(scoped as Record<string,unknown>)[site]:undefined;
      const origins=[...(Array.isArray(settings.allowedFrameOrigins)?settings.allowedFrameOrigins.filter(value=>typeof value==='string') as string[]:[]),
        ...(Array.isArray(saved)?saved.filter(value=>typeof value==='string') as string[]:[])];
      const context=await filler.readApplicationContext(wc,id,origins);
      if(!context.titles.length && browserService) {
        const observed=await browserService.observe(wc,randomUUID(),[]) as any;
        const records=Array.isArray(observed?.result?.application_records)?observed.result.application_records:[];
        context.titles=[...new Set<string>(records.filter((row:any)=>typeof row?.title==='string'&&row.title.trim()).map((row:any)=>row.title.trim()))].slice(0,50);
      }
      const current=filler.pageContext(wc,id);
      if(!applicationPage || !current || current.generation!==applicationPage.generation || current.url!==context.url ||
          typeof active!=='number' || tabs.get(active)?.view.webContents!==wc) throw new Error('当前页面已变化，请重新识别岗位。');
      // A company identified on the current page outranks a previously saved
      // host-level hint (shared ATS hosts can serve multiple employers).
      const company=context.company||learnedRecruitCompany(context.url);
      applicationCandidates=context.titles.map((title,index)=>({id:`candidate-${index}`,company,title,recordUrl:context.url,
        date:context.records.find(row=>row.title===title)?.date||'',sourceStatus:context.records.find(row=>row.title===title)?.sourceStatus||''}));
      applicationMessage=context.titles.length?`已读取 ${context.titles.length} 个岗位，请核对后新增。`:'未识别到岗位，可手动填写后新增。';
    }
    if(command.action==='filler-application-save') {
      if(!fillerApplications) throw new Error('投递记录服务尚未就绪。');
      const id=runtime?.state.instanceId;if(!id)throw new Error('本地实例尚未就绪。');
      const context=filler.pageContext(wc,id);if(!context)throw new Error('当前页面已变化。');
      const result=await fillerApplications.register(context,{company:command.company,title:command.title,record_url:command.recordUrl,
        ...(command.city?{city:command.city}:{}),progress_url_confirmed:true},true);
      applicationQueue=await fillerApplications.pending();applicationMessage=(result as any).queued?'本地服务暂不可用，已加入待补传队列。':
        command.recordUrl?'投递记录已保存。':'投递记录已保存；进度链接待补，官网状态暂不能自动复核。';
      try {rememberRecruitCompany(command.recordUrl,command.company);} catch { /* Registration is already committed. */ }
      const applicationId=(result as any).result?.application_id;
      if(!(result as any).queued&&typeof applicationId==='string'&&command.recordUrl===context.url&&
          applicationCandidates.some(item=>item.title===command.title&&item.sourceStatus)) {
        try {
          const synced=await syncObservedApplicationStatus(wc,context,[applicationId]);
          applicationMessage=synced[0]?.success?'投递记录已保存，官网状态已核实并同步。':'投递记录已保存；官网状态未能核实，原有进度未改动。';
        } catch {applicationMessage='投递记录已保存；官网状态同步未完成，原有进度未改动。';}
      }
    }
    if(command.action==='filler-application-save-batch') {
      if(!fillerApplications) throw new Error('投递记录服务尚未就绪。');
      const id=runtime?.state.instanceId;if(!id)throw new Error('本地实例尚未就绪。');
      const context=filler.pageContext(wc,id);if(!context)throw new Error('当前页面已变化。');
      applicationBatchResults=[];
      applicationBatchResults=await fillerApplications.registerBatch(context,command.records.map(record=>({
        company:record.company,title:record.title,record_url:record.recordUrl,city:record.city,progress_url_confirmed:true})),true);
      applicationQueue=await fillerApplications.pending();
      if(applicationBatchResults.some(row=>row.status==='saved'||row.status==='queued'))
        try {rememberRecruitCompany(context.url,command.records[0].company);} catch { /* Registration is already committed. */ }
      const count=(status:string)=>applicationBatchResults.filter(row=>row.status===status).length;
      applicationMessage=`新增完成：已保存 ${count('saved')} 条，待补传 ${count('queued')} 条，失败 ${count('failed')} 条。`;
      const ids=applicationBatchResults.filter(row=>row.status==='saved'&&row.applicationId&&
        applicationCandidates.some(item=>item.title===command.records[row.index]?.title&&item.sourceStatus))
        .map(row=>row.applicationId!);
      if(ids.length&&command.records.every(row=>row.recordUrl===context.url)) {
        try {
          const synced=await syncObservedApplicationStatus(wc,context,ids);
          applicationMessage+=` 官网状态核实并同步 ${synced.filter(row=>row.success).length} 条；未核实的记录未改变原有进度。`;
        } catch {applicationMessage+=' 官网状态同步未完成，原有进度未改动。';}
      }
    }
    if(command.action==='filler-application-sync-page') {
      if(!fillerApplications || !browserService) throw new Error('官网进度同步尚未就绪。');
      const id=runtime?.state.instanceId;if(!id)throw new Error('本地实例尚未就绪。');
      const context=filler.pageContext(wc,id);
      if(!context || !applicationPage || context.generation!==applicationPage.generation ||
          context.tabId!==applicationPage.tabId || context.url!==applicationPage.url) throw new Error('页面已变化，请重新识别。');
      const cards=command.candidateIds.map(candidateId=>applicationCandidates.find(item=>item.id===candidateId));
      const titles=cards.map(item=>{
        if(typeof item?.title!=='string' || !item.title.trim() || typeof item.sourceStatus!=='string' || !item.sourceStatus.trim())
          throw new Error('所选岗位缺少当前页官网状态，请重新识别。');
        return item.title;
      });
      const matches=await fillerApplications.candidates(context,titles.map(title=>({company:command.company,title})));
      if(matches.some(item=>item.existing.length!==1 || typeof item.existing[0]?.id!=='string'))
        throw new Error('所选岗位没有唯一的已有投递记录，请在工作台核对后重试。');
      const ids=matches.map(item=>item.existing[0].id as string);
      if(new Set(ids).size!==ids.length) throw new Error('所选岗位对应同一投递记录，无法批量同步。');
      const synced=await syncObservedApplicationStatus(wc,context,ids);
      applicationMessage=`当前页进度已核实并同步 ${synced.filter(row=>row.success).length} 条；未核实 ${synced.filter(row=>!row.success).length} 条，原阶段未改动。`;
    }
    if(command.action==='filler-application-cancel') {if(!fillerApplications)throw new Error('投递记录服务尚未就绪。');await fillerApplications.cancel(command.queueId);applicationQueue=await fillerApplications.pending();}
    if(command.action==='filler-application-correct') {
      if(!fillerApplications)throw new Error('投递记录服务尚未就绪。');
      await fillerApplications.correctLink(command.queueId,command.recordUrl,command.city,true);
      applicationQueue=await fillerApplications.pending();
      applicationMessage='待补传链接已修正，尚未写入投递记录；请点击该条重试。';
    }
    if(command.action==='filler-application-retry') {
      if(!fillerApplications)throw new Error('投递记录服务尚未就绪。');
      const [result]=await fillerApplications.retry([command.queueId]);
      applicationQueue=await fillerApplications.pending();
      applicationMessage=result?.queued?'该条补传未完成，请查看队列中的原因。':'该条投递记录已补传。';
    }
    if(command.action==='filler-application-flush') {if(!fillerApplications)throw new Error('投递记录服务尚未就绪。');await fillerApplications.retry(applicationQueue.map(item=>item.id));applicationQueue=await fillerApplications.pending();}
    layout();return state();
  }
  if (command.action === 'open') createTab(command.url);
  else if (command.action === 'select') { if (!tabs.has(command.id) || tabs.get(command.id)!.background) throw new Error('Unknown tab'); active = command.id; layout(); }
  else if (command.action === 'close') { if (tabs.get(command.id)?.background) throw new Error('Unknown tab'); closeTab(command.id); }
  else if (command.action === 'home') { workbenchRequested = false; active = null; layout(); }
  else if (command.action === 'workbench') showWorkbench();
  else if (command.action === 'reload' && active === 'workbench') workbench?.webContents.reload();
  else if (command.action === 'enable-writes') await changeWriteOptIn(true);
  else if (command.action === 'disable-writes') await changeWriteOptIn(false);
  else if (command.action === 'capture') {
    const tab = typeof active === 'number' ? tabs.get(active) : undefined;
    if (!tab || tab.background || !browserService) throw new Error('Select an owned recruitment page first');
    captureDraft = await browserService.observe(tab.view.webContents, randomUUID(), [], true);
    notice = 'Capture is an evidence-only draft; no business record has been saved.';
    workbenchRequested = false;
    active = null; layout();
  }
  else if (command.action === 'use-capture') {
    const draft = (captureDraft as { result?: { draft?: { title?: string; url?: string; page_text?: string } } })?.result?.draft;
    if (!draft || !api || !runtime?.origin || typeof draft.title !== 'string' || !draft.title.trim()) throw new Error('Capture draft and owned workbench are required');
    const url = new URL(draft.url || '');
    if (!['https:', 'http:'].includes(url.protocol) || url.username || url.password) throw new Error('Invalid captured URL');
    const value = { company_name: '', job_title: draft.title.slice(0, 512), record_url: '', note: `${url.href}\n${draft.page_text || ''}`.slice(0, 4000) };
    showWorkbench();
    const wc = workbench!.webContents;
    const deliver = () => { if (!wc.isDestroyed() && runtime?.origin === api && new URL(wc.getURL()).origin === api) wc.send('desktop:application-draft', value); };
    if (wc.isLoadingMainFrame()) wc.once('did-finish-load', deliver); else deliver();
  }
  else if (command.action === 'hide') { if (tray) window.hide(); }
  else if (command.action === 'quit') {
    let activeTasks: { runId: string; currentStep: string }[] = [];
    try { activeTasks = (await runtime?.activity())?.activeTasks ?? []; } catch { /* Fall back to the general warning. */ }
    if (activeTasks.length) {
      const summary = activeTasks.slice(0,3).map(task => `${task.runId.slice(0,12)}…（${task.currentStep}）`).join('\n');
      const { response } = await dialog.showMessageBox(window, {
        type: 'warning', title: '任务仍在运行', buttons: ['继续运行', '停止任务并退出'], defaultId: 0, cancelId: 0,
        message: `当前有 ${activeTasks.length} 个后台任务尚未完成。`,
        detail: `${summary}\n\n退出会立即停止这些任务，未完成的阶段需要下次重新运行。选择“继续运行”将保留当前窗口并让任务继续。`,
      });
      if (response === 0) return state();
    } else {
      const { response } = await dialog.showMessageBox(window, { type: 'question', title: '退出 RecruitOps', buttons: ['取消', '退出'], defaultId: 0, cancelId: 0, message: '确定退出 RecruitOps？', detail: '网页中未保存的填写内容可能丢失。退出后，本地服务及后台任务将停止。' });
      if (response === 0) return state();
    }
    quitting = true; app.quit();
  } else {
    const tab = typeof active === 'number' ? tabs.get(active) : undefined;
    if (!tab) return state();
    const wc = tab.view.webContents;
    if (command.action === 'back' && wc.navigationHistory.canGoBack()) wc.navigationHistory.goBack();
    if (command.action === 'forward' && wc.navigationHistory.canGoForward()) wc.navigationHistory.goForward();
    if (command.action === 'reload') { tab.error = ''; wc.reload(); }
    if (command.action === 'clear-site') {
      const origin = new URL(wc.getURL()).origin;
      const { response } = await dialog.showMessageBox(window, { title: '清除此站登录', buttons: ['取消', '清除登录'], defaultId: 0, cancelId: 0, message: `确定退出登录并清除 ${origin} 的本地数据？`, detail: '其他单点登录站点的数据不会被清除。刷新后，未保存的表单内容将丢失。' });
      if (response === 1) {
        await wc.session.clearStorageData({ origin, storages: ['filesystem', 'indexdb', 'localstorage', 'serviceworkers', 'cachestorage'] });
        const host = new URL(origin).hostname;
        const cookies = await wc.session.cookies.get({});
        for (const cookie of cookies) {
          const domain = cookie.domain?.replace(/^\./, '');
          if (domain && (host === domain || host.endsWith('.' + domain))) await wc.session.cookies.remove(origin + cookie.path, cookie.name);
        }
        wc.reload();
      }
    }
  }
  publish(); return state();
}
if (!app.requestSingleInstanceLock()) app.quit();
else {
  app.on('second-instance', () => { if (window) { window.show(); if (window.isMinimized()) window.restore(); window.focus(); } });
  void app.whenReady().then(async () => {
    fs.mkdirSync(app.getPath('userData'), { recursive: true });
    try {
      if(app.isPackaged) require('../packaging/resources.cjs').validateFiller(path.join(process.resourcesPath,'desktop-filler'));
      filler=new FillerService(require(app.isPackaged?path.join(process.resourcesPath,'desktop-filler/index.cjs'):path.resolve(__dirname,'../../../packages/desktop_filler/index.cjs')),publish);
    }
    catch { notice='本地简历闪填模块暂不可用。'; }
    let size = { width: 1280, height: 800 };
    try { const saved = JSON.parse(fs.readFileSync(stateFile, 'utf8')); if (Number.isFinite(saved.width) && Number.isFinite(saved.height)) size = { width: Math.max(960, saved.width), height: Math.max(640, saved.height) }; } catch {}
    const area = screen.getPrimaryDisplay().workAreaSize;
    window = new BrowserWindow({ width: Math.min(size.width, area.width), height: Math.min(size.height, area.height), minWidth: 960, minHeight: 640, show: false, title: 'RecruitOps Desktop Preview', backgroundColor: '#f4f6f8', webPreferences: { ...securePreferences, preload: path.join(__dirname, 'preload.js'), partition: 'shell' } });
    Menu.setApplicationMenu(null);
    const shellSession = window.webContents.session;
    denyPermissions(shellSession);
    shellSession.webRequest.onBeforeRequest((details, callback) => callback({ cancel: ![shellUrl, pathToFileURL(path.join(__dirname, '../renderer/shell.js')).href, pathToFileURL(path.join(__dirname, '../renderer/styles.css')).href].includes(details.url) }));
    window.webContents.on('will-navigate', event => event.preventDefault());
    window.webContents.setWindowOpenHandler(() => ({ action: 'deny' }));
    secureRecruitmentSession(session.fromPartition('persist:recruitment'));
    try {
      const adapterPath = app.isPackaged ? path.join(process.resourcesPath, 'desktop-browser', 'index.cjs') : path.resolve(__dirname, '../../../packages/desktop_browser/index.cjs');
      const adapter = require(adapterPath) as BrowserAdapter & { loadObservationResources(): unknown };
      adapter.loadObservationResources(); browserService = new BrowserService(adapter);
    } catch { notice = 'Browser adapter resources unavailable; observation and capture disabled.'; }
    const startRuntime = async () => { try {
      bootstrapStatus = app.isPackaged ? 'verifying_resources' : 'offline'; publish();
      const repo = path.resolve(__dirname, '../../..');
      const fixtureMode = testMode && process.env.RECRUITOPS_DESKTOP_TEST_RUNTIME;
      if (fixtureMode && !['read-only', 'writes', 'identity-failure', 'crash', 'desktop', 'delayed-desktop', 'filler', 'active-task'].includes(fixtureMode)) throw new Error('invalid_fixture_mode');
      const launch = fixtureMode ? {
        executable: path.join(repo, '.venv-desktop-tests/Scripts/python.exe'),
        args: [path.join(__dirname, '../tests/runtime_fixture.py'), fixtureMode], cwd: repo,
        env: { SystemRoot: process.env.SystemRoot, WINDIR: process.env.WINDIR, PYTHONUTF8: '1', NO_PROXY: '127.0.0.1,localhost,::1' },
        expectedInstance: fixtureMode === 'writes' ? 'fixture-instance' : undefined,
        desktop: fixtureMode === 'desktop' || fixtureMode === 'delayed-desktop' || fixtureMode === 'filler' || fixtureMode === 'active-task'
      } : app.isPackaged ? await packagedRuntimeLaunch(process.resourcesPath, app.getPath('userData'), process.env,readOnlyPreference()) : runtimeLaunch(repo, process.env);
      if (quitting) return;
      if (launch) startOwnedRuntime(launch);
    } catch (error) {
      const code = (error as Error).message;
      configurationError = /^[a-z_]{1,100}$/.test(code) ? code : 'owned_runtime_configuration_failed';
      bootstrapStatus = 'failed'; publish();
    } };
    const assertSender = (event: Electron.IpcMainInvokeEvent) => {
      if (!trustedSender(event.sender.id, window.webContents.id, event.senderFrame?.url ?? '', shellUrl, event.senderFrame === window.webContents.mainFrame)) throw new Error('Untrusted IPC sender');
    };
    ipcMain.handle('desktop:state', event => { assertSender(event); return state(); });
    ipcMain.handle('desktop:command', async (event, command) => { assertSender(event); try { return await execute(command); } finally { layout(); } });
    ipcMain.handle('desktop:apply-saved-configuration', event => {
      if (!workbench || event.sender !== workbench.webContents ||
          event.senderFrame !== workbench.webContents.mainFrame ||
          !api || new URL(event.senderFrame.url).origin !== api) throw new Error('Untrusted IPC sender');
      return applySavedConfiguration();
    });
    // Native bitmap icon, no personal or external asset dependency.
    const pixels = Buffer.alloc(16 * 16 * 4);
    for (let i = 0; i < pixels.length; i += 4) { pixels[i] = 90; pixels[i + 1] = 145; pixels[i + 2] = 25; pixels[i + 3] = 255; }
    try {
      tray = new Tray(nativeImage.createFromBitmap(pixels, { width: 16, height: 16 }));
      tray.setToolTip('RecruitOps 秋招工作台');
      tray.setContextMenu(Menu.buildFromTemplate([{ label: '打开工作台', click: () => window.show() }, { label: '退出…', click: () => { window.show(); void execute({ action: 'quit' }); } }]));
      tray.on('double-click', () => window.show());
    } catch { notice = '系统托盘不可用，关闭窗口将退出软件。'; }
    window.on('resize', layout);
    window.on('close', event => {
      if (!quitting && tray && !testMode) { event.preventDefault(); window.hide(); }
    });
    window.once('ready-to-show', () => window.show());
    void window.loadURL(shellUrl).then(startRuntime);
  });
  app.on('before-quit', event => {
    quitting = true;
    if (runtime && !runtimeStopped) {
      event.preventDefault();
      bridge?.stop();
      void runtime.stop().then(() => { runtimeStopped = true; app.quit(); });
      return;
    }
    if (window && !window.isDestroyed()) { const [width, height] = window.getSize(); try { fs.writeFileSync(stateFile, JSON.stringify({ width, height })); } catch {} }
    for (const tab of tabs.values()) tab.view.webContents.close();
    tabs.clear(); workbench?.webContents.close(); tray?.destroy();
  });
  app.on('window-all-closed', () => app.quit());
}
