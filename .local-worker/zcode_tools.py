"""Task-owned ZCode MCP tools; native built-ins are not granted by the adapter.

Argument protection reuses the common record/native profile policy. It is not
an OS sandbox for commands that compute their targets at runtime.
"""
import argparse
import base64
import fnmatch
import hashlib
import http.client
import ipaddress
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import threading
import uuid
from urllib.parse import urlsplit,urljoin

import model_registry as registry
import record_protection

HERE = Path(__file__).resolve().parent
GUARD = HERE / 'agent-runtime/workbuddy-native-guard.cjs'
POLICY = HERE / 'agent-runtime/record-policy.cjs'
NAMES = ('read_file', 'write_file', 'edit_file', 'list_directory', 'search_files', 'read_image', 'fetch_url','run_command')
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


def fetch_url(url):
    """Public GET only; no cookies/proxy/auth, resolved IP pinned per redirect."""
    for _ in range(5):
        parsed=urlsplit(url)
        if (len(url)>4096 or any(ord(c)<33 for c in url) or parsed.scheme not in ('http','https') or
                not parsed.hostname or parsed.username is not None or parsed.password is not None):raise ValueError('只允许公开 HTTP(S) 地址')
        port=parsed.port or (443 if parsed.scheme=='https' else 80)
        addresses={r[4][0] for r in socket.getaddrinfo(parsed.hostname,port,type=socket.SOCK_STREAM)}
        if not addresses or any(not ipaddress.ip_address(ip).is_global for ip in addresses):raise ValueError('拒绝本机、内网、保留地址和混合解析')
        chosen=sorted(addresses)[0]
        connection=(http.client.HTTPSConnection if parsed.scheme=='https' else http.client.HTTPConnection)(parsed.hostname,port,timeout=20)
        connection._create_connection=lambda address,timeout=20,source_address=None,**kw:socket.create_connection((chosen,port),timeout,source_address)
        try:
            connection.request('GET',(parsed.path or '/')+('?' + parsed.query if parsed.query else ''),headers={'User-Agent':'ModelDesk-ZCode/1.0','Accept':'text/*,application/json'})
            response=connection.getresponse()
            if response.status in (301,302,303,307,308):
                location=response.getheader('Location')
                if not location:raise ValueError('重定向缺少目标')
                url=urljoin(url,location);continue
            content_type=response.getheader('Content-Type','').lower()
            if not (content_type.startswith('text/') or content_type.startswith('application/json')):raise ValueError('网页读取仅支持文本或 JSON')
            data=response.read(500001)
            return {'url':url,'status':response.status,'content':data[:500000].decode('utf-8',errors='replace')[:50000],
                    'truncated':len(data)>500000 or len(data.decode('utf-8',errors='replace'))>50000}
        finally:connection.close()
    raise ValueError('重定向次数超限')


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
        self.lock=threading.RLock()
        self.proof = {'task_id': self.job['task_id'], 'nonce': self.job['nonce'], 'revision': revision(),
                      'job_hash': hashlib.sha256(self.body).hexdigest(), 'listed': False, 'stopped': False, 'events': []}
        if self.proof_file.exists():
            old = json.loads(self.proof_file.read_text(encoding='utf-8'))
            if any(old.get(k) != self.proof[k] for k in ('task_id', 'nonce', 'revision', 'job_hash')):
                raise ValueError('ZCode 工具证据不属于本任务')
            self.proof = old

    def save(self):
        with self.lock:registry.write_json(self.proof_file, self.proof)

    def guard(self, tool, args):
        reason=self.guard_batch([{'tool_name':tool,'tool_input':args}])[0]
        if reason:raise ValueError(reason)

    def guard_batch(self, inputs):
        if self.file.read_bytes() != self.body or self.proof['stopped']:
            raise ValueError('工具配置变化或此前拒绝，本轮已停止')
        if not inputs:return []
        script = 'const fs=require("fs"),g=require(process.argv[1]);const i=JSON.parse(fs.readFileSync(0,"utf8"));console.log(JSON.stringify(i.inputs.map(input=>g.decide(i.job,input))));'
        node = shutil.which('node')
        if not node:
            raise ValueError('Node 未就绪，工具保护未执行')
        reply = subprocess.run([node, '-e', script, str(GUARD)], input=json.dumps({'job':self.job,'inputs':inputs}), text=True, encoding='utf-8',
            capture_output=True, timeout=15, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        if reply.returncode:
            raise ValueError('工具保护校验失败')
        result=json.loads(reply.stdout)
        if not isinstance(result,list) or len(result)!=len(inputs):raise ValueError('工具保护响应无效')
        return result

    def call(self, name, args):
        fields = {'read_file': {'path'}, 'write_file': {'path','content'}, 'edit_file': {'path','old_text','new_text'},
                  'list_directory': {'path'}, 'search_files':{'path','pattern','text'}, 'read_image':{'path'},'fetch_url':{'url'},'run_command': {'command'}}
        event = {'tool': name, 'input_hash': hashlib.sha256(json.dumps(args,sort_keys=True,ensure_ascii=False).encode()).hexdigest(),
                 'ok': False, 'status':'in_progress','at': registry.now()}
        with self.lock:self.proof['events'].append(event);self.save()
        try:
            if name not in fields or not isinstance(args, dict) or set(args) != fields[name] or any(not isinstance(v,str) for v in args.values()):
                raise ValueError('未知工具或参数')
            if self.file.read_bytes()!=self.body or self.proof['stopped']:raise ValueError('本轮已停止或配置变化')
            if name=='fetch_url':
                result=fetch_url(args['url'])
            elif name == 'run_command':
                self.guard('PowerShell', args)
                event['command_hash'] = hashlib.sha256(args['command'].encode()).hexdigest()
                from powershell_runtime import find_pwsh
                shell=find_pwsh()
                if not shell:raise ValueError('需要原生 PowerShell 7，未改用其他 shell')
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
                tool = {'read_file':'Read','read_image':'Read','write_file':'Write','edit_file':'Edit','list_directory':'Glob','search_files':'Glob'}[name]
                self.guard(tool, {'path':args['path']} if name in ('list_directory','search_files') else {'file_path':args['path']})
                path = (self.project / args['path']).resolve()
                if name=='list_directory':
                    items=sorted(path.iterdir())[:2000]
                    reasons=self.guard_batch([{'tool_name':'Read','tool_input':{'file_path':str(item)}} for item in items])
                    rows=[{'name':item.name,'directory':item.is_dir()} for item,reason in zip(items,reasons) if not reason]
                    result={'entries':rows[:500],'truncated':len(items)>=2000 or len(rows)>500}
                elif name=='search_files':
                    rows=[];scanned=0;truncated=False
                    for directory,dirs,files in os.walk(path,followlinks=False):
                        candidates=[Path(directory)/name for name in dirs if name not in ('.git','.model-desk','node_modules','venv','__pycache__')]
                        candidates=[p for p in candidates if not p.is_symlink() and not getattr(p,'is_junction',lambda:False)()]
                        reasons=self.guard_batch([{'tool_name':'Glob','tool_input':{'path':str(p)}} for p in candidates])
                        dirs[:]=[p.name for p,reason in zip(candidates,reasons) if not reason]
                        candidates=[Path(directory)/name for name in files[:2001-scanned]]
                        reasons=self.guard_batch([{'tool_name':'Read','tool_input':{'file_path':str(p)}} for p in candidates])
                        for item,reason in zip(candidates,reasons):
                            scanned+=1
                            if scanned>2000:truncated=True;break
                            if reason:continue
                            relative=str(item.relative_to(path)).replace('\\','/')
                            if not (fnmatch.fnmatchcase(relative,args['pattern']) or fnmatch.fnmatchcase(item.name,args['pattern'])):continue
                            try:
                                if not args['text']:rows.append({'file':relative})
                                elif item.stat().st_size<=LIMIT:
                                    for number,line in enumerate(item.read_text(encoding='utf-8').splitlines(),1):
                                        if args['text'] in line:rows.append({'file':relative,'line':number,'text':line[:500]})
                                        if len(rows)>=200:break
                            except (ValueError,OSError,UnicodeError):continue
                            if len(rows)>=200:truncated=True;break
                        if truncated:break
                    result={'matches':rows,'scanned_files':scanned,'truncated':truncated}
                elif name=='read_image':
                    if path.stat().st_size>LIMIT:raise ValueError('图片过大')
                    data=path.read_bytes()
                    mime=('image/png' if data.startswith(b'\x89PNG\r\n\x1a\n') else 'image/jpeg' if data.startswith(b'\xff\xd8\xff') else 'image/webp' if data.startswith(b'RIFF') and data[8:12]==b'WEBP' else None)
                    if mime is None:raise ValueError('仅支持 PNG/JPEG/WebP 图片')
                    result={'image_data':base64.b64encode(data).decode('ascii'),'mime_type':mime}
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
            event['status']='completed'
            return result
        except Exception:
            self.proof['stopped']=True
            event['status']='failed'
            raise ValueError('工具调用被拒绝或无法确认完成；本轮停止，请报告主脑，不换工具重试') from None
        finally:
            self.save()


def main():
    import anyio
    from mcp import types
    from mcp.server.lowlevel import Server
    from mcp.server.stdio import stdio_server
    parser=argparse.ArgumentParser();parser.add_argument('--job',required=True)
    tools=Tools(parser.parse_args().job);server=Server('modeldesk_zcode')
    fields={'read_file':('path',),'read_image':('path',),'fetch_url':('url',),'write_file':('path','content'),'edit_file':('path','old_text','new_text'),
            'list_directory':('path',),'search_files':('path','pattern','text'),'run_command':('command',)}
    @server.list_tools()
    async def list_tools():
        tools.proof['listed']=True;tools.save()
        names=NAMES if tools.job['kind']=='edit' else ('read_file','list_directory','search_files','read_image','fetch_url')
        descriptions={'run_command':'前台运行 PowerShell 7 命令并返回真实退出码，不启动后台服务。',
                      'search_files':'在 path 下按 pattern 文件通配符搜索，text 为空时只列文件；非空时按字面量搜索行。自动排除工作台记录、认证与常见依赖目录；结果有上限。',
                      'read_image':'读取 PNG/JPEG/WebP 图片并以图片内容返回；遵守文件读取范围。',
                      'fetch_url':'只读获取公开 HTTP(S) 网页文本；不携带登录凭据、不访问内网或本机，重定向同样校验。'}
        return [types.Tool(name=n,description=descriptions.get(n,'操作当前任务文件；不访问记录、认证或其他会话；拒绝后停止。'),
            inputSchema={'type':'object','properties':{k:{'type':'string'} for k in fields[n]},'required':list(fields[n]),'additionalProperties':False}) for n in names]
    @server.call_tool()
    async def call_tool(name,arguments):
        try:result=await anyio.to_thread.run_sync(lambda:tools.call(name,arguments))
        except ValueError as error:raise ValueError(str(error)) from None
        if 'image_data' in result:return [types.ImageContent(type='image',data=result['image_data'],mimeType=result['mime_type'])]
        return [types.TextContent(type='text',text=json.dumps(result,ensure_ascii=False))]
    async def serve():
        async with stdio_server() as (read,write):
            await server.run(read,write,server.create_initialization_options())
    anyio.run(serve)

if __name__=='__main__':main()
