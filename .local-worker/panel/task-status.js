/*
 * Model Desk · 任务状态展示组件（只读）
 * ------------------------------------------------------------------
 * 输入：后台 task_status 核心生成的中文状态摘要（schema === 1）。
 * 输出：HTML 字符串，动态文本全部转义，由宿主页面自行插入。
 *
 * 约束（与 docs/TASK_STATUS_VIEW.md 展示组件契约一致）：
 *   - 无依赖、无网络、不触碰页面结构、不挂接事件，只拼装字符串
 *   - schema/source/code/label 严格校验，不合法一律返回空字符串
 *   - tone 只接受 neutral/accent/success/warning/danger，其余降为 neutral
 *   - 状态结论完全来自后台摘要，组件不做推断、不维护第二套状态机
 *   - 长文本按上限截断并带可见省略号，绝不静默丢弃或伪装结论
 */
(function () {
  'use strict';

  var SCHEMA = 1;
  var SOURCE = 'controller_projection';
  var CODE_RE = /^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$/;
  var TONES = { neutral: true, accent: true, success: true, warning: true, danger: true };

  var MAX_LABEL = 80;
  var MAX_TEXT = 400;
  var MAX_FACTS = 4;
  var MAX_FACT_KEY = 20;
  var MAX_FACT_VALUE = 160;
  var MAX_RAW_STATUS = 40;
  var MAX_BADGE_LABEL = 24;

  /* ---------- 基础工具 ---------- */

  function isObject(v) {
    return typeof v === 'object' && v !== null && !Array.isArray(v);
  }

  /** 只接受可安全显示为文本的标量，其余视为缺失 */
  function text(v) {
    if (typeof v === 'string') return v;
    if (typeof v === 'number' && isFinite(v)) return String(v);
    if (typeof v === 'boolean') return v ? 'true' : 'false';
    return '';
  }

  /** 文本与属性双上下文转义，杜绝把传入内容当作标记、类名或属性边界 */
  function esc(v) {
    return text(v).replace(/[&<>"'`=/]/g, function (c) {
      switch (c) {
        case '&': return '&amp;';
        case '<': return '&lt;';
        case '>': return '&gt;';
        case '"': return '&quot;';
        case '\'': return '&#39;';
        case '`': return '&#96;';
        case '=': return '&#61;';
        case '/': return '&#47;';
        default: return c;
      }
    });
  }

  /** data-* 属性用的安全 token：白名单字符、小写、去首尾下划线 */
  function tok(v) {
    return String(v == null ? '' : v)
      .toLowerCase()
      .replace(/[^a-z0-9_.-]+/g, '_')
      .replace(/^_+|_+$/g, '')
      .slice(0, MAX_RAW_STATUS);
  }

  var CONTROL_RE = /[\u0000-\u0008\u000B\u000C\u000E-\u001F\u007F]/g;

  /** 展示文本：去控制字符、压平空白、按上限截断并带可见省略号 */
  function clean(v, max) {
    var s = text(v).replace(CONTROL_RE, '').replace(/\s+/g, ' ').trim();
    if (!s || s.length <= max) return s;
    var cut = s.slice(0, max);
    var last = cut.charCodeAt(cut.length - 1);
    if (last >= 0xD800 && last <= 0xDBFF) cut = cut.slice(0, -1);
    return cut + '…';
  }

  /* ---------- 契约校验 ---------- */

  function valid(summary) {
    try {
      if (!isObject(summary)) return false;
      if (typeof summary.schema !== 'number' || summary.schema !== SCHEMA) return false;
      if (summary.source !== SOURCE) return false;
      if (typeof summary.code !== 'string' || !CODE_RE.test(summary.code)) return false;
      if (typeof summary.label !== 'string' || !summary.label.trim()) return false;
      return true;
    } catch (err) {
      return false;
    }
  }

  /* ---------- 归一化（只做展示裁剪，不改状态结论） ---------- */

  function normFacts(raw) {
    var out = [];
    if (!Array.isArray(raw)) return out;
    for (var i = 0; i < raw.length && out.length < MAX_FACTS; i++) {
      var item = raw[i];
      if (!isObject(item)) continue;
      var k = clean(item.label, MAX_FACT_KEY);
      var v = clean(item.value, MAX_FACT_VALUE);
      if (k && v) out.push({ key: k, value: v });
    }
    return out;
  }

  function norm(summary) {
    var tone = typeof summary.tone === 'string' &&
      Object.prototype.hasOwnProperty.call(TONES, summary.tone) ? summary.tone : 'neutral';
    return {
      code: summary.code,
      label: clean(summary.label, MAX_LABEL),
      tone: tone,
      detail: clean(summary.detail, MAX_TEXT),
      nextStep: clean(summary.next_step, MAX_TEXT),
      facts: normFacts(summary.facts),
      rawStatus: tok(clean(summary.raw_status, MAX_RAW_STATUS))
    };
  }

  /* ---------- 渲染 ---------- */

  function renderInner(n) {
    var attrs = ' class="md-task-status md-task-status--' + n.tone + '"' +
      ' role="status"' +
      ' aria-label="任务当前进度"' +
      ' data-status-code="' + esc(n.code) + '"' +
      ' data-tone="' + n.tone + '"' +
      (n.rawStatus ? ' data-raw-status="' + esc(n.rawStatus) + '"' : '');

    var parts = ['<div class="md-task-status__head">' +
      '<span class="md-task-status__label">' + esc(n.label) + '</span></div>'];
    if (n.detail) {
      parts.push('<p class="md-task-status__detail">' + esc(n.detail) + '</p>');
    }
    if (n.facts.length) {
      var items = '';
      for (var i = 0; i < n.facts.length; i++) {
        items += '<li class="md-task-status__fact">' +
          '<span class="md-task-status__fact-key">' + esc(n.facts[i].key) + '</span>' +
          '<span class="md-task-status__fact-val">' + esc(n.facts[i].value) + '</span></li>';
      }
      parts.push('<ul class="md-task-status__facts">' + items + '</ul>');
    }
    if (n.nextStep) {
      parts.push('<div class="md-task-status__next">' +
        '<span class="md-task-status__next-key">下一步</span>' +
        '<span class="md-task-status__next-val">' + esc(n.nextStep) + '</span></div>');
    }
    return '<div' + attrs + '><div class="md-task-status__body">' + parts.join('') + '</div></div>';
  }

  function render(summary) {
    if (!valid(summary)) return '';
    try {
      var n = norm(summary);
      if (!n.label) return '';
      return renderInner(n);
    } catch (err) {
      return '';
    }
  }

  function badge(summary) {
    if (!valid(summary)) return '';
    try {
      var n = norm(summary);
      if (!n.label) return '';
      return '<span class="md-task-badge md-task-badge--' + n.tone + '"' +
        ' data-status-code="' + esc(n.code) + '"' +
        ' data-tone="' + n.tone + '"' +
        ' title="' + esc(n.label) + '">' + esc(clean(n.label, MAX_BADGE_LABEL)) + '</span>';
    } catch (err) {
      return '';
    }
  }

  var api = { valid: valid, render: render, badge: badge };

  var root = (typeof globalThis === 'object' && globalThis) || null;
  if (root) root.ModelDeskTaskStatus = api;
  if (typeof window === 'object' && window && window !== root) window.ModelDeskTaskStatus = api;
  if (typeof module === 'object' && module && module.exports) module.exports = api;
})();
