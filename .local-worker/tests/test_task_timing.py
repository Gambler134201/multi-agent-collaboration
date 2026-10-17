"""Offline deadline regressions: no provider calls or live records."""
import io
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import task_timing
import panel_server as panel
import model_registry as registry
from test_panel_observability import Base


class DeadlineTests(unittest.TestCase):
    def test_unlimited_has_no_deadline_even_after_days(self):
        for value in (0, None, ''):
            with mock.patch.object(task_timing.time, 'monotonic', return_value=10):
                end = task_timing.deadline(value, grace=30)
            with mock.patch.object(task_timing.time, 'monotonic', return_value=999999):
                self.assertIsNone(end)
                self.assertFalse(task_timing.expired(end))
                self.assertIsNone(task_timing.remaining(end))

    def test_explicit_optional_limit_and_invalid_values(self):
        with mock.patch.object(task_timing.time, 'monotonic', return_value=10):
            end = task_timing.deadline(30)
            self.assertEqual(task_timing.remaining(end), 30)
        with mock.patch.object(task_timing.time, 'monotonic', return_value=41):
            self.assertTrue(task_timing.expired(end))
        for value in (-1, 1, 29, True, '600', 1.2, 2147484):
            with self.assertRaises(ValueError):
                task_timing.normalize(value)


class ChatTimingTests(Base):
    def stream_task(self, seconds=0, cancel=False):
        task_id = self.task('timing', status='queued', timeout=seconds)
        run = registry.task_dir(task_id)
        (run / 'prompt.txt').write_text('offline fixture', encoding='utf-8')
        if cancel:
            (run / 'cancelled.flag').touch()
        payload = b'data: ' + json.dumps({'choices':[{'delta':{'content':'done'}}]}).encode() + b'\n'
        response = io.BytesIO(payload + b'data: [DONE]\n')
        response.headers = {'Content-Type':'text/event-stream'}
        with mock.patch.object(panel, 'remote_request', return_value=response) as request, \
             mock.patch.object(task_timing, 'deadline', return_value=30 if seconds else None), \
             mock.patch.object(task_timing.time, 'monotonic', return_value=999999):
            panel.run_chat(run, {'timeout':seconds}, registry.get_model('home'))
        self.assertEqual(request.call_args.kwargs['timeout'], 120)
        return registry.read_task(run)

    def test_slow_task_finishes_without_total_cutoff(self):
        task = self.stream_task()
        self.assertEqual(task['status'], 'completed')
        self.assertEqual(task['answer'], 'done')

    def test_explicit_positive_limit_still_optional(self):
        task = self.stream_task(30)
        self.assertEqual(task['status'], 'failed')
        self.assertIn('超过设定时间', task['error'])

    def test_manual_stop_works_without_total_cutoff(self):
        self.assertEqual(self.stream_task(cancel=True)['status'], 'cancelled')

    def test_new_dispatch_default_and_null_store_unlimited(self):
        for extra in ({}, {'timeout':None}, {'timeout':30}):
            body = {'prompt':'offline fixture', 'kind':'chat', 'engine':'direct',
                    'route_mode':'manual', 'model_id':'home', **extra}
            with mock.patch.object(panel.EXECUTOR, 'submit'), \
                 mock.patch.object(panel.deepseek_billing, 'enrich_models', side_effect=lambda rows: rows):
                task = panel.submit_task(body)
            self.assertEqual(task['timeout'], extra.get('timeout') or 0)


if __name__ == '__main__':
    unittest.main()
