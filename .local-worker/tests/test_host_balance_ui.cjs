'use strict';

// Offline presentation checks for the DSH host-account top bar. The real
// app.js helper block is loaded in a VM; no panel, DSH, account or model is
// started and no real balance is read.

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const ROOT = path.resolve(__dirname, '..', '..');
const APP = fs.readFileSync(path.join(ROOT, '.local-worker', 'panel', 'app.js'), 'utf8');
const HTML = fs.readFileSync(path.join(ROOT, '.local-worker', 'panel', 'index.html'), 'utf8');
const CLIENT = fs.readFileSync(path.join(ROOT, 'integrations', 'dsh-brain', 'client.mjs'), 'utf8');

const start = APP.indexOf('function hostAccountTime');
const end = APP.indexOf('function renderHostAccount');
assert.ok(start >= 0 && end > start, 'host account presentation helpers must exist');
const source = APP.slice(start, end) + '\nmodule.exports = { hostAccountPresentation };';
const context = { module: { exports: {} }, Intl, Date };
vm.createContext(context);
vm.runInContext(source, context, { filename: 'panel/app.js#host-account' });
const { hostAccountPresentation } = context.module.exports;

test('ready DSH balance shows official source, amount and update time', () => {
  const view = hostAccountPresentation({
    status: 'ready',
    fetched_at: '2026-10-18T15:30:00Z',
    wallets: [
      { kind: 'normal', currency: 'CNY', balance: '12.34' },
      { kind: 'bonus', currency: 'USD', balance: '1.00' },
    ],
  });
  assert.equal(view.title, 'DSH 登录账户余额');
  assert.equal(view.rows.length, 2);
  assert.equal(view.rows[0].label, '账户余额（人民币）');
  assert.equal(view.rows[0].value, '¥12.34');
  assert.equal(view.rows[1].label, '赠送余额（美元）');
  assert.equal(view.rows[1].value, '$1.00');
  assert.match(view.statusText, /更新/);
  assert.match(view.sourceText, /DSH 官方登录账户/);
  assert.doesNotMatch(view.sourceText, /account\.getBalance/);
  assert.match(view.sourceText, /与执行模型 API 账户分开/);
  assert.match(view.refreshNote, /只读取最近同步/);
  assert.equal(view.error, '');
});

test('unknown, signed-out, expired and error states stay unknown and clear amounts', () => {
  for (const [account, pattern] of [
    [{ status: 'signed-out' }, /未登录/],
    [{ status: 'unavailable' }, /余额暂不可用/],
    [{ status: 'expired', fetched_at: '2026-10-18T15:30:00Z', wallets: [] }, /账户状态已过期/],
    [{ status: 'error', error: '官方余额接口失败', wallets: [] }, /读取失败/],
    [{ status: 'updating' }, /正在读取/],
  ]) {
    const view = hostAccountPresentation(account);
    assert.equal(view.rows.length, 0, JSON.stringify(account));
    assert.match(view.message, pattern);
    assert.equal(view.title, 'DSH 登录账户余额');
  }
  const signedOut = hostAccountPresentation({
    status: 'signed-out',
    wallets: [{ kind: 'normal', currency: 'CNY', balance: '99.00' }],
  });
  assert.equal(signedOut.rows.length, 0, 'non-ready states never render a stale wallet');
});

test('stale ready data is labelled as last data, not current', () => {
  const view = hostAccountPresentation({
    status: 'ready',
    stale: true,
    fetched_at: '2026-10-18T15:30:00Z',
    wallets: [{ kind: 'normal', currency: 'CNY', balance: '5.00' }],
  });
  assert.match(view.statusText, /上次数据/);
  assert.equal(view.rows.length, 1);
});

test('panel HTML and client entry carry the display-only DSH context', () => {
  assert.match(HTML, /id="usage-title"/);
  assert.match(HTML, /id="usage-source"/);
  assert.match(HTML, /id="project-global-note"/);
  assert.match(APP, /const isDshHost=viewQuery\.get\('host'\)==='dsh'/);
  assert.match(APP, /function renderHostAccount\(\)/);
  assert.match(APP, /state\.host_account=await api\('\/api\/host-account'/);
  assert.match(CLIENT, /HOST_CONTEXT = 'dsh'/);
  assert.match(CLIENT, /searchParams\.set\('host', HOST_CONTEXT\)/);
  // The host query is a display choice, not an authorization secret: the panel
  // still uses its existing session credential for mutations.
  assert.doesNotMatch(CLIENT, /X-Panel-Token|fetch\(/);
});

test('Codex view still renders its quota windows rather than DSH wallet data', () => {
  const elements = new Map();
  const element = id => {
    if (!elements.has(id)) elements.set(id, { setAttribute() {} });
    return elements.get(id);
  };
  const sandbox = {
    isDshHost: false,
    state: { codex_usage: { status: 'ready', updated_at: '2026-10-18T15:30:00Z',
      buckets: [{ name: 'Codex', windows: [{ remaining_percent: 62.5, window_minutes: 300 }] }] } },
    $: element,
    setMarkup: (el, html) => { el.html = html; },
    escapeText: value => String(value),
    beijingClock: () => '23:30',
    usageWindowLabel: () => '5 小时',
    usageResetTime: () => '重置时间',
    renderHostAccount: () => { throw new Error('must not render DSH wallet in Codex'); },
  };
  const from = APP.indexOf('function renderUsage()');
  const to = APP.indexOf('// Global execution-model scope', from);
  vm.runInNewContext(APP.slice(from, to) + '\nrenderUsage();', sandbox);
  assert.equal(element('usage-title').textContent, 'GPT / Codex 额度');
  assert.match(element('usage-windows').html, /剩余 62.5%/);
  assert.match(element('usage-windows').html, /<progress value="62.5"/);
  assert.equal(element('usage-source').hidden, true);
});
