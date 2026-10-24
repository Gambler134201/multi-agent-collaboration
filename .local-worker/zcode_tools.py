"""Task-owned ZCode MCP tools; native built-ins are not granted by the adapter.

Argument protection reuses the common record/native profile policy. It is not
an OS sandbox for commands that compute their targets at runtime.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import uuid

import model_registry as registry
import record_protection

HERE = Path(__file__).resolve().parent
GUARD = HERE / 'agent-runtime/workbuddy-native-guard.cjs'
POLICY = HERE / 'agent-runtime/record-policy.cjs'
NAMES = ('read_file', 'write_file', 'edit_file', 'list_directory', 'run_command')
LIMIT = 2 * 1024 * 1024


def revision():
    return hashlib.sha256(Path(__file__).read_bytes() + GUARD.read_bytes() + POLICY.read_bytes()).hexdigest()


def prepare(run, state, protected_roots):
    project, run = Path(state['project']).resolve(), Path(run).resolve()
    if run != project / '.model-desk/runs' / state['id']:
        raise ValueError('ZCode 工具必须绑定当前项目任务')
    files, readonly = state.get('files') or [], state.get('readonly_files') or []
    record_protection.validate_files(files + readonly, project)
    if any(not (project / f).resolve().is_relative_to(project) for f in files + readonly):
        raise ValueError('ZCode 声明文件越出项目')
    job = {'project': str(project), 'task_id': state['id'], 'kind': state['kind'],
           'files': files, 'readonly_files': readonly, 'access_mode': state.get('zcode_access_mode', 'project'),
           'protected_roots': [str(Path(p).resolve()) for p in protected_roots],
           'tools': ['Read', 'Glob'] + (['Write', 'Edit', 'PowerShell'] if state['kind'] == 'edit' else []),
           'revision': revision(), 'nonce': uuid.uuid4().hex}
    path = run / 'zcode-tools-job.json'
    with path.open('x', encoding='utf-8') as stream:
        json.dump(job, stream, ensure_ascii=False)
    return path


class Tools:
    def __init__(self, file):
        self.file = Path(file).resolve()
        self.body = self.file.read_bytes()
        self.job = json.loads(self.body)
        self.project = Path(self.job['project']).resolve()
        self.run = self.file.parent
        if (self.run != self.project / '.model-desk/runs' / self.job['task_id'] or
                self.job['revision'] != revision() or self.job['access_mode'] not in ('project', 'full') or
                self.job['kind'] not in ('chat', 'edit')):
            raise ValueError('ZCode 工具身份或保护版本变化')
        self.proof_file = self.run / 'zcode-tools-proof.json'
        self.proof = {'task_id': self.job['task_id'], 'nonce': self.job['nonce'], 'revision': revision(),
                      'job_hash': hashlib.sha256(self.body).hexdigest(), 'listed': False, 'stopped': False, 'events': []}
        if self.proof_file.exists():
            old = json.loads(self.proof_file.read_text(encoding='utf-8'))
            if any(old.get(k) != self.proof[k] for k in ('task_id', 'nonce', 'revision', 'job_hash')):
                raise ValueError('ZCode 工具证据不属于本任务')
            self.proof = old

    def save(self):
        registry.write_json(self.proof_file, self.proof)

    def guard(self, tool, args):
        if self.file.read_bytes() != self.body or self.proof['stopped']:
            raise ValueError('工具配置变化或此前拒绝，本轮已停止')
        script = 'const fs=require("fs"),g=require(process.argv[1]);const i=JSON.parse(fs.readFileSync(0,"utf8"));console.log(JSON.stringify({reason:g.decide(i.job,i.input)}));'
        node = shutil.which('node')
        if not node:
            raise ValueError('Node 未就绪，工具保护未执行')
        reply = subprocess.run([node, '-e', script, str(GUARD)], input=json.dumps({'job':self.job,
            'input':{'tool_name':tool,'tool_input':args}}), text=True, encoding='utf-8',
            capture_output=True, timeout=15, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        if reply.returncode:
            raise ValueError('工具保护校验失败')
        reason = json.loads(reply.stdout).get('reason')
        if reason:
            raise ValueError(reason)

    def call(self, name, args):
        fields = {'read_file': {'path'}, 'write_file': {'path','content'}, 'edit_file': {'path','old_text','new_text'},
                  'list_directory': {'path'}, 'run_command': {'command'}}
        event = {'tool': name, 'input_hash': hashlib.sha256(json.dumps(args,sort_keys=True,ensure_ascii=False).encode()).hexdigest(),
                 'ok': False, 'at': registry.now()}
        try:
            if name not in fields or not isinstance(args, dict) or set(args) != fields[name] or any(not isinstance(v,str) for v in args.values()):
                raise ValueError('未知工具或参数')
            if name == 'run_command':
                self.guard('PowerShell', args)
                event['command_hash'] = hashlib.sha256(args['command'].encode()).hexdigest()
                shell = shutil.which('pwsh')
                if not shell:
                    raise ValueError('需要原生 PowerShell 7，未改用其他 shell')
                env = {k:v for k,v in os.environ.items() if not any(p in k.upper() for p in ('API_KEY','TOKEN','SECRET','NODE_OPTIONS'))}
                with (self.run / ('zcode-command-'+uuid.uuid4().hex+'.log')).open('w+',encoding='utf-8') as output:
                    process = subprocess.Popen([shell,'-NoLogo','-NoProfile','-NonInteractive','-Command',args['command']],
                        cwd=self.project,env=env,stdin=subprocess.DEVNULL,stdout=output,stderr=subprocess.STDOUT,
                        encoding='utf-8',creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
                    # No implicit timeout. Cancellation only stops this owned process tree.
                    while process.poll() is None:
                        if (self.run/'cancelled.flag').exists():
                            subprocess.run(['taskkill','/PID',str(process.pid),'/T','/F'],capture_output=True)
                            process.wait();raise ValueError('任务已取消')
                        try:process.wait(timeout=.25)
                        except subprocess.TimeoutExpired:pass
                    output.seek(0);text=output.read(50000)
                event['exit_code'] = process.returncode
                result = {'exit_code':process.returncode,'output':text}
            else:
                tool = {'read_file':'Read','write_file':'Write','edit_file':'Edit','list_directory':'Glob'}[name]
                self.guard(tool, {'path':args['path']} if name=='list_directory' else {'file_path':args['path']})
                path = (self.project / args['path']).resolve()
                if name=='list_directory':
                    rows=[]
                    for item in sorted(path.iterdir()):
                        try:self.guard('Read',{'file_path':str(item)})
                        except ValueError:continue
                        rows.append({'name':item.name,'directory':item.is_dir()})
                        if len(rows)>=500:break
                    result={'entries':rows}
                elif name=='read_file':
                    if path.stat().st_size>LIMIT:raise ValueError('文件过大')
                    result={'content':path.read_text(encoding='utf-8')}
                else:
                    if name=='edit_file':
                        if path.stat().st_size>LIMIT:raise ValueError('文件过大')
                        content=path.read_text(encoding='utf-8');old=args['old_text']
                        if not old or content.count(old)!=1:raise ValueError('替换原文须非空且唯一')
                        content=content.replace(old,args['new_text'],1)
                    else:content=args['content']
                    if len(content.encode())>LIMIT:raise ValueError('文件过大')
                    path.parent.mkdir(parents=True,exist_ok=True)
                    self.guard(tool,{'file_path':str(path)})
                    path.write_text(content,encoding='utf-8',newline='\n')
                    result={'sha256':hashlib.sha256(path.read_bytes()).hexdigest()}
            event['ok']=True
            return result
        except Exception:
            self.proof['stopped']=True
            raise ValueError('工具调用被拒绝或无法确认完成；本轮停止，请报告主脑，不换工具重试') from None
        finally:
            self.proof['events'].append(event);self.save()


def main():
    import anyio
    from mcp import types
    from mcp.server.lowlevel import Server
    from mcp.server.stdio import stdio_server
    parser=argparse.ArgumentParser();parser.add_argument('--job',required=True)
    tools=Tools(parser.parse_args().job);server=Server('modeldesk_zcode')
    fields={'read_file':('path',),'write_file':('path','content'),'edit_file':('path','old_text','new_text'),
            'list_directory':('path',),'run_command':('command',)}
    @server.list_tools()
    async def list_tools():
        tools.proof['listed']=True;tools.save()
        names=NAMES if tools.job['kind']=='edit' else ('read_file','list_directory')
        return [types.Tool(name=n,description=('前台运行 PowerShell 7 命令并返回真实退出码，不启动后台服务。' if n=='run_command' else '操作当前任务文件；不访问记录、认证或其他会话；拒绝后停止。'),
            inputSchema={'type':'object','properties':{k:{'type':'string'} for k in fields[n]},'required':list(fields[n]),'additionalProperties':False}) for n in names]
    @server.call_tool()
    async def call_tool(name,arguments):
        try:result=await anyio.to_thread.run_sync(lambda:tools.call(name,arguments))
        except ValueError as error:raise ValueError(str(error)) from None
        return [types.TextContent(type='text',text=json.dumps(result,ensure_ascii=False))]
    async def serve():
        async with stdio_server() as (read,write):
            await server.run(read,write,server.create_initialization_options())
    anyio.run(serve)

if __name__=='__main__':main()
