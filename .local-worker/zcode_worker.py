"""ZCode desktop-account executor. No credential files, API proxy or fallback."""
import argparse
from functools import lru_cache
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
from urllib.request import urlopen
import uuid

import model_registry as registry
import execution_result
import zcode_tools

HERE=Path(__file__).resolve().parent
CONFIG='zcode.json'
CATALOG='zcode-catalog.json'
MODE='scoped-mcp-development-v1'
MODEL='account:zai-start-plan/GLM-5.3-Flash'


def configuration():
    file=Path(registry.HERE)/CONFIG
    if not file.exists():return {'home':os.environ.get('MODEL_DESK_ZCODE_HOME',''),'port':19765,'access_mode':'project'}
    data=json.loads(file.read_text(encoding='utf-8'))
    if (set(data)!={'home','port','access_mode'} or not isinstance(data['home'],str) or
            type(data['port']) is not int or not 1024<=data['port']<=65535 or data['access_mode'] not in ('project','full')):
        raise ValueError('ZCode 连接配置无效')
    return data


@lru_cache(maxsize=4)
def _install(home,stamp):
    from workbuddy_executor import _asar_package
    root=Path(home);meta=_asar_package(root/'resources/app.asar')
    if meta.get('name')!='@zcode/desktop' or meta.get('version')!='3.16.3':
        raise ValueError('ZCode 安装身份或版本未经核实；本版支持 3.16.3')
    return {'version':meta['version'],'executable':str(root/'ZCode.exe'),
            'fingerprint':hashlib.sha256((root/'resources/glm/zcode.cjs').read_bytes()).hexdigest()}


def installation(config):
    root=Path(config['home'])
    if not root.is_absolute():raise ValueError('ZCode 安装目录需要绝对路径')
    paths=[root/'ZCode.exe',root/'resources/app.asar',root/'resources/glm/zcode.cjs']
    stamps=tuple((p.stat().st_size,p.stat().st_mtime_ns) for p in paths)
    return dict(_install(str(root.resolve()),stamps))


def catalog():
    path=Path(registry.HERE)/CATALOG
    if not path.exists():return {'models':[],'preferred':None,'updated_at':None}
    data=json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(data.get('models'),list):raise ValueError('ZCode 模型列表不可读')
    return data


def model_row_id(code):
    if not isinstance(code,str) or '/' not in code or len(code)>200:raise ValueError('ZCode 模型身份无效')
    return 'zcode-'+hashlib.sha256(code.encode()).hexdigest()[:12]


def catalog_model(code):
    return next((m for m in catalog()['models'] if m['providerId']+'/'+m['modelId']==code),None)


def driver(job, *, on_event=None):
    node=shutil.which('node')
    if not node:raise ValueError('ZCode 桥接需要 Node 22 或更新版本')
    with tempfile.TemporaryDirectory(prefix='modeldesk-zcode-driver-') as folder:
        file=Path(folder)/'job.json';registry.write_json(file,job)
        process=subprocess.Popen([node,str(HERE/'agent-runtime/run-zcode.mjs'),str(file)],
            stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,
            text=True,encoding='utf-8',errors='replace',creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        result=[]
        try:
            for line in process.stdout:
                event=json.loads(line);result.append(event)
                if on_event:on_event(event)
            code=process.wait()
        finally:
            process.stdout.close()
            if process.poll() is None:process.terminate();process.wait()
        if code and not any(e['type']=='error' for e in result):result.append({'type':'error','message':'ZCode 桥接异常退出，结果未知；未重试'})
        return result


def refresh_catalog():
    config=configuration();info=installation(config)
    events=driver({**config,'action':'catalog'})
    row=next((e for e in events if e['type']=='catalog'),None)
    if not row:raise ValueError('ZCode 原生模型读取失败；请通过带本地桥接参数的入口打开 ZCode')
    data={k:row[k] for k in ('models','preferred')};data.update(updated_at=registry.now(),installation=info['fingerprint'],connection=config)
    registry.write_json(Path(registry.HERE)/CATALOG,data)
    return {'ok':True,**data,'model_called':False}


def selection(code):
    row=catalog_model(code)
    if not row:raise ValueError('模型不在已读取的 ZCode 原生列表中')
    current=catalog().get('preferred') or {}
    preferred=(current.get('options') or {}).get('reasoningLevel') if (current.get('providerId') or '')+'/'+current.get('modelId','')==code else None
    levels=row.get('reasoning',[])
    level=preferred if preferred in levels else 'max' if 'max' in levels else levels[-1] if levels else None
    return {'providerId':row['providerId'],'modelId':row['modelId'],**({'options':{'reasoningLevel':level}} if level else {})}


def select_model(code):
    return registry.upsert_model({'provider':'zcode','model':code,'name':'ZCode · '+selection(code)['modelId']})


def configure(body):
    if not isinstance(body,dict) or set(body)!={'home','port'} or type(body['port']) is not int:
        raise ValueError('ZCode 连接只接受安装目录和本机桥接端口')
    config={**body,'access_mode':configuration()['access_mode']}
    if not 1024<=config['port']<=65535:raise ValueError('本机端口无效')
    installation(config)
    registry.write_json(Path(registry.HERE)/CONFIG,config)
    return {'ok':True,'configuration':config,'model_called':False}


def set_access_mode(value):
    if value not in ('project','full'):raise ValueError('ZCode 访问权限无效')
    config=configuration();config['access_mode']=value;registry.write_json(Path(registry.HERE)/CONFIG,config)
    return {'ok':True,'access_mode':value}


def launch():
    config=configuration();info=installation(config)
    # Explicit UI action only; never called from availability/import/preflight.
    subprocess.Popen([info['executable'],'--remote-debugging-port='+str(config['port']),
        '--remote-debugging-address=127.0.0.1'],stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    return {'ok':True,'model_called':False,'message':'已请求打开 ZCode；原来运行的实例不会被关闭。若原实例未开启桥接，请任务结束后完全退出，再点击连接。'}


def availability():
    result={'name':'ZCode','engine':'zcode','ready':False,'installed':False,'version':None,
            'account_verified':False,'billing_source':'zcode-native-account','native_catalog':{'models':[]},
            'message':'请配置 ZCode，并通过本机桥接入口启动；不会使用 API 或其他渠道'}
    try:
        config=configuration();result.update(configuration_values=config,access_mode=config['access_mode'])
        info=installation(config);result.update(info,installed=True)
        with urlopen('http://127.0.0.1:%d/json/list'%config['port'],timeout=1) as response:pages=json.load(response)
        expected=(Path(config['home'])/'resources/app.asar/out/renderer/index.html').as_uri()
        ready=sum(p.get('type')=='page' and p.get('url','').split('?')[0].casefold()==expected.casefold() for p in pages)==1
        data=catalog();valid=data.get('installation')==info['fingerprint'] and data.get('connection',{}).get('home')==config['home']
        result.update(ready=ready and valid,native_catalog=data,
            message='本机原生宿主可连接；账户授权在真实任务时核实' if ready and valid else '本机宿主或原生模型列表尚未就绪')
    except (OSError,ValueError,KeyError,TypeError):pass
    return result


def validate_task(body,settings):
    if not settings.get('paid_allowed'):raise ValueError('未授权使用非本地原生账户执行')
    if settings.get('enforce_paid_budget') or settings.get('enforce_model_call_limit'):
        raise ValueError('ZCode 原生额度/底层请求次数不可逐请求计量，不能满足已开启的强制预算或次数限制')
    if body.get('kind','chat') not in ('chat','edit') or body.get('continue_task') or body.get('reuse_session'):
        raise ValueError('ZCode 仅支持新咨询/编辑会话；不复用旧会话')
    if body.get('request_output_tokens'):raise ValueError('ZCode 不支持工作台逐请求输出限额')
    if body.get('kind','chat')=='chat' and body.get('test_command'):raise ValueError('只读咨询不执行命令')


def recovery_guard(state):
    if state.get('zcode',{}).get('prompt_attempted') or state.get('zcode',{}).get('cleanup') not in (None,'closed','not_created'):
        raise ValueError('ZCode 原请求可能已送达或会话未关闭，不自动重发')


def answer_text(snapshot):
    parts=[]
    for message in snapshot.get('messages') or []:
        if (message.get('info') or message).get('role')!='assistant':continue
        for part in message.get('parts') or []:
            if part.get('type')=='text' and isinstance(part.get('text'),str):parts.append(part['text'])
    return '\n\n'.join(parts)


def run_task(run,state,model,*,execute=None):
    import workflow_settings,execution_endpoints,delegation_policy,context_manager
    run=Path(run).resolve();project=Path(state['project']).resolve();tracker=execution_result.Tracker('zcode')
    native={'prompt_attempted':False,'cleanup':'not_created'};status,error,answer='failed','','';events=[];before={};changed=[];hashes={};test=None
    tracker.mark('prepare','running')
    def digest(path):return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None
    try:
        if state.get('engine')!='zcode' or model.get('provider')!='zcode' or state['model_id']!=model_row_id(model['model']) or state['model']!=model['model']:
            raise ValueError('ZCode 原生模型与任务身份不匹配')
        if run!=project/'.model-desk/runs'/state['id']:raise ValueError('任务不属于当前项目')
        settings=workflow_settings.snapshot();validate_task(state,settings);execution_endpoints.gate('zcode',model,settings)
        if state.get('route',{}).get('mode')=='auto' and not settings.get('enabled'):raise ValueError('协作开关已关闭')
        recovery_guard(state);config=configuration();info=installation(config)
        if state.get('zcode_connection')!=config or state.get('zcode_tool_revision')!=zcode_tools.revision():raise ValueError('ZCode 原配置或保护版本已变化；旧任务不自动换版')
        chosen=state.get('zcode_selection')
        if chosen!=model.get('native_selection'):raise ValueError('ZCode 模型或思考强度快照不匹配')
        before={f:digest(project/f) for f in state.get('files') or []}
        tool_job=zcode_tools.prepare(run,state,[Path.home()/name for name in ('.zcode','.ssh','.aws','.azure','.gnupg','.kube')]+[config['home']])
        sid='modeldesk-zcode-'+uuid.uuid4().hex
        prompt=(run/'prompt.txt').read_text(encoding='utf-8')
        prompt+='\n\n执行边界：仅使用 modeldesk_zcode 提供的文件和 PowerShell 工具；自行运行测试并报告真实结果。拒绝后停止，不换工具重试；不读写 .model-desk、认证及其他会话，不启用子代理或后台服务，不发布。临时文件可保留，无须为收尾清理。'
        prompt+='\n业务文件：'+json.dumps(state.get('files') or [],ensure_ascii=False)
        if state.get('test_command'):prompt+='\n必须原样执行的自测命令：'+state['test_command']
        job={**config,'action':'task','project':str(project),'session_id':sid,'selection':chosen,'kind':state['kind'],
             'python':sys_executable(),'tools_script':str(HERE/'zcode_tools.py'),'tool_job':str(tool_job),
             'prompt':prompt,'cancel_file':str(run/'cancelled.flag'),'timeout':state.get('timeout',0)}
        registry.patch_task(run,status='running',phase='ZCode 正在连接',started_at=registry.now(),worker_pid=os.getpid(),
            zcode_session_id=sid,zcode=native,executor_version=info['version'],billing_source='zcode-native-account',
            native_credits_used=None,cost_cny=None,cost_settled_cny=None,model_calls=None,
            sandbox_mode='scoped-mcp-tool-argument-guard',subagent_decision={'decision':'deny','reason':'工具白名单不含子代理'})
        def on_event(e):
            if e['type']=='session_creating':native['cleanup']='pending'
            if e['type']=='connected':tracker.mark('prepare','completed');registry.patch_task(run,phase='ZCode 正在执行')
            if e['type']=='prompt_attempted':native['prompt_attempted']=True;tracker.mark('execute','running')
            if e['type']=='closed':native['cleanup']='closed'
            if e['type']=='error':native['outcome_unknown']=native['prompt_attempted']
            registry.patch_task(run,zcode=dict(native),execution_evidence=tracker.snapshot())
        events=(execute or driver)(job,on_event=on_event)
        errors=[e['message'] for e in events if e['type']=='error']
        final=next((e for e in events if e['type']=='result'),{})
        result=final.get('value')
        if errors or result is None:raise ValueError('; '.join(errors) or 'ZCode 没有返回可核对终态')
        if final.get('terminal_event') in ('turn.failed','turn.cancelled') or result.get('session',{}).get('status') in ('error','running','waiting','paused'):
            raise ValueError('ZCode 原生执行没有正常完成，保留失败状态')
        actual=result.get('settings',{}).get('model')
        if actual!=chosen:raise ValueError('ZCode 实际模型/思考强度与派发快照不符或缺失')
        native['observed_selection']=actual
        answer=answer_text(result)
        if not answer.strip():raise ValueError('ZCode 未返回最终答复，未宣称完成')
        tracker.mark('execute','completed');tracker.mark('verify','running')
        proof=zcode_tools.Tools(tool_job).proof
        if not proof['listed'] or proof['stopped']:raise ValueError('ZCode 工具尚未加载或本轮工具被拒绝')
        if state.get('test_command'):
            expected=hashlib.sha256(state['test_command'].encode()).hexdigest()
            matches=[e for e in proof['events'] if e.get('command_hash')==expected]
            last=matches[-1] if matches else {}
            test={'source':'zcode-mcp-command','command_sha256':expected,'exit_code':last.get('exit_code'),
                  'verified':last.get('ok') is True and last.get('exit_code')==0}
            if not test['verified']:raise ValueError('ZCode 指定自测命令没有真实通过证据')
        tracker.mark('verify','completed');status='completed'
    except Exception as exc:
        error=context_manager._redact(str(exc))[:1800]
        tracker.fail(tracker.current,'ZCODE_EXECUTION_FAILED',error,source='adapter',category='unknown')
    finally:
        tracker.mark('session_close','running')
        if native['cleanup']=='pending':status='failed';error=(error+'；原生会话关闭未确认，不能自动重发').strip('；');tracker.fail('session_close','SESSION_CLOSE_UNCONFIRMED',error,source='adapter')
        else:tracker.mark('session_close','completed' if native['cleanup']=='closed' else 'not_applicable')
        for f,old in before.items():
            current=digest(project/f)
            if current!=old:changed.append(f);hashes[f]=current
        if (run/'cancelled.flag').exists():status='cancelled'
        if answer:(run/'answer.txt').write_text(answer,encoding='utf-8')
        proof=locals().get('proof') or {}
        calls=[{'title':e['tool'],'status':'completed' if e['ok'] else 'failed','input_hash':e['input_hash'],
                'exit_code':e.get('exit_code')} for e in proof.get('events',[])]
        tracker.mark('artifacts','completed');tracker.mark('record','completed')
        registry.patch_task(run,status=status,phase='执行完成' if status=='completed' else 'ZCode 执行失败',ended_at=registry.now(),
            zcode=native,answer=answer,error=error or None,changed_files=changed,file_hashes=hashes,test_result=test,
            tool_calls=calls,record_protection=bool(proof.get('listed')),cost_cny=None,cost_settled_cny=None,native_credits_used=None,
            summary='ZCode 原生执行结束；等待主脑独立验收，原生额度用量未返回',exit_code=0 if status=='completed' else 1,
            execution_evidence=tracker.snapshot())
        try:delegation_policy.record_executor(run,{'completed':status=='completed','test':test,'changed_files':changed})
        except Exception:
            tracker.fail('record','REVIEW_RECORD_FAILED','执行报告未写入验收记录',source='persistence')
            registry.patch_task(run,review_error='ZCode 执行报告未写入验收记录',execution_evidence=tracker.snapshot())
    return 0 if status=='completed' else 1


def sys_executable():
    import sys
    return sys.executable


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--run-id',required=True);args=parser.parse_args()
    run=registry.task_dir(args.run_id);state=registry.read_task(run)
    return run_task(run,state,registry.get_model(state['model_id']))

if __name__=='__main__':raise SystemExit(main())
