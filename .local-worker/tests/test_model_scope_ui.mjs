// Lightweight front-end check for the global execution-model scope.
//
// Runs completely offline with Node's own vm: it loads the real panel/app.js
// helper section and parses the real panel/index.html, so it verifies the
// shared enum, the visible selector and the wiring without starting the panel,
// calling a provider or touching any real record.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const root = new URL('../', import.meta.url);
const appSource = fs.readFileSync(new URL('panel/app.js', root), 'utf8');
const htmlSource = fs.readFileSync(new URL('panel/index.html', root), 'utf8');

// ---------------------------------------------------------------- app.js helpers
const start = appSource.indexOf("const MODEL_SCOPE_VALUES=");
const end = appSource.indexOf('function renderWorkflow()', start);
assert.ok(start >= 0 && end > start, 'scope helper block must exist in app.js');
const helperSource = appSource.slice(start, end);

let scopeState = {};
const context = {
  state: new Proxy({}, { get: (_target, key) => key === 'models' ? [] : scopeState[key] }),
  $: () => ({ textContent: '', title: '', hidden: false }),
};
vm.createContext(context);
const exported = ';({MODEL_SCOPE_VALUES,MODEL_SCOPE_LABELS,scopeValue,scopeLabel,modelInScope,'
  + 'inScopeModels,scopeHintText})';
const helpers = vm.runInContext(helperSource + exported, context);
const { MODEL_SCOPE_VALUES, MODEL_SCOPE_LABELS, scopeValue, scopeLabel, modelInScope,
        inScopeModels, scopeHintText } = helpers;

assert.deepEqual([...MODEL_SCOPE_VALUES], ['all', 'paid_only', 'local_only'],
  'the UI enum must mirror the backend enum');
assert.deepEqual(Object.keys(MODEL_SCOPE_LABELS).sort(),
  ['all', 'local_only', 'paid_only'], 'every enum value needs one label');

scopeState = { workflow: { model_scope: 'local_only' } };
assert.equal(scopeValue(), 'local_only');
assert.equal(scopeLabel(), '仅本地/自建模型');
assert.match(scopeHintText(), /不会回退或调用任何付费候选/);

scopeState = { workflow: { model_scope: 'paid_only' } };
assert.equal(scopeValue(), 'paid_only');
assert.equal(scopeLabel(), '仅付费模型');
assert.match(scopeHintText(), /不会自动开启付费授权/);

// Missing/unknown values fall back to the legacy default: the UI must never look
// like a filter the backend did not apply.
for (const workflow of [undefined, {}, { model_scope: undefined }, { model_scope: 'paid' },
                        { model_scope: 'ALL' }, { model_scope: 1 }]) {
  scopeState = { workflow };
  assert.equal(scopeValue(), 'all', JSON.stringify(workflow));
  assert.equal(scopeLabel(), '全部模型（兼容默认）');
}

// Classification mirrors provider/is_paid: a remotely connected home service is
// still local, and a loopback URL with a key is not silently called local.
const remoteHome = { id: 'home', provider: 'local', api_base: 'http://home.example:1234/v1', api_key_set: true };
const paidCloud = { id: 'cloud', provider: 'deepseek', api_base: 'https://api.deepseek.com', api_key_set: true };
assert.equal(modelInScope(remoteHome, 'local_only'), true);
assert.equal(modelInScope(remoteHome, 'paid_only'), false);
assert.equal(modelInScope(paidCloud, 'paid_only'), true);
assert.equal(modelInScope(paidCloud, 'local_only'), false);
assert.equal(modelInScope(paidCloud, 'all'), true);
assert.equal(modelInScope(remoteHome, 'all'), true);
assert.deepEqual(inScopeModels([remoteHome, paidCloud], ).length, 2);
scopeState = { workflow: { model_scope: 'local_only' } };
assert.deepEqual(inScopeModels([remoteHome, paidCloud]).map(m => m.id), ['home']);

// --------------------------------------------------------------- index.html DOM
function parseFragment(html) {
  const root = { tag: '#root', attrs: {}, children: [] };
  const stack = [root];
  const voidTags = new Set(['input', 'br', 'hr', 'img', 'meta', 'link', 'source']);
  const token = /<(\/?)([a-zA-Z0-9-]+)((?:\s+[^<>]*?)?)(\/?)>/g;
  let last = 0;
  let match;
  while ((match = token.exec(html)) !== null) {
    const text = html.slice(last, match.index).trim();
    if (text) stack[stack.length - 1].children.push({ tag: '#text', text });
    last = token.lastIndex;
    const [, closing, rawTag, rawAttrs, selfClosing] = match;
    const tag = rawTag.toLowerCase();
    if (closing) {
      for (let index = stack.length - 1; index > 0; index -= 1) {
        if (stack[index].tag === tag) { stack.length = index; break; }
      }
      continue;
    }
    const attrs = {};
    const attribute = /([a-zA-Z0-9-]+)(?:\s*=\s*"([^"]*)")?/g;
    let found;
    while ((found = attribute.exec(rawAttrs)) !== null) {
      attrs[found[1].toLowerCase()] = found[2] === undefined ? '' : found[2];
    }
    const node = { tag, attrs, children: [] };
    stack[stack.length - 1].children.push(node);
    if (!selfClosing && !voidTags.has(tag)) stack.push(node);
  }
  return root;
}

function findAll(node, tag, out = []) {
  if (node.tag === tag) out.push(node);
  for (const child of node.children || []) findAll(child, tag, out);
  return out;
}
function textOf(node) {
  if (node.tag === '#text') return node.text;
  return (node.children || []).map(textOf).join('');
}
function byId(node, id) {
  if (node.attrs && node.attrs.id === id) return node;
  for (const child of node.children || []) {
    const hit = byId(child, id);
    if (hit) return hit;
  }
  return null;
}

const dialogStart = htmlSource.indexOf('<dialog id="workflow-dialog"');
assert.ok(dialogStart >= 0, 'the budget dialog must exist');
const dialogEnd = htmlSource.indexOf('</dialog>', dialogStart);
const dialog = parseFragment(htmlSource.slice(dialogStart, dialogEnd + '</dialog>'.length));

const select = byId(dialog, 'workflow-scope');
assert.ok(select, 'the dialog must contain the global scope selector');
assert.equal(select.tag, 'select');
const options = findAll(select, 'option');
assert.deepEqual(options.map(option => option.attrs.value), ['all', 'paid_only', 'local_only']);
assert.deepEqual(options.map(textOf), ['全部模型（兼容默认）', '仅付费模型', '仅本地/自建模型']);

const labelled = findAll(dialog, 'label').find(node => node.attrs.for === 'workflow-scope');
assert.ok(labelled, 'the selector must have a label bound by for/id');
assert.match(textOf(labelled), /全局执行模型范围/);
const hint = textOf(byId(dialog, 'workflow-scope-hint'));
assert.match(hint, /付费授权/);
assert.match(hint, /余额/);
assert.match(hint, /请求次数/);
const dialogText = textOf(dialog);
assert.match(dialogText, /家中电脑/);
assert.match(dialogText, /DeepSeek/);
assert.match(dialogText, /所有项目生效/);
assert.match(dialogText, /手动指定、续接和恢复任务也不能跨范围/);
assert.match(dialogText, /排队任务在开始前检查新范围/);
assert.match(dialogText, /已在执行的任务继续运行/);
assert.match(dialogText, /没有可用模型时会明确提示/);
// The existing controls keep their ids; nothing was renamed or removed.
for (const id of ['workflow-paid', 'workflow-enforce-budget', 'workflow-enforce-calls',
                  'workflow-task-budget', 'workflow-daily-budget', 'workflow-max-calls',
                  'workflow-form-error', 'workflow-save-status']) {
  assert.ok(byId(dialog, id), `the existing control ${id} must survive`);
}

// The workflow bar shows the active scope next to the existing budget summary.
assert.match(htmlSource, /id="budget-summary"/);
assert.match(htmlSource, /id="workflow-scope-summary"/);
assert.match(htmlSource, /id="workflow-settings"/);
assert.match(htmlSource, /id="task-model"/);

// ------------------------------------------------------------------ app.js wiring
assert.match(appSource, /scopeSummary\.textContent=scopeText/);
assert.match(appSource, /function renderWorkflow\(\)/);
assert.match(appSource, /const inScope=inScopeModels\(enabled\)/);
assert.match(appSource, /范围外，不可选/);
assert.match(appSource, /workflow-scope'\)\.value=scopeValue\(\)/);
assert.match(appSource, /model_scope:\$\('workflow-scope'\)\.value/);
assert.match(appSource, /scopeHintText\(\$\('workflow-scope'\)\.value\)/);
assert.match(appSource, /执行中的任务不受影响，排队任务在启动前按新范围检查/);
// The task dialog must not silently create an out-of-scope option set.
assert.match(appSource, /if\(!inScope\.length\)\{toast\(/);

console.log('PASS model scope UI: shared enum, three labelled options, bound label, '
  + 'local/paid classification, task-picker filtering, settings payload wiring and workflow bar summary');
