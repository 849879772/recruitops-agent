'use strict';
// Explicit, mechanical import of code only. Never reads extension storage or resume files.
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const source = path.resolve(process.argv[2] || '');
if (!process.argv[2] || !path.isAbsolute(process.argv[2])) throw Error('absolute_source_required');
const target = path.resolve(__dirname, '../../packages/desktop_filler/browser-engine');
const existingProvenance = path.join(target, 'provenance.json');
if (fs.existsSync(existingProvenance) && JSON.parse(fs.readFileSync(existingProvenance, 'utf8')).local_patches?.length) {
  throw new Error('local_patches_require_manual_rebase');
}
const names = ['core.js', 'repeater-engine.js', 'content.js'];
const version = JSON.parse(fs.readFileSync(path.join(source, 'manifest.json'), 'utf8')).version;
const files = {};
fs.mkdirSync(target, { recursive: true });
for (const name of names) {
  const file = path.join(source, name);
  if (fs.lstatSync(file).isSymbolicLink()) throw Error('source_symlink_rejected');
  const bytes = fs.readFileSync(file);
  if (!bytes.length || bytes.length > 2 * 1024 * 1024) throw Error('source_size_invalid');
  fs.copyFileSync(file, path.join(target, name));
  files[name] = crypto.createHash('sha256').update(bytes).digest('hex');
}
fs.writeFileSync(path.join(target, 'provenance.json'), JSON.stringify({
  source: 'User-provided local resume-filler browser extension', version,
  imported_files: files, excluded: ['resume-data.js', 'default-resume.pdf', 'background.js', 'sms-bridge', 'agent-binding.local.js'],
}, null, 2) + '\n');
console.log(JSON.stringify({ version, imported: names }));
