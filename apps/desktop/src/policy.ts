import { isIP } from 'node:net';

export const securePreferences = {
  contextIsolation: true, sandbox: true, nodeIntegration: false,
  webSecurity: true, allowRunningInsecureContent: false, webviewTag: false
} as const;

export function isPrivateHost(host: string): boolean {
  const h = host.toLowerCase().replace(/^\[|\]$/g, '');
  if (isIP(h) === 6) {
    // Public CDNs commonly return both IPv4 and IPv6. Only admit global
    // unicast; keep local, mapped, translation and transition ranges blocked.
    const words = new URL(`https://[${h}]/`).hostname.slice(1, -1).split(':');
    const first = parseInt(words[0] || '0', 16);
    const second = parseInt(words[1] || '0', 16);
    return first < 0x2000 || first > 0x3fff || first === 0x2002 ||
      first === 0x2001 && (second < 0x200 || second === 0xdb8) || first === 0x3fff;
  }
  if (h === 'localhost' || !h.includes('.') || /\.(localhost|local|internal)$/.test(h)) return true;
  if (h.includes(':')) return true;
  if (!isIP(h)) return false;
  const [a, b] = h.split('.').map(Number);
  return a === 0 || a === 10 || a === 127 || a === 169 && b === 254 ||
    a === 172 && b >= 16 && b <= 31 || a === 192 && b === 168 ||
    a === 100 && b >= 64 && b <= 127 || a >= 224;
}

export function websiteUrl(value: unknown, fixtureOrigin?: string): string {
  if (typeof value !== 'string' || value.length > 4096) throw new Error('Invalid URL');
  const u = new URL(value);
  if (u.username || u.password || ['8012', '5433'].includes(u.port)) throw new Error('Forbidden target');
  if (fixtureOrigin && u.origin === fixtureOrigin) return u.href;
  if (u.protocol !== 'https:' || isPrivateHost(u.hostname)) throw new Error('Only public HTTPS recruitment sites are allowed');
  return u.href;
}

export function isolatedApi(value?: string): string | undefined {
  if (!value) return undefined;
  const u = new URL(value);
  if (u.protocol !== 'http:' || u.hostname !== '127.0.0.1' || !u.port ||
      ['8012', '5433'].includes(u.port) || u.username || u.password ||
      u.pathname !== '/' || u.search || u.hash) throw new Error('API must be an explicit isolated 127.0.0.1 origin (not 8012/5433)');
  return u.origin;
}

type JsonObject = { [key: string]: unknown };

export type Command = { action: 'home' | 'back' | 'forward' | 'reload' | 'hide' | 'quit' | 'clear-site' | 'workbench' | 'capture' | 'use-capture' | 'enable-writes' | 'disable-writes' |
  'filler-open' | 'filler-close' | 'filler-plugin' | 'filler-profile' | 'filler-profile-import' | 'filler-profile-export' |
  'filler-attachment-select' | 'filler-attachment-clear' | 'filler-demo-enable' | 'filler-demo-restore' |
  'filler-stop' |
  'filler-scan' | 'filler-prepare' | 'filler-undo' |
  'filler-application-detect' | 'filler-application-flush' } |
  { action: 'filler-fill'; scanId: string; fieldIds: string[] } |
  { action: 'filler-profile-save'; profile: JsonObject; expectedVersion?: number } |
  { action: 'filler-profile-create'; name: string; expectedVersion: number } |
  { action: 'filler-profile-select'; profileId: string; expectedVersion: number } |
  { action: 'filler-profile-rename'; profileId: string; name: string; expectedVersion: number } |
  { action: 'filler-profile-delete'; profileId: string; expectedVersion: number } |
  { action: 'filler-custom-save'; question: string; answer: string; scope: 'site' | 'global'; fieldId?: string; answerId?: string } |
  { action: 'filler-custom-delete'; answerId: string } |
  { action: 'filler-application-save'; company: string; title: string; recordUrl: string; city?: string } |
  { action: 'filler-application-save-batch'; records: {company: string; title: string; recordUrl: string; city?: string}[] } |
  { action: 'filler-application-retry'; queueId: string } |
  { action: 'filler-application-correct'; queueId: string; recordUrl: string; city?: string } |
  { action: 'filler-application-cancel'; queueId: string } |
  { action: 'filler-attachment-upload'; scanId: string; fieldId: string } |
  { action: 'open'; url: string } | { action: 'select' | 'close'; id: number };

function exactKeys(record: Record<string, unknown>, allowed: string[]) {
  return Object.keys(record).every(key => allowed.includes(key));
}
function boundedText(value: unknown, maximum: number, required = true): value is string {
  return typeof value === 'string' && value.length <= maximum && (!required || value.trim().length > 0);
}
function profileName(value: unknown): value is string {
  return boundedText(value, 80) && !/[\u0000-\u001f\u007f]/.test(value) && Buffer.byteLength(value) <= 240;
}
const PROFILE_ID = /^[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}$/;
function plainJsonObject(value: unknown): value is JsonObject {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return false;
  const prototype = Object.getPrototypeOf(value);
  if (prototype !== Object.prototype && prototype !== null) return false;
  try { return Buffer.byteLength(JSON.stringify(value), 'utf8') <= 1048576; }
  catch { return false; }
}

export function parseCommand(raw: unknown): Command {
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) throw new Error('Invalid command');
  const r = raw as Record<string, unknown>;
  const action = r.action;
  if (action === 'filler-fill' && Object.keys(r).length === 3 && typeof r.scanId === 'string' && r.scanId.length > 0 && r.scanId.length <= 128 &&
      Array.isArray(r.fieldIds) && r.fieldIds.length > 0 && r.fieldIds.length <= 300 && r.fieldIds.every(id => typeof id === 'string' && id.length > 0 && id.length <= 256) && new Set(r.fieldIds).size === r.fieldIds.length)
    return { action, scanId:r.scanId, fieldIds:r.fieldIds as string[] };
  if (action === 'filler-profile-save' && exactKeys(r, ['action','profile','expectedVersion']) && plainJsonObject(r.profile) &&
      (r.expectedVersion === undefined || Number.isSafeInteger(r.expectedVersion) && Number(r.expectedVersion) >= 0))
    return { action, profile:r.profile, ...(r.expectedVersion === undefined ? {} : {expectedVersion:Number(r.expectedVersion)}) };
  if (action === 'filler-profile-create' && exactKeys(r,['action','name','expectedVersion']) && profileName(r.name) &&
      Number.isSafeInteger(r.expectedVersion) && Number(r.expectedVersion) >= 0)
    return { action, name:r.name.trim(), expectedVersion:Number(r.expectedVersion) };
  if (action === 'filler-profile-select' && exactKeys(r,['action','profileId','expectedVersion']) && typeof r.profileId === 'string' && PROFILE_ID.test(r.profileId) &&
      Number.isSafeInteger(r.expectedVersion) && Number(r.expectedVersion) >= 0)
    return { action, profileId:r.profileId, expectedVersion:Number(r.expectedVersion) };
  if (action === 'filler-profile-rename' && exactKeys(r,['action','profileId','name','expectedVersion']) && typeof r.profileId === 'string' && PROFILE_ID.test(r.profileId) &&
      profileName(r.name) && Number.isSafeInteger(r.expectedVersion) && Number(r.expectedVersion) >= 0)
    return { action, profileId:r.profileId, name:r.name.trim(), expectedVersion:Number(r.expectedVersion) };
  if (action === 'filler-profile-delete' && exactKeys(r,['action','profileId','expectedVersion']) && typeof r.profileId === 'string' && PROFILE_ID.test(r.profileId) &&
      Number.isSafeInteger(r.expectedVersion) && Number(r.expectedVersion) >= 0)
    return { action, profileId:r.profileId, expectedVersion:Number(r.expectedVersion) };
  if (action === 'filler-custom-save' && exactKeys(r, ['action','question','answer','scope','fieldId','answerId']) &&
      boundedText(r.question,500) && boundedText(r.answer,4000) && (r.scope === 'site' || r.scope === 'global') &&
      (r.fieldId === undefined || boundedText(r.fieldId,256)) && (r.answerId === undefined || boundedText(r.answerId,64)))
    return {action,question:r.question.trim(),answer:r.answer,scope:r.scope,...(r.fieldId===undefined?{}:{fieldId:r.fieldId}),...(r.answerId===undefined?{}:{answerId:r.answerId})};
  if (action === 'filler-custom-delete' && exactKeys(r,['action','answerId']) && boundedText(r.answerId,64)) return {action,answerId:r.answerId};
  if (action === 'filler-application-save' && exactKeys(r, ['action','company','title','recordUrl','city']) &&
      boundedText(r.company,255) && boundedText(r.title,512) &&
      boundedText(r.recordUrl,2048) && (r.city === undefined || boundedText(r.city,255,false)))
    return {action,...r} as Command;
  if (action === 'filler-application-save-batch' && exactKeys(r,['action','records']) &&
      Array.isArray(r.records) && r.records.length > 0 && r.records.length <= 50) {
    const records = r.records.map(value => {
      if (!value || typeof value !== 'object' || Array.isArray(value) ||
          !exactKeys(value,['company','title','recordUrl','city'])) throw new Error('Invalid batch registration');
      const parsed = parseCommand({action:'filler-application-save',...value});
      if (parsed.action !== 'filler-application-save') throw new Error('Invalid batch registration');
      const {action: _, ...record} = parsed;
      return record;
    });
    return {action,records};
  }
  if (action === 'filler-application-cancel' && exactKeys(r,['action','queueId']) && boundedText(r.queueId,255)) return {action,queueId:r.queueId};
  if (action === 'filler-application-retry' && exactKeys(r,['action','queueId']) && boundedText(r.queueId,255)) return {action,queueId:r.queueId};
  if (action === 'filler-application-correct' && exactKeys(r,['action','queueId','recordUrl','city']) && boundedText(r.queueId,255) &&
      boundedText(r.recordUrl,2048) && (r.city === undefined || boundedText(r.city,255,false)))
    return {action,queueId:r.queueId,recordUrl:r.recordUrl,...(r.city===undefined?{}:{city:r.city})};
  if (action === 'filler-attachment-upload' && exactKeys(r,['action','scanId','fieldId']) && boundedText(r.scanId,128) && boundedText(r.fieldId,512)) return {action,scanId:r.scanId,fieldId:r.fieldId};
  if (['filler-open','filler-close','filler-plugin','filler-profile','filler-profile-import','filler-profile-export',
      'filler-attachment-select','filler-attachment-clear','filler-demo-enable','filler-demo-restore','filler-scan','filler-prepare',
      'filler-undo','filler-application-detect',
      'filler-application-flush','filler-stop'].includes(String(action)) && Object.keys(r).length === 1) return {action} as Command;
  if (action === 'open' && typeof r.url === 'string' && Object.keys(r).length === 2) return { action, url: r.url };
  if ((action === 'select' || action === 'close') && Number.isSafeInteger(r.id) && Number(r.id) > 0 && Object.keys(r).length === 2) return { action, id: Number(r.id) };
  if (['home', 'back', 'forward', 'reload', 'hide', 'quit', 'clear-site', 'workbench', 'capture', 'use-capture', 'enable-writes', 'disable-writes'].includes(String(action)) && Object.keys(r).length === 1) return { action } as Command;
  throw new Error('Unknown command or arguments');
}

export function trustedSender(senderId: number, expectedId: number, frameUrl: string, shellUrl: string, isMainFrame: boolean): boolean {
  return senderId === expectedId && isMainFrame && frameUrl === shellUrl;
}
