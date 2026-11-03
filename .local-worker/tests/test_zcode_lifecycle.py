"""ZCode lifecycle recovery: instance pinning, targeted finalization, no resend.

Every fixture is a fabricated terminal task record in a temporary run
directory.  The process table, the native bridge driver and the connection
identity are patched; nothing launches or terminates an application, no real
desktop, session or account is touched.
"""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import psutil
import panel_server as panel
import model_registry as registry
import zcode_worker as worker

SID='modeldesk-zcode-lifecycle-fixture'


class FinalizeSessionTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='zcode-lifecycle-');self.addCleanup(self.temp.cleanup)
        self.project=Path(self.temp.name).resolve()
        self.run=self.project/'.model-desk'/'runs'/'lifecycle-fixture'
        self.run.mkdir(parents=True)
        self.config={'home':str(self.project/'installed'),'port':19765,'access_mode':'project'}
        self.enterContext(patch.object(worker,'configuration',return_value=self.config))
        self.enterContext(patch.object(worker,'installation',return_value={'version':'3.16.3','fingerprint':'fixture'}))
        self.instance={'pid':4242,'created_at':100.0,'command_sha256':'a'*64}

    def record(self,**overrides):
        state={'id':'lifecycle-fixture','engine':'zcode','status':'failed','kind':'edit',
               'project':str(self.project),'zcode_session_id':SID,
               'zcode':{'prompt_attempted':True,'cleanup':'pending'},
               'zcode_connection':dict(self.config),'zcode_instance':dict(self.instance)}
        state.update(overrides)
        registry.write_json(self.run/'task.json',state)
        return state

    def native(self):
        return registry.read_task(self.run).get('zcode') or {}

    def test_unreadable_process_name_does_not_prove_desktop_absent(self):
        from types import SimpleNamespace
        self.record(zcode_instance=None)
        rows=[SimpleNamespace(info={'name':None,'exe':None,'cmdline':None})]
        with patch('psutil.process_iter',return_value=rows),patch.object(worker,'driver') as driver:
            with self.assertRaisesRegex(ValueError,'进程状态无法核实'):
                worker.finalize_session(self.run)
        driver.assert_not_called()
        self.assertEqual(self.native()['cleanup'],'pending')

    def test_malformed_session_id_is_not_missing_identity_evidence(self):
        for sid in ('foreign-existing-session','',False,0,{},'modeldesk-zcode-'):
            with self.subTest(sid=sid):
                self.record(zcode_session_id=sid,zcode={})
                with patch.object(worker,'driver') as driver:
                    with self.assertRaisesRegex(ValueError,'会话身份无效'):
                        worker.finalize_session(self.run)
                driver.assert_not_called()
                self.assertEqual(self.native(),{})

    def test_instance_exited_confirms_close_without_touching_the_bridge(self):
        self.record()
        with patch.object(worker,'instance_matches',return_value=False),patch.object(worker,'driver') as driver:
            result=worker.finalize_session(self.run)
        driver.assert_not_called()
        self.assertTrue(result['finalized']);self.assertTrue(result['closed'])
        self.assertEqual(result['method'],'instance_exited')
        native=self.native()
        self.assertEqual(native['cleanup'],'closed')
        self.assertEqual(native['finalize']['method'],'instance_exited')
        self.assertEqual(native['finalize']['session_id'],SID)

    def test_live_instance_closes_only_this_session_through_the_bridge(self):
        self.record()
        captured={}
        def driver(job,on_event=None):
            captured['job']=dict(job)
            return [{'type':'closed','session_id':job['session_id']}]
        with patch.object(worker,'instance_matches',return_value=True),patch.object(worker,'driver',side_effect=driver):
            result=worker.finalize_session(self.run)
        self.assertTrue(result['finalized']);self.assertEqual(result['method'],'bridge_close')
        self.assertEqual(captured['job']['action'],'finalize')
        self.assertEqual(captured['job']['session_id'],SID)
        self.assertNotIn('prompt',captured['job'])
        self.assertNotIn('selection',captured['job'])
        self.assertEqual(self.native()['cleanup'],'closed')

    def test_bridge_reports_inactive_session_as_confirmed_close(self):
        self.record()
        with patch.object(worker,'instance_matches',return_value=True), \
             patch.object(worker,'driver',return_value=[{'type':'closed','session_id':SID,'inactive':True}]):
            result=worker.finalize_session(self.run)
        self.assertEqual(result['method'],'session_inactive')
        self.assertEqual(self.native()['cleanup'],'closed')

    def test_unknown_bridge_outcome_keeps_resources_retained(self):
        self.record()
        with patch.object(worker,'instance_matches',return_value=True), \
             patch.object(worker,'driver',return_value=[{'type':'error','message':'ZCode 原生通道断开；结果未知，未重试'}]):
            with self.assertRaisesRegex(ValueError,'保留资源'):
                worker.finalize_session(self.run)
        self.assertEqual(self.native()['cleanup'],'pending')

    def test_unreadable_instance_identity_is_never_guessed(self):
        self.record()
        with patch.object(worker,'instance_matches',return_value=None),patch.object(worker,'driver') as driver:
            with self.assertRaisesRegex(ValueError,'无法核实'):
                worker.finalize_session(self.run)
        driver.assert_not_called();self.assertEqual(self.native()['cleanup'],'pending')

    def test_legacy_record_without_instance_uses_desktop_state(self):
        self.record(zcode_instance=None)
        with patch.object(worker,'desktop_state',return_value='absent'),patch.object(worker,'driver') as driver:
            result=worker.finalize_session(self.run)
        driver.assert_not_called();self.assertEqual(result['method'],'desktop_not_running')
        self.assertEqual(self.native()['cleanup'],'closed')
        # A running desktop without a pinned identity goes through the bridge.
        self.record(zcode_instance=None)
        with patch.object(worker,'desktop_state',return_value='running'), \
             patch.object(worker,'driver',return_value=[{'type':'closed','session_id':SID}]):
            result=worker.finalize_session(self.run)
        self.assertEqual(result['method'],'bridge_close')

    def test_unreadable_process_table_never_proves_desktop_absence(self):
        # Independent-review probe 2: a ZCode-named row whose executable cannot
        # be read leaves the desktop state unknown; that is not exit evidence.
        self.record(zcode_instance=None)
        rows=[SimpleNamespace(info={'name':'ZCode.exe','exe':None,'cmdline':None})]
        with patch('psutil.process_iter',return_value=rows),patch.object(worker,'driver') as driver:
            with self.assertRaisesRegex(ValueError,'无法核实'):
                worker.finalize_session(self.run)
        driver.assert_not_called()
        self.assertEqual(self.native()['cleanup'],'pending')

    def test_confirmed_cleanup_is_an_idempotent_no_op(self):
        self.record(zcode={'prompt_attempted':True,'cleanup':'closed'})
        with patch.object(worker,'driver') as driver:
            result=worker.finalize_session(self.run)
        driver.assert_not_called()
        self.assertFalse(result['finalized']);self.assertEqual(result['reason'],'cleanup_confirmed')
        self.record(zcode={'prompt_attempted':False,'cleanup':'not_created'})
        self.assertFalse(worker.finalize_session(self.run)['finalized'])

    def test_finalize_never_enables_a_resend(self):
        self.record()
        with patch.object(worker,'instance_matches',return_value=False):
            worker.finalize_session(self.run)
        with self.assertRaisesRegex(ValueError,'不自动重发'):
            worker.recovery_guard(registry.read_task(self.run))

    def test_non_terminal_incomplete_and_drifted_records_are_refused(self):
        self.record(status='running')
        with self.assertRaisesRegex(ValueError,'尚未结束'):
            worker.finalize_session(self.run)
        self.record(engine='dsh')
        with self.assertRaisesRegex(ValueError,'仅支持 ZCode'):
            worker.finalize_session(self.run)
        self.record(zcode_connection={'home':str(self.project/'other'),'port':19765,'access_mode':'project'})
        with self.assertRaisesRegex(ValueError,'连接配置已变化'):
            worker.finalize_session(self.run)
        self.record(zcode={'prompt_attempted':True,'cleanup':'pending'},zcode_session_id='native-foreign-session')
        with self.assertRaisesRegex(ValueError,'无法证明未创建'):
            worker.finalize_session(self.run)

    def test_malformed_instance_identity_is_unknown_not_exit_evidence(self):
        # Independent-review probe 1: a pid alone is not an identity.  A
        # readable process must not turn a malformed record into "exited".
        self.record(zcode_instance={'pid':4242})
        rows=[SimpleNamespace(pid=4242,create_time=lambda:100.0,cmdline=lambda:[str(self.project/'installed'/'ZCode.exe')])]
        with patch('psutil.process_iter',return_value=[]),patch('psutil.Process',return_value=rows[0]), \
             patch.object(worker,'driver') as driver:
            with self.assertRaisesRegex(ValueError,'无法核实'):
                worker.finalize_session(self.run)
        driver.assert_not_called()
        self.assertEqual(self.native()['cleanup'],'pending')

    def test_never_created_requires_provable_absence_of_a_session_id(self):
        # Independent-review probe 3: a missing session id with a "creation
        # started" marker is a contradiction, not a never-created proof.
        self.record(zcode={'cleanup':'pending'},zcode_session_id=None)
        with patch.object(worker,'driver') as driver:
            with self.assertRaisesRegex(ValueError,'无法证明未创建'):
                worker.finalize_session(self.run)
        driver.assert_not_called();self.assertEqual(self.native()['cleanup'],'pending')
        # A prompt marked attempted without any session id contradicts the
        # write order too.
        self.record(zcode={'prompt_attempted':True,'cleanup':None},zcode_session_id=None)
        with self.assertRaisesRegex(ValueError,'无法证明未创建'):
            worker.finalize_session(self.run)
        # Malformed send credentials cannot back the proof either.
        self.record(zcode={'prompt_attempted':'yes','cleanup':None},zcode_session_id=None)
        with self.assertRaisesRegex(ValueError,'无法证明未创建'):
            worker.finalize_session(self.run)
        self.record(zcode={'prompt_attempted':1,'cleanup':None},zcode_session_id=None)
        with self.assertRaisesRegex(ValueError,'无法证明未创建'):
            worker.finalize_session(self.run)
        # A garbage cleanup value is a corrupt record, not absence evidence.
        self.record(zcode={'prompt_attempted':False,'cleanup':'weird'},zcode_session_id=None)
        with self.assertRaisesRegex(ValueError,'无法证明未创建'):
            worker.finalize_session(self.run)

    def test_provable_never_created_records_the_write_order_basis(self):
        # The worker persists zcode_session_id before any createSession can
        # run, so a terminal record without one - with no contradicting
        # marker - is positive never-created evidence.
        self.record(zcode={'prompt_attempted':False,'cleanup':None},zcode_session_id=None)
        result=worker.finalize_session(self.run)
        self.assertTrue(result['finalized']);self.assertFalse(result['closed'])
        self.assertEqual(result['method'],'never_created')
        self.assertEqual(result['evidence']['basis'],'no session id was ever persisted for this task')
        self.assertEqual(self.native()['cleanup'],'not_created')
        # A record with no executor fields at all (worker died before its
        # first write) proves the same absence through the same order.
        self.record(zcode=None,zcode_session_id=None)
        result=worker.finalize_session(self.run)
        self.assertEqual(result['method'],'never_created')
        self.assertEqual(self.native()['cleanup'],'not_created')


class ControllerReconciliationTests(unittest.TestCase):
    """After a targeted finalize, the existing finalize_only recovery releases
    exactly this task's resources - the two-step targeted close-down."""

    def setUp(self):
        FinalizeSessionTests.setUp(self)
        # The controller resolves task ids through the registry home; point it
        # at this fixture's temporary run directory.
        self.enterContext(patch.object(registry,'task_dir',return_value=self.run))

    def record(self,**overrides):
        FinalizeSessionTests.record(self,**overrides)

    def native(self):
        return FinalizeSessionTests.native(self)

    def test_confirmed_cleanup_lets_controller_reconciliation_settle_this_task(self):
        self.record(status='failed',
                    controller_finalization={'schema':1,'state':'pending','at':'2026-10-25T00:00:00Z',
                                             'note':'等待控制器提交成果及释放资源'})
        with patch.object(worker,'instance_matches',return_value=False),patch.object(worker,'driver') as driver:
            result=worker.finalize_session(self.run)
        driver.assert_not_called()
        self.assertEqual(result['method'],'instance_exited')
        with patch.object(panel,'execution_resources') as resources,patch.object(panel,'task_leases') as leases:
            resources.snapshot.return_value={}
            leases.holders.return_value={}
            leases.key.side_effect=lambda kind,name:kind+':'+str(name)
            value=panel.reconcile_task_finalization('lifecycle-fixture',{'confirm':True})
        self.assertFalse(value['executor_replayed'])
        self.assertEqual(value['controller_finalization']['state'],'settled')
        self.assertEqual(registry.read_task(self.run)['controller_finalization']['state'],'settled')
        self.assertEqual(self.native()['cleanup'],'closed')

    def test_unconfirmed_cleanup_still_blocks_controller_reconciliation(self):
        self.record(status='failed',
                    controller_finalization={'schema':1,'state':'pending','at':'2026-10-25T00:00:00Z',
                                             'note':'等待控制器提交成果及释放资源'})
        with self.assertRaisesRegex(ValueError,'原生会话关闭未确认'):
            panel.reconcile_task_finalization('lifecycle-fixture',{'confirm':True})
        self.assertEqual(registry.read_task(self.run)['controller_finalization']['state'],'pending')

    def test_unknown_evidence_keeps_both_finalize_and_reconciliation_refused(self):
        # Symmetric chain: malformed identity and unreadable process tables
        # leave the cleanup unconfirmed, so the controller keeps the task's
        # resources retained instead of releasing on a guess.
        for state_overrides,patch_kwargs in (
                ({'zcode_instance':{'pid':4242}},{'psutil.Process':None}),
                ({'zcode_instance':None},{'psutil.process_iter':[SimpleNamespace(info={'name':'ZCode.exe','exe':None,'cmdline':None})]}),
                ({'zcode_instance':None},{'psutil.process_iter':[SimpleNamespace(info={'name':None,'exe':None,'cmdline':None})]}),
                ({'zcode_session_id':'foreign-existing-session'},{})):
            self.record(status='failed',
                        controller_finalization={'schema':1,'state':'pending','at':'2026-10-25T00:00:00Z',
                                                 'note':'等待控制器提交成果及释放资源'},
                        **state_overrides)
            with patch('psutil.process_iter',return_value=patch_kwargs.get('psutil.process_iter',[])), \
                 patch('psutil.Process',return_value=SimpleNamespace(pid=4242,create_time=lambda:100.0,cmdline=lambda:['x'])), \
                 patch.object(worker,'driver') as driver:
                with self.assertRaisesRegex(ValueError,'无法核实|会话身份无效'):
                    worker.finalize_session(self.run)
            driver.assert_not_called()
            with patch.object(panel,'execution_resources') as resources,patch.object(panel,'task_leases') as leases:
                resources.snapshot.return_value={};leases.holders.return_value={}
                with self.assertRaisesRegex(ValueError,'原生会话关闭未确认'):
                    panel.reconcile_task_finalization('lifecycle-fixture',{'confirm':True})
            self.assertEqual(self.native()['cleanup'],'pending')

    def test_desktop_absence_evidence_releases_only_this_task(self):
        self.record(status='failed',zcode_instance=None,
                    controller_finalization={'schema':1,'state':'pending','at':'2026-10-25T00:00:00Z',
                                             'note':'等待控制器提交成果及释放资源'})
        rows=[SimpleNamespace(info={'name':'ZCode.exe','exe':str(self.project/'installed'/'ZCode.exe'),'cmdline':[str(self.project/'installed'/'ZCode.exe'),'--type=crashpad-handler']})]
        with patch('psutil.process_iter',return_value=rows):
            result=worker.finalize_session(self.run)
        self.assertEqual(result['method'],'desktop_not_running')
        with patch.object(panel,'execution_resources') as resources,patch.object(panel,'task_leases') as leases:
            resources.snapshot.return_value={};leases.holders.return_value={}
            leases.key.side_effect=lambda kind,name:kind+':'+str(name)
            value=panel.reconcile_task_finalization('lifecycle-fixture',{'confirm':True})
        self.assertEqual(value['controller_finalization']['state'],'settled')
        self.assertFalse(value['executor_replayed'])


if __name__=='__main__':
    unittest.main()
