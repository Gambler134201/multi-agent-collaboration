"""Offline tests for the E2 observability/capability panel surface.

Every check runs against a **temporary** registry (HERE/REGISTRY/RUNS) and an
in-process panel HTTP server: no model call, no paid generation, no subagent is
started, the real running panel is never restarted, and no real record is
touched.  Dispatch is exercised with ``EXECUTOR.submit`` mocked, exactly like
``test_stage_workflow`` does.
"""
import json
import sqlite3
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock
from urllib.error import HTTPError
from urllib.request import Request, build_opener, ProxyHandler

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import codex_mcp_server as desk
import delegation_policy
import executor_adapter
import model_capabilities as caps
import model_registry as registry
import panel_server as panel
import stage_store
import workflow_settings
import project_scope

class ReviewerPolicyToggleTests(unittest.TestCase):
    def test_native_child_model_comes_from_lifecycle_not_guessed_parent(self):
        task={'model':'parent','model_id':'parent-id','subagent_events':[{'type':'start','source':'dsh-subagent-lifecycle','child_id':'c','child_model':'actual-child','provider':'spawn','local':True}]}
        child=panel._child_rows(task)[0]
        self.assertEqual(child['model'],'actual-child')
        self.assertEqual(child['model_name'],'actual-child')
        self.assertIsNone(child['model_id'])

    def test_verified_denied_model_can_be_manually_reenabled(self):
        row={'id':'cloud','executor':'dsh','enabled':True,'local':False,'state':'supported','policy':'deny','decision':'deny','proven_modes':['chat']}
        with mock.patch.object(panel,'capability_rows',return_value=[]), mock.patch.object(registry,'capability_models',return_value=[]), mock.patch.object(executor_adapter,'capabilities_view',return_value={'models':[row]}):
            result=panel.subagent_capabilities()['models'][0]
        self.assertTrue(result['allowable'])
        self.assertEqual(result['decision'],'deny')


class Base(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.saved = registry.HERE, registry.REGISTRY, registry.RUNS
        registry.HERE = Path(self.temp.name)
        registry.REGISTRY = registry.HERE / "models.json"
        registry.RUNS = registry.HERE / "runs"
        self.project = registry.HERE / "project"
        self.project.mkdir()
        (self.project / "a.py").write_text("def one():\n    return 1\n", encoding="utf-8")
        (self.project / "b.py").write_text("def two():\n    return 2\n", encoding="utf-8")
        self.write_models()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), panel.Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        self.opener = build_opener(ProxyHandler({}))

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        registry.HERE, registry.REGISTRY, registry.RUNS = self.saved
        self.temp.cleanup()

    # -- fixtures ---------------------------------------------------------
    def capability_identity(self, model):
        return executor_adapter.model_identity({
            "model": model, "agent_preset": "standard",
            "api_base": "https://api.deepseek.com", "model_version": ""})

    def write_models(self):
        cloud_identity = self.capability_identity("deepseek-flash")
        home_identity = executor_adapter.model_identity({
            "model": "qwen-test", "agent_preset": "native",
            "api_base": "http://127.0.0.1:1234/v1", "model_version": ""})
        registry.write_json(registry.REGISTRY, {
            "default_model": "home",
            "models": [
                {"id": "home", "name": "家中的 Qwen", "provider": "local",
                 "api_base": "http://127.0.0.1:1234/v1", "model": "qwen-test",
                 "agent_preset": "native", "enabled": True, "bypass_proxy": True,
                 "max_input_tokens": 32768, "max_output_tokens": 4096,
                 "model_version": "",
                 "subagent": {"schema": 1, "policy": "allow", "evidence": [{
                     "state": "supported", "measured": True, "modes": ["edit"],
                     "identity": home_identity, "tested_at": caps._now().isoformat(),
                     "test_id": "home-probe", "source": "fixture"}]}},
                {"id": "home2", "name": "本地第二个", "provider": "local",
                 "api_base": "http://127.0.0.1:1235/v1", "model": "qwen-second",
                 "agent_preset": "native", "enabled": True, "bypass_proxy": True,
                 "max_input_tokens": 32768, "max_output_tokens": 4096,
                 "model_version": ""},
                {"id": "cloud", "name": "云端模型", "provider": "deepseek",
                 "api_base": "https://api.deepseek.com", "model": "deepseek-flash",
                 "agent_preset": "standard", "enabled": True, "bypass_proxy": True,
                 "max_input_tokens": 1000000, "max_output_tokens": 64000,
                 "model_version": "",
                 "subagent": {"schema": 1, "policy": "allow", "evidence": [{
                     "state": "supported", "measured": True, "modes": ["chat"],
                     "read_only_probe": True, "identity": cloud_identity,
                     "tested_at": caps._now().isoformat(),
                     "executor_version": "0.2.0-rc.2", "test_id": "cloud-probe",
                     "source": "main verified read-only probe"}]}},
                {"id": "blank", "name": "未验证模型", "provider": "custom",
                 "api_base": "https://example.invalid/v1", "model": "unknown-model",
                 "agent_preset": "standard", "enabled": True, "model_version": ""},
            ]})

    def task(self, name, **fields):
        registry.patch_task(registry.task_dir(name), id=name, title="任务 " + name, kind="chat",
                            engine="dsh", model_id="home", model_name="家中的 Qwen",
                            model="qwen-test", status="completed", phase="回复完成",
                            created_at=registry.now())
        if fields:
            registry.patch_task(registry.task_dir(name), **fields)
        return name

    def real_review(self, name, state="accepted", criteria=("c1",)):
        """Write a real delegation review record for a task.

        The panel must read this file; a raw ``task.json`` review_state alone is
        only a cache and is never evidence of acceptance.
        """
        run = registry.task_dir(name)
        registry.patch_task(run, delegation={"schema": delegation_policy.SCHEMA, "mode": "outcome",
                                             "max_auto_rework": delegation_policy.MAX_AUTO_REWORK,
                                             "rework_count": 0})
        delegation_policy.initialize(run, list(criteria), "main")
        delegation_policy.record_executor(run, {"completed": state != "failed", "changed_files": []})
        if state == "accepted":
            rows = [{"id": "c" + str(index + 1), "status": "pass",
                     "evidence": [{"source": "reviewer_inspection", "detail": "独立检查 fixture"}]}
                    for index in range(len(criteria))]
            return delegation_policy.accept(run, "main", rows, "独立验收通过")
        return delegation_policy.load(run)

    def request(self, path, body=None, token=True, origin=None, method=None):
        headers = {"Content-Type": "application/json",
                   'X-Model-Desk-View':project_scope.encode_view_header({'mode':'all'})}
        if token:
            headers["X-Panel-Token"] = panel.TOKEN
        if origin:
            headers["Origin"] = origin
        data = json.dumps(body).encode() if body is not None else None
        request = Request(self.url + path, data=data, headers=headers, method=method)
        with self.opener.open(request, timeout=10) as response:
            return json.load(response)


class RetiredReserveClientTests(Base):
    def test_live_api_omits_fields_consumed_by_already_open_old_clients(self):
        name = self.task('old-client', budget_reserved_cny=135.623928,
                         cost_legacy_unsettled_cny=135.623928)
        daily = {'settled_cny':12.0, 'settled_calls':3, 'reserved_cny':900,
                 'legacy_reserved_cny':135.623928, 'unsettled_cny':0.5,
                 'unknown_calls':1, 'remaining_cny':8, 'unresolved':True}
        with mock.patch.object(panel.budget_relay, 'daily_state', return_value=daily), \
             mock.patch.object(panel.deepseek_billing, 'enrich_models', side_effect=lambda rows: rows), \
             mock.patch.object(panel.codex_usage, 'usage_state', return_value={'status':'unavailable'}):
            state = self.request('/api/state')
            workflow = self.request('/api/workflow')
            detail = self.request('/api/tasks/' + name)['task']
        for result in (state['workflow']['daily'], workflow['daily']):
            self.assertNotIn('legacy_reserved_cny', result)
            self.assertNotIn('reserved_cny', result)
            self.assertEqual(result['settled_cny'],12)
            self.assertEqual(result['unsettled_cny'],0.5)
            self.assertEqual(result['unknown_calls'],1)
        for row in (next(t for t in state['tasks'] if t['id']==name), detail):
            self.assertNotIn('budget_reserved_cny',row)
            self.assertNotIn('cost_legacy_unsettled_cny',row)
            self.assertFalse(panel._cost_facts(row)['known'])
        # Neither projection overwrites the source nor turns missing fees into 0.
        self.assertEqual(daily['legacy_reserved_cny'],135.623928)
        raw=registry.read_task(registry.task_dir(name))
        self.assertEqual(raw['budget_reserved_cny'],135.623928)
        self.assertEqual(raw['cost_legacy_unsettled_cny'],135.623928)
        self.assertNotIn('cost_settled_cny',raw)


class PanelSnapshotTests(Base):
    def test_polling_omits_audit_histories_but_detail_and_records_keep_them(self):
        tools = [{'id': str(i), 'title': 'read', 'status': 'completed'} for i in range(60)]
        audit = {'cost_requests': [{'request_id': str(i), 'note': 'x' * 1024}
                                   for i in range(100)],
                 'context_outputs': [{'preview': 'original evidence'}],
                 'meter_record': {'identity': 'original meter'},
                 'usage_calibration': {'observed': 13},
                 'preset_proof': {'verified': True}, 'context_index': {'path': 'index'}}
        name = self.task('polling-snapshot', tool_calls=tools, answer='full answer',
                         summary='full summary', cost_settled_cny=1.25,
                         cost_settled_calls=2, cost_unknown_calls=1, **audit)
        self.real_review(name)
        run = registry.task_dir(name)
        before = (run / 'task.json').read_bytes()
        with mock.patch.object(panel.deepseek_billing, 'enrich_models', side_effect=lambda rows: rows), \
             mock.patch.object(panel.codex_usage, 'usage_state', return_value={'status': 'unavailable'}):
            snapshot = next(t for t in self.request('/api/state')['tasks'] if t['id'] == name)
            detail = self.request('/api/tasks/' + name)['task']
        for field, value in audit.items():
            self.assertNotIn(field, snapshot)
            self.assertEqual(detail[field], value)
        self.assertEqual(snapshot['tool_calls'], tools[-8:])
        self.assertEqual(detail['tool_calls'], tools)
        self.assertEqual(snapshot['review']['state'], 'accepted')
        for field in ('answer', 'summary', 'cost_settled_cny', 'cost_settled_calls', 'cost_unknown_calls'):
            self.assertEqual(snapshot[field], detail[field])
        self.assertLess(len(json.dumps(snapshot)), len(json.dumps(detail)) / 5)
        self.assertEqual((run / 'task.json').read_bytes(), before)
        self.assertEqual(next(t for t in panel.public_tasks() if t['id'] == name)['cost_requests'],
                         audit['cost_requests'])


class StageKindTests(Base):
    def test_default_kind_is_edit_and_explicit_chat_persists(self):
        goal = stage_store.create_goal("目标", "持久 kind", str(self.project))
        legacy = stage_store.add_stage(goal["id"], "默认阶段", "默认 edit",
                                       files=["a.py"], acceptance=["c1"])
        self.assertEqual(legacy["kind"], stage_store.KIND_EDIT)
        self.assertEqual(stage_store.stage_view(legacy["id"])["kind_label"],
                         stage_store.KIND_LABELS[stage_store.KIND_EDIT])
        chat = stage_store.add_stage(goal["id"], "只读调查", "只读分析阶段", kind="chat",
                                     acceptance=["c2"])
        self.assertEqual(chat["kind"], "chat")
        self.assertEqual(stage_store.get_stage(chat["id"])["kind"], "chat")
        with self.assertRaises(ValueError):
            stage_store.add_stage(goal["id"], "坏类型", "x", kind="full")

    def test_old_database_migrates_to_edit(self):
        # Build the pre-kind schema the way an older release wrote it, then let
        # the store open it: the migration must add ``kind`` with ``edit``.
        db = stage_store.db_path()
        conn = sqlite3.connect(str(db))
        conn.executescript('''
            CREATE TABLE stages (
                id TEXT PRIMARY KEY, goal_id TEXT NOT NULL, ordinal INTEGER NOT NULL,
                title TEXT NOT NULL, objective TEXT NOT NULL DEFAULT '',
                project TEXT NOT NULL DEFAULT '', files TEXT NOT NULL DEFAULT '[]',
                acceptance TEXT NOT NULL DEFAULT '[]', candidates TEXT NOT NULL DEFAULT '[]',
                fixed_model TEXT NOT NULL DEFAULT '', depends_on TEXT NOT NULL DEFAULT '[]',
                status TEXT NOT NULL DEFAULT 'pending', session_policy TEXT NOT NULL DEFAULT 'new_session',
                session_key TEXT NOT NULL DEFAULT '', task_ids TEXT NOT NULL DEFAULT '[]',
                active_task_id TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL, data TEXT NOT NULL DEFAULT '{}');
        ''')
        conn.execute("INSERT INTO stages (id, goal_id, ordinal, title, objective, files, acceptance, "
                     "status, session_policy, created_at, updated_at) VALUES "
                     "('old-stage', 'old-goal', 1, '旧阶段', '旧记录', '[]', '[\"c\"]', "
                     "'ready', 'new_session', '2026-01-01T00:00:00+08:00', '2026-01-01T00:00:00+08:00')")
        conn.commit()
        conn.close()
        rows = stage_store.list_stages()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["kind"], stage_store.KIND_EDIT)
        self.assertEqual(stage_store.stage_view("old-stage")["kind"], stage_store.KIND_EDIT)
        with stage_store.connection() as check:
            columns = {row["name"] for row in check.execute("PRAGMA table_info(stages)")}
        self.assertIn("kind", columns)

    def test_public_goal_stage_api_accepts_kind(self):
        goal = self.request("/api/goals", {"title": "API 目标", "objective": "kind",
                                           "project": str(self.project)})
        stage = self.request("/api/stages", {"goal_id": goal["id"], "title": "只读阶段",
                                             "objective": "调查", "kind": "chat",
                                             "acceptance": ["c1"]})
        self.assertEqual(stage["kind"], "chat")
        self.assertEqual(self.request("/api/stages/" + stage["id"])["kind_label"], "只读分析 · chat")


class StageDispatchAuthorityTests(Base):
    def make_stage(self, title, kind, **extra):
        goal = stage_store.create_goal("派发目标", "权限", str(self.project))
        body = {"goal_id": goal["id"], "title": title, "objective": "deliver",
                "acceptance": ["c1"], "candidates": ["home"], "fixed_model": "home",
                "kind": kind}
        body.update(extra)
        return goal, stage_store.add_stage(**body)

    def dispatch(self, stage, body):
        with mock.patch.object(panel.EXECUTOR, "submit"), \
             mock.patch.object(panel.dsh_worker, "availability", return_value={"ready": True}):
            return self.request(f"/api/stages/{stage['id']}/dispatch", body)

    def test_request_kind_cannot_promote_or_demote_stage(self):
        _, chat_stage = self.make_stage("只读阶段", "chat")
        result = self.dispatch(chat_stage, {"prompt": "分析", "source": "codex", "kind": "edit"})
        self.assertEqual(result["task"]["kind"], "chat")
        self.assertEqual(result["task"]["files"], [])

        _, edit_stage = self.make_stage("实现阶段", "edit", files=["a.py"])
        result = self.dispatch(edit_stage, {"prompt": "实现", "source": "codex", "kind": "chat"})
        self.assertEqual(result["task"]["kind"], "edit")
        self.assertEqual(result["task"]["files"], ["a.py"])

    def test_caller_routing_cannot_replace_stage_acceptance_or_candidates(self):
        goal = stage_store.create_goal("权限目标", "routing", str(self.project))
        stage = stage_store.add_stage(goal["id"], "只读阶段", "obj", kind="chat", files=["a.py"],
                                      acceptance=["c1"], candidates=["home"])
        result = self.dispatch(stage, {
            "prompt": "分析", "source": "codex", "route_mode": "manual",
            "routing": {"acceptance": ["调用者伪造的验收条件"],
                        "eligible_model_ids": ["cloud"],
                        "category": "analysis", "complexity": "simple"}})
        requirements = result["task"]["route"]["requirements"]
        # The persisted stage conditions win; the caller's replacements are gone.
        self.assertEqual(requirements["acceptance"], ["c1"])
        self.assertNotIn("调用者伪造的验收条件", requirements["acceptance"])
        self.assertEqual(requirements["eligible_model_ids"], ["home"])
        # The override is visible and durable, never a silent replacement.
        self.assertEqual(sorted(result["stage_policy"]["ignored_request_fields"]),
                         ["acceptance", "eligible_model_ids"])
        self.assertEqual(result["stage_policy"]["acceptance_source"], "stage_store")
        self.assertEqual(result["stage_policy"]["candidate_source"], "stage_candidates")
        stored = json.loads((registry.task_dir(result["task"]["id"]) / "task.json").read_text(encoding="utf-8"))
        self.assertEqual(stored["stage_policy"]["ignored_request_fields"],
                         ["acceptance", "eligible_model_ids"])

    def test_fixed_model_wins_and_explicit_manual_model_is_recorded(self):
        _, fixed = self.make_stage("固定模型", "chat")  # make_stage fixes home
        result = self.dispatch(fixed, {"prompt": "x", "source": "codex", "model_id": "cloud"})
        self.assertEqual(result["task"]["model_id"], "home")
        self.assertEqual(result["stage_policy"]["candidate_source"], "stage_fixed_model")

        goal = stage_store.create_goal("手动目标", "manual", str(self.project))
        stage = stage_store.add_stage(goal["id"], "手动阶段", "obj", kind="chat", acceptance=["c1"],
                                      candidates=["home"])
        result = self.dispatch(stage, {"prompt": "x", "source": "codex", "route_mode": "manual",
                                       "model_id": "home2"})
        # An explicit manual model stays an explicit, recorded exception; the
        # automatic candidate set is not silently expanded.
        self.assertEqual(result["task"]["model_id"], "home2")
        self.assertEqual(result["stage_policy"]["candidate_source"], "explicit_manual")
        self.assertEqual(result["stage_policy"]["manual_model"], "home2")
        self.assertTrue(result["stage_policy"]["manual_reason"])
        self.assertEqual(result["task"]["route"]["requirements"]["eligible_model_ids"], ["home2"])

    def test_chat_stage_keeps_readonly_context_files_in_scope(self):
        goal = stage_store.create_goal("上下文目标", "files", str(self.project))
        chat = stage_store.add_stage(goal["id"], "只读调查", "obj", kind="chat",
                                     files=["a.py"], acceptance=["c1"], candidates=["home"])
        result = self.dispatch(chat, {"prompt": "分析", "source": "codex", "route_mode": "manual"})
        task = result["task"]
        # The configured context files still reach the index/handover scope ...
        self.assertEqual(task["files"], ["a.py"])
        self.assertEqual(task["readonly_files"], ["a.py"])
        self.assertTrue(task["files_readonly"])
        # ... while the read-only permission itself comes from the chat sandbox.
        self.assertEqual(task["kind"], "chat")
        view = self.request("/api/tasks/%s/observability" % task["id"])
        self.assertEqual(view["file_scope"]["mode"], "readonly_context")
        self.assertEqual(view["file_scope"]["readonly_files"], ["a.py"])

        edit = stage_store.add_stage(goal["id"], "实现", "obj", kind="edit",
                                     files=["b.py"], acceptance=["c1"], candidates=["home"])
        result = self.dispatch(edit, {"prompt": "改", "source": "codex", "route_mode": "manual"})
        self.assertEqual(result["task"]["files"], ["b.py"])
        self.assertFalse(result["task"].get("readonly_files"))
        self.assertFalse(result["task"]["files_readonly"])

    def test_readonly_context_files_are_never_silently_dropped(self):
        # A missing context file is a loud refusal, not a silent empty scope.
        goal = stage_store.create_goal("缺失文件", "missing", str(self.project))
        missing = stage_store.add_stage(goal["id"], "阶段", "obj", kind="chat",
                                        files=["missing.py"], acceptance=["c1"], candidates=["home"])
        with self.assertRaises(HTTPError) as error:
            self.dispatch(missing, {"prompt": "x", "source": "codex", "route_mode": "manual"})
        self.assertEqual(error.exception.code, 400)
        self.assertIn("只读上下文文件", json.loads(error.exception.read().decode("utf-8"))["error"])

        # Files without a project can never reach the index; refuse instead.
        no_project = stage_store.create_goal("无项目", "no-project")
        orphan = stage_store.add_stage(no_project["id"], "阶段", "obj", kind="chat",
                                       files=["a.py"], acceptance=["c1"], candidates=["home"])
        with self.assertRaises(HTTPError) as error:
            self.dispatch(orphan, {"prompt": "x", "source": "codex", "route_mode": "manual"})
        self.assertEqual(error.exception.code, 400)

    def test_sync_refuses_a_review_decided_on_different_conditions(self):
        goal = stage_store.create_goal("同步目标", "sync", str(self.project))
        stage = stage_store.add_stage(goal["id"], "阶段", "obj", kind="chat", files=["a.py"],
                                      acceptance=["c1"], candidates=["home"])
        result = self.dispatch(stage, {"prompt": "x", "source": "codex", "route_mode": "manual"})
        task_id = result["task"]["id"]
        run = registry.task_dir(task_id)
        # A tampered/foreign record accepted against another condition set.
        self.real_review(task_id, criteria=("另一条验收条件",))
        self.assertEqual(delegation_policy.load(run)["state"], delegation_policy.ACCEPTED)
        synced = stage_store.sync_from_tasks()
        self.assertEqual(stage_store.get_stage(stage["id"])["status"], stage_store.EXECUTOR_COMPLETED)
        self.assertTrue(any(item.get("criteria_mismatch") for item in synced["changed"]))
        self.assertNotEqual(stage_store.get_stage(stage["id"])["status"], stage_store.ACCEPTED)

    def test_sync_accepts_a_review_matching_the_stage_conditions(self):
        goal = stage_store.create_goal("同步通过", "sync-ok", str(self.project))
        stage = stage_store.add_stage(goal["id"], "阶段", "obj", kind="chat", files=["a.py"],
                                      acceptance=["c1"], candidates=["home"])
        result = self.dispatch(stage, {"prompt": "x", "source": "codex", "route_mode": "manual"})
        self.real_review(result["task"]["id"], criteria=("c1",))
        stage_store.sync_from_tasks()
        self.assertEqual(stage_store.get_stage(stage["id"])["status"], stage_store.ACCEPTED)

    def test_cross_origin_and_missing_token_are_refused(self):
        _, stage = self.make_stage("只读", "chat")
        with self.assertRaises(HTTPError) as error:
            self.request(f"/api/stages/{stage['id']}/dispatch", {"prompt": "x"}, token=False)
        self.assertEqual(error.exception.code, 403)
        with self.assertRaises(HTTPError) as error:
            self.request(f"/api/stages/{stage['id']}/dispatch", {"prompt": "x"},
                         origin="https://evil.example")
        self.assertEqual(error.exception.code, 403)

    def test_dependent_stage_is_not_dispatchable_before_independent_acceptance(self):
        goal = stage_store.create_goal("依赖目标", "gate", str(self.project))
        first = stage_store.add_stage(goal["id"], "第一", "one", acceptance=["c"],
                                      candidates=["home"], fixed_model="home")
        second = stage_store.add_stage(goal["id"], "第二", "two", depends_on=[first["id"]],
                                       acceptance=["c"], candidates=["home"], fixed_model="home")
        with self.assertRaises(HTTPError) as error:
            self.dispatch(second, {"prompt": "x", "source": "codex"})
        self.assertEqual(error.exception.code, 400)
        self.assertIn("前置阶段", json.loads(error.exception.read().decode("utf-8"))["error"])
        self.assertEqual(stage_store.get_stage(second["id"])["status"], stage_store.PENDING)

    def test_dispatch_never_touches_user_limits(self):
        before = workflow_settings.snapshot()
        _, stage = self.make_stage("限制检查", "chat")
        self.dispatch(stage, {"prompt": "x", "source": "codex", "route_mode": "manual"})
        after = workflow_settings.snapshot()
        for field in ("enabled", "paid_allowed", "enforce_paid_budget",
                      "enforce_model_call_limit", "task_budget_cny", "daily_budget_cny",
                      "max_model_calls"):
            self.assertEqual(before.get(field), after.get(field), field)


class CapabilitySurfaceTests(Base):
    def test_states_policy_and_evidence_are_separate(self):
        view = panel.subagent_capabilities()
        rows = {row["model_id"]: row for row in view["models"]}
        self.assertEqual(view["default_verdict"], "deny")

        cloud = rows["cloud"]
        self.assertEqual(cloud["policy"], "allow")
        self.assertEqual(cloud["state"], "supported")
        self.assertEqual(cloud["decision"], "allow")
        self.assertEqual(cloud["proven_modes"], ["chat"])
        self.assertTrue(cloud["allowable"])
        self.assertEqual(cloud["evidence_detail"]["test_id"], "cloud-probe")
        self.assertEqual(cloud["evidence_detail"]["modes"], ["chat"])
        self.assertEqual(cloud["evidence_detail"]["version"], cloud["identity"]["version"])
        self.assertTrue(cloud["evidence_detail"]["tested_at"])

        home = rows["home"]
        self.assertTrue(home["local"])
        self.assertEqual(home["decision"], "deny")
        self.assertFalse(home["allowable"])
        self.assertTrue(home["worker_usable"])
        self.assertTrue(any("本地" in reason or "home" in reason for reason in home["reasons"]))

        blank = rows["blank"]
        self.assertEqual(blank["decision"], "deny")
        self.assertEqual(blank["state"], "unverified")
        self.assertFalse(blank["allowable"])
        self.assertTrue(any("实测证据" in reason for reason in blank["reasons"]))

    def test_edit_mode_is_not_claimed_measured_from_read_only_evidence(self):
        row = {"id": "cloud", "provider": "deepseek", "model": "deepseek-flash",
               "agent_preset": "standard", "api_base": "https://api.deepseek.com",
               "model_version": "",
               "subagent": {"policy": "allow", "evidence": [{
                   "state": "supported", "measured": True, "modes": ["chat"],
                   "read_only_probe": True,
                   "identity": self.capability_identity("deepseek-flash"),
                   "tested_at": caps._now().isoformat()}]}}
        chat = caps.evaluate(row, mode="chat")
        edit = caps.evaluate(row, mode="edit")
        self.assertEqual(chat["decision"], "allow")
        self.assertEqual(edit["decision"], "deny")
        self.assertEqual(edit["proven_modes"], ["chat"])
        self.assertTrue(any("未覆盖 edit" in reason for reason in edit["reasons"]))

    def test_state_endpoint_exposes_capability_evidence(self):
        state = self.request("/api/state")
        capabilities = state["executor_capabilities"]
        rows = {row["model_id"]: row for row in capabilities["models"]}
        self.assertIn("evidence_detail", rows["cloud"])
        self.assertIn("allowable", rows["home"])
        for phrase in ("支持状态", "允许派生", "本地/home 禁止派生"):
            self.assertIn(phrase, capabilities["note"])


class ObservabilityTests(Base):
    def test_missing_usage_stays_unknown_not_zero(self):
        self.task("empty-usage", usage={})
        view = self.request("/api/tasks/empty-usage/observability")
        self.assertFalse(view["provider_usage"]["available"])
        for field in ("input_tokens", "cache_hit_tokens", "output_tokens", "total_tokens"):
            self.assertIsNone(view["provider_usage"][field], field)
        self.assertEqual(view["provider_usage"]["source"], "未取得（服务商未返回 usage）")
        self.assertFalse(view["cost"]["known"])
        self.assertIsNone(view["cost"]["settled_cny"], "unknown amount is not a real ¥0")
        self.assertIn("未知", view["cost"]["note"])

    def test_settled_and_unknown_cost_are_separate(self):
        self.task("costs", cost_schema=2, cost_settled_cny=0.5, cost_settled_calls=2,
                  cost_unsettled_cny=0.0, cost_unknown_calls=1, cost_legacy_unsettled_cny=1.5)
        view = self.request("/api/tasks/costs/observability")
        cost = view["cost"]
        self.assertTrue(cost["known"])
        self.assertEqual(cost["settled_cny"], 0.5)
        self.assertEqual(cost["settled_calls"], 2)
        self.assertEqual(cost["unknown_calls"], 1)
        # Legacy reserves never enter the current cost view or billing boundary...
        self.assertNotIn("legacy_unsettled_cny", cost)
        self.assertNotIn("legacy_unsettled_cny", view["billing_boundary"]["parent"])
        # ... while the raw record keeps the historical value for compatibility.
        raw = json.loads((registry.task_dir("costs") / "task.json").read_text(encoding="utf-8"))
        self.assertEqual(raw["cost_legacy_unsettled_cny"], 1.5)

    def test_provider_usage_marks_partial_and_unknown_calls(self):
        self.task("partial-usage", usage={"prompt_tokens": 1000, "completion_tokens": 40,
                                          "total_tokens": 1040},
                  cost_settled_cny=0.5, cost_settled_calls=2, cost_unknown_calls=1)
        view = self.request("/api/tasks/partial-usage/observability")
        usage = view["provider_usage"]
        self.assertTrue(usage["available"])
        self.assertTrue(usage["partial"])
        self.assertEqual(usage["known_calls"], 2)
        self.assertEqual(usage["unknown_calls"], 1)
        self.assertIn("未知", usage["scope"])
        self.assertIn("不是全部调用或整个目标的总 tokens", usage["scope"])

        self.task("complete-usage", usage={"prompt_tokens": 10, "completion_tokens": 2,
                                           "total_tokens": 12}, cost_settled_calls=1)
        complete = self.request("/api/tasks/complete-usage/observability")["provider_usage"]
        self.assertFalse(complete["partial"])
        self.assertNotIn("未知", complete["scope"])

    def test_pending_cost_never_shows_reserve_zero_as_a_real_fee(self):
        self.task("pending-cost", cost_schema=2, cost_settled_cny=0.0, cost_settled_calls=0,
                  cost_unsettled_cny=0.0, cost_unknown_calls=1, budget_reserved_cny=0)
        view = self.request("/api/tasks/pending-cost/observability")
        cost = view["cost"]
        self.assertFalse(cost["known"])
        self.assertIsNone(cost["settled_cny"], "the unknown amount is null, never ¥0.00")
        self.assertEqual(cost["recorded_settled_cny"], 0.0)
        self.assertFalse(cost["reserved_is_real_cost"])
        self.assertIn("预留", cost["note"])
        self.assertIsNone(view["billing_boundary"]["parent"]["settled_cny"])

    def test_provider_usage_cache_and_meter_confidence(self):
        self.task("usage", usage={"prompt_tokens": 1000, "completion_tokens": 40,
                                  "total_tokens": 1040, "prompt_cache_hit_tokens": 900,
                                  "prompt_cache_miss_tokens": 100},
                  meter_source="json_utf8_byte_proxy", meter_precision="approximate",
                  meter_input_tokens=2000, meter_input_origin="content_class_byte_proxy",
                  meter_output_target=4096, meter_output_origin="explicit_max_tokens_task_limit",
                  meter_window_kind="split_input_output_windows", meter_window_verified=False,
                  meter_fit_state="unknown", context_schema=1, context_bytes_before=878009,
                  context_bytes_after=678143, context_bytes_saved=199866,
                  context_offloaded_count=9, context_offloaded_bytes=211254,
                  context_preview_bytes=1200,
                  context_note="context_* 是上下文外置的字节审计，不等于 token 精确节省")
        view = self.request("/api/tasks/usage/observability")
        usage = view["provider_usage"]
        self.assertTrue(usage["available"])
        self.assertEqual(usage["input_tokens"], 1000)
        self.assertEqual(usage["cache_hit_tokens"], 900)
        self.assertEqual(usage["cache_miss_tokens"], 100)
        self.assertEqual(usage["output_tokens"], 40)
        meter = view["meter"]
        self.assertEqual(meter["confidence"], "approximate")
        self.assertEqual(meter["source"], "json_utf8_byte_proxy")
        self.assertFalse(meter["window_verified"])
        self.assertEqual(meter["output_target"], 4096)
        context = view["context"]
        self.assertEqual(context["offloaded_count"], 9)
        self.assertEqual(context["bytes_saved"], 199866)
        self.assertIn("不等于 token", context["note"])

    def test_prediction_interval_and_forecast_are_marked_as_prediction(self):
        self.task("forecast", route={
            "mode": "auto", "reason": "自动选择", "estimated_cost_cny": 3.2239,
            "requirements": {"category": "code_multi", "complexity": "complex",
                             "input_tokens": 65000, "output_tokens": 7000, "expected_calls": 24,
                             "capacity": {"estimate_semantics": "approximate"}},
            "failure_risk": {"verified_samples": 7, "confidence": "medium",
                             "quality_rework_rate": 0.43,
                             "interval": {"low": 1.15, "high": 1.74, "method": "wilson_95"},
                             "basis": "verified_independent_review"},
            "cost": {"prediction": {"estimated_cost_cny": 3.2, "interval_low_cny": 2.5,
                                    "interval_high_cny": 3.9, "source": "预测，不是实际账单"},
                     "observed": {"settled_cost_cny": 6.4, "observed_calls": 1165,
                                  "source": "实际账本"}},
            "single_request_output": {"task_target": 32768, "model_ceiling": 64000,
                                      "effective_target": 32768,
                                      "note": "模型最大输出是能力上限；任务单请求输出目标只约束本次请求"}})
        view = self.request("/api/tasks/forecast/observability")
        forecast = view["forecast"]
        self.assertEqual(forecast["retry_interval"]["method"], "wilson_95")
        self.assertEqual(forecast["retry_samples"], 7)
        self.assertEqual(forecast["requirements"]["estimate_semantics"], "approximate")
        self.assertEqual(forecast["single_request_output"]["model_ceiling"], 64000)
        self.assertEqual(view["request_output"]["task_target"], 32768)
        self.assertIn("预测", forecast["prediction"]["source"])
        self.assertIn("实际账本", forecast["observed_cost"]["source"])

    def test_children_are_real_lifecycle_only_and_never_invoiced(self):
        self.task("children",
                  subagent_capability={"decision": "allow", "state": "supported",
                                       "policy": "allow", "proven_modes": ["chat"],
                                       "mode": "chat", "reasons": ["用户策略允许"]},
                  subagent_events=[
                      {"schema": 1, "source": "run-dsh.mjs", "type": "parent",
                       "at": "2026-10-13T21:27:05.049Z"},
                      {"schema": 1, "source": "dsh-subagent-lifecycle", "type": "start",
                       "at": "2026-10-13T21:27:06.737Z", "provider": "spawn", "local": True,
                       "child_id": "child-1", "run_id": "run-1", "child_model": "deepseek-flash"},
                      {"schema": 1, "source": "dsh-subagent-lifecycle", "type": "end",
                       "at": "2026-10-13T21:27:08.099Z", "provider": "spawn", "local": True,
                       "child_id": "child-1", "run_id": "run-1", "stop_reason": "completed",
                       "output_chars": 0}],
                  subagent_usage={"schema": 1, "per_child": "unavailable",
                                  "authoritative": "aggregate",
                                  "aggregate": {"settled_cny": 0.006, "settled_calls": 3},
                                  "reason": "DSH exposes no per-child token field"})
        view = self.request("/api/tasks/children/observability")
        children = view["children"]
        self.assertEqual(children["count"], 1)
        self.assertEqual(children["verified"], 1)
        self.assertEqual(children["unverified"], 0)
        self.assertEqual(children["per_child_billing"], "unavailable")
        row = view["child_rows"][0]
        self.assertEqual(row["child_id"], "child-1")
        self.assertEqual(row["provider"], "spawn")
        self.assertEqual(row["stop_reason"], "completed")
        self.assertEqual(row["model"], "deepseek-flash")  # actual native lifecycle model; local means in-process
        self.assertIsNone(row["model_id"])  # do not attribute a different model to the parent's registry ID
        self.assertNotIsInstance(row["cost"], (int, float))
        self.assertIn("不可证实", row["cost"])
        self.assertIn("进程内 provider", row["local_flag_note"])
        boundary = view["billing_boundary"]
        self.assertEqual(boundary["children"]["per_child"], "unavailable")
        self.assertEqual(boundary["children"]["aggregate"]["settled_calls"], 3)
        # The parent task never recorded a settled call: unknown, never a fake 0.
        self.assertIsNone(boundary["parent"]["settled_calls"])

    def test_children_summary_says_unverified_when_nothing_measured(self):
        self.task("no-children")
        view = self.request("/api/tasks/no-children/observability")
        self.assertEqual(view["children"]["count"], 0)
        self.assertIn("未启用/未验证", view["children"]["state"])
        self.assertFalse(view["children"]["allowed"])

    def test_failure_classification_is_reuse_not_reinterpretation(self):
        self.task("system-fail", status="failed", error="HTTP 503 service unavailable")
        view = self.request("/api/tasks/system-fail/observability")
        failure = view["failure"]
        self.assertEqual(failure["outcome"], "system")
        self.assertFalse(failure["counts_as_model_quality"])
        self.assertTrue(failure["system_excluded"])

    def test_batch_endpoint_is_bounded_and_read_only(self):
        for index in range(3):
            self.task(f"batch-{index}")
        data = self.request("/api/observability")
        self.assertEqual(data["stats"]["tasks"], 3)
        self.assertEqual(data["stats"]["shown"], 3)
        self.assertTrue(all("cost" in row and "meter" in row for row in data["tasks"]))

    def test_unknown_task_observability_is_explicit(self):
        with self.assertRaises(HTTPError) as error:
            self.request("/api/tasks/does-not-exist/observability")
        self.assertEqual(error.exception.code, 404)


class ReviewSourceTests(Base):
    def make_stage_task(self, name, review_state=None):
        goal = stage_store.create_goal("验收来源", "review", str(self.project))
        stage = stage_store.add_stage(goal["id"], "阶段", "obj", kind="chat",
                                      acceptance=["c1"], candidates=["home"])
        stage_store.claim_dispatch(stage["id"], name)
        self.task(name, stage_id=stage["id"], goal_id=goal["id"], kind="chat",
                  files=["a.py"], readonly_files=["a.py"], files_readonly=True)
        if review_state:
            registry.patch_task(registry.task_dir(name), review_state=review_state)
        return goal, stage

    def real_review_state(self, task_id):
        single = self.request("/api/tasks/%s/observability" % task_id)
        batch = self.request("/api/observability")
        batch_row = next(item for item in batch["tasks"] if item["task_id"] == task_id)
        tree = self.request("/api/tree")
        tree_row = next(row for goal in tree["goals"] for stage in goal["stages"]
                        for row in stage["tasks"] if row["task_id"] == task_id)
        tasks = self.request("/api/state")["tasks"]
        state_row = next(item for item in tasks if item["id"] == task_id)
        for value in (single["review_state"], batch_row["review_state"],
                      tree_row["review_state"], (state_row.get("review") or {}).get("state")):
            self.assertEqual(value, single["review_state"])
        return single["review_state"]

    def test_all_views_read_the_real_review_not_the_task_cache(self):
        self.make_stage_task("review-task", review_state="returned")
        self.real_review("review-task", criteria=("c1",))
        # Leave a stale, wrong cache in task.json: the real record must win.
        registry.patch_task(registry.task_dir("review-task"), review_state="returned")
        self.assertEqual(self.real_review_state("review-task"), "accepted")

    def test_raw_acceptance_cache_without_a_review_is_not_accepted(self):
        self.make_stage_task("cache-only", review_state="accepted")
        self.assertIsNone(self.real_review_state("cache-only"))

    def test_corrupt_review_record_stays_unreadable_everywhere(self):
        self.make_stage_task("corrupt-task", review_state="accepted")
        run = registry.task_dir("corrupt-task")
        registry.patch_task(run, delegation={"schema": delegation_policy.SCHEMA, "mode": "outcome",
                                             "max_auto_rework": delegation_policy.MAX_AUTO_REWORK,
                                             "rework_count": 0})
        (run / delegation_policy.REVIEW_NAME).write_text("{ not json", encoding="utf-8")
        self.assertEqual(self.real_review_state("corrupt-task"), "unreadable")
        view = self.request("/api/tasks/corrupt-task/observability")
        self.assertTrue(view["review"]["error"])


class TreeTests(Base):
    def test_tree_reports_real_goal_stage_task_child_chain(self):
        goal = stage_store.create_goal("树目标", "tree", str(self.project))
        stage = stage_store.add_stage(goal["id"], "只读调查", "tree", kind="chat",
                                      acceptance=["c1"], candidates=["home"], fixed_model="home")
        stage_store.claim_dispatch(stage["id"], "tree-task")
        self.task("tree-task", stage_id=stage["id"], goal_id=goal["id"], kind="chat",
                  files=["a.py"], readonly_files=["a.py"], files_readonly=True,
                  file_hashes={"a.py": "abc123"},
                  subagent_events=[
                      {"source": "dsh-subagent-lifecycle", "type": "start",
                       "child_id": "c1", "provider": "spawn", "local": True,
                       "at": "2026-10-13T21:27:06Z"},
                      {"source": "dsh-subagent-lifecycle", "type": "end",
                       "child_id": "c1", "provider": "spawn", "local": True,
                       "at": "2026-10-13T21:27:08Z", "stop_reason": "completed"}])
        self.real_review("tree-task", criteria=("c1",))
        tree = self.request("/api/tree")
        self.assertEqual(len(tree["goals"]), 1)
        stage_row = tree["goals"][0]["stages"][0]
        self.assertEqual(stage_row["kind"], "chat")
        self.assertEqual(stage_row["depends_on"], [])
        self.assertIn("readiness", stage_row)
        task_row = stage_row["tasks"][0]
        self.assertEqual(task_row["task_id"], "tree-task")
        self.assertEqual(task_row["review_state"], "accepted")
        self.assertEqual(task_row["readonly_files"], ["a.py"])
        self.assertTrue(task_row["files_readonly"])
        self.assertEqual(task_row["file_hashes"], {"a.py": "abc123"})
        self.assertEqual(task_row["dsh_session_id"], None)
        self.assertEqual(len(task_row["children"]), 1)
        self.assertTrue(task_row["children"][0]["verified"])
        self.assertEqual(len(task_row["children"][0]["child_id"]), 2)
        self.assertEqual(task_row["children_state"]["count"], 1)

    def test_tree_distinguishes_cancelled_and_recovery_pending(self):
        goal = stage_store.create_goal("状态目标", "states", str(self.project))
        cancelled = stage_store.add_stage(goal["id"], "取消", "x", acceptance=["c"],
                                          candidates=["home"], fixed_model="home")
        stage_store.cancel_stage(cancelled["id"], "用户取消")
        pending = stage_store.add_stage(goal["id"], "待恢复", "y", acceptance=["c"],
                                        candidates=["home"], fixed_model="home")
        stage_store.claim_dispatch(pending["id"], "ghost-task")
        stage_store.recover()
        tree = self.request("/api/tree")
        rows = {stage["id"]: stage for stage in tree["goals"][0]["stages"]}
        self.assertEqual(rows[cancelled["id"]]["status"], stage_store.CANCELLED)
        self.assertEqual(rows[pending["id"]]["status"], stage_store.RECOVERY_PENDING)
        self.assertEqual(tree["stats"]["recovery_pending"], 1)
        self.assertEqual(len(tree["recovery"]), 1)

    def test_tree_does_not_fabricate_children_for_denied_task(self):
        goal = stage_store.create_goal("未验证目标", "deny", str(self.project))
        stage = stage_store.add_stage(goal["id"], "阶段", "x", acceptance=["c"],
                                      candidates=["home"], fixed_model="home")
        stage_store.claim_dispatch(stage["id"], "denied-task")
        self.task("denied-task", stage_id=stage["id"], goal_id=goal["id"],
                  subagent_capability={"decision": "deny", "state": "unverified",
                                       "policy": "unset", "proven_modes": [],
                                       "reasons": ["尚无该模型在该执行器上的实测证据"]})
        tree = self.request("/api/tree")
        task_row = tree["goals"][0]["stages"][0]["tasks"][0]
        self.assertEqual(task_row["children"], [])
        self.assertIn("未启用/未验证", task_row["children_state"]["state"])


class McpWhitelistTests(unittest.TestCase):
    def test_new_read_only_paths_are_whitelisted(self):
        for path in ("/api/tree", "/api/observability"):
            self.assertEqual(desk.validate_request(path, "GET"), path)
        self.assertEqual(desk.validate_request("/api/tasks/abc-1/observability", "GET"),
                         "/api/tasks/abc-1/observability")
        for path in ("/api/tree/extra", "/api/observability/extra", "/api/tasks/abc/observability/log"):
            with self.assertRaises(desk.PanelError):
                desk.validate_request(path, "GET")
        with self.assertRaises(desk.PanelError):
            desk.validate_request("/api/tree", "POST")
        with self.assertRaises(desk.PanelError):
            desk.validate_request("/api/observability?task=1", "GET")


if __name__ == "__main__":
    unittest.main(verbosity=2)

