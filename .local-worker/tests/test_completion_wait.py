"""Isolated task files and mocked native host. No services or live inference."""
import io
import contextlib
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import completion_wait as wait
import desktop_notify as desktop
import dispatch_task as dispatch


class SubscriptionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='model-desk-completion-')
        self.addCleanup(temporary.cleanup)
        self.project = str(Path(temporary.name).resolve())
        self.identity = str(uuid.uuid4())
        self.context = {'project': self.project, 'thread_id': self.identity, 'control_turn_id': 'original-turn'}
        self.idle = {'thread_id': self.identity, 'project': self.project,
                     'turns': [{'id': 'original-turn', 'status': 'completed'}]}
        for target, values in ((os.environ, {'CODEX_THREAD_ID': self.identity,
                                            'CODEX_APP_TOOLS_PIPE_PATH': 'fixture-no-real-pipe'}),):
            p = patch.dict(target, values)
            p.start(); self.addCleanup(p.stop)
        p = patch.object(wait.desktop, 'read_current', return_value=self.idle)
        self.reader = p.start(); self.addCleanup(p.stop)
        p = patch.object(wait.desktop, 'send_current', return_value={'host_accepted': True})
        self.sender = p.start(); self.addCleanup(p.stop)

    def task(self, task_id='task-a', status='running', **fields):
        run = wait.safe_child(self.project, 'runs', task_id)
        run.mkdir(parents=True, exist_ok=True)
        current = json.loads((run/'task.json').read_text(encoding='utf-8')) if (run/'task.json').exists() else {}
        data = {**current, 'id': task_id, 'project': self.project, 'status': status,
                'engine': 'dsh', 'timeout': 0, **fields}
        if status in wait.TERMINAL and 'ended_at' not in fields:
            data['ended_at'] = wait.now()
        wait.registry.write_json(run/'task.json', data)
        return data

    def arm(self, tasks=('task-a',)):
        with patch.object(wait, '_start', side_effect=lambda data: data):
            return wait.arm(self.project, list(tasks), self.context)

    def run_wait(self, data, sleeper=lambda _: None):
        return wait.run(self.project, data['id'], sleeper=sleeper, poll_seconds=0)

    def test_real_record_transition_not_text_or_silence_triggers_one_event(self):
        self.task(answer='I have finished', phase='执行完成')
        data = self.arm()
        ticks = []
        def tick(_):
            ticks.append(1)
            self.sender.assert_not_called()
            self.reader.assert_not_called()
            if len(ticks)==75:
                self.task(status='completed')
        result = self.run_wait(data, tick)
        self.assertEqual(len(ticks), 75)
        self.assertEqual(result['listener_status'], 'delivered')
        self.assertEqual(result['phase'], 'awaiting_review')
        self.assertEqual(result['send_attempts'], 1)
        self.sender.assert_called_once()
        self.assertEqual(wait.task_record(self.project,'task-a')['timeout'],0)
        self.assertFalse((wait.safe_child(self.project,'runs','task-a')/'review.json').exists())

    def test_batch_waits_for_all_then_sends_once(self):
        self.task(status='completed'); self.task('task-b', 'running')
        data = self.arm(('task-a','task-b'))
        def tick(_):
            self.sender.assert_not_called()
            self.task('task-b','completed')
        result = self.run_wait(data,tick)
        self.assertEqual(result['phase'],'awaiting_review')
        self.sender.assert_called_once()
        prompt = self.sender.call_args.args[1]
        self.assertIn('task-a',prompt); self.assertIn('task-b',prompt)

    def test_failure_cancel_and_recovery_are_attention_not_acceptance(self):
        for number, status in enumerate(('failed','cancelled','recovery_pending')):
            with self.subTest(status=status):
                task_id = 'task-' + str(number)
                self.task(task_id,status)
                result=self.run_wait(self.arm((task_id,)))
                self.assertEqual(result['phase'],'needs_attention')
                self.assertEqual(wait.task_record(self.project,task_id)['status'],status)
        self.assertEqual(self.sender.call_count,3)

    def test_incomplete_terminal_record_is_attention(self):
        self.task(status='completed',ended_at='')
        result=self.run_wait(self.arm())
        self.assertEqual(result['phase'],'needs_attention')
        self.sender.assert_called_once()

    def test_wrong_project_record_refused_before_start(self):
        self.task(project=self.project+'-other')
        with patch.object(wait,'_start') as starter, self.assertRaises(ValueError):
            wait.arm(self.project,['task-a'],self.context)
        starter.assert_not_called(); self.sender.assert_not_called()

    def test_missing_project_is_never_inferred_from_cwd(self):
        self.assertFalse(wait.same_path('',Path.cwd()))
        self.task(project='')
        with self.assertRaises(ValueError):self.arm()
        with patch.object(wait.Path,'cwd',return_value=Path(self.project)),patch.object(wait.desktop,'read_current',return_value={'thread_id':self.identity,'turns':[{'id':'turn'}]}):
            with self.assertRaises(ValueError):wait.preflight(self.project)

    def test_duplicate_registration_and_overlapping_batches_are_refused(self):
        self.task(); self.task('task-b')
        data=self.arm()
        with patch.object(wait,'_start') as starter:
            again=wait.arm(self.project,['task-a'],self.context)
            self.assertTrue(again['existing'])
            with self.assertRaises(ValueError):
                wait.arm(self.project,['task-a','task-b'],self.context)
        starter.assert_not_called()
        self.assertEqual(again['id'],data['id'])

    def test_concurrent_registration_starts_only_one_watcher(self):
        self.task()
        with patch.object(wait,'_start',side_effect=lambda data:data) as starter:
            with ThreadPoolExecutor(max_workers=6) as pool:
                records=list(pool.map(lambda _:wait.arm(self.project,['task-a'],self.context),range(12)))
        starter.assert_called_once()
        self.assertEqual(len({row['id'] for row in records}),1)

    def test_extended_length_resolve_prefix_keeps_same_project(self):
        """A Windows ``\\\\?\\`` resolve prefix is a representation, not a move."""
        if os.name != 'nt':
            self.skipTest('extended-length prefix is Windows-specific')
        self.task()
        original = Path.resolve

        def resolve_with_prefix(path, *args, **kwargs):
            result = original(path, *args, **kwargs)
            if 'notifications' in str(path):
                return Path('\\\\?\\' + str(result))
            return result

        with patch.object(wait.Path, 'resolve', resolve_with_prefix):
            root = wait.safe_child(self.project, 'notifications', 'wait-fixture')
        self.assertTrue(root.is_relative_to(Path(self.project)))
        self.assertEqual(wait._strip_windows_prefix('\\\\?\\UNC\\server\\share\\run'),
                         '\\\\server\\share\\run')
        self.assertEqual(wait._strip_windows_prefix('\\\\?\\C:\\project\\run'), 'C:\\project\\run')

    def test_different_chat_cannot_load_or_acknowledge(self):
        self.task(); data=self.arm()
        with patch.dict(os.environ,{'CODEX_THREAD_ID':str(uuid.uuid4())}), self.assertRaises(ValueError):
            wait.received(self.project,data['id'])

    def test_changed_batch_identity_is_refused(self):
        self.task(); data=self.arm()
        data['task_ids']=['different-task']; wait.save(data)
        with self.assertRaises(ValueError):
            wait.load(self.project,data['id'])

    def test_links_never_read_or_write_outside_project(self):
        with patch.object(wait.storage,'_is_link',return_value=True), self.assertRaises(ValueError):
            wait.safe_child(self.project,'runs','task-a')

    def test_unknown_send_is_never_retried(self):
        self.task(status='completed'); data=self.arm()
        self.sender.side_effect=desktop.NotificationError('fixture connection lost')
        result=self.run_wait(data)
        self.assertEqual(result['listener_status'],'delivery_unknown')
        self.run_wait(data)
        self.sender.assert_called_once()
        self.assertTrue((wait.paths(self.project,data['id'])/'send.claim').is_file())

    def test_host_rejection_is_never_retried(self):
        self.task(status='completed'); data=self.arm()
        self.sender.return_value={'host_accepted':False}
        self.assertEqual(self.run_wait(data)['listener_status'],'host_rejected')
        self.run_wait(data); self.sender.assert_called_once()

    def test_crash_after_claim_does_not_repeat_delivery(self):
        self.task(status='completed'); data=self.arm()
        (wait.paths(self.project,data['id'])/'send.claim').write_text(data['id'])
        self.run_wait(data)
        self.sender.assert_not_called(); self.reader.assert_not_called()

    def test_fast_receipt_is_not_overwritten_by_sender(self):
        self.task(status='completed'); data=self.arm()
        def delivered(*_):
            wait.received(self.project,data['id'])
            return {'host_accepted':True}
        self.sender.side_effect=delivered
        result=self.run_wait(data)
        self.assertEqual(result['listener_status'],'received')
        self.assertEqual(result['phase'],'reviewing')
        self.assertIn('receipt_at',result)

    def test_real_receipt_can_resolve_unknown_delivery_without_resending(self):
        self.task(status='completed');data=self.arm()
        self.sender.side_effect=desktop.NotificationError('fixture lost acknowledgement')
        self.assertEqual(self.run_wait(data)['listener_status'],'delivery_unknown')
        result=wait.received(self.project,data['id'])
        self.assertEqual(result['phase'],'reviewing')
        self.assertEqual(result['listener_status'],'received');self.sender.assert_called_once()

    def test_stop_does_not_cancel_worker_or_send(self):
        self.task(); data=self.arm()
        (wait.paths(self.project,data['id'])/'stop.flag').touch()
        self.assertEqual(self.run_wait(data)['listener_status'],'stopped')
        self.assertEqual(wait.task_record(self.project,'task-a')['status'],'running')
        self.assertFalse((wait.safe_child(self.project,'runs','task-a')/'cancelled.flag').exists())
        self.sender.assert_not_called()

    def test_stop_racing_with_send_claim_prevents_delivery(self):
        self.task(status='completed');data=self.arm()
        root=wait.paths(self.project,data['id'])
        original_lock=wait.registry.file_lock
        @contextlib.contextmanager
        def racing_lock(path):
            if Path(path).name=='state.lock':(root/'stop.flag').touch()
            with original_lock(path):yield
        with patch.object(wait.registry,'file_lock',side_effect=racing_lock):
            self.assertEqual(self.run_wait(data)['listener_status'],'stopped')
        self.assertFalse((root/'send.claim').exists());self.sender.assert_not_called()

    def test_waits_for_chat_to_be_idle_before_sending(self):
        self.task(status='completed'); data=self.arm()
        self.reader.side_effect=[{**self.idle,'turns':[{'id':'human-turn','status':'inProgress'}]},self.idle]
        def tick(_): self.sender.assert_not_called()
        self.assertEqual(self.run_wait(data,tick)['listener_status'],'delivered')
        self.assertEqual(self.reader.call_count,2); self.sender.assert_called_once()

    def test_native_project_change_refuses_delivery(self):
        self.task(status='completed'); data=self.arm()
        self.reader.return_value={**self.idle,'project':self.project+'-other'}
        self.assertEqual(self.run_wait(data)['listener_status'],'route_refused')
        self.sender.assert_not_called()

    def test_new_task_activity_during_native_read_defers_delivery(self):
        self.task(status='completed'); data=self.arm()
        reads=[]
        def read():
            reads.append(1)
            if len(reads)==1:self.task(status='running')
            return self.idle
        self.reader.side_effect=read
        def tick(_):self.task(status='completed')
        self.assertEqual(self.run_wait(data,tick)['listener_status'],'delivered')
        self.assertEqual(len(reads),2); self.sender.assert_called_once()

    def test_deleted_task_creates_one_attention_event_without_recreating_run(self):
        self.task(); data=self.arm()
        run=wait.safe_child(self.project,'runs','task-a')
        (run/'task.json').unlink()
        result=self.run_wait(data)
        self.assertEqual(result['phase'],'needs_attention')
        self.assertFalse((run/'task.json').exists()); self.sender.assert_called_once()

    def test_version_change_does_not_send(self):
        self.task(); data=self.arm()
        with patch.object(wait,'digest',return_value={'changed':'source'}):
            result=self.run_wait(data)
        self.assertEqual(result['listener_status'],'version_changed')
        self.sender.assert_not_called()

    def test_explicit_resume_checks_liveness_and_never_dispatches(self):
        self.task(); data=self.arm()
        with patch.object(wait,'preflight',return_value=self.context),patch.object(wait,'process_alive',return_value=True),patch.object(wait,'_start') as start:
            with self.assertRaises(ValueError):wait.resume(self.project,data['id'])
            start.assert_not_called()
        with patch.object(wait,'preflight',return_value=self.context),patch.object(wait,'process_alive',return_value=False),patch.object(wait,'_start',side_effect=lambda data:data) as start:
            self.assertEqual(wait.resume(self.project,data['id'])['listener_status'],'starting')
            start.assert_called_once()


class NativeBridgeTests(unittest.TestCase):
    def setUp(self):
        self.identity=str(uuid.uuid4())
        p=patch.dict(os.environ,{'CODEX_THREAD_ID':self.identity,'CODEX_APP_TOOLS_PIPE_PATH':'fixture-no-real-pipe'})
        p.start();self.addCleanup(p.stop)

    def test_native_framing_partial_io_exact_target_and_no_model_override(self):
        payload=json.dumps({'result':{'success':True}}).encode()
        class Pipe:
            def __init__(self):self.incoming=io.BytesIO(struct.pack('<I',len(payload))+payload);self.sent=b''
            def __enter__(self):return self
            def __exit__(self,*_):pass
            def read(self,count):return self.incoming.read(min(count,2))
            def write(self,data):self.sent+=data[:3];return min(len(data),3)
        pipe=Pipe()
        with patch('builtins.open',return_value=pipe):
            result=desktop._exchange('send_message_to_thread',{'threadId':self.identity,'prompt':'fixed'},self.identity,'fixture-call')
        self.assertTrue(result['host_accepted'])
        request=json.loads(pipe.sent[4:])
        self.assertEqual(request['params']['threadId'],self.identity)
        self.assertEqual(set(request['params']['arguments']),{'threadId','prompt'})

    def test_other_chat_refused_before_connection(self):
        with patch('builtins.open') as connection,self.assertRaises(ValueError):
            desktop._exchange('read_thread',{'threadId':str(uuid.uuid4())},self.identity,'fixture')
        connection.assert_not_called()

    def test_timeout_is_bounded_without_retry(self):
        with patch.object(desktop.subprocess,'run',side_effect=subprocess.TimeoutExpired('fixture',25)) as request:
            with self.assertRaises(ValueError):desktop.read_current()
        request.assert_called_once()
        self.assertEqual(request.call_args.kwargs['timeout'],25)

    def test_hidden_flags_and_no_secret_environment(self):
        with patch.dict(os.environ,{'FAKE_API_KEY':'do-not-inherit','UNRELATED':'not-needed'}):
            environment=desktop.child_environment()
        self.assertNotIn('FAKE_API_KEY',environment);self.assertNotIn('UNRELATED',environment)
        self.assertEqual(environment['CODEX_THREAD_ID'],self.identity)
        if os.name=='nt':
            flags=desktop.hidden_options()['creationflags']
            self.assertTrue(flags&subprocess.CREATE_NO_WINDOW)
            self.assertFalse(flags&subprocess.DETACHED_PROCESS)


class NotifyDispatchTests(unittest.TestCase):
    def test_continuation_and_stage_cannot_dispatch_to_another_project(self):
        for flag in ('--continue-task','--stage'):
            with self.subTest(flag=flag),tempfile.TemporaryDirectory() as directory:
                prompt=Path(directory)/'prompt.txt';prompt.write_text('fixture',encoding='utf-8')
                requests=[]
                def request(path,body=None):
                    requests.append((path,body))
                    self.assertIsNone(body)
                    other={'id':'old','project':directory+'-other','model_id':'fixture','engine':'dsh','kind':'edit','files':[]}
                    return {'task':other} if '/tasks/' in path else other
                with patch.object(sys,'argv',['dispatch_task.py','--task-file',str(prompt),'--project',directory,'--notify-on-completion',flag,'old']), \
                     patch.object(wait,'preflight',return_value={'project':directory}), \
                     patch.object(dispatch,'connect_panel',return_value=request):
                    with self.assertRaises(ValueError):dispatch.main()
                self.assertEqual(len(requests),1)

    def test_preflight_failure_cannot_submit_a_worker(self):
        with patch.object(sys,'argv',['dispatch_task.py','--task-file','unused','--project',str(Path.cwd()),'--notify-on-completion']), \
             patch.object(wait,'preflight',side_effect=ValueError('fixture host unavailable')),patch.object(dispatch,'connect_panel') as connect:
            with self.assertRaises(ValueError):dispatch.main()
        connect.assert_not_called()

    def test_dispatch_once_even_if_listener_start_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            prompt=Path(directory)/'prompt.txt';prompt.write_text('fixture',encoding='utf-8')
            task={'id':'task-a','status':'queued','engine':'dsh','model':'fixture'}
            output=io.StringIO()
            with patch.object(sys,'argv',['dispatch_task.py','--task-file',str(prompt),'--project',directory,'--notify-on-completion']), \
                 patch.object(wait,'preflight',return_value={'project':directory}),patch.object(wait,'arm',side_effect=ValueError('fixture start failure')), \
                 patch.object(dispatch,'connect_panel') as connect,patch('sys.stdout',output):
                connect.return_value.return_value=task
                self.assertEqual(dispatch.main(),2)
            connect.return_value.assert_called_once()
            self.assertEqual(connect.return_value.call_args.args[1]['timeout'],0)
            rows=[json.loads(line) for line in output.getvalue().splitlines()]
            self.assertFalse(rows[-1]['waiting']);self.assertFalse(rows[-1]['redispatched'])

    def test_success_returns_waiting_status_without_synchronous_wait(self):
        with tempfile.TemporaryDirectory() as directory:
            prompt=Path(directory)/'prompt.txt';prompt.write_text('fixture',encoding='utf-8')
            task={'id':'task-a','status':'queued','engine':'dsh','model':'fixture'}
            output=io.StringIO()
            with patch.object(sys,'argv',['dispatch_task.py','--task-file',str(prompt),'--project',directory,'--notify-on-completion']), \
                 patch.object(wait,'preflight',return_value={'project':directory}), \
                 patch.object(wait,'arm',return_value={'id':'fixture-wait','listener_status':'listening','phase':'waiting_execution'}), \
                 patch.object(dispatch,'connect_panel') as connect,patch.object(dispatch,'wait_for_task') as synchronous,patch('sys.stdout',output):
                connect.return_value.return_value=task
                self.assertEqual(dispatch.main(),0)
            synchronous.assert_not_called();connect.return_value.assert_called_once()
            self.assertTrue(json.loads(output.getvalue().splitlines()[-1])['waiting'])


if __name__=='__main__':unittest.main()
