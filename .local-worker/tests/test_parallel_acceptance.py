"""Parallel plan acceptance scenarios, offline: real controller concurrency, mock workers.

The plan (`后续待更新功能/多模型并行协作-后续更新计划.md`) requires proving real
execution-interval overlap with controllable events before `max_workers` is ever
raised.  These tests run the real `execute_task` path on a temporarily two-worker
queue; the engine worker subprocess is replaced by a controllable fake that
sleeps, writes deliverables into the task worktree and publishes the terminal
record - exactly the worker contract, minus the model.  No provider is called,
no real project is touched, and every record lives in an isolated temp registry.

Scenario mapping: 1 (overlap + same-model queue) tests 1-2; 2 (worktrees,
conflicts, user changes preserved) and 3 (a blocking integration check) test 3;
4 (records stay with the original project) is asserted inside test 1; 5 (paid
tasks under caps) test 4; 6 (cancel one, other intact) test 5; 7 (dependencies
need acceptance, one batch notification) is covered by test_stage_workflow and
slice five's queued-waiting behaviour.
"""
import concurrent.futures
import shutil
import subprocess
import tempfile
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import panel_server as panel
import model_registry as registry
import task_integration
import task_leases
import task_workspace as workspace
import workflow_settings
from test_stage_workflow import Base


def git(repo, *args):
    result = subprocess.run(['git', *args], cwd=str(repo), capture_output=True, text=True,
                            encoding='utf-8', errors='replace')
    assert result.returncode == 0, (args, result.stderr)
    return result.stdout


def make_repo(path):
    path = Path(path)
    path.mkdir(parents=True)
    git(path, 'init', '-b', 'main')
    git(path, 'config', 'user.name', 'fixture')
    git(path, 'config', 'user.email', 'fixture@example.com')
    (path / 'a.py').write_text('BASE = 1\n', encoding='utf-8')
    (path / 'shared.txt').write_text('base\n', encoding='utf-8')
    git(path, 'add', '-A')
    git(path, 'commit', '-m', 'baseline')
    return path


def commit_on_branch(repo, branch, edits):
    """Edit the fixture's existing task worktree, never the user's main tree."""
    tmp = workspace.workspace_root(repo) / branch
    for name, content in edits.items():
        (tmp / name).write_text(content, encoding='utf-8')
    git(tmp, 'add', '-A')
    git(tmp, '-c', 'user.name=fixture', '-c', 'user.email=f@x', 'commit', '-q', '-m', 'fixture')


class FakeProcess:
    def __init__(self, thread):
        self.thread = thread
        self.pid = 424242
        thread.start()

    def wait(self):
        self.thread.join()
        return 0

    def poll(self):
        return None if self.thread.is_alive() else 0

    def kill(self):
        pass


class ParallelAcceptanceTests(Base):
    def setUp(self):
        super().setUp()
        self.settings = {'enabled': True, 'paid_allowed': True, 'model_scope': 'all',
                         'enforce_paid_budget': False, 'enforce_model_call_limit': False,
                         'max_parallel_tasks': 2}
        workflow_settings.save_settings(self.settings)
        # The git repo IS Base's own project directory: records, locator index
        # and worktrees then all live inside one temp tree with one resolution
        # path, exactly like a real dispatch.
        self.git_project = self.project
        git(self.git_project, 'init', '-b', 'main')
        git(self.git_project, 'config', 'user.name', 'fixture')
        git(self.git_project, 'config', 'user.email', 'fixture@example.com')
        (self.git_project / 'shared.txt').write_text('base\n', encoding='utf-8')
        git(self.git_project, 'add', '-A')
        git(self.git_project, 'commit', '-m', 'baseline')
        self.baseline = git(self.git_project, 'rev-parse', 'HEAD').strip()
        self.second_model = registry.upsert_model({'provider': 'local', 'model': 'qwen2-test',
                                                   'name': 'Second local', 'api_base': 'http://127.0.0.1:1235/v1',
                                                   'max_input_tokens': 32768, 'max_output_tokens': 4096})
        self.paid_model = registry.upsert_model({'provider': 'deepseek', 'model': 'paid-test',
                                                 'name': 'Paid fixture', 'api_base': 'https://api.example.invalid',
                                                 'max_input_tokens': 32768, 'max_output_tokens': 4096})
        self.paid_model2 = registry.upsert_model({'provider': 'deepseek', 'model': 'paid-test-2',
                                                  'name': 'Paid fixture two', 'api_base': 'https://second.example.invalid',
                                                  'max_input_tokens': 32768, 'max_output_tokens': 4096})
        self.log = []
        self.hold = 0.4
        self.fail_tasks = ()
        self.worker_errors = []
        self.start_barrier = None
        self.started = {}
        self.futures = []
        self.timers = []
        real_timer = threading.Timer

        def timer(*args, **kwargs):
            item = real_timer(*args, **kwargs)
            self.timers.append(item)
            return item

        self.enterContext(patch.object(panel.threading, 'Timer', timer))
        self.real_popen = subprocess.Popen
        self.enterContext(patch.object(panel, 'LEASE_WAIT_BACKOFF', 0.1))
        self.enterContext(patch.object(panel.subprocess, 'Popen', self.fake_popen))
        self.executor = concurrent.futures.ThreadPoolExecutor(max_workers=2)
        real_submit = self.executor.submit

        def submit(*args, **kwargs):
            future = real_submit(*args, **kwargs)
            self.futures.append(future)
            return future

        self.enterContext(patch.object(self.executor, 'submit', submit))
        self.enterContext(patch.object(panel, 'EXECUTOR', self.executor))

    def tearDown(self):
        # Base restores registry globals and deletes the temp tree in tearDown,
        # BEFORE unittest cleanups run. Drain all producers first, even when an
        # assertion fails; otherwise workers write into a disappearing registry.
        for run in self.git_project.glob('.model-desk/runs/*'):
            (run / 'cancelled.flag').touch()
        for timer in self.timers:
            timer.cancel()
            timer.join()
        self.executor.shutdown(wait=True)
        super().tearDown()

    def fake_popen(self, command, *args, **kwargs):
        # Only engine-worker spawns are intercepted; every other subprocess
        # call (git plumbing, taskkill) goes to the real Popen.
        if str(command[0]).lower() == 'taskkill':
            # The fake has no OS process; never target a real Windows PID.
            return FakeProcess(threading.Thread(target=lambda: None))
        if not any(part == '--run-id' for part in command):
            return self.real_popen(command, *args, **kwargs)
        task_id = command[command.index('--run-id') + 1]

        def run():
            self.log.append({'task': task_id, 'event': 'start', 'at': time.monotonic()})
            self.started.setdefault(task_id, threading.Event()).set()
            if self.start_barrier:
                self.start_barrier.wait(timeout=10)
            time.sleep(self.hold)
            run_dir = registry.task_dir(task_id)
            status = 'failed' if task_id in self.fail_tasks else (
                'cancelled' if (run_dir / 'cancelled.flag').exists() else 'completed')
            if status == 'completed':
                plan = registry.read_task(run_dir).get('workspace') or {}
                if plan.get('created'):
                    (Path(plan['path']) / (task_id + '.txt')).write_text(
                        'deliverable of ' + task_id + '\n', encoding='utf-8')
            registry.patch_task(run_dir, status=status, phase='fake worker 终态', ended_at=registry.now())
            self.log.append({'task': task_id, 'event': 'end', 'at': time.monotonic()})

        def guarded_run():
            try:
                run()
            except Exception as error:
                self.worker_errors.append(error)
                raise

        return FakeProcess(threading.Thread(target=guarded_run))

    def add_task(self, task_id, model_id='home', files=('a.py',)):
        # Production record layout: the run dir lives under the record
        # project's .model-desk and the locator points there, exactly as
        # submit_task would create it.
        self.started[task_id] = threading.Event()
        run = registry.prepare_task_dir(task_id, str(self.git_project))
        registry.patch_task(run, id=task_id, title='fixture ' + task_id, kind='edit',
                            engine='dsh', model_id=model_id, model='qwen-test',
                            status='queued', phase='等待执行', project=str(self.git_project),
                            files=list(files), changed_files=[], model_calls=0,
                            route={'mode': 'manual', 'requirements': {'acceptance': ['fixture']}},
                            delegation={'schema': 1, 'mode': 'outcome', 'rework_count': 0,
                                        'inherited_rework_count': 0},
                            timeout=0, workspace={'eligible': True, 'reason': '',
                                                  'baseline': self.baseline})
        return run

    def submit(self, task_id):
        panel.EXECUTOR.submit(panel.execute_task, task_id)

    def await_terminal(self, runs, timeout=20):
        deadline = time.monotonic() + timeout
        pending = list(runs)
        while pending and time.monotonic() < deadline:
            pending = [run for run in pending
                       if registry.read_task(run)['status'] in {'queued', 'running'}]
            if pending:
                time.sleep(0.05)
        self.assertFalse(pending, 'tasks never reached a terminal state')
        return self.await_settled(runs, timeout)

    def await_settled(self, runs, timeout=20):
        """Wait past terminal status: the controller still commits deliverables
        and in-flight reschedule timers need to drain before assertions."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            states = [registry.read_task(run) for run in runs]
            terminal = all(state['status'] in {'completed', 'failed', 'cancelled'}
                           for state in states)
            if terminal and all(future.done() for future in self.futures) and not any(
                    timer.is_alive() for timer in self.timers):
                for future in self.futures:
                    future.result()
                self.assertEqual(self.worker_errors, [])
                return states
            time.sleep(0.05)
        self.fail('controller did not settle: ' + repr(states))

    def intervals(self, task_ids):
        spans = {}
        for entry in self.log:
            spans.setdefault(entry['task'], {})[entry['event']] = entry['at']
        return {task: (spans[task]['start'], spans[task]['end']) for task in task_ids}

    def test_independent_tasks_overlap_on_distinct_models(self):
        self.start_barrier = threading.Barrier(2)
        (self.git_project / 'b.py').write_text('user uncommitted edit\n', encoding='utf-8')
        run_a = self.add_task('t-para-a', model_id='home')
        run_b = self.add_task('t-para-b', model_id=self.second_model['id'])
        self.submit('t-para-a')
        self.submit('t-para-b')
        states = self.await_settled([run_a, run_b])
        self.assertTrue(all(state['status'] == 'completed' for state in states), states)
        (start_a, end_a), (start_b, end_b) = self.intervals(['t-para-a', 't-para-b']).values()
        self.assertLess(max(start_a, start_b), min(end_a, end_b),
                        'independent tasks did not genuinely overlap')
        # Scenario 4: records, worktrees and deliverable commits stay bound to
        # the original project under distinct workspace paths.
        for state in states:
            self.assertEqual(state['project'], str(self.git_project))
            plan = state['workspace']
            self.assertTrue(plan['created'])
            self.assertNotEqual(Path(plan['path']), self.git_project)
            self.assertTrue(workspace.branch_exists(self.git_project,
                                                    plan['branch'].rsplit('/', 1)[1]))
            self.assertEqual(state['workspace']['commit'],
                             git(plan['path'], 'rev-parse', 'HEAD').strip())
        self.assertEqual(task_leases.holders(), {})
        self.assertEqual((self.git_project / 'a.py').read_text(encoding='utf-8'), 'def one():\n    return 1\n')
        self.assertEqual((self.git_project / 'b.py').read_text(encoding='utf-8'), 'user uncommitted edit\n')

    def test_same_model_tasks_queue_behind_each_other(self):
        run_a = self.add_task('t-queue-a', model_id='home')
        run_b = self.add_task('t-queue-b', model_id='home')
        self.submit('t-queue-a')
        self.submit('t-queue-b')
        states = self.await_settled([run_a, run_b])
        self.assertTrue(all(state['status'] == 'completed' for state in states), states)
        (start_a, end_a), (start_b, end_b) = self.intervals(['t-queue-a', 't-queue-b']).values()
        self.assertGreaterEqual(max(start_a, start_b), min(end_a, end_b) - 0.01,
                                'same-model tasks overlapped instead of queueing')
        waiter = registry.read_task(run_b if start_b > start_a else run_a)
        self.assertGreaterEqual(waiter['lease_wait']['count'], 1)

    def test_distinct_model_ids_still_serialize_for_default_cap_backend_alias_or_original_writer(self):
        original_get=registry.get_model
        for number,reason in enumerate(('default-cap','backend-alias','original-writer')):
            with self.subTest(reason=reason):
                workflow_settings.save_settings({'max_parallel_tasks':1 if reason=='default-cap' else 2})
                a,b='t-barrier-a-'+str(number),'t-barrier-b-'+str(number)
                run_a=self.add_task(a)
                run_b=self.add_task(b,model_id=self.second_model['id'])
                if reason=='original-writer':registry.patch_task(run_b,workspace=None)
                def model_for(model_id,*args,**kwargs):
                    value=original_get(model_id,*args,**kwargs)
                    return {**value,'api_base':'http://localhost:1234/alias'} if reason=='backend-alias' else value
                with patch.object(registry,'get_model',side_effect=model_for):
                    self.submit(a);self.submit(b)
                    states=self.await_settled([run_a,run_b])
                self.assertTrue(all(row['status']=='completed' for row in states),states)
                (start_a,end_a),(start_b,end_b)=self.intervals([a,b]).values()
                self.assertGreaterEqual(max(start_a,start_b),min(end_a,end_b))

    def test_duplicate_delivery_starts_only_one_worker(self):
        run = self.add_task('t-duplicate')
        self.submit('t-duplicate')
        self.submit('t-duplicate')
        self.assertEqual(self.await_settled([run])[0]['status'], 'completed')
        panel.execute_task('t-duplicate')
        starts = [row for row in self.log if row['event'] == 'start']
        self.assertEqual(len(starts), 1)

    def test_completed_worker_keeps_lease_through_controller_commit(self):
        run_a = self.add_task('t-finalize-a')
        run_b = self.add_task('t-finalize-b')
        committing = threading.Event()
        release = threading.Event()
        waiting = threading.Event()
        commit = panel.commit_workspace_for_run
        reschedule = panel.reschedule_for_lease

        def held_commit(run, current):
            if current['id'] == 't-finalize-a':
                committing.set()
                if not release.wait(10):
                    raise AssertionError('finalizer was never released')
            return commit(run, current)

        def observed_wait(run, state, holder, **kwargs):
            result = reschedule(run, state, holder, **kwargs)
            if result and holder == 't-finalize-a':
                waiting.set()
            return result

        with patch.object(panel, 'commit_workspace_for_run', held_commit), patch.object(
                panel, 'reschedule_for_lease', observed_wait):
            try:
                self.submit('t-finalize-a')
                self.assertTrue(committing.wait(10))
                self.submit('t-finalize-b')
                self.assertTrue(waiting.wait(5))
                self.assertFalse(any(row['task'] == 't-finalize-b' for row in self.log))
                self.assertEqual(task_leases.holders()['model:home']['task_id'], 't-finalize-a')
            finally:
                release.set()
            states = self.await_settled([run_a, run_b])
        self.assertTrue(all(state['status'] == 'completed' for state in states), states)

    def test_conflicting_worktrees_detected_and_integration_gates_delivery(self):
        self.hold = 0.2
        run_a = self.add_task('t-conf-a', files=('a.py', 'shared.txt'))
        run_b = self.add_task('t-conf-b', files=('a.py', 'shared.txt'))
        self.submit('t-conf-a')
        self.submit('t-conf-b')
        states = self.await_settled([run_a, run_b])
        self.assertTrue(all(state['status'] == 'completed' for state in states), states)
        branches = [state['workspace']['branch'].rsplit('/', 1)[1] for state in states]
        # Two deliverables editing shared.txt incompatibly: precheck must catch
        # the conflict before any integration is attempted.
        commit_on_branch(self.git_project, branches[0], {'shared.txt': 'from a\n'})
        commit_on_branch(self.git_project, branches[1], {'shared.txt': 'from b\n'})
        report = workspace.precheck_merge(self.git_project, branches)
        self.assertFalse(report['clean'])
        self.assertEqual(report['pairs'][0]['conflicts'], ['shared.txt'])
        conflict = task_integration.run_integration(
            self.git_project, branches, name='conflicting', baseline=self.baseline,
            checks=[[sys.executable, '-c', 'raise AssertionError("must not run")']])
        self.assertEqual(conflict['verdict'], 'merge_conflict')
        blocked = task_integration.run_integration(
            self.git_project, [branches[0]], name='failing-check', baseline=self.baseline,
            checks=[[sys.executable, '-c', 'raise SystemExit(7)']])
        self.assertEqual(blocked['verdict'], 'check_failed')
        self.assertEqual(blocked['checks'][0]['exit_code'], 7)
        # The non-conflicting branch passes integration with a real check that
        # reads the merged deliverable; the user's project is never written.
        record = task_integration.run_integration(
            self.git_project, [branches[0]], name='acceptance-run', baseline=self.baseline,
            checks=[[sys.executable, '-c',
                     'from pathlib import Path;'
                     'assert "deliverable of t-conf-a" in Path("t-conf-a.txt").read_text();'
                     'print("INTEGRATION_OK")']])
        self.assertEqual(record['verdict'], 'passed')
        self.assertEqual((self.git_project / 'a.py').read_text(encoding='utf-8'), 'def one():\n    return 1\n')
        status = git(self.git_project, 'status', '--porcelain')
        self.assertTrue(all(row.endswith('.model-desk/') or row.endswith('-task-workspaces/')
                            for row in status.splitlines()), status)

    def test_paid_tasks_serialize_only_when_cap_enforced(self):
        self.start_barrier = threading.Barrier(2)
        self.hold = 0.3
        run_a = self.add_task('t-paid-a', model_id=self.paid_model['id'])
        run_b = self.add_task('t-paid-b', model_id=self.paid_model2['id'])
        self.submit('t-paid-a')
        self.submit('t-paid-b')
        self.await_settled([run_a, run_b])
        (start_a, end_a), (start_b, end_b) = self.intervals(['t-paid-a', 't-paid-b']).values()
        self.assertLess(max(start_a, start_b), min(end_a, end_b),
                        'with the cap disabled, paid tasks must behave like any other')
        self.start_barrier = None
        ledger = Path(registry.HERE) / 'budget-ledger.json'
        before = ledger.read_bytes() if ledger.exists() else b''
        workflow_settings.save_settings({**self.settings, 'enforce_paid_budget': True})
        self.log.clear()
        run_c = self.add_task('t-paid-c', model_id=self.paid_model['id'])
        run_d = self.add_task('t-paid-d', model_id=self.paid_model2['id'])
        self.submit('t-paid-c')
        self.submit('t-paid-d')
        self.await_settled([run_c, run_d])
        (start_c, end_c), (start_d, end_d) = self.intervals(['t-paid-c', 't-paid-d']).values()
        self.assertGreaterEqual(max(start_c, start_d), min(end_c, end_d) - 0.01,
                                'enforced budget let paid tasks overlap')
        self.assertEqual(task_leases.holders(), {})
        after = ledger.read_bytes() if ledger.exists() else b''
        self.assertEqual(before, after, 'the fake workers never bill; the ledger must not move')
        self.assertTrue(workflow_settings.snapshot()['enforce_paid_budget'])

    def test_cancelling_one_task_leaves_the_other_intact(self):
        self.hold = 0.6
        run_a = self.add_task('t-cancel-a', model_id='home')
        run_b = self.add_task('t-cancel-b', model_id=self.second_model['id'])
        self.submit('t-cancel-a')
        self.submit('t-cancel-b')
        self.assertTrue(self.started['t-cancel-a'].wait(10))
        (run_a / 'cancelled.flag').touch()
        states = self.await_settled([run_a, run_b])
        by_id = {state['id']: state for state in states}
        self.assertEqual(by_id['t-cancel-a']['status'], 'cancelled')
        self.assertEqual(by_id['t-cancel-b']['status'], 'completed')
        self.assertEqual(task_leases.holders(), {})
        self.assertIsNone(by_id['t-cancel-a']['workspace'].get('commit'))
        self.assertTrue(by_id['t-cancel-b']['workspace'].get('commit'))
        self.assertTrue(Path(by_id['t-cancel-a']['workspace']['path']).exists())


if __name__ == '__main__':
    unittest.main()
