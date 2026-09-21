import * as fs from 'node:fs';
import * as path from 'node:path';
import { createHash, randomUUID } from 'node:crypto';

export interface FillerProtection {
  available(): boolean;
  protect(plaintext: Buffer): Buffer;
  unprotect(ciphertext: Buffer): Buffer;
}
export type FillerProfile = Record<string, unknown>;
export type FillerMode = 'personal' | 'demo';
export interface FillerAttachment {
  id: string; name: string; type: 'pdf' | 'doc' | 'docx'; size: number; sha256: string;
}
type Slot = { profile: FillerProfile; settings: FillerProfile; attachment: FillerAttachment | null };
type PersonalProfile = { id: string; name: string; slot: Slot };
type State = { schema: 3; instanceId: string; revision: number; mode: FillerMode; activeProfileId: string; personalProfiles: PersonalProfile[]; demo: Slot };
export interface FillerSnapshot {
  schema: 3; revision: number; mode: FillerMode; profile: FillerProfile;
  settings: FillerProfile; attachment: FillerAttachment | null;
  activeProfileId: string; activeProfileName: string; personalProfiles: { id: string; name: string }[];
}
export class FillerStoreError extends Error {
  constructor(public readonly code: string) { super(code); this.name = 'FillerStoreError'; }
}
const MAX_JSON = 1024 * 1024;
const MAX_ATTACHMENT = 20 * 1024 * 1024;
const MAX_PROTECTED_ATTACHMENT = MAX_ATTACHMENT * 2 + MAX_JSON;
const MAX_STATE = 8 * 1024 * 1024;
const MAX_PERSONAL_PROFILES = 50;
const ID = /^[0-9a-f-]{36}$/;
const PROFILE_ID = /^[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}$/;
const HASH = /^[0-9a-f]{64}$/;
const activeTemporaryFiles = new Set<string>();
const io = (value: string) => path.toNamespacedPath(value);
const clone = <T>(value: T): T => JSON.parse(JSON.stringify(value));
function fail(code: string): never { throw new FillerStoreError(code); }
const hash = (data: Buffer) => createHash('sha256').update(data).digest('hex');
const object = (v: unknown): v is Record<string, unknown> => !!v && typeof v === 'object' && !Array.isArray(v);
const demo = (): Slot => ({ profile: { basic: { fullName: 'Demo Candidate', email: 'demo@example.invalid' } }, settings: {}, attachment: null });
const empty = (): Slot => ({ profile: {}, settings: {}, attachment: null });
function profileName(value: unknown): string {
  if (typeof value !== 'string') fail('filler_profile_name_invalid');
  const name = value.normalize('NFC').trim();
  if (!name || name.length > 80 || /[\u0000-\u001f\u007f]/.test(name) || Buffer.byteLength(name) > 240) fail('filler_profile_name_invalid');
  return name;
}
const profileNameKey = (value: string) => value.normalize('NFKC').toLowerCase();

function checkedJson(value: unknown): FillerProfile {
  let nodes = 0;
  const visit = (item: unknown, depth: number): unknown => {
    if (++nodes > 20000 || depth > 24) fail('filler_profile_invalid');
    if (item === null || typeof item === 'string' || typeof item === 'boolean') return item;
    if (typeof item === 'number' && Number.isFinite(item)) return item;
    if (Array.isArray(item)) return Array.from(item, child => visit(child, depth + 1));
    if (!object(item) || Object.getPrototypeOf(item) !== Object.prototype) fail('filler_profile_invalid');
    const result: FillerProfile = {};
    for (const [key, descriptor] of Object.entries(Object.getOwnPropertyDescriptors(item))) {
      if (!descriptor.enumerable) continue;
      if (!('value' in descriptor) || ['__proto__', 'constructor', 'prototype'].includes(key)) fail('filler_profile_invalid');
      if (/^(apikey|accesskey|secret|password|passwd|cookie|cookies|token|accesstoken|refreshtoken|authorization|credential|credentials)$/i.test(key.replace(/[_-]/g, ''))) {
        fail('filler_credential_field_forbidden');
      }
      result[key] = visit(descriptor.value, depth + 1);
    }
    return result;
  };
  if (!object(value)) fail('filler_profile_invalid');
  const profile = visit(value, 0) as FillerProfile;
  if (Buffer.byteLength(JSON.stringify(profile)) > MAX_JSON) fail('filler_profile_size');
  return profile;
}

function assertNoLinks(target: string) {
  const resolved = path.resolve(target);
  let cursor = path.parse(resolved).root;
  for (const component of resolved.slice(cursor.length).split(path.sep).filter(Boolean)) {
    cursor = path.join(cursor, component);
    try {
      if (fs.lstatSync(io(cursor)).isSymbolicLink()) fail('filler_link_forbidden');
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code === 'ENOENT') return;
      throw error;
    }
  }
}

/** Main-process only. Supply an already verified instance root/ID and native
 * protection (e.g. Electron safeStorage/DPAPI). No default plaintext provider.
 * Single main-process owner; revision checks reject stale store objects.
 */
export class FillerStore {
  private readonly directory: string;
  private readonly attachments: string;
  private readonly temporary: string;
  private readonly stateFile: string;
  private readonly backupFile: string;
  private state: State;
  private fingerprint: string | null = null;
  constructor(private options: {
    instanceRoot: string; instanceId: string; protection: FillerProtection;
    secureDirectory?: (directory: string) => void;
  }, recover = false) {
    if (!path.isAbsolute(options.instanceRoot) || !/^[0-9a-f]{32}$/.test(options.instanceId)) fail('filler_instance_invalid');
    assertNoLinks(options.instanceRoot);
    if (!fs.statSync(io(options.instanceRoot)).isDirectory()) fail('filler_instance_invalid');
    if (!options.protection.available()) fail('filler_protection_unavailable');
    this.directory = path.join(options.instanceRoot, 'resume-filler');
    this.attachments = path.join(this.directory, 'attachments');
    this.temporary = path.join(this.directory, 'temporary');
    this.stateFile = path.join(this.directory, 'state.bin');
    this.backupFile = path.join(this.directory, 'state.backup.bin');
    for (const directory of [this.directory, this.attachments, this.temporary]) {
      assertNoLinks(directory);
      fs.mkdirSync(io(directory), { recursive: true, mode: 0o700 });
      options.secureDirectory?.(directory);
    }
    this.cleanupTemporaryFiles();
    const first = { id: randomUUID(), name: '个人资料', slot: empty() };
    this.state = { schema: 3, instanceId: options.instanceId, revision: 0, mode: 'personal', activeProfileId: first.id,
      personalProfiles: [first], demo: demo() };
    if (recover) {
      const next = this.decode(this.read(this.backupFile, MAX_STATE));
      next.revision++;
      this.commit(next, true);
    } else if (fs.existsSync(io(this.stateFile))) {
      const bytes = this.read(this.stateFile, MAX_STATE);
      this.state = this.decode(bytes);
      this.fingerprint = hash(bytes);
    } else if (fs.existsSync(io(this.backupFile))) {
      fail('filler_state_missing_recovery_required');
    }
    this.collectAttachments();
  }
  private read(file: string, limit: number): Buffer {
    assertNoLinks(file);
    const descriptor = fs.openSync(io(file), 'r');
    try {
      const info = fs.fstatSync(descriptor);
      if (!info.isFile() || info.size > limit) fail('filler_file_size');
      const bytes = fs.readFileSync(descriptor);
      if (bytes.length > limit) fail('filler_file_size');
      return bytes;
    } finally { fs.closeSync(descriptor); }
  }
  private protect(data: Buffer): Buffer {
    if (!this.options.protection.available()) fail('filler_protection_unavailable');
    try {
      const result = this.options.protection.protect(data);
      if (!Buffer.isBuffer(result) || !result.length || result.equals(data)) fail('filler_protection_failed');
      return result;
    } catch { return fail('filler_protection_failed'); }
  }
  private unprotect(data: Buffer): Buffer {
    if (!this.options.protection.available()) fail('filler_protection_unavailable');
    try {
      const result = this.options.protection.unprotect(data);
      if (!Buffer.isBuffer(result)) fail('filler_decryption_failed');
      return result;
    }
    catch { return fail('filler_decryption_failed'); }
  }
  private validateAttachment(value: unknown): FillerAttachment | null {
    if (value === null) return null;
    if (!object(value) || typeof value.id !== 'string' || !ID.test(value.id)
      || typeof value.name !== 'string' || value.name !== path.basename(value.name)
      || /[\\/:\x00-\x1f]/.test(value.name) || value.name.length > 200
      || !['pdf', 'doc', 'docx'].includes(String(value.type))
      || path.extname(value.name).toLowerCase() !== '.' + value.type
      || !Number.isSafeInteger(value.size) || Number(value.size) < 1 || Number(value.size) > MAX_ATTACHMENT
      || typeof value.sha256 !== 'string' || !HASH.test(value.sha256)) fail('filler_attachment_invalid');
    return { id: value.id, name: value.name, type: value.type as FillerAttachment['type'], size: Number(value.size), sha256: value.sha256 };
  }
  private decode(bytes: Buffer): State {
    const plain = this.unprotect(bytes);
    if (plain.length > MAX_STATE) fail('filler_state_invalid');
    let data: unknown;
    try { data = JSON.parse(plain.toString('utf8')); } catch { return fail('filler_state_invalid'); }
    if (!object(data) || data.instanceId !== this.options.instanceId) fail('filler_instance_mismatch');
    if (data.schema !== 1 && data.schema !== 2 && data.schema !== 3) fail('filler_schema_unsupported');
    if (!Number.isSafeInteger(data.revision) || Number(data.revision) < 0) fail('filler_state_invalid');
    const slot = (value: unknown): Slot => {
      if (!object(value)) return fail('filler_state_invalid');
      return { profile: checkedJson(value.profile), settings: checkedJson(value.settings), attachment: this.validateAttachment(value.attachment) };
    };
    if (data.schema === 1 || data.schema === 2) {
      if (data.schema === 2 && data.mode !== 'personal' && data.mode !== 'demo') fail('filler_state_invalid');
      const id = randomUUID();
      const legacyDemo = data.schema === 1 ? demo() : slot(data.demo);
      if (legacyDemo.attachment) fail('filler_demo_attachment_forbidden');
      return { schema: 3, instanceId: this.options.instanceId, revision: Number(data.revision), mode: data.schema === 1 ? 'personal' : data.mode as FillerMode,
        activeProfileId: id, personalProfiles: [{ id, name: '个人资料', slot: slot(data.schema === 1 ? data : data.personal) }],
        demo: legacyDemo };
    }
    if (data.mode !== 'personal' && data.mode !== 'demo' || !Array.isArray(data.personalProfiles) ||
        data.personalProfiles.length < 1 || data.personalProfiles.length > MAX_PERSONAL_PROFILES ||
        typeof data.activeProfileId !== 'string' || !PROFILE_ID.test(data.activeProfileId)) fail('filler_state_invalid');
    const ids = new Set<string>(), names = new Set<string>();
    const personalProfiles = data.personalProfiles.map((value): PersonalProfile => {
      if (!object(value) || typeof value.id !== 'string' || !PROFILE_ID.test(value.id) || ids.has(value.id)) fail('filler_state_invalid');
      const name = profileName(value.name);
      const key = profileNameKey(name);
      if (name !== value.name || names.has(key)) fail('filler_state_invalid');
      ids.add(value.id); names.add(key);
      return { id: value.id, name, slot: slot(value.slot) };
    });
    if (!ids.has(data.activeProfileId)) fail('filler_state_invalid');
    const state: State = { schema: 3, instanceId: this.options.instanceId, revision: Number(data.revision), mode: data.mode,
      activeProfileId: data.activeProfileId, personalProfiles, demo: slot(data.demo) };
    if (state.demo.attachment) fail('filler_demo_attachment_forbidden');
    return state;
  }
  private atomic(file: string, bytes: Buffer) {
    assertNoLinks(file);
    const temp = file + '.' + randomUUID() + '.partial';
    let descriptor: number | undefined;
    try {
      descriptor = fs.openSync(io(temp), 'wx', 0o600);
      fs.writeFileSync(descriptor, bytes);
      fs.fsyncSync(descriptor);
      fs.closeSync(descriptor); descriptor = undefined;
      fs.renameSync(io(temp), io(file));
    } finally {
      if (descriptor !== undefined) fs.closeSync(descriptor);
      if (fs.existsSync(io(temp))) fs.unlinkSync(io(temp));
    }
  }
  private commit(next: State, recovery = false) {
    const bytes = this.protect(Buffer.from(JSON.stringify(next)));
    if (bytes.length > MAX_STATE) fail('filler_state_size');
    const current = fs.existsSync(io(this.stateFile)) ? this.read(this.stateFile, MAX_STATE) : null;
    if (!recovery && (current ? hash(current) : null) !== this.fingerprint) fail('filler_stale_revision');
    if (!recovery) this.collectAttachments();
    if (current && !recovery) this.atomic(this.backupFile, current);
    this.atomic(this.stateFile, bytes);
    this.state = next; this.fingerprint = hash(bytes);
  }
  private change(update: (state: State) => void) {
    if (this.state.revision >= Number.MAX_SAFE_INTEGER) fail('filler_revision_exhausted');
    const next = clone(this.state);
    update(next); next.revision++;
    this.commit(next);
    return this.snapshot();
  }
  private personalProfile(state: State, id = state.activeProfileId) {
    const profile = state.personalProfiles.find(item => item.id === id);
    if (!profile) fail('filler_state_invalid');
    return profile;
  }
  private activeSlot(state: State = this.state): Slot {
    return state.mode === 'demo' ? state.demo : this.personalProfile(state).slot;
  }
  private checkRevision(expectedRevision: number) {
    if (!Number.isSafeInteger(expectedRevision) || expectedRevision !== this.state.revision) fail('filler_stale_revision');
  }
  snapshot(): FillerSnapshot {
    const slot = this.activeSlot();
    return clone({ schema: 3, revision: this.state.revision, mode: this.state.mode, ...slot,
      activeProfileId: this.state.activeProfileId, activeProfileName: this.personalProfile(this.state).name,
      personalProfiles: this.state.personalProfiles.map(({ id, name }) => ({ id, name })) });
  }
  saveProfile(profile: unknown, expectedRevision = this.state.revision) {
    this.checkRevision(expectedRevision);
    const validated = checkedJson(profile);
    return this.change(state => { this.activeSlot(state).profile = validated; });
  }
  saveSettings(settings: unknown) {
    const validated = checkedJson(settings);
    return this.change(state => { this.activeSlot(state).settings = validated; });
  }
  createProfile(name: unknown, expectedRevision: number) {
    this.checkRevision(expectedRevision);
    const normalized = profileName(name), key = profileNameKey(normalized);
    if (this.state.personalProfiles.length >= MAX_PERSONAL_PROFILES) fail('filler_profile_limit');
    if (this.state.personalProfiles.some(item => profileNameKey(item.name) === key)) fail('filler_profile_name_conflict');
    const id = randomUUID();
    return this.change(state => {
      state.personalProfiles.push({ id, name: normalized, slot: empty() });
      state.activeProfileId = id; state.mode = 'personal';
    });
  }
  selectProfile(profileId: string, expectedRevision: number) {
    this.checkRevision(expectedRevision);
    if (!this.state.personalProfiles.some(item => item.id === profileId)) fail('filler_profile_not_found');
    if (this.state.mode === 'personal' && this.state.activeProfileId === profileId) return this.snapshot();
    return this.change(state => { state.activeProfileId = profileId; state.mode = 'personal'; });
  }
  renameProfile(profileId: string, name: unknown, expectedRevision: number) {
    this.checkRevision(expectedRevision);
    const profile = this.personalProfile(this.state, profileId), normalized = profileName(name), key = profileNameKey(normalized);
    if (this.state.personalProfiles.some(item => item.id !== profileId && profileNameKey(item.name) === key)) fail('filler_profile_name_conflict');
    if (profile.name === normalized) return this.snapshot();
    return this.change(state => { this.personalProfile(state, profileId).name = normalized; });
  }
  deleteProfile(profileId: string, expectedRevision: number) {
    this.checkRevision(expectedRevision);
    const index = this.state.personalProfiles.findIndex(item => item.id === profileId);
    if (index < 0) fail('filler_profile_not_found');
    if (this.state.personalProfiles.length === 1) fail('filler_profile_last_required');
    return this.change(state => {
      state.personalProfiles.splice(index, 1);
      if (state.activeProfileId === profileId) state.activeProfileId = state.personalProfiles[0].id;
    });
  }
  importJson(text: string) {
    if (typeof text !== 'string' || Buffer.byteLength(text) > MAX_JSON) fail('filler_profile_size');
    let value: unknown;
    try { value = JSON.parse(text); } catch { return fail('filler_profile_invalid'); }
    return this.saveProfile(value);
  }
  exportJson(fields?: string[]): string {
    const profile = this.activeSlot().profile;
    if (fields && (!Array.isArray(fields) || fields.some(key => !Object.hasOwn(profile, key)))) fail('filler_export_selection_invalid');
    return JSON.stringify(fields ? Object.fromEntries(fields.map(key => [key, profile[key]])) : profile, null, 2);
  }
  setMode(mode: FillerMode) {
    if (mode !== 'personal' && mode !== 'demo') fail('filler_mode_invalid');
    return mode === this.state.mode ? this.snapshot() : this.change(state => { state.mode = mode; });
  }
  /** Explicit main-process file-picker result only; never forward renderer paths. */
  replaceAttachment(selectedPath: string) {
    if (this.state.mode !== 'personal') fail('filler_demo_attachment_forbidden');
    if (!path.isAbsolute(selectedPath)) fail('filler_attachment_invalid');
    const bytes = this.read(selectedPath, MAX_ATTACHMENT);
    const attachment = this.validateAttachment({ id: randomUUID(), name: path.basename(selectedPath),
      type: path.extname(selectedPath).slice(1).toLowerCase(), size: bytes.length, sha256: hash(bytes) })!;
    const file = path.join(this.attachments, attachment.id + '.bin');
    // Retain the new encrypted blob while the metadata commit performs cleanup.
    this.pendingAttachment = attachment.id;
    try {
      const encrypted = this.protect(bytes);
      if (encrypted.length > MAX_PROTECTED_ATTACHMENT) fail('filler_attachment_size');
      this.atomic(file, encrypted);
      const profileId = this.state.activeProfileId;
      return this.change(state => { this.personalProfile(state, profileId).slot.attachment = attachment; });
    } catch (error) {
      if (fs.existsSync(io(file))) fs.unlinkSync(io(file));
      throw error;
    } finally { this.pendingAttachment = undefined; }
  }
  clearAttachment() {
    return this.change(state => { this.activeSlot(state).attachment = null; });
  }
  /** Plaintext exists only for this awaited upload callback; cleanup runs on failure too. */
  async withAttachment<T>(use: (file: string, metadata: FillerAttachment) => Promise<T>): Promise<T> {
    if (this.state.mode !== 'personal') return fail('filler_attachment_missing');
    const attachment = this.personalProfile(this.state).slot.attachment;
    if (!attachment) return fail('filler_attachment_missing');
    const bytes = this.unprotect(this.read(path.join(this.attachments, attachment.id + '.bin'), MAX_PROTECTED_ATTACHMENT));
    if (bytes.length !== attachment.size || hash(bytes) !== attachment.sha256) fail('filler_attachment_corrupt');
    const directory = path.join(this.temporary, 'upload-' + randomUUID());
    assertNoLinks(directory); fs.mkdirSync(io(directory), { recursive:false, mode:0o700 }); this.options.secureDirectory?.(directory);
    const file = path.join(directory, attachment.name);
    activeTemporaryFiles.add(file);
    try {
      this.atomic(file, bytes);
      return await use(file, clone(attachment));
    } finally {
      activeTemporaryFiles.delete(file);
      assertNoLinks(file);
      if (fs.existsSync(io(file))) fs.unlinkSync(io(file));
      assertNoLinks(directory);
      if (fs.existsSync(io(directory))) fs.rmdirSync(io(directory));
    }
  }
  cleanupTemporaryFiles() {
    assertNoLinks(this.temporary);
    for (const name of fs.readdirSync(io(this.temporary))) {
      if (/^upload-[0-9a-f-]{36}$/.test(name)) {
        const directory=path.join(this.temporary,name);assertNoLinks(directory);
        if(!fs.lstatSync(io(directory)).isDirectory())fail('filler_temporary_invalid');
        const children=fs.readdirSync(io(directory));
        if(children.length>1)fail('filler_temporary_invalid');
        for(const child of children){const file=path.join(directory,child);assertNoLinks(file);if(activeTemporaryFiles.has(file))continue;if(!fs.lstatSync(io(file)).isFile())fail('filler_temporary_invalid');fs.unlinkSync(io(file));}
        if(!fs.readdirSync(io(directory)).length)fs.rmdirSync(io(directory));
        continue;
      }
      if (!/^upload-[0-9a-f-]{36}\.(pdf|doc|docx)(\.[0-9a-f-]{36}\.partial)?$/.test(name)) continue;
      const file = path.join(this.temporary, name);
      if (activeTemporaryFiles.has(file)) continue;
      assertNoLinks(file);
      if (!fs.lstatSync(io(file)).isFile()) fail('filler_temporary_invalid');
      fs.unlinkSync(io(file));
    }
  }
  private pendingAttachment?: string;
  private collectAttachments() {
    const keep = new Set<string>(this.state.personalProfiles.map(item => item.slot.attachment?.id).filter((id): id is string => !!id));
    if (this.pendingAttachment) keep.add(this.pendingAttachment);
    if (fs.existsSync(io(this.backupFile))) {
      for (const item of this.decode(this.read(this.backupFile, MAX_STATE)).personalProfiles) {
        if (item.slot.attachment) keep.add(item.slot.attachment.id);
      }
    }
    assertNoLinks(this.attachments);
    for (const name of fs.readdirSync(io(this.attachments))) {
      if (!/^[0-9a-f-]{36}\.bin$/.test(name) || keep.has(name.slice(0, -4))) continue;
      const file = path.join(this.attachments, name);
      assertNoLinks(file);
      if (!fs.lstatSync(io(file)).isFile()) fail('filler_attachment_invalid');
      fs.unlinkSync(io(file));
    }
  }
  /** Explicit recovery, never silently reset corrupt/encrypted user data. */
  static recoverBackup(options: ConstructorParameters<typeof FillerStore>[0]): FillerStore {
    return new FillerStore(options, true);
  }
}
