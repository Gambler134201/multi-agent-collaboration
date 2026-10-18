# DSH 主脑插件：入口决定与实现记录

更新日期：2026-10-18。开发分支：`codex/brain-dsh-sidebar`。插件加载与项目/会话绑定基础已由主脑接手修正，并经离线检查及安装版 DSH 临时 ACP overlay 验证。原生桌面 GUI 安装/入口、真实模型规则遵循和自动唤醒仍未实测。

## 用户确认的使用方式

用户希望像当前 Codex 一样，在 DSH 原生软件里正常聊天，打开协作开关后自动使用框架。因此选择**原生插件**作为目标交付形式；专用 ACP 脚本用于开发验证，不作为日常聊天入口。

期望流程：一次安装插件 → 在 DSH 原生聊天选择项目及主脑模型 → 打开协作开关 → 插件绑定当前项目与聊天并加载协作规则 → 主脑按需分派 → 后台等待 → 交付后回到所属聊天独立验收。

不要求用户每次复制项目路径、手动运行派发命令或先说固定口令。项目或聊天身份缺失时应明确显示未绑定，不能猜测安装目录或最近项目。主脑模型由 DSH 原生选择，执行模型与各自的执行 Agent 继续由工作台管理。

## 当前实际验证范围

| 检查 | 结果 | 实际含义 |
| --- | --- | --- |
| 已安装 DSH 文件版本 | ProductVersion `0.2.1.0`；FileVersion `0.2.1-rc.2` | 安装文件身份；不等同于模型版本 |
| ACP initialize | 协议版本 1；Agent 名 `deepseek-harness-acp`，协议组件版本 `0.0.1` | 与桌面产品版本分别记录 |
| ACP 独立空会话 new/close | 通过 | 仅操作探针自己创建的临时项目会话 |
| 真实 DSH 挂载通用主脑 MCP | 成功发现九个工具，tools/list 一次，工具调用零次 | 证明安装版本可发现真实 stdio MCP 工具，未连接工作台或派发 |
| 模型轮次 | 未提交 session/prompt | 此检查没有执行模型推理；不据此保证启动过程没有其他网络请求 |
| 安装包静态扩展点 | 包含插件管理、串行 agent/created、Agent 作用域、工作区绑定及 MCP 模块 | 存在原生适配基础；静态存在不等于 GUI 安装/加载验收 |
| 本轮 DSH 原生插件代码 | 离线边界检查及安装版临时 ACP overlay 通过 | 两个临时原生会话分别挂载九工具，项目与会话一致，规则装配生效；未安装到真实 desktop profile |
| DSH 原生桌面开关、自动绑定、自动唤醒 | 尚未实测 | 不能以离线测试或 ACP 工具发现替代 |

探针只关闭自身会话和进程，未提交模型提示、读取账号密钥、改动真实 DSH 配置或部署运行版。探针脚本、厂商源码摘录及本机结果保留在忽略的本机任务目录，不随源码发布。

## 本轮实现（薄适配）

新增可选插件组合包 `integrations/dsh-brain/`：

- `package.json`：`dsh.bundle.patch = cordis.patch.yml`，预构建 CommonJS 入口 `index.cjs`，不含运行依赖；`@deepseek-ai/dsh-mcp-client` 作为可选 peer，从已安装的 DSH profile 动态解析，不硬编码厂商安装路径。
- `cordis.patch.yml`：只插入一个 `model-desk-dsh-brain` 条目，`configFile` 默认普通空值，由插件代码读取 `MODEL_DESK_DSH_BRAIN_CONFIG`。原 `!!js` 环境表达式在本机探针中未生效，已修正并重测；未配置时保持 inert。不添加全局项目绑定，不修改模型默认 Agent、preset、预算、次数或 runtime。
- `index.cjs`：插件主体。每个顶层原生 Agent 使用自己的 `agent.ctx` 挂载一个 stdio MCP client（默认 `serverName=model_desk_brain`），不共用全局 MCP 配置。

新增本机配置生成器 `.local-worker/dsh_brain_plugin.py`：只接受显式绝对路径，只创建用户指定的一个输出文件，已存在时拒绝覆盖；不写 DSH profile、workflow 设置或项目记录。生成内容不含 project/owner/session/role/host，项目与会话只能在运行时从原生聊天取得。

### 原生身份与首次绑定

- 只处理 `ctx.agents.roots()` 中与事件 `agent` **实例相等**的顶层 Agent；不用进程、cwd、服务目录或“最近项目”判断主脑。
- 可信身份要求 `agent.id === agent.session.id === agent.session.header.id`，且 `agent.session.header.cwd` 是已存在绝对目录的 realpath。任一项缺失、相对、不可解析或不是目录都不绑定，不猜。
- 首次绑定后，`agent/session.header.cwd`、id 或顶层身份漂移只令绑定失效，不自动改归属。不同原生会话分别绑定、分别使用自己的 owner/operator 摘要。
- owner 由 `ownerPrefix + SHA-256(native session id)` 稳定派生；`operator_session` 由同一原生 session id 的稳定摘要派生。原生 session id 作为 `--native-session-id` 声明保留。
- 身份状态如实记为 `user_bound`（用户安装插件并同意自动绑定当前聊天），不是 `host_verified`；不声称宿主已核实。主脑模型/宿主版本只记录实际可声明的来源，本轮启动参数不填模型和版本，避免猜测。
- 首次绑定记录只保存在当前插件模块的内存中，用于重复 apply/同一模块内热重载/恢复时不改归属；不写入 `.model-desk` 或宿主 profile。DSH 进程重启或插件模块被重新加载后，记录会重新建立；若那时原生 header 已指向其他项目，插件只能按当前原生 header 重新评估，不能阻止宿主层面的会话迁移。

### 工具、开关与作用域

- 每个 Agent 单独 `agent.ctx.plugin(mcpClientModule, config)`；`serverName` 在同一 Agent 作用域内唯一。并发 `agent/created` 以会话 id 去重，`agent/pre-step` 不挂载，插件重载先 dispose 旧实例，避免重复挂载。
- MCP 启动参数只由原生绑定生成：`--project <realpath>`、`--host dsh`、`--owner <derived>`、`--role brain`、`--session-binding user_bound`、`--session-id <derived>`、`--native-session-id <native>`、`--panel-url <explicit>`。工具参数不能改项目、角色、owner 或会话。
- MCP client 的 `cwd` 使用该原生项目；`brain_connect` 才显式连接已经运行的工作台。插件不会自动启动工作台，也不会在工具发现阶段访问工作台或调用模型。
- Agent 作用域工具会被子 scope 继承，因此 `agent.ctx.tools.guard` 做最终单调拒绝：只处理本 server 的 `brain_*` 工具，要求 `exec.agent` **实例等于**所属主脑 Agent；子 Agent、其他会话、缺失 agent、身份漂移或未知宿主 API 都拒绝。不注册全局工具屏蔽，DSH 正常工具不受影响。
- `brain_dispatch` 与 `brain_rework` 在 guard 内每次重新执行显式配置的 `workflow_settings.py --status`；缺失、不可读、非法、超时或关闭都按 OFF，不使用启动时缓存的允许值。现有 `brain_gateway` 后端仍会在派发/整改效果前再次校验。
- 开关关闭不取消已有任务、不改绑身份；`brain_status`/`brain_delivery`/`brain_review` 等读取与验收能力保留，仅拒绝新的派发/整改。插件在 pre-step 每轮开始时刷新规则可见性，guard 在每次变更前独立校验。
- 规则通过 Agent scope 的 `systemPrompt.section` 注册，并按当前 assembly 的 `context.agent` 过滤；子 Agent 看到空规则。规则明确：主脑判断风险与验收、执行端调查实现测试修正、候选/自报非独立证据、`completed` 不等于 `accepted`、最多一次整改、重复/未知保持原 request/task、选择 `model_id` 遵循已保存 Agent、关闭不取消已有任务。
- 当前未接通自动唤醒/后台续接，规则明确不承诺“完成会自动回来”，也不要求周期性轮询主脑模型；没有伪造无额度消耗等待。
- `MODEL_DESK_*EXECUTOR*` 环境标记存在时，插件不安装规则/工具；运行期出现该标记时 guard 和 pre-step 也全部拒绝主脑能力。
- 同时识别现有执行端实际设置的 `MODEL_DESK_RECORD_PROOF`、`MODEL_DESK_PRESET_PROOF`、`MODEL_DESK_SUBAGENT_LEDGER`；不能只检查名字带 EXECUTOR 的假设标记。
- 直接挂载 MCP 前调用安装版的 `Config` 补齐并校验参数；规则注册失败拒绝新派发。卸载等待 Cordis `inertia`，挂载中途卸载也回收迟到的连接；无法确认关闭时保留失效守卫并禁止重复加载。

## 安装配置（用户显式操作，不由本仓库执行）

1. 先显式启动已运行的模型工作台；插件不会自动启动它。
2. 把 `integrations/dsh-brain` 作为可选 bundle 安装到 **DSH desktop（主脑）profile**，不要安装到 DSH/Pi 执行器 profile。安装机制以你的 DSH 版本为准；本仓库未修改真实 profile。
3. 生成显式本机配置（路径占位，必须替换成自己的绝对路径）：

```powershell
<python> .local-worker\dsh_brain_plugin.py `
  --python "<venv>\Scripts\python.exe" `
  --brain-mcp-server "<安装目录>\.local-worker\brain_mcp_server.py" `
  --workflow-settings "<安装目录>\.local-worker\workflow_settings.py" `
  --output "<配置目录>\dsh-brain.json"
```

4. 设置 `MODEL_DESK_DSH_BRAIN_CONFIG` 指向该 JSON 后重启 DSH；或在 desktop profile 的 patch 中按 `model-desk-dsh-brain` id 覆盖 `config.configFile`。两种方式都没有时插件 fail closed（无规则、无工具）。
5. `--python`、`--brain-mcp-server` 与 `--workflow-settings` 应来自同一个工作台安装；插件 guard 与服务端 `brain_gateway` 必须看到同一套总开关。
6. 生成器拒绝覆盖已有文件，也不会创建父目录；输出不能位于 `.model-desk`。

## 离线验证命令与结果

```powershell
node --test .local-worker\tests\test_dsh_brain_plugin.cjs
node --test .local-worker\tests\test_dsh_brain_sidebar.cjs
<python> .local-worker\tests\test_dsh_brain_plugin.py
<python> .local-worker\tests\test_dsh_brain_desktop.py
```

执行器在模型接口连接重置后中断，原任务保留 failed；主脑接手检查并修正，未自动重派或伪记为执行模型完成。独立结果如下：

- Node 内置测试：18 项通过。除原有边界外，主脑先复现真实执行器标记漏判、规则失败仍能派发、迟到挂载泄漏、未等待 Cordis 清理的四项问题，再修正；补验关闭失败保留守卫和 Windows 当前盘根路径拒绝。
- Python 配置生成器：12 项中 11 项通过，目录软链接检查因本机无创建权限跳过 1 项；实际及链接目标均校验 `.model-desk` 路径。不能将该跳过项写为已实测通过。
- 通用主脑核心 56 项、现有 Codex MCP 37 项、可移植安装 19 项全部通过。本轮未改现有核心，合计定向检查 142 项，141 通过、1 跳过；不是全项目回归。
- 上述回归只用 mock、临时项目及临时状态脚本，不启动真实 DSH/工作台、不调用模型。
- 独立真实接口探针：安装版 DSH + 临时 ACP overlay 加载实际插件和真实 MCP 服务。两个临时项目/原生会话各有九工具，`brain_describe` 的项目/会话匹配，owner 和责任绑定不同，规则装配生效；借原生作用域链模拟继承者调用验收被拒绝。临时开关关闭拒绝派发，读取/验收仍可用。仅调用 `initialize`、两次 `session/new`、两次 `session/close` 和每会话一次本地 `brain_describe`；没有 `session/prompt`、`brain_connect`、派发或验收效果调用，也未修改真实宿主配置。作用域继承探针不是实际子 Agent 模型轮次。

本轮未重复全项目 `unittest discover`：只新增可选适配器和生成器，未修改既有 Python 核心；按改动运行上述独立定向检查。通用核心上轮全量回归的两项既有 DSH 环境缺件仍不能称为全绿。

## 仍未验证 / 明确边界

- 原生 GUI 插件管理和实际桌面聊天仍需验证。包的官方 CLI 安装/卸载与环境配置读取已有独立探针，不能将这些结果写成 GUI 验收通过。
- 临时 ACP overlay 已验证实际 `agent/created`、`session.header.cwd/id`、`ctx.agents.roots()`、MCP 解析、`tools.guard` 和 `systemPrompt.section`。尚未验证真实桌面聊天中的 pre-step 顺序、动态开关后的下一轮模型行为和 GUI 状态；ACP 接口探针不替代桌面验收。
- 未验证真实模型轮次中规则是否实际影响行为，也未验证关闭开关后规则在真实 GUI 中的可见状态。
- 未实现自动唤醒、后台续接、桌面通知、完成投递一次或等待恢复；插件未承诺这些能力。
- 当前 `0.2.1-rc.2` 的 MCP 解析与导出经探针验证，其他版本和正式 link/bundle 安装路径未验证；插件会从 DSH 上下文 baseUrl、插件包位置和运行目录动态尝试 require/import 并 fail closed。若解析不到可显式配置 `--mcp-client-path`，不保证任意版本兼容。
- bundle patch 不再依赖 `!!js` 读取环境；本机失败探针保留，改用插件内的显式环境指针读取后，安装版双会话探针通过。配置完整内联路径时不会混入继承的其他安装配置。
- 开关 guard 在每次派发/整改前同步执行一次 `workflow_settings.py --status`，可能给该工具调用增加一个本机 Python 启动延迟；超时/失败按关闭处理，不缓存允许状态。
- 插件只应在 desktop 主脑 profile 使用；代码中的顶层判断不能替代用户正确的 profile 安装范围。未知/缺失宿主 API 时只保持无规则/无工具，不会全局屏蔽 DSH 正常工具。
- 首次绑定仅在本插件模块生命周期内固定；DSH 进程重启或模块重新加载后的原生会话迁移属于宿主行为，插件不写持久归属记录来阻止。
- 本机控制器授权仍是应用层信任边界，不声称 OS 隔离或抵抗恶意同用户进程。
- 未改 `brain_context.py`、`brain_gateway.py`、`brain_requests.py`、`brain_transport.py`、`brain_mcp_server.py`、现有 Codex 面板、每模型 Agent 默认值、付费许可或工作台预算/次数。
- 现有 `.local-worker/panel/app.js` 已从 `window.location.search` 读取 `project` 并进入显式项目视图；本轮未改面板 HTML/JS。面板令牌由页面自己的 `/api/session` 获取，不进入入口 URL。
- 客户端只能读取浏览器侧原生会话行的 `cwd`，不能像 Host 那样做 `realpath`；若该路径是符号链接、8.3 短名或大小写别名，工作台可能把它当成不同的项目字符串。Host MCP 仍使用自己的 realpath 绑定，二者都不猜路径。
- 右侧栏入口的实际 GUI 可见性、点击后原生 browser tab 的呈现与真实项目切换仍需用户在已安装 DSH 中确认；本工作只完成源码、mock/临时状态测试和安装版 ASAR 只读接口核对，未实际安装 0.2.0、未重启 DSH、未看到 UI。
- 客户端面板 origin 在安装器 `prepare` 时写入准备包；手工安装源码包只使用 `http://127.0.0.1:8765`。若工作台实际监听其它端口，必须用安装器的 `--panel-url` 重新准备，不能由客户端猜端口。
- 原生 browser tab 支持 `multiple`，因此每次点击“模型工作台”都会新增一个原生浏览器 tab；这是 browser 包的既有行为，当前不做跨 tab 去重或静默复用。关闭最后一个 tab 的行为仍由原生 sidebar 规则决定。
- 若目标 profile 未启用 `@deepseek-ai/dsh-client-ui-sidebar-browser`，临时正文会显示“无法打开原生右侧栏浏览器”的可解释错误；不会退回自建 iframe、私有 DOM 或外部浏览器。

## 交付前必须验证（仍待真实宿主）

- 两个项目/聊天不会互读、错派或串用请求回执；身份不清时不能变更任务。
- 原生主脑聊天具备工具；执行器与子 Agent 不具备同一主脑权限。
- 原生右侧栏出现中文“模型工作台”，一次点击在当前会话所属项目打开工作台；两个会话/项目切换不串；未知会话只显示未选择提示；浏览器未启用时显示可解释错误；插件停用后入口消失且旧浏览器 tab 仍可解释。
- 开关关闭不触发新自动工作；开启时规则实际生效，不仅是界面开关变色。
- 正确交付可接受，故意缺陷会被拒绝；执行模型 completed 不直接变成验收通过。
- 后台等待不触发周期性主脑模型轮次，完成后能正确续接原聊天；失败/未知不重复派发。
- 保留现有 Codex 流程、项目内记录、每模型 Agent 默认值、付费许可和用户关闭的可选限制。
- 安装、更新、卸载不覆盖其他插件或账号配置；GUI 可见性由真实桌面使用验证。

## 官方依据

- [DSH 第一个插件](https://github.com/deepseek-ai/deepseek-harness/blob/master/docs/user/develop/basic/index.zh.md)：Cordis 插件与作用域资源清理。
- [插件打包与安装](https://github.com/deepseek-ai/deepseek-harness/blob/master/docs/user/develop/basic/publish.zh.md)：组合包、profile、配置层及安装机制。
- [扩展插件实操手册](https://github.com/deepseek-ai/deepseek-harness/blob/master/docs/cookbook/extension-cookbook.zh.md)：原生事件、工具注册、提示词扩展与 followup。
- [右侧 Sidebar](https://github.com/deepseek-ai/deepseek-harness/blob/master/docs/subsystems/sidebar-right.md)：tab 类型注册、guide 扩展、`ctx.sidebarRight` 与会话生命周期。
- [Sidebar Browser](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/client/ui-sidebar-browser/README.md)：原生浏览器 tab、loopback 页面与 `ctx.sidebarRight.openTab('browser', { params: { url } })`。
- [ACP 文档](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/acp/acp/README.zh.md)：自动化入口，与原生 GUI 接入分开验收。

官方主分支文档用于选择路线，安装版本的能力以本机验证为准。本阶段基础代码与接口边界已独立检查；下一步是原生桌面安装/入口及实际需求的协作与质量闭环，再验证后台唤醒。

## 原生右侧栏“模型工作台”入口（0.2.0 开发）

目标是把已运行的本机工作台放进 DSH 原生右侧栏，而不是在外部浏览器手填 URL。入口优先复用安装版自带的 `@deepseek-ai/dsh-client-ui-sidebar-browser`，不修改 DSH 安装目录、不向父页面注入私有 DOM，也不接管浏览器正文。

### 本机版本依据（0.2.1-rc.2）

- `integrations/dsh-brain/package.json` 的 `dsh.client` 与 `exports["./client"]` 由安装版 `@deepseek-ai/dsh-client-modules` 扫描；browser half 是 `client.mjs`，Host half 仍是 `index.cjs`。只读查看 `app.asar` 确认：`dsh-client-ui-sidebar-browser@0.2.1-rc.2` 声明 `dsh.client.platform: "web"`；`dsh-client-ui-sidebar-right@0.2.1-rc.2` 公开 `ctx.sidebarRightTabs.register()`、`ctx.sidebarRight.openTab()`、`sidebar.right.pane.tab` keyed seat，以及 `sidebar.right.pane.tab` 的 session scope/inject 形状。
- 浏览器 tab 在 Desktop 使用 Electron `<webview>`，支持 loopback HTTP(S)；引用包 README 明确允许 Client 插件调用 `ctx.sidebarRight.openTab('browser', { params: { url } })`。因此入口只负责“按当前原生会话拼 URL”，页面沙箱、历史、保活与关闭仍由原生 browser tab 负责。

### 实现与身份边界

- `client.mjs` 通过 `ctx.sidebarRightTabs.register()` 注册一个页面类型和 guide 条目，标题固定为中文“模型工作台”。点击后由该类型的临时正文调用 `tab.actions.openTab('browser', { params: { url }, replaceTab: true })`，把临时页替换成原生浏览器 tab；一次点击即可看到工作台。
- 临时正文的 `inject` 从当前 slot 取得 `sessionId`，只读取 `ctx.sessions.list` 中该原生会话行的 `cwd`。`cwd` 必须是绝对本机路径（Windows 盘符/UNC 或 POSIX 根）；空值、相对路径、控制字符或会话列表尚未加载时都不猜服务 cwd、不读最近项目、不改用屏上其他会话。未知身份显示可解释的“未选择项目”，并只提供显式“打开未选择项目的工作台”按钮；目标 URL 显式携带 `view=unbound`，避免读取页面记住的项目。Windows 桌面同时拒绝 `/work` 这类依赖当前盘符的目录；POSIX 客户端保留其根路径形式。
- 已知 `cwd` 用 `URLSearchParams.set('project', cwd)` 写入 loopback 工作台 origin，路径只出现在浏览器 URL 的 `project` 参数中；不附加 panel token、连接 capability、owner/session 凭据或父页面 postMessage。客户端没有 `fetch`、`/api`、`localStorage`、`postMessage` 或模型调用。
- 两个会话各自渲染自己的 guide/tab，正文只使用所属 tab 的 `sessionId` 与 `tab.actions`，切换会话不会借用屏上会话的项目；没有当前会话时不构造 project。隐藏/重新打开由原生 browser tab 的既有生命周期处理；插件停用/卸载时 Cordis effect 释放 guide 与正文注册，已打开的浏览器 tab 仍归原生 sidebar 管理。重复点击会按原生 browser 的行为新建浏览器 tab，不会把项目参数混到别的会话。
- 打开面板只加载工作台页面；本入口不调用模型、不派发任务、不写任务记录，也不要求空项目初始化 Git 或 records。工作台自身页面加载后的行为仍受其现有开关、预算与记录规则约束。

### 安装器与版本

- `dsh_brain_desktop.py` 的固定清单新增 `client.mjs`（六个文件）；`package.json` 版本升到 `0.2.0`，并补上 `exports["./client"]`、`dsh.client` 与 `files`。`prepare` 会把同一个 `panelUrl` 以带引号标记替换进准备包内的 `client.mjs`；源码包本身保留 `http://127.0.0.1:8765` 回环默认。清单、标记数量、归属收据与未知结果语义不变。
- 手工直接安装源码 bundle（不经过 `prepare`）时客户端使用默认 `http://127.0.0.1:8765`；自定义 `--panel-url` 必须走安装器的准备包。准备后再改 `panelUrl` 会让 Host MCP 与客户端入口不一致，必须重新 `prepare`（并完成对应的归属/卸载流程），不能只改 live `config.json`。安装器仍不覆盖已有归属收据，也不会自动更新 live preparation。
- 从已安装的 `0.1.0` 升级：先用原安装资料目录执行 `uninstall`，确认插件移除后再对新目录 `prepare`/`install` 0.2.0；不要覆盖或重写旧 `plan.json`/`receipt.json`。0.1.0 的旧收据仍可卸载其五个 Host 文件；新 `client.mjs` 只属于 0.2.0 准备包。

本轮离线结果：`test_dsh_brain_sidebar.cjs` 10 项通过；`test_dsh_brain_desktop.py` 36 项通过（含准备包 origin/标记两项）；`test_dsh_brain_plugin.py` 运行 12 项，其中 11 项通过、1 项目录软链接检查因权限跳过；`test_dsh_brain_plugin.cjs` 19 项通过。当前 worker 进程带有 `MODEL_DESK_RECORD_PROOF`/`PRESET_PROOF` 执行器标记，宿主测试需仅在子进程中清除这三个 `MODEL_DESK_*` 标记（`RECORD_PROOF`/`PRESET_PROOF`/`SUBAGENT_LEDGER`）后运行；这不是产品失败，插件按设计在该标记下禁用。静态只读 app.asar 探针确认三个 0.2.1-rc.2 包版本、guide/tab 公开点、browser loopback 接口和本地 client 清单均为 true。以上均未启动真实 DSH GUI/工作台或调用模型。

## 桌面安装入口（2026-10-18）

新增 `.local-worker/dsh_brain_desktop.py`。默认无安装副作用，操作明确分开：

```powershell
<python> .local-worker\dsh_brain_desktop.py prepare `
  --output "<尚不存在的本机安装资料目录>" `
  --workbench "<已部署新主脑接口的工作台目录>" `
  --python "<工作台 venv>\Scripts\python.exe" `
  --executable "<DSH目录>\DeepSeek Harness.exe" `
  --home "<已由 DSH 初始化的 DSH_HOME>"
<python> .local-worker\dsh_brain_desktop.py check --directory "<安装资料目录>"
<python> .local-worker\dsh_brain_desktop.py install --directory "<安装资料目录>"
<python> .local-worker\dsh_brain_desktop.py uninstall --directory "<原安装资料目录>"
```

- `prepare` 只创建新目录中的 `plugin.tgz`、`config.json` 和最后提交的 `plan.json`。0.2.0 包含六个明确源码/许可文件（新增 `client.mjs`），不含机器配置、模型密钥或记录；拒绝链接源码、已有输出与 `.model-desk` 路径。目录不完整时无法安装。
- `check/install` 使用 unknown 责任绑定连接已运行的新 broker，只读取 describe，不具备任务变更授权。旧版接口/连接失败/返回身份不匹配都拒绝安装；不会为安装启动或部署工作台。
- `install` 只对显式 DSH_HOME 的现有 desktop profile 调用已安装产品携带的官方 CLI，默认离线，禁用安装脚本和自动 peer 安装。只设置 `MODEL_DESK_DSH_BRAIN_CONFIG` 这一条 User 环境值，不改 PATH、账号或 profile patch。已有同名未知插件/环境值不能接管。
- 使用固定清单/hash、独占操作锁与原安装收据确认归属。重复成功安装不重复执行 CLI；命令结果未知时标 attention，不自动安装/卸载；不会把安装前快照覆盖回用户后来修改的 profile。
- 首次未知失败可显式执行 `reconcile-absent --directory <原安装资料目录>`：核查 DSH 及其安装进程已退出、无原生运行标记/锁、无本插件引用/链接/环境值、原 manifest 其他元数据不变。它只确认插件未安装，不承诺包缓存没有变化，不执行安装。保留原失败记录后，才允许单独的 `install --retry-verified`，至多一次。
- 官方 CLI 明确返回 `ERR_PNPM_NO_OFFLINE_META` / `ERR_PNPM_NO_OFFLINE_TARBALL` 时，可显式执行一次 `install --resolve-offline-cache --directory <原安装资料目录>`。它再次核查上述归属/未安装状态，通过同一官方入口联网解析依赖；继续禁用安装脚本、自动 peer 安装并保留原生兼容检查。未知错误、认证错误、已安装/元数据漂移或已有联网修复失败均不适用，不自动降级/联网/循环重试。所有先前失败收据保留。
- `uninstall` 比较原包路径与原收据，只移除本插件并清除仍属于本安装的环境值；保留其他插件与准备目录中的资料。环境值或插件来源被用户更改时拒绝覆盖。已删除/升级工作台仍可按原收据移除自己的包；宿主版本变化需先核查。
- 安装/卸载均不关闭或重启任何程序。已运行 DSH 不会自动获得新 bundle 成员和环境，需要用户择时退出再打开。安装包资料目录必须保留，不能作为普通临时文件清理。
- 本轮执行任务在启动前因 DeepSeek 余额查询 DNS 失败终止，模型调用 0；安装实现由主脑完成，没有绕过余额门禁或替换执行模型。

独立验证：安装器 24 项离线测试、插件 19 项 Node 测试通过；安装版 DSH 在临时 DSH_HOME 中使用官方 CLI 的完整 add/remove 探针通过，其他元数据不变。原先 remove 不支持直接传入的选项已改用其支持的 `--config.*` 形式，失败探针保留。原 bundle 配置表达式失败后改用代码读取环境，真实 DSH 双项目加载探针再次通过，无模型提示或 broker 变更。新接口叠加运行版其他源码的临时兼容镜像，56 项核心测试通过。安装预检另补真实 transport 协议形状测试，避免 mock 掩盖声明格式错误；两个不同安装资料目录也受同一个 profile 操作锁约束。真实 GUI 与主脑质量闭环不能由这些探针推定。

本机接入状态：队列确认空闲后，已备份并部署经过兼容验证的接口源码，工作台重新就绪。正式安装资料位于运行目录的 `.local-worker/desktop-integrations/`（已加入 Git 忽略规则，不能清理或公开上传）。首次官方 CLI 安装失败且原因未记录；用户完全退出 DSH 后核查没有已安装插件/原生运行标记，明确重试取得 `ERR_PNPM_NO_OFFLINE_META`，定位为缺少离线缓存。随后使用同一官方入口的一次显式联网缓存修复安装成功，两个先前失败记录均保留。0.1.0 实际安装的五个 Host 文件与准备包逐项 hash 一致，插件引用及环境指针匹配；其他 manifest 元数据、模型范围、付费许可和可选限制均未改变。真实只读主脑预检通过。本次诊断/安装未调用模型，没有自动重启 DSH。0.2.0 的 `client.mjs` 仅完成准备包/离线测试，尚未安装到 live profile。

独立恢复测试现为 34 项通过，覆盖原失败保留、原生标记/运行进程/配置变化拒绝、明确恢复、固定离线错误联网修复以及失败不重复。原生 GUI 仍需用户重新打开 DSH 后确认；真实协作质量与自动唤醒尚未验收，不由安装成功推定。

### 主脑独立检查（0.2.0 源码候选）

主脑检查了实际改动与安装版 SDK 的 session-list、slot 注入、tab actions 和 Client 模块导出形状，并独立执行：Python 48 项（47 通过、1 项目录软链接权限跳过），Node 30 项全部通过。补上未知项目的显式 `view=unbound` 和 Windows 当前盘符根路径拒绝检查；没有更改 Host/MCP 代码、主脑控制器、模型范围、默认 Agent 或用户预算设置。静态 API 依据不能证明实际 GUI 入口可见。

源码候选和六文件发布包已就绪；当前 live profile 仍为 0.1.0。升级需要用户择时退出 DSH，沿用原收据移除旧版本、在新资料目录准备并安装 0.2.0，再由用户确认右侧入口及双项目切换。原失败/安装收据保留，不关闭用户程序、不提前把 GUI 或自动唤醒标为验收通过。
