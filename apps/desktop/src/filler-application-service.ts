import { createHash } from 'node:crypto';

export type ApplicationPage = { instanceId: string; tabId: number; generation: number; url: string };
export type Registration = { company: string; title: string; record_url: string;
  application_id?: string; job_id?: string; city?: string; progress_url_confirmed?: boolean };
export type PendingRegistration = { id: string; instanceId: string; createdAt: string;
  attempts: number; error: string; registration: Registration; reconcile?: boolean };
export interface ApplicationQueueStore {
  load(): Promise<{ instanceId: string; items: PendingRegistration[] } | undefined>;
  save(value: { instanceId: string; items: PendingRegistration[] }): Promise<void>;
}
export type ApplicationConnection = { instanceId: string; origin: string; token: string };

function progressUrl(value: string, confirmed = false): string {
  if (typeof value !== 'string' || value.length > 2048 || /[\s\\]/.test(value)) throw new Error('invalid_progress_url');
  if (value === '') return '';
  const url = new URL(value);
  if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password) throw new Error('invalid_progress_url');
  const route = decodeURIComponent(url.pathname.replace(/\/+$/, '') + '/' + url.hash).toLowerCase();
  const detailRoute = route.replace(/\/position\/application\/?(?=$|#)/g, '/applications/');
  if (/(?:^|[/#!_-])(?:job|jobs|position|positions|jobdetail|job-detail|detail|apply)(?:[/.?!_-]|$)/.test(detailRoute)) throw new Error('job_detail_not_progress');
  if (!confirmed && !/(?:^|[/#!_-])(?:applications?|myapplications?|applicationrecords?|records?|progress|deliveries|投递记录)(?:[/.?!_-]|$)/.test(route)) throw new Error('progress_url_confirmation_required');
  return value;
}

function registration(value: Registration): Registration {
  if (!value || typeof value.company !== 'string' || typeof value.title !== 'string') throw new Error('invalid_registration');
  const company = value.company.trim(), title = value.title.trim();
  const jobId = value.job_id === undefined ? undefined :
    typeof value.job_id === 'string' ? value.job_id.trim() : '';
  if (!company || company.length > 255 || !title || title.length > 512 ||
      (value.application_id !== undefined && (typeof value.application_id !== 'string' || !value.application_id || value.application_id.length > 255)) ||
      (jobId !== undefined && (!jobId || jobId.length > 255)) ||
      (value.city !== undefined && (typeof value.city !== 'string' || value.city.length > 255))) throw new Error('invalid_registration');
  return { company, title, record_url: progressUrl(value.record_url, value.progress_url_confirmed === true),
    ...(value.application_id ? { application_id: value.application_id } : {}),
    ...(jobId ? { job_id: jobId } : {}),
    city: value.city || '', progress_url_confirmed: value.progress_url_confirmed === true };
}

export function companyNameFromPageTitle(value: unknown): string {
  if (typeof value !== 'string') return '';
  const title = value.replace(/\s+/g, ' ').trim();
  if (!title || title.length > 500) return '';
  const company = title.replace(/(?:\s*[-|｜—·]\s*|\s*)(?:校园招聘(?:官网|平台)?|社会招聘(?:官网|平台)?|实习招聘(?:官网|平台)?|招聘官网|招聘平台|招聘门户|campus\s+recruitment|campus\s+hiring|careers?)\s*$/i, '').trim();
  return company && company !== title && company.length <= 255 ? company : '';
}

export function recruitCompanyCacheKey(rawUrl: string): string {
  try {
    const url = new URL(rawUrl);
    if (!['http:', 'https:'].includes(url.protocol)) return '';
    const host = url.hostname.toLowerCase();
    if (host !== 'app.mokahr.com') return host;
    const match = url.pathname.match(/^\/(campus-recruitment|social-recruitment)\/([^/]+)\/([^/]+)/i);
    return match ? `${host}/${match[1].toLowerCase()}/${match[2].toLowerCase()}/${match[3].toLowerCase()}` : `${host}${url.pathname}`;
  } catch { return ''; }
}

// Main supplies a verified native connection and an instance-private atomic store.
// Neither the renderer nor a recruitment page may construct these dependencies.
export class FillerApplicationService {
  private items: PendingRegistration[] = [];
  private loaded = false;
  private busy = false;
  constructor(private instanceId: string, private connection: () => ApplicationConnection | undefined,
    private foreground: () => ApplicationPage | undefined, private store: ApplicationQueueStore,
    private changed: (event: { applications: true; counts: true; preservePosition: true }) => void,
    private request: typeof fetch = fetch, private timeoutMs = 10000,
    private companyAliases: Readonly<Record<string, string>> = {}) {
    if (!/^[a-f0-9]{32}$/.test(instanceId)) throw new Error('invalid_instance');
  }
  private page(context: ApplicationPage) {
    const current = this.foreground();
    if (!current || context.instanceId !== this.instanceId || current.instanceId !== this.instanceId ||
      current.tabId !== context.tabId || current.generation !== context.generation || current.url !== context.url) throw new Error('foreground_changed');
  }
  private async exclusive<T>(run: () => Promise<T>): Promise<T> {
    if (this.busy) throw new Error('application_operation_busy');
    this.busy = true;
    try { await this.load(); return await run(); } finally { this.busy = false; }
  }
  private async load() {
    if (this.loaded) return;
    const saved = await this.store.load();
    if (saved) {
      if (saved.instanceId !== this.instanceId || !Array.isArray(saved.items) || saved.items.length > 100) throw new Error('queue_instance_mismatch');
      const ids = new Set<string>();
      for (const item of saved.items) {
        if (item.instanceId !== this.instanceId || typeof item.id !== 'string' || !item.id || ids.has(item.id) ||
            !Number.isInteger(item.attempts) || item.attempts < 0 || item.attempts > 3 ||
            typeof item.createdAt !== 'string' || !Number.isFinite(Date.parse(item.createdAt)) ||
            typeof item.error !== 'string' || (item.reconcile !== undefined && typeof item.reconcile !== 'boolean')) throw new Error('invalid_queue');
        ids.add(item.id);
        item.registration = registration(item.registration);
      }
      this.items = structuredClone(saved.items);
    }
    this.loaded = true;
  }
  private async save(items: PendingRegistration[]) {
    await this.store.save({ instanceId: this.instanceId, items: structuredClone(items) });
    this.items = items;
  }
  private refresh() {
    // A renderer notification failure must not turn a committed write into a retry.
    try { this.changed({ applications: true, counts: true, preservePosition: true }); } catch { /* advisory */ }
  }
  private async api(path: 'applications' | 'application' | 'sync' | 'sync-local-observation' | 'sync-local-observations', body?: object): Promise<any> {
    const connection = this.connection();
    if (!connection) throw new Error('offline');
    if (connection.instanceId !== this.instanceId) throw new Error('instance_changed');
    const origin = new URL(connection.origin);
    if (origin.protocol !== 'http:' || !['127.0.0.1', '[::1]'].includes(origin.hostname) ||
        ['8012', '5433'].includes(origin.port) || origin.username || origin.password || origin.pathname !== '/' ||
        origin.search || origin.hash || !connection.token) throw new Error('unverified_connection');
    const controller = new AbortController();
    let timer: NodeJS.Timeout | undefined;
    try {
      return await Promise.race([ (async () => {
        const response = await this.request(`${origin.origin}/api/integrations/resume-filler/${path}`, {
          method: body ? 'POST' : 'GET', redirect: 'error', signal: controller.signal,
          headers: { Authorization: `Bearer ${connection.token}`, 'Content-Type': 'application/json',
            'X-RecruitOps-Instance-Id': this.instanceId }, body: body ? JSON.stringify(body) : undefined,
        });
        if (!response.ok) throw new Error(`http_${response.status}`);
        if (this.connection()?.instanceId !== this.instanceId) throw new Error('instance_changed');
        return await response.json();
      })(), new Promise<never>((_, reject) => {
        timer = setTimeout(() => { controller.abort(); reject(new Error('request_timeout')); }, this.timeoutMs);
      }) ]);
    } finally { clearTimeout(timer); }
  }
  async pending() { return this.exclusive(async () => structuredClone(this.items)); }
  async candidates(context: ApplicationPage, cards: { company: string; title: string; job_id?: string }[]) {
    this.page(context);
    if (!Array.isArray(cards) || cards.length > 50) throw new Error('invalid_candidates');
    const normalized = cards.map(card => {
      if (typeof card.company !== 'string' || typeof card.title !== 'string') throw new Error('invalid_candidates');
      const name = card.company.trim();
      const company = Object.hasOwn(this.companyAliases, name) ? this.companyAliases[name] : name;
      const title = card.title.trim();
      const jobId = card.job_id === undefined ? undefined :
        typeof card.job_id === 'string' ? card.job_id.trim() : '';
      if (!company || !title || company.length > 255 || title.length > 512 ||
          (jobId !== undefined && (!jobId || jobId.length > 255))) throw new Error('invalid_candidates');
      return { company, title, ...(jobId ? { job_id: jobId } : {}) };
    });
    const result = await this.api('applications');
    this.page(context);
    if (!Array.isArray(result.items)) throw new Error('invalid_response');
    return normalized.map(card => ({ ...card, existing: result.items.filter((row: any) =>
      row.company === card.company && row.title === card.title &&
      (card.job_id === undefined || row.job_id === card.job_id)) }));
  }
  async register(context: ApplicationPage, input: Registration, confirmed: boolean) {
    return this.exclusive(async () => {
      this.page(context);
      if (confirmed !== true) throw new Error('confirmation_required');
      return this.registerValue(registration(input));
    });
  }
  async registerBatch(context: ApplicationPage, inputs: Registration[], confirmed: boolean) {
    return this.exclusive(async () => {
      this.page(context);
      if (confirmed !== true) throw new Error('confirmation_required');
      if (!Array.isArray(inputs) || !inputs.length || inputs.length > 50) throw new Error('invalid_registration_batch');
      // Validate the whole selection before the first write; keep each result independent.
      const values = inputs.map(registration);
      if (new Set(values.map(value => JSON.stringify([value.company,value.title]))).size !== values.length)
        throw new Error('duplicate_registration_batch');
      const results: {index:number;status:'saved'|'queued'|'failed';error?:string;applicationId?:string}[] = [];
      for (const [index,value] of values.entries()) {
        try {
          this.page(context);
          const result = await this.registerValue(value);
          results.push({index,status:result.queued?'queued':'saved',...('error' in result?{error:result.error}:{}),
            ...(!result.queued&&typeof result.result?.application_id==='string'?{applicationId:result.result.application_id}:{})});
        } catch (error) {
          const code = error instanceof Error && /^(foreground_changed|queue_full|pending_conflict_correct_first)$/.test(error.message)
            ? error.message : 'registration_failed';
          results.push({index,status:'failed',error:code});
        }
      }
      return results;
    });
  }
  private async registerValue(value: Registration) {
      const id = createHash('sha256').update(JSON.stringify(value.job_id
        ? [this.instanceId, 'job', value.company, value.job_id, value.application_id || '']
        : [this.instanceId, value.company, value.title, value.application_id || ''])).digest('hex');
      const prior = this.items.find(item => item.id === id);
      if (prior && JSON.stringify(prior.registration) !== JSON.stringify(value)) throw new Error('pending_conflict_correct_first');
      if (!prior) {
        if (this.items.length >= 100) throw new Error('queue_full');
        await this.save([...this.items, { id, instanceId: this.instanceId, registration: value,
          createdAt: new Date().toISOString(), attempts: 0, error: '' }]);
      }
      return this.send(id);
  }
  private async send(id: string) {
    let item = this.items.find(row => row.id === id);
    if (!item) throw new Error('pending_not_found');
    if (item.attempts >= 3) return { queued: true, id, error: 'retry_limit' };
    item = { ...item, attempts: item.attempts + 1 };
    await this.save(this.items.map(row => row.id === id ? item! : row));
    try {
      // A response may have been lost after commit. Bind the exact existing identity
      // before replaying a corrected URL so registration cannot silently ignore it.
      if (item.reconcile && !item.registration.application_id) {
        const listed = await this.api('applications');
        if (!Array.isArray(listed.items)) throw new Error('invalid_response');
        const matches = listed.items.filter((row: any) => row.company === item!.registration.company &&
          row.title === item!.registration.title &&
          (item!.registration.job_id === undefined || row.job_id === item!.registration.job_id));
        if (matches.length > 1) throw new Error('pending_identity_ambiguous');
        if (matches.length === 1) {
          if (typeof matches[0].id !== 'string' || !matches[0].id) throw new Error('invalid_response');
          item = { ...item, registration: registration({ ...item.registration, application_id: matches[0].id }) };
          await this.save(this.items.map(row => row.id === id ? item! : row));
        }
      }
      const result = await this.api('application', item.registration);
      if (result.ok !== true || typeof result.application_id !== 'string') throw new Error('invalid_response');
      await this.save(this.items.filter(row => row.id !== id));
      this.refresh();
      return { queued: false, id, result };
    } catch (error) {
      const message = error instanceof Error ? error.message : '';
      const code = /^(offline|instance_changed|unverified_connection|request_timeout|invalid_response|pending_identity_ambiguous|http_\d{3})$/.test(message) ? message : 'transport_or_storage_failed';
      await this.save(this.items.map(row => row.id === id ? { ...row, error: code } : row));
      return { queued: true, id, error: code };
    }
  }
  async retry(ids: string[]) {
    return this.exclusive(async () => {
      if (!Array.isArray(ids) || ids.length > 100 || new Set(ids).size !== ids.length) throw new Error('invalid_retry');
      const results = [];
      for (const id of ids) {
        const result = await this.send(id); results.push(result);
        if (['http_401', 'http_403', 'instance_changed', 'unverified_connection'].includes(result.error || '')) break;
      }
      return results;
    });
  }
  async cancel(id: string) { return this.exclusive(() => this.save(this.items.filter(item => item.id !== id))); }
  async correctLink(id: string, recordUrl: string, city: string | undefined, confirmed: boolean) {
    const old = (await this.pending()).find(item => item.id === id);
    if (!old) throw new Error('pending_not_found');
    return this.correct(id, { ...old.registration, record_url: recordUrl,
      city: city ?? old.registration.city, progress_url_confirmed: true }, confirmed);
  }
  async correct(id: string, input: Registration, confirmed: boolean) {
    return this.exclusive(async () => {
      if (confirmed !== true) throw new Error('confirmation_required');
      const old = this.items.find(item => item.id === id);
      if (!old) throw new Error('pending_not_found');
      const value = registration(input);
      // A timeout may already have committed. Identity edits would create a second record.
      if (value.company !== old.registration.company || value.title !== old.registration.title ||
        value.application_id !== old.registration.application_id) throw new Error('pending_identity_immutable');
      await this.save(this.items.map(item => item.id === id ? { ...item, registration: value, attempts: 0, error: '', reconcile: true } : item));
    });
  }
  async sync(context: ApplicationPage, applicationId: string, observationOperationId: string) {
    return this.exclusive(async () => {
      this.page(context);
      if (!applicationId || !observationOperationId) throw new Error('observation_required');
      const result = await this.api('sync', { application_id: applicationId,
        observation_operation_id: observationOperationId, page_url: context.url });
      if (result.success === true) this.refresh();
      return result;
    });
  }
  async syncObservation(context: ApplicationPage, applicationId: string, observation: Record<string, unknown>) {
    return this.exclusive(async () => {
      this.page(context);
      if (!applicationId || !observation || typeof observation !== 'object' || Array.isArray(observation)) throw new Error('observation_required');
      const result = await this.api('sync-local-observation', { application_id: applicationId,
        page_url: context.url, observation });
      this.page(context);
      if (result.success === true) this.refresh();
      return result;
    });
  }
  async syncObservations(context: ApplicationPage, applicationIds: string[], observation: Record<string, unknown>) {
    return this.exclusive(async () => {
      this.page(context);
      if (!Array.isArray(applicationIds) || !applicationIds.length || applicationIds.length > 50 ||
          new Set(applicationIds).size !== applicationIds.length ||
          applicationIds.some(id => typeof id !== 'string' || !id || id.length > 255) ||
          !observation || typeof observation !== 'object' || Array.isArray(observation)) throw new Error('observation_required');
      const result = await this.api('sync-local-observations', { application_ids: applicationIds,
        page_url: context.url, observation });
      this.page(context);
      if (!Array.isArray(result.results) || result.results.length !== applicationIds.length ||
          result.results.some((item: any, index: number) => item?.application_id !== applicationIds[index] ||
            typeof item?.success !== 'boolean')) throw new Error('invalid_response');
      if (result.results.some((item: any) => item.success)) this.refresh();
      return result.results as {application_id:string;success:boolean;reason?:string}[];
    });
  }
}
