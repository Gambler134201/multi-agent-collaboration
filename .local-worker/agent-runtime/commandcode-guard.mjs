import {readFile, appendFile, realpath, lstat} from 'node:fs/promises';
import {resolve, relative, isAbsolute, dirname, sep} from 'node:path';
import {createHash} from 'node:crypto';

// Native beforeToolCall hook, not an OS sandbox. No directory-wide grants,
// terminal, nested agents, MCP, configuration tools or workbench record writes.
export default async function guard(cmd) {
  const raw = await readFile(process.env.MODEL_DESK_COMMANDCODE_JOB);
  const job = JSON.parse(raw);
  const root = await realpath(job.project);
  const allowed = new Set(job.files.map(p=>resolve(p).toLowerCase()));
  const log = event=>appendFile(job.audit, JSON.stringify({...event,task_id:job.task_id})+'\n');
  async function allowedPath(value) {
    if(typeof value!=='string'||!value||/[*?{}]/.test(value))return false;
    const target=resolve(root,value),rel=relative(root,target);
    if(rel==='..'||rel.startsWith('..'+sep)||isAbsolute(rel)||rel.split(/[\\/]/).some(p=>p.toLowerCase()==='.model-desk'))return false;
    if(!allowed.has(target.toLowerCase()))return false;
    let existing=target;
    while(true){try{await lstat(existing);break;}catch(e){if(!['ENOENT','ENOTDIR'].includes(e.code))return false;const p=dirname(existing);if(p===existing)return false;existing=p;}}
    const real=await realpath(existing),tail=relative(existing,target),actual=resolve(real,tail);
    return actual.toLowerCase()===target.toLowerCase()&&allowed.has(actual.toLowerCase());
  }
  cmd.hooks({beforeToolCall:async({toolName,input})=>{
    const paths=toolName==='read_file'?[...(Array.isArray(input?.paths)?input.paths:typeof input?.paths==='string'?[input.paths]:[]),...(input?.file_path?[input.file_path]:[])]:[input?.file_path];
    let reason=null;
    if(!['read_file','write_file','edit_file'].includes(toolName))reason='Only exact scoped file tools are available; shell, directories, MCP and delegation are denied.';
    else if(toolName!=='read_file'&&job.kind!=='edit')reason='This task is read-only.';
    else if(!paths.length||!(await Promise.all(paths.map(allowedPath))).every(Boolean))reason='Path is outside the exact task files or resolves through an alias.';
    await log({type:'tool_policy',tool:toolName,paths,allowed:!reason,reason});
    return reason?{block:true,additionalContext:reason}:undefined;
  }});
  await log({type:'guard_ready',project:root,kind:job.kind,
    job_sha256:createHash('sha256').update(raw).digest('hex')});
}
