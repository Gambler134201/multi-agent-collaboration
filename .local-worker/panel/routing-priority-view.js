/*
 * ModelDeskPriorityView — pure HTML-string renderer for the "模型使用顺序" bar.
 * No DOM, network, storage, event or timer access. Every dynamic string is escaped.
 */
(function (root) {
  'use strict';

  var MAX_SLOTS = 20;

  var ENTITIES = {
    '&': '&amp;',
    '<': '&lt;',
    '>': '&gt;',
    '"': '&quot;',
    "'": '&#39;'
  };

  function esc(value) {
    if (value === null || value === undefined) return '';
    return String(value).replace(/[&<>"']/g, function (ch) {
      return ENTITIES[ch];
    });
  }

  function str(value) {
    return value === null || value === undefined ? '' : String(value);
  }

  function asArray(value) {
    return Array.isArray(value) ? value : [];
  }

  function isObj(value) {
    return !!value && typeof value === 'object';
  }

  function hasOwn(obj, key) {
    return !!obj && Object.prototype.hasOwnProperty.call(obj, key);
  }

  /*
   * Explicit slot fields always win, including an explicit empty value: a draft
   * slot that says "no unavailable_reason" must never inherit a reason that
   * belongs to a different slot. `undefined` counts as absent.
   */
  function pick(obj, key, fallback) {
    if (isObj(obj) && hasOwn(obj, key) && obj[key] !== undefined) return obj[key];
    return fallback;
  }

  /* Errors/conflicts may arrive as plain strings, objects, or bare `true`. */
  function messageOf(value, fallback) {
    if (typeof value === 'string') return value;
    if (value && typeof value === 'object') {
      return str(value.message) || str(value.detail) || str(value.reason) || fallback;
    }
    return value ? fallback : '';
  }

  /* The agent that owns the model, never a hard-coded literal when known. */
  function agentOf(model) {
    var agent = str(model && model.default_agent).trim();
    var names={dsh:'DeepSeek Harness',pi:'Pi Agent',workbuddy:'WorkBuddy',zcode:'ZCode',opencode:'OpenCode',commandcode:'CommandCode',follow_global:'跟随全局默认'};
    return hasOwn(names,agent)?names[agent]:(agent||'跟随全局默认');
  }

  function modelName(model) {
    return str(model && model.name) || str(model && model.model) || str(model && model.id);
  }

  function modelLabel(model) {
    return modelName(model) + '（' + agentOf(model) + '）';
  }

  /* Null-prototype map: ids such as `constructor` must behave like any other id. */
  function buildModelIndex(models) {
    var byId = Object.create(null);
    var list = [];
    for (var i = 0; i < models.length; i += 1) {
      var model = models[i];
      if (!isObj(model)) continue;
      var id = str(model.id);
      if (!id || hasOwn(byId, id)) continue;
      byId[id] = model;
      list.push(model);
    }
    return { byId: byId, list: list };
  }

  /*
   * Optional enrichment for thin draft slots. Matching is by model id, never by
   * position, so a reordered draft can never borrow a neighbour's label/reason.
   */
  function findBase(policySlots, modelId) {
    if (!modelId) return null;
    for (var i = 0; i < policySlots.length; i += 1) {
      var candidate = policySlots[i];
      if (isObj(candidate) && str(candidate.model_id) === modelId) return candidate;
    }
    return null;
  }

  function renderModelOptions(list, selectedId) {
    var out = [];
    for (var i = 0; i < list.length; i += 1) {
      var model = list[i];
      var id = str(model.id);
      var label = modelLabel(model);
      if (model.enabled === false) label += '（已停用）';
      out.push(
        '<option value="' + esc(id) + '"' + (id === selectedId ? ' selected' : '') + '>' +
          esc(label) +
        '</option>'
      );
    }
    return out.join('');
  }

  function renderSlot(index, slot, base, total, modelIndex, locked) {
    var slotObj = isObj(slot) ? slot : {};
    var fallback = isObj(base) ? base : {};

    var selectedId = str(pick(slotObj, 'model_id', fallback.model_id));
    var label = str(pick(slotObj, 'label', fallback.label));
    var reason = str(pick(slotObj, 'unavailable_reason', fallback.unavailable_reason));
    var selectedModel =
      selectedId && hasOwn(modelIndex.byId, selectedId) ? modelIndex.byId[selectedId] : null;

    var position = index + 1;
    var disabled = locked ? ' disabled' : '';

    var html = [];
    html.push('<li class="priority-editor__slot" data-priority-index="' + index + '">');
    html.push('<span class="priority-editor__index" aria-hidden="true">' + position + '</span>');
    html.push('<div class="priority-editor__field">');
    html.push(
      '<select class="priority-editor__select" data-priority-select="' + index + '"' +
        ' title="' + esc(selectedModel ? modelLabel(selectedModel) : label || '保留空位（跳过）') + '"' +
        ' aria-label="第 ' + position + ' 顺位模型"' + disabled + '>'
    );
    html.push(
      '<option value=""' + (selectedId === '' ? ' selected' : '') + '>保留空位（跳过）</option>'
    );
    html.push(renderModelOptions(modelIndex.list, selectedId));
    if (selectedId !== '' && !selectedModel) {
      html.push(
        '<option value="' + esc(selectedId) + '" selected>' +
          esc((label || selectedId) + '（已缺失）') +
        '</option>'
      );
    }
    html.push('</select>');
    if (selectedModel) {
      var effort=selectedModel.reasoning_label||({default:'默认',off:'关闭',low:'低',medium:'中',high:'高',xhigh:'超高',max:'最高'}[selectedModel.thinking_level]||selectedModel.thinking_level||'默认');
      html.push('<span class="priority-editor__client">'+esc(agentOf(selectedModel))+' · 思考：'+esc(effort)+'</span>');
      html.push('<span class="priority-editor__model-code">' + esc(selectedModel.model) + '</span>');
    }
    html.push('<button type="button" class="priority-editor__pick" data-priority-pick="'+index+'"'+disabled+' aria-label="选择第 '+position+' 顺位的客户端和模型">选择客户端 / 模型</button>');

    var notes = [];
    var tone = 'priority-editor__slot-note';
    if (selectedId === '') {
      notes.push('保留空位，自动路由会跳过这一顺位。');
      if (label) notes.push('原顺位：' + label);
      if (reason) notes.push('缺失原因：' + reason);
    } else if (!selectedModel) {
      notes.push('所选模型 ' + selectedId + ' 已缺失，路由时会跳过这一顺位。');
      if (label) notes.push('原顺位：' + label);
      if (reason) notes.push('缺失原因：' + reason);
      tone += ' priority-editor__slot-note--warn';
    } else if (selectedModel.enabled === false) {
      notes.push('该模型已停用，路由时会跳过这一顺位。');
      if (label) notes.push('原顺位：' + label);
      if (reason) notes.push('记录原因：' + reason);
      tone += ' priority-editor__slot-note--warn';
    }
    if (notes.length) {
      html.push('<div class="priority-editor__slot-notes">');
      for (var n = 0; n < notes.length; n += 1) {
        if (n === 1) html.push('<details class="priority-editor__note-detail"><summary>查看顺位说明</summary>');
        html.push('<span class="' + tone + '">' + esc(notes[n]) + '</span>');
      }
      if (notes.length > 1) html.push('</details>');
      html.push('</div>');
    }
    html.push('</div>');

    html.push('<div class="priority-editor__order">');
    html.push(
      '<button type="button" class="priority-editor__order-btn" data-priority-up="' + index + '"' +
        ' aria-label="将第 ' + position + ' 顺位上移"' +
        (locked || index === 0 ? ' disabled' : '') +
        ' title="上移">↑</button>'
    );
    html.push(
      '<button type="button" class="priority-editor__order-btn" data-priority-down="' + index + '"' +
        ' aria-label="将第 ' + position + ' 顺位下移"' +
        (locked || index === total - 1 ? ' disabled' : '') +
        ' title="下移">↓</button>'
    );
    html.push(
      '<button type="button" class="priority-editor__order-btn" data-priority-remove="' + index + '"' +
        ' aria-label="删除第 ' + position + ' 顺位"' + disabled +
        ' title="删除">✕</button>'
    );
    html.push('</div>');
    html.push('</li>');
    return html.join('');
  }

  function render(data, draft) {
    var source = isObj(data) ? data : {};
    var policy = isObj(source.policy) ? source.policy : {};
    var state = isObj(draft) ? draft : {};

    var modelIndex = buildModelIndex(asArray(source.models));
    var policySlots = asArray(policy.slots);
    var draftSlots = Array.isArray(state.slots) ? state.slots : null;
    var slots = (draftSlots !== null ? draftSlots : policySlots).slice(0, MAX_SLOTS);

    var enabled = typeof state.enabled === 'boolean' ? state.enabled : policy.enabled === true;
    var readOnly = policy.readable === false;
    var busy = !!state.busy;
    var dirty = !!state.dirty;
    var conflict = messageOf(state.conflict, '配置已在别处更新，请先重置草稿再保存。');
    var error = messageOf(state.error, '操作失败，请重试。');
    var notice = messageOf(state.notice, '');
    var busyText = typeof state.busy === 'string' ? state.busy : '正在处理…';

    var locked = busy || readOnly;
    var attrDisabled = locked ? ' disabled' : '';
    var saveDisabled = busy || readOnly || !dirty || !!conflict;
    var resetDisabled = busy || (!dirty && !conflict);
    var addDisabled = locked || slots.length >= MAX_SLOTS;

    var html = [];
    html.push(
      '<section class="priority-editor' + (enabled ? '' : ' priority-editor--off') +
        '" aria-label="模型使用顺序">'
    );

    html.push('<div class="priority-editor__head">');
    html.push('<span class="priority-editor__title">模型使用顺序</span>');
    html.push('<div class="priority-editor__head-right">');
    html.push('<label class="priority-editor__switch">');
    html.push(
      '<input type="checkbox" class="priority-editor__checkbox" data-priority-enabled' +
        ' aria-label="启用指定模型顺序"' + (enabled ? ' checked' : '') + attrDisabled + '>'
    );
    html.push('<span class="priority-editor__switch-text">启用</span>');
    html.push('</label>');
    html.push(
      '<span class="priority-editor__counter">顺位 ' + slots.length + ' / ' + MAX_SLOTS + '</span>'
    );
    html.push('</div>');
    html.push('</div>');

    var chips = [];
    if (dirty) {
      chips.push('<span class="priority-editor__chip priority-editor__chip--dirty">未保存</span>');
    }
    if (busy) {
      chips.push(
        '<span class="priority-editor__chip priority-editor__chip--busy" role="status">' +
          esc(busyText) +
        '</span>'
      );
    }
    if (readOnly) {
      chips.push('<span class="priority-editor__chip priority-editor__chip--readonly">只读</span>');
    }
    if (!enabled) {
      chips.push('<span class="priority-editor__chip priority-editor__chip--off">未启用</span>');
    }
    if (chips.length) {
      html.push('<div class="priority-editor__chips">' + chips.join('') + '</div>');
    }

    html.push(
      '<p class="priority-editor__hint">顺位 1 → 2 → 3 只影响之后新建的自动任务；' +
        '实际的授权与能力校验仍然生效。</p>'
    );
    if (!enabled) {
      html.push(
        '<p class="priority-editor__hint priority-editor__hint--off">当前未启用指定顺序，' +
          '新自动任务沿用现有的自动路由规则；下方顺序仍可编辑，启用后按 1 → 2 → 3 生效。</p>'
      );
    }

    if (readOnly) {
      html.push(
        '<p class="priority-editor__error" role="alert">' +
          esc(messageOf(policy.error, '配置无法读取，暂时不能编辑顺序。')) +
        '</p>'
      );
    } else if (policy.error) {
      html.push(
        '<p class="priority-editor__error" role="alert">' +
          esc(messageOf(policy.error, '模型配置读取失败。')) +
        '</p>'
      );
    }
    if (conflict) {
      html.push('<p class="priority-editor__conflict" role="alert">' + esc(conflict) + '</p>');
    }
    if (error) {
      html.push('<p class="priority-editor__error" role="alert">' + esc(error) + '</p>');
    }
    if (notice) {
      html.push('<p class="priority-editor__notice" role="status">' + esc(notice) + '</p>');
    }

    if (slots.length) {
      html.push('<ol class="priority-editor__slots">');
      for (var s = 0; s < slots.length; s += 1) {
        var modelId = str(pick(slots[s], 'model_id', ''));
        html.push(
          renderSlot(s, slots[s], findBase(policySlots, modelId), slots.length, modelIndex, locked)
        );
      }
      html.push('</ol>');
    } else {
      html.push(
        '<p class="priority-editor__empty">还没有顺位。添加顺位后可指定自动任务优先尝试的模型。</p>'
      );
    }

    html.push('<div class="priority-editor__foot">');
    html.push(
      '<button type="button" class="priority-editor__btn" data-priority-add' +
        (addDisabled ? ' disabled' : '') + ' aria-label="添加一个顺位">添加顺位</button>'
    );
    html.push(
      '<button type="button" class="priority-editor__btn priority-editor__btn--primary"' +
        ' data-priority-save' + (saveDisabled ? ' disabled' : '') + '>保存</button>'
    );
    html.push(
      '<button type="button" class="priority-editor__btn" data-priority-reset' +
        (resetDisabled ? ' disabled' : '') + '>重置草稿</button>'
    );
    html.push(
      '<span class="priority-editor__foot-note">最多 ' + MAX_SLOTS + ' 个顺位，空位会被跳过。</span>'
    );
    html.push('</div>');

    html.push('</section>');
    return html.join('');
  }

  root.ModelDeskPriorityView = { render: render };
})(typeof globalThis !== 'undefined' ? globalThis : this);
