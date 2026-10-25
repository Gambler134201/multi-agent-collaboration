/* Model picker view. */
(function (root) {
  'use strict';

  var MAX_VISIBLE_MODELS = 200;
  var ALL_SOURCES = 'all';
  var NATIVE_DEFAULT_REASONING = {
    value: 'default',
    label: '默认（原生）'
  };

  function asText(value) {
    return value === null || value === undefined ? '' : String(value);
  }

  function escapeHtml(value) {
    return asText(value).replace(/[&<>"']/g, function (character) {
      if (character === '&') {
        return '&amp;';
      }
      if (character === '<') {
        return '&lt;';
      }
      if (character === '>') {
        return '&gt;';
      }
      if (character === '"') {
        return '&quot;';
      }
      return '&#39;';
    });
  }

  function asArray(value) {
    return Array.isArray(value) ? value : [];
  }

  function sourceEntries(data) {
    return asArray(data && data.sources).filter(function (source) {
      return source && typeof source === 'object';
    });
  }

  function sourceIdOf(source) {
    return asText(source && source.id);
  }

  function sourceLabelOf(source) {
    return asText(source && source.label) || sourceIdOf(source) || '未命名来源';
  }

  function buildSourceLabels(data) {
    var labels = new Map();

    sourceEntries(data).forEach(function (source) {
      var id = sourceIdOf(source);

      if (!labels.has(id)) {
        labels.set(id, sourceLabelOf(source));
      }
    });

    return labels;
  }

  function sourceLabelForModel(model, sourceLabels) {
    var id = asText(model && model.source_id);

    if (sourceLabels.has(id)) {
      return sourceLabels.get(id);
    }

    return id || '其他来源';
  }

  function modelTitle(model) {
    return asText(model && model.name) || asText(model && model.model) || '未命名模型';
  }

  function findModel(models, id) {
    if (!id) {
      return null;
    }

    for (var index = 0; index < models.length; index += 1) {
      if (asText(models[index] && models[index].id) === id) {
        return models[index];
      }
    }

    return null;
  }

  function unavailableReasonFor(model) {
    var reason = asText(model && model.unavailable_reason);

    if (!asText(model && model.id)) {
      return '缺少模型标识，无法选择。';
    }

    if (reason) {
      return reason;
    }

    if (model && model.selectable === false) {
      return '该模型当前不可用。';
    }

    return '';
  }

  function isModelAvailable(model) {
    return !!model && unavailableReasonFor(model) === '';
  }

  function normalizeReasoning(rawReasoning) {
    var options = [];
    var seen = Object.create(null);

    asArray(rawReasoning).forEach(function (item) {
      var value;
      var label;

      if (item && typeof item === 'object') {
        value = asText(item.value);
        label = asText(item.label) || value;
      } else {
        value = asText(item);
        label = value;
      }

      if (!value || seen[value]) {
        return;
      }

      seen[value] = true;
      options.push({
        value: value,
        label: label || value
      });
    });

    return options;
  }

  function reasoningStateFor(model, requestedReasoning) {
    var options = normalizeReasoning(model && model.reasoning);
    var requested = asText(requestedReasoning);

    if (options.length === 0) {
      return {
        options: [NATIVE_DEFAULT_REASONING],
        effective: NATIVE_DEFAULT_REASONING.value,
        valid: true
      };
    }

    for (var index = 0; index < options.length; index += 1) {
      if (options[index].value === requested) {
        return {
          options: options,
          effective: requested,
          valid: true
        };
      }
    }

    return {
      options: options,
      effective: '',
      valid: false
    };
  }

  function reasoningLabelFor(reasoningState, value) {
    for (var index = 0; index < reasoningState.options.length; index += 1) {
      if (reasoningState.options[index].value === value) {
        return reasoningState.options[index].label;
      }
    }

    return value;
  }

  function filter(data, query, sourceId) {
    var models = asArray(data && data.models);
    var sourceLabels = buildSourceLabels(data);
    var search = asText(query).trim().toLowerCase();
    var selectedSource = asText(sourceId).trim() || ALL_SOURCES;

    return models.filter(function (model) {
      if (!model || typeof model !== 'object') {
        return false;
      }

      if (selectedSource !== ALL_SOURCES &&
          asText(model.source_id) !== selectedSource) {
        return false;
      }

      if (!search) {
        return true;
      }

      var haystack = [
        asText(model.name),
        asText(model.model),
        sourceLabelForModel(model, sourceLabels)
      ].join('\n').toLowerCase();

      return haystack.indexOf(search) !== -1;
    });
  }

  function buildGroups(data, models) {
    var groups = [];
    var groupsByKey = new Map();
    var sourceLabels = buildSourceLabels(data);

    function ensureGroup(sourceId) {
      var key = 'source:' + sourceId;

      if (groupsByKey.has(key)) {
        return groupsByKey.get(key);
      }

      var group = {
        sourceId: sourceId,
        label: sourceLabels.has(sourceId) ? sourceLabels.get(sourceId) : (sourceId || '其他来源'),
        models: []
      };

      groupsByKey.set(key, group);
      groups.push(group);
      return group;
    }

    sourceEntries(data).forEach(function (source) {
      ensureGroup(sourceIdOf(source));
    });

    models.forEach(function (model) {
      var sourceId = asText(model && model.source_id);
      ensureGroup(sourceId).models.push(model);
    });

    return groups.filter(function (group) {
      return group.models.length > 0;
    });
  }

  function renderSourceOptions(data, sourceFilter) {
    var options = [];
    var renderedIds = Object.create(null);
    var hasKnownSelection = sourceFilter === ALL_SOURCES;

    options.push(
      '<option value="' + ALL_SOURCES + '"' +
      (sourceFilter === ALL_SOURCES ? ' selected' : '') +
      '>全部来源</option>'
    );

    sourceEntries(data).forEach(function (source) {
      var id = sourceIdOf(source);

      if (!id || id === ALL_SOURCES || renderedIds[id]) {
        return;
      }

      renderedIds[id] = true;
      if (id === sourceFilter) {
        hasKnownSelection = true;
      }

      options.push(
        '<option value="' + escapeHtml(id) + '"' +
        (id === sourceFilter ? ' selected' : '') +
        '>' + escapeHtml(sourceLabelOf(source)) + '</option>'
      );
    });

    if (!hasKnownSelection) {
      options.push(
        '<option value="' + escapeHtml(sourceFilter) + '" selected>' +
        '未知来源（' + escapeHtml(sourceFilter) + '）</option>'
      );
    }

    return options.join('');
  }

  function priorityContextOf(data) {
    var context = data && data.priority_context;

    return context && typeof context === 'object' && !Array.isArray(context) ?
      context : null;
  }

  function priorityClients(context) {
    var clients = [];
    var seen = Object.create(null);

    asArray(context && context.clients).forEach(function (client) {
      var id;

      if (!client || typeof client !== 'object') {
        return;
      }

      id = asText(client.id);

      if (!id || seen[id]) {
        return;
      }

      seen[id] = true;
      clients.push({
        id: id,
        label: asText(client.label) || id
      });
    });

    return clients;
  }

  function clientIsKnown(clients, id) {
    for (var index = 0; index < clients.length; index += 1) {
      if (clients[index].id === id) {
        return true;
      }
    }

    return false;
  }

  function renderClientOptions(clients, selectedId) {
    var options = [
      '<option value=""' +
      (selectedId ? '' : ' selected') +
      '>请选择客户端</option>'
    ];
    var known = false;

    clients.forEach(function (client) {
      if (client.id === selectedId) {
        known = true;
      }

      options.push(
        '<option value="' + escapeHtml(client.id) + '"' +
        (client.id === selectedId ? ' selected' : '') +
        '>' + escapeHtml(client.label) + '</option>'
      );
    });

    if (selectedId && !known) {
      options.push(
        '<option value="' + escapeHtml(selectedId) + '" selected>' +
        '未知客户端（' + escapeHtml(selectedId) + '）</option>'
      );
    }

    return options.join('');
  }

  function renderModelChoice(model, context) {
    var id = asText(model && model.id);
    var available = isModelAvailable(model);
    var selected = !!id && id === context.selectedId;
    var current = !!id && id === context.currentId;
    var pending = selected && !current;
    var locked = context.locked;
    var disabled = locked || !available;
    var reason = unavailableReasonFor(model);
    var title = modelTitle(model);
    var classes = 'model-picker__choice';
    var buttonTitle;

    if (selected) {
      classes += ' model-picker__choice--selected';
    }
    if (current) {
      classes += ' model-picker__choice--current';
    }
    if (!available) {
      classes += ' model-picker__choice--unavailable';
    }

    if (locked) {
      buttonTitle = '请等待当前操作完成。';
    } else if (!available) {
      buttonTitle = reason;
    } else {
      buttonTitle = '选择 ' + title;
    }

    var html = [
      '<li class="model-picker__item">',
      '<button type="button" class="', classes, '"',
      ' data-picker-choice="', escapeHtml(id), '"',
      ' aria-pressed="', selected ? 'true' : 'false', '"',
      disabled ? ' disabled' : '',
      ' title="', escapeHtml(buttonTitle), '">',
      '<span class="model-picker__choice-main">',
      '<span class="model-picker__choice-name">', escapeHtml(title), '</span>'
    ];

    if (asText(model && model.model)) {
      html.push(
        '<span class="model-picker__choice-model">',
        escapeHtml(model.model),
        '</span>'
      );
    }

    html.push('</span><span class="model-picker__choice-badges">');

    if (current) {
      html.push(
        '<span class="model-picker__badge model-picker__badge--current">',
        '<span aria-hidden="true">✓</span> 当前',
        '</span>'
      );
    }

    if (pending) {
      html.push(
        '<span class="model-picker__badge model-picker__badge--pending">',
        '待应用',
        '</span>'
      );
    }

    if (!available) {
      html.push(
        '<span class="model-picker__badge model-picker__badge--unavailable">',
        '不可用',
        '</span>'
      );
    }

    html.push('</span>');

    if (!available) {
      html.push(
        '<span class="model-picker__choice-reason">',
        escapeHtml(reason),
        '</span>'
      );
    }

    html.push('</button></li>');
    return html.join('');
  }

  function renderGroup(group, context) {
    var html = [
      '<section class="model-picker__group">',
      '<h3 class="model-picker__group-title">', escapeHtml(group.label), '</h3>',
      '<ul class="model-picker__choices">'
    ];

    group.models.forEach(function (model) {
      html.push(renderModelChoice(model, context));
    });

    html.push('</ul></section>');
    return html.join('');
  }

  function renderReasoning(model, reasoningState, locked) {
    var html = [
      '<section class="model-picker__reasoning">',
      '<label class="model-picker__reasoning-label">',
      '<span>思考强度</span>',
      '<select data-picker-reasoning'
    ];

    var unavailable = !!model && !isModelAvailable(model);
    var disabled = locked || !model || unavailable;

    if (disabled) {
      html.push(' disabled');
    }

    html.push('>');

    if (!model) {
      html.push(
        '<option value="" selected disabled>请先选择模型</option>'
      );
    } else if (!reasoningState.valid) {
      html.push(
        '<option value="" selected disabled>请选择思考强度</option>'
      );
    }

    if (model) {
      reasoningState.options.forEach(function (option) {
        var selected = reasoningState.valid &&
          option.value === reasoningState.effective;

        html.push(
          '<option value="', escapeHtml(option.value), '"',
          selected ? ' selected' : '',
          '>', escapeHtml(option.label), '</option>'
        );
      });
    }

    html.push('</select></label>');

    if (model && asText(model.current_reasoning)) {
      html.push(
        '<p class="model-picker__current-reasoning">',
        '已保存：',
        escapeHtml(reasoningLabelFor(
          reasoningState,
          asText(model.current_reasoning)
        )),
        '</p>'
      );
    }

    html.push('</section>');
    return html.join('');
  }

  function render(data, state) {
    data = data && typeof data === 'object' ? data : {};
    state = state && typeof state === 'object' ? state : {};

    var models = asArray(data.models);
    var rawQuery = asText(state.query);
    var sourceFilter = asText(state.source_id).trim() || ALL_SOURCES;
    var selectedId = asText(state.selected_id);
    var currentId = asText(data.current_model);
    var error = asText(state.error);
    var warning = asText(data.warning);
    var note = asText(data.note);
    var loading = !!state.loading;
    var busy = !!state.busy;
    var locked = loading || busy;
    var selectedModel = findModel(models, selectedId);
    var currentModel = findModel(models, currentId);
    var reasoningState = reasoningStateFor(selectedModel, state.reasoning);
    var filtered = filter(data, rawQuery, sourceFilter);
    var matchedCount = filtered.length;
    var visibleModels = filtered.slice(0, MAX_VISIBLE_MODELS);
    var groups = buildGroups(data, visibleModels);
    var label = asText(data.label);
    var endpoint = asText(data.endpoint);
    var priorityContext = priorityContextOf(data);
    var priorityMode = !!priorityContext;
    var priorityClientList = priorityMode ? priorityClients(priorityContext) : [];
    var prioritySlot = priorityMode ?
      (asText(priorityContext.slot).trim() || '未知') : '';
    var priorityClientId = priorityMode ?
      (asText(state.client_id).trim() || endpoint) : '';
    var priorityClientKnown = priorityMode ?
      clientIsKnown(priorityClientList, priorityClientId) : false;
    var titleText = priorityMode ?
      ('选择第 ' + prioritySlot + ' 顺位的客户端与模型') : '选择模型';
    var subtitle = [];
    var currentSummary;

    if (label) {
      subtitle.push(label);
    }
    if (endpoint) {
      subtitle.push(endpoint);
    }

    if (!currentId) {
      currentSummary = '未设置';
    } else if (currentModel) {
      currentSummary = modelTitle(currentModel) +
        (isModelAvailable(currentModel) ? '' : '（不可用）');
    } else {
      currentSummary = '已保存模型不在当前列表';
    }

    var selectedSummary = '未选择';
    if (selectedId) {
      selectedSummary = selectedModel ?
        modelTitle(selectedModel) +
          (isModelAvailable(selectedModel) ? '' : '（不可用）') :
        '所选项不在当前列表';
    }

    var pending = !!selectedId && selectedId !== currentId;
    var applyIssue = '';

    if (busy) {
      applyIssue = '正在应用模型，请稍候。';
    } else if (loading) {
      applyIssue = '模型列表正在加载，请稍候。';
    } else if (error) {
      applyIssue = '加载模型失败，请刷新后重试。';
    } else if (priorityMode && !priorityClientId) {
      applyIssue = '请先选择执行客户端';
    } else if (priorityMode && !priorityClientKnown) {
      applyIssue = '所选客户端不在当前列表中，请重新选择。';
    } else if (!selectedId) {
      applyIssue = '请选择一个可用模型。';
    } else if (!selectedModel) {
      applyIssue = '所选项不在当前列表中，请重新选择。';
    } else if (!isModelAvailable(selectedModel)) {
      applyIssue = '所选模型不可用：' +
        unavailableReasonFor(selectedModel);
    } else if (!reasoningState.valid) {
      applyIssue = '请选择该模型已声明的思考强度。';
    }

    var applySummary = '';
    if (!applyIssue) {
      applySummary = '将应用 ' + modelTitle(selectedModel) +
        '，思考强度：' +
        reasoningLabelFor(reasoningState, reasoningState.effective) + '。';
    }

    var applyHint = applyIssue || applySummary;
    var applyLabel = busy ? '正在应用…' :
      (loading ? '正在加载…' : (priorityMode ? '用于此顺位' : '应用模型'));
    var applyActionName = priorityMode ? '用于此顺位：' : '应用模型：';
    var applyDescription = applyActionName + (applyIssue || applySummary);

    var html = [
      '<div class="model-picker__root" data-picker-root',
      ' aria-busy="', locked ? 'true' : 'false', '">',
      '<header class="model-picker__header">',
      '<div class="model-picker__heading">',
      '<h2 class="model-picker__title">', escapeHtml(titleText), '</h2>'
    ];

    if (subtitle.length) {
      html.push(
        '<p class="model-picker__subtitle">',
        escapeHtml(subtitle.join(' · ')),
        '</p>'
      );
    }

    html.push(
      '</div>',
      '<button type="button" class="model-picker__close"',
      ' data-picker-close aria-label="关闭模型选择"',
      ' title="关闭模型选择">&times;</button>',
      '</header>'
    );

    if (priorityMode) {
      html.push(
        '<section class="model-picker__controls model-picker__controls--client"',
        ' aria-label="执行客户端">',
        '<label class="model-picker__field model-picker__field--client">',
        '<span class="model-picker__field-label">执行客户端</span>',
        '<select data-picker-client aria-label="执行客户端"',
        locked ? ' disabled' : '',
        '>',
        renderClientOptions(priorityClientList, priorityClientId),
        '</select>',
        '</label>',
        '</section>'
      );
    }

    html.push(
      '<section class="model-picker__controls" aria-label="搜索与来源筛选">',
      '<label class="model-picker__field">',
      '<span class="model-picker__field-label">搜索模型</span>',
      '<input type="search" data-picker-query',
      ' value="', escapeHtml(rawQuery), '"',
      ' placeholder="搜索模型名、模型 ID 或来源"',
      ' aria-label="搜索模型、模型 ID 或来源"',
      ' autocomplete="off" spellcheck="false"',
      locked ? ' disabled' : '',
      '>',
      '</label>',
      '<label class="model-picker__field model-picker__field--source">',
      '<span class="model-picker__field-label">来源</span>',
      '<select data-picker-source',
      locked ? ' disabled' : '',
      '>',
      renderSourceOptions(data, sourceFilter),
      '</select>',
      '</label>',
      '</section>'
    );

    if (error) {
      html.push(
        '<div class="model-picker__notice model-picker__notice--error"',
        ' role="alert">加载模型失败：',
        escapeHtml(error),
        '</div>'
      );
    }

    if (loading) {
      html.push(
        '<div class="model-picker__notice model-picker__notice--loading"',
        ' role="status">正在刷新模型…</div>'
      );
    }

    if (busy) {
      html.push(
        '<div class="model-picker__notice model-picker__notice--busy"',
        ' role="status">正在应用模型，请稍候…</div>'
      );
    }

    if (warning) {
      html.push(
        '<div class="model-picker__notice model-picker__notice--warning"',
        ' role="status">注意：',
        escapeHtml(warning),
        '</div>'
      );
    }

    if (note) {
      html.push(
        '<p class="model-picker__notice model-picker__notice--note">',
        escapeHtml(note),
        '</p>'
      );
    }

    html.push(
      '<div class="model-picker__summary">',
      '<p class="model-picker__current">',
      '<span class="model-picker__summary-label">当前：</span>',
      escapeHtml(currentSummary),
      '</p>',
      '<p class="model-picker__selection">',
      '<span class="model-picker__summary-label">',
      pending ? '待应用：' : '预览：',
      '</span>',
      escapeHtml(selectedSummary),
      '</p>',
      '<p class="model-picker__count" role="status" aria-live="polite">',
      '匹配 ', String(matchedCount), ' 个模型',
      '</p>',
      '</div>'
    );

    if (pending) {
      html.push(
        '<p class="model-picker__pending" role="status">',
        '待应用：',
        escapeHtml(selectedSummary),
        '（尚未保存）',
        '</p>'
      );
    }

    if (matchedCount > MAX_VISIBLE_MODELS) {
      html.push(
        '<p class="model-picker__truncated" role="status">',
        '共 ', String(matchedCount), ' 个匹配模型，仅显示前 ',
        String(MAX_VISIBLE_MODELS),
        ' 个。请缩小搜索范围以查看其余模型。',
        '</p>'
      );
    }

    html.push(
      '<div class="model-picker__list" role="region" aria-label="模型列表">'
    );

    if (groups.length === 0) {
      var emptyMessage;

      if (loading && models.length === 0) {
        emptyMessage = '正在加载模型…';
      } else if (error && models.length === 0) {
        emptyMessage = '加载失败，暂无模型可显示。';
      } else if (models.length === 0) {
        emptyMessage = '暂无可选模型。请刷新模型或管理连接。';
      } else {
        emptyMessage = '没有匹配的模型。请调整搜索关键词或来源筛选。';
      }

      html.push(
        '<p class="model-picker__empty" role="status">',
        emptyMessage,
        '</p>'
      );
    } else {
      groups.forEach(function (group) {
        html.push(renderGroup(group, {
          selectedId: selectedId,
          currentId: currentId,
          locked: locked
        }));
      });
    }

    html.push(
      '</div>',
      renderReasoning(selectedModel, reasoningState, locked)
    );

    html.push(
      '<div class="model-picker__apply-row">',
      '<button type="button" class="model-picker__apply"',
      ' data-picker-apply',
      applyIssue ? ' disabled' : '',
      ' aria-label="', escapeHtml(applyDescription), '"',
      ' title="', escapeHtml(applyHint), '">',
      escapeHtml(applyLabel),
      '</button>',
      '<p class="model-picker__apply-hint">',
      escapeHtml(applyHint),
      '</p>',
      '<p class="model-picker__apply-note">',
      '只可应用可用模型；思考强度必须是该模型已声明的值。',
      '</p>',
      '</div>',
      '<footer class="model-picker__footer">',
      '<div class="model-picker__footer-actions">',
      '<button type="button" class="model-picker__refresh"',
      ' data-picker-refresh',
      locked ? ' disabled' : '',
      '>刷新模型</button>',
      '<button type="button" class="model-picker__manage"',
      ' data-picker-manage',
      locked ? ' disabled' : '',
      '>管理连接</button>',
      '</div>'
    );

    if (priorityMode) {
      html.push(
        '<p class="model-picker__footnote model-picker__footnote--priority">',
        '选择仅加入排序草稿；点击排序栏的保存后生效，不修改执行端当前模型。',
        '</p>'
      );
    } else {
      html.push(
        '<label class="model-picker__pin">',
        '<input type="checkbox" data-picker-pin',
        state.pin === false ? '' : ' checked',
        locked ? ' disabled' : '',
        '>',
        '<span>同时用于此执行端后续自动任务</span>',
        '</label>',
        '<p class="model-picker__footnote">',
        '应用只影响新任务。勾选“同时用于此执行端后续自动任务”会更新',
        '该执行端候选，不修改运行中/已排队任务。',
        '</p>'
      );
    }

    html.push(
      '</footer>',
      '</div>'
    );

    return html.join('');
  }

  root.ModelDeskModelPickerView = {
    render: render,
    filter: filter
  };
}(typeof globalThis !== 'undefined' ? globalThis : this));
