"""Stage-F reliability acceptance: offline integration negatives.

No paid generation, no model call, no service restart.  Every test runs against a
temporary registry/run tree and covers a gap that was actually found while
preparing the final delivery:

* a controlled probe must show its *measurement target / pending acceptance*
  instead of pre-announcing ``chat`` as an already-proven mode, and the panel must
  clarify historical rows from the real recorded provenance;
* a local record/IO failure *after* the executor already finished must not be
  misreported as an executor failure;
* a pre-``kind`` stages.db is migrated forward without reinterpreting historical
  rows, including when several connections open it concurrently;
* the panel idle reloader fails closed on unreadable metadata and corroborates
  idleness with the real on-disk records and live worker processes.
"""
import io
import json
from decimal import Decimal
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import dsh_worker
import model_registry as registry
import panel_server as panel
import stage_store

RELOAD_SCRIPT = ROOT / 'tasks' / 'reload-panel-idle.ps1'


def probe_model(**overrides):
    row = {'id': 'cloud', 'name': 'cloud', 'provider': 'deepseek', 'model': 'deepseek-flash',
           'api_base': 'https://api.deepseek.com', 'agent_preset': 'standard', 'enabled': True,
           'model_version': '', 'executor_version': '0.2.0-rc.2'}
    row.update(overrides)
    return row


class SandboxBase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.saved = registry.HERE, registry.REGISTRY, registry.RUNS
        registry.HERE = Path(self.temp.name)
        registry.REGISTRY = registry.HERE / 'models.json'
        registry.RUNS = registry.HERE / 'runs'

    def tearDown(self):
        registry.HERE, registry.REGISTRY, registry.RUNS = self.saved
        self.temp.cleanup()

    def run_dir(self, name='t1', **fields):
        run = registry.RUNS / name
        run.mkdir(parents=True, exist_ok=True)
        registry.patch_task(run, id=name, status='queued', kind='edit')
        if fields:
            registry.patch_task(run, **fields)
        return run


class ProbeProvenanceTests(SandboxBase):
    """A probe round measures a mode; it does not prove it by itself."""

    def test_probe_reports_the_measurement_target_instead_of_a_proven_mode(self):
        run = self.run_dir('probe-edit')
        plan = {'schema': 1, 'kind': 'controlled-subagent-probe', 'mode': 'edit',
                'model_id': 'cloud', 'writes_models_json': False}
        with patch.object(dsh_worker.subagent_runtime, 'authorize_probe', return_value=plan):
            decision = dsh_worker.subagent_decision(probe_model(), run, probe=True)
        self.assertEqual(decision['decision'], 'allow')
        self.assertTrue(decision['probe'])
        # Nothing is proven yet, and the hard-coded chat label must not reappear.
        self.assertEqual(decision['proven_modes'], [])
        self.assertEqual(decision['probe_target_mode'], 'edit')
        self.assertTrue(decision['pending_acceptance'])
        self.assertTrue(any('测量目标' in reason and 'edit' in reason for reason in decision['reasons']))
        self.assertTrue(any('验收' in reason for reason in decision['reasons']))
        # The provisional decision is persisted verbatim, target mode included.
        recorded = registry.read_task(run)['subagent_capability']
        self.assertEqual(recorded['proven_modes'], [])
        self.assertEqual(recorded['probe_target_mode'], 'edit')
        self.assertTrue(recorded['pending_acceptance'])

    def test_probe_target_defaults_to_the_task_mode_when_the_plan_is_silent(self):
        run = self.run_dir('probe-chat', kind='chat', subagent_probe=True)
        with patch.object(dsh_worker.subagent_runtime, 'authorize_probe', return_value={}):
            decision = dsh_worker.subagent_decision(probe_model(), run, probe=True)
        self.assertEqual(decision['probe_target_mode'], 'chat')
        self.assertEqual(decision['proven_modes'], [])

    def test_historical_chat_label_is_clarified_by_the_real_measured_provenance(self):
        # Exact shape of the real 20261014-064058-b6c4d6 edit round: the generic
        # probe branch wrote proven_modes=['chat'] while the run really measured
        # edit, and run_edit_probe recorded the authoritative provenance beside it.
        task = {
            'id': '20261014-064058-b6c4d6',
            'subagent_capability': {
                'decision': 'allow', 'state': 'probe', 'policy': 'probe',
                'proven_modes': ['chat'], 'mode': 'edit',
                'reasons': ['受控探测：前置条件通过'],
            },
            'probe_mode_provenance': {
                'mode_measured': 'edit', 'sandbox_mode': 'danger-full-access',
                'recorded_by': 'subagent_runtime.run_edit_probe',
            },
        }
        summary = panel._children_summary(task, [], review_state='accepted')
        self.assertTrue(summary['probe'])
        self.assertTrue(summary['probe_accepted'])
        self.assertIn('受控探测', summary['state'])
        self.assertIn('edit', summary['state'])
        self.assertIn('已独立验收', summary['state'])
        # The historical label is preserved, not rewritten; the clarification is
        # additive and visible through the reasons the panel already renders.
        self.assertEqual(summary['capability']['proven_modes'], ['chat'])
        self.assertEqual(summary['capability']['probe_target_mode'], 'edit')
        self.assertEqual(summary['capability']['probe_mode_provenance']['mode_measured'], 'edit')
        self.assertFalse(summary['capability']['pending_acceptance'])
        self.assertTrue(any('已接受' in reason or 'kind=edit' in reason
                            for reason in summary['capability']['reasons']))

    def test_historical_probe_without_acceptance_still_reads_as_pending(self):
        task = {'subagent_capability': {'decision': 'allow', 'state': 'probe', 'policy': 'probe',
                                        'proven_modes': ['chat'], 'mode': 'edit',
                                        'reasons': ['受控探测：前置条件通过']},
                'probe_mode_provenance': {'mode_measured': 'edit'}}
        summary = panel._children_summary(task, [])
        self.assertFalse(summary['probe_accepted'])
        self.assertIn('待主脑独立验收', summary['state'])
        self.assertTrue(summary['capability']['pending_acceptance'])

    def test_a_new_probe_decision_needs_no_clarification_reason(self):
        task = {'subagent_capability': {'decision': 'allow', 'state': 'probe', 'policy': 'probe',
                                        'proven_modes': [], 'probe_target_mode': 'edit',
                                        'pending_acceptance': True, 'mode': 'edit',
                                        'reasons': ['本轮测量目标 edit；本轮结果未经主脑独立验收前不算已证实模式']}}
        summary = panel._children_summary(task, [])
        self.assertEqual(summary['capability']['probe_target_mode'], 'edit')
        self.assertFalse(any('kind=edit' in reason for reason in summary['capability']['reasons']))

    def test_normal_denied_task_keeps_its_plain_unverified_summary(self):
        task = {'subagent_capability': {'decision': 'deny', 'state': 'unverified',
                                        'policy': 'unset', 'proven_modes': [],
                                        'reasons': ['尚无该模型在该执行器上的实测证据']}}
        summary = panel._children_summary(task, [])
        self.assertIn('未启用/未验证', summary['state'])
        self.assertFalse(summary['probe'])


class FakeAcpProcess:
    """Minimal stand-in for the ACP child: two stream iterables and a pid."""

    def __init__(self, stdout_text):
        self.stdout = io.StringIO(stdout_text)
        self.stderr = io.StringIO('')
        self.pid = 987654
        self.returncode = 0

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        return self.returncode


class PostExecutionIOFailureTests(unittest.TestCase):
    """Finished work must never be relabelled as an executor failure by local IO."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.saved = registry.HERE, registry.REGISTRY, registry.RUNS
        self.saved_here = dsh_worker.HERE
        registry.HERE = Path(self.temp.name)
        registry.REGISTRY = registry.HERE / 'models.json'
        registry.RUNS = registry.HERE / 'runs'
        dsh_worker.HERE = registry.HERE
        registry.write_json(registry.HERE / 'settings.json', {
            'model': 'qwen-test', 'api_base': 'http://127.0.0.1:1234/v1', 'api_key_env': '',
            'max_input_tokens': 32768, 'max_output_tokens': 4096})
        registry.list_models()

    def tearDown(self):
        registry.HERE, registry.REGISTRY, registry.RUNS = self.saved
        dsh_worker.HERE = self.saved_here
        self.temp.cleanup()

    def test_final_status_write_failure_keeps_the_real_completed_outcome(self):
        project = registry.HERE / 'project'
        project.mkdir()
        run = registry.RUNS / 'io-task'
        run.mkdir(parents=True)
        (run / 'prompt.txt').write_text('只读任务：验证记录IO失败不误报', encoding='utf-8')
        model = registry.get_model('home')
        state = {'id': 'io-task', 'kind': 'chat', 'project': str(project), 'files': [],
                 'timeout': 30, 'model_id': model['id']}
        relay = SimpleNamespace(url='http://127.0.0.1:9/v1', token='relay-token')
        preset = {'id': 'standard', 'revision': 'r', 'source': 's', 'overrides': [],
                  'policy_overrides': [], 'declaration': {'id': 'agent-preset', 'config': {'plugins': []}}}
        events = '\n'.join(json.dumps(item, ensure_ascii=False) for item in [
            {'type': 'session', 'session_id': 'sess-io'},
            {'type': 'text_delta', 'text': '已完成'},
            {'type': 'result', 'result': {'status': 'completed', 'stopReason': 'end_turn'}},
        ]) + '\n'
        process = FakeAcpProcess(events)
        real_patch = registry.patch_task
        failed = {'once': False}

        def flaky(target, **fields):
            if not failed['once'] and fields.get('status') == 'completed' and 'exit_code' in fields:
                failed['once'] = True
                raise PermissionError('transient sharing violation')
            return real_patch(target, **fields)

        printed = io.StringIO()
        with patch.object(dsh_worker, 'availability', return_value={'ready': True}), \
             patch.object(dsh_worker.preset_runtime, 'load', return_value=preset), \
             patch.object(dsh_worker.preset_runtime, 'verify', return_value={}), \
             patch.object(dsh_worker.record_protection, 'verify_proof', return_value={'matched': 'parent', 'offline_fixture': True}), \
             patch.object(dsh_worker.subprocess, 'Popen', return_value=process), \
             patch.object(dsh_worker.registry, 'patch_task', side_effect=flaky), \
             patch('sys.stdout', new=printed):
            code = dsh_worker._run_task(run, state, model, relay)
        self.assertTrue(failed['once'], 'the final status write must have been exercised')
        self.assertEqual(code, 0)
        final = registry.read_task(run)
        # Real outcome preserved; the local IO problem is recorded beside it.
        self.assertEqual(final['status'], 'completed')
        self.assertEqual(final['exit_code'], 0)
        self.assertIn('io_error', final)
        self.assertNotEqual(final.get('phase'), 'DSH 执行失败')
        self.assertNotIn('error', final)
        self.assertTrue((run / 'result.json').is_file())
        self.assertIn('io_error', printed.getvalue())

    def test_a_real_executor_failure_still_records_failed(self):
        project = registry.HERE / 'project'
        project.mkdir()
        run = registry.RUNS / 'exec-task'
        run.mkdir(parents=True)
        (run / 'prompt.txt').write_text('只读任务：真实执行失败', encoding='utf-8')
        model = registry.get_model('home')
        state = {'id': 'exec-task', 'kind': 'chat', 'project': str(project), 'files': [],
                 'timeout': 30, 'model_id': model['id']}
        relay = SimpleNamespace(url='http://127.0.0.1:9/v1', token='relay-token')
        preset = {'id': 'standard', 'revision': 'r', 'source': 's', 'overrides': [],
                  'policy_overrides': [], 'declaration': {'id': 'agent-preset', 'config': {'plugins': []}}}
        process = FakeAcpProcess(json.dumps({'type': 'error', 'text': '子进程崩溃'}) + '\n')
        process.returncode = 1
        with patch.object(dsh_worker, 'availability', return_value={'ready': True}), \
             patch.object(dsh_worker.preset_runtime, 'load', return_value=preset), \
             patch.object(dsh_worker.preset_runtime, 'verify', return_value={}), \
             patch.object(dsh_worker.record_protection, 'verify_proof', return_value={'matched': 'parent', 'offline_fixture': True}), \
             patch.object(dsh_worker.subprocess, 'Popen', return_value=process), \
             patch('sys.stdout', new=io.StringIO()):
            code = dsh_worker._run_task(run, state, model, relay)
        self.assertEqual(code, 1)
        final = registry.read_task(run)
        self.assertEqual(final['status'], 'failed')
        self.assertIn('error', final)
        self.assertNotIn('io_error', final)


LEGACY_DDL = '''
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE goals (
    id TEXT PRIMARY KEY, title TEXT NOT NULL, objective TEXT NOT NULL DEFAULT '',
    project TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'active',
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL, data TEXT NOT NULL DEFAULT '{}');
CREATE TABLE stages (
    id TEXT PRIMARY KEY, goal_id TEXT NOT NULL, ordinal INTEGER NOT NULL, title TEXT NOT NULL,
    objective TEXT NOT NULL DEFAULT '', project TEXT NOT NULL DEFAULT '',
    files TEXT NOT NULL DEFAULT '[]', acceptance TEXT NOT NULL DEFAULT '[]',
    candidates TEXT NOT NULL DEFAULT '[]', fixed_model TEXT NOT NULL DEFAULT '',
    depends_on TEXT NOT NULL DEFAULT '[]', status TEXT NOT NULL DEFAULT 'pending',
    session_policy TEXT NOT NULL DEFAULT 'new_session', session_key TEXT NOT NULL DEFAULT '',
    task_ids TEXT NOT NULL DEFAULT '[]', active_task_id TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL, data TEXT NOT NULL DEFAULT '{}');
CREATE TABLE stage_log (
    seq INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, goal_id TEXT NOT NULL DEFAULT '',
    stage_id TEXT NOT NULL DEFAULT '', task_id TEXT NOT NULL DEFAULT '',
    action TEXT NOT NULL, actor TEXT NOT NULL DEFAULT 'main', detail TEXT NOT NULL DEFAULT '');
'''

LEGACY_STAGE_COLUMNS = ('id', 'goal_id', 'ordinal', 'title', 'objective', 'project', 'files',
                        'acceptance', 'candidates', 'fixed_model', 'depends_on', 'status',
                        'session_policy', 'session_key', 'task_ids', 'active_task_id',
                        'created_at', 'updated_at', 'data')


class StageSchemaMigrationTests(unittest.TestCase):
    """An old stages.db must gain the new column without reinterpreting rows."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.saved = registry.HERE, registry.REGISTRY, registry.RUNS
        registry.HERE = Path(self.temp.name)
        registry.REGISTRY = registry.HERE / 'models.json'
        registry.RUNS = registry.HERE / 'runs'

    def tearDown(self):
        registry.HERE, registry.REGISTRY, registry.RUNS = self.saved
        self.temp.cleanup()

    def make_legacy_db(self):
        path = registry.HERE / stage_store.DB_NAME
        conn = sqlite3.connect(path)
        conn.executescript(LEGACY_DDL)
        conn.execute("INSERT INTO meta VALUES ('schema', '1')")
        conn.execute("INSERT INTO goals VALUES ('g1','旧目标','','proj','active',?,?,'{}')",
                     ('2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00'))
        values = ('s1', 'g1', 1, '旧阶段', '', 'proj', '["a.py"]', '["c1","c2"]', '["home"]',
                  'home', '[]', 'pending', 'new_session', '', '[]', '',
                  '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00', '{"note":"keep"}')
        conn.execute('INSERT INTO stages (%s) VALUES (%s)'
                     % (','.join(LEGACY_STAGE_COLUMNS), ','.join('?' * len(LEGACY_STAGE_COLUMNS))), values)
        conn.commit()
        conn.close()
        return path

    def test_legacy_rows_gain_the_kind_default_and_keep_their_identity(self):
        self.make_legacy_db()
        stage = stage_store.get_stage('s1')
        self.assertIsNotNone(stage)
        self.assertEqual(stage['kind'], 'edit')  # historical default, not reinterpreted
        self.assertEqual(stage['title'], '旧阶段')
        self.assertEqual(stage['status'], 'pending')
        self.assertEqual(stage['acceptance'], ['c1', 'c2'])
        self.assertEqual(stage['candidates'], ['home'])
        self.assertEqual(stage['fixed_model'], 'home')
        self.assertEqual(stage['data'], {'note': 'keep'})
        check = sqlite3.connect(registry.HERE / stage_store.DB_NAME)
        try:
            columns = {row[1] for row in check.execute('PRAGMA table_info(stages)')}
        finally:
            check.close()
        self.assertIn('kind', columns)
        self.assertEqual(stage_store.list_goals()[0]['id'], 'g1')

    def test_concurrent_first_open_migrates_once_and_keeps_the_legacy_row(self):
        self.make_legacy_db()
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(lambda _: stage_store.list_stages('g1'), range(12)))
        for rows in results:
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]['id'], 's1')
            self.assertEqual(rows[0]['kind'], 'edit')
        check = sqlite3.connect(registry.HERE / stage_store.DB_NAME)
        try:
            columns = [row[1] for row in check.execute('PRAGMA table_info(stages)')]
            row_count = check.execute('SELECT COUNT(*) FROM stages').fetchone()[0]
        finally:
            check.close()
        self.assertEqual(columns.count('kind'), 1)
        self.assertEqual(row_count, 1)


class IdleReloadGuardTests(unittest.TestCase):
    """The idle reloader must fail closed and corroborate with real state."""

    @classmethod
    def setUpClass(cls):
        cls.shell = shutil.which('pwsh') or shutil.which('powershell')
        if not cls.shell or not RELOAD_SCRIPT.is_file():
            raise unittest.SkipTest('pwsh and reload-panel-idle.ps1 are required')

    def decide(self, task_rows=(), disk_rows=(), workers=(), metadata_readable=True):
        with tempfile.TemporaryDirectory() as temp:
            payload = Path(temp) / 'payload.json'
            payload.write_text(json.dumps({'task_rows': list(task_rows), 'disk_rows': list(disk_rows),
                                           'workers': list(workers),
                                           'metadata_readable': metadata_readable}), encoding='utf-8')
            script = (
                ". '%s' -AsLibrary; "
                "$p = Get-Content -LiteralPath '%s' -Raw -Encoding UTF8 | ConvertFrom-Json; "
                "$r = Test-DeskIdleState -TaskRows $p.task_rows -DiskRows $p.disk_rows "
                "-WorkerProcesses $p.workers -MetadataReadable ([bool]$p.metadata_readable); "
                "$r | ConvertTo-Json -Compress -Depth 4"
            ) % (RELOAD_SCRIPT, payload)
            done = subprocess.run([self.shell, '-NoProfile', '-ExecutionPolicy', 'Bypass', '-Command', script],
                                  capture_output=True, text=True, encoding='utf-8', timeout=90)
        self.assertEqual(done.returncode, 0, done.stderr)
        return json.loads(done.stdout.strip().splitlines()[-1])

    def test_metadata_omitting_a_running_task_is_not_idle(self):
        result = self.decide(task_rows=[], disk_rows=[{'id': 'real-running', 'status': 'running'}])
        self.assertFalse(result['idle'])
        self.assertTrue(any('real-running' in reason for reason in result['reasons']))

    def test_unreadable_metadata_is_never_idle(self):
        result = self.decide(metadata_readable=False)
        self.assertFalse(result['idle'])
        self.assertTrue(any('元数据' in reason for reason in result['reasons']))

    def test_api_running_rows_block_the_reload(self):
        result = self.decide(task_rows=[{'id': 'api-running', 'status': 'queued'}])
        self.assertFalse(result['idle'])
        self.assertTrue(any('api-running' in reason for reason in result['reasons']))

    def test_live_worker_process_blocks_the_reload(self):
        result = self.decide(workers=[{'pid': 4242, 'name': 'node.exe'}])
        self.assertFalse(result['idle'])
        self.assertTrue(any('4242' in reason for reason in result['reasons']))

    def test_completed_records_are_idle(self):
        result = self.decide(task_rows=[{'id': 't1', 'status': 'completed'}],
                             disk_rows=[{'id': 't1', 'status': 'completed'}])
        self.assertTrue(result['idle'], result['reasons'])
        self.assertEqual(result['reasons'], [])

    def test_decision_precedes_and_guards_the_restart(self):
        text = RELOAD_SCRIPT.read_text(encoding='utf-8')
        self.assertLess(text.index('Test-DeskIdleState -TaskRows'),
                        text.index('Stop-Process'))
        self.assertIn('-WindowStyle Hidden', text)
        # The old metadata-only check must be gone, and unreadable metadata must
        # abort instead of falling through to the restart.
        self.assertNotIn("throw 'Queue is not idle'", text)
        self.assertIn('面板元数据不可读，拒绝按空闲重启', text)


TASKS_DIR = ROOT / 'tasks'


def _read_json(path):
    path = Path(path)
    return json.loads(path.read_text(encoding='utf-8')) if path.is_file() else None


def _receipts(task_id):
    row = registry.read_task(registry.task_dir(task_id))
    values = row.get('cost_requests') or {}
    return list(values.values()) if isinstance(values, dict) else list(values)


def _receipt_cost(receipt):
    usage, rates = receipt.get('usage') or {}, receipt.get('rates') or {}
    if not usage or not rates:
        return None
    hit = Decimal(str(usage.get('prompt_cache_hit_tokens', usage.get('prompt_tokens', 0))))
    miss = Decimal(str(usage.get('prompt_cache_miss_tokens', 0)))
    out = Decimal(str(usage.get('completion_tokens', 0)))
    value = (hit * Decimal(str(rates['cache_hit_input'])) + miss * Decimal(str(rates['cache_miss_input']))
             + out * Decimal(str(rates['output']))) / Decimal(1000000)
    return value


def _audit_rounds(document):
    """Recompute settled/unknown receipts of an evidence document from real tasks."""
    settled, unknown, requests, recompute_ok = Decimal(0), 0, 0, True
    for round_row in document['rounds']:
        for receipt in _receipts(round_row['task']['task_id']):
            requests += 1
            if receipt.get('state') == 'unknown':
                unknown += 1
                continue
            if receipt.get('state') != 'settled':
                continue
            value = _receipt_cost(receipt)
            if value is None or value != Decimal(str(receipt['cost_cny'])):
                recompute_ok = False
            settled += Decimal(str(receipt['cost_cny']))
    return {'requests': requests, 'settled': settled, 'unknown': unknown, 'recompute_ok': recompute_ok}


class RecordedEvidenceTests(unittest.TestCase):
    """Re-verify already-produced evidence; never regenerate a paid probe.

    Each test skips when the recorded run/artifact no longer exists instead of
    inventing a replacement.
    """

    def test_stage_smoke_record_matches_the_real_two_stage_flow(self):
        record = _read_json(TASKS_DIR / 'stage-smoke-record.json')
        if not record:
            self.skipTest('stage-smoke-record.json missing')
        try:
            stages = {row['id']: row for row in stage_store.list_stages(record['goal_id'])}
            goal = stage_store.get_goal(record['goal_id'])
            first = registry.read_task(registry.task_dir(record['first_task']))
            second = registry.read_task(registry.task_dir(record['second_task']))
        except (OSError, ValueError):  # historic run removed from this workspace
            self.skipTest('historic smoke runs are not present in this workspace')
        self.assertEqual(goal['status'], stage_store.GOAL_COMPLETED)
        for stage_id in (record['first_stage'], record['second_stage']):
            self.assertIn(stages[stage_id]['status'], stage_store.ACCEPTED_STATES)
        self.assertEqual(first.get('dsh_session_id'), record['first_session'])
        self.assertEqual(second.get('dsh_session_id'), record['second_session'])
        self.assertNotEqual(record['first_session'], record['second_session'])
        for task_id in (record['first_task'], record['second_task']):
            review = _read_json(registry.task_dir(task_id) / 'review.json')
            self.assertIsNotNone(review, task_id)
            self.assertEqual(review['state'], 'accepted')
        prompt = (registry.task_dir(record['second_task']) / 'dsh-prompt.txt').read_text(encoding='utf-8')
        self.assertIn(record['first_task'], prompt)

    def test_offload_proof_really_contains_the_middle_marker_outside_the_preview(self):
        import hashlib
        proof = _read_json(TASKS_DIR / 'offload-readonly-proof.json')
        if not proof:
            self.skipTest('offload-readonly-proof.json missing')
        artifact = Path(proof['artifact'])
        if not artifact.is_file():
            self.skipTest('artifact no longer present')
        data = artifact.read_bytes()
        record = proof['metrics']['records'][0]
        self.assertEqual(hashlib.sha256(data).hexdigest(), record['digest'])
        self.assertEqual(len(data), record['bytes'])
        marker = proof['expected'].encode()
        self.assertIn(marker, data)
        self.assertNotIn(marker, data[:proof['metrics']['replaced_bytes']])
        task = registry.read_task(registry.task_dir(proof['task_id']))
        self.assertEqual(task.get('dsh_session_id'), proof['session'])
        self.assertEqual(task.get('sandbox_mode'), 'read-only')

    def test_probe_and_edit_receipts_recompute_to_the_recorded_subtotals(self):
        probe = _read_json(TASKS_DIR / 'subagent-probe-evidence.json')
        edit = _read_json(TASKS_DIR / 'edit-subagent-evidence.json')
        if not probe or not edit:
            self.skipTest('subagent evidence files missing')
        probe_audit, edit_audit = _audit_rounds(probe), _audit_rounds(edit)
        self.assertEqual(probe['round_count'], 4)
        self.assertEqual(edit['round_count'], 2)
        self.assertEqual(probe_audit['settled'], Decimal(str(probe['round_totals']['settled_cny'])))
        self.assertEqual(edit_audit['settled'], Decimal(str(edit['round_totals']['settled_cny'])))
        self.assertEqual(probe_audit['unknown'], probe['round_totals']['unknown_calls'])
        self.assertEqual(edit_audit['unknown'], edit['round_totals']['unknown_calls'])
        self.assertTrue(probe_audit['recompute_ok'])
        self.assertTrue(edit_audit['recompute_ok'])
        # Known receipts stay as recorded: 4 chat rounds + 2 edit rounds + 1 unknown.
        self.assertEqual(probe_audit['settled'] + edit_audit['settled'], Decimal('0.04105664'))
        self.assertEqual(probe_audit['unknown'] + edit_audit['unknown'], 1)
        self.assertEqual(probe['gateway']['per_child'], 'unavailable')
        self.assertEqual(edit['gateway']['per_child'], 'unavailable')
        self.assertNotEqual(probe['read_only_probe'], edit['read_only_probe'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
