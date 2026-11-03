# 执行端生命周期与能力契约

2026-10-27本机部署补记：必要ZCode实例校验/定向关闭与统一任务规则已安装，见[第二批部署记录](RULES_DEPLOYMENT_20261027.md)。本文件描述的源码能力比本次运行包更广；描述用executor_contract模块与完整适配器接口并未因此全部安装。原P2/P3报告中的未部署状态为历史记录，不能覆盖本次限定安装事实或扩大真机证据。

建立日期：2026-10-25。状态：P0/P1 已按[执行端轻适配与隔离收敛方案](RELEASE_20261103.md)交付并本地提交；P2（ZCode 隔离试点）已完成候选真实文件任务的独立复核（见下）；P3（生命周期恢复）已完成离线整改与限定范围真机验证。P2/P3 均未部署运行版，生产队列全链路、宿主 UI、其他执行端与其余故障场景仍另行验收。开发自测与 Codex 独立验收分开记录。

## P2 独立收口（2026-10-26，源码候选限定范围通过）

真实 ZCode 原生任务在 Git 隔离副本中 Read/Edit 业务文件、Write/Read 声明临时脚本、执行指定自测命令；可信 PostToolUse 命令哈希精确匹配且退出码0，Codex 独立复跑通过。控制器冻结提交只含业务文件，临时脚本保留，原项目字节不变，会话关闭、隔离控制器资源结清。此前命令哈希不匹配的失败记录保留，旧差异原因仍未知；本次通过不追认旧任务。

真机暴露的收尾接缝已修复（7543251）：只有本任务有效原生 job/会话/项目绑定、固定 scratch 声明、普通文件元数据检查通过的**未跟踪**临时材料可不计为验收业务脏改。业务文件变化、新业务文件、历史 tracked/staged scratch、坏 job/缺声明、其他执行端及链接别名仍不获豁免。临时树元数据检查设有上限，读取失败或超限保守拒绝；不读取文件内容、不自动清理，副本回收仍拒绝含保留材料的工作区。公共验收核心被 Codex/DSH 复用，宿主 UI 不据此追认。

该局部修复运行六组相关增量共87项无失败；断言调整位置后单独补跑通过，并重放真实冻结提交确认保留 scratch 可通过绑定。源码原生诊断开关另补严格布尔检查（288f4ae）。原生守卫未因此放宽，junction 解析候选未接线，运行版尚未部署。

## P3：生命周期恢复（ZCode 最小闭环；离线复核整改 + 受控真机验证通过）

闭环：**主/辅进程识别 → 服务就绪 → 实例身份绑定 → 专用会话定向关闭 → 本任务资源与收尾**。复用既有机制（进程过滤、`process_identity`、就绪等待、`recovery_guard`、控制器 `finalize_only` 核对收尾），没有新增 supervisor：

1. **实例身份绑定**：连接后、发送提示词前，`zcode_worker.desktop_instance()` 以 pid+创建时间+命令摘要钉扎当前桌面主进程（复用 crashpad/辅助进程过滤）写入 `zcode_instance`；读取失败保持 None（未知≠死亡）。
2. **实例变化语义**：`instance_matches()` 只在有正面证据时回答"已退出"（NoSuchProcess 或 PID 复用——创建时间/命令行不同）；无法核实一律"未知"并保留资源。任务中实例退出由桥接断开暴露为结果未知，`recovery_guard` 继续拒绝重发。
3. **定向会话收尾**：`zcode_worker.finalize_session`（CLI `--finalize-session`）把"关闭未确认"变成有身份证据的确认，分支与证据边界（2026-10-26 独立复核整改后）：钉扎实例已退出（须完整身份凭据+进程消失或 PID 复用）→closed；无钉扎记录且进程表**可读**且肯定无主程序→closed（exe 不可读或行检查出错=unknown，拒绝）；同实例存活→`run-zcode.mjs` 的 `finalize` action 经原生服务只关闭**本任务**的 `modeldesk-zcode-` 会话（"Session is not active" 视为正面证据）；终态记录缺少会话 id 时按写入顺序（会话 id 先于任何 createSession 落盘）证明从未创建，记录自相矛盾则拒绝。非终态、已确认、连接漂移、实例身份不可核实均拒绝；未知/畸形证据从不转为肯定确认；从不发送提示词、不枚举其他会话、不停止桌面。
4. **控制器链路**：确认后既有 `recover_task(finalize_only=true, confirm=true)` 通过原生关闭检查，只释放本任务资源并收尾；未确认仍被拒。finalize 永不解除重发限制。

共享语义（证据字段、unknown 语义、never-resend）在 `executor_contract.recovery` 按端如实描述：dsh/pi 无独立原生关闭入口（运行子进程即会话，控制器子进程退出证据即关闭证据）；WorkBuddy 沿用既有 `cleanup_lease`（真机验证另记）；ZCode 为本批新增。其他执行端接入与真机验证未做。

**验证状态（2026-10-26）**：独立复核（48+29 项离线增量 + 三个负例探针）推动整改（346acee/6a03cd6）；Codex 复核又补齐两处同类边界——进程名不可读不得判缺席、非空坏格式会话 id 必须拒绝而非当作缺失（288d4af，含测试与拒绝链路矩阵扩充）。随后在 ZCode 3.16.3 真机完成**限定范围验证**：原生桥接创建/读取两个独立空会话（不发送模型提示词）；未知实例证据下候选真实 finalize 与控制器 reconcile 均拒绝、资源/租约快照不变；恢复合法身份证据后，真实桥接只关闭目标会话（另一测试会话可读、重复关闭幂等返回 inactive），隔离资源表中仅目标任务的 reservation 与租约被释放（伴随拥有者保持原值），同一桌面主进程与原生通道全程存活。**限定结论：定向会话关闭及隔离资源收尾真机范围通过**；详细证据在机器本地隔离诊断目录，不入 Git。仍未真机验证：真实生成任务发送后崩溃、账户/实例切换、OS 休眠/宿主重启、多窗口、生产全局队列负载、DSH/Pi/WorkBuddy 恢复、两种宿主 UI 与安装版表现；P2 清单中"副本内改文件+临时文件+自测命令"的真实任务仍未做（本次验证未发送提示词）。

## P2 已交付候选（开发自测通过）

1. **声明式任务临时目录**（`task_workspace.py`、`zcode_native_tools.py`、`zcode_worker.py`、共享 guard）：隔离 worktree 的 edit 任务在 job 中声明 `.modeldesk-task-tmp`（工作台常量，加载器拒绝其他值）；共享 guard 在全部保护检查之后、未声明写拒绝之前放行该目录内的写入；交付提交排除该目录；临时材料不进 changed_files/成果哈希。非隔离任务、只读任务、旧 job、WorkBuddy job 行为不变。
2. **ZCode 适配器接缝**（`executor_adapter.py`）：events/cancel/outcome 从 unavailable 升级为 direct（读任务记录事件、写 cancelled.flag、新 `read_task_outcome` 读 task.json 成果）；new_session/usage 仍诚实拒绝（usage 保持 unknown）。WorkBuddy 保持纯任务级委托（P4）。
3. **守卫重复实现收敛**（`workbuddy-native-guard.cjs`、`zcode-native-guard.cjs`）：目录检索排除 glob 收敛为共享 `SEARCH_EXCLUSION_GLOB`（两者超集——WorkBuddy 的检索结果改为隐藏与 ZCode 相同的记录/认证/宿主配置文件，属只读结果收紧）；固定拒绝码收敛为共享 `denialCode` 并集。事件字段与取值、proof/denied.flag、绑定校验、temp_dir 放行全部不变，既有守卫 fixture 零修改通过。
4. **junction 解析匹配候选（未接线）**：[候选方案、兼容性影响与正反测试见终端保护文档](SHELL_GUARD.md)；候选模块在 `agent-runtime/candidates/`，不在守卫指纹内，不改变任何现网行为，接线与否属单独决策。

对应验收清单勾选项见下节。

## 契约（已实现）

统一生命周期，不统一替代工具：

`探测 → 准备执行根 → 创建原生会话 → 发送一次任务 → 接收事件 → 收集成果 → 关闭本会话 → 控制器收尾 → 独立验收`

`.local-worker/executor_contract.py` 是宿主中立的描述与投影层，不派发、不写记录、不重试：

- `describe(engine)`：每个执行端的生命周期步骤到 `execution_result` 共享阶段的映射、承载各步骤的既有记录字段与证据文件、能力声明（supported/unsupported/unverified）。任何描述都明确"不是操作系统沙箱"。
- `native_view(engine, task_state)`：把任务记录中该端已持久化的字段投影成规范只读视图（session_id、cleanup、prompt_attempted、outcome_unknown、prompt_certificate）。缺失值保持 None/unknown，不从答复或错误文字推断。
- `resend_decision(engine, task_state)`：保守重发门——记录证明"未发送且会话状态安全"才允许；未知发送一律不重发。DSH/Pi 记录不含逐请求发送凭据，答案固定 `unproven`，重派仍由控制器原子认领与显式恢复决定。契约测试保证该门与 zcode/workbuddy 各自的 recovery_guard 方向一致（guard 拒绝处契约不批准；契约直接拒绝处 guard 也拒绝）。

`.local-worker/executor_adapter.py` 的能力表已如实扩展到四个执行端：workbuddy/zcode 的 dispatch/session/events/cancel/outcome 是真实链路能力（委托到各自 `run_task`）；usage 因原生账户不提供逐任务计量保持 unsupported（费用未知，不估算）；subagent 保持 denied。新增 `WorkerTaskAdapter`：除任务级 dispatch 外逐操作以 `CapabilityNotWired` 拒绝并指名所需委托，不假装存在按操作传输——这正是 P2 的接缝。

契约用例（`tests/test_executor_contract.py`）同一套覆盖四个执行端：生命周期映射、能力三元态、委托与证据文件名钉扎（job/proof/events/flag 静默改名会在此失败）、native_view 投影、重发门与 worker guard 一致性、共享派发接线（`panel_server.WORKER_SCRIPTS` 覆盖四端 worker）。

## 既有机制（P0 盘点结论，不重造）

- 派发链：`panel_server.execute_task`（认领/租约/资源/工作区准备）→ 四个 worker 子进程 → 各端原生桥（run-dsh.mjs / run-pi.mjs / NativeChatClient / run-zcode.mjs）→ `execution_result.Tracker` 阶段证据 → `task_finalization` 收尾 → `delegation_policy` 独立验收。
- 保护分层：`record-policy.cjs`（共享 Node 决策核心）被 record-protection.cjs（DSH 插件）、endpoint-access.cjs（Pi）、workbuddy-native-guard.cjs（WorkBuddy 与 ZCode 共用）引用；Python 侧 `record_protection.validate_files`/`verify_proof` 与各端 Node 侧通过 revision 哈希互锁。认证文件与其他任务记录保护目前是各 guard 私有规则；PRIVATE_GLOB 在 zcode/workbuddy 两个 guard 各有一份——这是 P2 的收敛点，不是新问题。
- 收尾语义：`release_safe` 要求 worker 进程已确认退出，且 workbuddy/zcode 的原生 cleanup 为 closed/not_created；不满足则保留资源并落 `attention`，不自动重发。

## P2：ZCode 隔离试点 —— 变更边界（待复核，未实施）

目标（方案 §7 P2）：接入明确的执行根/临时目录与隔离策略；整合重复权限检查，保留关键保护。只动 ZCode 一端，不改四个执行器的整体结构。

### 允许修改

| 文件 | 范围 |
|---|---|
| `.local-worker/agent-runtime/workbuddy-native-guard.cjs`、`zcode-native-guard.cjs` | 收敛两份 PRIVATE_GLOB、denialCode/reasonCode 等重复实现到单一共享位置；固定 denial_rule 标识必须逐一保持 |
| `.local-worker/agent-runtime/record-policy.cjs`、`endpoint-access.cjs` | 仅当收敛需要；判定语义不变，revision 哈希机制照旧互锁 |
| `.local-worker/zcode_native_tools.py` | job 增加声明式任务临时目录等字段时，必须版本化（`MODE`/schema 提升），旧 job 拒绝自动升级 |
| `.local-worker/zcode_worker.py` | 执行根/临时目录接线；recovery_guard 与收尾语义不变 |
| `.local-worker/task_workspace.py`、`executor_contract.py` | 任务临时目录约定与契约字段扩展（新字段可选、缺省保守） |
| `.local-worker/tests/*`、`docs/ZCODE_EXECUTOR.md` | 契约/守卫/隔离回归与文档同步 |

### 明确不做

- 不删除或弱化任何守卫来让失败通过；sticky 拒绝语义、`denied.flag` 机制、探针自检全部保留。
- 不改四个执行端的默认权限、访问模式与用户设置；不迁移旧任务快照；旧 `zcode_tools.py` MCP 模式只保留历史兼容，不动。
- 不改 WorkBuddy/DSH/Pi 的 worker 结构（P4 范围）；不部署、不发布。
- 不以任何形式宣称 worktree 或 hook 等于操作系统沙箱；终端动态脚本无法证明的目标继续保留未知边界。

### 候选设计点（供复核）

1. 副本内任务临时目录：job 声明 worktree 内专用临时目录，guard 对该固定路径内的写与清理按参数级检查放行；目录外的规则一字不改。
2. 权限检查收敛：重复实现合并后，`test_zcode_native_guard.cjs`、`test_native_profile_boundary.cjs`、`test_record_protection.cjs`、`test_record_protection_runtime.cjs` 必须全量保持通过，作为"收敛不降级"的证据。
3. 受保护根的终端词法缺口（P0 盘点发现，已用记录性 fixture 固化现状）：`referencesProtectedRoot` 是纯词法匹配，指向受保护原树的 junction 别名在终端命令中不匹配；完整 job 形态下由隔离层按 `realpathDeep` 解析拒绝（`workspace_isolation`），但对非 record 的受保护根，终端递归删除经别名访问暂无判定层拦截（`test_native_profile_boundary.cjs` 记录用例）。P2 若收紧，须同步更新该记录用例。
4. P1 接缝接线：zcode 的 per-operation 绑定从 unavailable 升级为 direct（读 zcode 形状的事件/成果），仅在契约测试同步扩展后进行。

### P2 验收清单（可判定）

- [x] 原生 Read/Write/Edit/Bash 在任务副本内可用；声明临时目录内的测试材料写/清理可用——离线证据：`test_zcode_native_guard.cjs`（20 例，含临时目录放行与保护不变）、`test_zcode_native_tools.py`（隔离任务声明/非隔离不声明）、`test_task_dispatch.py`（交付提交排除临时目录）。
- [x] 原树（含 junction 别名）、`.model-desk`、认证文件、其他任务 `.local-worker/tasks|logs` 仍被拒；现有全部守卫 fixture 保持通过——`test_native_profile_boundary.cjs` 10 例、`test_zcode_native_guard.cjs` 20 例、`test_record_protection.cjs`、`test_endpoint_access.cjs`、`test_native_workspace_binding.py` 零修改通过。
- [x] 只读 chat 仍无写入/终端；最终模式漂移仍拒绝——`test_zcode_session_contract.cjs` 8 例（含只读漂移 fixture）。
- [x] 收敛后每个固定 denial_rule 标识不变；事件行仍不记录原始命令/路径——守卫 fixture 零修改通过即证据。
- [x] job/契约字段版本化；legacy 模式与旧记录不受影响——`temp_dir` 由加载器钉扎为工作台常量（`test_zcode_native_tools.py` 篡改用例）；`test_zcode_integration.py`、`test_zcode_parity.py` 全绿。
- [x] `test_executor_contract.py` 全绿；job/绑定变化时 native_view/resend 契约同步更新（ZCode 直连绑定已同步钉扎）。
- [x] 真实 ZCode 3.16.3 原生任务（副本内改文件+临时文件+自测命令）单独验证并记录；离线检查不得替代。2026-10-26 Codex独立复核：精确命令Post退出0、业务与临时文件工具回执、独立复跑、冻结提交排除临时材料、保留scratch验收绑定均通过。原始失败保留；真机证据在本机隔离诊断目录、不入Git。此结论不包括生产全局队列、宿主UI或安装版。

**未接线决策项**：junction 解析匹配候选是否接线进共享 guard 的 shells 分支（方案、兼容性影响与正反测试见 [SHELL_GUARD.md](SHELL_GUARD.md)），属权限边界变化，须单独决策。

## 验证边界

2026-10-26 任务规则第三批源码候选补充：四端复用固定拒绝分类；DSH/Pi proof 增加首拒 guard_denial，普通工具错误没有该字段，不凭错误文本判保护失败。DSH/Pi runner 的正面发送尝试 + 缺失终态分类为结果未知，但不新增未发送证书，也不改变上面的 resend 决策或资源恢复授权。新 schema=2 的原生自测精确缺证据只影响验证结论，旧 schema/旧工具模式仍严格；实际非零自测、真实拒绝和未确认收尾仍不通过。详见 [任务级契约](TASK_EXECUTION_POLICY.md)。未发送真实模型请求、未部署或推送。

2026-10-26 P4 首批候选将声明临时目录及保留材料验收推广至 WorkBuddy 原生隔离 edit。共用 `task_workspace.task_temp_path` 与既有提交排除；ZCode 原 API 保留。WorkBuddy 保护指纹新增工具/访问模式/目录契约，加载器同时核对任务保存的当前原生模式与版本；冻结验收要求有效 job、非空父会话 proof、真实隔离根及普通未跟踪材料。旧模式/过期版本/空父会话三组独立反例已补齐，链接、业务脏改和 tracked/staged 临时文件仍拒绝，回收不自动清理。

197 项 Python、37 项 Node 增量与当前 WorkBuddy 模型的一次限定原生闭环通过；此前回执未知任务只做定向关闭、资源收尾与主脑接管，原始失败不改写。DSH/Pi 临时目录推广、生产故障恢复与两宿主 GUI 未因此完成。详见[WorkBuddy](WORKBUDDY_EXECUTOR.md)与[首批交接](RELEASE_20261103.md)。

原开发自测、后续独立复核及限定范围真机结果分别记录。P2真实文件任务与P3定向会话收尾已按上述范围验证；桌面GUI人工观察、生产队列故障恢复、两宿主端到端和部署后的表现仍未验证。源码候选通过不等于运行版已更新。
