"""Actual worker boundaries with temporary records and a fake native driver."""
import hashlib
import json
from pathlib import Path
import unittest

import test_zcode_native_tools as fixtures
import execution_result
import model_registry as registry
import zcode_worker as worker
import zcode_native_tools as native


class ZCodeErrors(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.NativeController('test_controller_passes_native_setup_and_requires_bound_proof')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def run_case(self, execute, **kw):
        row, run = self.fixture.fixture.dispatch(legacy=False, **kw)
        calls = []
        def once(job, on_event):
            calls.append(job)
            return execute(job, on_event)
        worker.run_task(run, row, registry.get_model(row['model_id']), execute=once)
        self.assertEqual(len(calls), 1)
        result = registry.read_task(run)
        return result, execution_result.project(result)

    def test_selftest_failure_and_missing_or_invalid_exit_are_separate(self):
        command = 'fixture selftest'
        for exit_code in (4, None, False):
            with self.subTest(exit_code=exit_code):
                def execute(job, on_event):
                    result = self.fixture.execute(job, on_event)
                    data = native.load(job['tool_job'])
                    row = {'event': 'PostToolUse', 'session_id': job['session_id'], 'nonce': data['nonce'],
                           'tool': 'Bash', 'call_id': 'test', 'allowed': True, 'ok': exit_code in (None, False),
                           'command_hash': hashlib.sha256(command.encode()).hexdigest(), 'exit_code': exit_code}
                    (Path(job['tool_job']).parent/'zcode-native-events.jsonl').write_text(json.dumps(row)+'\n')
                    return result
                result, view = self.run_case(execute, test_command=command)
                self.assertEqual(result['status'], 'failed' if exit_code==4 else 'completed')
                self.assertIs(result['test_result']['verified'], False)
                self.assertEqual(view['failure']['kind'], 'verification_failed' if exit_code == 4 else 'verification_unconfirmed')
                self.assertEqual(next(s for s in view['stages'] if s['id'] == 'execute')['state'], 'completed')

    def test_unknown_receipt_is_typed_without_resend_or_false_acceptance(self):
        def execute(job, on_event):
            self.fixture.execute(job, on_event)
            # Some legacy/fake drivers return an error without a callback.
            return [{'type': 'error', 'message': 'fixture lost receipt'}]
        result, view = self.run_case(execute)
        self.assertTrue(result['zcode']['outcome_unknown'])
        self.assertEqual(view['failure']['kind'], 'outcome_unknown')
        self.assertFalse(view['review']['accepted'])
        with self.assertRaises(ValueError):
            worker.recovery_guard(result)

    def test_driver_exception_after_send_stays_unknown_even_if_session_closed(self):
        def execute(job, on_event):
            self.fixture.execute(job, on_event)
            raise OSError('fixture transport ended')
        result, view = self.run_case(execute)
        self.assertEqual(view['failure']['kind'], 'outcome_unknown')
        self.assertEqual(result['zcode']['cleanup'], 'closed')
        with self.assertRaises(ValueError):
            worker.recovery_guard(result)

    def test_native_permission_receipt_stays_a_hard_denial(self):
        def execute(job, on_event):
            result = self.fixture.execute(job, on_event)
            result[0]['permission_denied'] = True
            return result
        result, view = self.run_case(execute)
        self.assertEqual(view['failure']['kind'], 'permission_denied')
        self.assertEqual(result['status'], 'failed')
        with self.assertRaises(ValueError):
            worker.recovery_guard(result)

    def test_missing_terminal_after_send_stays_unknown(self):
        def execute(job, on_event):
            self.fixture.execute(job, on_event)
            return []
        result, view = self.run_case(execute)
        self.assertEqual(view['failure']['kind'], 'outcome_unknown')
        with self.assertRaises(ValueError):
            worker.recovery_guard(result)

    def test_hook_denial_after_native_turn_never_becomes_a_recoverable_tool_error(self):
        def execute(job, on_event):
            result = self.fixture.execute(job, on_event)
            (Path(job['tool_job']).parent/'zcode-native-denied.flag').write_text('denied')
            return result
        result, view = self.run_case(execute)
        self.assertEqual(view['failure']['kind'], 'permission_denied')
        self.assertEqual(result['status'], 'failed')
        self.assertFalse(view['review']['accepted'])

    def test_ordinary_failed_tool_does_not_make_a_completed_turn_fail(self):
        def execute(job, on_event):
            result = self.fixture.execute(job, on_event)
            data = native.load(job['tool_job'])
            row = {'event': 'PostToolUseFailure', 'session_id': job['session_id'], 'nonce': data['nonce'],
                   'tool': 'Read', 'call_id': 'missing-file', 'allowed': True, 'ok': False}
            (Path(job['tool_job']).parent/'zcode-native-events.jsonl').write_text(json.dumps(row)+'\n')
            return result
        result, view = self.run_case(execute)
        self.assertEqual(result['tool_calls'][0]['status'], 'failed')
        self.assertEqual(result['status'], 'completed')
        self.assertIsNone(view['failure'])
        self.assertFalse(view['review']['accepted'])

    def test_closed_unknown_is_not_an_execution_or_test_failure(self):
        def execute(job, on_event):
            return self.fixture.execute(job, lambda event: None if event['type'] == 'closed' else on_event(event))
        result, view = self.run_case(execute)
        self.assertEqual(view['failure']['kind'], 'cleanup_unconfirmed')
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(next(s for s in view['stages'] if s['id'] == 'execute')['state'], 'completed')


if __name__ == '__main__':
    unittest.main()
