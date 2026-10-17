// The only extension loaded by Model Desk's isolated Pi process.
import fs from 'node:fs';
import path from 'node:path';
import { createRequire } from 'node:module';
const require = createRequire(import.meta.url);

export default function protection(pi: any) {
  const file = process.env.MODEL_DESK_PI_JOB;
  if (!file) throw new Error('缺少工作台 Pi 任务配置');
  const job = JSON.parse(fs.readFileSync(file, 'utf8'));
  const policy = require(job.policy);
  const reads = new Set(['read', 'grep', 'find', 'ls']);
  const supported = new Set([...reads, 'write', 'edit', 'bash', 'powershell']);
  pi.on('tool_call', (event: any) => {
    const name = event.toolName;
    if (!supported.has(name)) return { block: true, reason: '工作台未授权此 Pi 工具；不允许隐式扩展或子代理' };
    if (job.kind !== 'edit' && !reads.has(name)) return { block: true, reason: '这是只读任务，不能运行写入或终端工具' };
    const args = { ...event.input };
    if (args.path !== undefined) args.file_path = args.path;
    const reason = policy.decide(name === 'powershell' ? 'pwsh' : name, args, { root: job.cwd });
    if (reason) return { block: true, reason };
  });
  pi.on('session_start', (_event: any, ctx: any) => {
    const sessionId = ctx.sessionManager.getSessionId();
    if (!sessionId || path.resolve(ctx.cwd) !== path.resolve(job.cwd)) throw new Error('Pi 父会话或项目绑定错误');
    fs.writeFileSync(job.proof_file, JSON.stringify({ source: 'model-desk-pi-protection',
      revision: job.revision, root: job.cwd, session_id: sessionId, kind: job.kind,
      session_file: ctx.sessionManager.getSessionFile(), tools_guard: true, at: new Date().toISOString() }));
  });
}
