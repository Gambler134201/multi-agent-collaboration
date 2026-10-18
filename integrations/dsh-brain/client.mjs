window.__ModuleLoader__.load({
  id: 'model-desk-dsh-brain',
  factory(require) {
    const React = require('react');

    const TYPE_ID = 'model-desk-dsh-brain/workbench';
    const KIND = 'model-desk-workbench';
    const TITLE = '模型工作台';
    const DESCRIPTION = '按当前原生会话的项目打开本机模型工作台';
    const HOST_CONTEXT = 'dsh';
    const FALLBACK_PANEL_ORIGIN = 'http://127.0.0.1:8765';

    // The desktop installer replaces this quoted marker while preparing the
    // package. A source checkout or a manually installed package keeps the
    // loopback default instead of inventing a service directory or port.
    const PANEL_ORIGIN = (() => {
      const configured = "__MODEL_DESK_PANEL_ORIGIN__";
      return /^http:\/\/(?:127\.0\.0\.1|localhost|\[::1\])(?::[0-9]{1,5})?$/.test(configured)
        ? configured
        : FALLBACK_PANEL_ORIGIN;
    })();

    function originUrl() {
      const url = new URL('/', PANEL_ORIGIN);
      if (url.protocol !== 'http:') throw new Error('panel origin must be loopback HTTP');
      if (url.username || url.password) throw new Error('panel origin must not carry credentials');
      return url;
    }

    function isAbsoluteProjectPath(value) {
      if (typeof value !== 'string' || value.length === 0) return false;
      if (/[\u0000-\u001f]/.test(value)) return false;
      if (/^[A-Za-z]:[\\/]/.test(value)) return true;
      if (/^\\\\[^\\]+\\[^\\]+/.test(value)) return true;
      // This page addresses a loopback workbench on the same desktop. A
      // Windows browser must not promote a current-drive-rooted /work into a
      // fully qualified project. POSIX clients keep their native root form.
      const windows = typeof navigator !== 'undefined' && /^Win/i.test(navigator.platform || '');
      return !windows && value.startsWith('/');
    }

    function buildWorkbenchUrl(project) {
      const url = originUrl();
      // Display-only host context: it tells the already-running workbench to
      // render the official DSH login-account balance instead of the Codex/GPT
      // quota. It is not an identity, capability or authorization credential.
      url.searchParams.set('host', HOST_CONTEXT);
      if (project !== undefined) url.searchParams.set('project', project);
      else url.searchParams.set('view', 'unbound');
      return url.toString();
    }

    // Pure identity decision used by the tab body and its offline checks. The
    // only accepted project is the current native session header's absolute
    // cwd; a missing, relative or still-loading row never falls back to a
    // service cwd, a recent project or another session.
    function resolveWorkbenchTarget(row, sessionId, phase) {
      if (typeof sessionId !== 'string' || sessionId.length === 0) {
        return {
          status: 'unknown',
          message: '当前没有可绑定的原生会话，未选择项目。',
        };
      }
      if (row === undefined || row === null) {
        if (phase === 'pending') {
          return {
            status: 'pending',
            message: '正在读取当前原生会话的工作区…',
          };
        }
        return {
          status: 'unknown',
          message: '当前原生会话不在可见会话列表中，未选择项目。',
        };
      }
      if (!isAbsoluteProjectPath(row.cwd)) {
        return {
          status: 'unbound',
          message: '当前原生会话没有可信的绝对工作区目录，未选择项目。',
        };
      }
      let url;
      try {
        url = buildWorkbenchUrl(row.cwd);
      } catch (_error) {
        return {
          status: 'error',
          message: '模型工作台的本机地址配置无效，未打开页面。',
        };
      }
      return {
        status: 'ready',
        project: row.cwd,
        url,
        message: '正在打开模型工作台…',
      };
    }

    function WorkbenchBody(props) {
      const { sessionId, useSessionList, useTabInfo } = props;
      const { tab } = useTabInfo();
      const row = useSessionList((snapshot) => (
        snapshot && snapshot.byId ? snapshot.byId[sessionId] : undefined
      ));
      const phase = useSessionList((snapshot) => (snapshot && snapshot.phase) || 'pending');
      const target = resolveWorkbenchTarget(row, sessionId, phase);
      const opened = React.useRef(false);
      const [failure, setFailure] = React.useState(null);

      const open = (url) => {
        try {
          // Replace this transient page with the native browser tab. The tab
          // action is bound to this tab's own session, so a background session
          // cannot borrow the on-screen session's project.
          tab.actions.openTab('browser', {
            params: { url },
            replaceTab: true,
          });
          return true;
        } catch (_error) {
          setFailure('无法打开原生右侧栏浏览器。请确认 DSH 已启用浏览器侧栏且当前会话可用后重试。');
          return false;
        }
      };

      React.useEffect(() => {
        if (opened.current || target.status !== 'ready') return;
        opened.current = true;
        open(target.url);
      }, [target.status, target.url, tab.actions]);

      const style = {
        boxSizing: 'border-box',
        color: 'var(--dsw-alias-label-secondary)',
        font: 'var(--dsw-font-xs-13)',
        lineHeight: 1.6,
        padding: '20px 24px',
      };
      const buttonStyle = {
        color: 'var(--dsw-alias-label-primary)',
        background: 'var(--dsw-alias-bg-layer-1)',
        border: '0.5px solid var(--dsw-alias-border-l2)',
        borderRadius: 'var(--dsw-radius-sm)',
        cursor: 'pointer',
        font: 'inherit',
        marginTop: '12px',
        padding: '5px 10px',
      };

      if (failure !== null) {
        const retryUrl = target.status === 'ready' ? target.url : buildWorkbenchUrl();
        return React.createElement(
          'div',
          { style },
          React.createElement('p', { style: { margin: 0 } }, failure),
          React.createElement(
            'button',
            {
              type: 'button',
              style: buttonStyle,
              onClick: () => {
                setFailure(null);
                open(retryUrl);
              },
            },
            '重试打开',
          ),
        );
      }
      if (target.status === 'error') {
        return React.createElement('div', { style }, target.message);
      }
      if (target.status === 'ready' || target.status === 'pending') {
        return React.createElement('div', { style }, target.message);
      }
      return React.createElement(
        'div',
        { style },
        React.createElement('p', { style: { margin: 0 } }, target.message),
        React.createElement(
          'button',
          {
            type: 'button',
            style: buttonStyle,
            onClick: () => {
              open(buildWorkbenchUrl());
            },
          },
          '打开未选择项目的工作台',
        ),
      );
    }

    function apply(ctx) {
      ctx.effect(() => ctx.sidebarRightTabs.register({
        id: TYPE_ID,
        kind: KIND,
        title: () => TITLE,
        guide: [{
          id: 'open',
          order: 20,
          title: () => TITLE,
          description: () => DESCRIPTION,
        }],
      }), 'model-desk-dsh-brain: sidebar workbench type');

      ctx.effect(() => ctx.slots.inject('sidebar.right.pane.tab', () => ctx.slots.register({
        name: 'sidebar.right.pane.tab',
        key: TYPE_ID,
        inject: (sessionId) => ({
          sessionId,
          hooks: { sessionList: ctx.sessions.list },
        }),
      }, WorkbenchBody)), 'model-desk-dsh-brain: sidebar workbench body');
    }

    return {
      inject: ['slots', 'sessions', 'sidebarRightTabs'],
      apply,
      _test: Object.freeze({
        TYPE_ID,
        KIND,
        TITLE,
        HOST_CONTEXT,
        PANEL_ORIGIN,
        isAbsoluteProjectPath,
        buildWorkbenchUrl,
        resolveWorkbenchTarget,
        WorkbenchBody,
      }),
    };
  },
});
