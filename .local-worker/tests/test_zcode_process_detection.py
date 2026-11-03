"""Only process-table fixtures; never launches or terminates an application."""
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import psutil
import zcode_worker

class ProcessDetectionTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.home=Path(self.temp.name);self.config={'home':str(self.home)}
    def row(self,role=None,cmdline=True,home=None):
        args=[str((home or self.home)/'ZCode.exe')]
        if role:args.append('--type='+role)
        return SimpleNamespace(info={'name':'ZCode.exe','exe':str((home or self.home)/'ZCode.exe'),
                                     'cmdline':args if cmdline else None})
    def detect(self,rows):
        with patch('psutil.process_iter',return_value=rows):return zcode_worker.desktop_running(self.config)
    def test_crash_reporter_alone_does_not_block_bridge_launch(self):
        self.assertFalse(self.detect([self.row('crashpad-handler')]))
    def test_actual_main_still_blocks_an_automatic_restart(self):
        self.assertTrue(self.detect([self.row('crashpad-handler'),self.row()]))
    def test_remaining_known_helpers_do_not_claim_a_live_desktop(self):
        self.assertFalse(self.detect([self.row('renderer'),self.row('gpu-process'),self.row('utility')]))
    def test_unknown_or_unreadable_role_stays_conservative(self):
        self.assertTrue(self.detect([self.row('future-helper')]))
        self.assertTrue(self.detect([self.row(cmdline=False)]))
    def test_another_installation_does_not_claim_the_configured_desktop(self):
        self.assertFalse(self.detect([self.row(home=self.home/'different')]))

class InstanceIdentityTests(unittest.TestCase):
    """P3: the pinned desktop instance identity, bound before any prompt."""
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.home=Path(self.temp.name);self.config={'home':str(self.home)}
        self.args=[str(self.home/'ZCode.exe'),'--remote-debugging-port=19765','--remote-debugging-address=127.0.0.1']
        self.identity={'pid':4242,'created_at':100.0,'command_sha256':zcode_worker.process_identity.command_digest(self.args)}
    def main_row(self,pid=4242,created=100.0):
        process=SimpleNamespace(pid=pid,create_time=lambda:created,cmdline=lambda:list(self.args))
        process.info={'name':'ZCode.exe','exe':str(self.home/'ZCode.exe'),'cmdline':list(self.args)}
        return process
    def test_desktop_instance_captures_the_main_process_identity(self):
        rows=[SimpleNamespace(info={'name':'ZCode.exe','exe':str(self.home/'ZCode.exe'),'cmdline':[str(self.home/'ZCode.exe'),'--type=crashpad-handler']}),
              self.main_row()]
        with patch('psutil.process_iter',return_value=rows), \
             patch.object(zcode_worker.process_identity,'capture',return_value=self.identity) as capture:
            self.assertEqual(zcode_worker.desktop_instance(self.config),self.identity)
        self.assertEqual(capture.call_args.args,(4242,))
    def test_desktop_instance_is_none_when_only_helpers_remain(self):
        rows=[SimpleNamespace(info={'name':'ZCode.exe','exe':str(self.home/'ZCode.exe'),'cmdline':[str(self.home/'ZCode.exe'),'--type=crashpad-handler']})]
        with patch('psutil.process_iter',return_value=rows),patch.object(zcode_worker.process_identity,'capture') as capture:
            self.assertIsNone(zcode_worker.desktop_instance(self.config))
        capture.assert_not_called()
    def test_instance_matches_true_for_the_same_live_instance(self):
        with patch('psutil.Process',return_value=self.main_row()):
            self.assertTrue(zcode_worker.instance_matches(self.identity))
    def test_instance_matches_false_when_the_pid_was_reused(self):
        # Same pid, different creation time or command line: the original
        # desktop (and its in-memory session) is gone.
        with patch('psutil.Process',return_value=self.main_row(created=200.0)):
            self.assertFalse(zcode_worker.instance_matches(self.identity))
        other=SimpleNamespace(pid=4242,create_time=lambda:100.0,cmdline=lambda:['C:\\elsewhere\\ZCode.exe'])
        with patch('psutil.Process',return_value=other):
            self.assertFalse(zcode_worker.instance_matches(self.identity))
    def test_instance_matches_false_on_exit_and_none_when_unreadable(self):
        with patch('psutil.Process',side_effect=psutil.NoSuchProcess(4242)):
            self.assertFalse(zcode_worker.instance_matches(self.identity))
        with patch('psutil.Process',side_effect=psutil.Error()):
            self.assertIsNone(zcode_worker.instance_matches(self.identity))
        with patch('psutil.Process') as process:
            self.assertIsNone(zcode_worker.instance_matches({'created_at':100.0}))
        process.assert_not_called()

    def test_instance_matches_unknown_for_incomplete_identities(self):
        # Independent-review probe 1: a pid alone is not an identity.  None of
        # the malformed shapes below may collapse to False ("exited"), no
        # matter what the process table shows; the process is never consulted.
        readable=SimpleNamespace(pid=4242,create_time=lambda:100.0,cmdline=lambda:['ZCode.exe'])
        for malformed in ({'pid':4242},{'pid':4242,'created_at':100.0},
                          {'pid':4242,'created_at':100.0,'command_sha256':'short'},
                          {'pid':-4242,'created_at':100.0,'command_sha256':'a'*64},
                          {'pid':4242,'created_at':0,'command_sha256':'a'*64},
                          {'pid':4242,'created_at':'100','command_sha256':'a'*64}):
            with patch('psutil.Process',return_value=readable) as process:
                self.assertIsNone(zcode_worker.instance_matches(malformed),malformed)
            process.assert_not_called()
        for broken in ({'pid':4242,'created_at':float('nan'),'command_sha256':'a'*64},
                       {'pid':4242,'created_at':float('inf'),'command_sha256':'a'*64}):
            self.assertIsNone(zcode_worker.instance_matches(broken),broken)
        # A nonexistent pid with a malformed identity is still unknown: exit
        # evidence needs valid credentials first.
        with patch('psutil.Process',side_effect=psutil.NoSuchProcess(4242)) as process:
            self.assertIsNone(zcode_worker.instance_matches({'pid':4242}))
        process.assert_not_called()
        self.assertTrue(zcode_worker.process_identity.well_formed(self.identity))

class DesktopStateTests(unittest.TestCase):
    """Three-valued desktop presence: positive absence needs a readable table."""
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.home=Path(self.temp.name);self.config={'home':str(self.home)}
    def row(self,role=None,exe=True,cmdline=True,home=None,name='ZCode.exe'):
        info={'name':name,'exe':str((home or self.home)/'ZCode.exe') if exe else None,
              'cmdline':[str((home or self.home)/'ZCode.exe')]+(['--type='+role] if role else []) if cmdline else None}
        return SimpleNamespace(info=info)
    def state(self,rows):
        with patch('psutil.process_iter',return_value=rows):return zcode_worker.desktop_state(self.config)
    def test_readable_table_without_main_process_is_positive_absence(self):
        self.assertEqual(self.state([self.row('crashpad-handler'),self.row('renderer')]),'absent')
        self.assertEqual(self.state([]),'absent')
        self.assertEqual(self.state([self.row(name='chrome.exe')]),'absent')
    def test_live_main_process_is_running(self):
        self.assertEqual(self.state([self.row('crashpad-handler'),self.row()]),'running')
        self.assertEqual(self.state([self.row(cmdline=False)]),'running')
    def test_unreadable_executable_or_rows_leave_state_unknown(self):
        # Independent-review probe 2: an unreadable path is not exit evidence.
        self.assertEqual(self.state([self.row(exe=False)]),'unknown')
        # A readable main row still decides "running" even among unreadables.
        self.assertEqual(self.state([self.row(),self.row(exe=False)]),'running')
        # An unreadable-only table stays unknown; an unreadable command line
        # on a readable executable stays conservatively "running" (the same
        # semantics desktop_running always had: unknown arguments are treated
        # as a potentially live main program, never as absence).
        self.assertEqual(self.state([SimpleNamespace(info={'name':'ZCode.exe','exe':str(self.home/'ZCode.exe')})]),'running')
        self.assertEqual(self.state([SimpleNamespace(info={'name':'ZCode.exe','exe':str(self.home/'ZCode.exe'),'cmdline':None})]),'running')
    def test_row_inspection_error_is_unknown_not_absent(self):
        class Broken:
            @property
            def info(self):raise psutil.Error()
        with patch('psutil.process_iter',return_value=[Broken()]):
            self.assertEqual(zcode_worker.desktop_state(self.config),'unknown')
    def test_desktop_running_stays_the_boolean_view_of_the_state(self):
        for rows,expected in (([],False),([self.row()],True),([self.row(exe=False)],False),
                              ([self.row('crashpad-handler')],False)):
            with patch('psutil.process_iter',return_value=rows):
                self.assertEqual(zcode_worker.desktop_running(self.config),expected)

class StartupReadinessTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.object(zcode_worker,'configuration',return_value={'home':'fixture','port':19765,'auto_connect':False}))
        self.enterContext(patch.object(zcode_worker,'installation',return_value={}))
        self.enterContext(patch.object(zcode_worker.time,'sleep'))
    def test_existing_bridge_waits_for_native_services_without_auto_launch_permission(self):
        with patch.object(zcode_worker,'bridge_connected',return_value=True), \
             patch.object(zcode_worker,'native_services_ready',side_effect=[False,True]) as ready, \
             patch.object(zcode_worker,'launch') as launch:
            zcode_worker.ensure_connection(allow_auto=False)
        self.assertEqual(ready.call_count,2);launch.assert_not_called()
    def test_explicit_connect_waits_for_services_before_catalog(self):
        with patch.object(zcode_worker,'bridge_connected',return_value=True), \
             patch.object(zcode_worker,'native_services_ready',side_effect=[False,True]), \
             patch.object(zcode_worker,'launch') as launch, \
             patch.object(zcode_worker,'refresh_catalog',return_value={'models':[]}) as catalog:
            self.assertTrue(zcode_worker.connect()['ok'])
        launch.assert_not_called();catalog.assert_called_once()
    def test_real_existing_desktop_without_bridge_is_never_restarted(self):
        with patch.object(zcode_worker,'bridge_connected',return_value=False), \
             patch.object(zcode_worker,'desktop_running',return_value=True), \
             patch.object(zcode_worker,'launch') as launch:
            with self.assertRaises(ValueError):zcode_worker.connect()
        launch.assert_not_called()
    def test_cancel_flag_created_mid_wait_aborts_loop_before_prompt(self):
        # The bridge is already connected, so the auto-launch branch is never
        # entered; the cancel flag appears only after the readiness wait loop
        # started. Every loop iteration must re-check it and abort with the
        # cancellation message before any prompt could be sent.
        def externally_cancelled(config):
            cancel.write_text('cancelled',encoding='utf-8')
            return False
        with tempfile.TemporaryDirectory() as folder:
            cancel=Path(folder)/'cancelled.flag'
            with patch.object(zcode_worker,'bridge_connected',return_value=True), \
                 patch.object(zcode_worker,'native_services_ready',side_effect=externally_cancelled), \
                 patch.object(zcode_worker,'launch') as launch, \
                 patch.object(zcode_worker,'driver') as driver:
                with self.assertRaisesRegex(ValueError,'已取消'):
                    zcode_worker.ensure_connection(cancel_file=cancel,allow_auto=True)
        launch.assert_not_called();driver.assert_not_called()

if __name__=='__main__':unittest.main()
