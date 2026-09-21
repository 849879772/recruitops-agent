'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const api = require('../../packages/desktop_filler/index.cjs');
const bundle = api.loadBundledFiller();

class FixtureEvent {
  constructor(type, options = {}) { this.type = type; this.bubbles = Boolean(options.bubbles); }
}

class FixtureElement {
  constructor(document, tagName, attributes = {}) {
    this.ownerDocument = document;
    this.tagName = tagName.toUpperCase();
    this.attributes = new Map(Object.entries(attributes).map(([key, value]) => [key, value === true ? '' : String(value)]));
    this.children = [];
    this.parentElement = null;
    this._value = '';
    this._checked = false;
    this._selected = false;
    this.labels = [];
    this.listeners = new Map();
    this.events = [];
    this.textContent = '';
    this.onClick = null;
  }
  getAttribute(name) { return this.attributes.has(name) ? this.attributes.get(name) : null; }
  setAttribute(name, value) { this.attributes.set(name, String(value)); this.ownerDocument.notifyMutation(this); }
  removeAttribute(name) { this.attributes.delete(name); this.ownerDocument.notifyMutation(this); }
  hasAttribute(name) { return this.attributes.has(name); }
  get type() { return this.getAttribute('type') || (this.tagName === 'INPUT' ? 'text' : this.tagName === 'BUTTON' ? 'submit' : ''); }
  get id() { return this.getAttribute('id') || ''; }
  get name() { return this.getAttribute('name') || ''; }
  get placeholder() { return this.getAttribute('placeholder') || ''; }
  get autocomplete() { return this.getAttribute('autocomplete') || ''; }
  get accept() { return this.getAttribute('accept') || ''; }
  get multiple() { return this.hasAttribute('multiple'); }
  get disabled() { return this.hasAttribute('disabled'); }
  set disabled(value) { if (value) this.setAttribute('disabled', ''); else this.removeAttribute('disabled'); }
  get readOnly() { return this.hasAttribute('readonly'); }
  get isContentEditable() { return this.getAttribute('contenteditable') === 'true'; }
  get isConnected() { return this.ownerDocument.contains(this); }
  get form() { return this.closest('form'); }
  get value() {
    if (this.tagName === 'OPTION') return this.getAttribute('value') ?? this.textContent;
    if (this.tagName === 'SELECT') return this.selectedOptions[0]?.value || '';
    return this._value;
  }
  set value(value) {
    if (this.tagName === 'OPTION') this.setAttribute('value', value);
    else this._value = String(value);
  }
  get checked() { return this._checked; }
  set checked(value) {
    this._checked = Boolean(value);
    if (this._checked && this.type === 'radio') {
      for (const other of this.ownerDocument.querySelectorAll('input[type="radio"]')) {
        if (other !== this && other.name === this.name && other.form === this.form) other._checked = false;
      }
    }
  }
  get selected() { return this._selected; }
  set selected(value) { this._selected = Boolean(value); }
  get options() { return this.children.filter(child => child.tagName === 'OPTION'); }
  get selectedOptions() { return this.options.filter(option => option.selected); }
  getClientRects() { return this.isConnected && !this.closest('[hidden],[inert],[aria-hidden="true"]') ? [{}] : []; }
  closest(selector) {
    for (let node = this; node; node = node.parentElement) {
      if (selector === '[data-resume-record]' && node.hasAttribute('data-resume-record')) return node;
      if (selector === '[data-resume-section]' && node.hasAttribute('data-resume-section')) return node;
      if (selector === 'form' && node.tagName === 'FORM') return node;
      if (selector === '[hidden],[inert],[aria-hidden="true"]'
          && (node.hasAttribute('hidden') || node.hasAttribute('inert') || node.getAttribute('aria-hidden') === 'true')) return node;
    }
    return null;
  }
  appendChild(child) {
    child.parentElement = this;
    this.children.push(child);
    this.ownerDocument.notifyMutation(this);
    return child;
  }
  removeChild(child) {
    this.children = this.children.filter(item => item !== child);
    child.parentElement = null;
    this.ownerDocument.notifyMutation(this);
    return child;
  }
  replaceChildren(...children) {
    for (const child of this.children) child.parentElement = null;
    this.children = [];
    for (const child of children) this.appendChild(child);
  }
  contains(child) { return child === this || this.children.some(item => item.contains(child)); }
  querySelectorAll(selector) {
    const direct = selector.startsWith(':scope > ');
    const query = direct ? selector.slice(':scope > '.length) : selector;
    const nodes = direct ? this.children : this.ownerDocument.descendants(this).slice(1);
    return nodes.filter(node => matchesSelector(node, query));
  }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
  addEventListener(type, listener) {
    if (!this.listeners.has(type)) this.listeners.set(type, []);
    this.listeners.get(type).push(listener);
  }
  dispatchEvent(event) {
    event.target = this;
    this.events.push(event.type);
    for (const listener of this.listeners.get(event.type) || []) listener.call(this, event);
    return true;
  }
  click() { this.onClick?.(); }
}

function matchesSelector(element, selector) {
  return selector.split(',').some(part => {
    const query = part.trim();
    const tag = query.match(/^[a-z]+/i)?.[0];
    if (tag && element.tagName !== tag.toUpperCase()) return false;
    for (const [, name, value] of query.matchAll(/\[([^\]=]+)(?:=["']?([^"'\]]*)["']?)?\]/g)) {
      if (!element.hasAttribute(name) || (value !== undefined && element.getAttribute(name) !== value)) return false;
    }
    return Boolean(tag || query.startsWith('['));
  });
}

class FixtureDocument {
  constructor() { this.roots = []; this.observers = new Set(); }
  createElement(tagName, attributes) { return new FixtureElement(this, tagName, attributes); }
  appendChild(element) { element.parentElement = null; this.roots.push(element); return element; }
  descendants(root) { return [root, ...root.children.flatMap(child => this.descendants(child))]; }
  all() { return this.roots.flatMap(root => this.descendants(root)); }
  contains(element) { return this.all().includes(element); }
  querySelectorAll(selector) { return this.all().filter(element => matchesSelector(element, selector)); }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
  getElementById(id) { return this.all().find(element => element.id === id) || null; }
  notifyMutation(target) {
    for (const observer of this.observers) {
      if (observer.target.contains(target)) queueMicrotask(() => observer.callback([{ type: 'childList' }]));
    }
  }
}

class FixtureMutationObserver {
  constructor(callback) { this.callback = callback; this.target = null; }
  observe(target) { this.target = target; (target.ownerDocument || target).observers.add(this); }
  disconnect() { (this.target?.ownerDocument || this.target)?.observers.delete(this); this.target = null; }
}

const inputPrototype = {};
Object.defineProperty(inputPrototype, 'value', { set(value) { this._value = String(value); } });
Object.defineProperty(inputPrototype, 'checked', { set(value) { this.checked = value; } });
const textareaPrototype = {};
Object.defineProperty(textareaPrototype, 'value', { set(value) { this._value = String(value); } });

function world({ href = 'https://fixture.example.test/form', subframe = false } = {}) {
  const document = new FixtureDocument();
  const location = { href };
  const context = vm.createContext({
    document, location, CSS: { escape: value => value }, Event: FixtureEvent,
    MutationObserver: FixtureMutationObserver,
    HTMLInputElement: { prototype: inputPrototype }, HTMLTextAreaElement: { prototype: textareaPrototype },
    getComputedStyle: () => ({ display: 'block', visibility: 'visible', opacity: '1' }),
    setTimeout: (callback, delay) => setTimeout(callback, Math.min(delay, 40)), clearTimeout,
  });
  vm.runInContext('globalThis.self=globalThis;globalThis.top=' + (subframe ? '{}' : 'globalThis'), context);
  return { context, document, location };
}

function field(document, { tag = 'input', type, key, value = '', name = '', id = '', labels = [], attributes = {} } = {}) {
  const attrs = { ...attributes };
  if (type) attrs.type = type;
  if (key) attrs['data-resume-key'] = key;
  if (name) attrs.name = name;
  if (id) attrs.id = id;
  const element = document.createElement(tag, attrs);
  element.value = value;
  element.labels = labels.map(textContent => ({ textContent }));
  return element;
}

function option(document, value, text, selected = false) {
  const item = document.createElement('option', { value });
  item.textContent = text;
  item.selected = selected;
  return item;
}

function ariaCombo(document, { key = 'city', id = 'city-list', choices = [], readonly = false, initiallyHidden = false } = {}) {
  const combo = field(document, { tag: 'input', key, attributes: {
    role: 'combobox', 'aria-controls': id, ...(readonly ? { readonly: true } : {}),
  } });
  const listbox = document.createElement('div', { id, role: 'listbox', ...(initiallyHidden ? { hidden: true } : {}) });
  combo.onClick = () => { if (initiallyHidden) listbox.removeAttribute('hidden'); };
  const options = choices.map(({ text, attributes = {} }) => {
    const item = document.createElement('div', { role: 'option', ...attributes });
    item.textContent = text;
    item.clicks = 0;
    item.onClick = () => {
      item.clicks++;
      for (const sibling of listbox.querySelectorAll('[role="option"]')) sibling.removeAttribute('aria-selected');
      item.setAttribute('aria-selected', 'true');
      combo.value = text;
    };
    listbox.appendChild(item);
    return item;
  });
  document.appendChild(combo);
  document.appendChild(listbox);
  return { combo, listbox, options };
}

function scanScript(worldState, profile, route) {
  return vm.runInContext(api.buildScanScript(bundle, profile, route), worldState.context);
}
function fillScript(worldState, preview, route) {
  return vm.runInContext(api.buildFillScript(bundle, {
    scanId: preview.scanId, fieldIds: preview.matches.map(match => match.fieldId), confirmed: true,
  }, route), worldState.context);
}
function cancelScript(worldState, route) {
  return vm.runInContext(api.buildCancelScript(bundle, route), worldState.context);
}
function testRoute(page, documentId) {
  return { instanceId: 'fixture', tabId: 'tab', frameId: 'main', documentId,
    profileVersion: '1', href: page.location.href };
}

test('FP-T10-P: native complex controls match exact accessible choice labels and verify writes', async () => {
  const page = world();
  const maleLabel = page.document.createElement('span', { id: 'male-choice' });
  maleLabel.textContent = '男';
  page.document.appendChild(maleLabel);
  const female = field(page.document, { type: 'radio', key: 'gender', name: 'gender', value: 'F', labels: ['女'] });
  const male = field(page.document, { type: 'radio', key: 'gender', name: 'gender', value: 'M', attributes: { 'aria-labelledby': 'male-choice' } });
  const js = field(page.document, { type: 'checkbox', key: 'skills', name: 'skills', value: 'js', labels: ['JavaScript'] });
  const sql = field(page.document, { type: 'checkbox', key: 'skills', name: 'skills', value: 'sql', attributes: { 'aria-label': 'SQL' } });
  const python = field(page.document, { type: 'checkbox', key: 'skills', name: 'skills', value: 'py', labels: ['Python'] });
  const date = field(page.document, { type: 'date', key: 'birthDate' });
  const city = field(page.document, { tag: 'select', key: 'city' });
  city.appendChild(option(page.document, 'bj', '北京'));
  city.appendChild(option(page.document, 'sh', '上海'));
  const languages = field(page.document, { tag: 'select', key: 'languages', attributes: { multiple: true } });
  languages.appendChild(option(page.document, 'en', 'English'));
  languages.appendChild(option(page.document, 'zh', '中文'));
  for (const element of [female, male, js, sql, python, date, city, languages]) page.document.appendChild(element);

  const preview = await scanScript(page, { gender: '男', skills: ['JavaScript', 'SQL'], birthDate: '2000-01-02', city: '北京', languages: ['中文', 'English'] });
  assert.equal(preview.matches.length, 7);
  assert.equal(male.checked, false);
  assert.equal((await fillScript(page, preview)).filled, 7);
  assert.equal(male.checked, true);
  assert.equal(female.checked, false);
  assert.equal(js.checked, true);
  assert.equal(sql.checked, true);
  assert.equal(python.checked, false);
  assert.equal(date.value, '2000-01-02');
  assert.equal(city.value, 'bj');
  assert.deepEqual(languages.selectedOptions.map(item => item.value), ['en', 'zh']);
  for (const element of [male, js, sql, python, date, city, languages]) assert.deepEqual(element.events, ['input', 'change']);
});

test('FP-T10-ARIA-P: combobox selects only the exact option in its unique controlled listbox and can undo', async () => {
  const page = world();
  const { combo, options } = ariaCombo(page.document, { choices: [
    { text: '北京', attributes: { 'aria-label': '北京' } }, { text: '北京大学' },
  ] });
  const unrelated = page.document.createElement('button', { type: 'button' });
  unrelated.textContent = 'Submit application';
  unrelated.clicks = 0;
  unrelated.onClick = () => unrelated.clicks++;
  page.document.appendChild(unrelated);

  const preview = await scanScript(page, { city: '北京' });
  assert.equal(preview.matches.length, 1);
  const result = await fillScript(page, preview);
  assert.equal(result.filled, 1);
  assert.equal(combo.value, '北京');
  assert.deepEqual(options.map(item => item.clicks), [1, 0]);
  assert.equal(unrelated.clicks, 0);

  const undone = await vm.runInContext(api.buildUndoScript(bundle), page.context);
  assert.equal(undone.restored, 1);
  assert.equal(combo.value, '');
});

test('FP-T10-ARIA-P: readonly input combobox opens its controlled listbox and selects an exact option', async () => {
  const page = world();
  const { combo, options } = ariaCombo(page.document, {
    readonly: true, initiallyHidden: true, choices: [{ text: '北京' }, { text: '北京大学' }],
  });
  const preview = await scanScript(page, { city: '北京' });
  assert.equal(preview.matches.length, 1);
  const result = await fillScript(page, preview);
  assert.equal(result.filled, 1);
  assert.equal(combo.value, '北京');
  assert.deepEqual(options.map(item => item.clicks), [1, 0]);

  const ordinary = world();
  ordinary.document.appendChild(field(ordinary.document, { key: 'city', attributes: { readonly: true } }));
  assert.equal((await scanScript(ordinary, { city: '北京' })).matches.length, 0);
});

for (const scenario of [
  { name: 'partial text does not match', requested: '北京', choices: [{ text: '北京海淀' }], reason: 'filler_option_missing' },
  { name: 'duplicate exact options are ambiguous', requested: '北京', choices: [{ text: '北京' }, { text: '北京' }], reason: 'filler_option_ambiguous' },
  { name: 'disabled exact option is rejected', requested: '北京', choices: [{ text: '北京', attributes: { 'aria-disabled': 'true' } }], reason: 'filler_option_disabled' },
]) {
  test('FP-T10-ARIA-N: ' + scenario.name, async () => {
    const page = world();
    const { options } = ariaCombo(page.document, { choices: scenario.choices });
    const unrelated = page.document.createElement('button', { type: 'button' });
    unrelated.clicks = 0; unrelated.onClick = () => unrelated.clicks++;
    page.document.appendChild(unrelated);
    const preview = await scanScript(page, { city: scenario.requested });
    const result = await fillScript(page, preview);
    assert.equal(result.failed.length, 1);
    assert.equal(result.results[0].reason, scenario.reason);
    assert.equal(options.reduce((sum, item) => sum + item.clicks, 0), 0);
    assert.equal(unrelated.clicks, 0);
  });
}

test('FP-T10-ARIA-N: absent or ambiguous aria-controls/owns containers remain unsupported', async () => {
  const missing = world();
  missing.document.appendChild(field(missing.document, { tag: 'input', key: 'city', attributes: {
    role: 'combobox', 'aria-controls': 'missing-list',
  } }));
  const noContainer = await scanScript(missing, { city: '北京' });
  assert.equal(noContainer.matches.length, 0);
  assert.ok(noContainer.diagnostics.some(item => item.reason === 'filler_custom_control_unsupported'));

  const duplicate = world();
  duplicate.document.appendChild(field(duplicate.document, { tag: 'input', key: 'city', attributes: {
    role: 'combobox', 'aria-owns': 'duplicate-list',
  } }));
  duplicate.document.appendChild(duplicate.document.createElement('div', { id: 'duplicate-list', role: 'listbox' }));
  duplicate.document.appendChild(duplicate.document.createElement('div', { id: 'duplicate-list', role: 'listbox' }));
  const ambiguous = await scanScript(duplicate, { city: '北京' });
  assert.equal(ambiguous.matches.length, 0);
  assert.ok(ambiguous.diagnostics.some(item => item.reason === 'filler_custom_control_unsupported'));
});

test('FP-T12-C: fill cancellation between fields keeps prior writes, undo, and same-document recovery', async () => {
  const page = world();
  const route = testRoute(page, 'cancel-active');
  const first = field(page.document, { key: 'name' });
  const second = field(page.document, { key: 'name' });
  page.document.appendChild(first); page.document.appendChild(second);
  const preview = await scanScript(page, { name: 'Synthetic' }, route);
  let changed;
  const firstChange = new Promise(resolve => { changed = resolve; });
  first.addEventListener('change', changed);

  const pending = fillScript(page, preview, route);
  await firstChange;
  assert.equal(first.value, 'Synthetic');
  assert.equal(cancelScript(page, route).code, 'filler_cancel_requested');
  const stopped = await pending;
  assert.equal(stopped.code, 'filler_fill_cancelled');
  assert.equal(stopped.filled, 1);
  assert.deepEqual([...stopped.skipped], [preview.matches[1].fieldId]);
  assert.equal(first.value, 'Synthetic');
  assert.equal(second.value, '');
  assert.equal(cancelScript(page, route).code, 'filler_operation_not_running');

  const undone = await vm.runInContext(api.buildUndoScript(bundle, route), page.context);
  assert.equal(undone.restored, 1);
  assert.equal(first.value, '');
  const refreshed = await scanScript(page, { name: 'Recovered' }, route);
  assert.equal((await fillScript(page, refreshed, route)).filled, 2, 'a later operation gets a fresh cancellation token');
});

test('FP-T12-C: cancellation queued before page-script startup is consumed only by that operation', async () => {
  const page = world();
  const route = testRoute(page, 'cancel-queued');
  const first = field(page.document, { key: 'name' });
  const second = field(page.document, { key: 'name' });
  page.document.appendChild(first); page.document.appendChild(second);
  assert.equal(cancelScript(page, route).code, 'filler_operation_not_running');

  const initial = await scanScript(page, { name: 'Existing' }, route);
  const firstFill = await vm.runInContext(api.buildFillScript(bundle, {
    scanId: initial.scanId, fieldIds: [initial.matches[0].fieldId], confirmed: true,
  }, route), page.context);
  assert.equal(firstFill.filled, 1);
  const next = await scanScript(page, { name: 'Next' }, route);
  const target = next.matches[1];
  const script = api.buildFillScript(bundle, { scanId: next.scanId, fieldIds: [target.fieldId], confirmed: true }, route);
  assert.equal(cancelScript(page, route).code, 'filler_cancel_queued');
  const stopped = await vm.runInContext(script, page.context);
  assert.equal(stopped.code, 'filler_fill_cancelled');
  assert.equal(stopped.filled, 0);
  assert.equal(first.value, 'Existing');
  assert.equal(second.value, '');
  assert.equal(cancelScript(page, route).code, 'filler_operation_not_running');

  const undone = await vm.runInContext(api.buildUndoScript(bundle, route), page.context);
  assert.equal(undone.restored, 1);
  assert.equal(first.value, '');
  const recovered = await scanScript(page, { name: 'Fresh' }, route);
  assert.equal((await fillScript(page, recovered, route)).filled, 2);
});

test('FP-T12-C: prepare cancellation wakes its in-flight observer and allows same-document rescan', async () => {
  const page = world();
  const route = testRoute(page, 'prepare-cancel');
  const section = page.document.createElement('section', { 'data-resume-section': 'education' });
  addEducationRecord(page.document, section);
  const button = page.document.createElement('button', { type: 'button', 'data-resume-add': true });
  let clicked;
  const clickStarted = new Promise(resolve => { clicked = resolve; });
  button.onClick = () => { addEducationRecord(page.document, section); clicked(); };
  section.appendChild(button);
  page.document.appendChild(section);
  const preview = await scanScript(page, { education: [{ school: 'A' }, { school: 'B' }] }, route);
  const pending = vm.runInContext(api.buildPrepareScript(bundle, {
    scanId: preview.scanId, sectionIds: ['education'], confirmed: true,
  }, route), page.context);
  await clickStarted;
  assert.equal(cancelScript(page, route).code, 'filler_cancel_requested');
  const stopped = await pending;
  assert.equal(stopped.code, 'filler_prepare_cancelled');
  assert.equal(stopped.results[0].code, 'filler_prepare_cancelled');
  assert.equal(stopped.results[0].added, 1, 'a row already created by the page is not removed');
  assert.equal(section.querySelectorAll('[data-resume-record]').length, 2);
  assert.equal(stopped.requiresRescan, true);
  assert.equal((await scanScript(page, { education: [{ school: 'A' }, { school: 'B' }] }, route)).ok, true);
});

test('FP-T10-N: custom comboboxes stay unsupported and cascading options are waited for and rescanned', async () => {
  const widget = world();
  const combo = field(widget.document, { tag: 'div', key: 'city', attributes: { role: 'combobox' } });
  widget.document.appendChild(combo);
  const unsupported = await scanScript(widget, { city: '北京' });
  assert.equal(unsupported.matches.length, 0);
  assert.ok(unsupported.diagnostics.some(item => item.reason === 'filler_custom_control_unsupported'));

  const page = world();
  const region = field(page.document, { tag: 'select', key: 'region' });
  region.appendChild(option(page.document, 'north', 'North'));
  const city = field(page.document, { tag: 'select', key: 'city' });
  city.appendChild(option(page.document, 'old', 'Old option'));
  region.addEventListener('change', () => city.replaceChildren(option(page.document, 'north-city', 'North City')));
  page.document.appendChild(region);
  page.document.appendChild(city);
  const preview = await scanScript(page, { region: 'north', city: 'north-city' });
  const result = await fillScript(page, preview);
  assert.equal(result.code, 'filler_fill_complete');
  assert.equal(result.filled, 2);
  assert.equal(city.value, 'north-city');

  const delayed = world();
  const delayedRegion = field(delayed.document, { tag: 'select', key: 'region' });
  delayedRegion.appendChild(option(delayed.document, 'north', 'North'));
  const delayedCity = field(delayed.document, { tag: 'select', key: 'city' });
  delayedCity.appendChild(option(delayed.document, 'old', 'Old option'));
  delayedRegion.addEventListener('change', () => setTimeout(() => {
    delayedCity.replaceChildren(option(delayed.document, 'north-city', 'North City'));
  }, 8));
  delayed.document.appendChild(delayedRegion);
  delayed.document.appendChild(delayedCity);
  const delayedPreview = await scanScript(delayed, { region: 'north', city: 'north-city' });
  const delayedResult = await fillScript(delayed, delayedPreview);
  assert.equal(delayedResult.code, 'filler_fill_complete');
  assert.equal(delayedResult.filled, 2);
  assert.equal(delayedCity.value, 'north-city');
});

function addEducationRecord(document, section) {
  const record = document.createElement('div', { 'data-resume-record': true });
  record.appendChild(field(document, { key: 'school', name: 'school' }));
  section.appendChild(record);
  return record;
}

function addSemanticRecord(document, section, keys) {
  const record = document.createElement('div');
  for (const key of keys) record.appendChild(field(document, { name: key }));
  section.appendChild(record);
  return record;
}

test('FP-T11-SEMANTIC-P: education, work, and project repeaters use named containers without resume markers', async () => {
  const sections = [
    { id: 'education', title: '教育经历', add: '添加教育经历', keys: ['school', 'major'], rows: [
      { school: '甲校', major: '计算机' }, { school: '乙校', major: '电子' },
    ] },
    { id: 'workExperience', title: '工作经历', add: '新增工作经历', keys: ['company', 'title'], rows: [
      { company: '甲公司', title: '工程师' }, { company: '乙公司', title: '研究员' },
    ] },
    { id: 'projects', title: '项目经历', add: '添加项目经历', keys: ['name', 'description'], rows: [
      { name: '项目甲', description: '检索' }, { name: '项目乙', description: '推荐' },
    ] },
  ];

  for (const item of sections) {
    const page = world();
    const root = page.document.createElement('section');
    const heading = page.document.createElement('h2');
    heading.textContent = item.title;
    root.appendChild(heading);
    addSemanticRecord(page.document, root, item.keys);
    const button = page.document.createElement('button', { type: 'button' });
    button.textContent = item.add;
    button.onClick = () => setTimeout(() => addSemanticRecord(page.document, root, item.keys), 2);
    root.appendChild(button);
    page.document.appendChild(root);
    assert.equal(root.hasAttribute('data-resume-section'), false);
    assert.equal(root.querySelector('[data-resume-record]'), null);

    const profile = { [item.id]: item.rows };
    const preview = await scanScript(page, profile);
    assert.deepEqual(JSON.parse(JSON.stringify(preview.repeaters.map(r => [r.count, r.desired, r.supported]))), [[1, 2, true]]);
    const prepared = await vm.runInContext(api.buildPrepareScript(bundle, {
      scanId: preview.scanId, sectionIds: [item.id], confirmed: true,
    }), page.context);
    assert.equal(prepared.results[0].added, 1);
    const refreshed = await scanScript(page, profile);
    assert.equal(refreshed.matches.length, 4);
    assert.equal((await fillScript(page, refreshed)).filled, 4);
    assert.deepEqual(root.querySelectorAll('input').map(el => el.value), item.rows.flatMap(row => item.keys.map(key => row[key])));
  }
});

test('FP-T11-SEMANTIC-N: ambiguous sections and multiple add buttons remain unsupported', async () => {
  const page = world();
  for (let index = 0; index < 2; index++) {
    const root = page.document.createElement('section');
    const heading = page.document.createElement('h2');
    heading.textContent = '教育经历';
    root.appendChild(heading);
    addSemanticRecord(page.document, root, ['school', 'major']);
    const button = page.document.createElement('button', { type: 'button' });
    button.textContent = '添加教育经历';
    root.appendChild(button);
    page.document.appendChild(root);
  }
  const preview = await scanScript(page, { education: [{ school: '甲', major: '一' }] });
  assert.equal(preview.repeaters[0].supported, false);
  await assert.rejects(vm.runInContext(api.buildPrepareScript(bundle, {
    scanId: preview.scanId, sectionIds: ['education'], confirmed: true,
  }), page.context), /filler_repeater_unsupported/);
  assert.equal(page.document.querySelectorAll('input').length, 4);
});

test('FP-T11-P/R: explicit async prepare adds only missing records and pairs three profile entries', async () => {
  const page = world();
  const section = page.document.createElement('section', { 'data-resume-section': 'education' });
  addEducationRecord(page.document, section);
  const button = page.document.createElement('button', { type: 'button', 'data-resume-add': true });
  button.textContent = 'Add education';
  button.onClick = () => setTimeout(() => addEducationRecord(page.document, section), 2);
  section.appendChild(button);
  page.document.appendChild(section);
  const profile = { education: [{ school: 'School A' }, { school: 'School B' }, { school: 'School C' }] };

  const first = await scanScript(page, profile);
  assert.equal(page.document.querySelectorAll('input').length, 1, 'scan must not add rows');
  const prepared = await vm.runInContext(api.buildPrepareScript(bundle, {
    scanId: first.scanId, sectionIds: ['education'], confirmed: true,
  }), page.context);
  assert.equal(prepared.results[0].added, 2);
  assert.equal(prepared.structureUndoSupported, false);

  const second = await scanScript(page, profile);
  assert.equal((await fillScript(page, second)).filled, 3);
  assert.deepEqual(page.document.querySelectorAll('input').map(element => element.value), ['School A', 'School B', 'School C']);
  const third = await scanScript(page, profile);
  const repeated = await vm.runInContext(api.buildPrepareScript(bundle, {
    scanId: third.scanId, sectionIds: ['education'], confirmed: true,
  }), page.context);
  assert.equal(repeated.results[0].added, 0);
});

test('FP-T11-N: ambiguous repeaters reject and no-progress completion remains unknown', async () => {
  const unsupported = world();
  const section = unsupported.document.createElement('section', { 'data-resume-section': 'education' });
  addEducationRecord(unsupported.document, section);
  section.appendChild(unsupported.document.createElement('button', { type: 'submit', 'data-resume-add': true }));
  unsupported.document.appendChild(section);
  const preview = await scanScript(unsupported, { education: [{ school: 'A' }, { school: 'B' }] });
  await assert.rejects(vm.runInContext(api.buildPrepareScript(bundle, {
    scanId: preview.scanId, sectionIds: ['education'], confirmed: true,
  }), unsupported.context), /filler_repeater_unsupported/);
  assert.equal(unsupported.document.querySelectorAll('input').length, 1);

  const stalled = world();
  const root = stalled.document.createElement('section', { 'data-resume-section': 'education' });
  addEducationRecord(stalled.document, root);
  root.appendChild(stalled.document.createElement('button', { type: 'button', 'data-resume-add': true }));
  stalled.document.appendChild(root);
  const pending = await scanScript(stalled, { education: [{ school: 'A' }, { school: 'B' }] });
  const result = await vm.runInContext(api.buildPrepareScript(bundle, {
    scanId: pending.scanId, sectionIds: ['education'], confirmed: true,
  }), stalled.context);
  assert.equal(result.results[0].code, 'filler_prepare_timeout_unknown');
  await assert.rejects(scanScript(stalled, { education: [] }), /filler_operation_busy/);
});

test('FP-T09-P/N/R: frame-local scans keep identical IDs separate and retain rejected-frame evidence', async () => {
  const hrefTop = 'https://top.fixture.example/form';
  const hrefChild = 'https://child.fixture.example/form';
  const top = world({ href: hrefTop });
  const child = world({ href: hrefChild, subframe: true });
  top.document.appendChild(field(top.document, { key: 'fullName', name: 'fullName' }));
  child.document.appendChild(field(child.document, { key: 'fullName', name: 'fullName' }));
  const topRoute = { instanceId: 'anon', tabId: 'tab', frameId: 'top', documentId: 'doc-top', profileVersion: '1', href: hrefTop };
  const childRoute = { ...topRoute, frameId: 'child', documentId: 'doc-child', href: hrefChild, allowSubframe: true };
  const topScan = await scanScript(top, { fullName: 'Top Candidate' }, topRoute);
  const childScan = await scanScript(child, { fullName: 'Child Candidate' }, childRoute);
  assert.equal(topScan.matches[0].fieldId, childScan.matches[0].fieldId);
  await assert.rejects(scanScript(child, { fullName: 'Denied' }, { ...childRoute, allowSubframe: false }), /filler_top_frame_required/);
  await assert.rejects(fillScript(child, childScan, { ...childRoute, frameId: 'other' }), /filler_frame_context_changed/);

  const restrictedRoute = { ...topRoute, frameId: 'restricted', documentId: 'doc-restricted', href: 'https://restricted.fixture.example/form', allowSubframe: true };
  const aggregate = api.aggregateFrameScans([
    { route: topRoute, status: 'scanned', scan: topScan },
    { route: childRoute, status: 'scanned', scan: childScan },
    { route: restrictedRoute, status: 'sandboxed' },
  ]);
  assert.equal(aggregate.ok, false);
  assert.equal(aggregate.partial, true);
  assert.equal(aggregate.failures[0].code, 'filler_frame_sandboxed');
  assert.notEqual(aggregate.fields[0].selectionId, aggregate.fields[1].selectionId);

  assert.equal((await fillScript(top, topScan, topRoute)).filled, 1);
  assert.equal((await fillScript(child, childScan, childRoute)).filled, 1);
  assert.equal(top.document.querySelector('input').value, 'Top Candidate');
  assert.equal(child.document.querySelector('input').value, 'Child Candidate');
  child.location.href += '?new-document';
  await assert.rejects(fillScript(child, childScan, childRoute), /filler_navigation_changed/);
});

test('FP-T13-P/R: visible Beisen-style controls surface exact unmapped fields without resume markers', async () => {
  const page = world({ href: 'https://beisen.fixture.example/form' });
  const pageUrl = new URL(page.location.href);
  page.location.origin = pageUrl.origin;
  page.location.pathname = pageUrl.pathname;
  const fullName = field(page.document, { name: 'fullName', id: 'full-name', labels: ['姓名'] });
  const email = field(page.document, { name: 'email', id: 'email', labels: ['邮箱'] });
  const site = field(page.document, { tag: 'select', name: 'interviewSite', id: 'interview-site', labels: ['面试站点'] });
  site.appendChild(option(page.document, 'beijing', '北京'));
  site.appendChild(option(page.document, 'shanghai', '上海'));
  const recommendation = field(page.document, { name: 'recommendationCode', id: 'recommendation-code', labels: ['推荐码'] });
  const graduate = field(page.document, { tag: 'select', name: 'freshGraduate', id: 'fresh-graduate', labels: ['应届生'] });
  graduate.appendChild(option(page.document, 'yes', '是'));
  graduate.appendChild(option(page.document, 'no', '否'));
  const photo = field(page.document, { type: 'file', name: 'photo', id: 'photo', labels: ['照片'] });
  const password = field(page.document, { type: 'password', name: 'password', labels: ['密码'] });
  for (const element of [fullName, email, site, recommendation, graduate, photo, password]) page.document.appendChild(element);

  const route = testRoute(page, 'beisen-controls');
  const profile = { basic: { fullName: 'Synthetic Candidate', email: 'candidate@example.test' } };
  const preview = await scanScript(page, profile, route);
  assert.equal(preview.totalFields, 6);
  assert.equal(preview.matches.length, 2);
  assert.equal(preview.candidates.length, 4);
  assert.equal(Array.from(preview.attachments).length, 0);
  assert.deepEqual(Array.from(preview.candidates, item => item.label.match(/面试站点|推荐码|应届生|照片/)?.[0]),
    ['面试站点', '推荐码', '应届生', '照片']);
  assert.deepEqual(Array.from(preview.candidates, item => item.customAnswerSupported), [true, true, true, false]);
  assert.ok(preview.candidates.every(item => !Object.hasOwn(item, 'value')));
  assert.equal(preview.candidates.at(-1).reason, 'filler_attachment_unsupported');
  assert.equal([...preview.matches, ...preview.candidates].some(item => /密码/.test(item.label)), false,
    'password is excluded before field extraction');

  const values = ['上海', 'SYNTHETIC-REF', '是'];
  const customAnswers = Array.from(preview.candidates).slice(0, 3).map((item, index) => ({
    id: `beisen-${index}`, origin: new URL(page.location.href).origin, pathname: '/form', label: item.label, value: values[index],
  }));
  const mapped = await scanScript(page, { ...profile, customAnswers }, route);
  assert.equal(mapped.matches.length, 5, JSON.stringify({
    answers: customAnswers.map(item => item.label),
    candidates: Array.from(mapped.candidates, item => ({ label: item.label, reason: item.reason })),
    diagnostics: Array.from(mapped.diagnostics),
  }));
  assert.equal(mapped.candidates.length, 1);
  const filled = await fillScript(page, mapped, route);
  assert.equal(filled.code, 'filler_fill_complete');
  assert.equal(filled.filled, 5);
  assert.equal(fullName.value, 'Synthetic Candidate');
  assert.equal(email.value, 'candidate@example.test');
  assert.equal(site.value, 'shanghai');
  assert.equal(recommendation.value, 'SYNTHETIC-REF');
  assert.equal(graduate.value, 'yes');
  assert.equal(photo.files, undefined, 'photo is listed as unsupported and never assigned');
  assert.equal(password.value, '');
});
