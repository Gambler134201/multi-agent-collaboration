"""Folder picker lifecycle and helper protocol, without opening an unattended dialog."""
import json
import tempfile
import threading
import time
import unittest
import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import folder_picker as picker


class PickerTests(unittest.TestCase):
    def result(self, jobs, job):
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            result = jobs.get(job['id'])
            if result['status'] != 'pending':
                return result
            time.sleep(.01)
        self.fail('fixture job did not complete')

    def test_select_validate_cancel_failure_and_no_shared_result(self):
        with tempfile.TemporaryDirectory(prefix='目录 空格 ') as directory:
            jobs = picker.PickerJobs(lambda initial: {'status': 'selected', 'project': directory})
            first = jobs.start()
            self.assertEqual(self.result(jobs, first)['project'], str(Path(directory).resolve()))
            jobs.picker = lambda initial: {'status': 'cancelled'}
            second = jobs.start(directory)
            self.assertNotEqual(first['id'], second['id'])
            self.assertNotIn('project', self.result(jobs, second))
            self.assertEqual(jobs.get(first['id'])['status'], 'selected')
            jobs.picker = lambda initial: {'status': 'selected', 'project': str(Path(directory)/'missing')}
            self.assertEqual(self.result(jobs, jobs.start())['status'], 'failed')
            jobs.picker = mock.Mock(side_effect=RuntimeError('RAW SECRET DIAGNOSTIC'))
            result = self.result(jobs, jobs.start())
            self.assertEqual(result['status'], 'failed')
            self.assertNotIn('SECRET', result['error'])

    def test_one_active_window_invalid_initial_and_expired_ids(self):
        event = threading.Event()
        jobs = picker.PickerJobs(lambda initial: (event.wait(2) or True) and {'status': 'cancelled'})
        job = jobs.start()
        try:
            with self.assertRaises(ValueError): jobs.start()
            for key in ('../bad', 'x'*32, '0'*32):
                with self.assertRaises(ValueError): jobs.get(key)
        finally:
            event.set()
        self.result(jobs, job)
        jobs.jobs[job['id']]['_created'] -= 601
        with self.assertRaises(ValueError): jobs.get(job['id'])
        with self.assertRaises(ValueError): jobs.start('relative-path')

    def test_native_subprocess_no_shell_and_timeout_cleanup(self):
        process = mock.Mock(returncode=0)
        process.communicate.return_value=(json.dumps({'status':'cancelled'}).encode(),b'')
        with mock.patch.object(picker,'find_pwsh',return_value='pwsh.exe'), mock.patch.object(picker.subprocess,'Popen',return_value=process) as popen:
            self.assertEqual(picker.native_pick(),{'status':'cancelled'})
            args, kwargs=popen.call_args
            self.assertIn('-STA',args[0]);self.assertNotIn('shell',kwargs)
            self.assertEqual(kwargs['creationflags'],picker.subprocess.CREATE_NO_WINDOW)
            process.communicate.side_effect=[picker.subprocess.TimeoutExpired('picker',300),(b'',b'')]
            with self.assertRaisesRegex(ValueError,'超时'): picker.native_pick()
            process.kill.assert_called_once()

    def test_native_selected_directory_and_malformed_output(self):
        with tempfile.TemporaryDirectory() as directory:
            process=mock.Mock(returncode=0)
            with mock.patch.object(picker,'find_pwsh',return_value='pwsh.exe'), mock.patch.object(picker.subprocess,'Popen',return_value=process):
                process.communicate.return_value=(json.dumps({'status':'selected','project':directory}).encode(),b'')
                self.assertEqual(picker.native_pick(directory)['project'],str(Path(directory).resolve()))
                for raw in (b'garbage',b'{}',b'[]'):
                    process.communicate.return_value=(raw,b'')
                    with self.assertRaises(ValueError): picker.native_pick()

