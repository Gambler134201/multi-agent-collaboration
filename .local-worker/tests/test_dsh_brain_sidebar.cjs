'use strict';

// Offline checks for the native right-sidebar workbench entry. They load the
// client bundle in a VM with a fake React/Cordis surface only: no DSH profile,
// workbench service, browser, model or real session is touched.

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const ROOT = path.resolve(__dirname, '..', '..');
const PACKAGE = path.join(ROOT, 'integrations', 'dsh-brain');
const CLIENT = path.join(PACKAGE, 'client.mjs');

function fakeReact(presetState) {
  const effects = [];
  const states = [];
  return {
    effects,
    states,
    createElement(type, props, ...children) {
      return { type, props: props || {}, children };
    },
    useEffect(fn) {
      effects.push(fn);
    },
    useRef(value) {
      return { current: value };
    },
    useState(value) {
      const cell = {
        value: states.length === 0 && presetState !== undefined ? presetState : value,
        updates: [],
      };
      states.push(cell);
      return [cell.value, (next) => {
        cell.value = typeof next === 'function' ? next(cell.value) : next;
        cell.updates.push(cell.value);
      }];
    },
  };
}

function loadClient(react, platform) {
  let registration;
  const sandbox = {
    URL,
    ...(platform === undefined ? {} : { navigator: { platform } }),
    window: {
      __ModuleLoader__: {
        load(value) {
          registration = value;
        },
      },
    },
  };
  vm.runInNewContext(fs.readFileSync(CLIENT, 'utf8'), sandbox, { filename: CLIENT });
  assert.ok(registration, 'client bundle registers a lazy factory');
  return registration.factory((specifier) => {
    if (specifier === 'react') return react;
    throw new Error('unexpected client module: ' + specifier);
  });
}

function makeContext(snapshot) {
  const types = [];
  const slots = [];
  const disposers = [];
  const source = {
    getSnapshot: () => snapshot,
    subscribe: () => () => {},
  };
  const ctx = {
    effect(fn) {
      const cleanup = fn();
      if (typeof cleanup === 'function') disposers.push(cleanup);
      return cleanup;
    },
    sidebarRightTabs: {
      register(definition) {
        definition.active = true;
        types.push(definition);
        return () => { definition.active = false; };
      },
    },
    slots: {
      inject(name, callback) {
        assert.equal(name, 'sidebar.right.pane.tab');
        const cleanup = callback();
        if (typeof cleanup === 'function') disposers.push(cleanup);
        return cleanup;
      },
      register(definition, component) {
        definition.active = true;
        slots.push({ definition, component });
        return () => { definition.active = false; };
      },
    },
    sessions: { list: source },
  };
  return { ctx, types, slots, disposers, source };
}

function bodyProps(definition, sessionId, snapshot, calls, throws = false) {
  const tab = {
    actions: {
      openTab(...args) {
        calls.push(args);
        if (throws) throw new Error('browser kind unavailable');
      },
    },
  };
  return {
    ...definition.inject(sessionId),
    useSessionList: (selector) => selector(snapshot),
    useTabInfo: () => ({ tab }),
  };
}

test('package manifest exposes the client half beside the Host bundle', () => {
  const manifest = JSON.parse(fs.readFileSync(path.join(PACKAGE, 'package.json'), 'utf8'));
  assert.equal(manifest.name, 'model-desk-dsh-brain');
  assert.equal(manifest.version, '0.2.6');
  assert.equal(manifest.exports['./client'], './client.mjs');
  assert.equal(manifest.exports['.'], './index.cjs');
  assert.ok(manifest.files.includes('client.mjs'));
  assert.equal(manifest.dsh.bundle.patch, 'cordis.patch.yml');
  assert.equal(manifest.dsh.client.platform, 'web');
  assert.equal(manifest.dsh.client.immediately, true);
  assert.ok(manifest.dsh.client.inject.includes('@deepseek-ai/dsh-client-ui-sidebar-right'));
  assert.ok(manifest.dsh.client.inject.includes('@deepseek-ai/dsh-api-session-controller'));
});

test('client exposes exactly one prepared loopback-origin marker and no private channels', () => {
  const source = fs.readFileSync(CLIENT, 'utf8');
  assert.equal(source.split('"__MODEL_DESK_PANEL_ORIGIN__"').length - 1, 1);
  for (const forbidden of [
    'postMessage', 'window.parent', 'window.top', 'document.body',
    'localStorage', 'sessionStorage', 'Authorization', 'token',
    'fetch(', 'XMLHttpRequest', '/api/', 'brain_connect',
  ]) {
    assert.equal(source.includes(forbidden), false, `client must not use ${forbidden}`);
  }
});

test('the prepared origin marker becomes the browser target origin', () => {
  const source = fs.readFileSync(CLIENT, 'utf8')
    .replace('"__MODEL_DESK_PANEL_ORIGIN__"', '"http://localhost:9123"');
  let registration;
  vm.runInNewContext(source, {
    URL,
    window: { __ModuleLoader__: { load(value) { registration = value; } } },
  }, { filename: 'prepared-client.mjs' });
  const plugin = registration.factory((specifier) => {
    if (specifier === 'react') return fakeReact();
    throw new Error('unexpected client module: ' + specifier);
  });
  assert.equal(plugin._test.PANEL_ORIGIN, 'http://localhost:9123');
  const target = plugin._test.resolveWorkbenchTarget({ cwd: 'E:\\work' }, 's1', 'ready');
  assert.equal(new URL(target.url).origin, 'http://localhost:9123');
  assert.equal(new URL(target.url).searchParams.get('project'), 'E:\\work');
});

test('guide entry and body are registered with one Chinese sidebar type', () => {
  const react = fakeReact();
  const plugin = loadClient(react);
  assert.deepEqual([...plugin.inject], ['slots', 'sessions', 'sidebarRightTabs']);
  const { ctx, types, slots, disposers } = makeContext({ phase: 'ready', byId: {} });
  plugin.apply(ctx);

  assert.equal(types.length, 1);
  const definition = types[0];
  assert.equal(definition.kind, 'model-desk-workbench');
  assert.equal(definition.id, 'model-desk-dsh-brain/workbench');
  assert.equal(definition.title(), '模型工作台');
  assert.equal(definition.guide.length, 1);
  assert.equal(definition.guide[0].title(), '模型工作台');
  assert.match(definition.guide[0].description(), /原生会话/);
  assert.ok(Number.isInteger(definition.guide[0].order));

  assert.equal(slots.length, 1);
  assert.equal(slots[0].definition.name, 'sidebar.right.pane.tab');
  assert.equal(slots[0].definition.key, definition.id);
  const injected = slots[0].definition.inject('session-1');
  assert.equal(injected.sessionId, 'session-1');
  assert.equal(injected.hooks.sessionList, ctx.sessions.list);

  for (const dispose of disposers) dispose();
  assert.equal(definition.active, false, 'type registration follows plugin lifetime');
  assert.equal(slots[0].definition.active, false, 'body registration follows plugin lifetime');
});

test('only an absolute native session cwd becomes an encoded project URL', () => {
  const plugin = loadClient(fakeReact());
  const resolve = plugin._test.resolveWorkbenchTarget;
  const row = { cwd: 'E:\\项目 A\\demo' };
  const target = resolve(row, 'session-1', 'ready');
  assert.equal(target.status, 'ready');
  const parsed = new URL(target.url);
  assert.equal(parsed.origin, 'http://127.0.0.1:8765');
  assert.deepEqual([...parsed.searchParams.keys()], ['host', 'project']);
  assert.equal(parsed.searchParams.get('host'), 'dsh');
  assert.equal(parsed.searchParams.get('project'), row.cwd);
  assert.match(target.url, /%5C/, 'backslashes are URL-encoded');
  assert.equal(plugin._test.buildWorkbenchUrl(), 'http://127.0.0.1:8765/?host=dsh&view=unbound');

  for (const cwd of ['relative/path', 'C:relative', '', 'E:\\line\nbreak', undefined, null, 42]) {
    assert.equal(resolve({ cwd }, 'session-1', 'ready').status, 'unbound', String(cwd));
  }
  assert.equal(resolve(undefined, 'session-1', 'pending').status, 'pending');
  assert.equal(resolve(undefined, 'session-1', 'ready').status, 'unknown');
  assert.equal(resolve(row, '', 'ready').status, 'unknown');

  assert.equal(plugin._test.isAbsoluteProjectPath('C:\\work'), true);
  assert.equal(plugin._test.isAbsoluteProjectPath('C:/work'), true);
  assert.equal(plugin._test.isAbsoluteProjectPath('\\\\server\\share'), true);
  assert.equal(plugin._test.isAbsoluteProjectPath('/srv/work'), true);
  assert.equal(plugin._test.isAbsoluteProjectPath('C:work'), false);
});

test('ready body replaces itself with the native browser tab exactly once', () => {
  const react = fakeReact();
  const plugin = loadClient(react);
  const snapshot = { phase: 'ready', byId: { s1: { cwd: 'E:\\work\\one' } } };
  const { ctx, slots } = makeContext(snapshot);
  plugin.apply(ctx);
  const calls = [];
  const props = bodyProps(slots[0].definition, 's1', snapshot, calls);
  const tree = slots[0].component(props);
  assert.equal(calls.length, 0, 'rendering alone does not open anything');
  for (const effect of react.effects) effect();
  assert.equal(calls.length, 1);
  assert.equal(calls[0][0], 'browser');
  assert.equal(calls[0][1].replaceTab, true);
  assert.equal(new URL(calls[0][1].params.url).searchParams.get('project'), 'E:\\work\\one');
  for (const effect of react.effects) effect();
  assert.equal(calls.length, 1, 'a replayed effect cannot open a second tab');
  assert.equal(tree.type, 'div');
});

test('unknown identity stays unbound and offers an explicit unselected action', () => {
  const react = fakeReact();
  const plugin = loadClient(react);
  const snapshot = { phase: 'ready', byId: { s1: { cwd: 'relative/work' } } };
  const { ctx, slots } = makeContext(snapshot);
  plugin.apply(ctx);
  const calls = [];
  const props = bodyProps(slots[0].definition, 's1', snapshot, calls);
  const tree = slots[0].component(props);
  for (const effect of react.effects) effect();
  assert.equal(calls.length, 0, 'no guessed project is opened');
  assert.match(tree.children[0].children[0], /未选择项目/);
  const button = tree.children.find((child) => child.type === 'button');
  assert.ok(button);
  button.props.onClick();
  assert.equal(calls.length, 1);
  const opened = new URL(calls[0][1].params.url);
  assert.equal(opened.searchParams.has('project'), false);
  assert.equal(opened.searchParams.get('view'), 'unbound', 'do not inherit a remembered project');
  assert.equal(opened.searchParams.get('host'), 'dsh', 'the DSH display context is explicit');
  assert.equal(opened.origin, 'http://127.0.0.1:8765');
});

test('Windows client does not bind current-drive-rooted directories', () => {
  const plugin = loadClient(fakeReact(), 'Win32');
  for (const cwd of ['/work', '\\work', 'E:work']) {
    assert.equal(plugin._test.resolveWorkbenchTarget({ cwd }, 's1', 'ready').status, 'unbound');
  }
  assert.equal(plugin._test.resolveWorkbenchTarget({ cwd: 'E:\\work' }, 's1', 'ready').status, 'ready');
  assert.equal(loadClient(fakeReact(), 'Linux x86_64')._test.resolveWorkbenchTarget(
    { cwd: '/srv/work' }, 's1', 'ready').status, 'ready');
});

test('two tabs in two sessions keep their own projects and tab actions', () => {
  const react = fakeReact();
  const plugin = loadClient(react);
  const snapshot = {
    phase: 'ready',
    byId: { s1: { cwd: 'E:\\work\\one' }, s2: { cwd: 'E:\\work\\two' } },
  };
  const { ctx, slots } = makeContext(snapshot);
  plugin.apply(ctx);
  const calls = [];
  for (const sessionId of ['s1', 's2']) {
    const props = bodyProps(slots[0].definition, sessionId, snapshot, calls);
    slots[0].component(props);
  }
  for (const effect of react.effects) effect();
  assert.equal(calls.length, 2);
  const projects = calls.map((call) => new URL(call[1].params.url).searchParams.get('project'));
  assert.deepEqual(projects, ['E:\\work\\one', 'E:\\work\\two']);
});

test('failure view retries with the same native target', () => {
  const react = fakeReact('无法打开原生右侧栏浏览器。');
  const plugin = loadClient(react);
  const snapshot = { phase: 'ready', byId: { s1: { cwd: 'E:\\work' } } };
  const { ctx, slots } = makeContext(snapshot);
  plugin.apply(ctx);
  const calls = [];
  const props = bodyProps(slots[0].definition, 's1', snapshot, calls);
  const tree = slots[0].component(props);
  assert.equal(calls.length, 0);
  const button = tree.children.find((child) => child.type === 'button');
  assert.ok(button);
  button.props.onClick();
  assert.equal(calls.length, 1);
  assert.equal(calls[0][0], 'browser');
  assert.equal(new URL(calls[0][1].params.url).searchParams.get('project'), 'E:\\work');
});

test('missing native browser becomes an explanatory failure, not an exception', () => {
  const react = fakeReact();
  const plugin = loadClient(react);
  const snapshot = { phase: 'ready', byId: { s1: { cwd: 'E:\\work' } } };
  const { ctx, slots } = makeContext(snapshot);
  plugin.apply(ctx);
  const calls = [];
  const props = bodyProps(slots[0].definition, 's1', snapshot, calls, true);
  assert.doesNotThrow(() => {
    slots[0].component(props);
    for (const effect of react.effects) effect();
  });
  assert.equal(calls.length, 1);
  assert.equal(react.states.length, 1);
  assert.match(react.states[0].updates[0], /原生右侧栏浏览器/);
});
