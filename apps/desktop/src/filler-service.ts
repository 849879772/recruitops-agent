import type { WebContents, WebFrameMain } from 'electron';
import type * as Adapter from '../../../packages/desktop_filler/index.cjs';
import type { FillerStore } from './filler-store';
import { FillerFrameExecutor } from './filler-frame-executor';

type Field = { fieldId: string; label: string; value: unknown; frameId: string; fillable: boolean; blocked: boolean; reason?: string };
type FrameBinding = { frame: WebFrameMain; route: Adapter.FrameContext; scanId: string; fieldIds: Set<string> };
type Binding = { wc: WebContents; url: string; generation: number; profileVersion: string; frames: Map<string, FrameBinding> };
type AttachmentBinding = { selectionId: string; frame: FrameBinding; target: Adapter.AttachmentTarget };
type ScanState = 'idle' | 'scanning' | 'complete' | 'partial' | 'failed' | 'consumed';
type ScanSummary = { framesScanned: number; framesFailed: number; controlsSeen: number; matched: number; needsAnswer: number; unsupported: number; attachments: number; filtered: number };
const emptyScanSummary = (): ScanSummary => ({ framesScanned: 0, framesFailed: 0, controlsSeen: 0, matched: 0, needsAnswer: 0, unsupported: 0, attachments: 0, filtered: 0 });

export class FillerService {
  private readonly bundle: Adapter.LocalFillerBundle;
  private profile?: Adapter.ResumeProfile;
  private profileVersion = '0';
  private generations = new Map<number, number>();
  private binding?: Binding;
  private fields: Field[] = [];
  private results: Record<string, unknown>[] = [];
  private diagnostics: Record<string, string>[] = [];
  private repeaters: { sectionId: string; label: string; desired: number; count: number }[] = [];
  private attachments: AttachmentBinding[] = [];
  private scanId = '';
  private scanState: ScanState = 'idle';
  private scanSummary = emptyScanSummary();
  private blockedFrameOrigins: string[] = [];
  private undoReady = false;
  private busy = false;
  private operation?: { binding: Omit<Binding, 'frames'>; timedOut: boolean; type: string; cancelRequested: boolean; frames: FrameBinding[] };
  private message = '';

  constructor(private adapter: typeof Adapter, private publish: () => void, private timeoutMs = 75000,
    private executor: Pick<FillerFrameExecutor, 'execute' | 'assignFile'> = new FillerFrameExecutor()) {
    this.bundle = adapter.loadDesktopFiller();
  }

  attach(wc: WebContents) {
    this.generations.set(wc.id, 0);
    const invalidate = () => {
      this.generations.set(wc.id, (this.generations.get(wc.id) || 0) + 1);
      if (this.binding?.wc === wc) { this.clearPlan(); this.message = '页面已变化，请重新扫描。'; this.publish(); }
      this.releaseNavigated(wc);
    };
    wc.on('did-start-navigation', (_event, _url, _inPlace, isMainFrame) => { if (isMainFrame) invalidate(); });
    wc.on('did-frame-navigate', invalidate);
    wc.on('render-process-gone', invalidate);
    wc.once('destroyed', () => { invalidate(); this.generations.delete(wc.id); });
  }

  setProfile(profile: Adapter.ResumeProfile | undefined, version: string | number = '0') {
    this.idle();
    this.profile = profile ? JSON.parse(JSON.stringify(profile)) : undefined;
    this.profileVersion = String(version);
    this.clearPlan();
    this.message = profile ? '简历资料已载入。' : '请先在“简历资料”中保存资料。';
  }

  pageContext(wc: WebContents, instanceId: string) {
    if (!this.generations.has(wc.id) || wc.isDestroyed()) return;
    return { instanceId, tabId: wc.id, generation: this.generations.get(wc.id)!, url: wc.getURL() };
  }

  private clearPlan() {
    this.binding = undefined; this.fields = []; this.results = []; this.repeaters = []; this.attachments = [];
    this.scanId = ''; this.scanState = 'idle'; this.scanSummary = emptyScanSummary(); this.undoReady = false;
    this.blockedFrameOrigins = [];
  }
  private idle() { if (this.busy) throw new Error('原操作尚未结束，请等待或刷新官网。'); }
  private valid(binding: Omit<Binding, 'frames'>) {
    return !binding.wc.isDestroyed() && binding.wc.getURL() === binding.url && this.generations.get(binding.wc.id) === binding.generation && binding.profileVersion === this.profileVersion;
  }
  private releaseNavigated(wc: WebContents) {
    // A child navigation does not stop work still running in sibling frames.
    if (this.operation?.binding.wc === wc && wc.isDestroyed()) { this.operation = undefined; this.busy = false; this.publish(); }
  }

  snapshot(wc?: WebContents) {
    const current = !!this.binding && this.binding.wc === wc && this.valid(this.binding);
    return {
      pluginReady: true, profileReady: !!this.profile, busy: this.busy,
      busyOperation: this.operation ? { type: this.operation.type, timedOut: this.operation.timedOut, cancelRequested: this.operation.cancelRequested } : undefined,
      fields: current ? this.fields : [], results: current ? this.results : [], scanId: current ? this.scanId : '',
      scanState: current ? this.scanState : 'idle', scanSummary: current ? this.scanSummary : emptyScanSummary(),
      blockedFrameOrigins: current ? this.blockedFrameOrigins : [],
      repeaters: current ? this.repeaters : [],
      attachmentTargets: current ? this.attachments.map(item=>({fieldId:item.selectionId,label:item.target.label,accept:item.target.accept,multiple:item.target.multiple})) : [],
      undoReady: current && this.undoReady, diagnostics: this.diagnostics, message: this.message,
      capabilities: { repeatedSections: true, frames: true, customAnswers: true, diagnostics: true },
    };
  }

  clearDiagnostics() { this.diagnostics = []; this.message = '诊断记录已清除。'; this.publish(); }

  private route(wc: WebContents, frame: WebFrameMain, instanceId: string): Adapter.FrameContext {
    return { instanceId, tabId: String(wc.id), frameId: `${frame.processId}:${frame.routingId}`,
      documentId: `${this.generations.get(wc.id) || 0}:${frame.url}`, profileVersion: this.profileVersion,
      href: frame.url, allowSubframe: frame !== wc.mainFrame };
  }

  private profileForFrame(route: Adapter.FrameContext): Adapter.ResumeProfile {
    const profile = this.profile! as Adapter.ResumeProfile & { customAnswers?: unknown[] };
    if (!Array.isArray(profile.customAnswers) || !profile.customAnswers.some(answer =>
      !!answer && typeof answer === 'object' && !Array.isArray(answer) &&
      (answer as Record<string, unknown>).origin === '*' && (answer as Record<string, unknown>).pathname === '*')) return profile;
    const page = new URL(route.href);
    return { ...profile, customAnswers: profile.customAnswers.map(answer => {
      if (!answer || typeof answer !== 'object' || Array.isArray(answer)) return answer;
      const item = answer as Record<string, unknown>;
      return item.origin === '*' && item.pathname === '*' ? { ...item, origin: page.origin, pathname: page.pathname } : item;
    }) };
  }

  private execute(wc: WebContents, frame: WebFrameMain, code: string) {
    return this.executor.execute(wc, frame, code);
  }

  async readApplicationContext(wc: WebContents, instanceId: string, allowedFrameOrigins: string[] = []): Promise<Adapter.ApplicationContext> {
    const operation = this.begin(wc, 'reading');
    const top = wc.mainFrame;
    const topUrl = new URL(operation.binding.url);
    const allowed = new Set([topUrl.origin]);
    for (const origin of allowedFrameOrigins.slice(0, 32)) {
      try { const parsed = new URL(origin); if (parsed.origin === origin && ['http:', 'https:'].includes(parsed.protocol)) allowed.add(origin); } catch { /* ignore malformed grants */ }
    }
    const frames = top.framesInSubtree.slice(0, 32);
    const task = Promise.all(frames.map(async frame => {
      if (frame.detached) return null;
      let frameUrl: URL;
      try { frameUrl = new URL(frame.url); } catch { return null; }
      if (frame !== top && !allowed.has(frameUrl.origin)) return null;
      const route = this.route(wc, frame, instanceId);
      try {
        const result = await this.execute(wc, frame, this.adapter.buildApplicationContextScript(this.bundle, route)) as Adapter.ApplicationContext;
        if (!result || result.url !== frame.url || typeof result.company !== 'string' || result.company.length > 255 ||
            !Array.isArray(result.titles) || result.titles.length > 50 ||
            result.titles.some(title => typeof title !== 'string' || !title.trim() || title.length > 512) ||
            !Array.isArray(result.records) || result.records.length !== result.titles.length ||
            result.records.some((row,index) => !row || row.title !== result.titles[index] ||
              typeof row.date !== 'string' || row.date.length > 40 || typeof row.sourceStatus !== 'string' || row.sourceStatus.length > 100)) return null;
        return {frameId: frame === top ? 0 : frame.routingId, frameUrl: frame.url, result};
      } catch { return null; }
    }));
    const samples = await this.bounded(operation, task) as ({frameId:number;frameUrl:string;result:Adapter.ApplicationContext}|null)[];
    const result = this.adapter.mergeApplicationContexts(samples.filter((sample): sample is NonNullable<typeof sample> => !!sample), operation.binding.url, [...allowed]);
    if (!result || result.url !== operation.binding.url || typeof result.company !== 'string' || result.company.length > 255 ||
        !Array.isArray(result.titles) || result.titles.length > 50 ||
        result.titles.some(title => typeof title !== 'string' || !title.trim() || title.length > 512) ||
        !Array.isArray(result.records) || result.records.length !== result.titles.length ||
        result.records.some((row,index) => !row || row.title !== result.titles[index] ||
          typeof row.date !== 'string' || row.date.length > 40 || typeof row.sourceStatus !== 'string' || row.sourceStatus.length > 100))
      throw new Error('岗位读取结果无效，请重新识别。');
    this.message = '';
    return result;
  }

  private begin(wc: WebContents, type: string) {
    if (this.busy || wc.isDestroyed() || wc.isLoadingMainFrame() || !this.generations.has(wc.id)) throw new Error('请等待当前官网页面加载完成。');
    const binding = { wc, url: wc.getURL(), generation: this.generations.get(wc.id)!, profileVersion: this.profileVersion };
    const operation = { binding, timedOut: false, type, cancelRequested: false, frames: [] as FrameBinding[] };
    this.operation = operation; this.busy = true; this.publish();
    return operation;
  }

  private async bounded<T>(operation: ReturnType<FillerService['begin']>, work: Promise<T>): Promise<T> {
    let timer: NodeJS.Timeout | undefined;
    const settled = () => {
      if (this.operation !== operation) return;
      this.operation = undefined; this.busy = false;
      if (operation.timedOut) this.message = '超时操作已结束，页面可能已部分变化，请重新扫描后再操作。';
      this.publish();
    };
    void work.then(settled, settled);
    try {
      const result = await Promise.race([work, new Promise<never>((_resolve, reject) => {
        timer = setTimeout(() => { operation.timedOut = true; this.message = '操作超时且仍在执行；可以请求停止后续填写，已填内容会保留。若网站脚本无响应，请等待或手动处理，勿重复提交。'; this.publish(); reject(new Error(this.message)); }, this.timeoutMs);
      })]);
      if (!this.valid(operation.binding)) throw new Error('页面或资料已变化，请重新扫描。');
      if (Buffer.byteLength(JSON.stringify(result) || '') > 1048576) throw new Error('闪填结果过大。');
      return result;
    } finally { clearTimeout(timer); }
  }

  async stop(wc: WebContents) {
    const operation = this.operation;
    if (!operation || operation.binding.wc !== wc || !this.valid(operation.binding)) throw new Error('当前页面没有正在执行的填写。');
    if (!['filling', 'preparing'].includes(operation.type)) throw new Error('当前操作不能取消，请等待结束。');
    if (operation.cancelRequested) return;
    operation.cancelRequested = true;
    this.message = '正在停止后续填写，已填内容保留；等待网站当前操作结束。';
    this.publish();
    let timer: NodeJS.Timeout | undefined;
    try {
      const requests = operation.frames.map(frame => this.execute(wc, frame.frame,
        this.adapter.buildCancelScript(this.bundle, frame.route)));
      const results = await Promise.race([
        Promise.allSettled(requests),
        new Promise<undefined>(resolve => { timer = setTimeout(() => resolve(undefined), 2000); }),
      ]);
      // Do not release the operation lock until the actual work settles.
      if (this.operation === operation && (!results || results.some(result => result.status === 'rejected'))) {
        this.message = '停止请求尚未完全确认；页面可能已部分填写，请等待当前操作结束。';
        this.publish();
      }
    } finally { clearTimeout(timer); }
  }

  async scan(wc: WebContents, instanceId: string, allowedFrameOrigins: string[] = []) {
    this.idle();
    if (!this.profile) throw new Error('请先在“简历资料”中保存资料。');
    this.clearPlan();
    this.diagnostics = [];
    const operation = this.begin(wc, 'scanning');
    this.binding = { ...operation.binding, frames: new Map() };
    this.scanState = 'scanning';
    this.message = '正在扫描当前页面…';
    this.publish();
    const topOrigin = new URL(wc.getURL()).origin;
    const allowed = new Set([topOrigin, ...allowedFrameOrigins]);
    const frames = wc.mainFrame.framesInSubtree;
    const task = Promise.all(frames.map(async frame => {
      const route = this.route(wc, frame, instanceId);
      if (frame.detached) return { route, status: 'detached' as const };
      let origin = '';
      try { origin = new URL(frame.url).origin; } catch { return { route, status: 'unreachable' as const }; }
      if (frame !== wc.mainFrame && !allowed.has(origin)) return { route, status: 'blocked' as const };
      try {
        const scan = await this.execute(wc, frame, this.adapter.buildScanScript(this.bundle, this.profileForFrame(route), route)) as Adapter.ScanResult;
        if (!scan || scan.ok !== true) return { route, status: 'unreachable' as const };
        return { route, status: 'scanned' as const, scan };
      } catch { return { route, status: 'unreachable' as const }; }
    }));
    let outcomes: Adapter.FrameScanOutcome[];
    let aggregate: Adapter.AggregateScanResult;
    try {
      outcomes = await this.bounded(operation, task) as Adapter.FrameScanOutcome[];
      aggregate = this.adapter.aggregateFrameScans(outcomes);
    } catch (error) {
      if (this.valid(operation.binding)) {
        this.binding = { ...operation.binding, frames: new Map() };
        this.scanState = 'failed';
        this.scanSummary = emptyScanSummary();
        this.diagnostics = [{ code: 'filler_scan_failed' }];
        if (!operation.timedOut) this.message = '扫描失败：页面框架未能完成读取。请确认表单页已加载且框架在允许范围内，再重试。';
        this.publish();
      }
      throw error;
    }
    const scanned = outcomes.filter(outcome => outcome.status === 'scanned');
    this.blockedFrameOrigins = [...new Set(outcomes.filter(outcome => outcome.status === 'blocked')
      .map(outcome => { try { const url = new URL(outcome.route.href); return url.protocol === 'https:' ? url.origin : ''; } catch { return ''; } })
      .filter(Boolean))].slice(0, 8);
    const candidateCount = aggregate.candidates?.length || 0;
    const mappableCount = (aggregate.candidates || []).filter(item => item.candidate.customAnswerSupported).length;
    const unsupportedCount = candidateCount - mappableCount;
    this.scanState = scanned.length === 0 ? 'failed' : aggregate.partial ? 'partial' : 'complete';
    this.scanSummary = {
      framesScanned: scanned.length, framesFailed: aggregate.failures.length,
      controlsSeen: scanned.reduce((count, outcome) => count + Math.max(
        Number.isSafeInteger(outcome.scan.totalFields) ? outcome.scan.totalFields : 0,
        outcome.scan.matches.length + (outcome.scan.candidates?.length || 0) + (outcome.scan.attachments?.length || 0)), 0),
      matched: aggregate.fields.length, needsAnswer: mappableCount, unsupported: unsupportedCount,
      attachments: scanned.reduce((count, outcome) => count + (outcome.scan.attachments?.length || 0), 0),
      filtered: scanned.reduce((count, outcome) => count + (Number.isSafeInteger(outcome.scan.blockedCount) ? outcome.scan.blockedCount : 0), 0),
    };
    const frameBindings = new Map<string, FrameBinding>();
    for (const outcome of outcomes) if (outcome.status === 'scanned') {
      const frame = frames.find(candidate => `${candidate.processId}:${candidate.routingId}` === outcome.route.frameId);
      if (frame) frameBindings.set(outcome.route.frameId, { frame, route: outcome.route, scanId: outcome.scan.scanId, fieldIds: new Set(outcome.scan.matches.map(item => item.fieldId)) });
    }
    this.scanId = scanned.length ? `${operation.binding.generation}:${Date.now()}` : '';
    this.fields = [
      ...aggregate.fields.map(item => ({ fieldId: item.selectionId, frameId: item.route.frameId,
        label: String(item.match.label || item.match.key || '未命名字段').slice(0, 300), value: item.match.value, fillable: true, blocked: false })),
      ...(aggregate.candidates || []).map(item => ({ fieldId: item.selectionId, frameId: item.route.frameId,
        label: item.candidate.label || item.candidate.fieldId, value: '', fillable: false,
        blocked: !item.candidate.customAnswerSupported, reason: item.candidate.reason })),
    ];
    this.diagnostics = [
      ...aggregate.failures.map(item => ({ code: item.code, frameId: item.route.frameId })),
      ...outcomes.flatMap(outcome => outcome.status === 'scanned' ? (outcome.scan.diagnostics || [])
        .filter(item => typeof item.fieldId === 'string' && item.fieldId.length <= 200
          && typeof item.reason === 'string' && /^filler_[a-z0-9_]{1,80}$/.test(item.reason))
        .map(item => ({ code: item.reason, fieldId: item.fieldId, frameId: outcome.route.frameId })) : []),
    ].slice(0, 500);
    this.repeaters = outcomes.flatMap(outcome => outcome.status === 'scanned' ? outcome.scan.repeaters
      .filter(item => item.supported && item.desired > item.count)
      .map(item => ({ sectionId:`${outcome.route.frameId}:${item.sectionId}`, label:item.sectionId, desired:item.desired, count:item.count })) : []);
    this.attachments = outcomes.flatMap(outcome => outcome.status === 'scanned' ? outcome.scan.attachments.map(target => ({
      selectionId:JSON.stringify([outcome.route.instanceId,outcome.route.tabId,outcome.route.frameId,outcome.route.documentId,outcome.scan.scanId,target.fieldId]),
      frame:frameBindings.get(outcome.route.frameId)!,target,
    })).filter(item=>!!item.frame) : []);
    this.binding = { ...operation.binding, frames: frameBindings };
    if (scanned.length === 0) this.message = `扫描失败：未能读取任何页面框架${aggregate.failures.length ? `（${aggregate.failures.length} 个不可用或受限）` : ''}。请确认表单页已加载，且表单所在框架在允许范围内。`;
    else if (this.scanSummary.controlsSeen === 0) this.message = `扫描完成：已读取 ${scanned.length} 个页面框架，但未发现可处理的表单控件；页面可能仍在渲染，或使用当前不支持的自定义组件。`;
    else if (this.scanSummary.matched === 0 && candidateCount === 0 && this.scanSummary.filtered > 0)
      this.message = `扫描完成：发现 ${this.scanSummary.controlsSeen} 个控件，但 ${this.scanSummary.filtered} 个未通过可见性或安全校验，因此没有可填写候选。请确认控件仍可见且未禁用后重试。`;
    else if (this.scanSummary.matched === 0) {
      const reasons = [];
      if (mappableCount) reasons.push(`${mappableCount} 项没有唯一资料答案，可添加站点自定义答案`);
      if (unsupportedCount) reasons.push(`${unsupportedCount} 项控件暂不支持自动填写`);
      if (this.scanSummary.attachments) reasons.push(`另识别 ${this.scanSummary.attachments} 个简历附件控件`);
      this.message = `扫描完成：没有可自动填写字段${reasons.length ? `；${reasons.join('；')}` : ''}。`;
    } else {
      const reasons = [];
      if (mappableCount) reasons.push(`${mappableCount} 项没有唯一资料答案，可添加站点自定义答案`);
      if (unsupportedCount) reasons.push(`${unsupportedCount} 项控件暂不支持自动填写`);
      if (this.scanSummary.attachments) reasons.push(`另识别 ${this.scanSummary.attachments} 个简历附件控件`);
      this.message = `扫描完成：可填写 ${this.scanSummary.matched} 项${reasons.length ? `；${reasons.join('；')}` : ''}。`;
    }
    if (aggregate.partial) this.message += `另有 ${aggregate.failures.length} 个页面框架不可用或受限。`;
    this.publish();
  }

  async fill(wc: WebContents, scanId: string, selectionIds: string[]) {
    this.idle();
    const binding = this.binding;
    if (!binding || binding.wc !== wc || !this.valid(binding) || scanId !== this.scanId || !selectionIds.length) throw new Error('预览已失效，请重新扫描。');
    const selected = selectionIds.map(id => {
      const field = this.fields.find(item => item.fieldId === id);
      if (!field) throw new Error('字段选择无效，请重新扫描。');
      const frame = binding.frames.get(field.frameId);
      if (!frame || frame.frame.detached) throw new Error('字段所在页面框架已失效。');
      const rawId = JSON.parse(id)[5];
      if (typeof rawId !== 'string' || !frame.fieldIds.has(rawId)) throw new Error('字段身份校验失败。');
      return { selectionId: id, rawId, frame };
    });
    const groups = new Map<FrameBinding, typeof selected>();
    for (const item of selected) groups.set(item.frame, [...(groups.get(item.frame) || []), item]);
    this.fields = []; this.scanId = ''; this.scanState = 'consumed';
    const operation = this.begin(wc, 'filling');
    operation.frames = [...groups.keys()];
    const task = Promise.all([...groups].map(async ([frame, items]) => {
      let result: Adapter.FillResult;
      let executionFailed = false;
      try {
        result = await this.execute(wc, frame.frame, this.adapter.buildFillScript(this.bundle,
          { scanId: frame.scanId, fieldIds: items.map(item => item.rawId), confirmed: true }, frame.route)) as Adapter.FillResult;
      } catch {
        executionFailed = true;
        result = { ok: false, filled: 0, failed: items.map(item => item.rawId), skipped: [], code: 'filler_fill_partial',
          results: items.map(item => ({ fieldId: item.rawId, ok: false, status: 'failed', reason: 'filler_frame_unavailable' })) } as Adapter.FillResult;
      }
      return { items: items.map(({ selectionId, rawId }) => ({ selectionId, rawId })), result, executionFailed };
    }));
    try {
      const groupsResult = await this.bounded(operation, task);
      this.results = groupsResult.flatMap(group => (group.result.results || []).map(result => {
        const selectedItem = group.items.find(item => item.rawId === result.fieldId);
        const reason = typeof result.reason === 'string' && /^filler_[a-z0-9_]{1,80}$/.test(result.reason) ? result.reason : '';
        return { fieldId: selectedItem?.selectionId || String(result.fieldId), ok: result.ok === true,
          reason, status: result.status === 'filled' ? 'success' : result.status };
      }));
      this.binding = binding; this.undoReady = true;
      if (groupsResult.some(group => group.executionFailed)) throw new Error('filler_frame_unavailable');
      const success = this.results.filter(result => result.status === 'success').length;
      this.message = operation.cancelRequested ? `填写已停止，已完成 ${success} 项，其余请检查；未刷新页面、未提交申请。` :
        `已填写 ${success} 项，其他 ${this.results.length - success} 项请逐项检查；未提交申请。`;
    } catch (error) {
      this.undoReady = this.valid(binding); this.binding = this.undoReady ? binding : undefined;
      this.message = '填写结果未能完整确认，页面可能已部分修改。请检查官网或执行安全撤销。';
      throw error;
    } finally { this.publish(); }
  }

  async prepare(wc: WebContents) {
    this.idle();
    const binding = this.binding;
    const sectionIds=this.repeaters.map(item=>item.sectionId);
    if (!binding || binding.wc !== wc || !this.valid(binding) || !sectionIds.length) throw new Error('当前扫描没有需要展开的经历区块。');
    const candidates = [...binding.frames.values()].filter(frame => sectionIds.some(id => id.startsWith(frame.route.frameId + ':')));
    if (!candidates.length) throw new Error('没有可准备的经历区块。');
    const operation = this.begin(wc, 'preparing');
    operation.frames = candidates;
    const outcomes = await this.bounded(operation, Promise.allSettled(candidates.map(async frame => this.execute(wc, frame.frame,
      this.adapter.buildPrepareScript(this.bundle, { scanId: frame.scanId,
        sectionIds: sectionIds.filter(id => id.startsWith(frame.route.frameId + ':')).map(id => id.slice(frame.route.frameId.length + 1)), confirmed: true }, frame.route)))));
    const partial = outcomes.some(outcome => outcome.status === 'rejected' || !(outcome.value as Adapter.PrepareResult)?.ok);
    this.clearPlan(); this.scanState = 'consumed'; this.message = operation.cancelRequested ? '已停止新增经历区块，已新增内容保留，请重新扫描。' :
      partial ? '部分经历区块未能准备完成，请核对页面并重新扫描。' : '经历区块已准备，请重新扫描后确认字段。'; this.publish();
  }

  async undo(wc: WebContents) {
    this.idle();
    const binding = this.binding;
    if (!binding || binding.wc !== wc || !this.valid(binding) || !this.undoReady) throw new Error('当前页面没有可撤销的填写。');
    const operation = this.begin(wc, 'undoing');
    const values = await this.bounded(operation, Promise.all([...binding.frames.values()].map(frame =>
      this.execute(wc, frame.frame, this.adapter.buildUndoScript(this.bundle, frame.route)).catch(() => ({ restored: 0, failed: ['frame_unavailable'] }))))) as Adapter.UndoResult[];
    const restored = values.reduce((count, item) => count + Number(item?.restored || 0), 0);
    const failed = values.reduce((count, item) => count + (Array.isArray(item?.failed) ? item.failed.length : 0), 0);
    this.clearPlan(); this.scanState = 'consumed'; this.message = `已安全撤销 ${restored} 项，${failed} 项未能撤销；已上传附件不会被远端撤回。`; this.publish();
  }

  async uploadAttachment(wc: WebContents, scanId: string, selectionId: string, store: FillerStore) {
    this.idle();
    const binding=this.binding,target=this.attachments.find(item=>item.selectionId===selectionId);
    if(!binding||binding.wc!==wc||!this.valid(binding)||scanId!==this.scanId||!target||target.frame.frame.detached) throw new Error('附件预览已失效，请重新扫描。');
    const metadata=store.snapshot().attachment;
    if(!metadata)throw new Error('请先在“简历资料”中选择默认附件。');
    const mime={pdf:'application/pdf',doc:'application/msword',docx:'application/vnd.openxmlformats-officedocument.wordprocessingml.document'}[metadata.type];
    const operation=this.begin(wc,'uploading');
    const task=store.withAttachment(async file=>{
      const ticket=await this.execute(wc,target.frame.frame,this.adapter.buildUploadScript(this.bundle,{scanId:target.frame.scanId,fieldId:target.target.fieldId,
        attachment:{id:metadata.id,name:metadata.name,size:metadata.size,mime},confirmed:true},target.frame.route)) as Adapter.UploadTicket;
      await this.executor.assignFile(wc, target.frame.frame,
        this.adapter.buildUploadTargetScript(this.bundle, ticket.uploadId, target.frame.route), file,
        () => this.valid(binding) && !target.frame.frame.detached);
      const deadline=Date.now()+10000;
      while(Date.now()<deadline){
        const status=await this.execute(wc,target.frame.frame,this.adapter.buildUploadStatusScript(this.bundle,ticket.uploadId,target.frame.route)) as Adapter.UploadStatus;
        if(status.code!=='filler_upload_pending')return status;
        await new Promise(resolve=>setTimeout(resolve,250));
      }
      return {ok:false,code:'filler_upload_timeout_unknown',requiresRescan:true,remoteUndoSupported:false} as Adapter.UploadStatus;
    });
    const status=await this.bounded(operation,task);
    this.clearPlan();
    this.message=status.ok?'附件已选择并由网站确认接收，请重新扫描解析后的表单。':'附件已交给网站，但解析结果未确认；请检查页面后重新扫描，远端附件不能本地撤销。';
    this.publish();return status;
  }
}
