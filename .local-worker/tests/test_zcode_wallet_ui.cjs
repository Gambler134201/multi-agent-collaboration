'use strict';

// Offline presentation checks for ZCode's native entitlement progress rows.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const ROOT = path.resolve(__dirname, '..', '..');
const APP = fs.readFileSync(path.join(ROOT, '.local-worker', 'panel', 'app.js'), 'utf8');

const start = APP.indexOf('function zcodeQuotaPresentation');
const end = APP.indexOf('function renderZcodeSettings');
assert.ok(start >= 0 && end > start, 'ZCode wallet helpers must exist');
const source = APP.slice(start, end) +
  '\nmodule.exports = { zcodeQuotaPresentation, zcodeWalletPresentation };';
const context = { module: { exports: {} }, Intl, Date };
vm.createContext(context);
vm.runInContext(source, context, { filename: 'panel/app.js#zcode-wallet' });
const { zcodeQuotaPresentation, zcodeWalletPresentation } = context.module.exports;

test('flat and model-scoped native limits both become remaining-percent rows', () => {
  const endpoint = {
    model: { native_selection: { providerId: 'account:zai-start-plan', modelId: 'GLM-5.3-Flash' } },
    runtime: {
      usage: {
        'account:zai-start-plan': {
          fetched_at: 1792825230.5,
          limits: [
            { label: 'GLM-5.3-Flash', unit: 'token', remaining: 267743675, total: 300000000, used: 32256325, expires_at: Date.UTC(2026, 9, 26, 1) },
            { label: 'GLM-5.3', unit: 'token', remaining: 200000000, total: 200000000, used: 0, expires_at: Date.UTC(2026, 9, 26, 1) },
          ],
        },
      },
    },
  };
  const shown = zcodeWalletPresentation(endpoint);
  assert.equal(shown.rows.length, 2);
  assert.equal(shown.rows[0].label, 'GLM-5.3-Flash');
  assert.match(shown.rows[0].displayPercent, /^89%$/);
  assert.equal(shown.rows[0].expiry, '10月26日');
  assert.match(shown.rows[0].title, /267,743,675/);
  assert.match(shown.rows[0].title, /300,000,000/);
  assert.equal(shown.rows[1].percent, 100);
});

test('missing capacity stays unknown instead of inventing zero or 100 percent', () => {
  const missing = zcodeQuotaPresentation({ label: '限额未知', remaining: 10, unit: 'token' });
  assert.equal(missing.percent, null);
  assert.equal(missing.displayPercent, '额度未知');
  assert.match(missing.title, /总量未提供/);

  const derived = zcodeQuotaPresentation({ label: '可由用量推算', remaining: 80, used: 20, unit: 'token' });
  assert.equal(derived.percent, 80);
  assert.match(derived.title, /总量 100 token/);
});

test('values are clamped for rendering while zero capacity never becomes full', () => {
  assert.equal(zcodeQuotaPresentation({ remaining: 120, total: 100 }).percent, 100);
  assert.equal(zcodeQuotaPresentation({ remaining: -1, total: 100 }).percent, 0);
  assert.equal(zcodeQuotaPresentation({ remaining: 0, total: 0 }).percent, null);
});

test('wallet markup uses native progress elements and an accessible percentage', () => {
  assert.match(APP, /class="endpoint-wallet zcode-wallet"/);
  assert.match(APP, /<progress value="\$\{row\.percent\.toFixed\(2\)\}"/);
  assert.match(APP, /aria-label="\$\{escapeText\(row\.label\)\}剩余/);
  assert.doesNotMatch(APP, /number\(rows\[0\]\.remaining\)\+' '\+escapeText\(rows\[0\]\.unit\)/);
});
