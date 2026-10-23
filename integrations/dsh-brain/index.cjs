'use strict';

/**
 * Optional DSH native main-brain adapter for the local Model Desk workbench.
 *
 * This file is a prebuilt CommonJS Cordis plugin. Loading it does not start the
 * workbench, read authentication data, or write DSH/workbench
 * configuration. On every top-level Agent creation it binds that exact native
 * session (session id + session header id + realpath of the native cwd) to one
 * agent-scoped stdio MCP client for the already-running `brain_mcp_server.py`.
 *
 * The project, owner and session arguments are derived only from the native
 * Agent/session identity. Tool calls cannot change them. A first binding is
 * fixed for this plugin instance: later native identity or project drift makes
 * the binding fail closed instead of silently moving ownership.
 * Ordinary loading/waiting never invokes a model. An explicitly requested
 * wakeup probe admits one fixed native followup, which starts a new model turn.
 */
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const {createBridge} = require('./wakeup.cjs');
const { spawnSync, execFile } = require('node:child_process');
const { createRequire } = require('node:module');
const { pathToFileURL, fileURLToPath } = require('node:url');
const hostAccount = require('./account.cjs');

const PLUGIN_NAME = 'model-desk-dsh-brain';
const RULE_SECTION = 'model-desk-brain-rules';
const RULE_ORDER = 9000;
const DEFAULT_SERVER_NAME = 'model_desk_brain';
const DEFAULT_OWNER_PREFIX = 'dsh';
const DEFAULT_TOOL_CALL_TIMEOUT_MS = 60_000;
const DEFAULT_STATUS_TIMEOUT_MS = 10_000;
const MAX_STATUS_TIMEOUT_MS = 30_000;
const MAX_TOOL_CALL_TIMEOUT_MS = 600_000;
const SERVER_NAME_PATTERN = /^[A-Za-z0-9_-]{1,32}$/;
const OWNER_PREFIX_PATTERN = /^[A-Za-z0-9][A-Za-z0-9_.:@+-]{0,63}$/;
const LOOPBACK_HOSTS = new Set(['127.0.0.1', 'localhost', '::1']);

const BRAIN_TOOLS = Object.freeze([
  'brain_connect',
  'brain_preflight',
  'brain_describe',
  'brain_candidates',
  'brain_route_preview',
  'brain_dispatch',
  'brain_status',
  'brain_request_status',
  'brain_delivery',
  'brain_review',
  'brain_rework',
  'brain_wakeup_probe',
  'brain_wait',
]);
const BRAIN_TOOL_SET = new Set(BRAIN_TOOLS);
const MUTATING_TOOLS = new Set(['brain_dispatch', 'brain_rework']);

const CONFIG_KEYS = new Set([
  'schema',
  'configFile',
  'python',
  'brainMcpServer',
  'workflowSettings',
  'panelUrl',
  'serverName',
  'ownerPrefix',
  'toolCallTimeoutMs',
  'statusTimeoutMs',
  'mcpClientPath',
]);
// A project/owner/session must never be configured globally for every chat.
const FORBIDDEN_CONFIG_KEYS = Object.freeze([
  'project',
  'projectPath',
  'cwd',
  'owner',
  'session',
  'sessionId',
  'nativeSessionId',
  'role',
  'host',
]);

// One live adapter instance per DSH root context. Re-applying (HMR/reload)
// disposes the previous instance before installing the new one.
const liveInstances = new WeakMap();
// First bindings survive plugin hot reload inside one DSH process, but are
// deliberately in memory only: the adapter never writes a session/project
// ownership record or a host profile.
const firstBindings = new WeakMap();

class ConfigError extends Error {}

function logWith(ctx, level, message) {
  const text = `[${PLUGIN_NAME}] ${String(message || '').replace(/[\r\n]+/g, ' ').slice(0, 600)}`;
  try {
    const logger = ctx && ctx.logger;
    const fn = logger && (logger[level] || logger.info || logger.warn);
    if (typeof fn === 'function') fn.call(logger, text);
  } catch {
    // Logging must never decide whether the adapter is safe to load.
  }
}

function safeMessage(error) {
  const text = error && error.message ? error.message : String(error || '');
  return text.replace(/[\r\n]+/g, ' ').slice(0, 300);
}

function executorMarker() {
  try {
    // These are the actual markers supplied by our DSH execution adapter.
    for (const key of ['MODEL_DESK_RECORD_PROOF', 'MODEL_DESK_PRESET_PROOF', 'MODEL_DESK_SUBAGENT_LEDGER']) {
      if (Object.hasOwn(process.env, key)) return key;
    }
    // Any MODEL_DESK_*EXECUTOR* marker means this process is an execution
    // context, not the desktop main-brain profile. Presence alone rejects;
    // a value of "0" is still a marker, by design (fail closed).
    for (const key of Object.keys(process.env)) {
      if (key.startsWith('MODEL_DESK_') && /EXECUTOR/i.test(key)) return key;
    }
    for (const key of Object.keys(process.env)) {
      if (!key.startsWith('MODEL_DESK_')) continue;
      const value = process.env[key];
      if (typeof value === 'string' && value.trim().toLowerCase() === 'executor') return key;
    }
    for (const key of ['MODEL_DESK_ROLE', 'MODEL_DESK_DSH_ROLE', 'DSH_AGENT_ROLE', 'DSH_ROLE']) {
      const value = process.env[key];
      if (typeof value === 'string' && value.trim().toLowerCase() === 'executor') return key;
    }
  } catch {
    return 'MODEL_DESK_EXECUTOR_CHECK_FAILED';
  }
  return null;
}

function hostReady(ctx) {
  return Boolean(
    ctx
    && typeof ctx === 'object'
    && typeof ctx.on === 'function'
    && typeof ctx.effect === 'function'
    && ctx.agents
    && typeof ctx.agents.roots === 'function',
  );
}

function agentHostReady(agent) {
  const agentCtx = agent && agent.ctx;
  return Boolean(
    agentCtx
    && typeof agentCtx === 'object'
    && typeof agentCtx.on === 'function'
    && typeof agentCtx.effect === 'function'
    && typeof agentCtx.plugin === 'function'
    && agentCtx.tools
    && typeof agentCtx.tools.guard === 'function'
    && agentCtx.systemPrompt
    && typeof agentCtx.systemPrompt.section === 'function',
  );
}

function isTopLevel(ctx, agent) {
  try {
    const roots = ctx.agents.roots();
    return Array.isArray(roots) && roots.some((root) => root === agent);
  } catch {
    return false;
  }
}

function absoluteExisting(value, label, allowDirectory = false) {
  if (typeof value !== 'string' || !value.trim()) {
    throw new ConfigError(`${label} 必须是显式绝对路径`);
  }
  const text = value.trim();
  if (text.includes('\0') || !fullyQualified(text)) {
    throw new ConfigError(`${label} 必须是显式绝对路径`);
  }
  let stat;
  try {
    stat = fs.statSync(text);
  } catch {
    throw new ConfigError(`${label} 指向的路径不存在`);
  }
  if (allowDirectory ? !(stat.isFile() || stat.isDirectory()) : !stat.isFile()) {
    throw new ConfigError(`${label} 指向的路径类型不正确`);
  }
  return text;
}

function normalizePanelUrl(value) {
  if (typeof value !== 'string' || !value.trim()) {
    throw new ConfigError('panelUrl 需要是本机回环 HTTP 地址');
  }
  let parsed;
  try {
    parsed = new URL(value.trim());
  } catch {
    throw new ConfigError('panelUrl 不是有效地址');
  }
  if (parsed.protocol !== 'http:') throw new ConfigError('panelUrl 只允许 http:// 回环地址');
  if (parsed.username || parsed.password) throw new ConfigError('panelUrl 不能包含账号或密码');
  const host = parsed.hostname.replace(/^\[|\]$/g, '').toLowerCase();
  if (!LOOPBACK_HOSTS.has(host)) throw new ConfigError('panelUrl 只允许 127.0.0.1 / localhost / ::1');
  if ((parsed.pathname && parsed.pathname !== '/') || parsed.search || parsed.hash) {
    throw new ConfigError('panelUrl 只能是不带路径、查询或片段的回环源');
  }
  return parsed.origin;
}

function integerInRange(value, fallback, minimum, maximum, label) {
  if (value === undefined || value === null) return fallback;
  if (typeof value !== 'number' || !Number.isInteger(value) || value < minimum || value > maximum) {
    throw new ConfigError(`${label} 需要是 ${minimum} 到 ${maximum} 之间的整数`);
  }
  return value;
}

function loadConfig(raw) {
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) {
    throw new ConfigError('插件配置需要是一个对象');
  }
  let fileConfig = {};
  // Bundle-layer !!js expressions are not a portable environment lookup in
  // the installed desktop loader. Resolve this one explicit setting here.
  // A full inline configuration remains independent of any inherited value.
  const inlinePaths = ['python', 'brainMcpServer', 'workflowSettings'].some((key) => Object.hasOwn(raw, key));
  const configFile = raw.configFile || (!inlinePaths ? process.env.MODEL_DESK_DSH_BRAIN_CONFIG : undefined);
  if (typeof configFile === 'string' && configFile.trim()) {
    const fileName = absoluteExisting(configFile, 'configFile');
    let text;
    try {
      text = fs.readFileSync(fileName, 'utf8');
    } catch {
      throw new ConfigError('configFile 无法读取');
    }
    let parsed;
    try {
      parsed = JSON.parse(text.replace(/^\uFEFF/, ''));
    } catch {
      throw new ConfigError('configFile 不是有效 JSON');
    }
    if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
      throw new ConfigError('configFile 内容需要是一个对象');
    }
    fileConfig = parsed;
  } else if (configFile !== undefined && configFile !== null && configFile !== '') {
    throw new ConfigError('configFile 需要是绝对路径字符串');
  }

  // Explicit inline config wins over the file; the file supplies the paths a
  // host profile does not need to repeat.
  const merged = { ...fileConfig, ...raw };
  for (const key of Object.keys(merged)) {
    if (!CONFIG_KEYS.has(key)) throw new ConfigError(`不支持的插件配置字段：${key}`);
  }
  for (const key of FORBIDDEN_CONFIG_KEYS) {
    if (Object.hasOwn(merged, key)) {
      throw new ConfigError(`插件配置不能固定 ${key}；项目、拥有者和会话只能来自当前原生聊天`);
    }
  }
  if (merged.schema !== undefined && merged.schema !== 1) {
    throw new ConfigError('插件配置 schema 只支持 1');
  }

  const config = {
    schema: 1,
    python: absoluteExisting(merged.python, 'python'),
    brainMcpServer: absoluteExisting(merged.brainMcpServer, 'brainMcpServer'),
    workflowSettings: absoluteExisting(merged.workflowSettings, 'workflowSettings'),
    panelUrl: normalizePanelUrl(merged.panelUrl === undefined ? 'http://127.0.0.1:8765' : merged.panelUrl),
    serverName: (() => {
      const value = merged.serverName === undefined ? DEFAULT_SERVER_NAME : merged.serverName;
      if (typeof value !== 'string' || !SERVER_NAME_PATTERN.test(value)) {
        throw new ConfigError('serverName 需要是 1 到 32 位字母、数字、下划线或连字符');
      }
      return value;
    })(),
    ownerPrefix: (() => {
      const value = merged.ownerPrefix === undefined ? DEFAULT_OWNER_PREFIX : merged.ownerPrefix;
      if (typeof value !== 'string' || !OWNER_PREFIX_PATTERN.test(value)) {
        throw new ConfigError('ownerPrefix 格式不正确');
      }
      return value;
    })(),
    toolCallTimeoutMs: integerInRange(
      merged.toolCallTimeoutMs,
      DEFAULT_TOOL_CALL_TIMEOUT_MS,
      1000,
      MAX_TOOL_CALL_TIMEOUT_MS,
      'toolCallTimeoutMs',
    ),
    statusTimeoutMs: integerInRange(
      merged.statusTimeoutMs,
      DEFAULT_STATUS_TIMEOUT_MS,
      1000,
      MAX_STATUS_TIMEOUT_MS,
      'statusTimeoutMs',
    ),
  };
  if (merged.mcpClientPath !== undefined && merged.mcpClientPath !== null && merged.mcpClientPath !== '') {
    config.mcpClientPath = absoluteExisting(merged.mcpClientPath, 'mcpClientPath', true);
  }
  return Object.freeze(config);
}

function recordsFor(ctx) {
  let records = firstBindings.get(ctx);
  if (!records) {
    records = new Map();
    firstBindings.set(ctx, records);
  }
  return records;
}

function normalizeMcpModule(moduleValue) {
  const candidates = [
    moduleValue,
    moduleValue && moduleValue.default,
    moduleValue && moduleValue.default && moduleValue.default.default,
  ];
  for (const candidate of candidates) {
    if (candidate && typeof candidate === 'object' && typeof candidate.apply === 'function') return candidate;
    if (typeof candidate === 'function') return candidate;
  }
  throw new Error('MCP client 模块没有可用的 apply 导出');
}

async function resolveMcpModule(state, agentCtx) {
  if (state.modulePromise) return state.modulePromise;
  state.modulePromise = (async () => {
    const specs = [];
    if (state.config.mcpClientPath) specs.push(state.config.mcpClientPath);
    specs.push('@deepseek-ai/dsh-mcp-client');

    const anchors = [__filename, path.join(__dirname, 'package.json')];
    if (agentCtx && typeof agentCtx.baseUrl === 'string' && agentCtx.baseUrl) {
      try {
        if (agentCtx.baseUrl.endsWith('/')) {
          anchors.push(fileURLToPath(new URL('dsh-brain-host.cjs', agentCtx.baseUrl)));
        } else {
          anchors.push(fileURLToPath(new URL(agentCtx.baseUrl)));
          // A context base may be a directory URL without a trailing slash;
          // include both interpretations so peer resolution still works.
          anchors.push(fileURLToPath(new URL('dsh-brain-host.cjs', `${agentCtx.baseUrl}/`)));
        }
      } catch {
        // Ignore a non-file base URL; the package/local anchors still apply.
      }
    }
    anchors.push(path.join(path.dirname(process.execPath), 'dsh-runtime.cjs'));

    let lastError;
    for (const spec of specs) {
      for (const anchor of anchors) {
        try {
          return normalizeMcpModule(createRequire(anchor)(spec));
        } catch (error) {
          lastError = error;
        }
        try {
          const requireFrom = createRequire(anchor);
          const resolved = requireFrom.resolve(spec);
          return normalizeMcpModule(await import(pathToFileURL(resolved).href));
        } catch (error) {
          lastError = error;
        }
      }
    }
    throw new Error(`无法解析 @deepseek-ai/dsh-mcp-client；请确认 DSH desktop profile 已提供该包。${safeMessage(lastError)}`);
  })();
  return state.modulePromise;
}

function statusEnv() {
  const env = {};
  for (const key of [
    'SystemRoot', 'SYSTEMROOT', 'WINDIR', 'PATH', 'Path', 'PATHEXT', 'COMSPEC',
    'TEMP', 'TMP', 'TMPDIR', 'USERPROFILE', 'LOCALAPPDATA', 'APPDATA', 'HOME',
  ]) {
    if (process.env[key] !== undefined) env[key] = process.env[key];
  }
  env.PYTHONIOENCODING = 'utf-8';
  env.PYTHONUTF8 = '1';
  env.PYTHONUNBUFFERED = '1';
  return env;
}

function parseStatus(result) {
  const failed = { readable: false, enabled: false, configuredEnabled: false, error: '' };
  if (!result) return failed;
  if (result.error || result.status !== 0) return failed;
  const text = String(result.stdout || '').replace(/^\uFEFF/, '').trim();
  if (!text) return failed;
  let payload;
  try {
    payload = JSON.parse(text);
  } catch {
    return failed;
  }
  if (!payload || typeof payload !== 'object' || Array.isArray(payload)) return failed;
  const errorText = typeof payload.error === 'string' ? payload.error : '';
  const readable = payload.readable === true && errorText === '';
  // Missing/unreadable settings, invalid fields and an explicit disabled switch
  // are all OFF. Only a readable, enabled snapshot may admit a new dispatch.
  const enabled = readable && payload.enabled === true;
  return {
    readable,
    enabled,
    modelScope: ['all','paid_only','local_only'].includes(payload.model_scope) ? payload.model_scope : 'unknown',
    paidAllowed: payload.paid_allowed === true,
    configuredEnabled: payload.enabled === true,
    error: errorText,
  };
}

function readStatusSync(config) {
  try {
    const result = spawnSync(config.python, [config.workflowSettings, '--status'], {
      cwd: path.dirname(config.workflowSettings),
      env: statusEnv(),
      encoding: 'utf8',
      timeout: config.statusTimeoutMs,
      maxBuffer: 256 * 1024,
      windowsHide: true,
    });
    return parseStatus(result);
  } catch (error) {
    return { readable: false, enabled: false, configuredEnabled: false, error: safeMessage(error) };
  }
}

function readStatus(config) {
  return new Promise((resolve) => {
    try {
      execFile(
        config.python,
        [config.workflowSettings, '--status'],
        {
          cwd: path.dirname(config.workflowSettings),
          env: statusEnv(),
          encoding: 'utf8',
          timeout: config.statusTimeoutMs,
          maxBuffer: 256 * 1024,
          windowsHide: true,
        },
        (error, stdout) => {
          resolve(parseStatus({
            error: error || null,
            status: error ? -1 : 0,
            stdout,
          }));
        },
      );
    } catch (error) {
      resolve({ readable: false, enabled: false, configuredEnabled: false, error: safeMessage(error) });
    }
  });
}

function resolveProject(value) {
  if (typeof value !== 'string' || !value.trim() || value.includes('\0')) return null;
  if (!fullyQualified(value)) return null;
  let real;
  try {
    const realpath = fs.realpathSync.native || fs.realpathSync;
    real = realpath(value);
  } catch {
    return null;
  }
  try {
    if (!fs.statSync(real).isDirectory()) return null;
  } catch {
    return null;
  }
  return real;
}

function fullyQualified(value) {
  if (!path.isAbsolute(value)) return false;
  if (process.platform !== 'win32') return true;
  // Windows rooted paths such as \\project use the current drive, which is
  // not native project identity. Require a drive root or an explicit UNC root.
  const root = path.win32.parse(value).root;
  return Boolean(root && root !== '\\' && root !== '/');
}

function validNativeSessionId(value) {
  return typeof value === 'string'
    && value.length > 0
    && value.length <= 300
    && value === value.trim()
    && !/[\0\r\n]/.test(value);
}

function nativeBinding(agent, ownerPrefix) {
  if (!agent || typeof agent !== 'object') return null;
  const session = agent.session;
  if (!session || typeof session !== 'object') return null;
  const header = session.header;
  if (!header || typeof header !== 'object') return null;

  const id = agent.id;
  if (!validNativeSessionId(id)) return null;
  if (session.id !== id) return null;
  if (header.id !== id) return null;

  const project = resolveProject(header.cwd);
  if (!project) return null;

  const digest = crypto
    .createHash('sha256')
    .update(`model-desk-dsh-brain\0${id}`, 'utf8')
    .digest('hex');
  return Object.freeze({
    id,
    project,
    // The native session id is declared, not host-verified. The stable digest
    // is the explicit user-bound operator session passed to brain_mcp_server.
    operator: `dshb-${digest.slice(0, 32)}`,
    owner: `${ownerPrefix}-${digest.slice(0, 16)}`,
  });
}

function sameIdentity(left, right) {
  return Boolean(
    left
    && right
    && left.id === right.id
    && left.project === right.project
    && left.owner === right.owner
    && left.operator === right.operator,
  );
}

function verifyBinding(state, binding) {
  if (binding.disposed || binding.invalid) return false;
  if (!isTopLevel(state.ctx, binding.agent)) return false;
  const current = nativeBinding(binding.agent, state.config.ownerPrefix);
  return sameIdentity(current, binding.identity);
}

function publicToolName(serverName, rawName) {
  return `mcp__${serverName}__${rawName}`;
}

function rawBrainTool(name, serverName) {
  if (typeof name !== 'string') return null;
  const prefix = `mcp__${serverName}__`;
  const raw = name.startsWith(prefix) ? name.slice(prefix.length) : name;
  return BRAIN_TOOL_SET.has(raw) ? raw : null;
}

function buildRules(config, identity) {
  const prefix = `mcp__${config.serverName}__`;
  return [
    '## 模型工作台主脑协作规则',
    `新会话首次响应任何用户消息之前，第一项为 ${prefix}brain_preflight；它一次返回归属、总开关、范围和候选，不以编程、创作或闲聊分类跳过。后续新执行需求每轮预检一次。`,
    '首次预检结束后，在给用户的第一条可见回复开头单独一行原样展示 user_signal.text，然后继续处理需求。不能先说“已就绪”再查；没有预检成功结果就显示“🔴 协作未就绪｜”及真实原因，不凭插件已加载或开关已开猜测就绪。',
    '就绪信号仅确认本次连接/开关/责任绑定/执行候选，不代表已经派发、模型接口实测可达或任务已验收。只在首次响应展示一次；后续状态发生变化时再明确提示，不在每条回复重复刷屏。',
    '',
    `本聊天绑定的项目来自 DSH 原生会话 header.cwd（严格 realpath）：${identity.project}。`,
    '该绑定由插件按当前原生聊天固定，不接受工具参数、cwd、安装目录或最近项目改写。',
    '会话责任登记为 user_bound（用户安装插件并同意自动绑定当前聊天），不是 host_verified；不要声称宿主已核实身份。',
    '',
    '### 总开关',
    '- 是否允许新派发/整改由工作台总开关决定；插件在每次派发或整改工具调用前都会重新校验，关闭时这些调用会被拒绝。',
    '- 开关开启时，遇到需要执行的工作就按本规则自动使用框架，不需要用户先说固定口令；开关关闭时不要自动派发。',
    '- 开关关闭不会取消已有任务、不会改绑项目或会话；仍可读取原任务、取回交付和做验收/退回，但不要再自动派发或整改。',
    '',
    '### 职责',
    '- 你是主脑：判断风险、拆分工作、选择执行模型、独立验收。执行端负责调查、实现、测试和修正。',
    '- 执行器的候选声明、自报完成、自报测试和模型名称都不是独立证据；completed 不等于 accepted。',
    '- 选择 model_id 时遵循保存的执行 Agent 和候选范围；不要自行更改执行 Agent、预算、次数或 runtime 默认值。',
    '',
    '### 派发',
    `- 每条新用户需求先调用一次 ${prefix}brain_preflight；经本原生入口允许时它按需启动/复用工作台，不调用模型、不派发。失败明确报告，不能伪造连接或不断重试。`,
    '- 在计划或首次工作说明中简要写出“协作：开/关、范围、派发/自行处理及理由”。本轮已预检不重复预检；纯完成通知沿用原任务验收流程，不为验收重新派发。',
    '- 调查实现、较长命令处理、生成文件、创作素材、文案或模型等都应纳入执行工作评估；能力匹配且范围明确时优先派发。简单一步、独立验收、核心风险判断或执行端能力不足可自做，并说明理由。不要只凭“创作不是编程”跳过。',
    `- 需要核对时先用 ${prefix}brain_candidates 和 ${prefix}brain_route_preview；不会调用模型。`,
    '- 新任务选型以真实候选的 scope_ok、selectable_model_ids 与 default_model_note 为准；默认模型在范围外，不代表范围内没有模型。派发时明确填写已核对的 model_id，不能自行换模型或修改全局默认。',
    '- 主脑和执行模型可以同名、同 provider，例如 DeepSeek 主脑派给独立 DeepSeek 执行会话；这不是执行器嵌套子代理，subagent deny 不排除普通执行候选。',
    `- 派发时使用稳定 request_id，并通过 ${prefix}brain_dispatch 快速取得原任务 ID。工具参数不能修改项目、角色、拥有者或会话。`,
    '- 同一 request_id 同内容会取回原任务，不同内容会被拒绝。结果未知或重复时保持原 request/task，不要换 ID 重发。',
    `- 派发回执丢失时用 ${prefix}brain_request_status 按原 request_id 只读核对；未查到不代表未执行，不换 ID 重派。`,
    `- 完成本批派发后调用 ${prefix}brain_wait（action=start，task_ids 为全部原任务编号）集中订阅一次；同一聊天不要同时订阅多个批次，不要反复轮询 status，不要周期性轮询主脑模型。`,
    '- 只有返回 waiting=true 才告诉用户“正在等待执行模型完成，完成后会自动回来验收”，然后结束本轮。监听启动失败或结果未知就保留原 task/wait，不重派、不自动重试。',
    `- 收到固定完成事件后先告知“执行已结束，正在独立验收”，用 ${prefix}brain_wait（action=received，原 wait_id）登记，再读取 status/delivery 并检查实际成果。执行完成、通知送达和验收通过是三个不同状态。`,
    '- 用户可在普通任务等待期间继续发消息；完成通知等待原聊天空闲再投递。关闭协作、关闭聊天或重启 DSH 后不保证唤醒；失败/取消/待恢复如实报告，不重新派发。status 只用于用户询问/诊断，stop 只停通知，不取消执行任务。',
    '- 仅用户明确要求诊断时使用 brain_wakeup_probe。start 确认监听后说明约30秒等待、请用户暂不发消息或关闭DSH，90秒未收到可询问状态；然后结束本轮。收到探针后先 received 确认，仅回复已收到；探针通过不代表真实任务闭环已验收。',
    '',
    '### 验收与整改',
    '- 每条验收条件都需要主脑独立证据（reviewer_file / reviewer_test / reviewer_inspection）；执行器自报不能单独让条件通过。',
    `- 用 ${prefix}brain_delivery 取回真实交付和证据状态，再用 ${prefix}brain_review 记录逐条结论；accept/final_accept 前检查实际文件、测试和边界。`,
    '- 未通过就 return；最多一次整改，整改保持原模型和执行器，由后端使用新会话。重复 review/rework 不得重复消耗整改次数。',
    '- 重复、未知或归属不清时保持原 request/task，不要自动收养旧任务或改写原责任绑定。',
  ].join('\n');
}

function makeBinding(state, agent, identity) {
  const binding = {
    state,
    agent,
    agentCtx: agent.ctx,
    identity,
    mounted: false,
    invalid: false,
    disposed: false,
    disposePromise: null,
    mountError: '',
    ruleDisposer: null,
    ruleActive: false,
    ruleError: '',
    ruleQueue: Promise.resolve(),
    desiredRules: false,
    guardDisposer: null,
    preStepDisposer: null,
    activationDisposer: null,
    mcpFiber: null,
    mountPromise: null,
    lastStatus: null,
  };

  function ruleText(context) {
    if (!context || context.agent !== binding.agent) return '';
    if (!binding.ruleActive || binding.invalid || binding.disposed || !binding.mounted) return '';
    return buildRules(state.config, binding.identity);
  }

  function applyRuleState() {
    if (binding.disposed) return;
    const want = Boolean(binding.desiredRules && binding.mounted && !binding.invalid && !binding.disposed);
    if (want && !binding.ruleDisposer) {
      try {
        const disposer = binding.agentCtx.systemPrompt.section({
          name: RULE_SECTION,
          order: RULE_ORDER,
          text: ruleText,
          interpolate: false,
        });
        if (typeof disposer !== 'function') throw new Error('systemPrompt.section 未返回 disposer');
        binding.ruleDisposer = disposer;
        binding.ruleActive = true;
        binding.ruleError = '';
      } catch (error) {
        binding.ruleDisposer = null;
        binding.ruleActive = false;
        binding.ruleError = safeMessage(error);
        state.log('warn', `主脑规则注册失败；工具仍按开关守卫并保持 fail closed：${binding.ruleError}`);
      }
    } else if (!want && binding.ruleDisposer) {
      const disposer = binding.ruleDisposer;
      binding.ruleDisposer = null;
      binding.ruleActive = false;
      try {
        disposer();
      } catch {
        // A stale section disposer is not a reason to allow dispatch.
      }
    }
  }

  binding.setRules = (enabled) => {
    binding.desiredRules = Boolean(enabled);
    const run = () => applyRuleState();
    binding.ruleQueue = binding.ruleQueue.then(run, run);
    return binding.ruleQueue;
  };

  // A guard is synchronous, so an explicit denial may remove the auto-dispatch
  // rule immediately instead of waiting for a queued rule update. The queue
  // still sees desiredRules=false and cannot re-add it.
  binding.disableRulesNow = () => {
    binding.desiredRules = false;
    if (!binding.ruleDisposer) {
      binding.ruleActive = false;
      return;
    }
    const disposer = binding.ruleDisposer;
    binding.ruleDisposer = null;
    binding.ruleActive = false;
    try {
      disposer();
    } catch {
      // The switch guard remains authoritative even if a stale section
      // disposer reported an error.
    }
  };

  return binding;
}

function mcpArgs(config, identity) {
  return [
    config.brainMcpServer,
    '--allow-service-start',
    '--project', identity.project,
    '--host', 'dsh',
    '--owner', identity.owner,
    '--role', 'brain',
    '--session-binding', 'user_bound',
    '--session-id', identity.operator,
    '--native-session-id', identity.id,
    '--launcher', 'dsh-brain-plugin',
    '--panel-url', config.panelUrl,
  ];
}

async function mountMcp(state, binding) {
  const moduleValue = await resolveMcpModule(state, binding.agentCtx);
  const config = state.config;
  const mcpConfig = {
    transport: 'stdio',
    serverName: config.serverName,
    command: config.python,
    args: mcpArgs(config, binding.identity),
    env: {
      PYTHONUTF8: '1',
      PYTHONIOENCODING: 'utf-8',
      PYTHONUNBUFFERED: '1',
      ...(binding.wakeup ? binding.wakeup.environment() : {}),
    },
    cwd: binding.identity.project,
    toolCallTimeoutMs: config.toolCallTimeoutMs,
    failOnStartupError: true,
  };
  if (typeof moduleValue.Config !== 'function') {
    throw new Error('安装版 MCP client 缺少 Config 校验接口');
  }
  // Direct ctx.plugin calls do not apply the loader's Config defaults. Match
  // the installed ACP adapter: validate/fill the transport config explicitly.
  const fiber = binding.agentCtx.plugin(moduleValue, moduleValue.Config(mcpConfig));
  try {
    const awaited = await Promise.resolve(fiber);
    if (awaited && (typeof awaited.dispose === 'function' || awaited.scope)) return awaited;
    return fiber;
  } catch (error) {
    if (fiber && fiber !== error) await disposeFiber(fiber);
    throw error;
  }
}

async function disposeFiber(fiber) {
  if (!fiber) return;
  if (typeof fiber.dispose !== 'function') throw new Error('MCP fiber 缺少 dispose 接口');
  await Promise.resolve(fiber.dispose());
  // Cordis may have claimed the raw disposer already. Its inertia still owns
  // asynchronous MCP shutdown; do not replace tools or drop guards before it.
  while (fiber.inertia !== undefined) await fiber.inertia;
}

async function disposeBinding(binding) {
  if (binding.disposed) return binding.disposePromise;
  binding.disposed = true;
  binding.invalid = true;
  binding.desiredRules = false;
  binding.disposePromise = (async () => {
    if(binding.wakeup)await binding.wakeup.dispose();
    try {
      await binding.ruleQueue;
    } catch {
      // A failed rule update already leaves the switch guard in force.
    }
    if (binding.ruleDisposer) {
      const disposer = binding.ruleDisposer;
      binding.ruleDisposer = null;
      binding.ruleActive = false;
      try {
        await Promise.resolve(disposer());
      } catch {
        // Continue disposing the remaining capabilities.
      }
    }
    // A mount already in flight belongs to this binding too. Waiting for it
    // here prevents a late server from surviving teardown without its guard.
    let fiber = binding.mcpFiber;
    if (binding.mountPromise) {
      try { fiber = await binding.mountPromise; } catch { /* failed startup */ }
    }
    try {
      await disposeFiber(fiber);
    } catch (error) {
      // Keep the invalid guard installed if shutdown cannot be established.
      binding.state.log('warn', 'MCP 卸载未确认，保留拒绝守卫，不复用此绑定。');
      throw error;
    }
    const disposers = [binding.activationDisposer, binding.preStepDisposer, binding.guardDisposer];
    binding.activationDisposer = null;
    binding.preStepDisposer = null;
    binding.guardDisposer = null;
    for (const disposer of disposers) {
      try {
        if (typeof disposer === 'function') await Promise.resolve(disposer());
      } catch {
        // Idempotent best effort; the agent scope owns the same registrations.
      }
    }
    const current = binding.state.bindings.get(binding.identity.id);
    if (current === binding) binding.state.bindings.delete(binding.identity.id);
  })();
  return binding.disposePromise;
}

function guardTool(binding, exec) {
  try {
    const serverName = binding.state.config.serverName;
    const raw = rawBrainTool(exec && exec.name, serverName);
    // Never block normal DSH tools: this guard owns only this server's tools.
    if (!raw) return undefined;

    if (executorMarker()) {
      return '当前进程带有 MODEL_DESK 执行器标记，主脑工具全部拒绝。';
    }
    if (binding.disposed || binding.invalid) {
      return '主脑绑定已失效；拒绝全部主脑工具调用。';
    }
    // The scope-inherited guard is the final instance check: child Agents and
    // any other Agent instance do not get this bound brain's capability.
    if (!exec || exec.agent !== binding.agent) {
      return '主脑工具只允许所属顶层主脑实例使用；子 Agent 或其他会话无权调用。';
    }
    if (!verifyBinding(binding.state, binding)) {
      binding.invalid = true;
      binding.disableRulesNow();
      return '原生会话身份或项目已漂移；主脑绑定保持原归属并拒绝全部主脑工具。';
    }
    if (MUTATING_TOOLS.has(raw) || (['brain_wakeup_probe','brain_wait'].includes(raw)&&(exec.args??exec.arguments)?.action==='start')) {
      if (binding.ruleError) return '主脑协作规则未就绪，拒绝新的派发或整改。';
      // Re-read the existing workflow_settings --status on every mutation. The
      // backend validates again; this guard never trusts a cached allow flag.
      const status = readStatusSync(binding.state.config);
      binding.lastStatus = status;
      if (!status.readable) {
        binding.disableRulesNow();
        return '协作总开关不可读；按关闭处理，不能派发或整改新任务。';
      }
      if (!status.enabled) {
        binding.disableRulesNow();
        return '协作总开关已关闭；不能派发或整改新任务。已有任务仍可读取和验收。';
      }
    }
    return undefined;
  } catch (error) {
    binding.invalid = true;
    binding.disableRulesNow();
    return `主脑权限校验失败，已拒绝本次调用（${safeMessage(error)}）。`;
  }
}

function onPreStep(binding, payload, next) {
  const run = async () => {
    let decision;
    // Preserve the host's decision and never reject ordinary chat just because
    // the collaboration switch is off. Enforcement lives in tools.guard.
    try {
      if (!payload || payload.agent !== binding.agent) return typeof next === 'function' ? next() : decision;
      if (binding.disposed || (payload && payload.signal && payload.signal.aborted)) return typeof next === 'function' ? next() : decision;
      if (executorMarker()) {
        binding.invalid = true;
        binding.disableRulesNow();
        return typeof next === 'function' ? next() : decision;
      }
      if (!verifyBinding(binding.state, binding)) {
        binding.invalid = true;
        binding.disableRulesNow();
        binding.state.log('warn', '原生会话身份或项目漂移；保留原绑定并停止主脑能力。');
        return typeof next === 'function' ? next() : decision;
      }
      // Refresh rule visibility at the start of a turn (or when new input was
      // claimed). Every dispatch/rework still performs its own synchronous
      // status read in guardTool(), so this is not an allow cache.
      const step = payload && payload.step;
      const hasNewInput = Boolean(payload && Array.isArray(payload.messages) && payload.messages.length > 0);
      if (step === undefined || step === 1 || hasNewInput) {
        const status = await readStatus(binding.state.config);
        binding.lastStatus = status;
        await binding.setRules(status.enabled && binding.mounted);
      }
    } catch (error) {
      binding.disableRulesNow();
      binding.state.log('warn', `主脑 pre-step 校验失败，规则按关闭处理：${safeMessage(error)}`);
    }
    return typeof next === 'function' ? next() : decision;
  };
  return run();
}

async function installBinding(state, agent, identity) {
  if (state.disposed) return;
  let record = state.records.get(identity.id);
  if (!record) {
    record = identity;
    state.records.set(identity.id, record);
  } else if (!sameIdentity(record, identity)) {
    state.log('warn', '当前原生会话与首次绑定不一致；不自动改归属，保持原绑定。');
    return;
  }
  if (state.bindings.has(identity.id)) {
    state.log('warn', '同一原生会话已有活动的插件绑定；不重复挂载。');
    return;
  }

  const binding = makeBinding(state, agent, record);
  state.bindings.set(identity.id, binding);
  try {
    if(typeof agent.followup==='function'){
      try{
        binding.wakeup=createBridge({agent,identity:record,
          isCurrent:()=>!binding.disposed&&!binding.invalid&&verifyBinding(state,binding)
            &&isTopLevel(state.ctx,agent),
          enabled:()=>{const status=readStatusSync(state.config);return status.readable&&status.enabled;}});
        await binding.wakeup.start();
      }catch(_){
        if(binding.wakeup)await binding.wakeup.dispose();
        binding.wakeup=null;
        state.log('warn','原聊天唤醒探针通道未就绪；普通主脑工具保持可用。');
      }
    }
    binding.guardDisposer = agent.ctx.tools.guard((exec) => guardTool(binding, exec));
    binding.preStepDisposer = agent.ctx.on(
      'agent/pre-step',
      (payload, next) => onPreStep(binding, payload, next),
      { prepend: true },
    );
    binding.mountPromise = mountMcp(state, binding);
    binding.mcpFiber = await binding.mountPromise;
    if (binding.disposed || state.disposed) return;
    binding.mounted = true;
    binding.lastStatus = await readStatus(state.config);
    await binding.setRules(binding.lastStatus.enabled);
    // Runtime context is refreshed by the installed DSH loop after pre-step;
    // ordinary prompt sections were assembled before that hook. Keep the
    // short activation fact next to the current input, not only in long rules.
    if (typeof agent.ctx.systemPrompt.context === 'function') {
      binding.activationDisposer = agent.ctx.systemPrompt.context({
        name: 'model-desk-collaboration-activation', order: 9000, interpolate: false,
        text: (context) => {
          const status = binding.lastStatus;
          if (context?.agent !== agent || binding.disposed || binding.invalid || !binding.mounted) return '';
          if (!status?.readable) return '【协作状态提示】首次用户回复开头显示“🔴 协作未就绪｜无法读取协作开关”；不自动派发。后续仅状态变化时提示。';
          if (!status.enabled) return '【协作状态提示】首次用户回复开头显示“⚪ 协作已关闭｜本轮不自动派发”；不调用协作预检、不启动后台、不自动派发。后续仅状态变化时提示。';
          if (!binding.ruleActive) return '【协作状态提示】首次用户回复开头显示“🔴 协作未就绪｜协作规则未加载”；不声称已就绪，不自动派发。';
          return `【协作预检】总开关：开｜范围：${status.modelScope}｜项目：${record.project}\n`
            + `新会话首次响应任何用户消息前，先调用 mcp__${state.config.serverName}__brain_preflight 一次；后续新执行需求每轮一次。`
            + '首次预检成功后，在给用户的第一条可见回复开头单独一行原样展示 user_signal.text；失败显示“🔴 协作未就绪｜”及真实原因。预检之前不得声称已就绪。后续仅状态变化时提示。'
            + '取得真实候选后决定派发或说明自做理由；创作/生成文件也属于执行评估范围。'
            + '本轮已有预检结果不重复调用；只接收原任务完成通知时直接走独立验收，不重新派发。';
        },
      });
    }
  } catch (error) {
    binding.mountError = safeMessage(error);
    state.log('warn', `主脑 MCP/规则初始化失败，按不可用处理，不启动工作台：${binding.mountError}`);
    void binding.setRules(false);
    if (!binding.disposed) await disposeBinding(binding);
  }
}

async function onCreated(state, payload) {
  if (state.disposed) return;
  const agent = payload && payload.agent;
  if (!agent) return;
  try {
    if (executorMarker()) {
      state.log('warn', '检测到 MODEL_DESK 执行器标记；不安装任何主脑规则或工具。');
      return;
    }
    if (!isTopLevel(state.ctx, agent)) return;
    if (!agentHostReady(agent)) {
      state.log('warn', 'Agent 缺少所需的原生作用域 API（tools.guard/systemPrompt.section/on/effect/plugin）；按不可用处理。');
      return;
    }
    const identity = nativeBinding(agent, state.config.ownerPrefix);
    if (!identity) {
      state.log('warn', '原生会话身份或项目不完整/不合法；不猜测绑定，保持未绑定。');
      return;
    }
    const existing = state.bindings.get(identity.id);
    if (existing) {
      if (existing.agent === agent) return;
      if (!existing.disposed) {
        if (isTopLevel(state.ctx, existing.agent)) {
          state.log('warn', '同一原生会话仍绑定在另一个活动 Agent；不重复挂载。');
          return;
        }
        // The old Agent is no longer a live root but its disposed event has not
        // arrived yet. Release it before binding the resumed instance.
        try {
          await disposeBinding(existing);
        } catch {
          return; // Teardown is unknown; do not replace that capability.
        }
      }
      // A resumed session may reuse the id after the old Agent was disposed.
      // Wait for its exact teardown, then bind the new instance to the same
      // immutable first-binding record. A project/identity change still fails
      // closed inside installBinding().
      try {
        await existing.disposePromise;
      } catch {
        return;
      }
      if (state.bindings.get(identity.id) === existing) state.bindings.delete(identity.id);
    }
    if (state.pending.has(identity.id)) {
      await state.pending.get(identity.id);
      return;
    }
    if (state.disposed) return;
    const work = installBinding(state, agent, identity);
    state.pending.set(identity.id, work);
    try {
      await work;
    } finally {
      state.pending.delete(identity.id);
    }
  } catch (error) {
    // A plugin failure must never reject native Agent creation.
    state.log('warn', `主脑插件绑定失败：${safeMessage(error)}`);
  }
}

async function onDisposed(state, payload) {
  const agent = payload && payload.agent;
  if (!agent || !state.bindings.has(agent.id)) return;
  const binding = state.bindings.get(agent.id);
  if (binding && binding.agent === agent) {
    try {
      await disposeBinding(binding);
    } catch {
      // The Agent scope is already gone; top-level cleanup remains.
    }
  }
}

function ensureWorkbench(config) {
  return new Promise((resolve, reject) => {
    execFile(config.python, [path.join(path.dirname(config.brainMcpServer), 'panel_service.py'),
      '--ensure', '--panel-url', config.panelUrl], {
      cwd: path.dirname(config.brainMcpServer), encoding: 'utf8',
      timeout: 50_000, maxBuffer: 256 * 1024, windowsHide: true,
    }, (error, stdout) => {
      let result;
      try { result = JSON.parse(String(stdout || '').trim()); } catch {}
      if (error || !result?.ready) reject(new Error(result?.error || '工作台启动未确认；不自动重试或停止已有服务。'));
      else resolve(result);
    });
  });
}

function createWorkbenchHandler(ctx, config, ensure = ensureWorkbench) {
  let opening = null;
  return async (endpoint, payload) => {
    try {
      if (endpoint !== 'open' || !payload || typeof payload !== 'object' || Array.isArray(payload)
          || Object.keys(payload).some(key => key !== 'sessionId')) throw new Error('工作台打开参数无效');
      let project = null;
      let sourceSession = null;
      let sourceAgent = null;
      if (payload.sessionId !== null) {
        if (!validNativeSessionId(payload.sessionId)) throw new Error('原生会话标识无效');
        const agent = ctx.agents.get(payload.sessionId);
        if (agent && !isTopLevel(ctx, agent)) throw new Error('不是顶层原生聊天');
        const session = agent?.session || ctx.sessions?.get?.(payload.sessionId);
        if (!session || session.id !== payload.sessionId || session.header?.id !== payload.sessionId)
          throw new Error('当前原生会话尚未就绪；不猜测项目');
        project = resolveProject(session.header.cwd);
        if (!project) throw new Error('当前原生会话项目不可核实；不沿用其他项目');
        sourceSession = session;
        sourceAgent = agent;
      }
      if (!opening) opening = ensure(config).finally(() => {opening = null;});
      const result = await opening;
      if (sourceSession && (sourceSession.header?.id !== payload.sessionId
          || resolveProject(sourceSession.header.cwd) !== project
          || (sourceAgent && (ctx.agents.get(payload.sessionId) !== sourceAgent || !isTopLevel(ctx, sourceAgent)))))
        throw new Error('启动期间原生会话或项目已变化；未打开旧目标，请手动重试');
      const url = new URL('/', config.panelUrl);
      url.searchParams.set('host', 'dsh');
      if (project) url.searchParams.set('project', project);
      else url.searchParams.set('view', 'unbound');
      return {ok: true, value: {url: url.toString(), project, started: result.started === true}};
    } catch (error) {
      return {ok: false, error: {code: 'model-desk/open-unavailable', message: safeMessage(error)}};
    }
  };
}

function workbenchFetchRoute(handler) {
  // Desktop's owned transport forwards /api, not arbitrary Host channels.
  // Exact Fetch routes run behind Connection's existing trust/auth admission;
  // do not replace the API Gateway's sole shared RPC interceptor.
  return {path: '/api/model-desk-dsh-brain/open', methods: ['POST'], requestBody: 'buffered',
    async fetch(request) {
      let message;
      try { message = await request.json(); } catch { return new Response('invalid JSON', {status: 400}); }
      if (!message || message.type !== 'client-request' || typeof message.rpcId !== 'string'
          || message.method !== 'model-desk-dsh-brain/open')
        return new Response('invalid workbench RPC envelope', {status: 400});
      const result = await handler('open', message.payload);
      if (!result.ok) result.error.details = {};
      return Response.json({type: 'server-response', rpcId: message.rpcId, result});
    }};
}

function createInstance(ctx, config) {
  const state = {
    ctx,
    config,
    records: recordsFor(ctx),
    bindings: new Map(),
    pending: new Map(),
    disposed: false,
    disposePromise: null,
    createdDisposer: null,
    disposedDisposer: null,
    cleanupDisposer: null,
    modulePromise: null,
    account: null,
    openDisposer: null,
    openInjection: null,
    log: (level, message) => logWith(ctx, level, message),
  };

  async function disposeInstance(fromEffect = false) {
    // The effect cleanup is also called by an outer dispose. In that case the
    // outer promise already owns teardown; returning the outer promise from the
    // cleanup would deadlock, so the re-entrant call is a no-op.
    if (fromEffect && state.disposed) return undefined;
    if (state.disposed) return state.disposePromise;
    state.disposed = true;
    state.disposePromise = (async () => {
      let teardownFailed = false;
      if (state.account) {
        // Account display is independent of MCP ownership.  Clearing it first
        // makes the workbench mark the old amount unavailable even if an MCP
        // shutdown below is not confirmed.
        try {
          await state.account.dispose();
        } catch {
          // The panel freshness limit still prevents an old amount from being
          // rendered as current after the adapter is gone.
        }
        state.account = null;
      }
      for (const binding of [...state.bindings.values()]) {
        try {
          await disposeBinding(binding);
        } catch {
          teardownFailed = true;
        }
      }
      state.bindings.clear();
      state.pending.clear();
      if (typeof state.openDisposer === 'function') await Promise.resolve(state.openDisposer());
      if (state.openInjection?.dispose) await state.openInjection.dispose();
      state.openDisposer = null;
      state.openInjection = null;
      const disposers = [state.createdDisposer, state.disposedDisposer];
      state.createdDisposer = null;
      state.disposedDisposer = null;
      for (const disposer of disposers) {
        try {
          if (typeof disposer === 'function') await Promise.resolve(disposer());
        } catch {
          // Listener removal is idempotent.
        }
      }
      if (!fromEffect && typeof state.cleanupDisposer === 'function') {
        const disposer = state.cleanupDisposer;
        state.cleanupDisposer = null;
        try {
          await Promise.resolve(disposer());
        } catch {
          // The effect cleanup is idempotent and re-enters as a no-op.
        }
      } else {
        state.cleanupDisposer = null;
      }
      if (teardownFailed) throw new Error('MCP 卸载未确认；主脑工具保持拒绝，重启宿主后再加载插件');
    })();
    return state.disposePromise;
  }

  return {
    async start() {
      const registerOpen = (rpcCtx) => {
        if (state.disposed || state.openDisposer) return;
        const connection = rpcCtx.connection;
        if (typeof connection?.fetch?.register === 'function') {
          const dispose = connection.fetch.register(workbenchFetchRoute(createWorkbenchHandler(rpcCtx, config)));
          state.openDisposer = dispose;
          rpcCtx.effect(() => () => {
            if (state.openDisposer === dispose) state.openDisposer = null;
          }, 'model-desk-dsh-brain: open route dependency lifetime');
        }
      };
      try {
        // Cordis rejects service reads outside the declared injection scope.
        // Even an optional property read (ctx.connection?.fetch) can prevent us
        // from reaching deferred registration, regardless of startup order.
        // Always let injection own acquisition/release; no WebServer is needed
        // to register the carrier-neutral exact API route.
        if (typeof ctx.inject !== 'function') throw new Error('原生依赖注入 API 不可用');
        state.openInjection = ctx.inject(['connection', 'agents', 'sessions'], registerOpen);
      } catch (error) {
        state.log('warn', `工作台原生打开通道不可用；保留主脑 MCP：${safeMessage(error)}`);
      }
      state.createdDisposer = ctx.on('agent/created', (payload) => onCreated(state, payload));
      state.disposedDisposer = ctx.on('agent/disposed', (payload) => onDisposed(state, payload));
      state.cleanupDisposer = ctx.effect(function* cleanupEffect() {
        yield () => disposeInstance(true);
      });

      // This reads only the official host account controller's public balance
      // projection and pushes it to the already-running local workbench.  It
      // starts BEFORE any MCP initialization await. A slow Agent binding must
      // not prevent account discovery/publication; neither path waits for the other.
      try {
        state.account = hostAccount.createAccountAdapter({
          ctx,
          panelUrl: config.panelUrl,
          log: state.log,
        });
        state.account.start().catch(() => {
          state.log('warn', 'DSH 账户余额适配器未启动（原始诊断已省略）。');
        });
      } catch {
        state.account = null;
        state.log('warn', 'DSH 账户余额适配器不可用（原始诊断已省略）。');
      }

      let roots;
      try {
        roots = ctx.agents.roots();
      } catch (error) {
        throw new Error(`ctx.agents.roots() 不可用：${safeMessage(error)}`);
      }
      if (!Array.isArray(roots)) throw new Error('ctx.agents.roots() 未返回数组');
      // Existing chats are bound too; concurrent created events still dedupe.
      for (const agent of roots) await onCreated(state, { agent, source: 'plugin-load' });
    },
    dispose() {
      return disposeInstance(false);
    },
  };
}

async function apply(ctx, rawConfig) {
  if (!ctx || typeof ctx !== 'object') return;
  const previous = liveInstances.get(ctx);
  if (previous) {
    try {
      await previous.dispose();
    } catch {
      logWith(ctx, 'warn', '旧插件卸载未确认；拒绝重复加载，请重启宿主。');
      return;
    }
  }
  const log = (level, message) => logWith(ctx, level, message);

  const marker = executorMarker();
  if (marker) {
    log('warn', `检测到执行器标记 ${marker}；插件不安装主脑规则或工具。`);
    return;
  }
  if (!hostReady(ctx)) {
    log('warn', 'DSH 宿主缺少 agents/on/effect API；插件保持 disabled（fail closed）。');
    return;
  }

  let config;
  try {
    config = loadConfig(rawConfig);
  } catch (error) {
    log('warn', `插件配置无效，保持 disabled（fail closed）：${safeMessage(error)}`);
    return;
  }

  const instance = createInstance(ctx, config);
  liveInstances.set(ctx, instance);
  try {
    await instance.start();
  } catch (error) {
    log('warn', `插件启动失败，保持 disabled（fail closed）：${safeMessage(error)}`);
    try {
      await instance.dispose();
    } catch {
      // Nothing else to release.
    }
    if (liveInstances.get(ctx) === instance) liveInstances.delete(ctx);
  }
}

module.exports.name = PLUGIN_NAME;
module.exports.inject = ['agents'];
module.exports.apply = apply;
// Exported for offline tests only; DSH itself never needs these.
module.exports._test = Object.freeze({
  BRAIN_TOOLS,
  RULE_SECTION,
  RULE_ORDER,
  buildRules,
  loadConfig,
  nativeBinding,
  parseStatus,
  publicToolName,
  rawBrainTool,
  resolveProject,
  createWorkbenchHandler,
  workbenchFetchRoute,
});
