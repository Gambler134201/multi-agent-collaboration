"""Narrow current-chat desktop bridge, verified by the local wakeup diagnostic.

Only read_thread and one fixed completion message are supported. Importing this
module does no I/O. Native requests have a bounded wait; worker runtime does not.
No host settings, approval policy, model configuration or secrets are accessed.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import uuid


class NotificationError(ValueError):
    pass


def hidden_options():
    if os.name != 'nt':
        return {}
    startup = subprocess.STARTUPINFO()
    startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startup.wShowWindow = subprocess.SW_HIDE
    return {'creationflags': subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP,
            'startupinfo': startup}


def child_environment():
    allowed = {'SYSTEMROOT', 'WINDIR', 'TEMP', 'TMP', 'CODEX_THREAD_ID', 'CODEX_APP_TOOLS_PIPE_PATH'}
    result = {key: value for key, value in os.environ.items() if key.upper() in allowed}
    result['PYTHONIOENCODING'] = 'utf-8'
    return result


def calling_thread():
    identity = os.environ.get('CODEX_THREAD_ID', '')
    try:
        uuid.UUID(identity)
    except (ValueError, AttributeError):
        raise NotificationError('当前桌面聊天身份不可用，未启用后台通知') from None
    if not os.environ.get('CODEX_APP_TOOLS_PIPE_PATH'):
        raise NotificationError('当前宿主未提供通知入口，未启用后台通知')
    return identity


def _exchange(tool, arguments, identity, call_id):
    if identity != calling_thread() or arguments.get('threadId') != identity:
        raise NotificationError('通知聊天身份不一致，拒绝发送')
    if tool not in {'read_thread', 'send_message_to_thread'}:
        raise NotificationError('不支持的桌面操作')
    request = {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call', 'params': {
        'callerSource': 'codex', 'hostId': 'local', 'namespace': 'codex_app',
        'threadId': identity, 'turnId': 'mcp-turn-' + call_id,
        'callId': call_id, 'tool': tool, 'arguments': arguments}}
    with open(os.environ['CODEX_APP_TOOLS_PIPE_PATH'], 'r+b', buffering=0) as stream:
        payload = json.dumps(request).encode('utf-8')
        frame = struct.pack('<I', len(payload)) + payload
        while frame:
            count = stream.write(frame)
            if not count:
                raise NotificationError('桌面连接中断，投递结果未知，不自动重发')
            frame = frame[count:]

        def read_exact(count):
            data = bytearray()
            while len(data) < count:
                part = stream.read(count - len(data))
                if not part:
                    raise NotificationError('桌面连接中断，投递结果未知，不自动重发')
                data.extend(part)
            return bytes(data)

        size = struct.unpack('<I', read_exact(4))[0]
        if size > 8 * 1024 * 1024:
            raise NotificationError('桌面响应过大，投递结果未知，不自动重发')
        response = json.loads(read_exact(size))
    if response.get('error') or response.get('result', {}).get('success') is not True:
        return {'host_accepted': False}
    result = response['result']
    if tool == 'send_message_to_thread':
        return {'host_accepted': True}
    for item in result.get('contentItems', []):
        if item.get('type') == 'inputText':
            try:
                data = json.loads(item['text'])
            except (ValueError, KeyError):
                continue
            if data.get('thread', {}).get('id') == identity:
                return {'thread_id': identity, 'project': data['thread'].get('cwd'),
                        'turns': [{key: turn.get(key) for key in ('id', 'status', 'startedAt', 'completedAt')}
                                  for turn in data.get('turns', [])]}
    raise NotificationError('桌面返回的聊天身份不完整')


def _bounded(operation, body):
    # Keep a blocking pipe read out of the supervisor. The child inherits only
    # the narrow host context, never API credentials or the panel session token.
    try:
        result = subprocess.run([sys.executable, str(Path(__file__).resolve()), '--child', operation],
                                input=json.dumps(body), text=True, encoding='utf-8',
                                capture_output=True, timeout=25, env=child_environment(),
                                **hidden_options())
    except (OSError, subprocess.TimeoutExpired):
        raise NotificationError('桌面请求未确认；不自动重发，不重新派发任务') from None
    if result.returncode:
        raise NotificationError('桌面请求失败；不自动重发，不重新派发任务')
    try:
        return json.loads(result.stdout)
    except ValueError:
        raise NotificationError('桌面请求结果无法确认；不自动重发') from None


def read_current():
    return _bounded('read', {'thread_id': calling_thread()})


def send_current(identity, prompt, call_id):
    if identity != calling_thread():
        raise NotificationError('通知聊天身份不一致，拒绝发送')
    return _bounded('send', {'thread_id': identity, 'prompt': prompt, 'call_id': call_id})


def _child(operation, body):
    identity = body['thread_id']
    if operation == 'read':
        return _exchange('read_thread', {'threadId': identity, 'turnLimit': 1,
                         'includeOutputs': False, 'maxOutputCharsPerItem': 0}, identity,
                         'model-desk-read-' + uuid.uuid4().hex)
    if operation == 'send':
        if not str(body.get('prompt', '')).startswith('[Model Desk completion event] '):
            raise NotificationError('拒绝非任务完成通知')
        return _exchange('send_message_to_thread', {'threadId': identity, 'prompt': body['prompt']},
                         identity, body['call_id'])
    raise NotificationError('不支持的桌面操作')


if __name__ == '__main__':
    try:
        if len(sys.argv) != 3 or sys.argv[1] != '--child':
            raise NotificationError('仅供当前聊天的后台通知使用')
        print(json.dumps(_child(sys.argv[2], json.load(sys.stdin))))
    except Exception:
        # Do not print host payloads, pipe paths or exception details.
        print(json.dumps({'error': 'native_request_failed'}))
        raise SystemExit(1)
