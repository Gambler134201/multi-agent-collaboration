"""Immutable trusted launcher context for the host-neutral main brain.

The main brain never infers its project, host, role, session responsibility or
owner from a tool argument, the process cwd, a service install directory or a
"recent project" list.  Those values come from the trusted launch/connection
configuration (the MCP entry's command-line flags, registered by the local
controller) and are frozen here.

Identity honesty is part of the contract:

* ``verified`` means the controller itself established the value.  Phase A has
  no native host handshake, so the MCP launcher cannot claim it; only internal
  code (or a future host adapter) may construct a verified identity.
* ``declared`` means the launcher supplied it; it is useful for audit but is not
  proof of a model/version/session.
* ``unknown`` means no trustworthy value exists.  A missing responsibility
  binding therefore refuses every mutation instead of guessing one.

Nothing in this module opens a socket, imports the panel, reads model keys or
starts a service.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import hmac
import json
import re
import secrets

import project_scope

SCHEMA = 1
ROLES = ('brain', 'executor')
SESSION_BINDINGS = ('host_verified', 'user_bound', 'unknown')
IDENTITY_STATES = ('verified', 'declared', 'unknown')

READ_OPERATIONS = frozenset({'describe', 'candidates', 'route_preview', 'status', 'delivery'})
MUTATING_OPERATIONS = frozenset({'dispatch', 'review', 'rework'})
OPERATIONS = READ_OPERATIONS | MUTATING_OPERATIONS

SAFE_ID = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_.:@+-]{0,127}$')
REQUEST_ID = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_-]{0,79}$')
OWNER_ID = re.compile(r'^[^\s/\\\x00-\x1f]{1,128}$')
MAX_TEXT = 300


class ContextError(ValueError):
    """A user-facing trusted-context/authority refusal."""


def _text(value, limit=MAX_TEXT):
    return str(value if value is not None else '').strip()[:limit]


def _now():
    return datetime.now(timezone.utc).isoformat()


def clean_request_id(value):
    """Validate a caller-supplied request id.  Never generate one silently."""
    if not isinstance(value, str) or not REQUEST_ID.fullmatch(value):
        raise ContextError('request_id 必须是 1-80 位字母、数字、下划线或短横线；不会自动生成新 ID')
    return value


def clean_task_id(value):
    if not isinstance(value, str) or not REQUEST_ID.fullmatch(value):
        raise ContextError('任务 ID 格式不正确')
    return value


@dataclass(frozen=True)
class Identity:
    """One public identity with an explicit evidence state."""

    value: str | None = None
    state: str = 'unknown'
    source: str = ''

    def __post_init__(self):
        if self.state not in IDENTITY_STATES:
            raise ContextError('身份状态只能是 verified / declared / unknown')
        if not isinstance(self.source, str) or len(self.source) > 80:
            raise ContextError('身份来源格式不正确')
        if self.state == 'unknown':
            if self.value not in (None, ''):
                raise ContextError('未知身份不能带一个自称可靠的值')
            object.__setattr__(self, 'value', None)
            return
        if not isinstance(self.value, str) or not self.value.strip():
            raise ContextError('已声明或已核实的身份需要有值')
        value = self.value.strip()
        if len(value) > MAX_TEXT or any(ch in value for ch in '\x00\r\n'):
            raise ContextError('身份值格式不正确')
        object.__setattr__(self, 'value', value)

    def public(self):
        return {'value': self.value, 'state': self.state, 'source': self.source}


def _identity(value, verified=False, source='launcher'):
    if isinstance(value, Identity):
        return value
    if value is None:
        return Identity(None, 'unknown', '')
    return Identity(str(value), 'verified' if verified else 'declared', source)


@dataclass(frozen=True, repr=False)
class BrainContext:
    """Frozen launch context shared by the MCP transport and the broker."""

    project: str
    host: str
    owner: str
    role: str
    host_version: Identity = field(default_factory=lambda: Identity(None, 'unknown', ''))
    model: Identity = field(default_factory=lambda: Identity(None, 'unknown', ''))
    native_session: Identity = field(default_factory=lambda: Identity(None, 'unknown', ''))
    operator_session: str | None = None
    session_binding: str = 'unknown'
    launcher: str = 'brain-mcp'
    declared_at: str = ''

    def __post_init__(self):
        try:
            project = project_scope.normalize_project(self.project)
        except (ValueError, OSError, TypeError) as error:
            raise ContextError('必须显式绑定一个已存在的本机项目绝对目录：' + str(error)) from None
        object.__setattr__(self, 'project', project)
        if not isinstance(self.host, str) or not SAFE_ID.fullmatch(self.host):
            raise ContextError('宿主标识格式不正确')
        if not isinstance(self.owner, str) or not OWNER_ID.fullmatch(self.owner):
            raise ContextError('主脑拥有者标识格式不正确')
        if self.role not in ROLES:
            raise ContextError('角色只能是由受信启动配置给出的 brain 或 executor')
        for name in ('host_version', 'model', 'native_session'):
            if not isinstance(getattr(self, name), Identity):
                raise ContextError(name + ' 身份结构不正确')
        if self.session_binding not in SESSION_BINDINGS:
            raise ContextError('会话责任归属状态不正确')
        if self.operator_session is not None:
            if not isinstance(self.operator_session, str) or not SAFE_ID.fullmatch(self.operator_session):
                raise ContextError('显式用户绑定的会话标识格式不正确')
        if self.session_binding == 'user_bound' and not self.operator_session:
            raise ContextError('user_bound 会话归属需要一个显式会话标识')
        if self.session_binding == 'host_verified' and self.native_session.state != 'verified':
            raise ContextError('没有宿主核实证据时不能声称 host_verified')
        if self.session_binding == 'unknown' and self.operator_session:
            raise ContextError('带有显式用户会话标识时不能登记为 unknown')
        if not isinstance(self.launcher, str) or not SAFE_ID.fullmatch(self.launcher):
            raise ContextError('启动入口标识格式不正确')
        if not isinstance(self.declared_at, str) or not self.declared_at:
            object.__setattr__(self, 'declared_at', _now())

    # -------------------------------------------------------------- public view
    @property
    def project_key(self):
        return project_scope.project_key(self.project)

    @property
    def session_reference(self):
        if self.operator_session:
            return self.operator_session
        if self.native_session.state == 'verified':
            return self.native_session.value
        return None

    @property
    def binding_key(self):
        """Responsibility identity, stable only for the same host/conversation."""
        parts = [self.project_key, self.host, self.owner, self.role,
                 self.session_binding, self.session_reference]
        return hashlib.sha256(json.dumps(parts, ensure_ascii=False).encode('utf-8')).hexdigest()

    @property
    def mutations_allowed(self):
        return self.role == 'brain' and self.session_binding != 'unknown'

    def public(self):
        return {
            'schema': SCHEMA,
            'project': self.project,
            'host': {'id': self.host, 'declared': True, 'version': self.host_version.public()},
            'role': self.role,
            'owner': {'id': self.owner, 'source': 'launcher', 'immutable': True,
                      'binding_key': self.binding_key},
            'model': self.model.public(),
            'session': {
                'binding': self.session_binding,
                'verified': self.session_binding == 'host_verified',
                'reference': self.session_reference,
                'operator_bound': bool(self.operator_session),
                'native': self.native_session.public(),
                'note': ('显式用户绑定的专用会话标识；不是从宿主原生会话核实'
                         if self.session_binding == 'user_bound' else
                         '已由宿主核实' if self.session_binding == 'host_verified'
                         else '缺少责任归属；变更操作会被拒绝'),
            },
            'launcher': self.launcher,
            'declared_at': self.declared_at,
            'mutations_allowed': self.mutations_allowed,
        }

    def declaration(self):
        """The JSON-safe launch declaration sent to the trusted controller.

        It deliberately carries no capability or controller credential.
        """
        return {
            'schema': SCHEMA,
            'project': self.project,
            'host': self.host,
            'owner': self.owner,
            'role': self.role,
            'host_version': self.host_version.public(),
            'model': self.model.public(),
            'native_session': self.native_session.public(),
            'operator_session': self.operator_session,
            'session_binding': self.session_binding,
            'launcher': self.launcher,
        }

    def __repr__(self):  # never includes a capability; there is nothing secret here
        return ('BrainContext(project=%r, host=%r, owner=%r, role=%r, session_binding=%r)'
                % (self.project, self.host, self.owner, self.role, self.session_binding))


def build_context(project, host, owner, role='brain', *, host_version=None, model=None,
                  native_session=None, native_session_verified=False,
                  operator_session=None, session_binding=None, launcher='brain-mcp',
                  declared_at=None):
    """Build one immutable context from trusted launch configuration only."""
    native = _identity(native_session, verified=bool(native_session_verified), source='host')
    if session_binding is None:
        if native.state == 'verified':
            session_binding = 'host_verified'
        elif operator_session:
            session_binding = 'user_bound'
        else:
            session_binding = 'unknown'
    return BrainContext(
        project=project, host=host, owner=owner, role=role,
        host_version=_identity(host_version, verified=False, source='launcher'),
        model=_identity(model, verified=False, source='launcher'),
        native_session=native,
        operator_session=operator_session or None,
        session_binding=session_binding,
        launcher=launcher,
        declared_at=_text(declared_at, 80) or _now(),
    )


_DECLARATION_KEYS = frozenset({'schema', 'project', 'host', 'owner', 'role', 'host_version',
                               'model', 'native_session', 'operator_session', 'session_binding',
                               'launcher'})
_IDENTITY_KEYS = frozenset({'value', 'state', 'source'})


def _declared_identity(value, name):
    if value is None:
        return Identity(None, 'unknown', '')
    if not isinstance(value, dict):
        raise ContextError(name + ' 身份声明格式不正确')
    unknown = set(value) - _IDENTITY_KEYS
    if unknown:
        raise ContextError(name + ' 身份声明含未知字段：' + '、'.join(sorted(unknown)))
    state = value.get('state', 'unknown')
    if state == 'verified':
        raise ContextError('控制器注册入口不能把声明值当作已核实身份：' + name)
    if state not in ('declared', 'unknown'):
        raise ContextError(name + ' 身份状态不正确')
    return Identity(value.get('value'), state, _text(value.get('source'), 80))


def from_declaration(declaration):
    """Validate a controller registration declaration.

    A remote declaration can only be ``declared`` or ``unknown``: claiming a
    verified model/version/native session would invent evidence this phase does
    not have.  The controller itself authorizes the connection, but it does not
    turn a launcher claim into host verification.
    """
    if not isinstance(declaration, dict):
        raise ContextError('连接声明需要是对象')
    unknown = set(declaration) - _DECLARATION_KEYS
    if unknown:
        raise ContextError('连接声明含未知字段：' + '、'.join(sorted(unknown)))
    if declaration.get('schema', SCHEMA) != SCHEMA:
        raise ContextError('连接声明版本不匹配')
    binding = declaration.get('session_binding', 'unknown')
    if binding == 'host_verified':
        raise ContextError('阶段 A 没有宿主原生会话核实入口，不能登记 host_verified；请使用显式用户绑定或 unknown')
    if binding not in ('user_bound', 'unknown'):
        raise ContextError('连接声明的会话归属状态不正确')
    operator = declaration.get('operator_session')
    if binding == 'user_bound' and not operator:
        raise ContextError('user_bound 连接声明需要 operator_session')
    if binding == 'unknown' and operator:
        raise ContextError('带 operator_session 的连接不能声明 unknown')
    return build_context(
        project=declaration.get('project'),
        host=declaration.get('host'),
        owner=declaration.get('owner'),
        role=declaration.get('role'),
        host_version=_declared_identity(declaration.get('host_version'), 'host_version'),
        model=_declared_identity(declaration.get('model'), 'model'),
        native_session=_declared_identity(declaration.get('native_session'), 'native_session'),
        native_session_verified=False,
        operator_session=operator,
        session_binding=binding,
        launcher=declaration.get('launcher') or 'brain-mcp',
    )


# ------------------------------------------------------------ operation authority

def require_operation(context, operation):
    """Validate the trusted role/session binding for one broker operation."""
    if not isinstance(context, BrainContext):
        raise ContextError('缺少受信任的连接上下文')
    if not isinstance(operation, str) or operation not in OPERATIONS:
        raise ContextError('未知的主脑操作；只允许固定的协作接口')
    if operation in MUTATING_OPERATIONS:
        if context.role != 'brain':
            raise ContextError('执行器角色的连接不能调用主脑变更工具')
        if context.session_binding == 'unknown':
            raise ContextError('缺少会话责任归属，拒绝变更操作；请在启动配置中显式绑定专用会话')
    return context


# ------------------------------------------------------- controller-side authority

# A plain string such as actor="main" cannot prove anything.  The broker issues a
# signed, process-local authority only for a validated brain connection; the
# delegation policy requires it for privileged changes to a brain-owned task.
_AUTHORITY_SECRET = secrets.token_bytes(32)


@dataclass(frozen=True, repr=False)
class Authority:
    owner: str
    project_key: str
    role: str
    session_binding: str
    binding_key: str
    signature: str = field(repr=False)

    def __repr__(self):
        return 'Authority(owner=%r, project_key=%r, role=%r)' % (
            self.owner, self.project_key, self.role)


def _sign(owner, project_key, role, session_binding, binding_key):
    payload = json.dumps([owner, project_key, role, session_binding, binding_key],
                         ensure_ascii=False).encode('utf-8')
    return hmac.new(_AUTHORITY_SECRET, payload, hashlib.sha256).hexdigest()


def issue_authority(context):
    """Issue a signed mutation authority for one validated brain connection."""
    if not isinstance(context, BrainContext):
        raise ContextError('缺少受信任的连接上下文')
    if context.role != 'brain':
        raise ContextError('执行器角色不能获得主脑变更授权')
    if context.session_binding == 'unknown':
        raise ContextError('缺少会话责任归属，不能签发变更授权')
    key = context.project_key
    return Authority(context.owner, key, context.role, context.session_binding, context.binding_key,
                     _sign(context.owner, key, context.role, context.session_binding, context.binding_key))


def verify_authority(authority):
    if (not isinstance(authority, Authority) or authority.role != 'brain'
            or authority.session_binding == 'unknown'):
        return False
    try:
        expected = _sign(authority.owner, authority.project_key, authority.role,
                         authority.session_binding, authority.binding_key)
        return hmac.compare_digest(expected, authority.signature)
    except (AttributeError, TypeError):
        return False


def authority_matches(authority, owner, project=None, binding_key=None):
    """True only for a genuine authority for this owner and (when known) project."""
    if not verify_authority(authority):
        return False
    if not isinstance(owner, str) or not owner or authority.owner != owner:
        return False
    if binding_key is not None and authority.binding_key != binding_key:
        return False
    if project:
        try:
            if project_scope.project_key(project) != authority.project_key:
                return False
        except (ValueError, OSError, TypeError):
            return False
    return True
