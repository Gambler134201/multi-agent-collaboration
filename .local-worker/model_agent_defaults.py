"""Per-execution-model default agent preference.

The preference is stored on one model row as ``default_agent``:

* ``follow_global`` - inherit the current global default (legacy rows too)
* ``dsh``           - the model saves DeepSeek Harness as its default
* ``pi``            - the model saves Pi Agent as its default

Only a new task resolves this preference; an existing task keeps the engine
snapshot in its own ``task.json``.  The module deliberately has no import-time
dependency on the registry or the engine declarations, so it can be used by the
router and by the atomic panel patch without a circular import.
"""
from copy import deepcopy

FIELD = 'default_agent'
FOLLOW_GLOBAL = 'follow_global'
AGENT_VALUES = ('dsh', 'pi', 'workbuddy', 'zcode')
VALUES = (FOLLOW_GLOBAL, *AGENT_VALUES)
LABELS = {FOLLOW_GLOBAL: '跟随全局默认', 'dsh': 'DeepSeek Harness', 'pi': 'Pi Agent', 'workbuddy': 'WorkBuddy', 'zcode': 'ZCode'}
SOURCE_LABELS = {'explicit': '本次显式指定', 'model': '模型指定', 'global': '跟随全局默认'}
INVALID_MESSAGE = ('模型默认执行 Agent 无效；请选择“跟随全局默认 / DeepSeek Harness / Pi Agent”，'
                   '不会自动改用其他 Agent')


def normalize(value=None):
    """Normalize a stored or submitted value.

    A missing/blank legacy value means ``follow_global``; every other value must
    be one of the declared choices.  An invalid value is refused rather than
    silently treated as inheritance.
    """
    if value is None:
        return FOLLOW_GLOBAL
    if not isinstance(value, str):
        raise ValueError(INVALID_MESSAGE)
    text = value.strip().lower()
    if not text:
        return FOLLOW_GLOBAL
    if text not in VALUES:
        raise ValueError(INVALID_MESSAGE)
    return text


def normalize_update(value):
    """Strict normalization for the preference-only atomic patch."""
    if not isinstance(value, str) or value.strip().lower() not in VALUES:
        raise ValueError(INVALID_MESSAGE)
    return value.strip().lower()


def stored(model):
    """The saved field, with legacy/missing rows inheriting the global default."""
    if not isinstance(model, dict):
        raise ValueError(INVALID_MESSAGE)
    return normalize(model.get(FIELD))


def effective(model, settings):
    """Resolve ``(agent, source)`` for a model without executing anything.

    Precedence inside this helper is model preference > global default.  The
    caller handles an explicit task override before calling it, which produces
    the full ``explicit > model > global`` order.
    """
    import execution_engines
    value = stored(model)
    if value == FOLLOW_GLOBAL:
        agent = execution_engines.selected(settings)
        execution_engines.validate_model(agent, model)
        return agent, 'global'
    execution_engines.validate_model(value, model)
    return value, 'model'


def describe(model, settings):
    """Public explanation used by model cards and task hints."""
    result = deepcopy(model) if isinstance(model, dict) else {}
    result[FIELD] = result.get(FIELD, FOLLOW_GLOBAL)
    try:
        value = stored(model)
        agent, source = effective(model, settings)
        return {
            'field': FIELD,
            'value': value,
            'label': LABELS[value],
            'source': source,
            'source_label': SOURCE_LABELS[source],
            'effective_agent': agent,
            'effective_label': LABELS.get(agent, agent),
            'valid': True,
            'error': '',
        }
    except ValueError as error:
        return {
            'field': FIELD,
            'value': result.get(FIELD),
            'label': '配置无效',
            'source': 'invalid',
            'source_label': '配置无效',
            'effective_agent': None,
            'effective_label': '配置无效',
            'valid': False,
            'error': str(error),
        }


def _raw_registry():
    """Read the registry under the shared lock without persisting row defaults."""
    import json
    import model_registry as registry
    if not registry.REGISTRY.exists():
        return registry.read_registry()
    return json.loads(registry.REGISTRY.read_text(encoding='utf-8'))


def update(model_id, value):
    """Atomically update only the default-agent field of one model.

    Connection fields, keys, thinking/preset settings, prices, scope/budget and
    the global default are untouched.  No task is re-routed or migrated.
    """
    import model_registry as registry
    normalized = normalize_update(value)
    with registry.registry_lock():
        data = _raw_registry()
        row = next((item for item in data.get('models', [])
                    if isinstance(item, dict) and item.get('id') == model_id), None)
        if row is None:
            raise ValueError('模型不存在')
        import execution_engines
        if normalized == FOLLOW_GLOBAL and row.get('provider') in ('workbuddy', 'zcode'):
            raise ValueError('WorkBuddy 原生模型必须固定使用 WorkBuddy 执行端')
        execution_engines.validate_model(normalized, row)
        row[FIELD] = normalized
        registry.write_json(registry.REGISTRY, data)
    return registry.public_model(row)


def public_rows(models, settings):
    """Attach ``agent_default`` information to public model rows for the panel."""
    rows = []
    for model in models:
        row = deepcopy(model)
        row['agent_default'] = describe(model, settings)
        rows.append(row)
    return rows
