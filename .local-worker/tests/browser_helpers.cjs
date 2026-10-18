// Shared, portable Playwright lookup for the optional mock-browser checks.
//
// Nothing here contacts a workbench, provider or account.  The checks themselves
// serve the real panel files from an isolated local HTTP server or intercept all
// requests in the browser, so they are offline by default.
//
// Dependency discovery, in order:
//   1. MODEL_DESK_PLAYWRIGHT - absolute path to a Playwright module or package;
//   2. MODEL_DESK_NODE_MODULES - a node_modules directory containing playwright;
//   3. normal require('playwright') resolution, including NODE_PATH.
//
// The browser channel can be selected with MODEL_DESK_BROWSER_CHANNEL.  When it
// is unset, the caller's channel is tried first and the other of
// "bundled Chromium" / "Microsoft Edge" is tried as a fallback.  This keeps the
// checks runnable with whichever Playwright installation a contributor has,
// without baking a machine-specific module path into source.
'use strict';

const path = require('node:path');
const { createRequire } = require('node:module');

const moduleRequire = createRequire(__filename);

function candidates() {
  const list = [];
  if (process.env.MODEL_DESK_PLAYWRIGHT) list.push(process.env.MODEL_DESK_PLAYWRIGHT);
  if (process.env.MODEL_DESK_NODE_MODULES) {
    list.push(path.join(process.env.MODEL_DESK_NODE_MODULES, 'playwright'));
  }
  list.push('playwright');
  return [...new Set(list)];
}

function loadPlaywright() {
  const errors = [];
  for (const spec of candidates()) {
    try {
      return moduleRequire(spec);
    } catch (error) {
      errors.push(`${spec}: ${error.message}`);
    }
  }
  throw new Error(
    'Playwright is not available for the optional mock-browser checks.\n'
    + 'Install Playwright in your own Node environment (not as a project dependency),\n'
    + 'or point the checks at an existing installation with one of:\n'
    + '  set MODEL_DESK_PLAYWRIGHT=C:\\path\\to\\playwright\n'
    + '  set MODEL_DESK_NODE_MODULES=C:\\path\\to\\node_modules\n'
    + '  set NODE_PATH=C:\\path\\to\\node_modules\n'
    + 'Tried:\n  ' + errors.join('\n  ')
  );
}

function browserOptions(options) {
  const merged = { headless: true, ...options };
  const channel = process.env.MODEL_DESK_BROWSER_CHANNEL;
  if (channel !== undefined) {
    if (!channel || ['chromium', 'bundled', 'default'].includes(channel.toLowerCase())) {
      delete merged.channel;
    } else {
      merged.channel = channel;
    }
  }
  return merged;
}

async function launch(options = {}) {
  const { chromium } = loadPlaywright();
  const requested = browserOptions(options);
  const attempts = [requested];
  if (process.env.MODEL_DESK_BROWSER_CHANNEL === undefined) {
    if (requested.channel) {
      const bundled = { ...requested };
      delete bundled.channel;
      attempts.push(bundled);
    } else {
      attempts.push({ ...requested, channel: 'msedge' });
    }
  }
  const errors = [];
  for (const attempt of attempts) {
    try {
      return await chromium.launch(attempt);
    } catch (error) {
      errors.push(error.message.split('\n')[0]);
    }
  }
  throw new Error('No usable Playwright browser could be launched:\n  ' + errors.join('\n  '));
}

module.exports = {
  loadPlaywright,
  chromium: { launch },
  browserOptions,
  launch,
};
