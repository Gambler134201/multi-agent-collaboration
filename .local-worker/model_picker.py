"""Shared model picker. Queries metadata; applies explicit NEW-task choices.

Keys stay in the existing registry. Selection creates an immutable model row
when settings differ, so queued/running tasks retain their prior model ID.
"""
import copy
import hashlib
import json
from pathlib import Path
import threading
import time
from urllib.request import Request, build_opener, ProxyHandler, HTTPRedirectHandler
from urllib.error import HTTPError
from urllib.parse import urlparse

import model_registry as registry
import execution_endpoints as endpoints
import thinking_config
import native_models

CACHE='model-picker-catalog.json'
_LOCK=threading.RLock()
DEFAULT={'value':'default','label':'沿用默认'}


def digest(value):
    return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()


def read():
    p=Path(registry.HERE)/CACHE
    if not p.exists():return {}
    d=json.loads(p.read_text(encoding='utf-8'))
    if not isinstance(d,dict):raise ValueError('模型选择目录缓存无效')
    return d


def source_binding(row):
    return digest({k:row.get(k) for k in ('id','provider','api_base','secret','api_key_env','default_agent','model')})


def raw_models():
    with registry.registry_lock():return registry.read_registry()['models']


def levels(model,engine=None):
    native=model.get('native_reasoning') or {}
    if native.get('options'):
        return [DEFAULT,*[{'value':v['value'],'label':v.get('label',v['value'])} for v in native['options'] if v['value']!='default']]
    if engine in ('opencode','commandcode','workbuddy'):
        code=model.get('native_model_id') or (('opencode-go/' if '/go/' in model.get('api_base','') else 'opencode/')+model['model'] if engine=='opencode' else model['model'])
        item=native_models.entry(engine,code)
        if item:return [DEFAULT,*[{'value':v['value'],'label':thinking_config.LABELS.get(v['value'],v.get('label',v['value']))} for v in item['reasoning'] if v['value']!='default']]
    if model.get('provider')=='zcode':
        import zcode_worker
        entry=zcode_worker.catalog_model(model['model'])
        return [DEFAULT,*[{'value':v,'label':thinking_config.LABELS.get(v,v)} for v in (entry or {}).get('reasoning',[]) if v!='default']]
    info=thinking_config.profile(model)
    return [{'value':v,'label':thinking_config.LABELS.get(v,v)} for v in info.get('supported',['default'])]


def selected_reasoning(model):
    if model.get('provider')=='zcode':return model.get('native_selection',{}).get('options',{}).get('reasoningLevel','default')
    return (model.get('native_reasoning') or {}).get('value') or model.get('thinking_level','default')


def sources(engine,raw,workflow):
    result=[]
    if engine in ('pi','dsh'):
        for m in raw:
            if m.get('provider') not in ('workbuddy','zcode'):
                result.append({'id':'api:'+m['id'],'label':m.get('name',m['model'])+' · '+(urlparse(m.get('api_base','')).hostname or 'API'),
                               'kind':'api','source_ref':m['id']})
    if engine in ('workbuddy','zcode'):
        result.append({'id':'native:'+engine,'label':endpoints.LABELS[engine]+' 登录账户','kind':'native','source_ref':None})
    if engine in ('opencode','commandcode'):
        for m in raw:
            host=urlparse(m.get('api_base','')).hostname
            if m.get('default_agent')!=engine:continue
            if engine=='opencode' and host=='opencode.ai':
                kind='opencode-go' if '/go/' in m.get('api_base','') else 'opencode'
                result.append({'id':'native:'+engine+':'+m['id'],'label':('OpenCode Go' if kind=='opencode-go' else 'OpenCode 免费 / Zen')+' · '+m.get('name',m['model']),
                               'kind':'native','source_ref':m['id'],'native_provider':kind})
            elif engine=='commandcode' and host=='api.commandcode.ai':
                result.append({'id':'native:'+engine+':'+m['id'],'label':'CommandCode 登录账户模型 · '+m.get('name',m['model']),
                               'kind':'native','source_ref':m['id']})
    return [{'id':'saved','label':'已保存模型','kind':'saved','source_ref':None},*result]


def catalog(engine,workflow,*,raw=None,controls=None):
    if engine not in endpoints.IDS:raise ValueError('执行端无效')
    raw=raw_models() if raw is None else raw;cached=read().get(engine,{})
    ss=sources(engine,raw,workflow);models=[];by_id={m['id']:m for m in raw}
    controls=endpoints.snapshot() if controls is None else controls
    if not controls['readable']:raise ValueError(controls['error'])
    current=controls['endpoints'][engine]['current_model']
    associated=[m for m in raw if endpoints.associated(m,workflow)==engine]
    enabled=[m for m in associated if m.get('enabled',True)]
    if current is None and len(enabled)==1:current=enabled[0]['id']
    warnings=[]
    for m in associated:
        try:options=levels(m,engine)
        except (OSError,ValueError,KeyError):options=[DEFAULT]
        code=m.get('native_model_id') or (('opencode-go/' if '/go/' in m.get('api_base','') else 'opencode/')+m['model'] if engine=='opencode' else m['model'])
        models.append({'id':'saved:'+m['id'],'source_id':'saved','name':m.get('name',m['model']),'model':code,
                       'reasoning':options,'current_reasoning':selected_reasoning(m),'registered_id':m['id'],
                       'selectable':m.get('enabled',True),'unavailable_reason':'' if m.get('enabled',True) else '此模型已停用',
                       '_source_ref':m['id'],'_kind':'saved'})
    native_cache=native_models.read().get(engine,{}) if engine in ('opencode','commandcode','workbuddy','zcode') else {}
    for source in ss:
        if source['id']=='saved':continue
        entry=cached.get(source['id'],{})
        ref=by_id.get(source['source_ref'])
        valid=entry.get('binding')==(source_binding(ref) if ref else native_identity(engine))
        if not valid:continue
        stale=time.time()-entry.get('fetched_at',0)>3600
        for m in entry.get('models',[]):
            m={**m,'reasoning':[{'value':v['value'],'label':thinking_config.LABELS.get(v['value'],v.get('label',v['value']))} for v in m.get('reasoning',[])]}
            item={**m,'id':'choice:'+digest([source['id'],m['model']])[:24],'source_id':source['id'],
                  '_source_ref':source['source_ref'],'_kind':source['kind'],'registered_id':None,'current_reasoning':'default'}
            if stale:item.update(selectable=False,unavailable_reason='目录已过期，请刷新此来源')
            models.append(item)
    if engine in ('workbuddy','zcode') and not native_cache.get('models'):warnings.append('尚未读取该执行端原生模型，点击“刷新模型”。')
    if engine in ('opencode','commandcode') and len(ss)==1:warnings.append('请先在“管理连接”中保存此执行端对应的账户连接。')
    result={'endpoint':engine,'label':endpoints.LABELS[engine],'sources':ss,'models':models,
            'current_model':'saved:'+current if current else '',
            'current_reasoning':selected_reasoning(by_id[current]) if current in by_id else 'default',
            'warning':' '.join(warnings),'note':'目录支持不等于当前账号有额度；保存不运行模型。',
            'revision':digest([[(m['id'],source_binding(m),m.get('enabled'),selected_reasoning(m)) for m in raw],controls,cached])}
    identity=native_identity(engine) if engine in ('workbuddy','zcode','opencode','commandcode') else None
    for choice in models:
        # A slot draft binds this choice and its source, independently of other
        # models opened in the same catalog after it was selected.
        ref=by_id.get(choice['_source_ref'])
        choice['choice_revision']=digest([engine,identity,ref,choice])
    return result


def native_identity(engine):
    try:
        value=native_models.identity(engine)
        if engine=='workbuddy':
            import workbuddy_worker
            value=digest([value,workbuddy_worker.configuration()])
        if engine=='zcode':
            import zcode_worker
            value=digest([value,zcode_worker.configuration()])
        return value
    except (OSError,ValueError,KeyError):return 'unavailable'


def public(data):
    return {**data,'models':[{k:v for k,v in m.items() if not k.startswith('_')} for m in data['models']]}


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):return None


def api_catalog(source):
    model=registry.get_model(source['source_ref'],require_enabled=False)
    handlers=[NoRedirect()]+([ProxyHandler({})] if model.get('bypass_proxy') else [])
    headers={'Accept':'application/json'}
    if model.get('api_key'):headers['Authorization']='Bearer '+model['api_key']
    try:
        with build_opener(*handlers).open(Request(model['api_base'].rstrip('/')+'/models',headers=headers),timeout=20) as response:
            raw=response.read(4000001)
        if len(raw)>4000000:raise ValueError()
        data=json.loads(raw)
        result=[]
        for row in data.get('data',[])[:2000]:
            code=row.get('id')
            if not isinstance(code,str) or not 0<len(code)<=200:continue
            opts=[DEFAULT]
            # Generic /models is not a universal reasoning contract. Only
            # existing provider adapters may declare non-default wire options.
            info=thinking_config.profile({**model,'model':code,'thinking_level':'default','thinking_info':None})
            opts=[{'value':v,'label':thinking_config.LABELS.get(v,v)} for v in info.get('supported',['default'])]
            result.append({'model':code,'name':str(row.get('name') or code)[:160],'reasoning':opts,'selectable':True,'unavailable_reason':''})
        if not result:raise ValueError()
        return result
    except HTTPError as error:
        raise ValueError('此服务拒绝模型目录查询（HTTP %d）。可选择已保存模型或管理连接；没有发送生成请求。'%error.code) from None
    except Exception:
        raise ValueError('读取此 API 连接的模型目录失败，请核对 Base URL、Key 和连接状态；没有发送生成请求。') from None


def refresh(engine,source_id,workflow):
    data=catalog(engine,workflow)
    source=next((s for s in data['sources'] if s['id']==source_id),None)
    if source is None or source_id=='saved':raise ValueError('请先选择要读取的 API 或原生来源')
    ref=next((m for m in raw_models() if m['id']==source['source_ref']),None)
    before=source_binding(ref) if ref else native_identity(engine)
    if source['kind']=='api':models=api_catalog(source)
    else:
        entry=native_models.refresh(engine);models=[]
        for row in entry['models']:
            if engine=='opencode' and not row['model'].startswith(source['native_provider']+'/'):continue
            models.append(row)
    raw=raw_models();ref=next((m for m in raw if m['id']==source['source_ref']),None)
    bound=source_binding(ref) if ref else native_identity(engine)
    if bound!=before:raise ValueError('读取期间连接或客户端发生变化，请重新刷新')
    with registry.file_lock(Path(registry.HERE)/'model-picker-catalog.lock'):
        cache=read();section=cache.setdefault(engine,{})
        section[source_id]={'binding':bound,'fetched_at':time.time(),'models':models}
        registry.write_json(Path(registry.HERE)/CACHE,cache)
    return {'ok':True,'catalog':public(catalog(engine,workflow)),'model_called':False}


def details(engine,choice_id,workflow):
    data=catalog(engine,workflow);choice=next((m for m in data['models'] if m['id']==choice_id),None)
    if not choice or not choice['selectable']:raise ValueError('模型不可用，请刷新目录')
    if engine in ('opencode','workbuddy') and choice['_kind']=='native':
        if engine=='opencode':native_models.opencode_details(choice['model'],choice['_source_ref'])
        else:native_models.workbuddy_details(choice['model'])
        # Refresh the source view from the verified details cache, no second CLI listing.
        with registry.file_lock(Path(registry.HERE)/'model-picker-catalog.lock'):
            cached=read();entry=cached[engine][choice['source_id']]
            native=native_models.entry(engine,choice['model'])
            entry['models']=[native if m['model']==choice['model'] else m for m in entry['models']]
            registry.write_json(Path(registry.HERE)/CACHE,cached)
    return {'ok':True,'catalog':public(catalog(engine,workflow)),'model_called':False}


def _new_row(engine,choice,reasoning,raw):
    base=next((copy.deepcopy(m) for m in raw if m['id']==choice['_source_ref']),None)
    if choice['_kind']=='saved' and base and selected_reasoning(base)==reasoning:return base,False
    if not base:
        base={'provider':engine,'api_base':'','secret':'','api_key_env':'','enabled':True,'bypass_proxy':False,
              'max_input_tokens':32768,'max_output_tokens':4096,'preferred_tasks':['单文件修改','文案/文档','信息整理'],
              'capability_notes':'来自客户端原生目录；能力尚未通过真实模型任务实测。','max_task_complexity':'simple',
              'cost_preference':'balanced','pricing_cny':None,'agent_preset':'standard','subagent':{}}
    code=choice['model'];base.update(default_agent=engine,enabled=True,thinking_level='default',last_check=None)
    old_code=base.get('native_model_id') or base.get('model')
    if old_code!=code:
        # Neither pricing nor measured capability transfers to a different model.
        base.update(pricing_cny=None,subagent={},model_version='',max_task_complexity='simple',
                    capability_notes='来自模型目录；当前型号能力尚未通过实际任务独立验收。')
    if engine=='zcode':
        import zcode_worker
        base.update(provider='zcode',model=code,native_selection=zcode_worker.selection(code,None if reasoning=='default' else reasoning))
    elif engine in ('opencode','commandcode','workbuddy'):
        native=native_models.entry(engine,code)
        if choice['_kind']=='saved':native=native or {'reasoning':choice['reasoning'],'control_id':(base.get('native_reasoning') or {}).get('config_id')}
        if not native:raise ValueError('此原生模型尚未取得匹配目录，请刷新后再选择')
        if reasoning!='default' and (reasoning not in {v['value'] for v in native.get('reasoning',[])} or engine in ('workbuddy','opencode') and not native.get('control_id')):
            raise ValueError('原生思考强度元数据已变化，请重新读取该模型')
        if engine=='opencode':
            base.update(model=code.split('/',1)[1],native_model_id=code)
        else:base['model']=code
        if engine=='workbuddy':base['provider']='workbuddy'
        base['native_reasoning']={'value':reasoning,'config_id':native.get('control_id'),'options':native.get('reasoning') or [DEFAULT]}
    else:
        base.pop('native_model_id',None);base.pop('native_reasoning',None);base.pop('native_selection',None)
        base['model']=code;base['thinking_level']=reasoning
        if (base.get('thinking_info') or {}).get('model')!=code:base['thinking_info']=None
        thinking_config.validate(base)
    original_id=base['id'] if 'id' in base else None
    ident='pick-'+digest([engine,choice['source_id'],code,reasoning,source_binding(base)])[:20]
    existing=next((m for m in raw if m['id']==ident),None)
    if existing and existing.get('enabled',True):return existing,False
    if existing:ident='pick-'+digest([ident,registry.now()])[:20]
    base.update(id=ident,name=(endpoints.LABELS[engine]+' · '+choice['name'])[:100],created_at=registry.now(),
                picker_source={'source_id':choice['source_id'],'source_ref':choice['_source_ref'],'model':code})
    return base,True


def select(engine,body,workflow):
    if (not isinstance(body,dict) or set(body)!={'choice_id','reasoning','revision','pin'} or type(body['pin']) is not bool
            or any(not isinstance(body[k],str) for k in ('choice_id','reasoning','revision'))):raise ValueError('模型与思考强度需完整确认保存')
    import routing_priority
    with _LOCK,registry.file_lock(Path(registry.HERE)/'model-picker-settings.lock'),registry.registry_lock(),registry.file_lock(Path(registry.HERE)/'execution-endpoints.lock'),registry.file_lock(Path(registry.HERE)/'routing-priority.lock'):
        raw=registry.read_registry();controls=endpoints.snapshot();policy=routing_priority.snapshot()
        data=catalog(engine,workflow,raw=raw['models'],controls=controls)
        if data['revision']!=body['revision']:raise ValueError('模型目录或连接已经变化，请刷新后重新选择')
        choice=next((m for m in data['models'] if m['id']==body['choice_id']),None)
        if not choice or not choice['selectable']:raise ValueError('请选择当前目录中可用的模型')
        if body['reasoning'] not in {r['value'] for r in choice['reasoning']}:raise ValueError('此模型未声明所选思考强度')
        if not controls['readable'] or body['pin'] and not policy['readable']:raise ValueError('执行端或自动顺序设置无法核实，未保存')
        paths=[registry.REGISTRY,Path(registry.HERE)/endpoints.CONFIG_NAME,Path(registry.HERE)/routing_priority.CONFIG]
        # Keep settings locked through rollback and publication.
        before={p:p.read_bytes() if p.exists() else None for p in paths}
        row,new=_new_row(engine,choice,body['reasoning'],raw['models'])
        next_policy=copy.deepcopy(policy)
        if body['pin'] and policy['enabled']:
            owned={m['id'] for m in raw['models'] if endpoints.associated(m,workflow)==engine}
            slots=[];inserted=False
            for slot in policy['slots']:
                if slot['model_id'] in owned:
                    if not inserted:slots.append({'label':row['name'],'model_id':row['id'],'unavailable_reason':''});inserted=True
                else:slots.append(slot)
            if not inserted:
                if len(slots)>=20:raise ValueError('自动顺序已满，请先管理顺序')
                slots.append({'label':row['name'],'model_id':row['id'],'unavailable_reason':''})
            next_policy={'schema':1,'enabled':True,'slots':slots}
        try:
            if new:registry.write_json(registry.REGISTRY,{**raw,'models':raw['models']+[row]})
            if body['pin'] and policy['enabled']:registry.write_json(paths[2],next_policy)
            controls['endpoints'][engine]['current_model']=row['id']
            registry.write_json(paths[1],{'schema':1,'endpoints':controls['endpoints']})
        except BaseException:
            for p,previous in before.items():
                if previous is None:
                    if p.exists():p.unlink()
                else:
                    temp=p.with_name(p.name+'.picker-restore');temp.write_bytes(previous);temp.replace(p)
            raise
    return {'ok':True,'model_id':row['id'],'model_called':False,'message':'当前模型和思考强度已保存，仅影响新任务；已有任务保持原设置。'}


def save_priority(body,workflow=None):
    """Register native/API variants and publish the ordered slots together.

    A chooser only returns a public draft. This explicit final save is the only
    mutation; endpoint defaults and queued/running records are never written.
    """
    import re
    import routing_priority as priority
    if workflow is None:
        import workflow_settings
        workflow=workflow_settings.snapshot()
    if not isinstance(body,dict) or set(body)!={'enabled','slots','expected','selections'}:
        raise ValueError('客户端模型顺序需完整确认保存')
    selections=body['selections']
    if not isinstance(selections,list) or len(selections)>20 or not isinstance(body['slots'],list):raise ValueError('顺位选择参数无效')
    with _LOCK,registry.registry_lock(),registry.file_lock(Path(registry.HERE)/'execution-endpoints.lock'),registry.file_lock(Path(registry.HERE)/'routing-priority.lock'):
        raw=registry.read_registry();controls=endpoints.snapshot();rows=[];resolved={};catalogs={}
        for selection in selections:
            if (not isinstance(selection,dict) or set(selection)!={'id','endpoint','choice_id','reasoning','revision'} or
                    any(not isinstance(selection[k],str) for k in selection) or
                    not re.fullmatch(r'pending:[a-zA-Z0-9-]{1,80}',selection['id']) or selection['id'] in resolved):
                raise ValueError('顺位客户端模型草稿无效')
            if not any(isinstance(s,dict) and s.get('model_id')==selection['id'] for s in body['slots']):raise ValueError('不能保存未使用的模型草稿')
            engine=selection['endpoint']
            if engine not in catalogs:catalogs[engine]=catalog(engine,workflow,raw=raw['models'],controls=controls)
            choice=next((m for m in catalogs[engine]['models'] if m['id']==selection['choice_id']),None)
            if not choice or not choice['selectable'] or choice['choice_revision']!=selection['revision']:
                raise ValueError('顺位模型或连接已变化，请重新选择该顺位的客户端模型')
            if selection['reasoning'] not in {v['value'] for v in choice['reasoning']}:raise ValueError('顺位模型未声明所选思考强度')
            row,new=_new_row(engine,choice,selection['reasoning'],raw['models'])
            if new and row['id'] not in {m['id'] for m in rows}:rows.append(row)
            resolved[selection['id']]=row
        slots=[{**slot,'model_id':resolved[slot['model_id']]['id'],'label':resolved[slot['model_id']]['name'],'unavailable_reason':''}
               if isinstance(slot,dict) and isinstance(slot.get('model_id'),str) and slot['model_id'] in resolved else slot for slot in body['slots']]
        data=priority.validate({'enabled':body['enabled'],'slots':slots,'expected':body['expected']},{m['id'] for m in raw['models']+rows})
        priority.check_expected(body['expected'])
        paths=[registry.REGISTRY,Path(registry.HERE)/priority.CONFIG]
        before={p:p.read_bytes() if p.exists() else None for p in paths}
        try:
            if rows:registry.write_json(registry.REGISTRY,{**raw,'models':raw['models']+rows})
            registry.write_json(paths[1],data)
        except BaseException:
            for p,previous in before.items():
                if previous is None:p.unlink(missing_ok=True)
                else:
                    temp=p.with_name(p.name+'.priority-restore');temp.write_bytes(previous);temp.replace(p)
            raise
    return {'ok':True,**priority.snapshot(),'model_called':False,
            'saved_models':[{k:row.get(k) for k in ('id','name','model','default_agent','thinking_level','native_reasoning','native_selection')} for row in resolved.values()]}
