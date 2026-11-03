# 原生 Shell 元数据来源核实（2026-10-26）

P4 交接"Shell 解析器接线"的核实阶段产出。目的：在把 tree-sitter-bash 或 PowerShell AST 接进放行/拒绝逻辑之前，先回答**每个执行端的终端工具实际由哪个解释器执行、该选择能否绑定到本任务/本调用、依据哪个版本**。方法全部是只读检查：安装目录公开程序代码、官方文档、本仓守卫/桥接代码与现有测试 fixture。未读取认证或其他聊天，未发送任何模型提示词，未改安装目录，未部署。源码候选验证通过不等于运行版更新，也不追认 GUI 或其他执行端。

## 证据等级

| 等级 | 含义 | 今日状态 |
|---|---|---|
| `per_call_receipt` | 逐调用载荷字段直接声明方言/shell 路径 | **四端均无**（DSH exec 对象、Pi tool_call 事件、WorkBuddy/ZCode hook 输入 JSON 及各端工具结果均核实无此字段） |
| `pinned_construction` | 版本钉扎的安装代码按工具名固定了解释器 argv，或调用存在本身蕴含某个门控成立 | DSH 两工具、Pi 两工具、WorkBuddy PowerShell 工具 |
| `ambiguous_construction` | 安装代码按环境相关条件在两种方言间选择，调用载荷不带判定字段 | WorkBuddy Bash 工具（Git Bash 缺失时静默回退 PowerShell） |
| `unknown` | 无可采信证据；换版本必须重新核实 | ZCode 全部工具名（会话级原生读回路径存在但未接线，见下） |

按 P4 交接边界：环境变量、"本机装了什么程序"的复算，一律不替代实际选择证据；本候选表因此只收录上述等级，并把每个证据的版本条件写死，超出研究版本的安装一律降级 unknown。

## 结论矩阵

| 执行端（版本条件） | 工具名 | 方言 | 级别 | 关键残余未知 |
|---|---|---|---|---|
| DSH（0.2.1 系，dsh-tools 0.2.1-rc.2） | `bash` | bash | pinned_construction | PATH 解析到哪个 bash（语法不变；WSL bash 的路径语义不同）；展开安装下版本读不出（见下） |
| DSH（同上） | `pwsh` | powershell | pinned_construction | pwsh 7 还是 5.1 服务本调用 |
| Pi（1.3.0，pi_worker 精确钉扎） | `bash` | bash | pinned_construction | 解析到哪个 bash 可执行文件 |
| Pi（同上） | `powershell` | powershell | pinned_construction | pwsh 还是 powershell.exe |
| WorkBuddy（5.8.3；5.8.2 本批未复核） | `PowerShell` | powershell | pinned_construction | 运行时版本/edition（core/desktop） |
| WorkBuddy（同上） | `Bash` | **unknown** | ambiguous_construction | Git Bash 找到与否决定 bash -c 还是静默 PowerShell 回退，载荷不可见 |
| ZCode（3.16.3，zcode_worker 精确钉扎） | 全部 | **unknown** | unknown | 会话级选择可经原生服务读回（见下），本批未接线未验证 |
| WorkBuddy/ZCode/DSH/Pi | — | — | per_call_receipt 无 | — |

重要事实：**"Bash" 工具名在 WorkBuddy 和 ZCode 上都不能证明 bash 方言**——WorkBuddy 找不到 Git Bash 时静默回退 PowerShell 配置（快照预热日志 "Git Bash not found, shell falls back to PowerShell"）；ZCode 自动检测不到 Git Bash 时落到 legacy-shell（`ComSpec`/cmd.exe）。这正是交接文档"不要按工具名 Bash 猜测"的具体形态。WorkBuddy 宿主自己的沙箱映射（`toolName==="powershell" ? "powershell" : "posix"`）在回退分支上同样是错的，不能当作权威。

## 各端明细

### DSH（E:\dsh，产品版本 0.2.1.0；dsh-tools 0.2.1-rc.2）

- 工具注册名 `bash`（dsh-tool-bash）与 `pwsh`（dsh-tool-pwsh），与 `record-policy.cjs` 的 `TERMINAL_TOOLS` 字面一致。
- `dsh-bash-local` 的 `execute()` 以固定 argv `["bash","-c",command]` 经 `ctx.subprocess` 派生；`dsh-subprocess-local` 对裸名按 PATH+PATHEXT 解析（Git Bash/MSYS/WSL 命中都是 bash 语法）。
- `dsh-pwsh-local` 以 `[resolvePwshPath(config.pwshPath),"-NoLogo","-NoProfile","-NonInteractive","-Command", ENCODING_PREAMBLE+command]` 执行；解析顺序：显式 config → `%ProgramFiles%\PowerShell\7\pwsh.exe` → PATH 逐项 pwsh.exe → `%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe`（5.1）。执行文本 = UTF8 前导 + 用户命令（解析边界事实）。
- guard 面（`ctx.tools.guard`，record-protection.cjs 挂载处）收到的 exec 对象字段：`token, callId, rootCallId, name, signal, agent?, parent?, schema?, arguments`——**无 shell 字段**；guard 在 `tools/pre-execute` 瀑布与权限判定之后、工具体之前运行。
- 版本来源：`executor_adapter.installed_executor_version` 只解析 `resources/app.asar`；本机为展开布局（`resources/app`，无 app.asar）时返回 None，能力摘要落 `unversioned-v1`。展开布局下可改读 `resources/app/package.json`（`@deepseek-ai/dsh-desktop` 0.2.1-rc.2）——这是接线批次的一个配套小改，不是本批内容。

### Pi（E:\pi，@earendil-works/pi-coding-agent 1.3.0，pi_worker 精确钉扎）

- 官方 windows.md：原生 Windows 下 bash 工具用 Git Bash（shellPath → Program Files → PATH bash.exe）；powershell 工具 pwsh 优先、回退 Windows PowerShell，`-NoProfile -NonInteractive -ExecutionPolicy Bypass`；WSL 下为 Linux bash。
- 安装代码与文档一致：`dist/core/tools/bash.js` `createLocalShellOperations("bash", () => getShellConfig(shellPath))`；`dist/core/tools/powershell.js` `createLocalShellOperations("PowerShell", getPowerShellConfig)` + UTF8 前导拼接；`dist/utils/shell.js` 是 1.3.0 包内导出的解析器（agent-session 创建工具时用 `settingsManager.getShellPath()` + 该解析器）。
- 扩展 API（pi-protection.ts 所在面）：`tool_call` 事件仅 `{type, toolCallId, parentToolCallId?, toolName, input}`；`pi.getAllTools()` 只暴露注解；**无任何 shell 元数据暴露**。扩展可以进程内 require 钉扎包的解析器复算，但这属于"复算"而非回执，按交接边界不作为证据，仅作为未来接线的候选路径记录。

### WorkBuddy（E:\workbuddy，@genie/workbuddy-desktop 5.8.3）

- PowerShell 工具：`isPowerShellToolEnabled() = Windows && CODEBUDDY_USE_POWERSHELL_TOOL 非 "0"/"false" && findPowerShell() !== null`；`doFindPowerShell` 顺序：`CODEBUDDY_POWERSHELL_PATH`（版本校验）→ PATH `pwsh` → PATH `powershell` → `%PROGRAMFILES%\PowerShell\7\pwsh.exe` → System32 5.1，宿主用 `$PSVersionTable` 逐候选探测并按 CLI 进程记忆化。调用存在即蕴含找到运行时；执行 argv 前缀 `[-NoProfile,-NonInteractive,-WindowStyle,Hidden,-Command]`，另有 `$LASTEXITCODE` 包装与 PATH 前导。
- Bash 工具：`getShellConfiguration()` 在 Windows 下 `findBashPath()`（`CODEBUDDY_CODE_GIT_BASH_PATH` → git.exe 同级 bin/bash.exe → PATH 非规范 bash 校验）命中则 `{executable, ["-c"], shell:"bash"}`，**未命中则静默改用 PowerShell 配置**。工具 schema 文本自称 "This tool runs Git Bash (POSIX sh), not cmd.exe or PowerShell"，与回退分支矛盾；官方 tools 文档称"无 Git Bash 时 Bash 工具自动禁用"，代码显示是回退而非禁用。两说以代码为准 → 歧义。
- 逐调用可见面：hook 输入完整 schema（base：`hook_event_name, session_id, transcript_path, cwd, permission_mode?, tool_use_id|call_id, hook_specific_output?`；PreToolUse 加 `tool_name, tool_input`；PostToolUse 加 `tool_response`）**无 shell 字段**；工具结果 `stdout/stderr/exitCode/signal`（+可选 `meta.lastKnownCwd`）无 shell 字段；无 CLI/IPC 查询口。
- 本仓执行器（workbuddy_executor.py）剥离子进程 `CODEBUDDY_* / WORKBUDDY_*` 环境变量，会话恒走默认发现；版本钉扎 `SUPPORTED_VERSIONS=('5.8.2','5.8.3')` 对 asar 内 package.json + CLI 文件哈希。本批构造证据只对 5.8.3 有效。

### ZCode（E:\zcode，@zcode/desktop 3.16.3，zcode_worker 精确钉扎 + zcode.cjs 哈希）

- 两套 shell 体系：集成终端面板走 node-pty（pwsh→powershell→ComSpec→cmd，任务不用）；**代理 Bash 工具**走 child_process spawn。
- Bash 工具输入 schema 严格 `{command, timeout?, description?, run_in_background?, dangerouslyDisableSandbox?}`——**无 cwd/workdir 字段**；请求构造恒定 `shellProfile:"posix-bash"` + 会话级 `shellOverride`。
- 会话初始化时解析一次并记忆化（持久化于会话存储 `runtime:bash_shell_selection`）：桌面设置 `integratedTerminalShell`（`~/.zcode/v2/setting.json`，缺省 auto）+ Windows 自动检测（`C:\Program Files\Git\bin\bash.exe`、`C:\Program Files (x86)\...` → PATH git.exe 同级 bin/bash.exe）；两者皆无 → legacy-shell → `ComSpec`/cmd.exe。**PowerShell 不是代理 Bash 工具的可能方言**。
- hook 输入完整 schema（PreToolUse：`cwd, hookEventName, mode, riskLevel, sideEffectScope, sessionId, timestamp, toolCallId, toolInput, toolName, traceId, turnId` + snake_case 别名与 `transcript_path`、`agent_type`、`permission_mode`；PostToolUse 加 `tool_response, toolResultPreview`）**无 shell 字段**；hook 进程环境注入 `CLAUDE_CODE_SESSION_ID/ZCODE_*` 等，无 shell 变量；hook 之后运行时独立读取会话的 shell 选择，hook 既收不到也改不了。
- **会话级证据路径（未接线）**：解析结果可发送送提示词前经既有 CDP 桥读回——`settingService.get().integratedTerminalShell` 与 `systemService.listIntegratedTerminalShells()`（与现桥绑定的 zcodeAgentService 同属 services 对象，需一次运行时核验）；发送后可经 `readSession().messages[*].info.contextSnapshot.envInfo.shell` 交叉核对（"Git Bash"/"CMD"/"system shell" 显示名）。选择每会话固定，故发送前读回对该会话全部 Bash 调用有效——这是 ZCode 端唯一"原生 API + 可绑定本任务"的路径，属接线批次工作。

### 官方文档核对（CodeBuddy 系 hooks/tools 文档）

- hooks 文档：PreToolUse/PostToolUse 输入字段与本仓守卫消费的一致，无 shell 字段；Windows 上 hook 命令本身强制 Git Bash 执行（`CODEBUDDY_CODE_GIT_BASH_PATH`）。
- tools 文档：Bash 工具依赖 Git Bash；PowerShell 工具自动检测 7+ 优先回退 5.1，`CODEBUDDY_POWERSHELL_PATH` 可强制、`CODEBUDDY_USE_POWERSHELL_TOOL=0` 可禁用。与安装代码核对一致（除"Bash 无 Git Bash 即禁用"与代码的静默回退矛盾，按代码记录）。

## 与本仓现状对齐

- `workbuddy-native-guard.cjs` 第 236 行 `policy.decide(name==='PowerShell'?'pwsh':'bash',…)` 与 `pi-protection.ts` 第 21 行 `decide(name === 'powershell' ? 'pwsh' : name,…)` 都从工具名推方言，但 `record-policy.cjs` 的 decide/decideTerminal **完全不使用该参数做方言判定**（只用于 `TERMINAL_TOOLS` 成员判断），当前全部解析是方言无关的词法启发式——所以今天没有任何行为依赖这个（可能错误的）推断。
- 守卫 hook 输入、事件行（固定字段集）、`tool_response` 读取与上述各端 schema 完全对齐；事件行新增字段必须像 `command_diag` 一样显式选入。
- 既有 fixture（test_zcode_native_guard 20 例、test_native_profile_boundary 10 例）观察到的载荷与本矩阵一致。

## 候选模块与 fixture（未接线）

`.local-worker/agent-runtime/candidates/shell-dialect.cjs`：纯函数证据表，`dialectFor(executor, toolName, version)` 返回 `{dialect:'bash'|'powershell'|null, level, basis, limits}`。版本条件内嵌（DSH 0.2.1 系 / Pi 1.3.0 / WorkBuddy 5.8.3 / ZCode 3.16.3），超出即 unknown；模块不做任何 IO，由调用方传入其既有校验已核实的版本。与 junction 候选同理：不被任何钉扎文件引用，不在任何 revision 摘要覆盖内，存在本身不改变任何行为。

测试：`node --test .local-worker/tests/test_shell_dialect_candidate.cjs`，8 例全过——DSH/Pi 正例（构造级 basis 断言）、WorkBuddy PowerShell 正例 + Bash 歧义负例、ZCode 全工具名 unknown（含 "Bash" 不得猜成 bash 的显式断言）、跨执行端工具名不混淆、非终端工具 unknown、未核实版本全部降级 unknown、畸形输入不抛不编造、证明方言携带残余 limits、钉扎文件零引用。

## 最小接线设计（供决策，未实施）

1. **接缝形状**：`record-policy.cjs` 的 `decide/decideTerminal` 增加可选 dialect 入参（缺省 unknown，行为与今日逐字节一致）；成熟解析组件只对 `dialectFor` 返回非空方言的调用启用；unknown 恒走现词法路径。解析组件本身不在本候选内——接线时应与组件一次换版，避免钉扎文件两次失效。
2. **调用方取证**：
   - DSH：record-protection.cjs 的 config 增加 executor 版本（dsh_worker 已有 `installed_executor_version`；展开安装需先补 `resources/app/package.json` 读取），`exec.name` 即工具名 → `dialectFor('dsh', name, version)`。
   - Pi：pi-protection.ts 用 job 中已校验的 1.3.0 → `dialectFor('pi', event.toolName, version)`。
   - WorkBuddy：守卫内 `PowerShell` 可给 powershell（构造级），`Bash` 保持 unknown——**不得解析**，直到宿主提供可区分回退分支的回执。
   - ZCode：先接会话级读回（run-zcode.mjs 预检扩展 CDP 读取并写入任务记录，发送提示词前完成；一次运行时核验 services 对象内容后实施），读回成功才允许 bash 方言判定；读不到保持 unknown。这是四端中最有价值的接线点，也是唯一需要动桥接的。
3. **解析组件的现实约束**：守卫是零依赖单文件、hook 超时 10 秒、DSH 插件与 Pi 扩展同样精简。bash 方言的 tree-sitter-bash 需 vendor 的 wasm + loader（入 revision 摘要）；PowerShell 没有成熟的纯 JS 解析器，AST 方案需常驻/冷启 pwsh 进程，一次性 hook 进程内不可行——powershell 方言建议继续词法 + 保守拒绝，或只在非 hook 层（worker 复核）使用 AST。此取舍属接线批次决策。
4. **版本churn**：任何接线改动钉扎文件都会换指纹，旧会话/任务按现有机制自然失效、仅新任务生效；应把接缝、方言表与解析组件合并为一次换版。

## 未验证项

- 全部结论来自静态只读检查（构建 a82dd43e / 5.8.3 / 0.2.1-rc.2 / 1.3.0）；未驱动任何真实应用、未发送提示词、未做真机验证。本批无真实模型用量。
- ZCode services 对象是否同时携带 settingService/systemService（一次 CDP 运行时核验未做）；`envInfo.shell` 在发送前是否可读未验证。
- WorkBuddy 5.8.2 未按 5.8.3 口径复核；ACP/serve 协议是否在别处暴露 shell 未穷尽；WorkBuddy hook 命令在本机的实际执行 shell（Git Bash 还是回退）未探测——按边界不复算。
- DSH `bash` 的 PATH 解析目标与 WSL bash 的路径语义差异、pwsh 7/5.1 的实际命中，均为机器状态，未探测也不作为证据。
- `test_record_protection_runtime.cjs` 在本 worktree 因缺 `.local-worker/venv`（机器本地）无法运行；主运行目录已有该环境，此前增量通过。其余本批相关 Node 检查（守卫 37 项基线 + 候选 8 项）全部通过。
- 运行版部署、两宿主 GUI、DSH/Pi 真机派发未涉及。
