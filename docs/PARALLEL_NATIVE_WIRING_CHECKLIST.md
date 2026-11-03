# 原生引擎工作区接线清单（调研记录，2026-10-25）

## 实施进度（2026-10-25）

以下清单保留为原始接线依据；候选现已接通 WorkBuddy/ZCode 的原生工具工作区。
`task_workspace.native_roots` 验证原记录目录、任务专用分支、工作区位置及 Git common-dir；
`execution_files` 把原项目绝对声明映射为副本内相对路径，不修改记录中的原始范围。
DSH/Pi 同样使用严格根校验，DSH 不再把 state.project 写成执行目录。

原生 job/proof 增加 split 时的 record_project。WorkBuddy 会话 cwd、ZCode workspacePath
及 .zcode profile 使用执行副本，job/proof/events、ZCode 会话索引和 config 锁仍留原项目。
ZCode .zcode/config.json 是原生读取所需的副本配置，仍排除于成果提交；没有把业务文件
放进 .model-desk。Python 和 Node 同时拒绝错绑、丢失副本、目录别名和 Git 身份漂移。

隔离保护独立于 full/project：原项目和其他任务副本的显式文件/终端路径不能因完全访问
而放开；本任务副本和必要的外部普通临时文件按原权限处理。保护版本已包含共享
endpoint-access.cjs；旧任务不会自动换版本。任意程序动态计算的路径仍不是 OS 沙箱。

包含原项目绝对路径的指定测试命令保守保持串行，不静默改写；已有隔离计划遇到此
命令则拒绝启动。隔离任务不复用旧原生会话，默认新会话续接/整改继续有效。

已跑 186 项相关增量检查（1 项需要安装版 DSH 的检查跳过），包括原生 job/hook、
两主脑公共派发、DSH/Pi 准备、绝对路径、原树保护和错误绑定；新增隔离检查 14 项。
Node 原生守卫 17 项及 endpoint access 断言通过。未执行真实模型任务；未部署，
不据此声称各桌面入口原生并行已通过。Aider/direct 编辑工作区仍未接入。

阶段交接的 worktree 成果证据由另一项正式 ZCode 任务处理，尚需主脑独立验收。

---

只读调研结论，供后续批次把 WorkBuddy/ZCode（及 Aider）接入任务工作区时使用。
参照模式：`dsh_worker.py` / `pi_worker.py` 调用 `task_workspace.execution_roots(state)`
拆分**记录项目**（state['project']，拥有记录/会话/索引）与**执行根**（任务
worktree，承载文件工作、guard、前后摘要）。`run` 目录永远通过
`project_storage.runs_dir` 位于记录项目下——下文所有 `run == project/'.model-desk/runs'/id`
断言都以此为前提。工作区隔离计划目前只对 `engine in ('dsh','pi') and kind=='edit'` 生成
（`panel_server.workspace_plan_for`）。

## workbuddy_worker.py

- `:185` validate_task 要求 body['project'] 绝对路径且存在——**记录身份**（派发时对记录项目）。
- `:255` `project = Path(state['project']).resolve()`——**拆分点**。
- `:256-257` `run.parent != project/'.model-desk'/'runs'` → "任务记录不属于本项目"——**断言会破**。
- `:260` validate_files、`:261` before 摘要、`:444` after 摘要——**执行侧**。
- `:289-290` `native_tools.prepare(run, project, ...)`——run 记录侧、project 执行侧（guard 范围）。
- `:308` 旧 `tools.project != project` 检查；`workbuddy_tools.py:58-59` 内部断言——**断言会破**。
- `:312` 旧 prepare 的 job 嵌 `'project'`（workbuddy_tools.py:177）——**执行侧**（范围）。
- `:333` `bridge.create_client(..., project, ...)`——**执行侧**：`workbuddy_host_bridge.py:262-271`
  要求目录存在且 lease cwd == project；`:282` 原生宿主 `cwd=project`；
  `workbuddy_executor.py:233-259,474` 的 `self.project` 是进程 cwd 与 session/new cwd，
  必须等于 `native_job['project']` / `tools.project`。
- 控制器弄脏项目：**无**。WorkBuddy hook 以 `--settings` CLI JSON 下发
  （workbuddy_executor.py:138-142），不写入项目；job/proof/events 全在 run 下。

## workbuddy_native_tools.py

- `:95-98` prepare 的 run↔project 断言——**断言会破**。
- `:101-107` validate_files 与逐文件包含检查——**执行侧**。
- `:108` job 字段 `'project'`——guard 全部范围判断基于它（**执行侧值**），但见 guard 断言。
- `:110` `protected_roots` 来自 config['profile']——**与项目无关，无需改**。
- job/proof/events 路径（:114/:151/:157）——**记录侧，无需改**。
- `:123-124` `load()` 从 `data['project']` 重导出 project 并断言 job 文件位置——**断言会破**。
- `:135-145` settings()：hook 命令只嵌 job 文件路径——**记录侧**。
- `agent-runtime/workbuddy-native-guard.cjs:125` 硬断言
  `run === job.project/.model-desk/runs/task_id`——**断言会破（Node guard 内，仅改 Python 不够）**；
  全部范围判断按 `job.project` 解析（:61,70-74,83,92,101,114,131,144,148）——**执行侧**。

## zcode_worker.py / zcode_native_tools.py

- `zcode_worker.py:277` 拆分点；`:285` run↔project 断言——**断言会破**。
- `:299` before、`:379` after 摘要——**执行侧**；`:301` protected roots 与项目无关。
- `:312-314` driver job `'project'`——**执行侧**：`run-zcode.mjs:52` 以它为会话
  `workspacePath`、`:70` 加载项目级 hook、`:60` 断言 profile 路径 ==
  `job.project/.zcode/config.json`。
- `zcode_native_tools.py:75` run↔project 断言——**断言会破**；`:80-82` 文件校验/包含——**执行侧**。
- `:83` `install_profile(project)`——**会弄脏 worktree**：写 `project/.zcode/config.json`
  （:36,:68）与 `project/.model-desk/zcode-native-config.lock`（:45）；hook 声明
  `args=[node, GUARD, str(project)]` 把项目路径嵌进 guard argv。
- `:92-98` 会话索引 `project/'.model-desk/zcode-native-sessions.json'` + 锁——用途是
  **记录身份**，但若 project 变成 worktree 会**弄脏 worktree**，必须显式保留在记录项目。
- `:104-114` `load()`：`:105` job 文件位置断言、`:110` profile 路径、`:112` 读会话索引——
  前两者**断言会破**，索引为**记录身份**。
- `agent-runtime/zcode-native-guard.cjs:78-79` 硬断言 `run === job.project/.model-desk/runs/task_id`
  ——**断言会破（Node guard 内）**；范围判断按 `job.project` 解析（:97,105）。
- 旧适配 `zcode_tools.py:39,91` 同型断言、`:149` 工具服务 cwd——同批处理。

## run_worker.py（Aider/direct 编辑）

- panel 以命令行传 `--project state["project"]`（panel_server.py:1085）——边界处即**记录身份**；
  拆分需控制器传执行根或让 worker 读取 state 的 workspace 计划。
- `:63` 拆分点；`:68-77` 文件解析/校验——**执行侧**；`:97-98` run 目录无断言——**记录侧**。
- `:103-107` `patch_task(project=...)`——**记录身份**（需独立字段，仿 DSH/Pi 的 workspace）。
- `:152-153` git toplevel 判断、`:160-161` Aider cwd——**执行侧**（worktree 下自然正确）。
- `:175` result.json 嵌 project——**记录身份**（报告元数据）。
- Aider 以 `--no-auto-commits --no-dirty-commits` 运行；**无控制器弄脏**。

## chat_worker / 共享模块

- chat_worker / run_chat：不读项目——**无需接线**。
- execution_engines.py：无路径逻辑，无需改。
- project_storage.py：全部**记录身份**（runs_dir/task_dir/records_db/locator）——拆分的不变量来源。
- context_manager：`default_index_path(run)` 在 run/context 下（记录侧）；`ensure_index` 的
  project 参数是被描述的树——zcode/workbuddy worker 不调 ensure_index，无需接线。

## 硬阻断汇总

1. ZCode：`zcode_worker.py:285`、`zcode_native_tools.py:75,105,110`、`zcode_tools.py:39,91`、
   `zcode-native-guard.cjs:78-79,60`——job.project 与 run/profile 绑定。
2. WorkBuddy：`workbuddy_worker.py:256`、`workbuddy_native_tools.py:97,124`、
   `workbuddy_tools.py:58-59`、`workbuddy-native-guard.cjs:125`——同型绑定。
3. 两者的绑定都在 **Node guard 的每次 hook 事件**里强制执行，只改 Python 不足够；
   需要为 job 增加"记录项目 vs 执行根"两个路径并同步修改 guard 断言与校验链，
   属于跨语言结构改动，单独成批并做真实会话验证。
4. ZCode 专属脏区：install_profile 与会话索引会写进执行根——除提交排除外，
   release 的脏区保护会把它们当成果拦截；接线时这两类写入必须显式改到记录项目。
