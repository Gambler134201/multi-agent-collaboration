import fs from 'node:fs';
import path from 'node:path';
import { spawn } from 'node:child_process';

const job = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
const emit = value => process.stdout.write(JSON.stringify(value) + '\n');
const samePath = (a, b) => path.resolve(a || '.').toLowerCase() === path.resolve(b || '.').toLowerCase();
const tools = job.kind === 'edit' ? 'read,write,edit,powershell,bash,grep,find,ls' : 'read,grep,find,ls';
const args = [job.cli, '--mode', 'rpc', '--offline', '--no-approve', '--no-extensions', '--no-mcp',
  '--no-skills', '--no-prompt-templates', '--no-themes', '--extension', job.extension,
  '--provider', 'desk-worker', '--model', job.model, '--tools', tools,
  ...(job.session_file ? ['--session', job.session_file] : ['--session-dir', job.session_dir])];
fs.rmSync(job.proof_file, { force: true });
const child = spawn(job.node, args, { cwd: job.cwd, env: process.env, windowsHide: true, stdio: ['pipe', 'pipe', 'pipe'] });
child.stdout.setEncoding('utf8'); child.stderr.setEncoding('utf8');
emit({ type: 'process', pid: child.pid });
const pending = new Map();
let counter = 0, buffer = '', stderr = '', settled, prompted = false, finished = false;
let cancelling = false, lastAssistant = null, extensionError = null;
const done = new Promise(resolve => { settled = resolve; });
child.stderr.on('data', chunk => { stderr = (stderr + chunk).slice(-12000); });
child.on('error', error => { extensionError = error.message; settled({ status: 'failed', error: { message: error.message } }); });
child.on('close', code => {
  for (const entry of pending.values()) { clearTimeout(entry.timer); entry.reject(new Error(stderr || 'Pi RPC 提前退出：' + code)); }
  pending.clear();
  if (!finished) settled({ status: 'failed', error: { message: stderr || 'Pi 未返回 agent_settled，不能视为完成' } });
});
function request(type, fields = {}) {
  return new Promise((resolve, reject) => {
    const id = 'desk-' + (++counter);
    const timer = setTimeout(() => { pending.delete(id); reject(new Error('Pi 控制命令未确认：' + type)); }, 20000);
    pending.set(id, { resolve, reject, timer });
    child.stdin.write(JSON.stringify({ id, type, ...fields }) + '\n', error => {
      if (error) { clearTimeout(timer); pending.delete(id); reject(error); }
    });
  });
}
child.stdout.on('data', chunk => {
  buffer += chunk.toString('utf8');
  for (let index; (index = buffer.indexOf('\n')) >= 0;) {
    const line = buffer.slice(0, index).replace(/\r$/, ''); buffer = buffer.slice(index + 1);
    if (!line.trim()) continue;
    let event;
    try { event = JSON.parse(line); } catch { extensionError = 'Pi 返回了非 JSONL 控制数据'; continue; }
    if (event.type === 'response') {
      const entry = pending.get(event.id);
      if (entry) { pending.delete(event.id); clearTimeout(entry.timer); event.success ? entry.resolve(event.data) : entry.reject(new Error(event.error || 'Pi 命令被拒绝')); }
    } else if (event.type === 'extension_error') {
      extensionError = event.error || 'Pi 保护扩展出错';
    } else if (event.type === 'message_update' && event.assistantMessageEvent?.type === 'text_delta') {
      emit({ type: 'text_delta', text: event.assistantMessageEvent.delta || '' });
    } else if (event.type === 'message_end' && event.message?.role === 'assistant') {
      lastAssistant = event.message;
    } else if (event.type.startsWith('tool_execution_')) {
      emit({ type: 'tool_call', toolCallId: event.toolCallId, title: event.toolName,
        status: event.type === 'tool_execution_start' ? 'in_progress' : event.type === 'tool_execution_end' ? (event.isError ? 'failed' : 'completed') : 'in_progress',
        rawInput: event.args, rawOutput: event.result || event.partialResult });
    } else if (event.type === 'agent_settled' && prompted) {
      finished = true;
      if (cancelling || fs.existsSync(job.cancel_file)) settled({ status: 'cancelled', stopReason: 'cancelled' });
      else if (event.aborted) settled({ status: 'failed', error: { message: 'Pi 意外中断，未收到用户取消请求' } });
      else if (extensionError || !lastAssistant || lastAssistant.stopReason !== 'stop') settled({ status: 'failed',
        error: { message: extensionError || lastAssistant?.errorMessage || ('Pi 非正常结束：' + lastAssistant?.stopReason) } });
      else settled({ status: 'completed', stopReason: 'end_turn' });
    }
  }
});
let cancelTimer;
try {
  emit({ type: 'phase', text: '连接 Pi Agent' });
  const state = await request('get_state');
  if (state.model?.provider !== 'desk-worker' || state.model?.id !== job.model ||
      String(state.model?.baseUrl || '').replace(/\/$/, '') !== job.api_base.replace(/\/$/, '')) throw new Error('Pi 没有选择任务指定的模型和计费网关，已拒绝执行');
  if (job.session_id && state.sessionId !== job.session_id) throw new Error('Pi 续接到了不同会话，已拒绝执行');
  if (!state.sessionFile || !samePath(path.dirname(state.sessionFile), job.session_file ? path.dirname(job.session_file) : job.session_dir)) throw new Error('Pi 会话文件不属于指定任务目录');
  const proof = JSON.parse(fs.readFileSync(job.proof_file, 'utf8'));
  if (extensionError || proof.source !== 'model-desk-pi-protection' || proof.revision !== job.revision ||
      proof.session_id !== state.sessionId || !samePath(proof.root, job.cwd) || proof.kind !== job.kind || !proof.tools_guard) throw new Error('Pi 记录写入保护未绑定本任务，已拒绝开始');
  emit({ type: 'session', session_id: state.sessionId, session_file: state.sessionFile });
  emit({ type: 'record_protection', engine: 'pi', session_id: state.sessionId });
  await request('set_auto_retry', { enabled: false });
  if (fs.existsSync(job.cancel_file)) throw new Error('任务已取消');
  prompted = true;
  const response = await request('prompt', { message: fs.readFileSync(job.prompt_file, 'utf8') });
  if (response?.disposition !== 'started') throw new Error('Pi 未开始本次任务：' + response?.disposition);
  cancelTimer = setInterval(() => {
    if (!cancelling && fs.existsSync(job.cancel_file)) {
      cancelling = true;
      request('clear_queue').then(() => request('abort')).catch(error => { extensionError = error.message; });
    }
  }, 200);
  const result = await done;
  emit({ type: 'result', result });
  if (result.status !== 'completed') process.exitCode = 1;
} catch (error) {
  emit({ type: 'error', text: error.message }); process.exitCode = 1;
} finally {
  clearInterval(cancelTimer);
  child.stdin.end();
  if (child.exitCode === null) {
    await new Promise(resolve => { child.once('close', resolve); setTimeout(resolve, 1500).unref(); });
  }
  if (child.exitCode === null) {
    // Closing the RPC client ends the owned Pi process. The Python worker still
    // owns tree cancellation if a tool refuses to shut down.
    child.kill();
    await new Promise(resolve => { child.once('close', resolve); setTimeout(resolve, 5000).unref(); });
  }
}
