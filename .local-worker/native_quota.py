"""Read-only native account quotas, independent of task budgets and routing.

Rendering reads local snapshots only. Explicit refreshes query fixed account
interfaces, never generation endpoints. Native apps retain their own login.
"""
from datetime import datetime
import copy
import hashlib
import json
import math
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, build_opener, HTTPRedirectHandler, ProxyHandler

import model_registry as registry

IDS=('workbuddy','zcode','opencode','commandcode')
SETTINGS='native-quota-settings.json'
CACHE='native-quota-cache.json'
INTERVALS=(60,180,300,900)
SOURCES={
    'workbuddy':('WorkBuddy 桌面登录账户','https://www.codebuddy.cn/docs/workbuddy/Credits'),
    'zcode':('ZCode 原生 Start Plan 权益',''),
    'opencode':('OpenCode Go 账户用量','https://opencode.ai/v2/docs/console/go'),
    'commandcode':('CommandCode 原生 /usage','https://commandcode.ai/docs/resources/usage-limits'),
}
_LOCK=threading.RLock()
_REFRESH_LOCKS={key:threading.Lock() for key in IDS}


def number(v):
    return v if type(v) in (int,float) and math.isfinite(v) and v>=0 else None


def timestamp(v):
    if number(v) is not None:
        return v/1000 if v>100000000000 else v
    if isinstance(v,str):
        try:
            return datetime.fromisoformat(v.replace('Z','+00:00')).timestamp()
        except (ValueError,OverflowError):pass
    return None


def clean_limits(rows):
    if not isinstance(rows,list):raise ValueError('额度接口未提供有效明细')
    result=[]
    for r in rows[:40]:
        if not isinstance(r,dict):continue
        values={k:number(r.get(k)) for k in ('remaining','total','used','remaining_percent')}
        if values['remaining_percent'] is not None and values['remaining_percent']>100:values['remaining_percent']=None
        if not any(v is not None for v in values.values()):continue
        result.append({'label':str(r.get('label') or '账户额度')[:120],
                       'unit':str(r.get('unit') or '额度')[:40],**values,
                       'reset_at':timestamp(r.get('reset_at')),
                       'reset_label':str(r.get('reset_label') or '重置/到期')[:60]})
    if not result:raise ValueError('原生接口未提供额度数值；不会将未知显示为零')
    return result


def read(name):
    p=Path(registry.HERE)/name
    if not p.exists():return {}
    data=json.loads(p.read_text(encoding='utf-8'))
    if not isinstance(data,dict):raise ValueError('额度同步配置或快照无效')
    return data


def settings(key):
    if key not in IDS:raise ValueError('此执行端没有原生额度设置')
    v={'enabled':False,'interval_seconds':180,'port':19766,'model_id':'','provider':'account:zai-start-plan'}
    raw=read(SETTINGS).get(key,{})
    if not isinstance(raw,dict) or set(raw)-set(v):raise ValueError('额度同步设置字段无效')
    v.update(raw)
    if type(v['enabled']) is not bool or type(v['interval_seconds']) is not int or v['interval_seconds'] not in INTERVALS:
        raise ValueError('额度同步开关或间隔无效')
    if type(v['port']) is not int or not 1024<=v['port']<=65535:raise ValueError('本机额度端口无效')
    if not isinstance(v['model_id'],str) or len(v['model_id'])>100:raise ValueError('额度账户模型无效')
    if v['provider'] not in ('account:zai-start-plan','account:bigmodel-start-plan'):raise ValueError('ZCode 账户渠道无效')
    return v


def save(key,body):
    if not isinstance(body,dict) or not body or set(body)-{'enabled','interval_seconds','port','model_id','provider'}:
        raise ValueError('额度同步设置需要独立保存')
    with _LOCK:
        old=read(SETTINGS);merged={**settings(key),**body}
        # Validate before publishing; no temporary invalid settings reach readers.
        if type(merged['enabled']) is not bool or type(merged['interval_seconds']) is not int or merged['interval_seconds'] not in INTERVALS:raise ValueError('额度同步间隔无效')
        if type(merged['port']) is not int or not 1024<=merged['port']<=65535:raise ValueError('本机额度端口无效')
        if not isinstance(merged['model_id'],str) or len(merged['model_id'])>100:raise ValueError('额度模型无效')
        if merged['provider'] not in ('account:zai-start-plan','account:bigmodel-start-plan'):raise ValueError('账户渠道无效')
        if key=='opencode' and merged['model_id'] and not any(m['id']==merged['model_id'] for m in opencode_models()):raise ValueError('请选择已配置的 OpenCode Go 账户模型')
        registry.write_json(Path(registry.HERE)/SETTINGS,{**old,key:merged})
    return {'ok':True,'sync':merged,'model_called':False}


def opencode_models():
    return [m for m in registry.list_models()['models'] if urlparse(m.get('api_base','')).hostname=='opencode.ai'
            and any(part in urlparse(m.get('api_base','')).path for part in ('/go/','/go'))
            and not m.get('model','').endswith('-free') and m.get('api_key_set')]


def account_model(s):
    models=opencode_models()
    if s['model_id']:
        return next((m for m in models if m['id']==s['model_id']),None)
    native=[m for m in models if m.get('default_agent')=='opencode']
    return native[0] if len(native)==1 else models[0] if len(models)==1 else None


def binding(key,s,model=None):
    if key=='opencode':
        m=account_model(s)
        return 'go:'+str((m or {}).get('id'))+':'+str((m or {}).get('updated_at',''))
    if key=='workbuddy':
        import workbuddy_worker
        return json.dumps(workbuddy_worker.configuration(),sort_keys=True)+':'+str(s['port'])
    if key=='zcode':
        import zcode_worker
        return json.dumps(zcode_worker.configuration(),sort_keys=True)+':'+str((model or {}).get('native_selection',{}).get('providerId') or s['provider'])
    import commandcode_worker
    return json.dumps(commandcode_worker.configuration(),sort_keys=True)


def view(key,model=None):
    if key not in IDS:return None
    source,url=SOURCES[key]
    base={'capability':'native','source_label':source,'source_url':url,'status':'never','can_refresh':True,
          'fetched_at':None,'last_attempt_at':None,'limits':[],'error':'','message':'账户额度独立于单次任务费用；不同单位不合并。'}
    try:
        s=settings(key);base['sync']=s
        tag=binding(key,s,model)
        row=read(CACHE).get(key,{})
        if row.get('binding')==tag:
            base.update({k:copy.deepcopy(row.get(k)) for k in ('fetched_at','last_attempt_at','limits','error','status') if k in row})
            if base['status']=='ready' and time.time()-base['fetched_at']>max(180,s['interval_seconds']*2):base['status']='stale'
        if key=='opencode':
            m=account_model(s);base['account_models']=[{'id':v['id'],'name':v['name']} for v in opencode_models()]
            base['source_model_id']=(m or {}).get('id','')
            if m:base['source_label']=source+' · '+m['name']
            else:base.update(status='not_connected',can_refresh=False,error='请在额度设置中选择已有 OpenCode Go 账户模型。')
            base['message']='显示 Go 账户共享的 5 小时、周、月用量；免费模型没有独立可查询额度，不将 Go 剩余量当作免费额度。'
        if key in ('workbuddy','zcode'):
            base['message']='通过本机只读桥接调用客户端原生账户服务；未连接时请使用下方连接入口，不自动重启客户端。'
    except (OSError,ValueError,KeyError,TypeError):
        base.update(status='error',error='额度来源或本机设置未就绪，请检查连接设置。',sync={'enabled':False,'interval_seconds':180})
    return base


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):return None


def fetch_opencode(s):
    m=account_model(s)
    if m is None:raise ValueError('请选择已配置的 OpenCode Go 账户模型')
    # Only this fixed read-only account endpoint receives the existing key.
    native=registry.get_model(m['id'],require_enabled=False)
    request=Request('https://opencode.ai/inference/go/v1/usage',headers={
        'Authorization':'Bearer '+native['api_key'],'Accept':'application/json','User-Agent':'opencode/2.1.3'})
    try:
        with build_opener(NoRedirect()).open(request,timeout=20) as response:
            raw=response.read(262145)
        if len(raw)>262144:raise ValueError('账户响应超过上限')
        data=json.loads(raw)['usage']
    except HTTPError as e:
        raise ValueError('OpenCode Go 账户查询失败（HTTP %d）；请检查账户权益和连接。'%e.code) from None
    rows=[]
    for key,label in [('rolling','5 小时窗口'),('weekly','每周窗口'),('monthly','每月窗口')]:
        v=data.get(key,{})
        pct=number(v.get('percent'))
        if pct is not None and pct<=100:rows.append({'label':label,'unit':'%','remaining_percent':100-pct,
            'remaining':100-pct,'used':pct,'total':100,'reset_at':v.get('resetsAt'),'reset_label':'重置'})
    return clean_limits(rows)


def run_json(argv,*,env=None,timeout=35,prefix=''):
    try:
        p=subprocess.run(argv,capture_output=True,text=True,encoding='utf-8',errors='replace',
                         timeout=timeout,env=env,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    except subprocess.TimeoutExpired:raise ValueError('原生额度查询超时，请稍后手动刷新；未调用模型') from None
    for line in p.stdout.splitlines():
        if prefix and not line.startswith(prefix):continue
        try:v=json.loads(line[len(prefix):])
        except ValueError:continue
        if isinstance(v,dict) and isinstance(v.get('limits'),list) and p.returncode==0:return clean_limits(v['limits'])
    raise ValueError('原生额度查询失败；请确认客户端已登录、本机桥接与版本匹配。')


def fetch_commandcode(s):
    import commandcode_worker
    if not commandcode_worker.availability().get('ready'):raise ValueError('请先配置已适配的 CommandCode 客户端')
    c=commandcode_worker.configuration();cli=str(Path(c['cli']).with_name('cli.mjs'))
    loader=Path(__file__).resolve().parent/'agent-runtime/commandcode-quota-loader.mjs'
    env={**os.environ,'MODEL_DESK_QUOTA_CLI':cli,'DO_NOT_TRACK':'1','CMD_TELEMETRY_DISABLED':'1'}
    return run_json([c['node'],'--no-warnings','--experimental-loader',loader.as_uri(),cli],env=env,prefix='MODEL_DESK_QUOTA:')


def fetch_workbuddy(s):
    import workbuddy_worker
    import runtime_paths
    config=workbuddy_worker.configuration();workbuddy_worker.installation(config)
    assets=Path(config['home'])/'resources/app.asar.unpacked/renderer/assets'
    usage=list(assets.glob('UserMenu-*.js'));environment=list(assets.glob('environment-*.js'))
    if len(usage)!=1 or len(environment)!=1:raise ValueError('WorkBuddy 原生额度模块版本不匹配')
    with tempfile.TemporaryDirectory(prefix='modeldesk-quota-') as temp:
        job=Path(temp)/'job.json';job.write_text(json.dumps({'home':config['home'],'port':s['port'],
            'usageModule':usage[0].name,'environmentModule':environment[0].name}),encoding='utf-8')
        return run_json([runtime_paths.node_executable(),str(Path(__file__).resolve().parent/'agent-runtime/workbuddy-quota.mjs'),str(job)])


def fetch_zcode(s,model):
    import zcode_worker
    provider=(model or {}).get('native_selection',{}).get('providerId') or s['provider']
    data=zcode_worker.refresh_usage(provider)['usage']
    return clean_limits([{**r,'reset_at':r.get('expires_at')} for r in data['limits']])


def refresh(key,model=None,automatic=False):
    if key not in IDS:raise ValueError('执行端无效')
    if not _REFRESH_LOCKS[key].acquire(blocking=False):return {'ok':False,'busy':True,'model_called':False}
    try:
        s=settings(key);tag=binding(key,s,model);now=time.time()
        with _LOCK:
            previous=read(CACHE).get(key,{})
        delay=max(180,s['interval_seconds']*2) if previous.get('status')=='error' else s['interval_seconds']
        if automatic and (not s['enabled'] or previous.get('binding')==tag and now-previous.get('last_attempt_at',0)<delay):
            return {'ok':True,'cooldown':True,'quota':view(key,model),'model_called':False}
        if previous.get('binding')==tag and now-previous.get('last_attempt_at',0)<5:
            return {'ok':True,'cooldown':True,'quota':view(key,model),'model_called':False}
        try:
            rows=fetch_zcode(s,model) if key=='zcode' else globals()['fetch_'+key](s)
            result={'binding':tag,'status':'ready','limits':rows,'fetched_at':time.time(),'last_attempt_at':now,'error':''}
        except (ValueError,OSError,KeyError,TypeError,URLError):
            result={'binding':tag,'status':'error','limits':[],'fetched_at':None,'last_attempt_at':now,
                    'error':{'workbuddy':'WorkBuddy 本机额度桥接未连接，请使用额度连接入口；正在运行的客户端不会被自动重启。',
                             'zcode':'ZCode 原生额度桥接未连接或登录账户权益不可用，请先连接并确认账户渠道。',
                             'opencode':'OpenCode Go 账户额度查询失败，请核对已有模型的账户连接。',
                             'commandcode':'CommandCode 原生额度查询失败，请确认客户端登录与受支持版本。'}[key]}
            with _LOCK:
                old=read(CACHE).get(key,{})
                if old.get('binding')==tag and old.get('limits'):
                    result['limits']=old['limits'];result['fetched_at']=old.get('fetched_at')
        with _LOCK:registry.write_json(Path(registry.HERE)/CACHE,{**read(CACHE),key:result})
        return {'ok':result['status']=='ready','quota':view(key,model),'model_called':False}
    finally:_REFRESH_LOCKS[key].release()


def connect(key):
    if key=='zcode':
        import zcode_worker
        c=zcode_worker.configuration()
        try:
            if zcode_worker.bridge_connected(c):return {'ok':True,'message':'ZCode 原生额度桥接已连接。','model_called':False}
        except (OSError,ValueError):pass
        if zcode_worker.desktop_running(c):return {'ok':False,'needs_restart':True,'message':'ZCode 正在运行且未开启本机桥接。请保存工作并退出后再连接；不会自动关闭窗口。','model_called':False}
        return zcode_worker.launch()
    if key!='workbuddy':raise ValueError('此额度源无需本机桥接')
    import psutil
    import workbuddy_worker
    c=workbuddy_worker.configuration();info=workbuddy_worker.installation(c);s=settings(key)
    try:
        with build_opener(ProxyHandler({}),NoRedirect()).open('http://127.0.0.1:%d/json/list'%s['port'],timeout=2) as response:pages=json.load(response)
        expected=(Path(c['home'])/'resources/app.asar/renderer').as_uri().casefold()+'/'
        if sum(p.get('type')=='page' and p.get('url','').split('?')[0].casefold().startswith(expected) for p in pages)==1:
            return {'ok':True,'message':'WorkBuddy 原生额度桥接已连接。','model_called':False}
    except (OSError,ValueError,TypeError):pass
    for process in psutil.process_iter(['exe']):
        try:
            if process.info['exe'] and Path(process.info['exe']).resolve()==Path(info['executable']).resolve():
                return {'ok':False,'needs_restart':True,'message':'WorkBuddy 已运行。请保存工作并退出客户端，再点击连接；工作台不会关闭现有窗口。','model_called':False}
        except (psutil.Error,OSError):pass
    subprocess.Popen([info['executable'],'--remote-debugging-port='+str(s['port']),'--remote-debugging-address=127.0.0.1'],
                     stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
                     creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    return {'ok':True,'message':'已请求打开带本机只读额度桥接的 WorkBuddy，请确认登录后刷新额度。','model_called':False}
