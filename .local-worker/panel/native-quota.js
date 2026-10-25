/* Native quota view module. */
(function (global) {
  'use strict';

  var INTERVAL_OPTIONS = [60, 180, 300, 900];
  var DEFAULT_INTERVAL = 180;

  var ENDPOINTS = {
    workbuddy: { name: 'WorkBuddy' },
    zcode: { name: 'ZCode' },
    opencode: { name: 'OpenCode' },
    commandcode: { name: 'CommandCode' }
  };

  /* Accept common spellings, but keep the caller's id for data attributes so
     event wiring can key on the original endpoint id. */
  var ENDPOINT_ALIASES = {
    workbuddy: 'workbuddy',
    'work-buddy': 'workbuddy',
    work_buddy: 'workbuddy',
    zcode: 'zcode',
    'z-code': 'zcode',
    z_code: 'zcode',
    opencode: 'opencode',
    'open-code': 'opencode',
    open_code: 'opencode',
    commandcode: 'commandcode',
    'command-code': 'commandcode',
    command_code: 'commandcode'
  };

  var OFFICIAL_SOURCE_HOSTS = {
    'www.codebuddy.cn': true,
    'www.codebuddy.ai': true,
    'opencode.ai': true,
    'commandcode.ai': true
  };

  var STATUS_META = {
    ready: { label: '已同步', tone: 'ready', notice: '' },
    stale: {
      label: '已过期',
      tone: 'stale',
      notice: '额度数据已过期，当前显示的是上次同步结果。'
    },
    error: {
      label: '同步失败',
      tone: 'error',
      notice: '额度同步失败，下方信息可能不是最新数据。'
    },
    not_connected: {
      label: '未连接',
      tone: 'muted',
      notice: '执行端尚未连接，当前没有可用的原生额度数据。'
    },
    unsupported: {
      label: '不支持原生额度',
      tone: 'muted',
      notice: '该来源不支持原生额度同步。'
    },
    never: {
      label: '尚未同步',
      tone: 'never',
      notice: '尚未同步过原生额度。'
    },
    unknown: {
      label: '状态未知',
      tone: 'unknown',
      notice: '额度状态未知，不能确认已同步。'
    }
  };

  function isObject(value) {
    return value !== null && typeof value === 'object';
  }

  function hasOwn(object, key) {
    return Object.prototype.hasOwnProperty.call(object, key);
  }

  function stringValue(value) {
    return typeof value === 'string' ? value : '';
  }

  function finiteNumber(value) {
    return typeof value === 'number' && isFinite(value) ? value : null;
  }

  function escapeHtml(value) {
    return String(value).replace(/[&<>"']/g, function (character) {
      if (character === '&') return '&amp;';
      if (character === '<') return '&lt;';
      if (character === '>') return '&gt;';
      if (character === '"') return '&quot;';
      return '&#39;';
    });
  }

  function safeSourceUrl(value) {
    var raw = stringValue(value).trim();
    if (!raw || /[\u0000-\u0020\u007f]/.test(raw)) return '';
    var parsed;
    try {
      parsed = new URL(raw);
    } catch (error) {
      return '';
    }
    if (parsed.protocol !== 'https:') return '';
    if (parsed.username || parsed.password || parsed.port) return '';
    if (!hasOwn(OFFICIAL_SOURCE_HOSTS, parsed.hostname.toLowerCase())) return '';
    return raw;
  }

  function normalizeLimit(rawLimit) {
    var raw = isObject(rawLimit) ? rawLimit : {};
    var remaining = finiteNumber(raw.remaining);
    var total = finiteNumber(raw.total);
    var used = finiteNumber(raw.used);
    var percent = finiteNumber(raw.remaining_percent);
    var zeroTotal = total !== null && total === 0;

    if (percent !== null && (percent < 0 || percent > 100)) percent = null;
    if (total !== null && total < 0) percent = null;
    /* A zero total can only keep an explicit 0%; it must never become a full
       or partially full bar. Derivation is disabled below for total <= 0. */
    if (total !== null && total === 0 && percent !== 0) percent = null;

    if (percent === null && total !== null && total > 0) {
      var derived = null;
      if (remaining !== null) {
        derived = (remaining / total) * 100;
      } else if (used !== null) {
        derived = ((total - used) / total) * 100;
      }
      if (derived !== null && isFinite(derived) && derived >= 0 && derived <= 100) {
        percent = derived;
      }
    }

    return {
      label: stringValue(raw.label).trim() || '未命名额度',
      unit: stringValue(raw.unit).trim(),
      remaining: remaining,
      total: total,
      used: used,
      percent: percent,
      zeroTotal: zeroTotal,
      resetAt: secondsValue(raw.reset_at),
      resetLabel: stringValue(raw.reset_label).trim()
    };
  }

  function secondsValue(value) {
    var seconds = finiteNumber(value);
    if (seconds === null || seconds <= 0) return null;
    if (Math.abs(seconds * 1000) > 8640000000000000) return null;
    return seconds;
  }

  function normalizeSync(rawSync) {
    var raw = isObject(rawSync) ? rawSync : {};
    var supplied = raw.interval_seconds;
    var hasSuppliedInterval = supplied !== undefined && supplied !== null &&
      supplied !== '';
    var rawInterval = finiteNumber(supplied);
    var intervalMismatch = hasSuppliedInterval &&
      (rawInterval === null || INTERVAL_OPTIONS.indexOf(rawInterval) === -1);
    return {
      enabled: raw.enabled === true,
      interval: intervalMismatch || rawInterval === null ?
        DEFAULT_INTERVAL : rawInterval,
      intervalMismatch: intervalMismatch
    };
  }

  function normalizeQuota(rawQuota) {
    var raw = isObject(rawQuota) ? rawQuota : {};
    var status = stringValue(raw.status).trim().toLowerCase();
    var capability = stringValue(raw.capability).trim().toLowerCase();

    if (!hasOwn(STATUS_META, status) || status === 'unknown') status = 'unknown';
    if (capability !== 'native' && capability !== 'unsupported') {
      capability = 'unknown';
    }

    /* An unsupported source is never advertised as a successful native sync,
       even if contradictory status fields arrive. */
    if (status === 'unsupported') capability = 'unsupported';
    if (capability === 'unsupported') status = 'unsupported';

    var limits = [];
    if (Array.isArray(raw.limits)) {
      for (var index = 0; index < raw.limits.length; index += 1) {
        if (isObject(raw.limits[index])) {
          limits.push(normalizeLimit(raw.limits[index]));
        }
      }
    }

    return {
      status: status,
      sourceLabel: stringValue(raw.source_label).trim(),
      sourceUrl: stringValue(raw.source_url).trim(),
      capability: capability,
      canRefresh: raw.can_refresh === true,
      fetchedAt: secondsValue(raw.fetched_at),
      lastAttemptAt: secondsValue(raw.last_attempt_at),
      error: stringValue(raw.error).trim(),
      message: stringValue(raw.message).trim(),
      limits: limits,
      sync: normalizeSync(raw.sync)
    };
  }

  function normalize(endpoint) {
    var raw = isObject(endpoint) ? endpoint : {};
    var rawId = stringValue(raw.id).trim();
    var lookup = rawId.toLowerCase();
    var key = hasOwn(ENDPOINT_ALIASES, lookup) ? ENDPOINT_ALIASES[lookup] : '';
    if (!key) {
      return {
        valid: false,
        id: rawId,
        key: '',
        name: '',
        quota: normalizeQuota(null)
      };
    }
    return {
      valid: true,
      id: rawId,
      key: key,
      name: stringValue(raw.name).trim() || ENDPOINTS[key].name,
      quota: normalizeQuota(raw.quota)
    };
  }

  function statusMeta(status) {
    return hasOwn(STATUS_META, status) ? STATUS_META[status] : STATUS_META.unknown;
  }

  function formatNumber(value) {
    if (value === null || value === undefined || !isFinite(value)) return '';
    if (value === 0) return '0';
    return new Intl.NumberFormat('zh-CN',{maximumFractionDigits:2}).format(value);
  }

  function formatPercent(value) {
    if (value === null || !isFinite(value)) return '';
    if (value >= 100) return '100';
    /* Truncate rather than round up: 99.999% must not look like a full bar. */
    return String(Math.floor(value * 100) / 100);
  }

  function formatTimestamp(seconds) {
    if (seconds === null) return null;
    var milliseconds = seconds * 1000;
    var date = new Date(milliseconds);
    if (isNaN(date.getTime())) return null;

    function pad(value) {
      return value < 10 ? '0' + value : String(value);
    }

    var display = date.getFullYear() + '-' + pad(date.getMonth() + 1) + '-' +
      pad(date.getDate()) + ' ' + pad(date.getHours()) + ':' +
      pad(date.getMinutes()) + ':' + pad(date.getSeconds()) + '（本地时间）';
    return { iso: date.toISOString(), display: display };
  }

  function renderTime(seconds) {
    var formatted = formatTimestamp(seconds);
    if (!formatted) {
      return '<span class="native-quota__unknown">未知</span>';
    }
    return '<time class="native-quota__time" datetime="' +
      escapeHtml(formatted.iso) + '">' + escapeHtml(formatted.display) + '</time>';
  }

  function renderBadge(status) {
    var meta = statusMeta(status);
    return '<span class="native-quota__badge native-quota__badge--' +
      escapeHtml(meta.tone) + '">' + escapeHtml(meta.label) + '</span>';
  }

  function refreshAccess(endpoint) {
    var quota = endpoint.quota;
    if (quota.status === 'unsupported' || quota.capability === 'unsupported') {
      return { allowed: false, reason: '该来源不支持原生额度刷新。' };
    }
    if (quota.capability === 'unknown') {
      return { allowed: false, reason: '来源能力未知，无法确认支持原生额度刷新。' };
    }
    if (quota.status === 'not_connected') {
      return { allowed: false, reason: '执行端尚未连接，当前无法刷新额度。' };
    }
    if (quota.canRefresh !== true) {
      return { allowed: false, reason: '该来源当前未提供额度刷新能力。' };
    }
    return { allowed: true, reason: '' };
  }

  function isBusy(busy, endpoint) {
    if (busy === true) return true;
    if (!isObject(busy)) return false;
    return busy[endpoint.id] === true || busy[endpoint.key] === true ||
      busy.all === true;
  }

  function renderRefreshButton(endpoint, access, busy) {
    var disabled = !access.allowed || busy;
    var buttonLabel = (busy ? '正在刷新 ' : '刷新 ') + endpoint.name + ' 额度';
    var hint = busy ? '正在刷新额度，请稍候。' :
      (access.allowed ? '刷新将重新读取当前来源的额度。' : access.reason);
    return '<div class="native-quota__actions">' +
      '<button type="button" class="native-quota__refresh" data-quota-refresh="' +
      escapeHtml(endpoint.id) + '"' + (disabled ? ' disabled' : '') +
      (busy ? ' aria-busy="true"' : '') +
      ' aria-label="' + escapeHtml(buttonLabel) + '">' +
      escapeHtml(busy ? '刷新中…' : '刷新额度') + '</button>' +
      '<span class="native-quota__action-hint">' + escapeHtml(hint) + '</span>' +
      '</div>';
  }

  function renderSource(endpoint) {
    var quota = endpoint.quota;
    var label = quota.sourceLabel || '来源未标注';
    var href = safeSourceUrl(quota.sourceUrl);
    var link = '';
    if (href) {
      link = ' <a class="native-quota__source-link" href="' + escapeHtml(href) +
        '" target="_blank" rel="noopener noreferrer" aria-label="' +
        escapeHtml('在新标签页打开 ' + endpoint.name + ' 官方来源') +
        '">查看来源</a>';
    }
    return '<p class="native-quota__source">来源：<span class="native-quota__source-label">' +
      escapeHtml(label) + '</span>' + link + '</p>';
  }

  function renderNotices(quota) {
    var meta = statusMeta(quota.status);
    var items = [];
    var notices = [];

    if (meta.notice) items.push(meta.notice);
    if (quota.error) items.push('错误详情：' + quota.error);
    if (quota.message && quota.message !== quota.error &&
        quota.message !== meta.notice) {
      items.push(quota.message);
    }

    for (var index = 0; index < items.length; index += 1) {
      if (items.indexOf(items[index]) !== index) continue;
      var modifier = quota.status === 'error' ? 'error' :
        (quota.status === 'stale' ? 'warning' : 'info');
      notices.push('<p class="native-quota__notice native-quota__notice--' +
        modifier + '">' + escapeHtml(items[index]) + '</p>');
    }

    return notices.length ?
      '<div class="native-quota__notices">' + notices.join('') + '</div>' : '';
  }

  function renderPercent(limit) {
    if (limit.percent !== null) {
      return '剩余 <strong>' + escapeHtml(formatPercent(limit.percent)) + '%</strong>';
    }
    if (limit.zeroTotal) {
      return '百分比不可用（总量为 0）';
    }
    return '剩余百分比未知';
  }

  function renderProgress(limit) {
    if (limit.percent === null) {
      return '<span class="native-quota__progress native-quota__progress--unknown" aria-hidden="true"></span>';
    }
    var percent = escapeHtml(formatPercent(limit.percent));
    return '<progress class="native-quota__progress'+(limit.percent<=10?' native-quota__progress--low':limit.percent<=30?' native-quota__progress--warning':'')+'" max="100" value="' + percent +
      '" aria-label="' + escapeHtml(limit.label + ' 剩余百分比') +
      '" aria-valuetext="' + escapeHtml('剩余 ' + percent + '%') + '">' +
      percent + '%</progress>';
  }

  function numberNode(value) {
    if (value === null) {
      return '<span class="native-quota__unknown">未知</span>';
    }
    return '<strong class="native-quota__number">' +
      escapeHtml(formatNumber(value)) + '</strong>';
  }

  function renderReset(limit) {
    var time = formatTimestamp(limit.resetAt);
    var parts = [];
    if (limit.resetLabel) {
      parts.push(escapeHtml(limit.resetLabel) + (time ? '' : '（时间未知）'));
    }
    if (time) {
      parts.push('<time class="native-quota__time" datetime="' +
        escapeHtml(time.iso) + '">' + escapeHtml(time.display) + '</time>');
    }
    if (!parts.length) parts.push('<span class="native-quota__unknown">未提供</span>');
    return '<p class="native-quota__reset">重置/到期：' + parts.join(' · ') + '</p>';
  }

  function renderLimits(quota) {
    if (!quota.limits.length) {
      return '<p class="native-quota__empty">未提供额度明细。</p>';
    }

    var items = [];
    for (var index = 0; index < quota.limits.length; index += 1) {
      var limit = quota.limits[index];
      var unit = limit.unit ? ' <span class="native-quota__unit">' +
        escapeHtml(limit.unit) + '</span>' : '';
      var used = '';
      if (limit.used !== null) {
        used = '<span class="native-quota__used">已用 ' + numberNode(limit.used) +
          unit + '</span>';
      }
      items.push('<li class="native-quota__limit">' +
        '<div class="native-quota__limit-head">' +
        '<span class="native-quota__limit-label">' + escapeHtml(limit.label) + '</span>' +
        '<span class="native-quota__percent' +
        (limit.percent === null ? ' native-quota__percent--unknown' : '') + '">' +
        renderPercent(limit) + '</span></div>' +
        renderProgress(limit) +
        '<div class="native-quota__amounts">' +
        '<span class="native-quota__remaining">剩余 ' + numberNode(limit.remaining) +
        '</span><span class="native-quota__separator"> / </span>' +
        '<span class="native-quota__total">总量 ' + numberNode(limit.total) +
        '</span>' + unit + '</div>' + used + renderReset(limit) + '</li>');
    }

    return '<div class="native-quota__limits">' +
      '<h4 class="native-quota__section-title">额度明细</h4>' +
      '<ul class="native-quota__limit-list">' + items.join('') + '</ul></div>';
  }

  function renderSyncSummary(endpoint, access) {
    var sync = endpoint.quota.sync;
    if (!access.allowed) return '自动同步：不可用';
    if (!sync.enabled) return '自动同步：未开启';
    if (sync.intervalMismatch) return '自动同步：已开启（间隔需要重新设置）';
    return '自动同步：已开启（每 ' + sync.interval + ' 秒，仅工作台可见时执行）';
  }

  function render(endpoint, options) {
    var normalized = normalize(endpoint);
    if (!normalized.valid) {
      return '<section class="native-quota native-quota--invalid" ' +
        'aria-label="原生账户额度">' +
        '<p class="native-quota__empty">无法识别该执行端，未显示任何额度信息。</p>' +
        '</section>';
    }

    var opts = isObject(options) ? options : {};
    var quota = normalized.quota;
    var meta = statusMeta(quota.status);
    var access = refreshAccess(normalized);
    var busy = isBusy(opts.busy, normalized);

    return '<section class="native-quota native-quota--' + escapeHtml(meta.tone) +
      '" data-quota-endpoint="' + escapeHtml(normalized.id) +
      '" data-quota-status="' + escapeHtml(quota.status) +
      '" data-quota-capability="' + escapeHtml(quota.capability) +
      '" aria-label="' + escapeHtml(normalized.name + ' 原生账户额度') + '">' +
      '<header class="native-quota__header">' +
      '<div class="native-quota__heading">' +
      '<h3 class="native-quota__title">' + escapeHtml(normalized.name) + ' 原生额度</h3>' +
      renderSource(normalized) + '</div>' + renderBadge(quota.status) + '</header>' +
      '<dl class="native-quota__meta">' +
      '<div class="native-quota__meta-item"><dt>更新时间</dt><dd>' +
      renderTime(quota.fetchedAt) + '</dd></div>' +
      '<div class="native-quota__meta-item"><dt>最近尝试</dt><dd>' +
      renderTime(quota.lastAttemptAt) + '</dd></div></dl>' +
      renderNotices(quota) + renderLimits(quota) +
      '<footer class="native-quota__footer">' +
      '<p class="native-quota__sync-state">' +
      escapeHtml(renderSyncSummary(normalized, access)) + '</p>' +
      renderRefreshButton(normalized, access, busy) + '</footer></section>';
  }

  function renderSettings(endpoint) {
    var normalized = normalize(endpoint);
    if (!normalized.valid) {
      return '<section class="native-quota-settings native-quota-settings--invalid" ' +
        'aria-label="原生额度同步设置">' +
        '<p class="native-quota__empty">无法识别该执行端，无法配置额度同步。</p>' +
        '</section>';
    }

    var options = arguments.length > 1 && isObject(arguments[1]) ? arguments[1] : {};
    var quota = normalized.quota;
    var sync = quota.sync;
    var access = refreshAccess(normalized);
    var busy = isBusy(options.busy, normalized);
    var checked = access.allowed && sync.enabled;
    var autoState = !access.allowed ? '自动同步不可用' :
      (sync.enabled ? '自动同步已开启' : '自动同步已关闭');

    var intervalOptions = [];
    for (var index = 0; index < INTERVAL_OPTIONS.length; index += 1) {
      var seconds = INTERVAL_OPTIONS[index];
      var intervalLabel = seconds === 60 ? '60 秒（1 分钟）' :
        (seconds === 180 ? '180 秒（3 分钟）' :
        (seconds === 300 ? '300 秒（5 分钟）' : '900 秒（15 分钟）'));
      intervalOptions.push('<option value="' + seconds + '"' +
        (sync.interval === seconds ? ' selected' : '') + '>' +
        escapeHtml(intervalLabel) + '</option>');
    }

    var disabledNote = '';
    if (!access.allowed) {
      disabledNote = '<p class="native-quota-settings__warning">设置已禁用：' +
        escapeHtml(access.reason) + '</p>';
      if (sync.enabled) {
        disabledNote += '<p class="native-quota-settings__warning">' +
          '现有记录为已开启自动同步，但该来源当前不可刷新，自动同步不会生效。</p>';
      }
    }
    if (sync.intervalMismatch) {
      disabledNote += '<p class="native-quota-settings__warning">' +
        '现有同步间隔不在可选范围内，下拉框暂显示 180 秒；请重新选择。</p>';
    }

    return '<section class="native-quota-settings" data-quota-settings="' +
      escapeHtml(normalized.id) + '" aria-label="' +
      escapeHtml(normalized.name + ' 额度同步设置') + '">' +
      '<header class="native-quota-settings__header">' +
      '<div><h3 class="native-quota-settings__title">额度与同步</h3>' +
      '<p class="native-quota-settings__endpoint">执行端：<strong>' +
      escapeHtml(normalized.name) + '</strong></p></div>' +
      '<span class="native-quota-settings__state">' +
      escapeHtml(autoState) + '</span></header>' + renderSource(normalized) +
      '<div class="native-quota-settings__quota">' +
      '<p class="native-quota-settings__status">当前额度状态：' +
      renderBadge(quota.status) + '<span class="native-quota-settings__updated">更新时间：' +
      renderTime(quota.fetchedAt) + '</span></p>' +
      renderNotices(quota) + renderLimits(quota) + '</div>' +
      '<div class="native-quota-settings__controls">' +
      '<div class="native-quota-settings__row">' +
      '<label class="native-quota-settings__option">' +
      '<input type="checkbox" class="native-quota-settings__checkbox" ' +
      'data-quota-auto="' + escapeHtml(normalized.id) + '" aria-label="' +
      escapeHtml('为 ' + normalized.name + ' 启用自动额度同步') + '"' +
      (checked ? ' checked' : '') + (access.allowed ? '' : ' disabled') + '>' +
      '<span>自动同步额度</span></label></div>' +
      '<div class="native-quota-settings__row">' +
      '<label class="native-quota-settings__option native-quota-settings__option--interval">' +
      '<span>同步间隔</span><select class="native-quota-settings__select" ' +
      'data-quota-interval="' + escapeHtml(normalized.id) + '" aria-label="' +
      escapeHtml(normalized.name + ' 自动额度同步间隔') + '"' +
      (access.allowed ? '' : ' disabled') + '>' + intervalOptions.join('') +
      '</select></label></div>' +
      '<p class="native-quota-settings__note">自动同步仅在模型工作台页面可见时执行；' +
      '页面隐藏或关闭后不会在后台请求额度。</p>' + disabledNote + '</div>' +
      '<footer class="native-quota-settings__footer">' +
      renderRefreshButton(normalized, access, busy) + '</footer></section>';
  }

  var api = {
    render: render,
    renderSettings: renderSettings,
    normalize: normalize
  };

  if (typeof Object.freeze === 'function') Object.freeze(api);
  global.ModelDeskNativeQuota = api;
})(typeof globalThis !== 'undefined' ? globalThis :
  (typeof window !== 'undefined' ? window : this));
