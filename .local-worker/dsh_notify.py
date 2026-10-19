"""Bound DSH plugin pipe, with fixed messages only. No account/auth file access."""
import json
import os
from pathlib import Path
import re
import struct
import subprocess
import sys

PREFIX='MODEL_DESK_DSH_WAKE_'
KEYS=('PIPE','TOKEN','SESSION','OWNER','BINDING')
LIMIT=65536


def config():
    values={key:os.environ.get(PREFIX+key,'') for key in KEYS}
    if os.environ.get('MODEL_DESK_COMPLETION_HOST')!='dsh':
        raise ValueError('DSH 完成通知宿主未绑定')
    if not re.fullmatch(r'\\\\\.\\pipe\\model-desk-dsh-wake-[0-9a-f]{32}',values['PIPE']):
        raise ValueError('DSH 原生通知通道不可用；请安装匹配的主脑插件')
    if not re.fullmatch(r'[0-9a-f]{64}',values['TOKEN']):
        raise ValueError('DSH 原生通知授权缺失')
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,128}',values['SESSION']) or not values['OWNER']:
        raise ValueError('DSH 原生聊天归属缺失')
    return values


def authorization_context():
    values=config()
    if not re.fullmatch(r'[0-9a-f]{64}',values['BINDING']):
        raise ValueError('DSH 责任会话尚未绑定；不能启动任务完成通知')
    return {'owner':values['OWNER'],'binding_key':values['BINDING']}


def calling_thread(): return config()['SESSION']


def child_environment():
    values=config()
    keep={'SYSTEMROOT','WINDIR','TEMP','TMP'}
    env={key:value for key,value in os.environ.items() if key.upper() in keep}
    env.update({PREFIX+key:value for key,value in values.items()})
    env.update(MODEL_DESK_COMPLETION_HOST='dsh',PYTHONIOENCODING='utf-8')
    return env


def exchange(operation,body):
    values=config()
    request={'operation':operation,'token':values['TOKEN'],'session':values['SESSION'],
             'owner':values['OWNER'],**body}
    payload=json.dumps(request,ensure_ascii=False).encode('utf-8')
    if len(payload)>LIMIT: raise ValueError('DSH 通知请求过大')
    with open(values['PIPE'],'r+b',buffering=0) as stream:
        pending=struct.pack('<I',len(payload))+payload
        while pending:
            count=stream.write(pending)
            if not count: raise ValueError('DSH 通知投递结果未知；不自动重试')
            pending=pending[count:]
        def read_exact(count):
            result=bytearray()
            while len(result)<count:
                part=stream.read(count-len(result))
                if not part: raise ValueError('DSH 通知响应未确认')
                result.extend(part)
            return bytes(result)
        size=struct.unpack('<I',read_exact(4))[0]
        if not 1<=size<=LIMIT: raise ValueError('DSH 通知响应无效')
        result=json.loads(read_exact(size))
    if not isinstance(result,dict) or result.get('error'):
        raise ValueError('DSH 原生通知被拒绝；未重发或重新派发')
    return result


def call(operation,body=None):
    from desktop_notify import hidden_options
    try:
        result=subprocess.run([sys.executable,str(Path(__file__).resolve()),'--child',operation],
            input=json.dumps(body or {}),encoding='utf-8',text=True,capture_output=True,
            timeout=20,env=child_environment(),**hidden_options())
        if result.returncode: raise ValueError('DSH 通知未确认')
        return json.loads(result.stdout)
    except (OSError,ValueError,subprocess.TimeoutExpired):
        raise ValueError('DSH 原生通知未确认；不自动重发，不重新派发任务') from None


def read_current(): return call('read')


def send_current(identity,prompt,call_id):
    if identity!=calling_thread() or not prompt.startswith('[Model Desk completion event] '):
        raise ValueError('DSH 通知身份或内容不匹配')
    reference,_=json.JSONDecoder().raw_decode(prompt[len('[Model Desk completion event] '):])
    if set(reference)!={'project','wait_id','task_ids'} or call_id!='model-desk-'+reference['wait_id']:
        raise ValueError('DSH 只接受固定任务完成引用')
    return call('send',{'reference':reference})


def probe(action,probe_id=None):
    if action not in {'start','status','received','stop'}: raise ValueError('未知唤醒探针操作')
    return call('probe',{'action':action,**({'probe_id':probe_id} if probe_id else {})})


if __name__=='__main__':
    try:
        if len(sys.argv)!=3 or sys.argv[1]!='--child': raise ValueError('internal only')
        print(json.dumps(exchange(sys.argv[2],json.load(sys.stdin)),ensure_ascii=False))
    except Exception:
        print(json.dumps({'error':'dsh_native_notification_unconfirmed'}))
        raise SystemExit(1)
