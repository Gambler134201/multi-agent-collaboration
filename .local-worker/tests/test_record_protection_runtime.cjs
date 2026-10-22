'use strict';
/**
 * Real-DSH offline runtime test for the workbench-record write guard.
 *
 * It drives the *installed* DSH through the production runner
 * (`agent-runtime/run-dsh.mjs`) against a local fake OpenAI-compatible provider
 * (no real LLM inference, no paid calls).  The production patch is rendered by
 * the same Python code the worker uses, so the plugin is mounted and loaded
 * exactly as in a real task.
 *
 * Proves, with actual tool calls:
 *   - a `write`/`edit` into `.model-desk` is refused before the tool body runs
 *     and the original record bytes do not change;
 *   - a terminal command that targets `.model-desk` is refused;
 *   - a junction alias into `.model-desk` is refused;
 *   - normal source, cache cleanup and formatting work before a refusal;
 *   - after refusal, cross-tool mutations stop and direct reads still work;
 *   - the trusted DSH preset bridge still writes its proof into the protected
 *     record tree (the workbench's own records keep growing);
 *   - a stale/unknown protection identity refuses to start the turn at all.
 *
 * Run with:  node .local-worker/tests/test_record_protection_runtime.cjs
 */
const { spawn, spawnSync, execFileSync } = require('child_process');
const crypto = require('crypto');
const fs = require('fs');
const http = require('http');
const os = require('os');
const path = require('path');

const HERE = path.dirname(__filename);
const ROOT = path.dirname(HERE);
const PYTHON = process.env.MODEL_DESK_TEST_PYTHON || path.join(ROOT, 'venv', 'Scripts', 'python.exe');
const RUNNER = path.join(ROOT, 'agent-runtime', 'run-dsh.mjs');

const failures = [];
let checks = 0;
function check(name, body) {
  try {
    body();
    checks += 1;
    console.log('ok   - ' + name);
  } catch (error) {
    failures.push(name);
    console.log('FAIL - ' + name + ' :: ' + (error && error.message));
  }
}

const HELPER = [
  'import json, sys',
  'from pathlib import Path',
  'req = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))',
  'sys.path.insert(0, req["root"])',
  'import dsh_worker',
  'import preset_runtime',
  'import record_protection',
  'import model_registry as registry',
  'import yaml',
  'mode = req["mode"]',
  'project = Path(req["project"])',
  'run = Path(req["run"])',
  'if mode == "render":',
  '    model = {"id": "record-probe", "name": "record probe", "provider": req["provider"],',
  '             "model": "record-probe", "agent_preset": req["preset"], "api_base": req["api_base"],',
  '             "api_key": "", "bypass_proxy": True, "max_input_tokens": 32768, "max_output_tokens": 1024}',
  '    preset = preset_runtime.load(model, dsh_worker.EXECUTABLE)',
  '    patch = dsh_worker.build_patch(model, req["kind"], preset, record_root=project, access=req.get("access", "full"))',
  '    patch[3]["config"]["workspaceRoot"] = str(project)',
  '    if req.get("extra_plugin"):',
  '        patch.append({"insert": [{"name": req["extra_plugin"], "config": req.get("extra_config") or {}}]})',
  '    Path(req["patch"]).write_text(yaml.dump(patch, Dumper=preset_runtime.Dumper, allow_unicode=True, sort_keys=False), encoding="utf-8")',
  '    job = dict(dsh_worker.availability())',
  '    job.update({"access_mode": req.get("access", "full"), "task_id": req["task_id"], "kind": req["kind"], "model": model["model"], "cwd": str(project),',
  '                "patch": req["patch"], "state_dir": req["state_dir"], "session_key": req["session_key"],',
  '                "prompt_file": req["prompt"], "cancel_file": req["cancel"], "timeout": 0,',
  '                "record_proof": req["record_proof"], "record_revision": req.get("revision_override") or record_protection.revision(),',
  '                "record_root": str(project), "subagent_ledger": None})',
  '    registry.write_json(Path(req["job"]), job)',
  '    print(json.dumps({"preset": preset["id"], "record_revision": record_protection.revision()}))',
  'elif mode == "verify":',
  '    proof = record_protection.verify_proof(run, project, req.get("session_id") or None)',
  '    print(json.dumps(proof, ensure_ascii=False))',
  'elif mode == "control":',
  '    registry.write_json(run / "task.json", {"id": req["task_id"], "status": "running", "note": "control-plane write"})',
  '    (run / "dsh.log").write_text("control-plane log line\\n", encoding="utf-8")',
  '    print(json.dumps(registry.read_task(run), ensure_ascii=False))',
].join('\n');

function digest(file) {
  return crypto.createHash('sha256').update(fs.readFileSync(file)).digest('hex');
}

function helper(request) {
  const file = path.join(request.sandbox, 'request-' + request.mode + '.json');
  fs.writeFileSync(file, JSON.stringify(request));
  const done = spawnSync(PYTHON, ['-c', HELPER, file], {
    encoding: 'utf8', env: { ...process.env, PYTHONIOENCODING: 'utf-8' },
  });
  if (done.status !== 0) {
    throw new Error('python helper ' + request.mode + ' failed: ' + (done.stderr || done.stdout));
  }
  return done.stdout.trim();
}

function textOf(content) {
  if (typeof content === 'string') return content;
  if (Array.isArray(content)) return content.map(textOf).join('\n');
  if (content && typeof content === 'object') return textOf(content.text || content.content || '');
  return '';
}

/** Local fake OpenAI-compatible provider; returns the planned tool calls. */
function startProvider(steps) {
  const captured = [];
  const server = http.createServer((request, response) => {
    let raw = '';
    request.on('data', (chunk) => { raw += chunk; });
    request.on('end', () => {
      let body = {};
      try { body = JSON.parse(raw); } catch (error) { body = {}; }
      captured.push(body);
      const index = captured.length - 1;
      const step = index < steps.length ? steps[index] : null;
      const chunks = [];
      if (step) {
        chunks.push({ id: 'record-probe', choices: [{ index: 0, delta: { role: 'assistant', tool_calls: [
          { index: 0, id: step.id, type: 'function', function: { name: step.name, arguments: JSON.stringify(step.args) } }] }, finish_reason: null }] });
        chunks.push({ id: 'record-probe', choices: [{ index: 0, delta: {}, finish_reason: 'tool_calls' }],
          usage: { prompt_tokens: 10, completion_tokens: 5, total_tokens: 15 } });
      } else {
        chunks.push({ id: 'record-probe', choices: [{ index: 0, delta: { role: 'assistant', content: 'RECORD_PROTECTION_PROBE_FINISHED' }, finish_reason: null }] });
        chunks.push({ id: 'record-probe', choices: [{ index: 0, delta: {}, finish_reason: 'stop' }],
          usage: { prompt_tokens: 10, completion_tokens: 5, total_tokens: 15 } });
      }
      response.writeHead(200, { 'Content-Type': 'text/event-stream' });
      for (const chunk of chunks) response.write('data: ' + JSON.stringify(chunk) + '\n\n');
      response.write('data: [DONE]\n\n');
      response.end();
    });
  });
  return new Promise((resolve) => {
    server.listen(0, '127.0.0.1', () => resolve({ server, captured, port: server.address().port }));
  });
}

function toolResults(captured) {
  const results = new Map();
  for (const body of captured) {
    for (const message of body.messages || []) {
      if (message && message.role === 'tool' && message.tool_call_id) {
        results.set(message.tool_call_id, textOf(message.content));
      }
    }
  }
  return results;
}

function makeSandbox(taskId) {
  const sandbox = fs.mkdtempSync(path.join(os.tmpdir(), 'md-record-runtime-'));
  const project = path.join(sandbox, 'project');
  const run = path.join(project, '.model-desk', 'runs', taskId);
  fs.mkdirSync(run, { recursive: true });
  fs.mkdirSync(path.join(project, 'src'), { recursive: true });
  fs.mkdirSync(path.join(project, '.model-desk-backup'), { recursive: true });
  fs.mkdirSync(path.join(project, 'src', '__pycache__'));
  fs.writeFileSync(path.join(project, 'src', '__pycache__', 'discard.txt'), 'cache');
  fs.writeFileSync(path.join(project, '.model-desk', 'records.db'), 'REAL-RECORD-BYTES');
  fs.writeFileSync(path.join(run, 'task.json'), '{"id":"' + taskId + '","status":"queued"}');
  fs.writeFileSync(path.join(run, 'dsh-prompt.txt'), '记录保护验证：只执行指定操作。', 'utf8');
  let alias = null;
  try {
    alias = path.join(project, 'alias-records');
    fs.symlinkSync(path.join(project, '.model-desk'), alias, 'junction');
  } catch (error) {
    alias = null;
  }
  // Generate a minimal test profile; never copy user authentication/config.
  const home = path.join(sandbox, 'dsh-home');
  const profile = path.join(home, 'profiles', 'acp');
  fs.mkdirSync(profile, { recursive: true });
  fs.writeFileSync(path.join(profile, 'cordis.yml'), '[]\n');
  fs.writeFileSync(path.join(profile, 'cordis.patch.yml'), '[]\n');
  fs.writeFileSync(path.join(profile, 'package.json'), JSON.stringify({ name: 'dsh-profile-acp', private: true,
    dependencies: {}, dsh: { profile: { bundles: ['@deepseek-ai/dsh-base', '@deepseek-ai/dsh-acp-app'] } } }));
  fs.writeFileSync(path.join(profile, 'pnpm-workspace.yaml'), 'packages:\n  - .\nnodeLinker: hoisted\nautoInstallPeers: false\n');
  fs.mkdirSync(path.join(sandbox, 'state'), { recursive: true });
  return { sandbox, project, run, home, alias, taskId };
}

async function runDsh(box, kind, apiBase, options) {
  const settings = options || {};
  const request = {
    mode: 'render', root: ROOT, sandbox: box.sandbox, project: box.project, run: box.run,
    task_id: box.taskId, kind, access: settings.access || 'full', provider: settings.preset === 'native' ? 'local' : 'custom',
    preset: settings.preset, api_base: apiBase, state_dir: path.join(box.sandbox, 'state'),
    session_key: 'record-probe-' + box.taskId + '-' + kind,
    patch: path.join(box.sandbox, 'patch-' + kind + '.yml'),
    job: path.join(box.sandbox, 'job-' + kind + '.json'),
    prompt: path.join(box.run, 'dsh-prompt.txt'),
    cancel: path.join(box.run, 'cancelled.flag'),
    record_proof: path.join(box.run, 'record-proof.json'),
    revision_override: settings.revision_override || '',
  };
  helper(request);
  const started = Date.now();
  const launch = {
    cwd: box.project,
    env: {
      ...process.env,
      DSH_HOME: box.home,
      DSH_WORKER_API_KEY: 'record-probe-token',
      MODEL_DESK_PRESET_PROOF: path.join(box.run, 'preset-proof.json'),
      MODEL_DESK_RECORD_PROOF: request.record_proof,
      DSH_TELEMETRY_MODE: 'DISABLED',
      PYTHONIOENCODING: 'utf-8',
      NO_PROXY: '127.0.0.1,localhost',
      no_proxy: '127.0.0.1,localhost',
    },
  };
  // Keep the event loop available: the mock provider lives in this process.
  // spawnSync here deadlocks every real tool test until its timeout expires.
  const done = await new Promise((resolve, reject) => {
    const child = spawn(process.execPath, [RUNNER, request.job], launch);
    let stdout='', stderr='', expired=false;
    child.stdout.setEncoding('utf8');child.stderr.setEncoding('utf8');
    child.stdout.on('data', chunk => { stdout += chunk; });
    child.stderr.on('data', chunk => { stderr += chunk; });
    const timer=setTimeout(() => {
      expired=true;
      // Only this test's own live child tree is stopped on a test deadline.
      try {
        if(process.platform==='win32')execFileSync('taskkill',['/PID',String(child.pid),'/T','/F'],{stdio:'ignore'});
        else child.kill('SIGKILL');
      } catch(error) { child.kill(); }
    },60000);
    child.on('error', error => {clearTimeout(timer);reject(error);});
    child.on('close', (status,signal) => {
      clearTimeout(timer);
      resolve({status,signal,stdout,stderr,error:expired?new Error('isolated runtime test timed out'):null});
    });
  });
  done.elapsedMs = Date.now() - started;
  return { request, done };
}

const recordRel = path.join('.model-desk', 'records.db');
const backupRel = path.join('.model-desk-backup', 'keep.txt');
const sourceRel = path.join('src', 'ok.py');

async function editScenario(preset) {
  const taskId = 'record-runtime-edit-'+preset;
  const box = makeSandbox(taskId);
  const record = path.join(box.project, recordRel);
  const before = digest(record);
  const steps = [
    { id: 'step-read', name: 'read', args: { file_path: record } },
    { id: 'step-write-source', name: 'write', args: { file_path: path.join(box.project, sourceRel), content: 'print(2)\n' } },
    { id: 'step-write-backup', name: 'write', args: { file_path: path.join(box.project, backupRel), content: 'backup-ok' } },
    { id: 'step-format', name: 'pwsh', args: { command: 'Get-ChildItem src | Format-List', description: 'read source listing' } },
    { id: 'step-clean-cache', name: 'pwsh', args: { command: 'Remove-Item -LiteralPath src/__pycache__ -Recurse -Force; Get-ChildItem src | Select-Object Name', description: 'clean only test cache' } },
    { id: 'step-write-record', name: 'write', args: { file_path: record, content: 'HACKED-1' } },
    { id: 'step-edit-record', name: 'edit', args: { file_path: record, old_string: 'REAL-RECORD-BYTES', new_string: 'HACKED-2' } },
    { id: 'step-write-new-record', name: 'write', args: { file_path: path.join(box.run, 'forged-record.json'), content: '{}' } },
    { id: 'step-pwsh-delete', name: 'pwsh', args: { command: 'Remove-Item -Recurse -Force .model-desk', description: 'delete workbench records' } },
    { id: 'step-pwsh-write', name: 'pwsh', args: { command: 'Set-Content .model-desk\\records.db HACKED-3', description: 'overwrite workbench records' } },
    { id: 'step-pwsh-glob', name: 'pwsh', args: { command: 'Remove-Item -Recurse -Force .model*', description: 'wildcard cleanup' } },
    { id: 'step-pwsh-pipe', name: 'pwsh', args: { command: 'Get-ChildItem -Force | Remove-Item -Recurse -Force', description: 'pipeline cleanup' } },
    { id: 'step-pwsh-read-exec', name: 'pwsh', args: { command: 'rg --pre=evil .model-desk', description: 'read-oriented command execution' } },
    { id: 'step-retry-source', name: 'write', args: { file_path: path.join(box.project, sourceRel), content: 'BYPASS' } },
    { id: 'step-retry-terminal', name: 'pwsh', args: { command: 'Set-Content src/ok.py BYPASS', description: 'try different tool' } },
  ];
  if (box.alias) {
    steps.push({ id: 'step-write-alias', name: 'write', args: { file_path: path.join(box.alias, 'forged.json'), content: 'x' } });
  }
  steps.push({ id: 'step-read-back', name: 'read', args: { file_path: record } });
  const provider = await startProvider(steps);
  let outcome;
  try {
    outcome = await runDsh(box, 'edit', 'http://127.0.0.1:' + provider.port + '/v1', { preset });
  } finally {
    provider.server.close();
  }
  const results = toolResults(provider.captured);
  const stdout = outcome.done.stdout || '';
  const stderr = outcome.done.stderr || '';
  if (process.env.MD_RECORD_DEBUG) {
    fs.writeFileSync(process.env.MD_RECORD_DEBUG,
      JSON.stringify({ exit: outcome.done.status, signal: outcome.done.signal, elapsedMs: outcome.done.elapsedMs,
        error: outcome.done.error ? String(outcome.done.error) : null, stdout, stderr, captured: provider.captured }, null, 2));
  }

  check('edit: the record-write guard is loaded before the turn starts', () => {
    if (!stdout.includes('"type":"record_protection"')) {
      throw new Error('no pre-turn protection event; exit=' + outcome.done.status + ' stderr=' + stderr.slice(0, 400));
    }
  });
  check('edit: a write tool call into .model-desk is refused with the workbench message', () => {
    const text = results.get('step-write-record') || '';
    if (!text.includes('由工作台管理')) throw new Error('unexpected tool result: ' + text);
  });
  check('edit: an edit tool call into .model-desk is refused', () => {
    const text = results.get('step-edit-record') || '';
    if (!text.includes('由工作台管理')) throw new Error('unexpected tool result: ' + text);
  });
  check('edit: creating a new file inside .model-desk is refused', () => {
    const text = results.get('step-write-new-record') || '';
    if (!text.includes('由工作台管理')) throw new Error('unexpected tool result: ' + text);
    if (fs.existsSync(path.join(box.run, 'forged-record.json'))) throw new Error('the forged record was created');
  });
  check('edit: terminal deletion and overwrite of .model-desk are refused', () => {
    for (const id of ['step-pwsh-delete', 'step-pwsh-write','step-pwsh-glob','step-pwsh-pipe','step-pwsh-read-exec']) {
      const text = results.get(id) || '';
      if (!text.includes('由工作台管理')) throw new Error(id + ' result: ' + text);
    }
  });
  if (box.alias) {
    check('edit: a junction alias into .model-desk is refused', () => {
      const text = results.get('step-write-alias') || '';
      if (!text.includes('由工作台管理')) throw new Error('unexpected tool result: ' + text);
      if (fs.existsSync(path.join(box.alias, 'forged.json'))) throw new Error('the aliased write landed');
    });
  }
  check('edit: the original record bytes are unchanged after all refusals', () => {
    if (digest(record) !== before) throw new Error('the record file changed');
    if (fs.readFileSync(record, 'utf8') !== 'REAL-RECORD-BYTES') throw new Error('record content differs');
  });
  check('edit: normal source and a similar backup directory still accept writes', () => {
    const source = path.join(box.project, sourceRel);
    const backup = path.join(box.project, backupRel);
    if (!fs.existsSync(source) || fs.readFileSync(source, 'utf8').indexOf('print(2)') < 0) throw new Error('normal source write failed: ' + (results.get('step-write-source') || ''));
    if (!fs.existsSync(backup) || fs.readFileSync(backup, 'utf8') !== 'backup-ok') throw new Error('similar directory write failed: ' + (results.get('step-write-backup') || ''));
  });
  check('edit: harmless formatting and explicit cache cleanup work before refusal', () => {
    for (const id of ['step-format', 'step-clean-cache']) {
      const text = results.get(id);
      if (typeof text !== 'string' || text.includes('已拒绝') || text.includes('已暂停')) throw new Error(id + ': ' + text);
    }
    if (fs.existsSync(path.join(box.project, 'src', '__pycache__'))) throw new Error('cache cleanup did not execute');
  });
  check('edit: cross-tool writes are paused after refusal', () => {
    for (const id of ['step-retry-source', 'step-retry-terminal']) {
      if (!(results.get(id) || '').includes('已暂停')) throw new Error(id + ': ' + results.get(id));
    }
  });
  check('edit: direct read of a record still works after refusal', () => {
    const text = results.get('step-read-back') || '';
    if (text.includes('由工作台管理')) throw new Error('a read-only command was refused: ' + text);
    if (!text.includes('REAL-RECORD-BYTES')) throw new Error('the read did not return the record: ' + text);
  });
  check('edit: the trusted DSH bridge still writes its proof into the record tree', () => {
    const presetProof = path.join(box.run, 'preset-proof.json');
    if (!fs.existsSync(presetProof)) throw new Error('the preset bridge proof was not written');
    const data = JSON.parse(fs.readFileSync(presetProof, 'utf8'));
    if (!data.session_id) throw new Error('the preset proof has no parent session');
    const recordProof = JSON.parse(fs.readFileSync(path.join(box.run, 'record-proof.json'), 'utf8'));
    if (recordProof.scope !== 'global' || recordProof.probe !== 'ok') throw new Error('bad record proof: ' + JSON.stringify(recordProof));
    if (!recordProof.session_id) throw new Error('record proof has no session id');
    return data.session_id;
  });
  const session = JSON.parse(fs.readFileSync(path.join(box.run, 'preset-proof.json'), 'utf8')).session_id;
  check('edit: the worker-side proof verification accepts the parent session', () => {
    helper({ mode: 'verify', root: ROOT, sandbox: box.sandbox, project: box.project, run: box.run, session_id: session });
  });
  check('edit: the control plane still writes task records and logs', () => {
    const out = JSON.parse(helper({ mode: 'control', root: ROOT, sandbox: box.sandbox, project: box.project, run: box.run, task_id: taskId }));
    if (out.note !== 'control-plane write') throw new Error('control-plane write failed: ' + JSON.stringify(out));
    if (!fs.existsSync(path.join(box.run, 'dsh.log'))) throw new Error('the log file was not written');
  });
  fs.rmSync(box.sandbox, { recursive: true, force: true });
}

async function chatScenario() {
  const taskId = 'record-runtime-chat';
  const box = makeSandbox(taskId);
  const record = path.join(box.project, recordRel);
  const before = digest(record);
  const steps = [
    { id: 'step-chat-write', name: 'write', args: { file_path: path.join(box.run, 'forged-record.json'), content: '{}' } },
    { id: 'step-chat-source', name: 'write', args: { file_path: path.join(box.project, sourceRel), content: 'print(3)\n' } },
  ];
  const provider = await startProvider(steps);
  let outcome;
  try {
    outcome = await runDsh(box, 'chat', 'http://127.0.0.1:' + provider.port + '/v1', { preset: 'standard' });
  } finally {
    provider.server.close();
  }
  const stdout = outcome.done.stdout || '';
  check('chat: the guard is mounted for a read-only session too', () => {
    if (!stdout.includes('"type":"record_protection"')) {
      throw new Error('no pre-turn protection event; stderr=' + (outcome.done.stderr || '').slice(0, 400));
    }
    const proof = JSON.parse(fs.readFileSync(path.join(box.run, 'record-proof.json'), 'utf8'));
    if (proof.scope !== 'global' || proof.root.toLowerCase() !== box.project.toLowerCase()) {
      throw new Error('bad proof: ' + JSON.stringify(proof));
    }
  });
  check('chat: the read-only session cannot write records or source', () => {
    if (digest(record) !== before) throw new Error('the record changed');
    if (fs.existsSync(path.join(box.project, sourceRel))) throw new Error('a read-only chat wrote a source file');
    if (fs.existsSync(path.join(box.run, 'forged-record.json'))) throw new Error('a read-only chat wrote a record');
  });
  fs.rmSync(box.sandbox, { recursive: true, force: true });
}

async function staleIdentityScenario() {
  const taskId = 'record-runtime-stale';
  const box = makeSandbox(taskId);
  const record = path.join(box.project, recordRel);
  const before = digest(record);
  const provider = await startProvider([{ id: 'step-never', name: 'write', args: { file_path: record, content: 'HACKED' } }]);
  let outcome;
  try {
    outcome = await runDsh(box, 'edit', 'http://127.0.0.1:' + provider.port + '/v1', {
      preset: 'native', revision_override: 'stale-protection-identity',
    });
  } finally {
    provider.server.close();
  }
  check('stale protection identity: the turn never starts and no tool request is sent', () => {
    const text = (outcome.done.stdout || '') + (outcome.done.stderr || '');
    if (!text.includes('工作台记录写入保护内容与派发记录不一致')) throw new Error('unexpected refusal: ' + text.slice(0, 600));
    if (provider.captured.length !== 0) throw new Error('the model was called ' + provider.captured.length + ' time(s)');
    if (outcome.done.status === 0) throw new Error('the runner reported success');
  });  check('stale protection identity: the record is untouched', () => {
    if (digest(record) !== before) throw new Error('the record changed');
  });
  fs.rmSync(box.sandbox, { recursive: true, force: true });
}

async function accessScenario(access) {
  const box=makeSandbox('access-'+access),outside=path.join(box.sandbox,'ordinary.txt');
  const provider=await startProvider([{id:'outside-write',name:'write',args:{file_path:outside,content:'ordinary'}}]);
  try{
    const outcome=await runDsh(box,'edit','http://127.0.0.1:'+provider.port+'/v1',{preset:'native',access});
    check('DSH '+access+': native adapter starts with matching access proof',()=>{
      if(outcome.done.status!==0)throw Error(outcome.done.stderr||outcome.done.stdout);
      const proof=JSON.parse(fs.readFileSync(path.join(box.run,'record-proof.json'),'utf8'));
      if(proof.access_mode!==access)throw Error('permission snapshot mismatch');
    });
    check('DSH '+access+': actual outside write follows selected permission',()=>{
      if(access==='full'){
        if(fs.readFileSync(outside,'utf8')!=='ordinary')throw Error('full write failed');
      }else{
        if(fs.existsSync(outside))throw Error('outside write was allowed');
        if(!(toolResults(provider.captured).get('outside-write')||'').includes('项目外'))throw Error('missing containment refusal');
      }
    });
  }finally{provider.server.close();fs.rmSync(box.sandbox,{recursive:true,force:true});}
}

async function main() {
  if (!fs.existsSync(PYTHON)) throw new Error('the project venv python is missing: ' + PYTHON);
  if (!fs.existsSync(RUNNER)) throw new Error('the DSH runner is missing: ' + RUNNER);
  console.log('real-DSH offline record-protection runtime test\n');
  await editScenario('native');
  await editScenario('standard');
  await chatScenario();
  await staleIdentityScenario();
  await accessScenario('full');
  await accessScenario('project');
  console.log('\n' + checks + '/' + (checks + failures.length) + ' checks passed');
  if (failures.length) {
    for (const failure of failures) console.error('failed: ' + failure);
    process.exitCode = 1;
  }
}

module.exports={makeSandbox,helper,startProvider};
if(require.main===module)main().catch((error) => {
  console.error(error && error.stack ? error.stack : error);
  process.exitCode = 1;
});
