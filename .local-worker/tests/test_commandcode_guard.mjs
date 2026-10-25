import assert from 'node:assert/strict';
import {mkdtemp,writeFile,readFile,mkdir,symlink} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import guard from '../agent-runtime/commandcode-guard.mjs';
const root=await mkdtemp(join(tmpdir(),'modeldesk-cmd-guard-'));
const project=join(root,'project');await mkdir(project);
const file=join(project,'input.txt');await writeFile(file,'fixture');
const audit=join(root,'audit.ndjson'),jobfile=join(root,'job.json');
let hook;
const job={project,files:[file],kind:'chat',task_id:'offline-guard',audit};
await writeFile(jobfile,JSON.stringify(job));process.env.MODEL_DESK_COMMANDCODE_JOB=jobfile;
await guard({hooks:value=>{hook=value.beforeToolCall;}});
assert.equal(await hook({toolName:'read_file',input:{paths:[file]}}),undefined);
assert.equal(await hook({toolName:'read_file',input:{file_path:file}}),undefined);
for(const [tool,input] of [['write_file',{file_path:file}],['edit_file',{file_path:file}],['shell',{}],
 ['read_directory',{path:project}],['read_file',{paths:[join(root,'outside')]}],
 ['read_file',{paths:[join(project,'.model-desk','task.json')]}],['read_file',{paths:['*.txt']}],
 ['mcp__rea__open',{}],['spawn_agent',{}]]){
 assert.equal((await hook({toolName:tool,input})).block,true,tool);
}
job.kind='edit';await writeFile(jobfile,JSON.stringify(job));await guard({hooks:value=>{hook=value.beforeToolCall;}});
assert.equal(await hook({toolName:'write_file',input:{file_path:file}}),undefined);
assert.equal(await hook({toolName:'edit_file',input:{file_path:file}}),undefined);
const rows=(await readFile(audit,'utf8')).trim().split('\n').map(JSON.parse);
assert.equal(rows.filter(r=>r.type==='guard_ready').length,2);
assert(rows.filter(r=>r.type==='guard_ready').every(r=>r.job_sha256.length===64&&r.task_id==='offline-guard'));
assert.equal(await readFile(file,'utf8'),'fixture');
console.log('ok - CommandCode native guard: exact files, readonly, outside/records, unknown tools, edit and proof');
