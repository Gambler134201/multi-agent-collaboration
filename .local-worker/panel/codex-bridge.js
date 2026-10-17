// MCP Apps host bridge for the same workbench UI. No browser-to-loopback requests.
(() => {
  'use strict';
  window.modelDeskHost = true;
  const pending = new Map();
  let nextId = 1;
  // Per-instance host context. The opener resolves host threadId to directory
  // metadata and delivers the normalized project in its tool result.
  // Only the first trusted project binds this instance, and a manual choice in
  // the panel permanently outranks any later (possibly late or stale) event.
  let hostContextView = null;
  let hostContextLocked = false;
  function trustedProject(view) {
    if (!view || view.mode !== 'project') return null;
    if (typeof view.project !== 'string' || !view.project) return null;
    if (view.project.includes('://') || view.project.includes('\0')) return null;
    if (!/^(?:[a-z]:[\\/]|\/|\\\\[^\\]+\\)/i.test(view.project)) return null;
    return {mode:'project', project:view.project};
  }
  function projectContext(view) {
    if (hostContextLocked || hostContextView) return;
    const context = trustedProject(view);
    if (!context) return;
    hostContextView = context;
    window.modelDeskInitialView = context;
    window.dispatchEvent(new CustomEvent('modeldesk-project-context', {detail:context}));
  }
  // Called by app.js after the user picks a project; host events must not rebind.
  window.modelDeskLockProjectContext = () => { hostContextLocked = true; };
  function notify(method, params) {
    window.parent.postMessage({jsonrpc:'2.0', method, params}, '*');
  }
  function request(method, params) {
    if (window.parent === window) return Promise.reject(new Error('请从 Codex 的模型工作台入口打开。'));
    const id = nextId++;
    return new Promise((resolve, reject) => {
      const timeout = setTimeout(() => {
        pending.delete(id);
        reject(new Error('Codex 插件连接超时，请确认模型工作台已启用。'));
      }, 45000);
      pending.set(id, {resolve, reject, timeout});
      window.parent.postMessage({jsonrpc:'2.0', id, method, params}, '*');
    });
  }
  window.addEventListener('message', event => {
    if (event.source !== window.parent) return;
    const message = event.data;
    if (!message || message.jsonrpc !== '2.0') return;
    if (message.method==='ui/notifications/tool-input' && message.params?.arguments?.project)
      projectContext({mode:'project',project:message.params.arguments.project});
    if (message.method==='ui/notifications/tool-result') {
      const result=message.params?.structuredContent||message.params?.result?.structuredContent;
      if(result?.view) projectContext(result.view);
    }
    if (message.id !== undefined && pending.has(message.id)) {
      const item = pending.get(message.id);
      pending.delete(message.id);
      clearTimeout(item.timeout);
      if (message.error) item.reject(new Error(message.error.message || 'Codex 插件请求失败。'));
      else item.resolve(message.result);
    }
  });
  // The ui/initialize result carries the documented MCP Apps host context only
  // (theme, display mode, tool info, ...); it has no project field, so nothing
  // is bound from it.
  const ready = request('ui/initialize', {
    appInfo:{name:'model-desk', version:'1.0.0'},
    appCapabilities:{}, protocolVersion:'2025-11-21'
  }).then(result => {
    notify('ui/notifications/initialized', {});
    return result;
  });
  // The normal panel's api() function continues to handle errors and user interactions.
  window.fetch = async (path, options = {}) => {
    if (typeof path !== 'string' || !path.startsWith('/api/')) throw new Error('插件仅支持工作台接口。');
    await ready;
    const method = options.method || 'GET';
    const body = options.body === undefined ? undefined : JSON.parse(options.body);
    const encoded=options.headers?.['X-Model-Desk-View'];
    const view=encoded?JSON.parse(decodeURIComponent(escape(atob(encoded.replace(/-/g,'+').replace(/_/g,'/'))))):{mode:'unbound',project:null};
    const result = await request('tools/call', {
      name:'panel_request', arguments:{path, method, view, ...(body === undefined ? {} : {body})}
    });
    if (result?.isError) {
      throw new Error(result.content?.find(item => item.type === 'text')?.text || '工作台请求失败。');
    }
    let payload = result?.structuredContent;
    if (!payload) {
      try { payload = JSON.parse(result.content.find(item => item.type === 'text').text); } catch {}
    }
    if (!payload || !Number.isInteger(payload.status)) throw new Error('工作台返回了无效响应。');
    return new Response(JSON.stringify(payload.data), {
      status:payload.status, headers:{'Content-Type':'application/json; charset=utf-8'}
    });
  };
  document.addEventListener('click', event => {
    const anchor = event.target.closest('a[href]');
    if (!anchor || !/^https?:/.test(anchor.href)) return;
    event.preventDefault();
    ready.then(() => request('ui/open-link', {url:anchor.href})).catch(error => {
      if (typeof toast === 'function') toast(error.message, true);
    });
  });
  window.addEventListener('pagehide', () => {
    for (const item of pending.values()) {
      clearTimeout(item.timeout);
      item.reject(new Error('工作台面板已关闭。'));
    }
    pending.clear();
  });
})();
