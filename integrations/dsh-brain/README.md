# model-desk-dsh-brain

可选的原生 DSH 主脑适配器。它把当前 DSH 桌面聊天绑定到本地模型工作台的通用主脑
stdio MCP（`brain_mcp_server.py`），并按工作台协作总开关加载主脑规则、开放派发/整改
或保留读取/验收能力。

它只应安装到 **DSH desktop（主脑）profile**，不安装到 DSH/Pi 执行器 profile，也不修改
DSH 默认模型、preset、预算、调用次数或 runtime。

## 0.2.5：共用等待宿主层与原聊天唤醒探针

本版新增 `wakeup.cjs`，安装包共八文件。插件只向所属顶层 Agent 的 MCP 子进程提供临时本机管道；
通道及随机授权值不进入页面、工具结果、URL、任务提示或持久配置，执行器与其他聊天没有此通道。
DSH 的普通工具不受管道启动失败影响，不回退到 Codex 或其他聊天。

仅用户明确要求诊断时使用 `brain_wakeup_probe`：先 `brain_connect`，再 `action=start`；确认
`waiting=true` 后说明约 30 秒等待、请用户暂时不要发消息或关闭 DSH，然后结束本轮。90 秒
未收到可询问 `action=status`（原 probe_id）。固定事件进入原聊天后，模型用 `action=received`
确认并简短回复。`action=stop` 可停止尚未投递的探针；重复 start/status 不重复投递，未知结果不重试。
探针状态只在当前插件进程内保存，重启后不能冒称已收到。

普通加载和后台等待不调用模型；**投递探针会触发主脑新一轮，产生主脑用量**，不启动执行模型。
这不是定时反复调用主脑。探针不等于真实任务通知：本阶段真实任务自动订阅、接收登记与验收工具
的集成尚未开放，也不支持跨工作区或关闭 DSH 后唤醒。源代码中的共享等待核心已有 DSH 宿主适配，
但应在原生探针验收后再接通正式任务入口；项目历史只读不授予通知/续接/验收权限。

0.2.0 另含一个原生右侧栏 `client.mjs`：在右侧栏 guide 注册中文“模型工作台”入口，按
当前原生会话的绝对 `cwd` 生成 `project` 参数，并用 DSH 自带 browser tab 打开本机工作台。
它不修改 DSH 安装目录，不注入私有 DOM，不向父页面/URL 传递 panel token 或连接 capability。

## 不做什么

- 不启动模型工作台；加载与等待不调用模型；只在明确的固定事件投递后触发主脑一轮。不读取账号、密钥或会话历史。
- 不写 DSH profile、workflow 设置或项目记录；本包只含预构建 CJS 和 patch。
- 不提供固定的全局项目/会话/拥有者配置。项目、拥有者和会话只来自当前原生聊天。
- 真实任务自动唤醒/后台续接尚未开放；诊断探针不能用来承诺业务任务会自动回来，禁止周期性轮询主脑。
- 不改变现有 Codex 面板、brain_context、brain_gateway 或执行 Agent 选择。

## 安装与配置

1. 把本目录作为可选 bundle 安装到 DSH desktop profile（依赖
   `@deepseek-ai/dsh-mcp-client` 由已安装的 DSH desktop 提供）。安装机制以你的 DSH
   版本为准；本仓库不改真实 profile。
2. 用生成器写出本机配置，路径全部显式指定（示例只是占位）：

   ```powershell
   <python> .local-worker\dsh_brain_plugin.py `
     --python "<venv>\Scripts\python.exe" `
     --brain-mcp-server "<安装目录>\.local-worker\brain_mcp_server.py" `
     --workflow-settings "<安装目录>\.local-worker\workflow_settings.py" `
     --output "<配置目录>\dsh-brain.json"
   ```

   生成器只创建该输出文件，已存在时拒绝覆盖，不修改 profile。配置中不含
   project/owner/session；这些只在运行时从原生聊天取得。
3. 设置 `MODEL_DESK_DSH_BRAIN_CONFIG` 指向该文件后重启 DSH；或在 desktop profile 的
   patch 中按 `model-desk-dsh-brain` id 覆盖 `config.configFile`。二者都没有时插件
   保持 inert（fail closed）。

也可使用 `.local-worker/dsh_brain_desktop.py`：`prepare` 在新目录准备固定清单的插件包、
机器配置与安装计划；`check` 验证已运行工作台的新主脑接口；显式 `install` 才通过
安装版 DSH 的官方 desktop CLI 安装，并设置一个 User 环境值；`uninstall` 按原收据
只移除本插件。它不重启 DSH、不启动工作台、不改账号配置，不覆盖其他插件或用户 patch。
服务接口不可用时拒绝安装；未知命令结果保留收据，不自动重试或盲目恢复。
`check` 会同时报告控制器就绪与安装收据状态，attention 不会被当作可直接安装。
安装资料请放在持久目录并保留；工作台自己的 `.local-worker/desktop-integrations/`
已排除于 Git，不能作为普通临时文件清理或随源码发布。

原 bundle 的 `!!js` 环境表达式在本机加载探针中未生效，现已改为普通空值配置，
由插件代码读取这一条环境变量。完整内联路径配置不受继承的环境变量影响。

## 原生右侧栏入口（0.2.0）

`package.json` 的 `dsh.client` 和 `exports["./client"]` 让同一 bundle 带出一个浏览器半侧；
`client.mjs` 只使用本机 DSH 0.2.1-rc.2 已公开的接口：

- `ctx.sidebarRightTabs.register({ id, kind, title, guide })` 注册页面类型和中文 guide 条目；
- `ctx.slots.register({ name: 'sidebar.right.pane.tab', key })` 注册临时正文；
- 正文的 `inject(sessionId)` 取当前原生会话，`ctx.sessions.list` 的该行 `cwd` 必须是绝对路径；
- 点击后调用所属 tab 的 `openTab('browser', { params: { url }, replaceTab: true })`，复用
  `@deepseek-ai/dsh-client-ui-sidebar-browser` 的原生浏览器 tab。

`project` 只来自该原生会话的绝对 `cwd`，经 `URLSearchParams` 编码。缺失/相对/尚未加载
的身份显示“未选择项目”，可显式打开未绑定工作台；不猜服务目录或最近项目。正文只使用
自己 tab 的 `sessionId` 与 actions，切换会话不串项目。入口注册随 Cordis effect 释放；
已打开的原生 browser tab 仍由 sidebar 管理。重复点击会按 browser 的既有 `multiple`
行为新建 tab，不做静默去重。浏览器插件未启用时，正文显示可解释错误，不退回自建 iframe。

安装器 `prepare` 会把 Host 配置里的同一个 loopback `panelUrl` 写入准备包中的
`client.mjs`；直接链接源码包时客户端使用默认 `http://127.0.0.1:8765`，自定义端口必须
经安装器准备。准备后再改 `panelUrl` 会使 Host 与客户端入口不一致，必须重新 `prepare`
而不能只改 live `config.json`。固定清单现为 `index.cjs`、`client.mjs`、`package.json`、
`cordis.patch.yml`、`README.md`、`LICENSE`。从 0.1.0 升级时先用原收据卸载旧包，再对新
目录准备/安装 0.2.0；不覆盖旧 `plan.json`/`receipt.json`。

## 运行时边界

- `agent/created` 串行 await：只处理 `ctx.agents.roots()` 中同一实例的顶层 Agent。
- 绑定要求 `agent.id === agent.session.id === agent.session.header.id`，且
  `agent.session.header.cwd` 是已存在绝对目录的 realpath。缺失/相对/漂移都 fail closed。
- 第一次绑定后不因原生身份或项目变化自动改归属；不同聊天各自绑定。
- 每个 Agent 的 `agent.ctx` 单独挂载 `@deepseek-ai/dsh-mcp-client`，serverName 默认
  `model_desk_brain`；并发 created 和 pre-step 不重复挂载，Agent/插件释放时 dispose。
- 工具使用 `agent.ctx.tools.guard` 最终校验 `exec.agent` 必须就是所属主脑实例，子
  scope 继承到的工具也会被拒绝；只拒绝本 server 的 `brain_*` 工具，不屏蔽 DSH 正常工具。
- 派发/整改在 guard 中重新执行 `workflow_settings.py --status`；缺失、不可读、非法或
  关闭都按 OFF。关闭时仍允许读取/验收，但拒绝新的派发/整改；后端会再次校验。
- 规则注册失败时拒绝新派发；MCP 关闭会等待 Cordis 异步清理，挂载未完成就卸载时也
  会回收稍后完成的连接。卸载未确认则保留拒绝守卫，禁止重复挂载。
- 规则通过 Agent scope 的 `systemPrompt.section` 贡献，并按当前 assembly 的 Agent
  身份过滤；子 Agent 看到的是空规则。

## 配置字段

`python`、`brainMcpServer`、`workflowSettings` 必须是显式绝对路径且存在；
`panelUrl` 只允许回环 HTTP origin；`serverName`/`ownerPrefix` 有格式限制；
`toolCallTimeoutMs`/`statusTimeoutMs` 为有界整数。可选的 `mcpClientPath` 只用于
显式指定已安装的 MCP client 模块；默认从安装版动态解析，不硬编码厂商路径。

`configFile` 只读；内联配置覆盖文件配置。`project`、`owner`、`session`、`role`、
`host`、`cwd` 等字段会被拒绝，以免形成项目固定的全局绑定。

如果 DSH 以 link/软链方式安装 bundle、导致从插件目录解析不到
`@deepseek-ai/dsh-mcp-client`，可在生成器加 `--mcp-client-path` 指向已安装模块（或
在配置中写 `mcpClientPath`）。插件从 DSH 上下文、插件包位置和运行目录动态尝试，
仍解析不到时不加载任何规则/工具。

## 离线检查

```powershell
node --test .local-worker\tests\test_dsh_brain_plugin.cjs
node --test .local-worker\tests\test_dsh_brain_sidebar.cjs
<python> .local-worker\tests\test_dsh_brain_plugin.py
<python> .local-worker\tests\test_dsh_brain_desktop.py
```

测试使用临时目录、假状态脚本和 mock Agent/Cordis API；不启动服务、不加载真实 DSH
profile、不调用模型。

主脑另用已安装 DSH `0.2.1-rc.2` 的临时 ACP overlay 验证：两个临时项目各自加载九工具、
`brain_describe` 的项目/原生会话一致、规则装配生效、继承作用域的验收调用被拒绝，
临时开关关闭后拒绝派发但保留验收。没有发送 `session/prompt` 或调用 `brain_connect`。
这是原生插件接口验证；尚未验收桌面 GUI 安装/入口、右侧栏实际可见性、真实模型行为或
自动唤醒。右侧栏入口目前只经 mock Cordis/React、准备包和 app.asar 只读接口核对。

## 0.2.1 原生登录账户余额

新增 `account.cjs`，只通过 DSH 官方 `accountController` 的安全公开接口读取登录状态和钱包金额。DSH 入口增加展示用 `host=dsh`，顶部显示原生登录账户余额；与工作台执行模型 API 账户分开。控制器接收公开快照仍需原 panel token，不放宽 origin/CSRF 检查；token、原始 SDK 诊断和 profile 信息不进入页面或模型。约两分钟同步，注销/过期/停用清除旧金额；“更新显示”读取最近快照。

原生账户服务暂不可用时保持未知。此功能不启动服务、不请求模型、不修改原生登录状态或工作台模型/预算。固定源码清单现在为七文件（含 `account.cjs`）；安装预检要求工作台提供账户快照接口。旧版本按原收据卸载，再在新目录准备/安装；不得覆盖旧收据。打开旧工作台 tab 不会自动补 host 参数，升级后请重新从原生入口打开。

主脑与执行模型同名/同 provider 是允许的；嵌套子代理 deny 不排除普通执行候选。手动默认模型在执行范围外时应查看 `brain_candidates` 的真实范围候选并明确填写 `model_id`，不能据此说范围内没有模型或静默替换默认。

## 0.2.2 账户适配器启动修正

账户适配器在已有聊天 MCP 初始化之前独立启动；MCP 未完成不会阻塞余额同步，账户接口失败也不会阻止主脑绑定。服务解析依次检查插件 `ctx`、`ctx.root`、当前顶层 Agent 的 `ctx`，仅取官方 `accountController`；不会扫描嵌套 Agent、认证文件或其他账户服务。

服务尚未注册时先发布不含金额的 `unavailable` 状态，再每 15 秒重查公开服务注册表；取到服务后恢复约两分钟的余额同步。固定诊断日志只报告解析路径 `plugin/root/agent`、公开登录状态和首次发布是否成功，不记录账号身份、金额、凭据或 SDK 原始错误。重入启动复用同一启动 Promise，停用时取消重试。

七文件清单保持不变。0.2.1 已安装不代表真实余额已接通；0.2.2 离线验证与实际桌面验收分别记录。升级仍需完全退出 DSH，并按原收据处理；插件不会自行重启 DSH。

## 0.2.3 严格 Cordis 上下文兼容修正

DSH 自带 Cordis 4.0.5 的插件上下文是严格服务代理。直接读取未声明的 `ctx.version` / `ctx.app` 会抛错，0.2.2 因而在创建账户适配器时退出，尚未进入快照发布。0.2.3 将这些可选版本提示的读取分别保护，缺失时使用产品包版本或已支持的兼容版本；原始上下文异常不记录、不外传。

除普通 mock 回归外，使用安装产品中的真实 Cordis 创建临时插件上下文，假账户与内存发布器全程不连接实际账户或工作台：0.2.2 复现创建抛错，0.2.3 创建成功并发布 updating → ready。真实余额仍需升级后验证。

## 0.2.4 主脑工具文本明细兼容

配套工作台 `brain_mcp_server.py` 同时在 `content` 中返回说明和精简 JSON，保留原 `structuredContent`。安装版 DSH MCP 客户端会保留结构化结果，但模型文本投影只读取 content；此前只有固定摘要，主脑看不到候选、任务状态或交付明细。

九个正式工具及本服务错误结果统一兼容，尤其保留未知结果的 outcome/request_reference，避免盲目重发。JSON 镜像只取已受主脑网关项目/拥有者校验的公开结果，不读取全局 /api/state 或暴露私有连接能力；不更改工具参数、授权或实际任务状态。配套网关原有 answer_truncated 等提示仍保留，文本不额外截断明细。

本次改动涉及工作台端 MCP 源码，升级需同步配套文件并重建安装计划中的源码指纹；已有 MCP 进程需要重新加载。不要直接改旧收据/指纹，也不要只安装七文件包后声称修复生效。真实 stdio/临时 HTTP mock 闭环会将返回交给安装版 DSH 渲染器再次核对，全程不连接真实模型或用户 profile。
