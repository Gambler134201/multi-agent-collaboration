"""Dispatch wiring for workspace isolation: plan at creation, worktree at execution.

No model is called: submissions are queued with a patched executor and the
controller-side prepare/commit helpers are driven directly on fixture records.
"""
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import panel_server as panel
import model_registry as registry
import execution_engines as engines
import workflow_settings
import task_workspace as workspace
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
    (path / 'a.py').write_text('def one():\n    return 1\n', encoding='utf-8')
    git(path, 'add', '-A')
    git(path, 'commit', '-m', 'baseline')
    return path


class DispatchWorkspaceTests(Base):
    def setUp(self):
        super().setUp()
        workflow_settings.save_settings({'enabled': True, 'paid_allowed': True, 'model_scope': 'all',
                                         'enforce_paid_budget': False, 'enforce_model_call_limit': False})
        self.enterContext(patch.object(engines, 'availability', return_value={'ready': True}))
        self.enterContext(patch.object(panel, 'connectivity_gate',
                                       return_value={'blocked': False, 'checked': False, 'message': '',
                                                     'verdict': 'fixture', 'check': None}))
        self.git_project = make_repo(Path(self.temp.name) / 'git-project')

    def submit_edit(self, project, files=('a.py',)):
        body = {'kind': 'edit', 'model_id': registry.list_models()['default_model'],
                'route_mode': 'manual', 'project': str(project), 'files': list(files),
                'prompt': 'fixture deliverable', 'engine': 'dsh', 'source': 'codex',
                'routing': {'acceptance': ['fixture']}}
        with patch.object(panel.EXECUTOR, 'submit'):
            row = panel.submit_task(body)
        return registry.task_dir(row['id'])

    def test_git_project_records_baseline_plan_at_creation(self):
        run = self.submit_edit(self.git_project)
        plan = registry.read_task(run)['workspace']
        self.assertTrue(plan['eligible'])
        self.assertEqual(plan['baseline'], git(self.git_project, 'rev-parse', 'HEAD').strip())
        self.assertIsNone(registry.read_task(run).get('workspace_commit_error'))

    def test_dependent_task_starts_from_accepted_prerequisite_code(self):
        parent=workspace.prepare(self.git_project,'prerequisite')
        (Path(parent['path'])/'a.py').write_text('def one():\n    return 42\n',encoding='utf-8')
        commit=workspace.commit_task_results(parent['path'],'prerequisite')['commit']
        body={'kind':'edit','model_id':'home','route_mode':'manual','project':str(self.git_project),
              'files':['a.py'],'prompt':'fixture','engine':'dsh','routing':{'acceptance':['fixture']}}
        with patch.object(panel.EXECUTOR,'submit'):
            row=panel.submit_task(body,prerequisite_baseline=commit)
        run=registry.task_dir(row['id'])
        plan=panel.prepare_workspace_for_run(run,registry.read_task(run))
        self.assertEqual(plan['baseline'],commit)
        self.assertIn('return 42',(Path(plan['path'])/'a.py').read_text())
        self.assertIn('return 1',(self.git_project/'a.py').read_text())


    def test_commit_skips_ignored_local_state_without_exclude_error(self):
        project=make_repo(Path(self.temp.name)/'ignored-local')
        (project/'.gitignore').write_text('/*\n!/.gitignore\n!/docs/\n',encoding='utf-8')
        (project/'.zcode').mkdir()
        (project/'.zcode'/'session.json').write_text('{}',encoding='utf-8')
        (project/'docs').mkdir()
        (project/'docs'/'run.md').write_text('deliverable\n',encoding='utf-8')
        result=workspace.commit_task_results(str(project),'fixture-ignored')
        self.assertTrue(result['committed'])
        show=workspace._git(project,'show','--name-only','--format=',result['commit'])
        files=[line for line in show.stdout.splitlines() if line.strip()]
        self.assertIn('docs/run.md',files)
        self.assertFalse(any(name.startswith('.zcode') for name in files))

    def test_commit_excludes_unignored_local_controller_state(self):
        project=make_repo(Path(self.temp.name)/'unignored-local')
        (project/'.zcode').mkdir()
        (project/'.zcode'/'session.json').write_text('{}',encoding='utf-8')
        (project/'docs').mkdir()
        (project/'docs'/'run.md').write_text('deliverable\n',encoding='utf-8')
        result=workspace.commit_task_results(str(project),'fixture-unignored')
        self.assertTrue(result['committed'])
        show=workspace._git(project,'show','--name-only','--format=',result['commit'])
        files=[line for line in show.stdout.splitlines() if line.strip()]
        self.assertIn('docs/run.md',files)
        self.assertFalse(any(name.startswith('.zcode') for name in files))
        status=workspace._git(project,'status','--porcelain').stdout
        self.assertIn('.zcode',status)

    def test_commit_excludes_the_declared_task_temp_directory(self):
        project=make_repo(Path(self.temp.name)/'temp-dir-commit')
        (project/workspace.TASK_TEMP_DIR).mkdir()
        (project/workspace.TASK_TEMP_DIR/'pytest.log').write_text('scratch\n',encoding='utf-8')
        (project/'docs').mkdir()
        (project/'docs'/'run.md').write_text('deliverable\n',encoding='utf-8')
        result=workspace.commit_task_results(str(project),'fixture-temp')
        self.assertTrue(result['committed'])
        show=workspace._git(project,'show','--name-only','--format=',result['commit'])
        files=[line for line in show.stdout.splitlines() if line.strip()]
        self.assertIn('docs/run.md',files)
        self.assertFalse(any(name.startswith(workspace.TASK_TEMP_DIR) for name in files))
        # A worktree holding only scratch material commits nothing on purpose;
        # the material stays in place until a human recycles the copy.
        scratch=make_repo(Path(self.temp.name)/'temp-dir-only')
        (scratch/workspace.TASK_TEMP_DIR).mkdir()
        (scratch/workspace.TASK_TEMP_DIR/'pytest.log').write_text('scratch\n',encoding='utf-8')
        self.assertEqual(workspace.commit_task_results(str(scratch),'fixture-temp-only'),
                         {'committed':False,'reason':'no_relevant_changes'})

    def test_non_git_project_records_serial_reason(self):
        plain = Path(self.temp.name) / 'plain-project'
        plain.mkdir()
        (plain / 'a.py').write_text('x = 1\n', encoding='utf-8')
        run = self.submit_edit(plain)
        plan = registry.read_task(run)['workspace']
        self.assertFalse(plan['eligible'])
        self.assertIn('不自动初始化', plan['reason'])

    def test_dirty_declared_files_block_isolation(self):
        (self.git_project / 'a.py').write_text('dirty user change\n', encoding='utf-8')
        run = self.submit_edit(self.git_project)
        plan = registry.read_task(run)['workspace']
        self.assertFalse(plan['eligible'])
        self.assertIn('未提交改动', plan['reason'])
        self.assertTrue(any('a.py' in row for row in plan['dirty']))

    def test_chat_tasks_get_no_workspace_plan(self):
        body = {'kind': 'chat', 'model_id': registry.list_models()['default_model'],
                'route_mode': 'manual', 'project': str(self.git_project), 'files': [],
                'prompt': 'read only question', 'engine': 'dsh', 'source': 'codex'}
        with patch.object(panel.EXECUTOR, 'submit'):
            row = panel.submit_task(body)
        self.assertIsNone(registry.read_task(registry.task_dir(row['id']))['workspace'])

    def test_prompt_mentioning_project_path_gets_nonblocking_notice(self):
        base = {'kind': 'edit', 'model_id': registry.list_models()['default_model'],
                'route_mode': 'manual', 'project': str(self.git_project), 'files': ['a.py'],
                'engine': 'dsh', 'source': 'codex', 'routing': {'acceptance': ['fixture']}}
        with patch.object(panel.EXECUTOR, 'submit'):
            row = panel.submit_task({**base, 'prompt': 'run git rev-parse HEAD inside ' + str(self.git_project)})
        self.assertIn('workspace_isolation', row['prompt_notice'])
        with patch.object(panel.EXECUTOR, 'submit'):
            clean = panel.submit_task({**base, 'prompt': 'fixture deliverable'})
        self.assertFalse(clean.get('prompt_notice'))

    def test_plan_inherits_parent_deliverable_commit(self):
        parent_plan = {'created': True, 'commit': 'abc123', 'branch': 'modeldesk-task/parent'}
        plan = panel.workspace_plan_for('dsh', 'edit', str(self.git_project), ['a.py'],
                                        link_parent={'workspace': parent_plan})
        self.assertEqual(plan['baseline'], 'abc123')
        self.assertEqual(plan['based_on'], 'modeldesk-task/parent')

    def test_absolute_original_test_command_stays_serial_without_rewrite(self):
        command = 'python "' + str(self.git_project / 'a.py') + '"'
        plan = panel.workspace_plan_for('zcode', 'edit', str(self.git_project), ['a.py'],
                                        test_command=command)
        self.assertFalse(plan['eligible'])
        self.assertIn('未改写命令', plan['reason'])
        plan = panel.workspace_plan_for('zcode', 'edit', str(self.git_project), ['a.py'],
                                        test_command='python a.py')
        self.assertTrue(plan['eligible'])

    def test_prepare_creates_worktree_and_commit_records_deliverable(self):
        run = self.submit_edit(self.git_project)
        state = registry.read_task(run)
        plan = panel.prepare_workspace_for_run(run, state)
        self.assertTrue(plan['created'])
        worktree = Path(plan['path'])
        self.assertTrue(worktree.is_dir())
        self.assertEqual(git(worktree, 'rev-parse', 'HEAD').strip(), plan['baseline'])
        self.assertEqual(registry.read_task(run)['workspace']['path'], str(worktree))
        # Reruns are refused: a crashed attempt never silently reuses a branch.
        with self.assertRaisesRegex(ValueError, '已存在'):
            panel.prepare_workspace_for_run(run, state)
        # Deliverable plus controller droppings: only the deliverable is committed.
        registry.patch_task(run, status='completed')
        (worktree / 'result.txt').write_text('deliverable\n', encoding='utf-8')
        (worktree / '.zcode').mkdir()
        (worktree / '.zcode' / 'config.json').write_text('{}\n', encoding='utf-8')
        committed = panel.commit_workspace_for_run(run, registry.read_task(run))
        self.assertTrue(committed['committed'])
        names = git(worktree, 'show', '--name-only', '--format=', 'HEAD')
        self.assertIn('result.txt', names)
        self.assertNotIn('.zcode/config.json', names)
        self.assertEqual(registry.read_task(run)['workspace']['commit'], committed['commit'])
        self.assertEqual(panel.commit_workspace_for_run(run, registry.read_task(run)),
                         {'committed': False, 'reason': 'no_relevant_changes'})
        # The user's own tree stays untouched: only the workbench record
        # directory appears (untracked), and the business file is unchanged.
        status = git(self.git_project, 'status', '--porcelain')
        self.assertTrue(status)
        self.assertTrue(all(row.endswith('.model-desk/') for row in status.splitlines()))
        self.assertEqual((self.git_project / 'a.py').read_text(encoding='utf-8'),
                         'def one():\n    return 1\n')

    def test_prepare_skips_ineligible_and_non_wired_engines(self):
        plain = Path(self.temp.name) / 'plain-project'
        plain.mkdir()
        (plain / 'a.py').write_text('x = 1\n', encoding='utf-8')
        run = self.submit_edit(plain)
        self.assertIsNone(panel.prepare_workspace_for_run(run, registry.read_task(run)))
        state = registry.read_task(run)
        state.update(engine='zcode', workspace={'eligible': True})
        self.assertIsNone(panel.prepare_workspace_for_run(run, state))

    def test_commit_requires_completed_workspace_task(self):
        run = self.submit_edit(self.git_project)
        panel.prepare_workspace_for_run(run, registry.read_task(run))
        registry.patch_task(run, status='failed')
        self.assertIsNone(panel.commit_workspace_for_run(run, registry.read_task(run)))
        registry.patch_task(run, status='completed', workspace=None)
        self.assertIsNone(panel.commit_workspace_for_run(run, registry.read_task(run)))


class LeaseWiringTests(Base):
    def setUp(self):
        super().setUp()
        workflow_settings.save_settings({'enabled': True, 'paid_allowed': True, 'model_scope': 'all',
                                         'enforce_paid_budget': False, 'enforce_model_call_limit': False})

    def state(self, task_id, engine='dsh', model_id='home'):
        run = self.task(task_id)
        return run, {'id': task_id, 'engine': engine, 'model_id': model_id,
                     'model': 'qwen-test', 'project': str(self.project)}

    def test_model_lease_is_taken_and_owner_released(self):
        run, state = self.state('t-lease-a')
        lease = panel.acquire_model_lease(run, state)
        self.assertEqual(lease['resource'], 'model:home')
        self.assertEqual(panel.task_leases.holders()['model:home']['task_id'], 't-lease-a')
        record = registry.read_task(run)
        self.assertEqual(record['model_lease']['resource'], 'model:home')
        self.assertIsNone(record['model_lease']['recovered_from'])
        panel.release_task_leases(state)
        self.assertEqual(panel.task_leases.holders(), {})
        panel.release_task_leases(state)

    def test_terminal_holder_is_recovered_with_evidence(self):
        old_run, _ = self.state('t-lease-old')
        registry.patch_task(old_run, status='completed')
        panel.task_leases.acquire('model', 'home', 't-lease-old')
        run, state = self.state('t-lease-new')
        lease = panel.acquire_model_lease(run, state)
        self.assertTrue(lease.get('recovered'))
        self.assertEqual(registry.read_task(run)['model_lease']['recovered_from'], 't-lease-old')
        self.assertEqual(panel.task_leases.holders()['model:home']['task_id'], 't-lease-new')
        panel.release_task_leases(state)

    def test_terminal_holder_still_finalizing_is_not_taken_over(self):
        old_run, _ = self.state('t-finishing')
        registry.patch_task(old_run, status='completed')
        panel.task_leases.acquire('model', 'home', 't-finishing')
        panel.MANAGED.add('t-finishing')
        self.addCleanup(panel.MANAGED.discard, 't-finishing')
        run, state = self.state('t-wait-finalizer')
        with patch.object(panel.threading, 'Timer'):
            self.assertEqual(panel.acquire_model_lease(run, state), panel.LEASE_WAITING)
        self.assertEqual(panel.task_leases.holders()['model:home']['task_id'], 't-finishing')

    def test_recovery_checks_holder_under_record_lock(self):
        old_run, _ = self.state('t-locked-old')
        registry.patch_task(old_run, status='completed')
        panel.task_leases.acquire('model', 'home', 't-locked-old')
        run, state = self.state('t-locked-new')
        real_recover = panel.task_leases.recover
        entered = threading.Event()
        changed = threading.Event()

        def change_record():
            entered.set()
            registry.patch_task(old_run, status='running')
            changed.set()

        thread = threading.Thread(target=change_record)

        def recover(*args, **kwargs):
            thread.start()
            self.assertTrue(entered.wait(2))
            self.assertFalse(changed.wait(0.1), 'holder lock was released before transfer')
            return real_recover(*args, **kwargs)

        try:
            with patch.object(panel.task_leases, 'recover', side_effect=recover):
                self.assertTrue(panel.acquire_model_lease(run, state)['recovered'])
        finally:
            thread.join(3)
        self.assertTrue(changed.is_set())

    def test_terminal_holder_with_live_or_unknown_worker_is_not_recovered(self):
        old_run, _ = self.state('t-worker-alive')
        registry.patch_task(old_run, status='completed')
        panel.task_leases.acquire('model', 'home', 't-worker-alive')
        run, state = self.state('t-new-blocked')
        for verdict in (True, panel.WORKER_STATE_UNKNOWN):
            with patch.object(panel, 'recorded_worker_alive', return_value=verdict):
                with self.assertRaisesRegex(ValueError, '占用'):
                    panel.acquire_model_lease(run, state)
        self.assertEqual(panel.task_leases.holders()['model:home']['task_id'], 't-worker-alive')

    def test_holder_released_during_conflict_handling_gets_fresh_acquire(self):
        old_run, _ = self.state('t-release-window')
        registry.patch_task(old_run, status='running')
        panel.task_leases.acquire('model', 'home', 't-release-window')
        run, state = self.state('t-after-release')

        def finish_between_reads(*args, **kwargs):
            panel.task_leases.release('model', 'home', 't-release-window')
            return False

        with patch.object(panel, 'reschedule_for_lease', side_effect=finish_between_reads):
            result = panel.acquire_model_lease(run, state)
        self.assertTrue(result['acquired'])
        self.assertEqual(panel.task_leases.holders()['model:home']['task_id'], 't-after-release')

    def test_bad_lease_registry_is_not_treated_as_a_waitable_conflict(self):
        path = registry.HERE / 'task-leases.json'
        path.write_bytes(b'broken registry')
        run, state = self.state('t-bad-lease')
        with patch.object(panel, 'reschedule_for_lease') as reschedule:
            with self.assertRaisesRegex(ValueError, '注册表无效'):
                panel.acquire_model_lease(run, state)
        reschedule.assert_not_called()
        self.assertEqual(path.read_bytes(), b'broken registry')

    def test_active_holder_refuses_the_task(self):
        old_run, _ = self.state('t-lease-live')
        registry.patch_task(old_run, status='running')
        panel.task_leases.acquire('model', 'home', 't-lease-live')
        run, state = self.state('t-lease-new')
        with self.assertRaisesRegex(ValueError, '占用'):
            panel.acquire_model_lease(run, state)
        self.assertEqual(panel.task_leases.holders()['model:home']['task_id'], 't-lease-live')

    def test_non_agent_engines_hold_the_same_model_lease(self):
        run, state = self.state('t-direct', engine='direct')
        lease = panel.acquire_model_lease(run, state)
        self.assertEqual(lease['resource'], 'model:home')
        self.assertEqual(panel.task_leases.holders()['model:home']['task_id'], 't-direct')
        panel.release_task_leases(state)
        self.assertEqual(panel.task_leases.holders(), {})
        idempotent = panel.release_task_leases(state)
        self.assertEqual(idempotent, [{'resource': 'model:home', 'released': False, 'reason': 'not_held'},
                                      {'resource': 'budget:paid', 'released': False, 'reason': 'not_held'}])
        legacy = dict(state, model_id=None)
        self.assertIsNone(panel.acquire_model_lease(run, legacy))

    def test_cancelled_task_returns_before_any_lease(self):
        run, state = self.state('t-cancelled')
        (run / 'cancelled.flag').touch()
        self.assertIsNone(panel.execute_task('t-cancelled'))
        self.assertEqual(panel.task_leases.holders(), {})

    def managed_holder(self, holder_id):
        old_run, _ = self.state(holder_id)
        registry.patch_task(old_run, status='running')
        panel.task_leases.acquire('model', 'home', holder_id)
        with panel.ACTIVE_LOCK:
            panel.ACTIVE[holder_id] = object()
        self.addCleanup(panel.ACTIVE.pop, holder_id, None)

    def test_managed_holder_reschedules_instead_of_failing(self):
        self.managed_holder('t-lease-live')
        run, state = self.state('t-lease-new')
        calls = []
        self.enterContext(patch.object(panel, 'LEASE_WAIT_BACKOFF', 0.05))
        self.enterContext(patch.object(panel.EXECUTOR, 'submit',
                                       side_effect=lambda fn, *a, **kw: calls.append(fn)))
        outcome = panel.acquire_model_lease(run, state)
        self.assertEqual(outcome, panel.LEASE_WAITING)
        record = registry.read_task(run)
        self.assertEqual(record['status'], 'queued')
        self.assertIn('等待模型占用', record['phase'])
        self.assertEqual(record['lease_wait'], {'count': 1, 'last_at': record['lease_wait']['last_at'],
                                                'holder': 't-lease-live'})
        deadline = time.time() + 3
        while not calls and time.time() < deadline:
            time.sleep(0.02)
        self.assertEqual(calls, [panel.execute_task])
        self.assertEqual(panel.task_leases.holders()['model:home']['task_id'], 't-lease-live')

    def test_second_wait_increments_and_cancelled_wait_never_resubmits(self):
        self.managed_holder('t-lease-live')
        run, state = self.state('t-lease-new')
        registry.patch_task(run, lease_wait={'count': 4, 'last_at': 'earlier', 'holder': 't-lease-live'})
        state['lease_wait'] = {'count': 4, 'last_at': 'earlier', 'holder': 't-lease-live'}
        calls = []
        self.enterContext(patch.object(panel, 'LEASE_WAIT_BACKOFF', 0.05))
        self.enterContext(patch.object(panel.EXECUTOR, 'submit',
                                       side_effect=lambda fn, *a, **kw: calls.append(fn)))
        self.assertEqual(panel.acquire_model_lease(run, state), panel.LEASE_WAITING)
        self.assertEqual(registry.read_task(run)['lease_wait']['count'], 5)
        (run / 'cancelled.flag').touch()
        time.sleep(0.3)
        self.assertEqual(calls, [])


class ExecutionClaimTests(Base):
    def test_deleted_task_delivery_does_not_recreate_records(self):
        run = registry.task_dir('t-deleted')
        self.assertFalse(run.exists())
        self.assertIsNone(panel.claim_queued_execution(run))
        self.assertFalse(run.exists())

    def test_preparing_attempt_blocks_history_deletion_before_popen(self):
        panel.MANAGED.add('t-preparing')
        self.addCleanup(panel.MANAGED.discard, 't-preparing')
        with self.assertRaisesRegex(ValueError, '队列空闲'):
            panel.history_idle_guard()

    def test_concurrent_queue_deliveries_have_one_winner(self):
        run = self.task('t-claim', status='queued')
        barrier = threading.Barrier(2)

        def claim():
            barrier.wait(3)
            return panel.claim_queued_execution(run)

        try:
            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(lambda _: claim(), range(2)))
            self.assertEqual(sum(row is not None for row in results), 1)
            self.assertEqual(registry.read_task(run)['status'], 'running')
            self.assertIn('t-claim', panel.MANAGED)
        finally:
            panel.MANAGED.discard('t-claim')

    def test_terminal_delivery_never_replays_or_releases_another_lease(self):
        for status in ('completed', 'failed', 'cancelled', 'running', 'recovery_pending'):
            run = self.task('t-claim-old', status=status)
            before = (run / 'task.json').read_bytes()
            with patch.object(panel.subprocess, 'Popen') as popen:
                panel.execute_task('t-claim-old')
            popen.assert_not_called()
            self.assertEqual((run / 'task.json').read_bytes(), before)

    def test_timer_does_not_replay_a_terminal_task(self):
        run = self.task('t-stale-timer', status='queued')
        state = registry.read_task(run)
        panel.MANAGED.add('holder')
        self.addCleanup(panel.MANAGED.discard, 'holder')
        with patch.object(panel.threading, 'Timer') as timer, patch.object(panel.EXECUTOR, 'submit') as submit:
            self.assertTrue(panel.reschedule_for_lease(run, state, 'holder'))
            callback = timer.call_args.args[1]
            registry.patch_task(run, status='completed')
            callback()
        submit.assert_not_called()


class BudgetLeaseTests(Base):
    def setUp(self):
        super().setUp()
        self.paid_settings = {'enabled': True, 'paid_allowed': True, 'model_scope': 'all',
                              'enforce_paid_budget': True, 'enforce_model_call_limit': False}

    def paid_state(self, task_id, engine='dsh'):
        run = self.task(task_id)
        return run, {'id': task_id, 'engine': engine, 'model_id': 'paid-test',
                     'model': 'paid-test', 'project': str(self.project)}

    def test_paid_task_holds_budget_lease_under_enforced_cap(self):
        workflow_settings.save_settings(self.paid_settings)
        run, state = self.paid_state('t-budget-a')
        lease = panel.acquire_budget_lease(run, state, {'provider': 'deepseek'},
                                           workflow_settings.snapshot())
        self.assertEqual(lease['resource'], 'budget:paid')
        self.assertEqual(panel.task_leases.holders()['budget:paid']['task_id'], 't-budget-a')
        self.assertEqual(registry.read_task(run)['budget_lease']['resource'], 'budget:paid')
        panel.release_task_leases(state)
        self.assertNotIn('budget:paid', panel.task_leases.holders())

    def test_disabled_cap_or_local_model_hold_no_budget_lease(self):
        workflow_settings.save_settings({'enabled': True, 'paid_allowed': True, 'model_scope': 'all',
                                         'enforce_paid_budget': False, 'enforce_model_call_limit': False})
        run, state = self.paid_state('t-budget-b')
        self.assertIsNone(panel.acquire_budget_lease(run, state, {'provider': 'deepseek'},
                                                     workflow_settings.snapshot()))
        self.assertIsNone(panel.acquire_budget_lease(run, state, {'provider': 'local'},
                                                     {'enforce_paid_budget': True}))
        self.assertEqual(panel.task_leases.holders(), {})

    def test_budget_conflict_waits_for_managed_and_refuses_unmanaged(self):
        workflow_settings.save_settings(self.paid_settings)
        old_run, _ = self.paid_state('t-budget-live')
        registry.patch_task(old_run, status='running')
        panel.task_leases.acquire('budget', 'paid', 't-budget-live')
        run, state = self.paid_state('t-budget-new')
        model = {'provider': 'deepseek'}
        settings = workflow_settings.snapshot()
        with panel.ACTIVE_LOCK:
            panel.ACTIVE['t-budget-live'] = object()
        self.addCleanup(panel.ACTIVE.pop, 't-budget-live', None)
        calls = []
        self.enterContext(patch.object(panel, 'LEASE_WAIT_BACKOFF', 0.05))
        self.enterContext(patch.object(panel.EXECUTOR, 'submit',
                                       side_effect=lambda fn, *a, **kw: calls.append(fn)))
        self.assertEqual(panel.acquire_budget_lease(run, state, model, settings), panel.LEASE_WAITING)
        deadline = time.time() + 3
        while not calls and time.time() < deadline:
            time.sleep(0.02)
        self.assertEqual(calls, [panel.execute_task])
        panel.ACTIVE.pop('t-budget-live', None)
        with self.assertRaisesRegex(ValueError, '付费任务'):
            panel.acquire_budget_lease(run, state, model, settings)
        self.assertEqual(panel.task_leases.holders()['budget:paid']['task_id'], 't-budget-live')


if __name__ == '__main__':
    unittest.main()
