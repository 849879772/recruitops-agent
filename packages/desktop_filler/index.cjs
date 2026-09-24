'use strict';

const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const vm = require('node:vm');
const { TextDecoder } = require('node:util');

const SOURCE_FILES = Object.freeze(['core.js', 'repeater-engine.js', 'content.js']);
const MAX_FILE_BYTES = 2 * 1024 * 1024;
const MAX_PROFILE_BYTES = 1024 * 1024;
const bundles = new WeakSet();
const builtins = new WeakSet();
const browserEngines = new WeakSet();
const STATE_KEY = '__RECRUITOPS_LOCAL_FILLER_V1__';
const latestOperations = new Map();

function fail(code) { throw new Error(code); }

function assertNoLinks(target) {
  let current = path.resolve(target);
  for (;;) {
    if (fs.lstatSync(current).isSymbolicLink()) fail('filler_symlink_rejected');
    const parent = path.dirname(current);
    if (parent === current) break;
    current = parent;
  }
}

function loadLocalFiller(folder) {
  if (typeof folder !== 'string' || !path.isAbsolute(folder)) fail('filler_absolute_folder_required');
  try {
    assertNoLinks(folder);
    if (!fs.statSync(folder).isDirectory()) fail('filler_folder_required');
    const root = fs.realpathSync(folder);
    const files = [];
    const sources = [];
    for (const name of SOURCE_FILES) {
      const filename = path.join(root, name);
      assertNoLinks(filename);
      const before = fs.lstatSync(filename);
      if (!before.isFile() || before.size <= 0 || before.size > MAX_FILE_BYTES) fail('filler_source_size_invalid');
      const fd = fs.openSync(filename, fs.constants.O_RDONLY | (fs.constants.O_NOFOLLOW || 0));
      let bytes;
      try {
        const opened = fs.fstatSync(fd);
        if (!opened.isFile() || opened.ino !== before.ino || opened.dev !== before.dev
            || opened.size !== before.size) fail('filler_source_changed');
        bytes = Buffer.alloc(opened.size + 1);
        let count = 0;
        while (count < bytes.length) {
          const read = fs.readSync(fd, bytes, count, bytes.length - count, null);
          if (!read) break;
          count += read;
        }
        if (count !== opened.size) fail('filler_source_changed');
        bytes = bytes.subarray(0, count);
        assertNoLinks(filename);
        const after = fs.lstatSync(filename);
        if (after.ino !== opened.ino || after.dev !== opened.dev || after.size !== opened.size
            || after.mtimeMs !== opened.mtimeMs) fail('filler_source_changed');
      } finally { fs.closeSync(fd); }
      const source = new TextDecoder('utf-8', { fatal: true }).decode(bytes);
      files.push(Object.freeze({ name, bytes: bytes.length,
        sha256: crypto.createHash('sha256').update(bytes).digest('hex') }));
      sources.push(source);
    }
    const hash = crypto.createHash('sha256').update(JSON.stringify(files)).digest('hex');
    const bundle = Object.freeze({ source: sources.join('\n;\n'), hash, files: Object.freeze(files) });
    bundles.add(bundle);
    return bundle;
  } catch (error) {
    if (error instanceof Error && /^filler_[a-z_]+$/.test(error.message)) throw error;
    fail('filler_source_unreadable');
  }
}

function profileText(text) {
  if (typeof text !== 'string' || Buffer.byteLength(text) > MAX_PROFILE_BYTES) fail('filler_profile_size_invalid');
}

function cleanProfile(value) {
  let nodes = 0;
  function visit(item, depth) {
    if (++nodes > 20000 || depth > 24) fail('filler_profile_too_complex');
    if (item === null || typeof item === 'string' || typeof item === 'boolean') return item;
    if (typeof item === 'number' && Number.isFinite(item)) return item;
    if (Array.isArray(item)) return item.map(child => visit(child, depth + 1));
    if (!item || typeof item !== 'object' || Object.getPrototypeOf(item) !== Object.prototype) fail('filler_profile_invalid');
    const result = {};
    for (const [key, child] of Object.entries(item)) {
      if (['__proto__', 'constructor', 'prototype'].includes(key)) fail('filler_profile_invalid');
      result[key] = visit(child, depth + 1);
    }
    return result;
  }
  if (!value || Array.isArray(value) || typeof value !== 'object') fail('filler_profile_object_required');
  const result = visit(value, 0);
  if (Buffer.byteLength(JSON.stringify(result)) > MAX_PROFILE_BYTES) fail('filler_profile_size_invalid');
  return result;
}

function parseProfileJson(text) {
  profileText(text);
  try { return cleanProfile(JSON.parse(text)); }
  catch { fail('filler_profile_invalid'); }
}

function parseLegacyProfile(text) {
  profileText(text);
  if (!literalExport(text)) fail('filler_legacy_profile_invalid');
  // Compatibility for explicitly trusted local exports, NOT a sandbox for hostile JS.
  const context = vm.createContext(Object.create(null), {
    codeGeneration: { strings: false, wasm: false }, microtaskMode: 'afterEvaluate',
  });
  try {
    const source = new vm.Script(text + '\n;(() => {\n'
      + 'const p = globalThis.DEFAULT_RESUME ?? globalThis.LOCAL_RESUME_DATA;\n'
      + 'if (!p || typeof p !== "object" || Array.isArray(p)) throw 0;\n'
      + 'return JSON.stringify(p);\n})()', { filename: 'local-profile-import' });
    const result = source.runInContext(context, { timeout: 100 });
    return parseProfileJson(result);
  } catch { fail('filler_legacy_profile_invalid'); }
}

// Restrict legacy compatibility to data literals before using vm. Node vm is
// not a security boundary for arbitrary scripts (including async microtasks).
function literalExport(text) {
  let index = 0;
  function whitespace() {
    for (;;) {
      while (/\s/.test(text[index] || '') && index < text.length) index++;
      if (text.startsWith('//', index)) { while (index < text.length && text[index] !== '\n') index++; }
      else if (text.startsWith('/*', index)) {
        const end = text.indexOf('*/', index + 2);
        if (end < 0) throw 0;
        index = end + 2;
      } else break;
    }
  }
  function token(pattern) {
    whitespace();
    const match = pattern.exec(text.slice(index));
    if (!match) throw 0;
    index += match[0].length;
  }
  function string() {
    whitespace();
    const quote = text[index++];
    if (quote !== '"' && quote !== "'") throw 0;
    while (index < text.length) {
      const ch = text[index++];
      if (ch === quote) return;
      if (ch === '\\') index++;
      else if (ch === '\n' || ch === '\r') throw 0;
    }
    throw 0;
  }
  function value(depth) {
    if (depth > 24) throw 0;
    whitespace();
    const ch = text[index];
    if (ch === '"' || ch === "'") return string();
    if (ch !== '{' && ch !== '[') return token(/^(?:true\b|false\b|null\b|-?(?:0|[1-9]\d*)(?:\.\d+)?(?:[eE][+-]?\d+)?)/);
    index++;
    const close = ch === '{' ? '}' : ']';
    whitespace();
    while (text[index] !== close) {
      if (ch === '{') {
        whitespace();
        if (text[index] === '"' || text[index] === "'") string();
        else token(/^[A-Za-z_$][A-Za-z0-9_$]*/);
        token(/^:/);
      }
      value(depth + 1);
      whitespace();
      if (text[index] === close) break;
      token(/^,/);
      whitespace();
    }
    index++;
  }
  try {
    token(/^globalThis\.(?:DEFAULT_RESUME|LOCAL_RESUME_DATA)\s*=/);
    value(0);
    whitespace();
    if (text[index] === ';') index++;
    whitespace();
    return index === text.length;
  } catch { return false; }
}

function bundleCheck(bundle) {
  if (!bundle || !bundles.has(bundle)) fail('filler_bundle_required');
}

function operationScope(bundle, context) {
  bundleCheck(bundle);
  return JSON.stringify([bundle.hash, frameContext(context)]);
}

function issueOperationId(bundle, context) {
  const scope = operationScope(bundle, context);
  const id = crypto.randomUUID();
  latestOperations.delete(scope);
  latestOperations.set(scope, id);
  while (latestOperations.size > 256) latestOperations.delete(latestOperations.keys().next().value);
  return id;
}

function literal(value) {
  return JSON.stringify(value).replace(/</g, '\\u003c').replace(/\u2028/g, '\\u2028').replace(/\u2029/g, '\\u2029');
}

// Keep framework labels in both answer matching and the scan-to-fill identity check.
function fieldContext(el) {
  const containerSelector = '.form-item,.el-form-item,.ant-form-item,.ivu-form-item';
  const clean = text => String(text || '').replace(/\s+/g, ' ').trim().replace(/^[*＊\s]+|[:：*＊\s]+$/g, '');
  const container = el.closest(containerSelector);
  const frameworkLabels = container ? Array.from(container.querySelectorAll(
    '.form-item__title .form-item__text,.form-item__title > label,.el-form-item__label,.ant-form-item-label > label,.ivu-form-item-label'
  )).filter(node => node.closest(containerSelector) === container).map(node => clean(node.textContent)).filter(Boolean) : [];
  const titles = Array.from(new Set(frameworkLabels));
  const section = el.closest('.form-part,[data-resume-section],fieldset,section');
  const headings = section ? Array.from(section.querySelectorAll('.head-title,legend,h1,h2,h3,h4,[role="heading"]'))
    .filter(node => node.closest('.form-part,[data-resume-section],fieldset,section') === section)
    .map(node => clean(node.textContent)).filter(Boolean) : [];
  const record = el.closest('.ux-standard-form');
  const formId = node => node.querySelector('.form[id]')?.id || '';
  const recordType = record ? formId(record) : '';
  const peers = recordType ? Array.from(document.querySelectorAll('.ux-standard-form')).filter(node => formId(node) === recordType) : [];
  const recordVisible = node => !node.closest('[hidden],[inert],[aria-hidden="true"]')
    && node.getClientRects().length > 0 && !['hidden', 'collapse'].includes(getComputedStyle(node).visibility);
  const recordAmbiguous = peers.some(node => !recordVisible(node));
  const recordIndex = record ? peers.indexOf(record) : -1;
  const ancestry = [];
  for (let node = container; node && node !== section && node !== document.body; node = node.parentElement) {
    ancestry.push([node.tagName, node.id || '', Array.from(node.parentElement?.children || []).indexOf(node)]);
  }
  const placeholders = [el.getAttribute('placeholder'), el.getAttribute('aria-label'), el.name, el.id].filter(Boolean);
  const excluded = Boolean(el.closest('header,nav,[role="search"]')) || el.type === 'search'
    || placeholders.some(text => /搜索|检索|search.*(?:job|keyword)|(?:job|keyword).*search/i.test(text));
  return { titles, headings, ancestry, excluded, recordType, recordIndex, recordAmbiguous };
}

function phoenixSelection(el) {
  const recognized = /(?:^|\s)phoenix-select__input(?:\s|$)/.test(el.getAttribute('class') || '');
  const root = recognized ? el.closest('.phoenix-select') : null;
  const supported = Boolean(root && root.querySelectorAll('.phoenix-select__input').length === 1
    && !/(?:^|\s)phoenix-select--multi(?:\s|$)/.test(root.getAttribute('class') || ''));
  const disabled = Boolean(root && /(?:^|\s)phoenix-select--disabled(?:\s|$)/.test(root.getAttribute('class') || ''));
  const empty = Boolean(root?.querySelector('.phoenix-select__placeHolder--show'));
  const labels = root ? Array.from(root.querySelectorAll('.phoenix-select__tipEle,.phoenix-select__singleLabel'))
    .map(node => node.textContent.trim()).filter(Boolean) : [];
  const values = Array.from(new Set(labels));
  return { recognized, supported, disabled, value: empty ? '' : values.length === 1 ? values[0] : null };
}

// Independently authored, dependency-free resource. No extension source is bundled.
function bundledEngine(chrome, fieldContext, phoenixSelection) {
  const attr = 'data-local-resume-field-id';
  let sequence = 0;
  const originals = new Map();
  const markers = new WeakMap();
  let recordMetadata = new WeakMap();
  let radioPlans = new Map();
  const blocked = /password|passwd|captcha|one.time|verification|otp|sms|验证码|密码|短信/i;
  const labelledByText = el => (el.getAttribute('aria-labelledby') || '').split(/\s+/).filter(Boolean)
    .map(id => document.getElementById(id)?.textContent).filter(Boolean);
  const label = el => {
    const context = fieldContext(el);
    return context.titles.length ? context.titles.join(' / ') :
      [el.getAttribute('aria-label'), ...labelledByText(el), ...Array.from(el.labels || []).map(l => l.textContent),
        el.getAttribute('placeholder'), el.name, el.id].filter(Boolean).join(' ').trim();
  };
  const choiceLabels = el => {
    return [el.getAttribute('aria-label'), ...Array.from(el.labels || []).map(l => l.textContent),
      ...labelledByText(el)]
      .filter(Boolean).map(text => String(text).trim()).filter(Boolean);
  };
  const visible = el => el.isConnected && !el.disabled &&
    (!el.readOnly || phoenixSelection(el).supported || (el.getAttribute('role') === 'combobox' && Boolean(controlledListbox(el)))) &&
    !el.closest('[hidden],[inert],[aria-hidden="true"]') && el.getClientRects().length > 0 &&
    !['hidden', 'collapse'].includes(getComputedStyle(el).visibility) && getComputedStyle(el).opacity !== '0';
  const eligible = el => {
    const combo = el.getAttribute('role') === 'combobox';
    const allowedComboButton = combo && el.tagName === 'BUTTON' && el.type === 'button';
    return visible(el) && !phoenixSelection(el).disabled && !fieldContext(el).excluded && !blocked.test(label(el) + ' ' + el.autocomplete) &&
      (!['password', 'hidden', 'submit', 'button', 'reset', 'image'].includes(el.type) || allowedComboButton);
  };
  const mark = el => {
    if (markers.has(el) && document.querySelectorAll('[' + attr + '="' + CSS.escape(markers.get(el)) + '"]').length > 1) markers.delete(el);
    if (!markers.has(el)) {
      let id; do { id = 'rf-' + (++sequence); } while (document.querySelector('[' + attr + '="' + id + '"]'));
      markers.set(el, id);
    }
    el.setAttribute(attr, markers.get(el)); return markers.get(el);
  };
  const get = id => document.querySelector('[' + attr + '="' + CSS.escape(id) + '"]');
  const value = el => {
    const phoenix = phoenixSelection(el);
    if (phoenix.recognized) return phoenix.value;
    if (el.getAttribute('role') === 'combobox') {
      if (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA') return el.value;
      const root = controlledListbox(el);
      const selected = root ? Array.from(root.querySelectorAll('[role="option"]'))
        .filter(option => option.getAttribute('aria-selected') === 'true') : [];
      if (selected.length === 1) return optionLabel(selected[0]);
      return el.getAttribute('aria-valuetext') || el.textContent.trim();
    }
    return el.tagName === 'SELECT' && el.multiple ? Array.from(el.selectedOptions).map(o => o.value)
      : ['radio', 'checkbox'].includes(el.type) ? el.checked : el.isContentEditable ? el.textContent : el.value;
  };
  const shape = el => JSON.stringify([label(el), fieldContext(el), el.type, el.getAttribute('role'), el.getAttribute('data-resume-key'),
    el.getAttribute('aria-controls'), el.getAttribute('aria-owns'),
    Array.from(el.options || []).map(o => [o.value, o.textContent, o.disabled])]);
  function controlledListbox(el) {
    const refs = [...(el.getAttribute('aria-controls') || '').split(/\s+/),
      ...(el.getAttribute('aria-owns') || '').split(/\s+/)].filter(Boolean);
    const ids = Array.from(new Set(refs));
    if (ids.length !== 1) return null;
    const matches = Array.from(document.querySelectorAll('[id]')).filter(node => node.id === ids[0]);
    return matches.length === 1 && matches[0].getAttribute('role') === 'listbox' ? matches[0] : null;
  }
  function optionLabel(option) {
    const ids = (option.getAttribute('aria-labelledby') || '').split(/\s+/).filter(Boolean);
    return String(option.getAttribute('aria-label') || ids.map(id => document.getElementById(id)?.textContent).filter(Boolean).join(' ')
      || option.textContent || '').trim();
  }
  function optionMatches(root, raw) {
    const text = String(raw).trim();
    const matches = Array.from(root.querySelectorAll('[role="option"]'))
      .filter(option => optionLabel(option) === text || option.getAttribute('data-value') === text);
    if (matches.length > 1) throw new Error('filler_option_ambiguous');
    if (!matches.length) return null;
    const option = matches[0];
    if (option.disabled || option.getAttribute('aria-disabled') === 'true'
        || option.closest('[aria-disabled="true"]') || !visible(option)) throw new Error('filler_option_disabled');
    return option;
  }
  function waitForMutation(predicate, timeoutMs, cancelToken) {
    return new Promise(resolve => {
      let timer;
      let observer;
      const finish = result => {
        observer?.disconnect();
        clearTimeout(timer);
        if (cancelToken?.wake === finish) cancelToken.wake = null;
        resolve(result);
      };
      const check = () => {
        if (cancelToken?.cancelled) return finish('cancelled');
        let ready = false;
        try { ready = predicate(); } catch { return finish('changed'); }
        if (ready) finish('ready');
      };
      observer = new MutationObserver(check);
      observer.observe(document, { childList: true, subtree: true, attributes: true });
      timer = setTimeout(() => finish('timeout'), timeoutMs);
      if (cancelToken) cancelToken.wake = finish;
      check();
    });
  }
  async function setCombobox(el, requested, cancelToken) {
    if (typeof requested !== 'string' && typeof requested !== 'number') throw new Error('filler_answer_type_invalid');
    const target = String(requested).trim();
    const root = controlledListbox(el);
    if (!root) throw new Error('filler_combobox_unsupported');
    const before = value(el);
    const readOnly = el.readOnly || el.getAttribute('aria-readonly') === 'true';
    if (!target) {
      if (readOnly && String(before).trim()) throw new Error('filler_combobox_readonly');
      if (readOnly) return;
      if (el.tagName !== 'INPUT' && el.tagName !== 'TEXTAREA') throw new Error('filler_option_missing');
      const prototype = el.tagName === 'TEXTAREA' ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
      Object.getOwnPropertyDescriptor(prototype, 'value').set.call(el, '');
      el.dispatchEvent(new Event('input', { bubbles: true }));
      return;
    }
    if ((el.tagName === 'INPUT' || el.tagName === 'TEXTAREA') && !readOnly) {
      const prototype = el.tagName === 'TEXTAREA' ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
      Object.getOwnPropertyDescriptor(prototype, 'value').set.call(el, target);
      el.dispatchEvent(new Event('input', { bubbles: true }));
    } else {
      if (typeof el.click !== 'function') throw new Error('filler_combobox_unsupported');
      el.click();
    }
    const opened = await waitForMutation(() => {
      const current = controlledListbox(el);
      return Boolean(current && visible(current) && current.querySelectorAll('[role="option"]').length);
    }, 2000, cancelToken);
    if (opened === 'cancelled') throw new Error('filler_operation_cancelled');
    if (opened !== 'ready') throw new Error('filler_option_missing');
    const currentRoot = controlledListbox(el);
    if (!currentRoot) throw new Error('filler_combobox_ambiguous');
    const option = optionMatches(currentRoot, target);
    if (!option) {
      if (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA') {
        const prototype = el.tagName === 'TEXTAREA' ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
        Object.getOwnPropertyDescriptor(prototype, 'value').set.call(el, before);
        el.dispatchEvent(new Event('input', { bubbles: true }));
      }
      throw new Error('filler_option_missing');
    }
    if (typeof option.click !== 'function') throw new Error('filler_combobox_unsupported');
    option.click();
    const selected = await waitForMutation(() => {
      if (option.getAttribute('aria-selected') === 'true') return true;
      return value(el) === target && (before !== target || el.getAttribute('role') === 'combobox');
    }, 1000, cancelToken);
    if (selected === 'cancelled') throw new Error('filler_operation_cancelled');
    if (selected !== 'ready') throw new Error('filler_option_selection_unverified');
  }
  async function setPhoenix(el, requested, cancelToken) {
    if (typeof requested !== 'string' && typeof requested !== 'number') throw new Error('filler_answer_type_invalid');
    const target = String(requested).trim();
    const info = phoenixSelection(el);
    if (!info.supported || info.disabled) throw new Error('filler_custom_control_unsupported');
    if (info.value === target) return;
    const root = el.closest('.phoenix-select');
    const fingerprint = shape(el);
    const popups = () => Array.from(document.querySelectorAll('.phoenix-selectList')).filter(visible);
    // A portal has no aria owner: require the only newly opened list, never a pre-existing one.
    if (popups().length) throw new Error('filler_combobox_ambiguous');
    if (!target) {
      const buttons = Array.from(root.querySelectorAll('.phoenix-select__clearIcon')).filter(visible);
      if (buttons.length !== 1) throw new Error('filler_option_missing');
      buttons[0].click();
    } else {
      el.click();
      const opened = await waitForMutation(() => popups().length > 0, 2000, cancelToken);
      if (opened === 'cancelled') throw new Error('filler_operation_cancelled');
      if (opened !== 'ready') throw new Error('filler_option_missing');
      const lists = popups();
      if (lists.length !== 1 || !eligible(el) || el.closest('.phoenix-select') !== root || shape(el) !== fingerprint)
        throw new Error('filler_combobox_ambiguous');
      const options = Array.from(lists[0].querySelectorAll('.phoenix-selectList__listItem'))
        .filter(option => option.textContent.trim() === target);
      if (options.length !== 1) throw new Error(options.length ? 'filler_option_ambiguous' : 'filler_option_missing');
      const option = options[0];
      if (!visible(option) || option.getAttribute('aria-disabled') === 'true'
          || /(?:^|\s)phoenix-selectList__listItem--disabled(?:\s|$)/.test(option.getAttribute('class') || ''))
        throw new Error('filler_option_disabled');
      option.click();
    }
    const selected = await waitForMutation(() => phoenixSelection(el).value === target, 1000, cancelToken);
    if (selected === 'cancelled') throw new Error('filler_operation_cancelled');
    if (selected !== 'ready') throw new Error('filler_option_selection_unverified');
  }
  const set = (el, v) => {
    if (el.tagName === 'SELECT') {
      const requested = (Array.isArray(v) ? v : [v]).map(String);
      const options = Array.from(el.options);
      const chosen = requested.map(s => {
        const found = options.filter(o => o.value === s || o.textContent.trim() === s);
        if (found.length > 1) throw new Error('filler_option_ambiguous');
        if (!found.length) throw new Error('filler_option_missing');
        if (found[0].disabled) throw new Error('filler_option_disabled');
        return found[0];
      });
      if ((!el.multiple && chosen.length !== 1) || new Set(chosen).size !== chosen.length) throw new Error('filler_option_ambiguous');
      for (const o of options) o.selected = chosen.includes(o);
    } else if (el.isContentEditable) el.textContent = String(v);
    else {
      const prop = ['checkbox', 'radio'].includes(el.type) ? 'checked' : 'value';
      const prototype = el.tagName === 'TEXTAREA' ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
      Object.getOwnPropertyDescriptor(prototype, prop).set.call(el, prop === 'checked' ? Boolean(v) : String(v));
    }
    el.dispatchEvent(new Event('input', { bubbles: true }));
    el.dispatchEvent(new Event('change', { bubbles: true }));
  };
  const paths = profile => {
    const out = [];
    const walk = (v, key) => {
      if (v !== null && typeof v === 'object' && !Array.isArray(v)) {
        for (const [k, child] of Object.entries(v)) if (k !== 'customAnswers') walk(child, key ? key + '.' + k : k);
      } else if (v !== null && v !== undefined) out.push([key, v]);
    };
    walk(profile, ''); return out;
  };
  const aliases = {
    fullName: /^(姓名|中文姓名|真实姓名|申请人姓名|full name|name)$/i,
    email: /^(邮箱|电子邮箱|电子邮件|邮箱地址|电子邮箱地址|常用邮箱|email)$/i,
    phone: /^(手机|手机号|手机号码|联系电话|联系手机|移动电话|phone|mobile)$/i,
    gender: /^(性别|gender)$/i, birthDate: /^(出生日期|出生年月|生日|birth date)$/i,
    age: /^年龄$/, nationality: /^民族$/, countryRegion: /^(国籍|国籍\/地区)$/,
    height: /^身高(?:[（(]cm[）)])?$/i, weight: /^体重(?:[（(]kg[）)])?$/i,
    idNumber: /^(证件号码|身份证号|身份证号码)$/, nativePlace: /^籍贯$/,
    currentResidence: /^(现居住地|现居地|现居住地址|现住址)$/,
    studyMode: /^(学习形式|学习方式)$/, applicantStatus: /^(第几届应届生|应届生届别|毕业届别)$/,
    englishLevel: /^(英语等级|英语水平)$/, englishScore: /^英语成绩$/,
    emergencyContact: /^(紧急联系人|紧急联系人姓名)$/, emergencyPhone: /^(紧急联系电话|紧急联系人电话)$/,
    recruitmentSource: /^(招聘渠道|信息来源|应聘来源)$/, healthStatus: /^健康状况$/,
    interviewSite: /^(面试地点|面试站点)$/, desiredSalary: /^(期望薪资|期望月薪|薪资要求)$/,
    preferredCity: /^(期望城市|意向城市|意向工作地|期望工作城市)$/,
    selfIntroduction: /^(自我介绍|自我评价)$/, skills: /^(专业技能|技能特长)$/,
    school: /^(学校|学校名称|毕业院校|就读学校)$/, college: /^(学院|院系)$/,
    major: /^(专业|专业名称|所学专业)$/, degree: /^(学历|学位|最高学历)$/,
    startDate: /^(开始时间|开始日期|入学时间)$/, endDate: /^(结束时间|结束日期|毕业时间)$/,
    gpa: /^(GPA|平均绩点)$/i, gradeRank: /^(成绩排名|专业排名)$/,
    name: /^(项目名称|奖项名称|证书名称)$/, role: /^(项目角色|担任角色)$/,
    summary: /^(项目描述|项目简介)$/, results: /^(项目成果|项目业绩)$/,
  };
  const controlSelector = 'input,textarea,select,[contenteditable="true"],[role="combobox"]';
  function sectionKind(key) {
    if (/(education|educat|school|degree|学历|教育|学校)/i.test(key)) return 'education';
    if (/(work|employment|experience|career|intern|实习|工作|任职|职业)/i.test(key)) return 'work';
    if (/(project|项目)/i.test(key)) return 'project';
    return null;
  }
  function kindMatches(text, kind) {
    const pattern = kind === 'education' ? /education|school|degree|教育|学历|学校/i
      : kind === 'work' ? /work|employment|experience|career|intern|工作|实习|任职|职业/i
        : /project|项目/i;
    return pattern.test(text || '');
  }
  function sectionHeading(root) {
    return Array.from(root.children || []).filter(child =>
      /^H[1-6]$/.test(child.tagName) || child.tagName === 'LEGEND' || child.getAttribute('role') === 'heading')
      .map(child => String(child.textContent || '').trim()).filter(Boolean);
  }
  function controlsWithin(root) {
    return Array.from(root.querySelectorAll(controlSelector)).filter(el =>
      !['submit', 'button', 'reset', 'image'].includes(el.type) && el.getAttribute('role') !== 'button');
  }
  function controlSignature(root) {
    const controls = controlsWithin(root);
    if (controls.length < 2) return '';
    return JSON.stringify(controls.map(el => [el.getAttribute('data-resume-key') || '', el.name || '', label(el)]));
  }
  function recordsFor(root) {
    const tagged = Array.from(root.querySelectorAll('[data-resume-record]'));
    if (tagged.length) {
      const parent = tagged[0].parentElement;
      return tagged.every(record => record.parentElement === parent) ? tagged : null;
    }
    const structural = Array.from(root.querySelectorAll('fieldset,article,[role="group"]'))
      .filter(node => node !== root && controlsWithin(node).length >= 2);
    const outerStructural = structural.filter(node => !structural.some(other => other !== node && other.contains(node)));
    if (outerStructural.length) return outerStructural;

    const containers = [root, ...Array.from(root.querySelectorAll('div,section,article,fieldset,[role="group"]'))];
    const repeated = [];
    for (const parent of containers) {
      const children = Array.from(parent.children || []).filter(child => controlsWithin(child).length > 0);
      if (children.length < 2) continue;
      const signatures = children.map(controlSignature);
      if (signatures[0] && signatures.every(signature => signature === signatures[0])) repeated.push(children);
    }
    if (repeated.length === 1) return repeated[0];
    if (repeated.length > 1) return null;

    const direct = Array.from(root.children || []).filter(child => controlsWithin(child).length > 0);
    if (direct.length === 1 && controlsWithin(direct[0]).length >= 2) return direct;
    if (!direct.length) return [];
    return null;
  }
  function addButtons(root) {
    return Array.from(root.querySelectorAll('button')).filter(button => {
      const name = [button.getAttribute('aria-label'), button.getAttribute('title'), button.textContent]
        .filter(Boolean).join(' ').trim();
      return button.type === 'button' && visible(button) && !button.disabled
        && (button.hasAttribute('data-resume-add') || /\b(add|new|create)\b|添加|新增|增加|新建|补充/i.test(name))
        && !/submit|save|delete|remove|cancel|提交|保存|删除|移除|取消/i.test(name);
    });
  }
  function findRepeaters(profile) {
    const plans = [];
    for (const [sectionId, rows] of Object.entries(profile)) {
      if (!Array.isArray(rows) || sectionId === 'customAnswers') continue;
      const kind = sectionKind(sectionId);
      const containers = Array.from(document.querySelectorAll('section,fieldset,article,[role="group"],div'))
        .filter(root => root.getAttribute('data-resume-section') === sectionId
          || (kind && sectionHeading(root).some(text => kindMatches(text, kind))));
      const uniqueRoots = Array.from(new Set(containers));
      const root = uniqueRoots.length === 1 ? uniqueRoots[0] : null;
      const buttons = root ? addButtons(root) : [];
      const button = buttons.length === 1 ? buttons[0] : null;
      const records = root && button ? recordsFor(root) : null;
      const supported = Boolean(root && button && records && rows.length <= 10);
      plans.push({ sectionId, count: records?.length || 0, desired: rows.length, supported,
        reason: rows.length > 10 ? 'filler_repeater_limit'
          : !root || uniqueRoots.length !== 1 || !records ? 'filler_repeater_unsupported'
            : buttons.length !== 1 ? 'filler_repeater_unsupported' : 'ready',
        root, button, getRecords: root && button ? () => recordsFor(root) : null });
    }
    return plans;
  }
  function resolve(el, profile) {
    const context = fieldContext(el);
    if (context.titles.length > 1) return { reason: 'filler_answer_label_ambiguous' };
    let key = el.getAttribute('data-resume-key');
    let recordInfo;
    for (let node = el; node; node = node.parentElement) {
      if (recordMetadata.has(node)) { recordInfo = recordMetadata.get(node); break; }
    }
    const record = recordInfo ? recordInfo.node : el.closest('[data-resume-record]');
    let section = recordInfo?.sectionId || el.closest('[data-resume-section]')?.getAttribute('data-resume-section');
    let data = profile;
    let recordIndex;
    if (record && section) {
      if (recordInfo) recordIndex = recordInfo.index;
      else {
        const records = Array.from(record.parentElement.querySelectorAll(':scope > [data-resume-record]'));
        recordIndex = records.indexOf(record);
      }
      data = profile[section]?.[recordIndex] || {};
    } else if (context.recordType && context.recordIndex >= 0) {
      const sectionAliases = {
        education: /education|教育/i, projects: /project|项目/i,
        internships: /internship|实习/i, work: /workexperience|employment|工作经历/i,
        awards: /award|获奖/i, certificates: /certificate|证书/i, publications: /publication|论文/i,
      };
      const kinds = Object.entries(sectionAliases).filter(([, pattern]) => pattern.test(context.recordType));
      if (kinds.length > 1) return { reason: 'filler_answer_conflict' };
      if (kinds.length === 1) {
        if (context.recordAmbiguous) return { reason: 'filler_answer_label_ambiguous' };
        section = kinds[0][0];
        recordIndex = context.recordIndex;
        // Never reuse record zero for every visible education/project entry.
        data = profile[section]?.[recordIndex] || {};
      }
    }
    const hints = [el.name, el.id, el.getAttribute('aria-label'), ...labelledByText(el),
      ...context.titles, ...Array.from(el.labels || []).map(l => l.textContent)]
      .filter(Boolean).map(s => String(s).trim().replace(/^[*＊\s]+|[:：*＊\s]+$/g, ''));
    const candidates = paths(data).filter(([p]) => key ? p === key :
      hints
        .some(s => s && (s.toLowerCase() === p.split('.').pop().toLowerCase() || aliases[p.split('.').pop()]?.test(s))));
    const answers = (Array.isArray(profile.customAnswers) ? profile.customAnswers : []).filter(a =>
      a && a.origin === location.origin && a.pathname === location.pathname && a.label === label(el));
    if (answers.length > 1 || (!answers.length && candidates.length > 1)) return { reason: 'filler_answer_conflict' };
    if (answers.length === 1) candidates.splice(0, candidates.length, ['custom:' + answers[0].id, answers[0].value]);
    if (!candidates.length) return { reason: 'filler_answer_missing' };
    let v = candidates[0][1];
    if (el.type === 'radio') {
      if (String(v) !== el.value && !choiceLabels(el).includes(String(v).trim())) return { reason: 'filler_choice_not_selected' };
      v = true;
    }
    if (el.type === 'checkbox' && Array.isArray(v)) {
      v = v.some(choice => String(choice) === el.value || choiceLabels(el).includes(String(choice).trim()));
    }
    if (el.type === 'checkbox' && typeof v !== 'boolean') return { reason: 'filler_answer_type_invalid' };
    if (Array.isArray(v) && !(el.tagName === 'SELECT' && el.multiple)) return { reason: 'filler_answer_type_invalid' };
    if (v === null || v === undefined || (typeof v === 'object' && !Array.isArray(v))) return { reason: 'filler_answer_type_invalid' };
    return { key: candidates[0][0], value: v, section, recordIndex };
  }
  function scan(profile) {
    radioPlans = new Map();
    recordMetadata = new WeakMap();
    const controls = Array.from(document.querySelectorAll(controlSelector));
    const eligibleControls = controls.filter(eligible);
    const labelCounts = new Map();
    for (const el of eligibleControls) {
      const text = label(el);
      if (text) labelCounts.set(text, (labelCounts.get(text) || 0) + 1);
    }
    const matches = [], candidates = [], diagnostics = [], attachments = [], repeaters = [];
    const plans = findRepeaters(profile);
    for (const plan of plans) {
      for (const [index, node] of (plan.getRecords?.() || []).entries())
        recordMetadata.set(node, { node, sectionId: plan.sectionId, index });
      repeaters.push(plan);
    }
    const addCandidate = (el, fieldId, text, reason, customAnswerSupported = false) => {
      const item = { fieldId, label: text.slice(0, 300) || '未命名控件',
        controlKind: el.type || el.tagName.toLowerCase(), reason, customAnswerSupported };
      candidates.push(item);
      diagnostics.push({ fieldId, reason });
    };
    for (const el of eligibleControls) {
      const fieldId = mark(el), text = label(el);
      if (el.type === 'file') {
        if (/resume|\bcv\b|简历/i.test(text) && !/photo|transcript|certificate|照片|成绩单|证书/i.test(text))
          attachments.push({ fieldId, label: text, accept: el.accept, multiple: el.multiple });
        else addCandidate(el, fieldId, text, 'filler_attachment_unsupported');
        continue;
      }
      if (el.getAttribute('role') === 'combobox') {
        if (!controlledListbox(el)) { addCandidate(el, fieldId, text, 'filler_custom_control_unsupported'); continue; }
      }
      const phoenix = phoenixSelection(el);
      if (phoenix.recognized && (!phoenix.supported || /日期|年月|时间|生日|地区|居住|籍贯/.test(text))) {
        addCandidate(el, fieldId, text, 'filler_custom_control_unsupported'); continue;
      }
      const answer = resolve(el, profile);
      if (answer.reason) {
        const duplicateLabel = !text || labelCounts.get(text) !== 1 || text.length > 300;
        const reason = duplicateLabel && ['filler_answer_missing', 'filler_answer_conflict', 'filler_choice_not_selected'].includes(answer.reason)
          ? 'filler_answer_label_ambiguous' : answer.reason;
        const customAnswerSupported = !duplicateLabel
          && ['filler_answer_missing', 'filler_answer_conflict', 'filler_choice_not_selected'].includes(answer.reason);
        addCandidate(el, fieldId, text, reason, customAnswerSupported);
      }
      else {
        matches.push({ fieldId, label: text, controlKind: el.type || el.tagName.toLowerCase(), ...answer });
        if (el.type === 'radio') radioPlans.set(fieldId, controls.filter(e => e.type === 'radio' && e.name === el.name && e.form === el.form)
          .map(e => ({ el: e, label: label(e), checked: e.checked })));
      }
    }
    return { ok: true, totalFields: eligibleControls.length, emptyFields: eligibleControls.filter(e => !value(e)).length,
      matches, candidates, diagnostics, attachments, repeaters };
  }
  chrome.runtime.onMessage.addListener((message, sender, reply) => {
    try {
      if (message.type === 'RESUME_SCAN') return reply(scan(message.resume));
      if (message.type === 'RESUME_FILL') {
        const a = message.assignments[0], el = get(a.fieldId);
        if (!el || !eligible(el) || el.type === 'file') return reply({ ok: false });
        const related = el.type === 'radio' ? Array.from(document.querySelectorAll('input[type="radio"]'))
          .filter(e => e.name === el.name && e.form === el.form) : [el];
        if (el.type === 'radio') {
          const plan = radioPlans.get(a.fieldId);
          if (!plan || plan.length !== related.length || plan.some(p => !related.includes(p.el) || !eligible(p.el)
              || p.el.type !== 'radio' || label(p.el) !== p.label || p.el.checked !== p.checked))
            return reply({ ok: false, reason: 'filler_field_changed_rescan' });
        }
        for (const node of related) if (!originals.has(node)
            || JSON.stringify(originals.get(node).after) !== JSON.stringify(value(node))) originals.set(node, { before: value(node) });
        const saveAfter = () => {
          for (const node of related) {
            const saved = originals.get(node); saved.after = value(node); saved.shape = shape(node);
            saved.afterInput = phoenixSelection(node).recognized ? node.value : null;
            saved.id = mark(node); saved.group = related;
          }
        };
        if (phoenixSelection(el).recognized || el.getAttribute('role') === 'combobox') {
          const setChoice = phoenixSelection(el).recognized ? setPhoenix : setCombobox;
          setChoice(el, a.value, message.cancelToken).then(() => {
            saveAfter();
            const verified = String(value(el)).trim() === String(a.value).trim();
            reply({ ok: verified, filled: verified ? 1 : 0, reason: verified ? undefined : 'filler_option_selection_unverified' });
          }, error => {
            saveAfter();
            reply({ ok: false, reason: error?.message && /^filler_[a-z_]+$/.test(error.message)
              ? error.message : 'filler_field_failed' });
          });
          return;
        }
        let reason;
        try { set(el, a.value); } catch (error) { reason = error?.message && /^filler_[a-z_]+$/.test(error.message) ? error.message : 'filler_field_failed'; }
        saveAfter();
        const expected = el.tagName === 'SELECT' ? (Array.isArray(a.value) ? a.value : [a.value]).map(String) : a.value;
        const actual = el.tagName === 'SELECT' ? Array.from(el.selectedOptions).map(o => [o.value, o.textContent.trim()]) : value(el);
        const verified = el.tagName === 'SELECT' ? actual.length === expected.length && expected.every(v => actual.some(o => o.includes(v)))
          : String(actual) === String(expected);
        return reply({ ok: !reason && verified, filled: !reason && verified ? 1 : 0, reason });
      }
      if (message.type === 'RESUME_UNDO') {
        (async () => {
          let restored = 0; const failed = [];
          const canRestore = (el, old) => eligible(el) && get(old.id) === el && old.shape === shape(el)
            && JSON.stringify(value(el)) === JSON.stringify(old.after)
            && (!phoenixSelection(el).recognized || el.value === old.afterInput);
          const unsafeNodes = new Set();
          for (const [el, old] of originals) if (!canRestore(el, old)) for (const node of old.group) unsafeNodes.add(node);
          for (const [el, old] of originals) {
            if (JSON.stringify(old.before) === JSON.stringify(old.after)) { originals.delete(el); continue; }
            if (unsafeNodes.has(el)) { failed.push(old.id); continue; }
            try {
              if (phoenixSelection(el).recognized) await setPhoenix(el, String(old.before ?? ''), null);
              else if (el.getAttribute('role') === 'combobox') await setCombobox(el, String(old.before ?? ''), null);
              else set(el, old.before);
              restored++; originals.delete(el);
            } catch { failed.push(old.id); }
          }
          reply({ ok: !failed.length, restored, failed });
        })().catch(() => reply({ ok: false, restored: 0, failed: [] }));
        return;
      }
    } catch { reply({ ok: false, reason: 'filler_execution_failed' }); }
  });
}

function loadBundledFiller() {
  const source = '(' + bundledEngine.toString() + ')(chrome,' + fieldContext.toString() + ',' + phoenixSelection.toString() + ');';
  const hash = crypto.createHash('sha256').update(source).digest('hex');
  const bundle = Object.freeze({ source, hash, files: Object.freeze([
    Object.freeze({ name: 'builtin-engine', bytes: Buffer.byteLength(source), sha256: hash }),
  ]) });
  bundles.add(bundle); builtins.add(bundle); return bundle;
}

// The desktop ships the same DOM adapters as the user's browser extension,
// without its background process, profile data, SMS bridge or network APIs.
function loadDesktopFiller() {
  const bundle = loadLocalFiller(path.join(__dirname, 'browser-engine'));
  browserEngines.add(bundle);
  builtins.add(bundle);
  return bundle;
}

function frameContext(context) {
  if (context === undefined) return null;
  const keys = ['instanceId', 'tabId', 'frameId', 'documentId', 'profileVersion', 'href'];
  if (!context || keys.some(k => typeof context[k] !== 'string' || !context[k] || context[k].length > 4096)
      || Object.keys(context).some(k => ![...keys, 'allowSubframe'].includes(k))
      || (context.allowSubframe !== undefined && typeof context.allowSubframe !== 'boolean')) fail('filler_frame_context_invalid');
  try { if (!['https:', 'http:'].includes(new URL(context.href).protocol)) fail('filler_frame_context_invalid'); }
  catch { fail('filler_frame_context_invalid'); }
  return Object.fromEntries([...keys, 'allowSubframe'].map(k => [k, context[k] ?? false]));
}

function aggregateFrameScans(outcomes) {
  if (!Array.isArray(outcomes) || outcomes.length > 100) fail('filler_frame_results_invalid');
  const seen = new Set(), fields = [], candidates = [], failures = [];
  for (const outcome of outcomes) {
    const route = frameContext(outcome?.route);
    if (!route) fail('filler_frame_results_invalid');
    const key = JSON.stringify(route);
    if (seen.has(key)) fail('filler_frame_results_invalid');
    seen.add(key);
    if (outcome.status !== 'scanned') {
      if (!['blocked', 'sandboxed', 'detached', 'unreachable'].includes(outcome.status)) fail('filler_frame_results_invalid');
      failures.push({ route, code: 'filler_frame_' + outcome.status }); continue;
    }
    const scan = outcome.scan;
    if (!scan || scan.ok !== true || JSON.stringify(frameContext(scan.route)) !== key || !Array.isArray(scan.matches)
        || (scan.candidates !== undefined && !Array.isArray(scan.candidates))) fail('filler_frame_results_invalid');
    for (const match of scan.matches) fields.push({ route, scanId: scan.scanId, fieldId: match.fieldId,
      selectionId: JSON.stringify([route.instanceId, route.tabId, route.frameId, route.documentId, scan.scanId, match.fieldId]), match });
    const localIds = new Set(scan.matches.map(match => match?.fieldId).filter(value => typeof value === 'string'));
    for (const candidate of scan.candidates || []) {
      if (!candidate || typeof candidate.fieldId !== 'string' || !candidate.fieldId || candidate.fieldId.length > 200
          || localIds.has(candidate.fieldId) || typeof candidate.label !== 'string' || candidate.label.length > 300
          || typeof candidate.controlKind !== 'string' || candidate.controlKind.length > 32
          || !/^filler_[a-z0-9_]{1,80}$/.test(candidate.reason || '')
          || typeof candidate.customAnswerSupported !== 'boolean'
          || Object.keys(candidate).some(name => !['fieldId', 'label', 'controlKind', 'reason', 'customAnswerSupported'].includes(name)))
        fail('filler_frame_results_invalid');
      if (localIds.has(candidate.fieldId)) fail('filler_frame_results_invalid');
      localIds.add(candidate.fieldId);
      candidates.push({ route, scanId: scan.scanId, fieldId: candidate.fieldId,
        selectionId: JSON.stringify([route.instanceId, route.tabId, route.frameId, route.documentId, scan.scanId, candidate.fieldId]), candidate });
    }
  }
  return { ok: failures.length === 0 && outcomes.length > 0, partial: failures.length > 0 && outcomes.some(o => o.status === 'scanned'), fields, candidates, failures };
}

function bootstrap(bundle, resetForScan = false, context, allowBusy = false) {
  bundleCheck(bundle);
  const route = frameContext(context);
  return `const route=${literal(route)}, routeKey=JSON.stringify(route);
if(globalThis.top!==globalThis.self && !route?.allowSubframe) throw new Error('filler_top_frame_required');
if(route && route.href!==location.href) throw new Error('filler_navigation_changed');
const key=${literal(STATE_KEY)}, hash=${literal(bundle.hash)};
let state=globalThis[key];
if(state?.busy && !${allowBusy}) throw new Error('filler_operation_busy');
if(state && state.hash!==hash) throw new Error('filler_source_changed_reload_required');
if(state&&(state.href!==location.href||state.document!==document||state.routeKey!==routeKey)&&${resetForScan}){
  delete globalThis[key];
  delete globalThis.__LOCAL_RESUME_FILLER_HANDLER__;
  state=null;
}
if(!state){
  let handler=null;
  const chrome={runtime:{onMessage:{addListener(fn){handler=fn;},removeListener(fn){if(handler===fn)handler=null;}}},storage:{session:{async set(){}}}};
  ${bundle.source}
  if(typeof handler!=='function') throw new Error('filler_handler_missing');
  state={hash,routeKey,scan:null,used:false,changed:new Map(),cancelQueue:new Set(),settledOperations:new Set(),operation:null,href:location.href,document,dispatch(message){
    if(!['RESUME_SCAN','RESUME_FILL','RESUME_UNDO',...(${browserEngines.has(bundle)}?['RESUME_DISCOVER_REPEATERS','RESUME_PREPARE_SCOPED','RECRUIT_GET_PAGE_CONTEXT']:[])].includes(message.type)) throw new Error('filler_command_rejected');
    return new Promise((resolve,reject)=>{try{handler(message,{},resolve);}catch{reject(new Error('filler_execution_failed'));}});
  }};
  Object.defineProperty(globalThis,key,{value:state,configurable:true});
}
if(state.href!==location.href||state.document!==document) throw new Error('filler_navigation_changed');
if(state.routeKey!==routeKey) throw new Error('filler_frame_context_changed');
const live=()=>state.href===location.href&&state.document===document&&(globalThis.top===globalThis.self||route?.allowSubframe);
const sensitive=/password|passwd|captcha|one.time|verification|verify.code|(?:^|[^a-z])otp(?:[^a-z]|$)|sms|验证码|密码|短信/i;
const unsafe=(m)=>sensitive.test([m.key,m.label,m.controlKind].join(' '));
const find=(m)=>document.querySelector('[data-local-resume-field-id="'+CSS.escape(m.fieldId)+'"]');
const fieldContext=${fieldContext.toString()};
const phoenixSelection=${phoenixSelection.toString()};
const semantics=(el)=>JSON.stringify([el.tagName,el.type,el.id,el.name,el.getAttribute('aria-label'),el.getAttribute('placeholder'),el.getAttribute('autocomplete'),el.getAttribute('role'),el.getAttribute('data-resume-key'),el.getAttribute('accept'),el.getAttribute('data-max-bytes'),el.multiple,
 fieldContext(el),
 Array.from(el.options||[]).map(o=>[o.value,o.textContent,o.disabled]),
 Array.from(el.labels||[]).map(l=>l.textContent).join(' '),el.getAttribute('aria-labelledby'),
 (el.getAttribute('aria-labelledby')||'').split(/\\s+/).map(id=>document.getElementById(id)?.textContent||'').join(' '),el.getAttribute('aria-controls'),el.getAttribute('aria-owns')]);
const identity=(el)=>JSON.stringify([el.tagName,el.type,el.id,el.name,el.getAttribute('aria-label'),el.getAttribute('placeholder'),el.getAttribute('autocomplete'),el.getAttribute('role'),el.getAttribute('data-resume-key'),el.getAttribute('accept'),el.getAttribute('data-max-bytes'),el.multiple,
 fieldContext(el),
 Array.from(el.labels||[]).map(l=>l.textContent).join(' '),el.getAttribute('aria-labelledby'),
 (el.getAttribute('aria-labelledby')||'').split(/\\s+/).map(id=>document.getElementById(id)?.textContent||'').join(' '),el.getAttribute('aria-controls'),el.getAttribute('aria-owns')]);
const current=(el)=>JSON.stringify([phoenixSelection(el).recognized?phoenixSelection(el).value:el.getAttribute('role')==='combobox'?(el.value??el.getAttribute('aria-valuetext')??el.textContent?.trim()):el.value,
 phoenixSelection(el).recognized?el.value:null,
 el.checked,el.isContentEditable||(${browserEngines.has(bundle)}&&!('value' in el))?el.textContent:null,el.multiple?Array.from(el.selectedOptions||[]).map(o=>o.value):null,
 el.type==='file'?Array.from(el.files||[]).map(f=>[f.name,f.size,f.lastModified]):null]);
const visible=(el)=>{const style=getComputedStyle(el);return !el.closest('[hidden],[inert],[aria-hidden="true"]')&&style.display!=='none'&&style.visibility!=='hidden'&&style.visibility!=='collapse'&&style.opacity!=='0'&&el.getClientRects().length>0;};
const controlledListbox=el=>{const refs=[...(el.getAttribute('aria-controls')||'').split(/\\s+/),...(el.getAttribute('aria-owns')||'').split(/\\s+/)].filter(Boolean),ids=Array.from(new Set(refs));if(ids.length!==1)return null;const nodes=Array.from(document.querySelectorAll('[id]')).filter(node=>node.id===ids[0]);return nodes.length===1&&nodes[0].getAttribute('role')==='listbox'?nodes[0]:null;};
const safe=(m,el)=>{const combo=el?.getAttribute('role')==='combobox',comboButton=combo&&el.tagName==='BUTTON'&&el.type==='button',comboReadOnly=combo&&Boolean(controlledListbox(el));return Boolean(el&&el.isConnected&&visible(el)&&!unsafe(m)&&!sensitive.test(semantics(el))
 && (!['password','file','submit','reset','button','image','hidden'].includes((el.type||'').toLowerCase())||comboButton)
 && (!['BUTTON','A'].includes(el.tagName)||comboButton)&&(!combo&&el.getAttribute('role')==='button'?false:true)
 && !fieldContext(el).excluded&&!phoenixSelection(el).disabled&&!el.disabled&&(!el.readOnly||comboReadOnly||phoenixSelection(el).supported||(${browserEngines.has(bundle)}&&['element-cascader','element-date','element-select','ant-date','ud-date','calendar-date','ant-select','ud-select','atsx-select','feishu-month','feishu-year'].includes(m.controlKind)))&&el.getAttribute('aria-disabled')!=='true'&&m.controlKind!=='file');
};
const safeCandidate=(m,el)=>m?.controlKind==='file'&&el?.type==='file'
 ?Boolean(el.isConnected&&visible(el)&&!unsafe(m)&&!sensitive.test(semantics(el))&&!el.disabled&&!el.readOnly&&el.getAttribute('aria-disabled')!=='true')
 :safe(m,el);
const snapshot=(m,el)=>({assignment:m,element:el,identity:identity(el),semantics:semantics(el),value:current(el)});
const validate=(entry)=>Boolean(entry&&safe(entry.assignment,entry.element)&&find(entry.assignment)===entry.element
 &&entry.semantics===semantics(entry.element)&&entry.value===current(entry.element));
`;
}

function buildApplicationContextScript(bundle, context) {
  if (!browserEngines.has(bundle)) fail('filler_application_context_unavailable');
  return `(async()=>{${bootstrap(bundle, true, context)}
const result=await state.dispatch({type:'RECRUIT_GET_PAGE_CONTEXT',allowHostFallback:false});
if(!live())throw new Error('filler_navigation_changed');
if(result?.ok!==true)throw new Error('filler_application_context_failed');
if(globalThis.top!==globalThis.self){let current=globalThis;while(current!==globalThis.top){const owner=current.frameElement;if(owner){if(!owner.getClientRects().length)throw new Error('filler_frame_not_visible');for(let node=owner;node;node=node.parentElement){const style=getComputedStyle(node);if(node.hidden||node.inert||node.getAttribute('aria-hidden')==='true'||style.display==='none'||style.visibility!=='visible'||Number(style.opacity)===0)throw new Error('filler_frame_not_visible');}}current=current.parent;}}
const text=(value,max)=>typeof value==='string'?value.trim().slice(0,max):'';
const seen=new Set();
const titleCandidates=(Array.isArray(result.titleCandidates)?result.titleCandidates:[])
 .map(item=>({title:text(item?.title,512),date:text(item?.date,40),sourceStatus:text(item?.sourceStatus,100)})).filter(item=>{
   if(!item.title||seen.has(item.title))return false;seen.add(item.title);return true;
 }).slice(0,50);
return {company:text(result.companyGuess,255),titles:titleCandidates.map(item=>item.title),records:titleCandidates,url:location.href};
})()`;
}

function mergeApplicationContexts(samples, pageUrl, allowedFrameOrigins = []) {
  const expected = new URL(pageUrl);
  const allowed = new Set([expected.origin, ...allowedFrameOrigins]);
  const valid = (Array.isArray(samples) ? samples : []).filter(sample => {
    try {
      const url = new URL(sample.frameUrl);
      return Number.isSafeInteger(sample.frameId) && sample.frameId >= 0 && allowed.has(url.origin) &&
        sample.result?.url === sample.frameUrl && Array.isArray(sample.result.titles) && Array.isArray(sample.result.records);
    } catch { return false; }
  }).sort((a,b) => Number(a.frameId !== 0) - Number(b.frameId !== 0) || a.frameId - b.frameId);
  const company = valid.find(sample => sample.frameId === 0 && sample.result.company)?.result.company
    || valid.find(sample => sample.result.company)?.result.company || '';
  const records = new Map();
  for (const sample of valid) for (const row of sample.result.records.slice(0,50)) {
    if (records.size >= 50) break;
    if (!row || typeof row.title !== 'string' || !row.title.trim() || row.title.length > 512 ||
        typeof row.date !== 'string' || row.date.length > 40 || typeof row.sourceStatus !== 'string' || row.sourceStatus.length > 100) continue;
    const title = row.title.trim();
    const key = title.normalize('NFKC').replace(/\s+/g,' ').toLocaleLowerCase();
    const prior = records.get(key);
    const candidate = {title, date:row.date.trim(), sourceStatus:row.sourceStatus.trim()};
    if (!prior) records.set(key,candidate);
    else records.set(key,{title:prior.title,date:prior.date||candidate.date,sourceStatus:prior.sourceStatus||candidate.sourceStatus});
    if (records.size >= 50) break;
  }
  const merged = [...records.values()];
  return {company, titles:merged.map(row=>row.title), records:merged, url:expected.href};
}

function buildScanScript(bundle, profile, context) {
  const resume = cleanProfile(profile);
  const scanId = crypto.randomUUID();
  return `(async()=>{${bootstrap(bundle, true, context)}
if(state.upload)throw new Error('filler_upload_pending');
state.busy=true;try{
state.scan=null;
const resume=${literal(resume)};
if(${browserEngines.has(bundle)}){
 resume.customFields={...(resume.customFields||{})};
 for(const answer of resume.customAnswers||[])if((answer.origin==='*'||answer.origin===location.origin)&&(answer.pathname==='*'||answer.pathname===location.pathname)&&typeof answer.label==='string')resume.customFields[answer.label]=answer.value;
}
const result=await state.dispatch({type:'RESUME_SCAN',resume});
if(!result || result.ok!==true) throw new Error('filler_scan_failed');
if(${browserEngines.has(bundle)}){
 result.candidates=(result.missingFields||[]).map(m=>({...m,label:m.label||m.ariaLabel||m.placeholder||m.name||'',reason:'filler_answer_missing',customAnswerSupported:true}));
 result.attachments=(result.manualFields||[]).filter(m=>{
  const identity=[m.label,m.ariaLabel,m.placeholder].filter(Boolean).join(' ');
  if(/其他附件|获奖|证明|证书|成绩单|作品集|作品|材料|照片|图片|视频|音频|证件|photo|certificate/i.test(identity))return false;
  if(/导入|一键|智能解析|自动解析|自动填充|识别解析/.test(identity))return false;
  return /简历|resume|curriculum|\\bcv\\b/i.test([identity,m.section,m.context].filter(Boolean).join(' '));
 })
  .filter(m=>safeCandidate(m,find(m))).map(m=>({...m,accept:find(m).accept||'',multiple:Boolean(find(m).multiple)}));
 result.candidates.push(...(result.manualFields||[]).filter(m=>!result.attachments.some(a=>a.fieldId===m.fieldId))
  .map(m=>({...m,label:m.label||m.ariaLabel||m.placeholder||m.name||'',reason:'filler_attachment_unsupported',customAnswerSupported:false})));
 const report=await state.dispatch({type:'RESUME_DISCOVER_REPEATERS',resume});
 result.repeaters=(report.plans||[]).filter(p=>p.desiredCount<=10).map(p=>({...p,count:p.currentCount,desired:p.desiredCount,supported:true,reason:'ready'}));
}
const rawMatches=JSON.parse(JSON.stringify(result.matches||[]));
const matches=rawMatches.filter(m=>typeof m.fieldId==='string'&&safe(m,find(m)));
const rawCandidates=JSON.parse(JSON.stringify(result.candidates||[]));
const candidates=rawCandidates.filter(m=>typeof m.fieldId==='string'&&safeCandidate(m,find(m))
 &&typeof m.label==='string'&&/^filler_[a-z0-9_]{1,80}$/.test(m.reason||''))
 .map(m=>({fieldId:m.fieldId,label:m.label.slice(0,300),controlKind:String(m.controlKind||'').slice(0,32),reason:m.reason,
  customAnswerSupported:m.customAnswerSupported===true}));
if(!live()) throw new Error('filler_navigation_changed');
const attachments=${builtins.has(bundle)}?(result.attachments||[]):[];
const repeaters=${builtins.has(bundle)}?(result.repeaters||[]):[];
state.scan={id:${literal(scanId)},issuedAt:Date.now(),profile:resume,matches,entries:matches.map(m=>snapshot(m,find(m))),href:location.href,
 attachments:attachments.map(m=>snapshot(m,find(m))),repeaters:repeaters.map(r=>({...r,buttonSemantics:r.button?semantics(r.button):null}))};
const repeatPreview=repeaters.map(({sectionId,count,desired,supported,reason})=>({sectionId,count,desired,supported,reason}));
return {ok:true,totalFields:result.totalFields,emptyFields:result.emptyFields,matches,candidates,scanId:state.scan.id,issuedAt:state.scan.issuedAt,sourceHash:hash,blockedCount:rawMatches.length-matches.length+rawCandidates.length-candidates.length,topFrameOnly:!route,route,attachments,repeaters:repeatPreview,diagnostics:${builtins.has(bundle)}?(result.diagnostics||[]):[]};
}finally{state.busy=false;}
})()`;
}

function buildFillScript(bundle, request, context) {
  if (!request || request.confirmed !== true || typeof request.scanId !== 'string'
      || !/^[a-f0-9-]{36}$/.test(request.scanId) || !Array.isArray(request.fieldIds)
      || !request.fieldIds.length || request.fieldIds.length > 300
      || request.fieldIds.some(id => typeof id !== 'string' || !id.length || id.length > 200)
      || new Set(request.fieldIds).size !== request.fieldIds.length
      || Object.keys(request).some(k => !['scanId', 'fieldIds', 'confirmed'].includes(k))) fail('filler_confirmation_required');
  const operationId = issueOperationId(bundle, context);
  return `(async()=>{${bootstrap(bundle, false, context)}
const request=${literal(request)};
if(!state.scan||state.scan.id!==request.scanId||state.scan.href!==location.href) throw new Error('filler_stale_scan');
if(Date.now()-state.scan.issuedAt>120000||Date.now()<state.scan.issuedAt) throw new Error('filler_scan_expired');
const entries=request.fieldIds.map(id=>state.scan.entries.find(e=>e.assignment.fieldId===id));
if(entries.some(e=>!validate(e))) throw new Error('filler_field_changed_rescan');
const profile=state.scan.profile;
state.scan=null;state.used=true;
const operation={id:${literal(operationId)},cancelled:state.cancelQueue.delete(${literal(operationId)}),wake:null};
state.operation=operation;state.busy=true;try{
const results=[];
const deadline=Date.now()+60000;
let cancelled=false;
const optionState=entry=>{
 const requested=(Array.isArray(entry.assignment.value)?entry.assignment.value:[entry.assignment.value]).map(String);
 for(const target of requested){
  const found=Array.from(entry.element.options||[]).filter(option=>option.value===target||option.textContent.trim()===target);
  if(found.length>1)return 'filler_option_ambiguous';
  if(!found.length)return 'filler_option_missing';
  if(found[0].disabled)return 'filler_option_disabled';
 }
 return 'ready';
};
const waitForOptions=entry=>new Promise(resolve=>{
 let timer,observer,finished=false;
 const finish=result=>{if(finished)return;finished=true;observer?.disconnect();clearTimeout(timer);if(operation.wake===finish)operation.wake=null;resolve(result);};
 const check=()=>{if(operation.cancelled)return finish('cancelled');const status=optionState(entry);if(status!=='filler_option_missing')finish(status);};
 if(typeof MutationObserver!=='function')return finish('filler_option_missing');
 observer=new MutationObserver(check);
 observer.observe(document,{childList:true,subtree:true,attributes:true});
 timer=setTimeout(()=>finish(optionState(entry)),1200);
 operation.wake=()=>finish('cancelled');
 check();
});
const rebaseCascade=async entry=>{
 const element=entry.element;
 if(element.tagName!=='SELECT'||!element.isConnected||find(entry.assignment)!==element||!safe(entry.assignment,element)
  ||identity(element)!==entry.identity||current(element)!==entry.value)return {reason:'filler_field_changed_rescan'};
 let status=optionState(entry);
 if(status==='filler_option_missing')status=await waitForOptions(entry);
 if(status==='cancelled')return {cancelled:true};
 if(status!=='ready')return {reason:status};
 let rescanned;
 try{rescanned=await state.dispatch({type:'RESUME_SCAN',resume:profile});}catch{return {reason:'filler_field_changed_rescan'};}
 const match=(rescanned?.matches||[]).find(item=>item.fieldId===entry.assignment.fieldId);
 if(!match||JSON.stringify(match.value)!==JSON.stringify(entry.assignment.value)||find(match)!==element
  ||!safe(match,element)||identity(element)!==entry.identity||current(element)!==entry.value)return {reason:'filler_field_changed_rescan'};
 const refreshed=snapshot(match,element);
 status=optionState(refreshed);
 return status==='ready'?{entry:refreshed}:{reason:status};
};
const rebaseReplaced=entry=>{
 if(entry.element.isConnected)return null;
 const replacements=Array.from(document.querySelectorAll('[data-local-resume-field-id]'))
  .filter(element=>element.getAttribute('data-local-resume-field-id')===entry.assignment.fieldId);
 if(replacements.length!==1)return null;
 const element=replacements[0];
 if(!safe(entry.assignment,element)||identity(element)!==entry.identity||current(element)!==entry.value
  ||semantics(element)!==entry.semantics)return null;
 return snapshot(entry.assignment,element);
};
for(let index=0;index<entries.length;index++){
 await new Promise(resolve=>setTimeout(resolve,0));
 let entry=entries[index];
 if(!validate(entry)&&results.some(item=>item.ok)){
  const replaced=rebaseReplaced(entry);
  if(replaced)entries[index]=entry=replaced;
 }
 if(${builtins.has(bundle)}&&results.some(item=>item.ok)&&entry.element.tagName==='SELECT'
  &&(!validate(entry)||optionState(entry)==='filler_option_missing')){
  const refreshed=await rebaseCascade(entry);
  if(refreshed.cancelled||operation.cancelled){
   cancelled=true;
   results.push({fieldId:entry.assignment.fieldId,ok:false,reason:'filler_operation_cancelled',status:'skipped'});
   for(const pending of entries.slice(index+1))results.push({fieldId:pending.assignment.fieldId,ok:false,reason:'filler_operation_cancelled',status:'skipped'});
   break;
  }
  if(refreshed.reason){
   results.push({fieldId:entry.assignment.fieldId,ok:false,reason:refreshed.reason,status:'failed'});
   for(const pending of entries.slice(index+1))results.push({fieldId:pending.assignment.fieldId,ok:false,reason:'filler_field_changed_rescan',status:'skipped'});
   break;
  }
  entries[index]=entry=refreshed.entry;
 }
 const failure=operation.cancelled?'filler_operation_cancelled':Date.now()>=deadline?'filler_deadline_exceeded':!live()?'filler_navigation_changed':!validate(entry)?'filler_field_changed_rescan':null;
 if(failure){
  if(failure==='filler_operation_cancelled')cancelled=true;
  else if(!${builtins.has(bundle)})throw new Error(failure);
  if(${browserEngines.has(bundle)}&&failure==='filler_field_changed_rescan'){
   results.push({fieldId:entry.assignment.fieldId,ok:false,reason:failure,status:'skipped'});continue;
  }
  for(const pending of entries.slice(index))results.push({fieldId:pending.assignment.fieldId,ok:false,reason:failure,status:'skipped'});
  break;
 }
 const result=await state.dispatch({type:'RESUME_FILL',assignments:[entry.assignment],cancelToken:operation});
 state.changed.set(entry.assignment.fieldId,snapshot(entry.assignment,entry.element));
 if(operation.cancelled&&result?.reason==='filler_operation_cancelled'){
  cancelled=true;
  results.push({fieldId:entry.assignment.fieldId,ok:false,reason:'filler_operation_cancelled',status:'skipped'});
  for(const pending of entries.slice(index+1))results.push({fieldId:pending.assignment.fieldId,ok:false,reason:'filler_operation_cancelled',status:'skipped'});
  break;
 }
 const ok=Boolean(result&&result.ok===true&&result.filled===1);
 const detail=Array.isArray(result?.results)?result.results.find(item=>item.fieldId===entry.assignment.fieldId):null;
 const detailReason=String(detail?.reason||'');
 const reason=/没有对应选项|找不到对应.*选项|没有可选|没有找到|无法选择/.test(detailReason)?'filler_option_missing':
  /未保持|未更新|未接受|没有保存|未检测到选中/.test(detailReason)?'filler_field_not_accepted':
  /结构已经变化|重新扫描/.test(detailReason)?'filler_field_changed_rescan':
  /不支持|没有可用.*输入/.test(detailReason)?'filler_custom_control_unsupported':'filler_field_failed';
 results.push({fieldId:entry.assignment.fieldId,ok,reason:ok?'filled':result?.reason||reason,status:ok?'filled':'failed'});
}
return {ok:!cancelled&&(${builtins.has(bundle)}?results.every(r=>r.ok):true),filled:results.filter(r=>r.ok).length,failed:results.filter(r=>!r.ok&&r.status!=='skipped').map(r=>r.fieldId),skipped:results.filter(r=>r.status==='skipped').map(r=>r.fieldId),code:cancelled?'filler_fill_cancelled':results.every(r=>r.ok)?'filler_fill_complete':'filler_fill_partial',results};
}finally{
 if(state.operation===operation)state.operation=null;
 state.settledOperations.add(operation.id);while(state.settledOperations.size>32)state.settledOperations.delete(state.settledOperations.values().next().value);
 state.busy=false;
}
})()`;
}

function buildUndoScript(bundle, context) {
  return `(async()=>{${bootstrap(bundle, false, context)}
if(!state.used) throw new Error('filler_nothing_to_undo');
if(${!builtins.has(bundle) || browserEngines.has(bundle)}&&Array.from(state.changed.values()).some(e=>!validate(e))) throw new Error('filler_undo_document_changed');
state.scan=null;
state.busy=true;try{
const result=await state.dispatch({type:'RESUME_UNDO'});
if(result&&result.ok===true){state.used=false;state.changed.clear();}
return {ok:result?.ok===true,restored:Number(result?.restored)||0,failed:Array.isArray(result?.failed)?result.failed.filter(id=>typeof id==='string'&&(${builtins.has(bundle)}||state.changed.has(id))):[],code:result?.ok===true?'filler_undo_complete':'filler_undo_partial',structureRestored:false,remoteUploadsReverted:false};
}finally{state.busy=false;}
})()`;
}

function buildCancelScript(bundle, context) {
  const operationId = latestOperations.get(operationScope(bundle, context)) || null;
  return `(()=>{${bootstrap(bundle, false, context, true)}
const operationId=${literal(operationId)};
if(!operationId||state.settledOperations.has(operationId))return {ok:true,requested:false,code:'filler_operation_not_running'};
if(state.operation?.id===operationId){
 state.operation.cancelled=true;
 try{state.operation.wake?.('cancelled');}catch{}
 return {ok:true,requested:true,code:'filler_cancel_requested'};
}
state.cancelQueue.add(operationId);
while(state.cancelQueue.size>16)state.cancelQueue.delete(state.cancelQueue.values().next().value);
return {ok:true,requested:true,code:'filler_cancel_queued'};
})()`;
}

function confirmedRequest(request, keys) {
  if (!request || request.confirmed !== true || typeof request.scanId !== 'string'
      || !/^[a-f0-9-]{36}$/.test(request.scanId)
      || Object.keys(request).some(k => !['scanId', 'confirmed', ...keys].includes(k))) fail('filler_confirmation_required');
}

function builtinCheck(bundle) {
  bundleCheck(bundle);
  if (!builtins.has(bundle)) fail('filler_builtin_required');
}

function requireScan(request) {
  return `const request=${literal(request)};
if(!state.scan||state.scan.id!==request.scanId)throw new Error('filler_stale_scan');
if(Date.now()-state.scan.issuedAt>120000||Date.now()<state.scan.issuedAt)throw new Error('filler_scan_expired');`;
}

function buildBrowserPrepareScript(bundle, request, context) {
  const operationId = issueOperationId(bundle, context);
  return `(async()=>{${bootstrap(bundle, false, context)}${requireScan(request)}
const plans=request.sectionIds.map(id=>state.scan.repeaters.find(r=>r.sectionId===id));
if(plans.some(p=>!p||!p.supported||p.desiredCount>10))throw new Error('filler_repeater_unsupported');
const profile=state.scan.profile;
const initial=await state.dispatch({type:'RESUME_DISCOVER_REPEATERS',resume:profile});
for(const plan of plans){
 const fresh=(initial.plans||[]).find(p=>p.sectionId===plan.sectionId);
 if(!fresh||fresh.controlId!==plan.controlId||fresh.currentCount!==plan.currentCount||fresh.desiredCount!==plan.desiredCount
  ||initial.discoveries.filter(d=>d.sectionKey===plan.sectionKey).length!==1)throw new Error('filler_repeater_changed_rescan');
}
state.scan=null;
const operation={id:${literal(operationId)},cancelled:state.cancelQueue.delete(${literal(operationId)}),wake:null};
state.operation=operation;state.busy=true;
const results=[],deadline=Date.now()+60000;
let cancelled=false;
try{
 for(const plan of plans){
  if(operation.cancelled){cancelled=true;break;}
  if(!live())throw new Error('filler_navigation_changed');
  if(Date.now()>=deadline)break;
  const result=await state.dispatch({type:'RESUME_PREPARE_SCOPED',resume:profile,repeaterPlans:[plan],cancelToken:operation,deadline});
  if(!live())throw new Error('filler_navigation_changed');
  const report=await state.dispatch({type:'RESUME_DISCOVER_REPEATERS',resume:profile});
  const fresh=(report.discoveries||[]).find(p=>p.sectionId===plan.sectionId);
  const count=Number(fresh?.currentCount??plan.currentCount);
  results.push({sectionId:plan.sectionId,added:Math.max(0,count-plan.currentCount),count,desired:plan.desiredCount,
   code:result?.ok===true&&count>=plan.desiredCount?'ready':'filler_prepare_partial'});
  if(operation.cancelled){cancelled=true;break;}
 }
 const ok=!cancelled&&results.length===plans.length&&results.every(r=>r.code==='ready');
 return {ok,code:cancelled?'filler_prepare_cancelled':ok?'filler_prepare_complete':'filler_prepare_partial',results,requiresRescan:true,structureUndoSupported:false};
}finally{
 if(state.operation===operation)state.operation=null;
 state.settledOperations.add(operation.id);while(state.settledOperations.size>32)state.settledOperations.delete(state.settledOperations.values().next().value);
 state.busy=false;
}
})()`;
}

function buildPrepareScript(bundle, request, context) {
  builtinCheck(bundle); confirmedRequest(request, ['sectionIds']);
  if (!Array.isArray(request.sectionIds) || !request.sectionIds.length || request.sectionIds.length > 10
      || request.sectionIds.some(s => typeof s !== 'string' || !s || s.length > 100)
      || new Set(request.sectionIds).size !== request.sectionIds.length) fail('filler_repeater_request_invalid');
  if (browserEngines.has(bundle)) return buildBrowserPrepareScript(bundle, request, context);
  const operationId = issueOperationId(bundle, context);
  return `(async()=>{${bootstrap(bundle, false, context)}${requireScan(request)}
const plans=request.sectionIds.map(id=>state.scan.repeaters.find(r=>r.sectionId===id));
if(plans.some(r=>!r||!r.supported||!r.root||!r.button))throw new Error('filler_repeater_unsupported');
state.scan=null;
const operation={id:${literal(operationId)},cancelled:state.cancelQueue.delete(${literal(operationId)}),wake:null};
state.operation=operation;state.busy=true;
const results=[],deadline=Date.now()+60000;
let uncertain=false,cancelled=false;
try{
 for(const plan of plans){
  const count=()=>plan.getRecords?.()?.length ?? -1;
  let added=0,code='ready';
  if(count()!==plan.count)throw new Error('filler_repeater_changed_rescan');
  while(count()<plan.desired){
   await new Promise(resolve=>setTimeout(resolve,0));
   if(operation.cancelled){cancelled=true;code='filler_prepare_cancelled';break;}
   if(!live())throw new Error('filler_navigation_changed');
   if(Date.now()>=deadline)throw new Error('filler_deadline_exceeded');
   if(!plan.root.isConnected||!plan.button.isConnected||!plan.root.contains(plan.button)||!visible(plan.button)
    ||plan.button.disabled||plan.button.type!=='button'||semantics(plan.button)!==plan.buttonSemantics
    ||/submit|save|提交|保存/i.test(plan.button.textContent||''))throw new Error('filler_repeater_changed_rescan');
   const before=count();
   const changed=await new Promise(resolve=>{
    let timer,observer;
    const finish=v=>{
     observer?.disconnect();clearTimeout(timer);
     if(operation.wake===finish)operation.wake=null;
     resolve(v);
    };
    observer=new MutationObserver(()=>{if(count()>before)finish('changed');});
    observer.observe(plan.root,{childList:true,subtree:true});
    timer=setTimeout(()=>finish(false),Math.min(3000,deadline-Date.now()));
    operation.wake=()=>finish('cancelled');
    try{plan.button.click();if(count()>before)finish(true);}catch{finish(false);}
   });
   if(changed==='cancelled'||operation.cancelled){cancelled=true;code='filler_prepare_cancelled';added+=Math.max(0,count()-before);break;}
   if(!changed){uncertain=true;code='filler_prepare_timeout_unknown';break;}
   added+=count()-before;
   if(count()>plan.desired){code='filler_repeater_excess';break;}
  }
  results.push({sectionId:plan.sectionId,added,count:count(),desired:plan.desired,code});
  if(uncertain||cancelled)break;
 }
 return {ok:!uncertain&&!cancelled&&results.every(r=>r.code==='ready'),code:cancelled?'filler_prepare_cancelled':uncertain?'filler_prepare_partial':'filler_prepare_complete',results,requiresRescan:true,structureUndoSupported:false};
}finally{
 if(state.operation===operation)state.operation=null;
 state.settledOperations.add(operation.id);while(state.settledOperations.size>32)state.settledOperations.delete(state.settledOperations.values().next().value);
 state.busy=uncertain;
}
})()`;
}

function buildUploadScript(bundle, request, context) {
  builtinCheck(bundle); confirmedRequest(request, ['fieldId', 'attachment']);
  const a = request.attachment;
  if (typeof request.fieldId !== 'string' || !a || typeof a.id !== 'string' || !/^[\w-]{1,128}$/.test(a.id)
      || typeof a.name !== 'string' || !/^[^\\/:\x00-\x1f]{1,180}\.(pdf|doc|docx)$/i.test(a.name)
      || !Number.isSafeInteger(a.size) || a.size <= 0 || a.size > 20 * 1024 * 1024
      || !['application/pdf', 'application/msword', 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'].includes(a.mime)
      || Object.keys(a).some(k => !['id', 'name', 'size', 'mime'].includes(k))) fail('filler_attachment_invalid');
  const uploadId = crypto.randomUUID();
  return `(async()=>{${bootstrap(bundle, false, context)}${requireScan(request)}
if(state.upload)throw new Error('filler_upload_pending');
const entry=state.scan.attachments.find(e=>e.assignment.fieldId===request.fieldId),el=entry?.element;
if(!entry||!el.isConnected||find(entry.assignment)!==el||!visible(el)||el.disabled||el.type!=='file'
 ||semantics(el)!==entry.semantics||current(el)!==entry.value||unsafe(entry.assignment)||sensitive.test(semantics(el)))throw new Error('filler_attachment_changed_rescan');
const accept=(el.accept||'').toLowerCase().split(',').map(s=>s.trim()).filter(Boolean);
const extension='.'+request.attachment.name.split('.').pop().toLowerCase();
if(accept.length&&!accept.some(s=>s===extension||s===request.attachment.mime||s==='application/*'))throw new Error('filler_attachment_type_rejected');
const max=Number(el.getAttribute('data-max-bytes'));
if(max>0&&request.attachment.size>max)throw new Error('filler_attachment_size_rejected');
state.scan=null;
state.upload={id:${literal(uploadId)},entry,attachment:request.attachment,issuedAt:Date.now()};
return {ok:true,uploadId:state.upload.id,fieldId:request.fieldId,attachmentId:request.attachment.id,
 selector:'[data-local-resume-field-id="'+CSS.escape(request.fieldId)+'"]',route,expiresInMs:10000,requiresRescan:true,remoteUndoSupported:false};
})()`;
}

function buildUploadStatusScript(bundle, uploadId, context) {
  builtinCheck(bundle);
  if (typeof uploadId !== 'string' || !/^[a-f0-9-]{36}$/.test(uploadId)) fail('filler_upload_invalid');
  return `(async()=>{${bootstrap(bundle, false, context)}
const upload=state.upload;
if(!upload||upload.id!==${literal(uploadId)})throw new Error('filler_upload_invalid');
const el=upload.entry.element;
let code='filler_upload_pending';
if(!el.isConnected||find(upload.entry.assignment)!==el)code='filler_upload_dom_changed_unknown';
else if(el.getAttribute('data-resume-upload-status')==='failed')code='filler_upload_rejected';
else if(el.files?.length===1&&el.files[0].name===upload.attachment.name&&el.files[0].size===upload.attachment.size)
 code=el.getAttribute('data-resume-upload-status')==='complete'?'filler_upload_complete':
  el.getAttribute('data-resume-upload-status')==='loading'?'filler_upload_pending':'filler_upload_selected_parse_unknown';
if(code==='filler_upload_pending'&&Date.now()-upload.issuedAt>60000)code='filler_upload_timeout_unknown';
if(code!=='filler_upload_pending')state.upload=null;
return {ok:code==='filler_upload_complete',code,requiresRescan:true,remoteUndoSupported:false};
})()`;
}

function buildUploadTargetScript(bundle, uploadId, context) {
  builtinCheck(bundle);
  if (typeof uploadId !== 'string' || !/^[a-f0-9-]{36}$/.test(uploadId)) fail('filler_upload_invalid');
  return `(()=>{${bootstrap(bundle, false, context)}
const upload=state.upload;
if(!upload||upload.id!==${literal(uploadId)}||upload.claimed)throw new Error('filler_upload_invalid');
if(Date.now()-upload.issuedAt>10000||Date.now()<upload.issuedAt)throw new Error('filler_upload_expired');
const entry=upload.entry,el=entry.element;
if(!live()||!el.isConnected||find(entry.assignment)!==el||!visible(el)||el.disabled||el.type!=='file'
 ||semantics(el)!==entry.semantics||current(el)!==entry.value)throw new Error('filler_attachment_changed_rescan');
upload.claimed=true;
return el;
})()`;
}

module.exports = { SOURCE_FILES, loadLocalFiller, loadBundledFiller, loadDesktopFiller, parseProfileJson, parseLegacyProfile, buildApplicationContextScript, mergeApplicationContexts,
  buildScanScript, buildFillScript, buildUndoScript, buildCancelScript, buildPrepareScript, buildUploadScript, buildUploadStatusScript, buildUploadTargetScript, aggregateFrameScans };
