"""Concentrated waiting: observable CLI output, safe attachment and recovery."""
import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import dispatch_task as dispatch


def record(status, phase=''):
    return {'id':'original-task', 'status':status, 'phase':phase, 'model':'fixture', 'engine':'dsh'}


class ConcentratedWaitTests(unittest.TestCase):
    def wait(self, states, **kwargs):
        calls = []
        iterator = iter(states)
        def request(path):
            calls.append(path)
            return {'task':next(iterator)}
        output = io.StringIO()
        with contextlib.redirect_stdout(output), patch.object(dispatch.time,'sleep'):
            code = dispatch.wait_for_task(request, 'original-task', **kwargs)
        return code, [json.loads(line) for line in output.getvalue().splitlines()], calls

    def test_many_intermediate_changes_are_consumed_locally(self):
        states = [record('queued','排队')] + [record('running',f'工具：step-{n}') for n in range(100)]
        code, rows, calls = self.wait(states + [record('completed')])
        self.assertEqual(code,0)
        self.assertEqual(len(rows),1)
        self.assertEqual(rows[0]['status'],'completed')
        self.assertEqual(len(calls),102)
        self.assertEqual(set(calls),{'/api/tasks/original-task'})

    def test_progress_is_explicit_and_tool_noise_is_collapsed(self):
        code, rows, _ = self.wait([record('queued','排队'),record('running','工具：read'),
                                  record('running','工具：write'),record('completed')],progress=True)
        self.assertEqual(code,0)
        self.assertEqual(len(rows),3)
        self.assertEqual(rows[1]['phase'],'执行 Agent 处理中')

    def test_terminal_status_and_exit_code_are_preserved(self):
        for status, expected in (('completed',0),('failed',1),('cancelled',1)):
            with self.subTest(status=status):
                code, rows, calls = self.wait([record(status)])
                self.assertEqual(code,expected)
                self.assertEqual(rows[0]['status'],status)
                self.assertEqual(len(calls),1)

    def test_compact_and_verbose_delivery(self):
        terminal = {**record('completed'),'summary':'s'*1500,
                    'test_result':{'exit_code':0,'output':'x'*1200,'extra':'retained in verbose'}}
        for verbose in (False,True):
            _, rows, _ = self.wait([terminal],verbose=verbose)
            self.assertEqual(len(rows[0]['summary']),1500 if verbose else 1200)
            self.assertEqual(len(rows[0]['test_result']['output']),1200 if verbose else 800)

    def test_connection_failure_reports_resume_without_faking_task_failure(self):
        output, error = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(error):
            code = dispatch.wait_for_task(lambda path: (_ for _ in ()).throw(OSError('offline')),'original-task')
        self.assertEqual(code,2)
        self.assertEqual(output.getvalue(),'')
        self.assertIn('--wait-task original-task',error.getvalue())
        self.assertIn('未被取消或重新派发',error.getvalue())

    def test_interrupt_stops_waiting_only(self):
        calls, error = [], io.StringIO()
        def request(path):
            calls.append(path)
            return {'task':record('running')}
        with patch.object(dispatch.time,'sleep',side_effect=KeyboardInterrupt), contextlib.redirect_stderr(error):
            code = dispatch.wait_for_task(request,'original-task')
        self.assertEqual(code,130)
        self.assertEqual(calls,['/api/tasks/original-task'])
        self.assertIn('--wait-task original-task',error.getvalue())


class AttachAndSubmitTests(unittest.TestCase):
    def test_attach_requires_no_task_file_and_never_posts(self):
        calls, output = [], io.StringIO()
        def request(path, body=None):
            calls.append((path,body))
            self.assertIsNone(body)
            return {'task':record('completed')}
        with patch.object(sys,'argv',['dispatch_task.py','--wait-task','original-task']), \
             patch.object(dispatch,'connect_panel',return_value=request), contextlib.redirect_stdout(output):
            code = dispatch.main()
        rows = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(code,0)
        self.assertEqual(rows[0],{'task_id':'original-task','attached':True})
        self.assertEqual(rows[1]['id'],'original-task')
        self.assertEqual(calls,[('/api/tasks/original-task',None)])

    def test_attach_connection_failure_is_resumable(self):
        output, error = io.StringIO(), io.StringIO()
        with patch.object(sys,'argv',['dispatch_task.py','--wait-task','original-task']), \
             patch.object(dispatch,'connect_panel',side_effect=OSError('offline')), \
             contextlib.redirect_stdout(output), contextlib.redirect_stderr(error):
            self.assertEqual(dispatch.main(),2)
        self.assertEqual(len(output.getvalue().splitlines()),1)
        self.assertIn('--wait-task original-task',error.getvalue())

    def test_invalid_id_and_conflicting_operations_never_connect(self):
        invalid = [['--wait-task','../other'],['--wait-task','a/b'],['--wait-task','a?x'],
                   ['--wait-task','original-task','--task-file','unused'],
                   ['--wait-task','original-task','--preview'],
                   ['--wait-task','original-task','--stage','stage'],
                   ['--wait-task','original-task','--continue-task','old'],
                   ['--wait-task','original-task','--model-id','home'],
                   ['--wait-task','original-task','--timeout','60'],
                   ['--task-file','unused','--progress']]
        for arguments in invalid:
            with self.subTest(arguments=arguments), patch.object(sys,'argv',['dispatch_task.py',*arguments]), \
                 patch.object(dispatch,'connect_panel') as connect, contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as error:
                    dispatch.main()
                self.assertEqual(error.exception.code,2)
                connect.assert_not_called()

    def test_new_dispatch_posts_once_and_defaults_to_unlimited_quiet_wait(self):
        calls, output = [], io.StringIO()
        states = iter([record('queued','排队'),record('running','工具：read'),record('completed')])
        def request(path, body=None):
            calls.append((path,body))
            if body is not None:
                self.assertEqual(path,'/api/tasks')
                self.assertEqual(body['timeout'],0)
                return record('queued')
            return {'task':next(states)}
        with tempfile.TemporaryDirectory() as directory:
            prompt = Path(directory)/'task.txt';prompt.write_text('fixture task',encoding='utf-8')
            with patch.object(sys,'argv',['dispatch_task.py','--task-file',str(prompt),'--wait']), \
                 patch.object(dispatch,'connect_panel',return_value=request), \
                 patch.object(dispatch.time,'sleep'), contextlib.redirect_stdout(output):
                code = dispatch.main()
        self.assertEqual(code,0)
        self.assertEqual(sum(body is not None for _,body in calls),1)
        rows = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(len(rows),2)
        self.assertEqual(rows[0]['task_id'],'original-task')
        self.assertEqual(rows[1]['status'],'completed')


if __name__ == '__main__':
    unittest.main()
