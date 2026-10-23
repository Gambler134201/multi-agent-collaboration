/*
 * Model Desk · 执行结果摘要组件（只读）
 * ------------------------------------------------------------------
 * 输入：符合合同（schema_version === 1）的任务结果对象
 * 输出：HTML 字符串（所有动态字段已转义，可插入工作台面板）
 *
 * 约束：
 *   - 只接受 schema_version === 1；无数据或不兼容 → 返回空字符串
 *   - 不读取任何全局任务状态，不自行推断“通过/验收”
 *   - 不请求 HTTP、不加载外部依赖、不使用 innerHTML、不生成脚本或事件处理器
 *   - 字段缺失或畸形时稳健降级，不抛异常
 */
(function () {
  'use strict';

  var SCHEMA_VERSION = 1;
  var MAX_INLINE_FILES = 8;
  var MAX_TEXT = 400;
  var MAX_STAGE_DETAIL = 200;
  var MAX_ISSUE = 300;

  /* ---------- 语义字典（合同固定，未知值一律降级为“未知”） ---------- */

  var EXEC_LABEL = {
    unknown: '执行状态未知',
    running: '执行中',
    completed: '执行完成',
    failed: '执行失败',
    cancelled: '执行已取消',
    not_run: '未开始执行',
    not_requested: '未要求执行',
    not_applicable: '执行不适用'
  };

  var STAGE_LABEL = {
    unknown: '状态未知',
    running: '进行中',
    completed: '已完成',
    failed: '失败',
    cancelled: '已取消',
    not_run: '未运行',
    not_requested: '未要求',
    not_applicable: '不适用'
  };

  var ARTIFACT_LABEL = {
    changed: '有文件改动',
    none: '无文件改动',
    unknown: '文件情况未知'
  };

  var VERIFY_LABEL = {
    passed: '自测通过',
    failed: '自测失败',
    not_run: '自测未运行',
    not_requested: '自测未要求',
    unknown: '自测状态未知'
  };

  var REVIEW_LABEL = {
    accepted: '主脑已验收',
    final_accepted: '主脑接手后通过',
    returned: '主脑已退回',
    blocked: '主脑判定阻塞',
    executor_failed: '执行器失败，未进入验收',
    executor_completed: '执行器完成，待主脑验收'
  };

  var REVIEW_FALLBACK = '尚无独立验收结论';

  /* review.state 写成通过类结论，但 accepted 不是严格 true 时：不显示通过，也不给通过配色 */
  var REVIEW_PASS_STATES = { accepted: true, final_accepted: true };
  var REVIEW_UNCONFIRMED = '验收结论待核实';

  var TONE = {
    exec: {
      completed: 'ok', failed: 'err', running: 'info', cancelled: 'mute',
      not_run: 'mute', not_requested: 'mute', not_applicable: 'mute', unknown: 'warn'
    },
    stage: {
      completed: 'ok', failed: 'err', running: 'info', cancelled: 'mute',
      not_run: 'mute', not_requested: 'mute', not_applicable: 'mute', unknown: 'warn'
    },
    artifact: { changed: 'info', none: 'mute', unknown: 'warn' },
    verify: { passed: 'ok', failed: 'err', not_run: 'warn', not_requested: 'mute', unknown: 'warn' },
    /* final_accepted 是主脑接手后的结论，不用“执行器通过”的绿色 */
    review: {
      accepted: 'ok', final_accepted: 'info', returned: 'warn', blocked: 'err',
      executor_failed: 'err', executor_completed: 'info', unknown: 'mute'
    }
  };

  /* ---------- 基础工具 ---------- */

  function isObject(v) {
    return typeof v === 'object' && v !== null && !Array.isArray(v);
  }

  /** 只接受可安全显示为文本的标量，其他类型返回空串（不 stringify 对象） */
  function text(v) {
    if (typeof v === 'string') return v.trim();
    if (typeof v === 'number' && isFinite(v)) return String(v);
    if (typeof v === 'boolean') return v ? 'true' : 'false';
    return '';
  }

  /** HTML 文本与属性双上下文转义 */
  function esc(v) {
    var s = typeof v === 'string' ? v : text(v);
    return s.replace(/[&<>"'`=/]/g, function (c) {
      switch (c) {
        case '&': return '&amp;';
        case '<': return '&lt;';
        case '>': return '&gt;';
        case '"': return '&quot;';
        case '\'': return '&#39;';
        case '`': return '&#96;';
        case '=': return '&#61;';
        default: return c;
      }
    });
  }

  /** data-* 属性用的安全 token（白名单，不做任何语义推断） */
  function tok(v) {
    return String(typeof v === 'string' ? v : '')
      .toLowerCase()
      .replace(/[^a-z0-9_.\-]/g, '_')
      .slice(0, 64);
  }

  function key(v) {
    return typeof v === 'string' ? v.trim().toLowerCase() : '';
  }

  function pick(map, k, fallback) {
    return (k && Object.prototype.hasOwnProperty.call(map, k) && map[k]) || fallback;
  }

  function tone(map, k) {
    return (k && Object.prototype.hasOwnProperty.call(map, k) && map[k]) || 'mute';
  }

  function trunc(s, max) {
    var s2 = String(s == null ? '' : s).replace(/\s+/g, ' ').trim();
    if (s2.length <= max) return s2;
    return s2.slice(0, max) + '…';
  }

  function tag(label, toneName, extraClass) {
    return '<span class="mdx-res-tag is-' + toneName + (extraClass ? ' ' + extraClass : '') + '">' + esc(label) + '</span>';
  }

  function note(s) {
    return '<span class="mdx-res-note">' + esc(s) + '</span>';
  }

  function row(modifier, kLabel, valueHtml, dataAttrs) {
    return '<div class="mdx-res-row mdx-res-row--' + modifier + '"' + (dataAttrs || '') + '>' +
      '<span class="mdx-res-k">' + esc(kLabel) + '</span>' +
      '<span class="mdx-res-v">' + valueHtml + '</span>' +
      '</div>';
  }

  /* ---------- 归一化 ---------- */

  function normStages(raw) {
    if (!Array.isArray(raw)) return [];
    var out = [];
    for (var i = 0; i < raw.length; i++) {
      var s = raw[i];
      if (!isObject(s)) continue;
      var id = text(s.id);
      var st = key(s.state);
      var label = text(s.label) || id || '未命名阶段';
      out.push({
        id: id,
        label: label,
        state: st,
        stateLabel: pick(STAGE_LABEL, st, STAGE_LABEL.unknown),
        detail: text(s.detail)
      });
    }
    return out;
  }

  function findStage(stages, id) {
    if (!id) return null;
    var i;
    for (i = 0; i < stages.length; i++) if (stages[i].id && stages[i].id === id) return stages[i];
    for (i = 0; i < stages.length; i++) if (stages[i].id && stages[i].id.toLowerCase() === id.toLowerCase()) return stages[i];
    return null;
  }

  function normFailure(raw, stages) {
    if (typeof raw === 'string') {
      var t = raw.trim();
      return t ? { stageText: '', stageKnown: false, stageId: '', code: '', message: t, source: '', rule: '' } : null;
    }
    if (!isObject(raw)) return null;
    var f = {
      stageText: '',
      stageKnown: false,
      stageId: '',
      code: text(raw.code),
      message: text(raw.message),
      source: text(raw.source),
      rule: text(raw.rule_id)
    };
    var stageRaw = text(raw.stage);
    if (stageRaw) {
      var hit = findStage(stages, stageRaw);
      if (hit) {
        f.stageKnown = true;
        f.stageText = hit.label;
        f.stageId = hit.id;
      } else {
        /* 只按 id 精确匹配；匹配不到就原样显示记录值，绝不从错误文字推断阶段 */
        f.stageText = stageRaw;
      }
    }
    if (!f.code && !f.message && !f.source && !f.rule && !f.stageText) return null;
    return f;
  }

  function normFiles(raw) {
    if (!Array.isArray(raw)) return [];
    var out = [];
    for (var i = 0; i < raw.length; i++) {
      var f = text(raw[i]);
      if (f) out.push(trunc(f, 240));
    }
    return out;
  }

  function normIssues(raw) {
    if (!Array.isArray(raw)) return [];
    var out = [];
    for (var i = 0; i < raw.length; i++) {
      var it = raw[i];
      var s = '';
      if (typeof it === 'string') s = trunc(it, MAX_ISSUE);
      else if (typeof it === 'number' || typeof it === 'boolean') s = String(it);
      else if (isObject(it)) {
        var bits = [];
        var fields = ['stage', 'code', 'message', 'detail', 'severity', 'rule_id'];
        for (var j = 0; j < fields.length; j++) {
          var v = text(it[fields[j]]);
          if (v) bits.push(fields[j] + ': ' + v);
        }
        if (bits.length) s = trunc(bits.join(' · '), MAX_ISSUE);
        else {
          try { s = trunc(JSON.stringify(it), 200); } catch (e) { s = '无法显示的问题记录'; }
        }
      }
      if (s) out.push(s);
    }
    return out;
  }

  function exitCodeText(v) {
    if (typeof v === 'number' && isFinite(v)) return '退出码 ' + v;
    if (typeof v === 'string' && /^-?\d{1,9}$/.test(v.trim())) return '退出码 ' + v.trim();
    return '';
  }

  /* ---------- 渲染 ---------- */

  function renderSafe(result) {
    if (!isObject(result)) return '';
    if (result.schema_version !== SCHEMA_VERSION) return '';

    var exec = isObject(result.execution) ? result.execution : null;
    var statusKey = exec ? key(exec.status) : '';
    var legacy = result.legacy === true;

    var stages = normStages(result.stages);
    var failure = normFailure(result.failure, stages);
    var artifacts = isObject(result.artifacts) ? result.artifacts : null;
    var verification = isObject(result.verification) ? result.verification : null;
    var review = isObject(result.review) ? result.review : null;
    var issues = normIssues(result.issues);

    var hasData = !!(exec || stages.length || failure || artifacts || verification || review || issues.length || legacy);
    if (!hasData) return '';

    var parts = [];
    var hiddenFiles = [];

    /* 头部：执行状态 + 引擎 + 历史记录标记 */
    var head = '<div class="mdx-res-head">' +
      '<span class="mdx-res-title">执行结果</span>' +
      tag(pick(EXEC_LABEL, statusKey, EXEC_LABEL.unknown), tone(TONE.exec, statusKey)) +
      (exec && text(exec.engine) ? '<span class="mdx-res-chip">' + esc(text(exec.engine)) + '</span>' : '') +
      (legacy ? '<span class="mdx-res-chip">历史记录</span>' : '') +
      '</div>';
    parts.push(head);

    if (legacy) {
      parts.push('<div class="mdx-res-alert">历史记录：阶段证据不足，未据此判定测试通过</div>');
    }

    /* 阶段：紧凑标签条，只按记录显示，不做推断 */
    if (stages.length) {
      var items = [];
      for (var i = 0; i < stages.length; i++) {
        var s = stages[i];
        var isFailStage = !!(failure && failure.stageKnown && failure.stageId === s.id);
        items.push(
          '<li class="mdx-res-stage" data-stage-id="' + tok(s.id) + '" data-state="' + tok(s.state) + '">' +
          '<span class="mdx-res-stage-label">' + esc(s.label) + '</span>' +
          tag(s.stateLabel, tone(TONE.stage, s.state)) +
          (isFailStage && s.detail ? '<span class="mdx-res-stage-detail">' + esc(trunc(s.detail, 120)) + '</span>' : '') +
          '</li>'
        );
      }
      parts.push('<ol class="mdx-res-stages">' + items.join('') + '</ol>');
    }

    /* 失败：阶段 / code / message / 来源 / 规则 */
    if (failure) {
      var fv = '';
      fv += tag('阶段：' + (failure.stageText ? failure.stageText : '未记录'), 'err');
      if (failure.code) fv += '<code class="mdx-res-code">' + esc(failure.code) + '</code>';
      if (failure.message) fv += '<span class="mdx-res-msg">' + esc(failure.message) + '</span>';
      var fmeta = [];
      if (failure.source) fmeta.push('来源 ' + failure.source);
      if (failure.rule) fmeta.push('规则 ' + failure.rule);
      if (fmeta.length) fv += note(fmeta.join(' · '));
      parts.push(row('fail', '失败', fv, ' data-failure-stage="' + tok(failure.stageId) + '"'));
    }

    /* 已有文件 */
    if (artifacts) {
      var aKey = key(artifacts.state);
      var files = normFiles(artifacts.files);
      var av = tag(pick(ARTIFACT_LABEL, aKey, ARTIFACT_LABEL.unknown), tone(TONE.artifact, aKey));
      if (files.length) {
        av += note('共 ' + files.length + ' 项');
        av += '<span class="mdx-res-files">';
        for (var k2 = 0; k2 < files.length; k2++) {
          if (k2 < MAX_INLINE_FILES) av += '<code class="mdx-res-file">' + esc(files[k2]) + '</code>';
          else hiddenFiles.push(files[k2]);
        }
        if (hiddenFiles.length) av += note('另有 ' + hiddenFiles.length + ' 项见扩展信息');
        av += '</span>';
      }
      if (text(artifacts.scope_label)) av += note(text(artifacts.scope_label));
      parts.push(row('files', '文件', av, ' data-artifacts-state="' + tok(aKey) + '"'));
    }

    /* 自测（执行器自身，绝不等同独立验收） */
    var vKey = verification ? key(verification.state) : '';
    var vv = tag(pick(VERIFY_LABEL, vKey, VERIFY_LABEL.unknown), tone(TONE.verify, vKey));
    if (verification) {
      if (text(verification.source)) vv += note('来源 ' + text(verification.source));
      var ec = exitCodeText(verification.exit_code);
      if (ec) vv += note(ec);
    } else {
      vv += note('结果未记录自测字段');
    }
    vv += note('执行器自测，不等于主脑独立验收');
    parts.push(row('verify', '自测', vv, ' data-verify-state="' + tok(vKey) + '"'));

    /* 独立验收（主脑结论）：通过类结论必须有严格 true 的 accepted 才按通过显示 */
    var rKey = review ? key(review.state) : '';
    var hasAcceptedField = !!(review && Object.prototype.hasOwnProperty.call(review, 'accepted'));
    var acceptedStrict = !!(review && review.accepted === true);
    var claimsPass = !!(rKey && Object.prototype.hasOwnProperty.call(REVIEW_PASS_STATES, rKey));
    var passConfirmed = claimsPass && acceptedStrict;
    var passUnconfirmed = claimsPass && !acceptedStrict;

    var rLabel, rTone;
    if (passUnconfirmed) {
      /* 记录自称通过但接受标记不成立：保持“待核实”，不给通过配色 */
      rLabel = REVIEW_UNCONFIRMED;
      rTone = 'warn';
    } else {
      rLabel = pick(REVIEW_LABEL, rKey, REVIEW_FALLBACK);
      rTone = tone(TONE.review, rKey);
    }
    var rv = tag(rLabel, rTone);
    var rnotes = [];
    if (review) {
      if (hasAcceptedField) rnotes.push('接受标记：' + (acceptedStrict ? '是' : '否（非严格 true）'));
      else rnotes.push('结果未记录接受标记');
      if (passUnconfirmed) rnotes.push('记录结论为通过类，但接受标记不成立，未视为通过');
      if (acceptedStrict && !claimsPass) rnotes.push('接受标记为 true，但记录结论不是通过，按记录结论显示');
      if (rKey === 'final_accepted' && acceptedStrict) rnotes.push('不代表执行器独立成功');
      if (claimsPass && (statusKey === 'failed' || statusKey === 'cancelled')) {
        rnotes.push('主脑结论不改变执行阶段状态');
      }
    } else {
      rnotes.push('结果未记录独立验收字段');
    }
    for (var m = 0; m < rnotes.length; m++) rv += note(rnotes[m]);
    parts.push(row('review', '独立验收', rv,
      ' data-review-state="' + tok(rKey) + '"' +
      ' data-accepted="' + (acceptedStrict ? 'true' : 'false') + '"' +
      ' data-review-confirmed="' + (passConfirmed ? 'true' : 'false') + '"'));

    /* 扩展信息：问题列表 / 阶段明细 / 其余文件 */
    var stageDetails = [];
    for (var n = 0; n < stages.length; n++) {
      if (!stages[n].detail) continue;
      if (failure && failure.stageKnown && failure.stageId === stages[n].id) continue;
      stageDetails.push(stages[n].label + '：' + trunc(stages[n].detail, MAX_STAGE_DETAIL));
    }
    var subs = [], summary = [];
    if (issues.length) {
      summary.push('问题 ' + issues.length);
      var lis = '';
      for (var q = 0; q < issues.length; q++) lis += '<li>' + esc(issues[q]) + '</li>';
      subs.push('<p class="mdx-res-subhead">问题</p><ul class="mdx-res-list">' + lis + '</ul>');
    }
    if (stageDetails.length) {
      summary.push('阶段明细 ' + stageDetails.length);
      var slis = '';
      for (var p2 = 0; p2 < stageDetails.length; p2++) slis += '<li>' + esc(stageDetails[p2]) + '</li>';
      subs.push('<p class="mdx-res-subhead">阶段明细</p><ul class="mdx-res-list">' + slis + '</ul>');
    }
    if (artifacts && hiddenFiles.length) {
      summary.push('其余文件 ' + hiddenFiles.length);
      var flis = '';
      for (var r2 = 0; r2 < hiddenFiles.length; r2++) flis += '<li><code class="mdx-res-file">' + esc(hiddenFiles[r2]) + '</code></li>';
      subs.push('<p class="mdx-res-subhead">其余文件</p><ul class="mdx-res-list">' + flis + '</ul>');
    }
    if (subs.length) {
      parts.push('<details class="mdx-res-more"><summary>扩展信息（' + esc(summary.join(' · ')) + '）</summary>' + subs.join('') + '</details>');
    }

    return '<div class="mdx-res" data-schema-version="' + SCHEMA_VERSION + '"' +
      ' data-execution-status="' + tok(statusKey) + '"' +
      (legacy ? ' data-legacy="true"' : '') + '>' +
      '<div class="mdx-res-box">' + parts.join('') + '</div>' +
      '</div>';
  }

  function render(result) {
    try {
      return renderSafe(result);
    } catch (e) {
      return '';
    }
  }

  var api = { render: render };

  var root = (typeof globalThis === 'object' && globalThis) || null;
  if (root) root.ModelDeskExecutionResult = api;
  if (typeof window === 'object' && window && window !== root) window.ModelDeskExecutionResult = api;
})();
