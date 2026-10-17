"""本机面板的“项目范围视图”纯工具：规范化视图、按真实目录身份筛选记录。

边界（重要）：
- 只服务本机面板视图选择，不是权限沙箱；不读任何配置、不碰历史/密钥/预算/开关。
- 没有任何全局“当前项目”变量，也没有缓存：每次调用只依赖入参，返回新对象。
- 归属只认“resolve 后的真实目录身份完全相等”：不用字符串前缀，不把 A2、A\\sub 并进 A。
- 拒绝一律 ValueError；不创建目录、不默认 cwd、解码失败不回退 all。
"""
import base64
import binascii
import json
import os
import re

__all__ = ['MODE_PROJECT', 'MODE_ALL', 'MODE_UNBOUND', 'VIEW_MODES', 'VIEW_KEYS', 'MAX_HEADER_CHARS',
           'normalize_project', 'project_key', 'parse_view', 'encode_view_header', 'decode_view_header',
           'belongs_to_project', 'filter_records']

MODE_PROJECT = 'project'
MODE_ALL = 'all'
MODE_UNBOUND = 'unbound'
VIEW_MODES = (MODE_PROJECT, MODE_ALL, MODE_UNBOUND)
VIEW_KEYS = frozenset(('mode', 'project'))
MAX_HEADER_CHARS = 8192

_URL_LIKE = re.compile(r'^[A-Za-z][A-Za-z0-9+.\-]*://')


def _reject(message, value):
    raise ValueError(f'{message}（值：{value!r}）')


def normalize_project(value):
    """把“存在的本机绝对目录”规范化为 resolve 后的展示路径字符串。

    拒绝（ValueError）：非字符串、空串、含 NUL、HTTP/其它 URL、相对或驱动器相对路径、
    不存在、不是目录。绝不创建目录，也绝不以 cwd 补全。
    """
    if not isinstance(value, str):
        _reject('项目路径必须是字符串', value)
    if not value:
        _reject('项目路径不能为空', value)
    if '\x00' in value:
        _reject('项目路径不能包含 NUL', value)
    if '://' in value or _URL_LIKE.match(value):
        _reject('项目路径必须是本机目录，不能是 URL', value)
    if not os.path.isabs(value):
        _reject('项目路径必须是绝对路径', value)
    if os.name == 'nt' and not os.path.splitdrive(value)[0]:
        _reject('项目路径必须包含明确盘符或 UNC 根，不能沿用当前驱动器', value)
    if not os.path.exists(value):
        _reject('项目目录不存在', value)
    if not os.path.isdir(value):
        _reject('项目路径不是目录', value)
    resolved = os.path.realpath(value)
    if not os.path.isdir(resolved):
        _reject('项目目录解析后不是目录', value)
    return resolved


def project_key(value):
    """返回用于身份比较的规范 key（大小写按 OS、分隔符/尾分隔符/别名已统一）。

    key 只用于“完全相等”比较，永远不能当作前缀来判断归属。
    """
    return os.path.normcase(normalize_project(value))


def parse_view(value):
    """解析面板视图 dict，返回规范 dict {'mode': ..., 'project': ...}。

    None -> {'mode': 'unbound', 'project': None}；project 视图的 project 必须是有效目录，
    并规范化为展示路径；all/unbound 不能带非空 project；未知字段/类型/模式拒绝。
    返回值是全新的 dict，与入参不共享对象，也不缓存。
    """
    if value is None:
        return {'mode': MODE_UNBOUND, 'project': None}
    if not isinstance(value, dict):
        _reject('视图必须是 dict', value)
    unknown = sorted(key for key in value if key not in VIEW_KEYS)
    if unknown:
        raise ValueError(f'视图含未知字段：{unknown}')
    mode = value.get('mode')
    if not isinstance(mode, str) or mode not in VIEW_MODES:
        _reject('视图 mode 必须是 project/all/unbound', mode)
    project = value.get('project')
    if project == '':
        project = None
    if mode != MODE_PROJECT:
        if project is not None:
            _reject(f'{mode} 视图不能带 project', project)
        return {'mode': mode, 'project': None}
    if project is None:
        _reject('project 视图必须带项目目录', project)
    return {'mode': MODE_PROJECT, 'project': normalize_project(project)}


def encode_view_header(view):
    """把视图（None 视作 unbound）编码成 UTF-8 JSON 的 URL-safe base64 头字符串。"""
    payload = json.dumps(parse_view(view), ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    header = base64.urlsafe_b64encode(payload.encode('utf-8')).decode('ascii')
    if len(header) > MAX_HEADER_CHARS:
        raise ValueError(f'视图头超过 {MAX_HEADER_CHARS} 字符（{len(header)}）')
    return header


def decode_view_header(value):
    """解码视图头；None 明确代表内部 legacy“无 scope 头”并原样返回 None。

    其余非法输入（非字符串、空串、超长、非 base64url、非 UTF-8、非 JSON、非 dict、
    视图本身不合法）一律 ValueError，绝不回退成 all。
    """
    if value is None:
        return None
    if not isinstance(value, str):
        _reject('视图头必须是字符串', value)
    if not value:
        _reject('视图头不能为空', value)
    if len(value) > MAX_HEADER_CHARS:
        raise ValueError(f'视图头超过 {MAX_HEADER_CHARS} 字符（{len(value)}）')
    try:
        raw = base64.b64decode(value, altchars=b'-_', validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError(f'视图头不是合法 base64url：{exc}') from exc
    try:
        text = raw.decode('utf-8')
    except UnicodeDecodeError as exc:
        raise ValueError(f'视图头不是 UTF-8：{exc}') from exc
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f'视图头不是 JSON：{exc}') from exc
    if not isinstance(data, dict):
        _reject('视图头 JSON 必须是对象', data)
    return parse_view(data)


def _view_key(view):
    """视图侧的规范 key；视图项目已不可解析时返回 None（不猜测归属）。"""
    project = view.get('project')
    if not isinstance(project, str) or not project:
        return None
    try:
        return project_key(project)
    except ValueError:
        return None


def _belongs(record, mode, key):
    if not isinstance(record, dict):
        return False
    if mode == MODE_UNBOUND:
        return False
    if mode == MODE_ALL:
        return True
    if key is None:
        return False
    record_project = record.get('project')
    if not isinstance(record_project, str) or not record_project.strip():
        return False
    try:
        return project_key(record_project) == key
    except ValueError:
        return False


def belongs_to_project(record, view):
    """记录是否属于视图范围；损坏/缺失 project 的记录不会自动归入任何项目。

    all：包含所有 dict 记录（含无归属记录）；unbound：恒 False；
    project：只按规范真实目录身份完全相等。
    视图本身必须始终合法（项目目录仍存在）：过期视图抛 ValueError，不静默变成“无匹配”。
    """
    parsed = parse_view(view)
    return _belongs(record, parsed['mode'], _view_key(parsed))


def filter_records(records, view):
    """按视图筛选记录，返回新 list；原始 dict 原样引用、不复制也不修改。"""
    if isinstance(records, (str, bytes, bytearray)) or not hasattr(records, '__iter__'):
        _reject('records 必须是记录的可迭代集合', records)
    parsed = parse_view(view)
    mode = parsed['mode']
    key = _view_key(parsed)
    return [record for record in records if _belongs(record, mode, key)]
