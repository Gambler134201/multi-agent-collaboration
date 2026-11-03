# 终端保护：工作目录与操作目标

2026-10-26（最新候选）：新任务 schema=3 取消全文扫描与“无法解析即拒绝”；只核对结构化路径及有限直接调用的明确目标。过滤/检索/输出文本不再独立构成拒绝，未知目录切换后的相对路径不猜成旧 cwd。动态脚本、管道、重定向、通配符和不透明操作交由原生权限及任务要求，参数守卫不证明这些 IO 安全。旧版本保留历史规则；下方解析器/加固设计属于历史或后置项，以 [当前契约](TASK_EXECUTION_POLICY.md) 为准。

2026-10-26（任务规则第二批，源码候选）：schema=2 新任务把保护位置冻结为路径声明，四端共用目标匹配；普通 auth-token 业务词和 tokens.json 文件不再单独触发认证拒绝。三个适配器侧的字面目录分支遍历收敛到 terminalFacts，继续使用已有 leadingLocation，不新增 Shell 语法。旧 schema=1/legacy 任务保持原认证文字规则。Git 发布/历史操作、未核实递归清理、记录及环境配置的残余全文检查仍保留，不能声称全部终端误判已解决。见 [任务级权限契约](TASK_EXECUTION_POLICY.md)。

2026-10-26（精简首批）：公共分析不再把整条字面输出的负载当成路径或操作。只识别有限的 echo/Write-Output/Write-Host 调用，真实参数与回执哈希不改写；复合命令、展开、重定向等不获得豁免。后续接线 junction 或 Shell 解析候选时，必须消费同一分析结果，不能重新扫描纯输出负载恢复误拦。保护必要性、整轮拒绝差异与本轮验证缺口见 [精简审计](GUARD_SIMPLIFICATION.md)。

## 2026-10-26：成熟解析组件的接线前提

对安装版公开代码的只读核对发现：ZCode 3.16.3 安装包的命令运行代码有 Windows cmd/Git Bash 与其他平台的方言选择分支；WorkBuddy 5.8.3 的公开 CLI 代码包含不同 PowerShell 运行时的选择。代码中存在分支不代表桌面宿主某个任务实际选择了其中哪条。当前 hook 回执没有可直接绑定本任务调用的 shell 路径/方言证据，因此未把 tree-sitter-bash 或 PowerShell AST 强行接入放行逻辑。

下一步应先获得原生宿主的实际选择回执，再接对应的成熟解析组件；未知方言不得猜测，解析结果也不是任意脚本副作用的安全证明。本批没有新增 Shell 解释器特例、安装依赖或接线下述 junction 候选；现有保护与其已知限制仍保留。

2026-10-26（第二批）已完成四端原生元数据来源核实：DSH/Pi 的安装代码按工具名固定解释器 argv（构造级证据，版本钉扎），WorkBuddy 的 PowerShell 工具可证但 Bash 工具存在 Git Bash 缺失时静默回退 PowerShell 的歧义分支，ZCode 的 Bash 工具在自动检测不到 Git Bash 时落到 cmd 旧式 shell 且选择是会话级、可经原生服务在发送提示词前读回（未接线）。四端逐调用载荷均无 shell 字段。结论矩阵、逐端代码位置、候选模块（`agent-runtime/candidates/shell-dialect.cjs`，未接线）与最小接线设计见 [SHELL_METADATA.md](SHELL_METADATA.md)。

2026-10-24 修复了一类 `record_ancestor` 误拦：将 `cd 项目目录 && rm 临时文件` 整体扫描时，目录参数曾被混作删除目标。公共记录保护现在单独解析前置的字面 `cd/chdir/Set-Location/sl`，按切换后的目录检查剩余操作。

同时修正：

- `cwd` 与 `workdir` 都参与相对路径解析；两个参数冲突时拒绝，不选择一个忽略另一个。
- 分号和 `||` 保留目录切换失败的可能，原目录和新目录都检查；`&&` 按切换成功后的目录检查。
- 通过 junction/符号链接切换目录时，同时检查逻辑与真实目录，防止 `..` 隐藏记录目录的真实上级。
- Pi/DSH 的“项目范围”设置也检查相对目录切换，`cd .. && ...` 不再绕过项目边界；完全访问仍依用户选择。
- 可证明只读的输出命令，不再因文本中出现 `copy/remove` 等词被当作修改命令。
- `.md` 等文件后缀不作为 `md` 创建目录命令识别；真实 `md/mkdir` 记录目标仍拒绝。
- WorkBuddy 验证字面切换的目标，仍拒绝原生配置、其他会话、认证及记录目标；事件只增加 `cwd_source` 和是否识别到前置目录切换，不记录原命令或路径。

支持的范围刻意有限。变量、替换、shell provider、复杂引号或过长的目录分支不能静态核实时，保守拒绝并给出工作目录诊断，不能理解为真实删除了记录。任意脚本的运行时计算目标仍无法完整判断；这不是 OS 沙箱或通用 shell 解释器。

真正的记录写入、覆盖记录的删除/移动、危险通配符及拒绝后的跨工具重试仍受保护。一次拒绝后本轮停止规则未放宽；旧失败记录不重写、不自动重派。

公共策略供 DSH、Pi、WorkBuddy 的新任务使用，两主脑共用执行端实现。保护代码指纹变化后旧会话/旧任务不得假装加载了新策略；使用新任务验证。离线检查覆盖公共策略、原生 WorkBuddy hook、Pi/DSH 安装运行时和临时 mock provider，不调用真实模型；原生 GUI 发起的新任务另行验证。

兼容检查另发现 DSH 已展开为 `resources/app` 时旧加载器仍只认 `app.asar`。现在只在归档不存在时读取该安装的展开 CLI 与预设，内容指纹使用实际字节；已有归档损坏不切到另一份来源。没有修改安装目录、预设选择、嵌套代理政策或用户权限。

## 候选（未接线）：受保护根的解析匹配

2026-10-25 盘点确认一个词法缺口：原生守卫判断终端命令是否触到 `protected_roots` 时只做词法子串匹配（workbuddy-native-guard.cjs 的 `referencesProtectedRoot`）。指向受保护原树的 junction 别名在命令文本中从不出现原树拼写，`type "<别名>\secret.txt"`、`Remove-Item "<别名>" -Recurse` 因此不被该层拦截。完整 job 形态（原树即 record_project）下 endpoint-access 隔离层按 realpathDeep 解析并拒绝（workspace_isolation）；其余受保护根（`protected_roots` 的其他条目）在终端路径上没有第二层判定。该现状是记录而非期望，由 test_native_profile_boundary.cjs 的 "documents current lexical boundary: junction alias to the original tree is only caught on resolved file targets" 固化。

候选方案在 `.local-worker/agent-runtime/candidates/protected-root-resolution.cjs`，未被任何模块引用，也不在守卫指纹（revision）覆盖的文件之列，仅存在不改变任何行为。判定逻辑（`referencesProtectedRootResolved(command, root, base)`）：按 record-policy 的词形规则取命令 token（`extractTokens`/`looksLikeTarget` 在该模块未导出，候选内逐字镜像并注明，接线时应改为导出），`resolveAgainst` 解析为绝对路径（相对调用方传入的 shell 工作目录；缺省用受保护根的父目录），`realpathDeep` 解析 junction/符号链接后，用 normalizePath 加分隔符前缀判断是否等于根或位于根内，`<root>-task-workspaces`、`<root>-backup` 等同名前缀目录不会误配。取舍有两处：含 `$`/`%`/反引号的 token 无法从文本证明目标，按跳过处理而非拒绝——逐 token 保守拒绝会把 `Write-Output "$env:USERNAME"` 这类普通命令一并拦下，递归加变量的清理仍由 unverified_recursive_cleanup 规则负责；空根、非字符串命令或解析过程异常返回 true（无法证明即拒绝）——解析器缺陷只会多拒终端命令，不会放开访问，接线后应关注 native_profile 拒绝率。

兼容性影响（若按"在词法匹配之外追加"接线）：

- 字面路径行为不变：兼容矩阵测试对不含别名/链接的命令集合逐例断言候选判定与词法判定一致（含大小写、混合分隔符、引号内纯路径、含路径的叙述文本、相对路径、变量输出命令）。
- 别名命令从放行变为拒绝，这是预期的收紧。逐一运行现有守卫测试套件并叠加候选判定模拟后，仅上述记录性测试的两条断言会变红（两条别名命令由放行变为"终端不能访问原生配置目录"，该测试注释本就要求改守卫后更新记录）；test_zcode_native_guard.cjs 全部 20 例、test_native_profile_boundary.cjs 其余 9 例（rg 排除、变量命令、工作区隔离、unverified_recursive_cleanup、原生配置字面路径）判定均不变。仅分析，未改守卫，现网不会真红。
- 词法可见而解析不可见的形态仍由词法层负责：开关参数携带的路径（`--path=<root>`）、代码字符串内嵌路径、`<root>.` 与 `<root>:stream` 拼写（Win32 realpath 拒绝这些形式）。因此接线只能追加，不能替换词法匹配。
- 无法解析的变量 token 不新增拒绝（见上），现有任务中含变量的命令不受影响；保守 true 仅在解析基础设施异常时触发，测试套件中无此形态。

测试：`node --test .local-worker/tests/test_protected_root_resolution_candidate.cjs`，10 例全部通过（临时目录内创建真实 junction，结束时清理，无需特权；守卫仅被测试 require 以调用其导出的纯函数）。用例清单：正例 "junction alias into the protected root is caught on resolved command tokens"、"literal paths of the protected root itself stay matches"；反例 "sibling copies and same-prefix directories are not the protected root"、"an alias to a non-protected tree does not become a protected reference"、"variable-bearing commands without protected references stay allowed"；取舍 "unresolvable tokens are skipped rather than conservatively refused (documented trade-off)"；兼容矩阵 "candidate agrees with the lexical matcher on literal paths"；差异记录 "documented divergences from the lexical matcher require additive wiring, not replacement"；base 语义 "relative alias tokens resolve against the caller-provided working directory"；故障取向 "cannot-prove inputs fail closed with true and never throw"。

决策问题（需单独决策，本候选未接线）：

1. 是否接线进 workbuddy-native-guard.cjs 的 shells 分支。缺口真实、形态明确，建议接线；zcode-native-guard 经 decideCall 委托同一 decide，接线后两个原生宿主同时获得该行为。
2. 检查点：在 shells 分支现有 `protected_roots` 词法检查处追加解析匹配，`base` 取 `args.workdir||args.cwd||job.project`，与前置目录切换分析的 cwd 语义一致。Read/Write 的 target() 已按 realpath 判断 protected_roots，无需改动。
3. 灰度：修改守卫文件即改变指纹，旧会话/任务按 revision 绑定自然失效，仅新任务生效，这是现有机制。如需更细粒度，可在 job 增加显式 opt-in 字段（需扩展 job 校验与控制器写入；字段缺失时行为与现在完全一致），按项目或任务逐步开启；默认全量也可辩护，因为行为只对别名命令收紧。
4. 接线前置：从 record-policy.cjs 导出 `extractTokens`/`looksLikeTarget` 并删除候选内镜像（或明确接受镜像副本），同时把记录性测试改为新行为并保留缺口说明。
