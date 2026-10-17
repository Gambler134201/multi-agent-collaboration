"""Global execution-model scope: which model class may execute a task.

The scope is one global enum stored beside the workflow switches.  It reuses the
registry's real ``provider`` classification instead of guessing from URLs, keys
or prices:

* ``provider == 'local'`` (the declared local/self-hosted class, including a
  remotely connected home service) is a **local** candidate;
* every other registered provider is a **paid** candidate under the same
  ``model_router.is_paid`` semantics the panel already exposes.

The main-brain role of GPT/Codex is not an execution model and is never filtered
by this scope.  This module owns the enum, labels and the pure classification
helpers; the authoritative paid predicate stays in ``model_router`` so there is
exactly one definition of "paid".

Pure module: no I/O, no settings reads, importable from the settings validator,
the router, the queue and the panel.  It deliberately does not import
``model_router`` (the router imports this module), so callers pass the already
computed ``paid`` flag or their own predicate.
"""

ALL = 'all'
PAID_ONLY = 'paid_only'
LOCAL_ONLY = 'local_only'

SCOPES = (ALL, PAID_ONLY, LOCAL_ONLY)

DEFAULT_SCOPE = ALL

LABELS = {
    ALL: '全部模型（兼容默认）',
    PAID_ONLY: '仅付费模型',
    LOCAL_ONLY: '仅本地/自建模型',
}

NOTES = {
    ALL: '全部已启用的执行模型都可被路由或手动指定；这是不选择新范围时的原有行为。',
    PAID_ONLY: '只允许注册为付费（非 local provider）的执行模型；不会自动开启付费授权，'
               '金额上限、余额和请求次数限制仍然生效。',
    LOCAL_ONLY: '只允许注册为本地/自建（provider 为 local，含远程连接的家中的自建服务）的执行模型；'
                '不会调用任何付费候选。',
}


def normalize(value):
    """Lowercase/trim a caller-supplied enum. Raises ValueError on a bad shape.

    Callers supply DEFAULT_SCOPE when the field is absent in a legacy file.
    Anything present, including null, must pass this strict gate. ``effective``
    is a display convenience and must not authorize execution.
    """
    if value is None:
        raise ValueError('执行模型范围不能为空；可选值：' + '、'.join(SCOPES))
    if not isinstance(value, str):
        raise ValueError('执行模型范围需要是字符串；可选值：' + '、'.join(SCOPES))
    text = value.strip().lower()
    if text not in SCOPES:
        raise ValueError('执行模型范围不是有效值：' + str(value)[:40] + '；可选值：' + '、'.join(SCOPES))
    return text


def effective(value):
    """Normalize a stored value, mapping absent/unknown to the legacy default."""
    if value is None:
        return DEFAULT_SCOPE
    if not isinstance(value, str):
        return DEFAULT_SCOPE
    text = value.strip().lower()
    return text if text in SCOPES else DEFAULT_SCOPE


def label(value):
    return LABELS[effective(value)]


def note(value):
    return NOTES[effective(value)]


def describe(value):
    """Public, no-secret summary of the active scope for API/UI payloads."""
    scope = effective(value)
    return {'value': scope, 'label': LABELS[scope], 'note': NOTES[scope],
            'is_default': scope == ALL}


def is_local(paid):
    return not bool(paid)


def candidate_kind(paid):
    """User-facing classification of one registered model."""
    return 'local' if is_local(paid) else 'paid'


def candidate_label(paid):
    return '本地/自建模型' if is_local(paid) else '付费模型'


def reason(scope, paid):
    """Why one candidate is outside the effective scope (never a substitution)."""
    active = effective(scope)
    return ('执行模型范围为“%s”，已排除%s；不会替换模型或回退到另一类'
            % (LABELS[active], candidate_label(paid)))


def matches(value, paid):
    """True when one candidate class is inside the effective scope.

    ``paid`` must come from the registry's real paid predicate
    (``model_router.is_paid``), never from inferring a provider by URL or price.
    """
    scope = effective(value)
    if scope == PAID_ONLY:
        return bool(paid)
    if scope == LOCAL_ONLY:
        return is_local(paid)
    return True


def fits(value, paid):
    """Alias with explicit wording for queue/worker guards."""
    return matches(value, paid)


def check(value, paid):
    """Single-candidate gate.

    Returns the active scope when the candidate is allowed, otherwise raises
    ValueError with the concrete classification and scope reason.  Callers must
    refuse the run; they must never substitute another model or fall back to the
    other class.
    """
    # Legacy callers supply DEFAULT_SCOPE when the field is absent.  An explicit
    # null in stored settings is corruption, not permission to widen the scope.
    scope = normalize(value)
    if matches(scope, paid):
        return scope
    kind = candidate_label(paid)
    raise ValueError('执行模型范围“%s”不允许%s；当前模型属于%s，不会被替换或回退，'
                     '请改用范围内的模型或调整执行模型范围'
                     % (LABELS[scope], kind, kind))
