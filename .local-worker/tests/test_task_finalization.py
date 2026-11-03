"""Executor terminal state must not outrun controller settlement, offline."""
import concurrent.futures
import contextlib
import io
import sys
import threading
import unittest
from unittest.mock import Mock, patch
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import completion_wait
import delegation_policy as policy
import dispatch_task
import model_registry as registry
import panel_server as panel
import task_finalization as finalization
import workflow_settings
import execution_resources as resources
import workbuddy_worker
import zcode_worker
import psutil
import process_identity
from test_stage_workflow import Base


class FinalizationTests(Base):
    def setUp(self):
        super().setUp()
        workflow_settings.save_settings({'enabled': True, 'paid_allowed': True, 'model_scope': 'all',
                                         'enforce_paid_budget': False, 'enforce_model_call_limit': False})

    def completed(self, name='t-settlement', **fields):
        run = self.task(name, status='completed', ended_at=registry.now(), **fields)
        policy.record_executor(run, {'completed': True, 'changed_files': []})
        return run

    def criteria(self, run):
        return [{'id': row['id'], 'status': 'pass',
                 'evidence': [{'source': 'reviewer_test', 'detail': 'independent fixture', 'exit_code': 0}]}
                for row in policy.load(run)['criteria']]

    def test_legacy_records_keep_compatibility_but_malformed_marker_refuses(self):
        self.assertEqual(finalization.view({})['state'], 'legacy')
        finalization.require_settled({})
        for value in (None, 'broken', {}, {'schema': 1, 'state': 'unknown'}, {'schema':True,'state':'settled'}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                finalization.require_settled({finalization.FIELD: value})

    def test_pending_blocks_accept_and_rework_without_mutating_review(self):
        run = self.completed(controller_finalization=finalization.pending())
        original = (run / 'review.json').read_bytes()
        with self.assertRaisesRegex(ValueError, '收尾尚未确认'):
            policy.accept(run, 'main', self.criteria(run))
        with self.assertRaisesRegex(ValueError, '收尾尚未确认'):
            policy.request_rework(run, 'main', ['c1'], 'fixture', 'a.py')
        self.assertEqual((run / 'review.json').read_bytes(), original)
        finalization.settle(run)
        self.assertEqual(policy.accept(run, 'main', self.criteria(run))['state'], policy.ACCEPTED)

    def test_completion_notification_waits_for_settlement_and_reports_attention(self):
        row = {'status': 'completed', 'ended_at': registry.now(), finalization.FIELD: finalization.pending()}
        self.assertIsNone(completion_wait.evaluate([row]))
        row[finalization.FIELD] = {'schema': 1, 'state': 'settled'}
        self.assertEqual(completion_wait.evaluate([row]), 'awaiting_review')
        row[finalization.FIELD] = {'schema': 1, 'state': 'attention', 'note': 'commit failed'}
        self.assertEqual(completion_wait.evaluate([row]), 'needs_attention')

    def test_cli_does_not_return_completed_before_controller_settles(self):
        waiting = {'id': 'fixture', 'status': 'completed', finalization.FIELD: finalization.pending()}
        done = {**waiting, finalization.FIELD: {'schema': 1, 'state': 'settled'}}
        with patch.object(dispatch_task, 'read_task_state', side_effect=[waiting, done]) as read, \
                patch.object(dispatch_task.time, 'sleep'), patch.object(dispatch_task, 'packet_for', return_value={}), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(dispatch_task.wait_for_task(None, 'fixture'), 0)
        self.assertEqual(read.call_count, 2)

    def test_restart_of_terminal_pending_marks_attention_without_replay(self):
        run = self.completed(controller_finalization=finalization.pending())
        with patch.object(panel.EXECUTOR, 'submit') as submit:
            actions = panel.recover_pending_tasks([{'run': run}])
        submit.assert_not_called()
        row = registry.read_task(run)
        self.assertEqual(row['status'], 'completed')
        self.assertEqual(finalization.view(row)['state'], 'attention')
        self.assertEqual(actions[0]['kind'], 'controller_finalize_interrupted')
        with self.assertRaises(ValueError):
            panel.recover_task(row['id'], {'confirm': True})

    def test_cli_recovery_pending_never_waits_for_a_nonexistent_finalizer(self):
        row={'id':'fixture','status':'recovery_pending',finalization.FIELD:finalization.pending()}
        with patch.object(dispatch_task,'read_task_state',return_value=row), \
                patch.object(dispatch_task.time,'sleep',side_effect=AssertionError('must return')), \
                patch.object(dispatch_task,'packet_for',return_value={}), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(dispatch_task.wait_for_task(None,'fixture'),1)

    def test_cancelling_unstarted_queue_settles_without_running_executor(self):
        run = self.task('t-cancel-finalize', status='queued', controller_finalization=finalization.pending())
        with patch.object(panel.subprocess, 'Popen') as popen:
            panel.cancel_task('t-cancel-finalize')
        popen.assert_not_called()
        row = registry.read_task(run)
        self.assertEqual(row['status'], 'cancelled')
        self.assertEqual(finalization.view(row)['state'], 'settled')

    def test_real_controller_keeps_terminal_result_pending_until_commit_returns(self):
        run = self.task('t-controller-finalize', status='queued')
        committing, release = threading.Event(), threading.Event()

        def worker_ended():
            registry.patch_task(run, status='completed', ended_at=registry.now())
            policy.record_executor(run, {'completed': True, 'changed_files': []})
            return 0

        def commit(*_):
            committing.set()
            if not release.wait(5):
                raise AssertionError('test did not release controller finalizer')

        process = Mock(pid=424242)
        process.wait.side_effect = worker_ended
        with patch.object(panel.subprocess, 'Popen', return_value=process), \
                patch.object(panel, 'commit_workspace_for_run', side_effect=commit), \
                concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(panel.execute_task, 't-controller-finalize')
            try:
                self.assertTrue(committing.wait(5))
                row = registry.read_task(run)
                self.assertEqual(row['status'], 'completed')
                self.assertEqual(finalization.view(row)['state'], 'pending')
                self.assertIsNone(completion_wait.evaluate([row]))
                with self.assertRaises(ValueError):
                    policy.accept(run, 'main', self.criteria(run))
            finally:
                release.set()
            future.result(timeout=5)
        row = registry.read_task(run)
        self.assertEqual(finalization.view(row)['state'], 'settled')
        self.assertEqual(completion_wait.evaluate([row]), 'awaiting_review')

    def test_cancel_during_prepare_exception_is_terminal_not_unmanaged_running(self):
        run=self.task('t-cancel-prepare',status='queued',engine='workbuddy')
        def refuse(*args,**kwargs):
            (run/'cancelled.flag').touch()
            raise ValueError('fixture preparation failed')
        with patch.object(panel,'scope_gate',side_effect=refuse),patch.object(panel.subprocess,'Popen') as spawn:
            panel.execute_task('t-cancel-prepare')
        spawn.assert_not_called()
        row=registry.read_task(run)
        self.assertEqual(row['status'],'cancelled')
        self.assertTrue(row['ended_at'])
        self.assertIn('fixture preparation failed',row['controller_error'])
        self.assertEqual(finalization.view(row)['state'],'settled')
        self.assertNotIn(row['id'],panel.MANAGED)

    def test_native_cancel_is_observed_by_worker_without_taskkill(self):
        run=self.task('t-native-cancel',status='queued',engine='workbuddy')
        def spawn(*args,**kwargs):
            (run/'cancelled.flag').touch()
            return process
        def ended():
            registry.patch_task(run,status='cancelled',ended_at=registry.now(),workbuddy={'cleanup':'closed'})
            return 0
        process=Mock(pid=424242)
        process.wait.side_effect=ended
        with patch.object(panel.subprocess,'Popen',side_effect=spawn),patch.object(panel.subprocess,'run') as command:
            panel.execute_task('t-native-cancel')
        command.assert_not_called()
        self.assertEqual(finalization.view(registry.read_task(run))['state'],'settled')
        self.assertEqual(resources.snapshot(),{})

    def test_unconfirmed_process_retains_resources_until_explicit_proven_finalization(self):
        run=self.task('t-process-unknown',status='queued',engine='pi')
        process=Mock(pid=424242)
        process.wait.side_effect=OSError('fixture wait interrupted')
        with patch.object(panel.subprocess,'Popen',return_value=process):
            panel.execute_task('t-process-unknown')
        row=registry.read_task(run)
        self.assertEqual(row['status'],'failed')
        self.assertEqual(finalization.view(row)['state'],'attention')
        self.assertIn(row['id'],resources.snapshot())
        with patch.object(panel,'recorded_worker_alive',return_value=panel.WORKER_STATE_UNKNOWN):
            with self.assertRaises(ValueError):panel.recover_task(row['id'],{'confirm':True,'finalize_only':True})
        self.assertIn(row['id'],resources.snapshot())
        with patch.object(panel,'recorded_worker_alive',return_value=False),patch.object(panel.EXECUTOR,'submit') as submit:
            result=panel.recover_task(row['id'],{'confirm':True,'finalize_only':True})
        submit.assert_not_called()
        self.assertFalse(result['executor_replayed'])
        self.assertEqual(resources.snapshot(),{})
        self.assertEqual(finalization.view(registry.read_task(run))['state'],'settled')

    def test_native_unknown_session_closure_cannot_be_cleared_by_dead_worker(self):
        run=self.completed('t-native-unknown',engine='zcode',launcher_pid=424242,
                           zcode={'cleanup':'unknown'},controller_finalization=finalization.pending())
        row=registry.read_task(run)
        resources.acquire(row['id'],row['project'],resources.requests_for(row,{},{}))
        with patch.object(panel,'recorded_worker_alive',return_value=False),patch.object(panel.EXECUTOR,'submit') as submit:
            with self.assertRaisesRegex(ValueError,'原生会话关闭未确认'):
                panel.recover_task(row['id'],{'confirm':True,'finalize_only':True})
        submit.assert_not_called()
        self.assertIn(row['id'],resources.snapshot())

    def test_workbuddy_early_cancel_publishes_not_created_without_model_lookup(self):
        for engine,module in [('workbuddy',workbuddy_worker),('zcode',zcode_worker)]:
            run=self.task('t-early-'+engine,status='running',engine=engine)
            (run/'cancelled.flag').touch()
            with patch.object(sys,'argv',[engine+'_worker.py','--run-id',run.name]), \
                    patch.object(registry,'get_model') as model:
                self.assertEqual(module.main(),1)
            model.assert_not_called()
            row=registry.read_task(run)
            self.assertEqual(row['status'],'cancelled')
            self.assertEqual(row[engine]['cleanup'],'not_created')

    def test_restart_cancelled_flag_becomes_terminal_without_replay(self):
        run=self.task('t-restart-cancel',status='running',controller_finalization=finalization.pending())
        (run/'cancelled.flag').touch()
        with patch.object(panel,'recorded_worker_alive',return_value=False),patch.object(panel.EXECUTOR,'submit') as submit:
            panel.recover_pending_tasks([{'run':run}])
        submit.assert_not_called()
        self.assertEqual(registry.read_task(run)['status'],'cancelled')
        self.assertEqual(finalization.view(registry.read_task(run))['state'],'attention')

    def test_settled_or_accepted_task_cannot_rewrite_previously_accepted_results(self):
        for name,marker,accepted in [('settled',{'schema':1,'state':'settled'},False),
                                     ('accepted',{'schema':1,'state':'attention'},True)]:
            run=self.completed('t-no-recommit-'+name,controller_finalization=marker)
            if accepted:
                record=policy.load(run)
                record['state']='accepted'
                registry.write_json(run/'review.json',record)
            with patch.object(panel,'commit_workspace_for_run') as commit,patch.object(resources,'release') as release:
                with self.assertRaises(ValueError):
                    panel.recover_task(run.name,{'confirm':True,'finalize_only':True})
            commit.assert_not_called()
            release.assert_not_called()

    def test_surviving_node_wrapper_prevents_releasing_dead_python_workers_resources(self):
        run=self.completed('t-node-alive',engine='pi',process_pid=424242,started_at='2026-10-25T00:00:00+00:00',
                           controller_finalization=finalization.pending())
        process=Mock()
        process.cmdline.return_value=['node.exe',str(panel.HERE/'agent-runtime/run-pi.mjs'),str(run/'pi-job.json')]
        process.create_time.return_value=1792900000
        with patch.object(psutil,'Process',return_value=process),patch.object(resources,'release') as release:
            self.assertIs(panel.recorded_worker_alive(registry.read_task(run)),True)
            with self.assertRaisesRegex(ValueError,'进程仍在运行或身份未知'):
                panel.recover_task(run.name,{'confirm':True,'finalize_only':True})
        release.assert_not_called()

    def test_child_process_hash_and_birth_time_prevent_pid_reuse_confusion(self):
        process=Mock()
        arguments=['node.exe','fixture-native-cli','--session','fixture']
        process.cmdline.return_value=arguments
        process.create_time.return_value=1000.0
        state={'child_pid':424242,'child_process_identity':{'pid':424242,'created_at':1000.0,
                   'command_sha256':process_identity.command_digest(arguments)}}
        with patch.object(psutil,'Process',return_value=process):
            self.assertIs(panel.recorded_worker_alive(state),True)
            process.create_time.return_value=2000.0
            self.assertIs(panel.recorded_worker_alive(state),False)
            self.assertEqual(panel.recorded_worker_alive({'child_pid':424242}),panel.WORKER_STATE_UNKNOWN)

    def test_interrupted_task_can_settle_for_main_takeover_without_replaying_or_changing_raw_status(self):
        run=self.completed('t-interrupted-takeover',
                           controller_finalization={'schema':1,'state':'attention','note':'controller interrupted'})
        registry.patch_task(run,status='recovery_pending')
        policy.record_executor(run,{'completed':False,'changed_files':[]})
        with patch.object(panel.EXECUTOR,'submit') as submit,patch.object(panel,'recorded_worker_alive',return_value=False):
            result=panel.recover_task(run.name,{'confirm':True,'finalize_only':True})
        submit.assert_not_called()
        self.assertEqual(result['controller_finalization']['state'],'settled')
        self.assertEqual(registry.read_task(run)['status'],'recovery_pending')
        accepted=policy.final_accept(run,'main',self.criteria(run),'main verified actual results')
        self.assertEqual(accepted['state'],'final_accepted')


if __name__ == '__main__':
    unittest.main()
