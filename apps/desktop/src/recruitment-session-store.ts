import fs from 'node:fs';
import path from 'node:path';
import { createHash } from 'node:crypto';
import type { Cookie, CookiesSetDetails } from 'electron';

type CookieChange = (event: unknown, cookie: Cookie, cause: string, removed: boolean) => void;
export interface RecruitmentSession {
  cookies: {
    get(filter: Record<string, unknown>): Promise<Cookie[]>;
    set(details: CookiesSetDetails): Promise<void>;
    flushStore(): Promise<void>;
    on(event: 'changed', listener: CookieChange): unknown;
    removeListener(event: 'changed', listener: CookieChange): unknown;
  };
  flushStorageData(): void;
}
export interface SessionProtection {
  available(): boolean;
  protect(text: string): Buffer;
  unprotect(ciphertext: Buffer): string;
}
type SavedCookie = { cookie: Cookie; recoverUntil: number };
const RECOVERY_LIFETIME_MS = 7 * 24 * 60 * 60 * 1000;
const MAX_FILE_BYTES = 4 * 1024 * 1024;
const MAX_COOKIES = 4096;
const keyOf = (cookie: Cookie) => JSON.stringify([cookie.domain, cookie.path || '/', cookie.name]);
const fingerprint = (cookie: Cookie) => createHash('sha256').update(JSON.stringify([
  keyOf(cookie), cookie.value, !!cookie.hostOnly, !!cookie.secure, !!cookie.httpOnly, cookie.sameSite
])).digest('hex');

// Chromium remains the authority for persistent cookies, including their expiry.
// Only otherwise ephemeral cookies get one encrypted, profile-bound recovery file.
export class RecruitmentSessionStore {
  private readonly filename: string;
  private readonly profile: string;
  private readonly saved = new Map<string, SavedCookie>();
  private readonly overwritten = new Map<string, {fingerprint: string; recoverUntil: number}>();
  private debounce?: NodeJS.Timeout;
  private periodic?: NodeJS.Timeout;
  private flushing?: Promise<void>;
  private flushAgain = false;
  private code = 'starting';
  private restored = 0;
  private readonly changed: CookieChange = (_event, cookie, cause, removed) => {
    if (this.flushing) this.flushAgain = true;
    const key = keyOf(cookie);
    if (removed || !this.recoverable(cookie)) {
      const previous = this.saved.get(key);
      if (removed && cause === 'overwrite' && previous) {
        this.overwritten.set(key, {fingerprint: fingerprint(previous.cookie), recoverUntil: previous.recoverUntil});
        if (this.overwritten.size > MAX_COOKIES) this.overwritten.delete(this.overwritten.keys().next().value!);
      } else this.overwritten.delete(key);
      // Logout/eviction must invalidate the previous recovery before a restart.
      if (this.saved.delete(key)) this.saveNow();
    } else {
      const previous = this.saved.get(key);
      const prior = previous ? {fingerprint: fingerprint(previous.cookie), recoverUntil: previous.recoverUntil}
        : this.overwritten.get(key);
      this.overwritten.delete(key);
      this.saved.set(key, { cookie: { ...cookie }, recoverUntil:
        prior && prior.fingerprint === fingerprint(cookie)
          ? prior.recoverUntil : this.now() + RECOVERY_LIFETIME_MS });
      if (this.debounce) clearTimeout(this.debounce);
      this.debounce = setTimeout(() => { this.debounce = undefined; void this.flush(); }, 1000);
      this.debounce.unref();
    }
  };

  constructor(private readonly directory: string, private readonly session: RecruitmentSession,
    private readonly protection: SessionProtection, private readonly now = Date.now) {
    this.filename = path.join(directory, 'recruitment-session', 'state.bin');
    const resolved = path.resolve(directory);
    this.profile = createHash('sha256').update(process.platform === 'win32' ? resolved.toLowerCase() : resolved).digest('hex');
  }

  status() { return { code: this.code, restored: this.restored }; }

  async start() {
    this.session.cookies.on('changed', this.changed);
    const recovery = this.load();
    const current = await this.session.cookies.get({});
    const existing = new Map(current.map(cookie => [keyOf(cookie), cookie]));
    for (const cookie of current) {
      if (this.recoverable(cookie)) {
        const previous = recovery.get(keyOf(cookie));
        this.saved.set(keyOf(cookie), { cookie, recoverUntil:
          previous && fingerprint(previous.cookie) === fingerprint(cookie)
            ? previous.recoverUntil : this.now() + RECOVERY_LIFETIME_MS });
      }
    }
    for (const [key, entry] of recovery) {
      if (existing.has(key)) continue; // Never overwrite Chromium's newer cookie.
      this.saved.set(key, entry);
      try {
        await this.session.cookies.set(this.details(entry.cookie));
        // Restoration must not extend the recovery lifetime.
        this.saved.set(key, entry);
        this.restored++;
      } catch { this.saved.delete(key); }
    }
    await this.flush();
    this.periodic = setInterval(() => { void this.flush(); }, 30000);
    this.periodic.unref();
  }

  private recoverable(cookie: Cookie): boolean {
    return cookie.session === true && cookie.expirationDate === undefined
      && typeof cookie.domain === 'string' && /^[.a-zA-Z0-9_\-:[\]]{1,253}$/.test(cookie.domain)
      && typeof cookie.name === 'string' && cookie.name.length <= 4096
      && typeof cookie.value === 'string' && cookie.value.length <= 65536
      && (!cookie.path || (cookie.path.startsWith('/') && cookie.path.length <= 4096))
      && ['unspecified', 'no_restriction', 'lax', 'strict'].includes(cookie.sameSite);
  }

  private details(cookie: Cookie): CookiesSetDetails {
    const host = cookie.domain!.replace(/^\./, '');
    return { url: `${cookie.secure ? 'https' : 'http'}://${host}${cookie.path || '/'}`,
      name: cookie.name, value: cookie.value, path: cookie.path || '/', secure: cookie.secure,
      httpOnly: cookie.httpOnly, sameSite: cookie.sameSite,
      ...(cookie.hostOnly ? {} : { domain: cookie.domain }) };
  }

  private assertNoLinks() {
    let current = this.filename;
    while (true) {
      try { if (fs.lstatSync(current).isSymbolicLink()) throw new Error('session_storage_link'); }
      catch (error) { if ((error as NodeJS.ErrnoException).code !== 'ENOENT') throw error; }
      const parent = path.dirname(current);
      if (parent === current) break;
      current = parent;
    }
  }

  private load(): Map<string, SavedCookie> {
    const result = new Map<string, SavedCookie>();
    if (!this.protection.available()) return result;
    try {
      this.assertNoLinks();
      if (fs.statSync(this.filename).size > MAX_FILE_BYTES) throw new Error('session_storage_size');
      const value = JSON.parse(this.protection.unprotect(fs.readFileSync(this.filename)));
      if (value.version !== 1 || value.profile !== this.profile || !Array.isArray(value.cookies)
        || value.cookies.length > MAX_COOKIES) throw new Error('invalid_session_storage');
      for (const entry of value.cookies) {
        if (entry && entry.cookie && this.recoverable(entry.cookie)
          && Number.isFinite(entry.recoverUntil) && entry.recoverUntil > this.now()
          && entry.recoverUntil <= this.now() + RECOVERY_LIFETIME_MS) result.set(keyOf(entry.cookie), entry);
      }
    } catch { /* Unreadable, transplanted or expired recovery never grants login. */ }
    return result;
  }

  saveNow() {
    if (this.debounce) { clearTimeout(this.debounce); this.debounce = undefined; }
    const temporary = this.filename + '.tmp';
    let safePaths = false;
    try {
      this.assertNoLinks();
      if (fs.existsSync(temporary) && fs.lstatSync(temporary).isSymbolicLink()) throw new Error('session_storage_link');
      safePaths = true;
      if (!this.protection.available()) {
        // No plaintext fallback; also invalidate older recovery after logout.
        if (fs.existsSync(this.filename)) fs.unlinkSync(this.filename);
        this.code = 'native_only';
        return;
      }
      fs.mkdirSync(path.dirname(this.filename), { recursive: true });
      const cookies = [...this.saved.values()].filter(entry => entry.recoverUntil > this.now())
        .sort((a, b) => b.recoverUntil - a.recoverUntil).slice(0, MAX_COOKIES);
      const text = JSON.stringify({ version: 1, profile: this.profile, cookies });
      if (Buffer.byteLength(text) > MAX_FILE_BYTES / 2) throw new Error('session_storage_size');
      const encrypted = this.protection.protect(text);
      if (encrypted.length > MAX_FILE_BYTES) throw new Error('session_storage_size');
      // One replacement, never a growing history or a readable cookie export.
      const fd = fs.openSync(temporary, 'w', 0o600);
      try { fs.writeFileSync(fd, encrypted); fs.fsyncSync(fd); } finally { fs.closeSync(fd); }
      fs.renameSync(temporary, this.filename);
      this.code = 'ready';
    } catch {
      this.code = 'save_failed';
      // If deletion could not be persisted, stale recovery must not be replayed.
      try { this.assertNoLinks(); if (fs.existsSync(this.filename)) fs.unlinkSync(this.filename); } catch {}
    } finally {
      try { if (safePaths && fs.existsSync(temporary) && !fs.lstatSync(temporary).isSymbolicLink()) fs.unlinkSync(temporary); } catch {}
    }
  }

  async flush() {
    this.saveNow();
    try { this.session.flushStorageData(); } catch { this.code = 'flush_failed'; }
    this.flushAgain = true;
    if (!this.flushing) this.flushing = (async () => {
      do {
        this.flushAgain = false;
        try { await this.session.cookies.flushStore(); }
        catch { this.code = 'flush_failed'; break; }
      } while (this.flushAgain);
    })().finally(() => { this.flushing = undefined; });
    await this.flushing;
  }

  stop() {
    if (this.periodic) clearInterval(this.periodic);
    this.periodic = undefined;
    this.saveNow();
    this.session.cookies.removeListener('changed', this.changed);
  }
}
