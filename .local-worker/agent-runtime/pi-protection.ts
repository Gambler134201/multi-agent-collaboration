// The only extension loaded by Model Desk's isolated Pi process.
import fs from 'node:fs';
import path from 'node:path';
import { createRequire } from 'node:module';
const require = createRequire(import.meta.url);

export default function protection(pi: any) {
  const file = process.env.MODEL_DESK_PI_JOB;
  if (!file) throw new Error('缺少工作台 Pi 任务配置');
  const job = JSON.parse(fs.readFileSync(file, 'utf8'));
  const policy = require(require('path').join(require('path').dirname(job.policy), 'endpoint-access.cjs'));
  const evidence = require(job.policy);
  const decide = policy.createGuard({ ...job, root: job.cwd, access_mode: job.access_mode || 'full' });
  function blocked(name: string, reason: string) {
    const proof=JSON.parse(fs.readFileSync(job.proof_file,'utf8'));
    if(!proof.guard_denial){
      proof.guard_denial=evidence.guardDenial(name,reason);
      fs.writeFileSync(job.proof_file,JSON.stringify(proof));
    }
    return {block:true,reason};
  }
  const reads = new Set(['read', 'grep', 'find', 'ls']);
  const supported = new Set([...reads, 'write', 'edit', 'bash', 'powershell']);
  pi.on('tool_call', (event: any) => {
    const name = event.toolName;
    if (!supported.has(name)) return blocked(name,'本任务未授权此原生工具');
    if (job.kind !== 'edit' && !reads.has(name)) return blocked(name,
      ['bash','powershell'].includes(name)?'只读任务没有终端权限':'只读任务没有写入权限');
    const args = { ...event.input };
    if (args.path !== undefined) args.file_path = args.path;
    const reason = decide(name === 'powershell' ? 'pwsh' : name, args);
    if (reason) return blocked(name,reason);
  });
  pi.on('session_start', (_event: any, ctx: any) => {
    const sessionId = ctx.sessionManager.getSessionId();
    if (!sessionId || path.resolve(ctx.cwd) !== path.resolve(job.cwd)) throw new Error('Pi 父会话或项目绑定错误');
    fs.writeFileSync(job.proof_file, JSON.stringify({ source: 'model-desk-pi-protection',
      revision: job.revision, root: job.cwd, session_id: sessionId, kind: job.kind, access_mode: job.access_mode || 'full',
      ...(job.task_policy ? {task_policy:job.task_policy} : {}),
      ...(job.record_project ? {record_project:job.record_project,context_root:job.context_root} : {}),
      session_file: ctx.sessionManager.getSessionFile(), tools_guard: true, at: new Date().toISOString() }));
  });
}
