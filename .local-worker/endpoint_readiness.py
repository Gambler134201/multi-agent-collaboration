"""Pure, shared explanation of local readiness; never an execution authorization.

Adapters supply fixed reason codes at the failing boundary. No error-message
parsing, model calls, filesystem access, settings mutation or repair on reads.
"""
import model_scope


# label, observed fact, user-controlled next step. No exception text or credentials.
REASONS = {
    'runtime_ready': ('本地预检通过', '安装和本地执行通道通过预检；未发送模型请求。', '可按原设置派发；账户授权、模型服务和资源占用仍在任务开始时核对。'),
    'auto_connect_pending': ('可自动连接', '尚未连接；已有有效模型缓存且允许在任务开始前自动连接。', '派发时按已保存设置连接，不需要提前调用模型。'),
    'configuration_missing': ('缺少安装配置', '尚未提供所需的安装位置或原生配置目录。', '在模型与连接中核对安装设置；Pi/DSH 的安装位置由启动环境提供。'),
    'configuration_unreadable': ('连接配置需修正', '连接配置无法读取或格式不正确。', '核对本执行端的连接设置；不会重建配置或猜测账户。'),
    'installation_path_invalid': ('安装位置无效', '配置的安装目录不是有效的绝对目录。', '填写当前完整安装的绝对目录。'),
    'installation_incomplete': ('安装文件缺失', '所需的程序、运行组件或保护文件不完整。', '核对安装位置并修复缺失组件；不会切换其他执行器。'),
    'version_unsupported': ('版本尚未适配', '检测到的产品身份或版本未通过当前适配器校验。', '核对产品版本并适配后再使用；不自动修改安装包或降级。'),
    'installation_unavailable': ('安装信息无法核对', '未能完整读取安装文件或产品元数据。', '核对安装位置、完整性和读取权限。'),
    'executable_missing': ('DSH 程序未找到', '当前启动环境没有解析到有效的 DSH 可执行文件。', '配置 MODEL_DESK_DSH_EXE 或程序所在 PATH；随后由你重新加载工作台后台。'),
    'node_missing': ('Node 未找到', 'DSH 所需的 Node 程序无法找到。', '配置 MODEL_DESK_NODE_EXE 或 Node 所在 PATH；不会改用其他执行端。'),
    'dependency_missing': ('运行依赖缺失', '控制器所需的 acpx 运行依赖尚未安装。', '在本项目的 .local-worker/agent-runtime 中按安装指南补齐依赖。'),
    'access_config_unreadable': ('权限设置无法读取', '保存的访问权限无法核对。', '在模型与连接中核对访问模式；不会自动改成完全访问。'),
    'native_tools_unavailable': ('原生工具配置不完整', '本地原生工具组件或保护版本无法核对。', '核对工作台配套文件是否成组安装；不要通过换工具绕过。'),
    'native_channel_unavailable': ('原生通道未就绪', '未找到唯一的同配置目录 WorkBuddy 原生通道；尚未核实登录授权。', '打开同一配置目录的 WorkBuddy。首次通道未初始化时，可手动发一条普通消息后刷新；工作台不会代发。'),
    'desktop_closed': ('桌面尚未连接', 'ZCode 没有连通原生桥接，且当前未允许自动连接。', '在模型与连接中点击打开 / 连接；无需先发送模型消息。'),
    'bridge_missing': ('桌面未开启桥接', 'ZCode 已打开，但当前实例没有可用桥接。', '等待现有任务结束后，由你退出 ZCode，再从卡片打开 / 连接；不会自动退出桌面。'),
    'connection_unknown': ('原生通道待核对', '无法确认桥接或桌面实例状态。', '核对本机通道和进程读取权限后刷新；不把未知当作程序已退出。'),
    'catalog_stale': ('模型列表需更新', '原生模型缓存缺失或不匹配当前安装及连接。', '先连接正确的原生通道，再点击刷新内置模型；不会生成模型内容。'),
    'runtime_unknown': ('执行环境待核对', '当前适配器没有提供可确认的本地就绪证据。', '核对模型与连接配置；未知状态不当作就绪。'),
    'endpoint_config_unreadable': ('执行端设置不可读', '执行端开关或模型选择设置无法核对。', '检查执行端配置；不会自动重建或放宽设置。'),
    'endpoint_disabled': ('执行端已禁用', '此卡片当前不接收新任务；运行中任务不受影响。', '需要新任务时由你开启此卡片。'),
    'workflow_unreadable': ('协作设置不可读', '协作总开关及限制无法核对。', '检查协作设置，保留原权限和预算。'),
    'workflow_disabled': ('协作总开关已关闭', '自动协作当前关闭；这不是安装或模型故障。', '需要自动协作时由你开启总开关；明确的手动操作仍按其自身入口检查。'),
    'model_missing': ('已选模型不可用', '保存的当前模型已移除或改属其他执行端。', '在模型与连接中明确重新选择；不会自动替换模型。'),
    'model_disabled': ('已选模型已停用', '保存的当前模型处于停用状态。', '由你启用或重新选择模型。'),
    'no_models': ('没有已启用模型', '此执行端尚无可用的已启用模型配置。', '添加模型或明确其执行端归属；原生账户请刷新并保存模型选择。'),
    'scope_invalid': ('模型范围设置无效', '当前模型范围无法核对。', '在预算与权限中检查兼容范围设置。'),
    'scope_excluded': ('模型被范围排除', '此卡片的自动路由候选不在当前模型范围内。', '按需要明确调整范围或选择范围内模型；不会自动切换。'),
    'paid_not_allowed': ('缺少付费 / 原生额度授权', '候选均为非本地模型，但当前未授权该类调用。', '需要时由你授权；赠送积分也不会被视为自动授权。'),
    'native_limits_unsupported': ('原生用量无法满足硬上限', '原生账户没有足够的逐任务计量来执行已开启的金额或调用次数硬上限。', '由你决定使用可计量的执行端，或调整限制；不会擅自关闭上限。'),
}


class DiagnosticError(ValueError):
    """A precise boundary reason that remains compatible with existing refusals."""
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code if code in REASONS else 'runtime_unknown'


def reason(code, engine=None):
    code = code if isinstance(code,str) and code in REASONS else 'runtime_unknown'
    label, detail, next_step = REASONS[code]
    if code=='configuration_missing' and engine=='pi':
        next_step='设置 MODEL_DESK_PI_HOME 指向完整 Pi 安装根目录，再由你重新加载工作台后台；当前进程不会自动改用其他安装。'
    return {'code': code, 'label': label, 'detail': detail, 'next_step': next_step}


def runtime(code, *, issues=None):
    codes = issues if issues is not None else ([] if code in {'runtime_ready','auto_connect_pending'} else [code])
    return {'schema': 1, **reason(code), 'issues': [reason(c) for c in dict.fromkeys(codes)],
            'read_only': True, 'source': 'local_precheck'}


def diagnose(row, settings):
    """Explain saved controls + adapter facts; never replace routing/task gates."""
    codes=[]
    if row.get('readable') is not True: codes.append('endpoint_config_unreadable')
    elif row.get('enabled') is not True: codes.append('endpoint_disabled')
    if settings.get('readable') is False: codes.append('workflow_unreadable')
    elif settings.get('enabled') is False: codes.append('workflow_disabled')
    info=row.get('runtime') or {};proof=info.get('readiness')
    runtime_codes=[]
    if isinstance(proof,dict) and type(proof.get('schema')) is int and proof['schema']==1:
        items=proof.get('issues')
        if isinstance(items,list):
            runtime_codes=[c.get('code') for c in items if isinstance(c,dict) and isinstance(c.get('code'),str)
                           and c.get('code') not in {'runtime_ready','auto_connect_pending'}]
    if info.get('ready') is not True and not runtime_codes:runtime_codes=['runtime_unknown']
    codes.extend(runtime_codes)
    models=row.get('models') or [];current=row.get('current_model')
    selected=next((m for m in models if m.get('id')==current),None) if current else None
    if current and selected is None: codes.append('model_missing')
    elif selected is not None and not selected.get('enabled',True): codes.append('model_disabled')
    candidates=[m for m in ([selected] if current else models) if m and m.get('enabled',True)]
    if not candidates and not current: codes.append('no_models')
    try:
        scope=model_scope.normalize(settings.get('model_scope',model_scope.DEFAULT_SCOPE))
        in_scope=[m for m in candidates if model_scope.matches(scope,m.get('provider')!='local')]
        if candidates and not in_scope: codes.append('scope_excluded')
    except ValueError:
        codes.append('scope_invalid');in_scope=[]
    if in_scope and all(m.get('provider')!='local' for m in in_scope) and not settings.get('paid_allowed'):
        codes.append('paid_not_allowed')
    native=row.get('id')
    if native in {'workbuddy','zcode'} and in_scope:
        limited=(any(settings.get(k) is not False for k in ('enforce_paid_budget','enforce_model_call_limit'))
                 if native=='workbuddy' else any(settings.get(k) for k in ('enforce_paid_budget','enforce_model_call_limit')))
        if limited: codes.append('native_limits_unsupported')
    issues=[reason(c,row.get('id')) for c in dict.fromkeys(codes)]
    code=issues[0]['code'] if issues else 'auto_connect_pending' if info.get('auto_connect_pending') else 'runtime_ready'
    return {'schema':1, **reason(code,row.get('id')), 'issues':issues,'runtime_ready':info.get('ready') is True,
            'precheck_passed':not issues, 'read_only':True,'source':'local_precheck',
            'note':'这是当前自动路由的本地预检，不证明模型服务、账户授权、任务资源或实际执行已通过。'}
