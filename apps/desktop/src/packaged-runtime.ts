import fs from 'node:fs';
import path from 'node:path';
import { createHash } from 'node:crypto';
import type { Launch } from './runtime-client';
const { validateRuntime, noLinks } = require('../packaging/resources.cjs');

export async function packagedRuntimeLaunch(resources: string, userData: string, env: NodeJS.ProcessEnv, readOnly = false): Promise<Launch> {
  const root = path.join(resources,'desktop-runtime');
  const verified = await validateRuntime(root);
  const bootstrap = path.join(resources,'desktop-bootstrap.py');
  if (!fs.existsSync(bootstrap)) throw new Error('packaged_bootstrap_missing');
  noLinks(bootstrap); noLinks(userData);
  const configuredRoot = env.RECRUITOPS_DESKTOP_ISOLATION_ROOT;
  if (!configuredRoot || path.basename(configuredRoot) !== '.desktop-runtime-tests') throw new Error('packaged_isolation_root_required');
  noLinks(configuredRoot);
  const isolatedRoot = fs.realpathSync(configuredRoot);
  const profile = fs.realpathSync(userData);
  const relative = path.relative(isolatedRoot,profile);
  if (!relative || relative.startsWith('..') || path.isAbsolute(relative)) throw new Error('packaged_profile_outside_isolation');
  const instance = path.join(isolatedRoot,'shell-runtime-'+createHash('sha256').update(profile.toLowerCase()).digest('hex').slice(0,24));
  const childEnv: NodeJS.ProcessEnv = {};
  // Chromium expands Windows-managed paths such as
  // %SystemDrive%\ProgramData before the isolated API is available. Keep only
  // these non-secret OS location variables so the expansion cannot fall back
  // to a literal relative path inside the sealed runtime bundle.
  for (const key of ['SystemRoot','WINDIR','SystemDrive','ProgramData']) if (env[key]) childEnv[key]=env[key];
  childEnv.HOME = childEnv.USERPROFILE = childEnv.APPDATA = childEnv.LOCALAPPDATA = userData;
  childEnv.TEMP = childEnv.TMP = path.join(userData,'bootstrap-temp');
  childEnv.NO_PROXY = childEnv.no_proxy = '127.0.0.1,localhost,::1';
  fs.mkdirSync(childEnv.TEMP,{recursive:true});
  return {executable:verified.executable,args:['-I','-B',bootstrap,'--resources',root,'--isolation-root',isolatedRoot,
    '--instance',instance,'--start',...(readOnly?[]:['--desktop'])],
    cwd:userData,env:childEnv,desktop:!readOnly};
}
