"""Native model metadata, never a generation or authentication-file API."""
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time
import model_registry as registry

CACHE='native-model-catalog.json'
MAX_AGE=86400
DEFAULT={'value':'default','label':'原生默认'}


def read():
    p=Path(registry.HERE)/CACHE
    if not p.exists():return {}
    d=json.loads(p.read_text(encoding='utf-8'))
    if not isinstance(d,dict):raise ValueError('原生模型目录缓存无效')
    return d


def write(engine,models,identity):
    safe=[]
    for m in models[:2000]:
        if not isinstance(m,dict) or not isinstance(m.get('model'),str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_./:+@-]{0,199}',m['model']):continue
        levels=[]
        for v in m.get('reasoning') or []:
            value=v.get('value') if isinstance(v,dict) else v
            if isinstance(value,str) and re.fullmatch(r'[a-zA-Z0-9_-]{1,40}',value) and value!='default':
                levels.append({'value':value,'label':str(v.get('label',value) if isinstance(v,dict) else value)[:80]})
        safe.append({k:m[k] for k in ('native_id','contextWindow','maxOutputTokens','minimumPlan','control_id') if k in m}|
                    {'model':m['model'],'name':str(m.get('name') or m['model'])[:160],'reasoning':[DEFAULT,*levels],
                     'selectable':m.get('selectable',True) is True,'unavailable_reason':str(m.get('unavailable_reason') or '')[:200]})
    if not safe:raise ValueError('原生接口没有返回可用模型目录')
    entry={'identity':identity,'fetched_at':time.time(),'models':safe}
    with registry.file_lock(Path(registry.HERE)/'native-model-catalog.lock'):
        registry.write_json(Path(registry.HERE)/CACHE,{**read(),engine:entry})
    return entry


def entry(engine,code):
    try:
        d=read().get(engine,{})
        if d.get('identity')!=identity(engine) or time.time()-d.get('fetched_at',0)>MAX_AGE:return None
        return next((m for m in d.get('models',[]) if m.get('model')==code and m.get('selectable')),None)
    except (OSError,ValueError,KeyError,TypeError):return None


def identity(engine):
    if engine=='opencode':
        import opencode_worker
        p=opencode_worker.executable();return f'{p}:{p.stat().st_size}:{p.stat().st_mtime_ns}'
    if engine=='commandcode':
        import commandcode_worker
        c=commandcode_worker.configuration();p=Path(c['cli']).with_name('cli.mjs')
        return f'{p}:{p.stat().st_size}:{p.stat().st_mtime_ns}'
    if engine=='workbuddy':
        import workbuddy_worker
        return workbuddy_worker.installation(workbuddy_worker.configuration())['fingerprint']
    if engine=='zcode':
        import zcode_worker
        return zcode_worker.installation(zcode_worker.configuration())['fingerprint']
    raise ValueError('原生执行端无效')


def refresh(engine):
    if engine=='opencode':
        import opencode_worker
        result=subprocess.run([str(opencode_worker.executable()),'models'],capture_output=True,text=True,
                              encoding='utf-8',errors='replace',timeout=30,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        if result.returncode:raise ValueError('OpenCode 原生模型目录读取失败；未调用模型')
        models=[]
        for line in result.stdout.splitlines():
            code=re.sub(r'\x1b\[[0-9;]*m','',line).strip()
            if re.fullmatch(r'opencode(?:-go)?/[A-Za-z0-9_./:+@-]+',code):
                models.append({'native_id':code,'model':code,'name':code.split('/',1)[1]})
    elif engine=='commandcode':
        import commandcode_worker
        if not commandcode_worker.availability().get('ready'):raise ValueError('CommandCode 原生客户端未就绪')
        c=commandcode_worker.configuration();cli=str(Path(c['cli']).with_name('cli.mjs'))
        loader=Path(__file__).resolve().parent/'agent-runtime/commandcode-models-loader.mjs'
        result=subprocess.run([c['node'],'--no-warnings','--experimental-loader',loader.as_uri(),cli],capture_output=True,
            text=True,encoding='utf-8',errors='replace',timeout=25,env={**os.environ,'MODEL_DESK_CATALOG_CLI':cli,'DO_NOT_TRACK':'1'},
            creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        data=next((json.loads(s[len('MODEL_DESK_CATALOG:'):]) for s in result.stdout.splitlines() if s.startswith('MODEL_DESK_CATALOG:')), {})
        if result.returncode or not data.get('models'):raise ValueError('CommandCode 原生目录读取失败；未调用模型')
        models=data['models']
    elif engine=='workbuddy':
        import workbuddy_worker
        data=workbuddy_worker.refresh_catalog()
        models=[{'model':m['model_id'],'name':m['name'],'reasoning':m.get('reasoning',[]),'control_id':m.get('control_id')}
                for m in data['models']]
    elif engine=='zcode':
        import zcode_worker
        data=zcode_worker.refresh_catalog()
        models=[{**m,'model':m['providerId']+'/'+m['modelId'],'name':m['modelId']} for m in data['models']]
    else:raise ValueError('原生执行端无效')
    return write(engine,models,identity(engine))


def control_options(options):
    """Only an explicitly advertised native reasoning selector is accepted."""
    for option in options or []:
        if option.get('id') not in ('reasoning','reasoning_effort','thought_level','thinking','effort','variant','model_variant'):continue
        values=[]
        def collect(rows):
            for item in rows or []:
                if isinstance(item.get('options'),list):collect(item['options'])
                elif isinstance(item.get('value'),str):values.append({'value':item['value'],'label':item.get('name') or item.get('label') or item['value']})
        collect(option.get('options'))
        if values:return {'control_id':option['id'],'reasoning':[DEFAULT,*[v for v in values if v['value']!='default']]}
    return {'control_id':None,'reasoning':[DEFAULT]}


def opencode_details(code,source_id):
    import opencode_worker as oc
    row=entry('opencode',code)
    if not row:raise ValueError('模型未在当前原生目录中，请先刷新')
    source=registry.get_model(source_id,require_enabled=False)
    expected='opencode-go/' if '/go/' in source.get('api_base','') else 'opencode/'
    if not code.startswith(expected):raise ValueError('原生渠道与 API 连接不匹配')
    client=None
    with tempfile.TemporaryDirectory(prefix='modeldesk-model-metadata-') as tmp:
        root=Path(tmp);home=root/'home';home.mkdir();model_id=code
        config=oc.native_config(root,[],'chat','modeldesk',model_id)
        config_file=home/'config/opencode/opencode.json';registry.write_json(config_file,config)
        env={k:v for k,v in os.environ.items() if not any(s in k.upper() for s in ('OPENCODE','API_KEY','TOKEN','SECRET','XDG_'))}
        for name in ('config','data','cache','state'):
            (home/name).mkdir(exist_ok=True);env['XDG_'+name.upper()+'_HOME']=str(home/name)
        if source.get('api_key'):env['OPENCODE_API_KEY']=source['api_key']
        process=subprocess.Popen([str(oc.executable()),'acp'],cwd=root,env=env,stdin=subprocess.PIPE,stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,text=True,encoding='utf-8',bufsize=1,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        try:
            client=oc.ACP(process,root,[],'chat',model_id=model_id)
            client.connect('modeldesk')
            result=control_options(getattr(client,'config_options',[]))
        finally:
            if client:client.close()
            elif process.poll() is None:process.terminate();process.wait(8)
    with registry.file_lock(Path(registry.HERE)/'native-model-catalog.lock'):
        data=read();data['opencode']['models']=[{**m,**result} if m['model']==code else m for m in data['opencode']['models']]
        registry.write_json(Path(registry.HERE)/CACHE,data)
    return result


def workbuddy_details(code):
    import workbuddy_worker as worker
    import workbuddy_host_bridge as bridge
    if not entry('workbuddy',code):raise ValueError('模型未在当前 WorkBuddy 目录中，请先刷新')
    config=worker.configuration();info=worker.installation(config);client=None
    with tempfile.TemporaryDirectory(prefix='modeldesk-wb-model-metadata-') as tmp:
        try:
            client=bridge.create_client(info,Path(tmp),config['profile'],code)
            response=client.connect()
            if response.get('current_model_id')!=code:
                changed=client.request('session/set_config_option',{'sessionId':client.session_id,'configId':'model','value':code})
                model=next((o for o in changed.get('configOptions',[]) if o.get('id')=='model'),None)
                if not model or model.get('currentValue')!=code:raise ValueError('WorkBuddy 未确认所选模型，未发送提示词')
                client.config_options=changed.get('configOptions') or []
            result=control_options(client.config_options)
        finally:
            if client:client.close()
    with registry.file_lock(Path(registry.HERE)/'native-model-catalog.lock'):
        data=read();data['workbuddy']['models']=[{**m,**result} if m['model']==code else m for m in data['workbuddy']['models']]
        registry.write_json(Path(registry.HERE)/CACHE,data)
    return result


def apply_control(client,control):
    if not control or control.get('value') in (None,'default'):return
    options=getattr(client,'config_options',[])
    advertised=control_options(options)
    if advertised['control_id']!=control.get('config_id') or control['value'] not in {v['value'] for v in advertised['reasoning']}:
        raise ValueError('原生客户端未声明所选思考强度，未发送提示词')
    result=client.request('session/set_config_option',{'sessionId':client.session_id if hasattr(client,'session_id') else client.session,
        'configId':control['config_id'],'value':control['value']})
    option=next((r for r in result.get('configOptions',[]) if r.get('id')==control['config_id']),None)
    if not option or option.get('currentValue')!=control['value']:raise ValueError('原生客户端未确认所选思考强度，未发送提示词')
