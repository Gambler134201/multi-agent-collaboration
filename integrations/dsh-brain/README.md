# model-desk-dsh-brain

可选的原生 DSH 主脑适配器。它把当前 DSH 桌面聊天绑定到本地模型工作台的通用主脑
stdio MCP（`brain_mcp_server.py`），并按工作台协作总开关加载主脑规则、开放派发/整改
或保留读取/验收能力。

它只应安装到 **DSH desktop（主脑）profile**，不安装到 DSH/Pi 执行器 profile，也不修改
DSH 默认模型、preset、预算、调用次数或 runtime。

## 不做什么

- 不启动模型工作台，不调用模型，不读取账号、密钥或会话历史。
- 不写 DSH profile、workflow 设置或项目记录；本包只含预构建 CJS 和 patch。
- 不提供固定的全局项目/会话/拥有者配置。项目、拥有者和会话只来自当前原生聊天。
- 未实现自动唤醒/后台续接；规则明确禁止承诺自动回来或周期性轮询。
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

原 bundle 的 `!!js` 环境表达式在本机加载探针中未生效，现已改为普通空值配置，
由插件代码读取这一条环境变量。完整内联路径配置不受继承的环境变量影响。

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
<python> .local-worker\tests\test_dsh_brain_plugin.py
```

测试使用临时目录、假状态脚本和 mock Agent/Cordis API；不启动服务、不加载真实 DSH
profile、不调用模型。

主脑另用已安装 DSH `0.2.1-rc.2` 的临时 ACP overlay 验证：两个临时项目各自加载九工具、
`brain_describe` 的项目/原生会话一致、规则装配生效、继承作用域的验收调用被拒绝，
临时开关关闭后拒绝派发但保留验收。没有发送 `session/prompt` 或调用 `brain_connect`。
这是原生插件接口验证；尚未验收桌面 GUI 安装/入口、真实模型行为或自动唤醒。
