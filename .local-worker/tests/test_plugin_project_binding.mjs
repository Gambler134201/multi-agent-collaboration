// Focused tests for the MCP Apps bridge: per-instance project context, timing,
// manual-selection lock and per-request view propagation. Uses a fake host, not
// a native click; the native-entrypoint verification gap is reported separately.
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';

const bridgeSource = readFileSync(new URL('../panel/codex-bridge.js', import.meta.url), 'utf8');

function encodeView(view) {
  return Buffer.from(unescape(encodeURIComponent(JSON.stringify(view))), 'binary')
    .toString('base64').replace(/\+/g, '-').replace(/\//g, '_');
}

// Values created inside the vm context have a different realm prototype, so
// compare plain JSON copies instead of cross-realm object identity.
const plain = value => (value === undefined ? undefined : JSON.parse(JSON.stringify(value)));

function createBridge() {
  const windowListeners = new Map();
  const documentListeners = new Map();
  const calls = [];
  const events = [];
  const parent = {};
  const windowObject = {
    parent,
    addEventListener(type, handler) {
      if (!windowListeners.has(type)) windowListeners.set(type, []);
      windowListeners.get(type).push(handler);
    },
    dispatchEvent(event) {
      events.push(event);
      for (const handler of windowListeners.get(event.type) || []) handler(event);
      return true;
    },
  };
  function deliver(message) {
    const event = {source:parent, data:message};
    for (const handler of windowListeners.get('message') || []) handler(event);
  }
  parent.postMessage = message => {
    calls.push(message);
    if (!message || message.id === undefined) return;
    let result = {};
    if (message.method === 'ui/initialize') {
      result = {protocolVersion:'2025-11-21', hostInfo:{name:'test-host', version:'1'},
                hostCapabilities:{}, hostContext:{theme:'light', displayMode:'inline'}};
    } else if (message.method === 'tools/call') {
      result = {structuredContent:{status:200, data:{ok:true}}};
    }
    deliver({jsonrpc:'2.0', id:message.id, result});
  };
  const context = {
    window:windowObject,
    document:{
      addEventListener(type, handler) {
        if (!documentListeners.has(type)) documentListeners.set(type, []);
        documentListeners.get(type).push(handler);
      },
    },
    CustomEvent:class CustomEvent {
      constructor(type, init) { this.type = type; this.detail = init && init.detail; }
    },
    Response:class Response {
      constructor(body, init) { this.body = body; this.status = init?.status; this.headers = init?.headers; }
    },
    setTimeout, clearTimeout, Map, Promise, JSON, console, Error, Buffer,
    btoa:value => Buffer.from(value, 'binary').toString('base64'),
    atob:value => Buffer.from(value, 'base64').toString('binary'),
  };
  vm.createContext(context);
  vm.runInContext(bridgeSource, context, {filename:'codex-bridge.js'});
  return {
    window:windowObject,
    calls,
    events,
    notify(method, params) { deliver({jsonrpc:'2.0', method, params}); },
    contextView() { return plain(windowObject.modelDeskInitialView ?? null); },
  };
}

test('initialization host context carries no project and binds nothing', () => {
  const bridge = createBridge();
  const initialize = bridge.calls.find(call => call.method === 'ui/initialize');
  assert.equal(initialize.params.appInfo.name, 'model-desk');
  assert.equal(bridge.contextView(), null);
  assert.equal(bridge.events.length, 0);
});

test('explicit opener arguments bind the panel instance once', () => {
  const bridge = createBridge();
  bridge.notify('ui/notifications/tool-input', {arguments:{project:'E:\\project-a'}});
  assert.deepEqual(bridge.contextView(), {mode:'project', project:'E:\\project-a'});
  bridge.notify('ui/notifications/tool-input', {arguments:{project:'E:\\project-a'}});
  assert.equal(bridge.events.length, 1, 'a repeated event must not re-dispatch the same context');
});

test('tool result view binds the panel instance', () => {
  const bridge = createBridge();
  bridge.notify('ui/notifications/tool-result', {structuredContent:{view:{mode:'project', project:'E:\\project-b'}}});
  assert.deepEqual(bridge.contextView(), {mode:'project', project:'E:\\project-b'});
});

test('the first trusted context wins over later events', () => {
  const bridge = createBridge();
  bridge.notify('ui/notifications/tool-input', {arguments:{project:'E:\\first'}});
  bridge.notify('ui/notifications/tool-result', {structuredContent:{view:{mode:'project', project:'E:\\late'}}});
  assert.deepEqual(bridge.contextView(), {mode:'project', project:'E:\\first'});
});

test('a manual project choice locks out later host events', () => {
  const bridge = createBridge();
  bridge.notify('ui/notifications/tool-input', {arguments:{project:'E:\\bound'}});
  bridge.window.modelDeskLockProjectContext();
  bridge.notify('ui/notifications/tool-result', {structuredContent:{view:{mode:'project', project:'E:\\stale'}}});
  bridge.notify('ui/notifications/tool-input', {arguments:{project:'E:\\stale'}});
  assert.deepEqual(bridge.contextView(), {mode:'project', project:'E:\\bound'});
});

test('missing, ambiguous, remote or invalid context stays unbound', () => {
  const cases = [
    undefined,
    {structuredContent:{}},
    {structuredContent:{view:{mode:'unbound', project:null}}},
    {structuredContent:{view:{mode:'all', project:null}}},
    {structuredContent:{view:{mode:'project', project:''}}},
    {structuredContent:{view:{mode:'project'}}},
    {structuredContent:{view:{mode:'project', project:'relative/project'}}},
    {structuredContent:{view:{mode:'project', project:'https://example.com/project'}}},
    {structuredContent:{view:{mode:'remote-project', project:'https://example.com/project'}}},
  ];
  for (const params of cases) {
    const bridge = createBridge();
    bridge.notify('ui/notifications/tool-result', params);
    assert.equal(bridge.contextView(), null, JSON.stringify(params));
  }
  const withoutProject = createBridge();
  withoutProject.notify('ui/notifications/tool-input', {arguments:{}});
  assert.equal(withoutProject.contextView(), null);
});

test('two panel instances keep independent contexts', () => {
  const a = createBridge();
  const b = createBridge();
  a.notify('ui/notifications/tool-input', {arguments:{project:'E:\\project-a'}});
  b.notify('ui/notifications/tool-input', {arguments:{project:'E:\\project-b'}});
  assert.deepEqual(a.contextView(), {mode:'project', project:'E:\\project-a'});
  assert.deepEqual(b.contextView(), {mode:'project', project:'E:\\project-b'});
  b.notify('ui/notifications/tool-result', {structuredContent:{view:{mode:'project', project:'E:\\project-c'}}});
  assert.deepEqual(a.contextView(), {mode:'project', project:'E:\\project-a'}, 'panel A must not see panel B events');
});

test('every panel request carries its own structured view', async () => {
  const bridge = createBridge();
  const view = {mode:'project', project:'E:\\project-a'};
  const headers = {'X-Model-Desk-View':encodeView(view)};
  await bridge.window.fetch('/api/state', {headers});
  await bridge.window.fetch('/api/workflow', {method:'POST', headers, body:JSON.stringify({enabled:true})});
  const panelCalls = bridge.calls.filter(call => call.method === 'tools/call');
  assert.equal(panelCalls.length, 2);
  for (const call of panelCalls) assert.equal(call.params.name, 'panel_request');
  assert.deepEqual(plain(panelCalls[0].params.arguments.view), view);
  assert.deepEqual(plain(panelCalls[1].params.arguments.view), view);
  assert.equal(panelCalls[0].params.arguments.path, '/api/state');
  assert.equal(panelCalls[0].params.arguments.method, 'GET');
  assert.equal(panelCalls[1].params.arguments.path, '/api/workflow');
  assert.deepEqual(plain(panelCalls[1].params.arguments.body), {enabled:true});
});

test('a panel request without a view header stays explicitly unbound', async () => {
  const bridge = createBridge();
  await bridge.window.fetch('/api/state');
  const call = bridge.calls.find(item => item.method === 'tools/call');
  assert.deepEqual(plain(call.params.arguments.view), {mode:'unbound', project:null});
});
