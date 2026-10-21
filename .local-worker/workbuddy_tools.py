"""Task-owned MCP file tools. No terminal, account, HTTP or nested agent tools.

Native built-ins and every other MCP server remain disabled. This boundary is
not an OS sandbox: it confines this server's file operations, not another
application running with the user's permissions.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import uuid

import record_protection

HERE = Path(__file__).resolve().parent
NAMES = ('read_file', 'write_file', 'edit_file')
MAX_BYTES = 2 * 1024 * 1024


class ToolRefused(ValueError):
    pass


def revision():
    return hashlib.sha256(Path(__file__).read_bytes() +
                          (HERE / 'record_protection.py').read_bytes()).hexdigest()


def relative_path(value):
    if not isinstance(value, str) or not value or len(value) > 1024:
        raise ToolRefused('需要任务内的相对文件路径')
    parts = value.replace('\\', '/').split('/')
    for part in parts:
        if (not part or part in ('.', '..') or part != part.rstrip(' .') or
                any(ord(c) < 32 or c in ':*?"<>|' for c in part) or
                re.fullmatch(r'(?i:con|prn|aux|nul|com[1-9]|lpt[1-9])', part.split('.')[0]) or
                part.lower() in ('.model-desk', '.git')):
            raise ToolRefused('文件路径不在允许范围；工作台记录与 Git 元数据不可访问')
    return '/'.join(parts)


class FileTools:
    def __init__(self, job_file):
        if not Path(job_file).is_absolute():
            raise ValueError('文件工具配置需要显式绝对路径')
        self.job_file = Path(job_file).resolve()
        self.job_bytes = self.job_file.read_bytes()
        self.job = json.loads(self.job_bytes)
        self.project = Path(self.job['project']).resolve()
        self.run = self.job_file.parent
        if (not Path(self.job['project']).is_absolute() or not self.project.is_dir() or
                self.run.parent != self.project / '.model-desk' / 'runs' or
                self.run.name != self.job.get('task_id') or
                not re.fullmatch(r'[a-f0-9]{32}', self.job.get('nonce', '')) or
                self.job.get('schema') != 1 or self.job.get('revision') != revision()):
            raise ValueError('WorkBuddy 文件工具任务身份或版本无效')
        self.writable = {relative_path(p) for p in self.job['files']}
        self.readable = self.writable | {relative_path(p) for p in self.job['readonly_files']}
        self.stopped = False
        self.events = []
        self.listed = False
        proof_file = self.run / 'workbuddy-tools-proof.json'
        if proof_file.exists():
            old = json.loads(proof_file.read_text(encoding='utf-8'))
            expected = self.proof()
            if (any(old.get(k) != expected[k] for k in ('task_id', 'nonce', 'project', 'revision', 'job_hash', 'tools')) or
                    type(old.get('writes_paused')) is not bool or type(old.get('tools_listed')) is not bool or
                    not isinstance(old.get('events'), list)):
                raise ValueError('已有工具证据不属于当前任务；不能覆盖或重置保护')
            self.stopped = old.get('writes_paused') is True
            self.listed = old.get('tools_listed') is True
            self.events = list(old.get('events') or [])

    def proof(self):
        return {'schema': 1, 'task_id': self.job['task_id'], 'nonce': self.job['nonce'],
                'project': str(self.project), 'revision': revision(),
                'job_hash': hashlib.sha256(self.job_bytes).hexdigest(),
                'tools_listed': self.listed, 'tools': list(NAMES),
                'server_pid': os.getpid(),
                'writes_paused': self.stopped, 'events': list(self.events)}

    def save_proof(self):
        # Control-plane evidence; never exposed as a writable model target.
        (self.run / 'workbuddy-tools-proof.json').write_text(
            json.dumps(self.proof(), ensure_ascii=False), encoding='utf-8')

    def list_tools(self):
        self.listed = True
        self.save_proof()
        return list(NAMES)

    def target(self, value, *, write=False):
        name = relative_path(value)
        allowed = self.writable if write else self.readable
        if name not in allowed:
            raise ToolRefused('文件未列入本任务的' + ('写入' if write else '读取') + '范围')
        lexical = self.project / name
        real = lexical.resolve()
        if (not real.is_relative_to(self.project) or
                record_protection.is_record_path(real, self.project) or
                any(p.lower() == '.git' for p in real.relative_to(self.project).parts) or
                (real.exists() and (not real.is_file() or real.stat().st_nlink > 1))):
            raise ToolRefused('文件别名、硬链接或实际目标不在允许范围')
        return name, real

    def call(self, name, arguments):
        path = None
        write = name in ('write_file', 'edit_file')
        try:
            if self.job_file.read_bytes() != self.job_bytes:
                raise ToolRefused('任务范围已变化，请停止并报告主脑')
            fields = {'read_file': {'path'}, 'write_file': {'path', 'content'},
                      'edit_file': {'path', 'old_text', 'new_text'}}
            if name not in fields or not isinstance(arguments, dict) or set(arguments) != fields[name]:
                raise ToolRefused('未知工具或参数；不能切换到终端、网络或其他工具')
            if write and self.stopped:
                raise ToolRefused('此前调用被拒，本轮写入已暂停；请报告主脑，不要换工具重试')
            path, target = self.target(arguments['path'], write=write)
            if name == 'read_file' and not target.exists():
                self.events.append({'tool': name, 'path': path, 'ok': True, 'write': False, 'exists': False})
                self.save_proof()
                return {'path': path, 'exists': False}
            if name == 'write_file':
                content = arguments['content']
            else:
                if target.stat().st_size > MAX_BYTES:
                    raise ToolRefused('文件超过工具大小上限')
                content = target.read_bytes().decode('utf-8')
                if name == 'edit_file':
                    old, new = arguments['old_text'], arguments['new_text']
                    if (not isinstance(old, str) or not old or not isinstance(new, str) or
                            content.count(old) != 1):
                        raise ToolRefused('替换原文须非空且只出现一次；本次未修改文件')
                    content = content.replace(old, new, 1)
            if not isinstance(content, str) or len(content.encode('utf-8')) > MAX_BYTES:
                raise ToolRefused('需要大小限制内的 UTF-8 文本')
            if write:
                target.parent.mkdir(parents=True, exist_ok=True)
                # Recheck after directory creation; never follow a changed alias.
                if self.target(arguments['path'], write=True)[1] != target:
                    raise ToolRefused('文件实际目标已变化')
                target.write_bytes(content.encode('utf-8'))
            result = {'path': path, 'sha256': hashlib.sha256(content.encode('utf-8')).hexdigest()}
            if not write:
                result['content'] = content
            self.events.append({'tool': name, 'path': path, 'ok': True, 'write': write,
                                'sha256': result['sha256']})
            self.save_proof()
            return result
        except (OSError, UnicodeError, ValueError) as error:
            # Fail closed for subsequent mutations, including a different tool.
            self.stopped = True
            self.events.append({'tool': name if name in NAMES else 'unknown',
                                'path': path, 'ok': False, 'write': write})
            self.save_proof()
            reason = str(error) if isinstance(error, ToolRefused) else '工具调用未能确认完成'
            raise ToolRefused(reason + '。本轮写入已暂停；保留现场并报告主脑，不要换工具或改写参数重试') from None


def prepare(run, project, task_id, files, readonly_files=()):
    if not Path(run).is_absolute() or not Path(project).is_absolute():
        raise ValueError('工具配置需要显式项目与任务目录绝对路径')
    run, project = Path(run).resolve(), Path(project).resolve()
    if run.parent != project / '.model-desk' / 'runs' or run.name != task_id:
        raise ValueError('工具配置必须保存在当前项目、本任务的运行目录')
    if not isinstance(task_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', task_id):
        raise ValueError('任务标识无效')
    job = {'schema': 1, 'task_id': task_id, 'project': str(project),
           'nonce': uuid.uuid4().hex, 'revision': revision(),
           'files': [relative_path(p) for p in files],
           'readonly_files': [relative_path(p) for p in readonly_files]}
    for p in job['files'] + job['readonly_files']:
        real = (project / p).resolve()
        if not real.is_relative_to(project) or record_protection.is_record_path(real, project):
            raise ValueError('任务文件范围含受保护目录或项目外别名')
    run.mkdir(parents=True, exist_ok=True)
    path = run / 'workbuddy-tools-job.json'
    # One fresh job per execution; never silently rebind a previous lease.
    with path.open('x', encoding='utf-8') as out:
        json.dump(job, out, ensure_ascii=False)
    return path


def server_config(job_file):
    FileTools(job_file)  # Validate before native host creation.
    return {'name': 'modeldesk-files', 'command': sys.executable,
            'args': [str(Path(__file__).resolve()), '--job', str(Path(job_file).resolve())], 'env': []}


def verify(job_file):
    tools = FileTools(job_file)
    proof = json.loads((tools.run / 'workbuddy-tools-proof.json').read_text(encoding='utf-8'))
    expected = tools.proof()
    if any(proof.get(k) != expected[k] for k in ('task_id', 'nonce', 'project', 'revision', 'job_hash', 'tools')) or proof.get('tools_listed') is not True:
        raise ValueError('WorkBuddy 未返回匹配本任务的文件工具加载证据')
    return proof


def main():
    import anyio
    from mcp import types
    from mcp.server.lowlevel import Server
    from mcp.server.stdio import stdio_server
    parser = argparse.ArgumentParser()
    parser.add_argument('--job', required=True)
    tools = FileTools(parser.parse_args().job)
    server = Server('modeldesk-files')

    @server.list_tools()
    async def list_tools():
        tools.list_tools()
        fields = {'read_file': ('path',), 'write_file': ('path', 'content'),
                  'edit_file': ('path', 'old_text', 'new_text')}
        return [types.Tool(name=name, description='仅访问本任务列出的文件；拒绝后停止写入并报告主脑。',
                inputSchema={'type': 'object', 'properties': {k: {'type': 'string'} for k in fields[name]},
                             'required': list(fields[name]), 'additionalProperties': False}) for name in NAMES]

    @server.call_tool()
    async def call_tool(name, arguments):
        try:
            value = tools.call(name, arguments)
            return [types.TextContent(type='text', text=json.dumps(value, ensure_ascii=False))]
        except ToolRefused as error:
            return types.CallToolResult(isError=True, content=[types.TextContent(type='text', text=str(error))])

    async def serve():
        async with stdio_server() as (reader, writer):
            await server.run(reader, writer, server.create_initialization_options())
    anyio.run(serve)


if __name__ == '__main__':
    main()
