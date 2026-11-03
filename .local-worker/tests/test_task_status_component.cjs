'use strict';
// Offline checks for the read-only task status display component.
// Node built-ins only: no browser, no HTTP, no panel startup, no model work.
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const componentPath = path.resolve(__dirname, '..', 'panel', 'task-status.js');
const cssPath = path.resolve(__dirname, '..', 'panel', 'task-status.css');
const api = require(componentPath);

const BASE = {
  schema: 1,
  source: 'controller_projection',
  code: 'awaiting_review',
  label: '等待独立验收',
  tone: 'warning',
  detail: '执行器已结束，但主脑尚未确认成果符合要求。',
  next_step: '主脑检查实际文件和测试后记录验收。',
  raw_status: 'completed',
  facts: [
    { label: '执行', value: '执行结束' },
    { label: '验收', value: '尚未独立验收' },
    { label: '收尾', value: '已确认' },
    { label: '集成', value: '未记录集成' }
  ]
};

const clone = (v) => JSON.parse(JSON.stringify(v));
const variant = (patch) => Object.assign(clone(BASE), patch);
const count = (hay, needle) => hay.split(needle).length - 1;

test('module surface: valid/render/badge exported per contract', () => {
  assert.equal(typeof api.valid, 'function');
  assert.equal(typeof api.render, 'function');
  assert.equal(typeof api.badge, 'function');
  assert.equal(api, globalThis.ModelDeskTaskStatus);
  assert.equal(typeof api.valid(clone(BASE)), 'boolean');
  assert.equal(api.valid(clone(BASE)), true);
});

test('valid(): strict schema, source, code and label rules', () => {
  assert.equal(api.valid(null), false);
  assert.equal(api.valid(undefined), false);
  assert.equal(api.valid(42), false);
  assert.equal(api.valid('summary'), false);
  assert.equal(api.valid([]), false);
  assert.equal(api.valid({}), false);
  assert.equal(api.valid(variant({ schema: '1' })), false, 'schema must be number 1');
  assert.equal(api.valid(variant({ schema: 2 })), false);
  assert.equal(api.valid(variant({ schema: NaN })), false);
  assert.equal(api.valid(variant({ schema: undefined })), false);
  assert.equal(api.valid(variant({ source: 'controller_projection ' })), false);
  assert.equal(api.valid(variant({ source: 'frontend_guess' })), false);
  for (const code of ['', ' ', 42, null, 'has space', '状态', '<script>', 'a'.repeat(65), '-lead']) {
    assert.equal(api.valid(variant({ code })), false, 'code: ' + String(code));
  }
  for (const label of ['', '   ', 42, null, {}, []]) {
    assert.equal(api.valid(variant({ label })), false, 'label: ' + String(label));
  }
  assert.equal(api.valid(variant({ code: 'Integration.Accepted-1' })), true);
});

test('valid(): never throws on hostile shapes', () => {
  const weird = [
    Symbol('x'),
    () => {},
    new Map(),
    new Set(),
    { schema: { valueOf: () => 1 } },
    { get schema() { throw new Error('boom'); } }
  ];
  for (const w of weird) {
    assert.equal(typeof api.valid(w), 'boolean');
    assert.equal(api.render(w), '');
    assert.equal(api.badge(w), '');
  }
});

test('render(): contract example shows label, detail, next step and four facts', () => {
  const out = api.render(clone(BASE));
  assert.equal(typeof out, 'string');
  assert.ok(out.length > 0);
  assert.ok(out.includes('role="status"'));
  assert.ok(out.includes('aria-label="任务当前进度"'));
  assert.ok(out.includes('data-status-code="awaiting_review"'));
  assert.ok(out.includes('data-tone="warning"'));
  assert.ok(out.includes('data-raw-status="completed"'));
  assert.ok(out.includes('md-task-status--warning'));
  assert.ok(out.includes('等待独立验收'));
  assert.ok(out.includes('执行器已结束，但主脑尚未确认成果符合要求。'));
  assert.ok(out.includes('下一步'));
  assert.ok(out.includes('主脑检查实际文件和测试后记录验收。'));
  assert.equal(count(out, 'md-task-status__fact-key'), 4);
  for (const fact of BASE.facts) {
    assert.ok(out.includes('>' + fact.label + '</span>'));
    assert.ok(out.includes('>' + fact.value + '</span>'));
  }
});

test('render(): no data or bad types degrade to empty string', () => {
  for (const bad of [null, undefined, 0, '', [], {}, new Date()]) {
    assert.equal(api.render(bad), '');
  }
  assert.equal(api.render(variant({ schema: '1' })), '');
  assert.equal(api.render(variant({ source: 'controller' })), '');
  assert.equal(api.render(variant({ code: 'bad code' })), '');
  assert.equal(api.render(variant({ label: '  ' })), '');
});

test('render(): minimal summary renders only the status label', () => {
  const out = api.render({
    schema: 1,
    source: 'controller_projection',
    code: 'ready',
    label: '可以安排执行'
  });
  assert.ok(out.includes('可以安排执行'));
  assert.ok(out.includes('md-task-status--neutral'));
  assert.ok(!out.includes('md-task-status__detail'));
  assert.ok(!out.includes('<ul'));
  assert.ok(!out.includes('下一步'));
  assert.ok(!out.includes('data-raw-status'));
});

test('render(): tone whitelist downgrades unknown values to neutral', () => {
  for (const tone of ['critical', 'WARNING', '', 'ok', 3, null, 'danger"><script>']) {
    const out = api.render(variant({ tone }));
    assert.ok(out.includes('md-task-status--neutral'), 'tone: ' + String(tone));
    assert.ok(out.includes('data-tone="neutral"'), 'tone: ' + String(tone));
    if (String(tone)) {
      assert.ok(!out.includes('md-task-status--' + tone), 'raw tone leaked: ' + String(tone));
    }
    assert.ok(!out.includes('<script>'));
  }
});

test('render(): hostile text is escaped everywhere, never executable markup', () => {
  const out = api.render(variant({
    label: '<img src=x onerror=alert(1)>',
    detail: '</div><script>alert("xss")</script>',
    next_step: '<a href="javascript:alert(1)">点我</a> & \'quote\' `bt` = /slash/',
    facts: [{ label: '"onmouseover="oops', value: '<b>粗体</b>&amp;' }]
  }));
  assert.ok(out.includes('&lt;img src&#61;x onerror&#61;alert(1)&gt;'));
  assert.ok(out.includes('&lt;&#47;div&gt;&lt;script&gt;alert(&quot;xss&quot;)&lt;&#47;script&gt;'));
  assert.ok(out.includes('&lt;a href&#61;&quot;javascript:alert(1)&quot;&gt;点我&lt;&#47;a&gt;'));
  assert.ok(out.includes('&quot;onmouseover&#61;&quot;oops'));
  assert.ok(out.includes('&lt;b&gt;粗体&lt;&#47;b&gt;&amp;amp;'));
  assert.ok(!out.includes('<img'));
  assert.ok(!out.includes('<script'));
  assert.ok(!out.includes('onerror='));
  assert.ok(!out.includes('</div><script'));
  assert.ok(!out.includes('href="'));
  // 固定无障碍标签不随输入变化
  assert.ok(out.includes('aria-label="任务当前进度"'));
});

test('render(): control characters stripped, whitespace collapsed', () => {
  const out = api.render(variant({
    label: '  多  空格  标签  ',
    detail: '  行一\n\n  行二 \t 行三 \u0000\u0007 ',
    next_step: '先核对\u2028再决定'
  }));
  assert.ok(out.includes('多 空格 标签'));
  assert.ok(out.includes('行一 行二 行三'));
  assert.ok(out.includes('先核对 再决定'));
  assert.ok(!out.includes('\u0000'));
  assert.ok(!out.includes('\u0007'));
  assert.ok(!out.includes('\n'));
  assert.ok(!out.includes('\u2028'));
});

test('render(): facts keep order, skip malformed rows and cap at four', () => {
  const out = api.render(variant({
    facts: [
      { label: '执行', value: '执行结束' },
      'not-an-object',
      { label: '验收', value: '尚未独立验收' },
      { label: '' },
      { label: '收尾', value: '已确认' },
      { label: '集成', value: '未记录集成' },
      { label: '多余', value: '不应出现' },
      42
    ]
  }));
  assert.equal(count(out, 'md-task-status__fact-key'), 4);
  assert.ok(out.includes('>执行</span>'));
  assert.ok(out.includes('>验收</span>'));
  assert.ok(out.includes('>收尾</span>'));
  assert.ok(out.includes('>集成</span>'));
  assert.ok(!out.includes('多余'));
  assert.ok(!api.render(variant({ facts: 'none' })).includes('<ul'));
  const emptyValue = api.render(variant({ facts: [{ label: '执行', value: '   ' }] }));
  assert.ok(!emptyValue.includes('md-task-status__fact'));
});

test('render(): huge fields truncated with a visible ellipsis, conclusion preserved', () => {
  const longLabel = '已'.repeat(5000);
  const longText = '详'.repeat(20000);
  const longFact = '值'.repeat(9000);
  const out = api.render(variant({
    label: longLabel,
    detail: longText,
    next_step: longText,
    facts: [{ label: '执行', value: longFact }]
  }));
  assert.ok(out.includes('…'));
  assert.ok(out.length < 4000, 'rendered length ' + out.length);
  assert.ok(!out.includes('已'.repeat(81)));
  assert.ok(!out.includes('详'.repeat(401)));
  assert.ok(!out.includes('值'.repeat(161)));

  // 截断不改变状态结论：tone/code 仍按后台摘要原样呈现
  const accepted = api.render(variant({
    code: 'accepted',
    label: '已独立验收',
    tone: 'success',
    detail: longText
  }));
  assert.ok(accepted.includes('md-task-status--success'));
  assert.ok(accepted.includes('data-status-code="accepted"'));
  assert.ok(accepted.includes('已独立验收'));
});

test('render(): control-only label degrades to empty string', () => {
  assert.equal(api.render(variant({ label: '\u0001\u0002' })), '');
  assert.equal(api.badge(variant({ label: '\u0001\u0002' })), '');
});

test('badge(): small label with same strict validation', () => {
  const out = api.badge(clone(BASE));
  assert.ok(out.startsWith('<span class="md-task-badge md-task-badge--warning"'));
  assert.ok(out.includes('data-status-code="awaiting_review"'));
  assert.ok(out.includes('title="等待独立验收"'));
  assert.ok(out.includes('>等待独立验收</span>'));
  assert.ok(!out.includes('下一步'));
  assert.ok(!out.includes('md-task-status__'));
  for (const bad of [null, {}, variant({ schema: '1' }), variant({ code: 'x y' })]) {
    assert.equal(api.badge(bad), '');
  }
  assert.ok(api.badge(variant({ tone: 'mystery' })).includes('md-task-badge--neutral'));
  const long = api.badge(variant({ label: '长'.repeat(60) }));
  const shown = long.match(/>([^<]*)<\/span>$/)[1];
  assert.ok(shown.includes('…'));
  assert.ok(shown.length <= 25);
  assert.ok(!shown.includes('长'.repeat(25)));
});

test('render()/badge() are pure: no input mutation, stable output, hostile getters safe', () => {
  const input = clone(BASE);
  const before = JSON.stringify(input);
  const first = api.render(input);
  const second = api.render(input);
  const badgeHtml = api.badge(input);
  assert.equal(first, second);
  assert.ok(badgeHtml.length > 0);
  assert.equal(JSON.stringify(input), before);

  const frozen = Object.freeze(clone(BASE));
  assert.ok(api.render(frozen).length > 0);

  const throwingLabel = {};
  Object.defineProperty(throwingLabel, 'schema', { value: 1, enumerable: true });
  Object.defineProperty(throwingLabel, 'source', { value: 'controller_projection', enumerable: true });
  Object.defineProperty(throwingLabel, 'code', { value: 'running', enumerable: true });
  Object.defineProperty(throwingLabel, 'label', { get() { throw new Error('boom'); }, enumerable: true });
  assert.equal(api.valid(throwingLabel), false);
  assert.equal(api.render(throwingLabel), '');
  assert.equal(api.badge(throwingLabel), '');

  const hostileDetail = clone(BASE);
  Object.defineProperty(hostileDetail, 'detail', { get() { throw new Error('boom'); }, enumerable: true });
  assert.equal(api.render(hostileDetail), '');
});

test('component source stays offline and free of page side effects', () => {
  const src = fs.readFileSync(componentPath, 'utf8');
  const banned = [
    'XMLHttpRequest', 'WebSocket', 'EventSource', 'fetch(', 'sendBeacon',
    'innerHTML', 'outerHTML', 'insertAdjacentHTML', 'document.write', 'createElement',
    'addEventListener', 'attachEvent', 'dispatchEvent',
    'eval(', 'new Function', 'setTimeout', 'setInterval',
    'localStorage', 'sessionStorage', 'indexedDB', 'require(', 'import('
  ];
  for (const token of banned) {
    assert.ok(!src.includes(token), 'component must not use: ' + token);
  }
  assert.ok(src.includes('aria-label="任务当前进度"'));
  assert.ok(src.includes('data-status-code='));
});

test('component style: prefix isolated, theme aware, narrow-width safe', () => {
  const css = fs.readFileSync(cssPath, 'utf8');
  assert.ok(css.includes('.md-task-status'));
  assert.ok(css.includes('.md-task-badge'));

  // 只检查样式规则里的类名；注释中的文件名（如 .css）不参与
  const cssBody = css.replace(/\/\*[\s\S]*?\*\//g, '');
  const classNames = [...cssBody.matchAll(/\.([A-Za-z][A-Za-z0-9_-]*)/g)].map((m) => m[1]);
  assert.ok(classNames.length > 10);
  for (const name of classNames) {
    assert.ok(name.startsWith('md-task-'), 'class outside prefix: ' + name);
  }

  const flat = css.replace(/\s+/g, ' ').toLowerCase();
  for (const banned of ['url(', '@import', 'expression(', 'behavior:', 'position:fixed', 'position: fixed', '@charset']) {
    assert.ok(!flat.includes(banned), 'style must not use: ' + banned);
  }

  assert.ok(css.includes('[data-theme="light"]'));
  assert.ok(css.includes('prefers-color-scheme'));
  for (const hostVar of ['var(--ink,', 'var(--green,', 'var(--orange,', 'var(--red,']) {
    assert.ok(css.includes(hostVar), 'missing host variable: ' + hostVar);
  }

  assert.ok(css.includes('container-type: inline-size'));
  assert.ok(css.includes('container-name: md-task-status'));
  assert.ok(/@container md-task-status \(max-width: \d+em\)/.test(css));
  assert.ok(/@media \(max-width: \d+em\)/.test(css));
  assert.ok(count(css, 'overflow-wrap: anywhere') >= 3);
  assert.ok(css.includes('flex-wrap: wrap'));
  assert.ok(count(css, 'min-width: 0') >= 2);
  assert.ok(count(css, 'max-width: 100%') >= 1);
});
