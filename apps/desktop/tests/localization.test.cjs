'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');

test('tray and exit confirmation use Chinese and default to cancellation', () => {
  const source = fs.readFileSync(path.join(__dirname, '../src/main.ts'), 'utf8');
  assert.match(source, /label: '打开工作台'/);
  assert.match(source, /label: '退出…'/);
  assert.match(source, /title: '退出 RecruitOps', buttons: \['取消', '退出'\], defaultId: 0, cancelId: 0/);
  assert.match(source, /退出后，本地服务及后台任务将停止/);
  assert.match(source, /if \(response === 1\) \{ quitting = true; app.quit\(\); \}/);
  for (const old of ['Show RecruitOps', 'Quit RecruitOps?', 'Clear site session', 'Tray unavailable']) {
    assert.equal(source.includes(old), false);
  }
});
