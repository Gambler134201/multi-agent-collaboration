from pathlib import Path
import json
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import collaboration_status as status
import brain_mcp_server as brain


class StartupStatusTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.project = self.temp.name
        self.settings = {'enabled': True, 'readable': True, 'model_scope': 'local_only', 'paid_allowed': False}
        self.candidates = {'workflow': self.settings, 'dispatch_ready_model_ids': ['home'],
                           'models': [{'model_id': 'home', 'name': 'Qwen', 'engine': 'pi', 'dispatch_ready': True,
                                       'capability_notes': 'DO_NOT_EXPORT', 'api_key': 'DO_NOT_EXPORT'}]}

    def call(self, settings=None, candidates=None, project=None):
        with mock.patch.object(status.workflow_settings, 'snapshot', return_value=settings or self.settings), \
             mock.patch.object(status.brain_gateway, '_candidates', return_value=candidates if candidates is not None else self.candidates) as read:
            result = status.snapshot(self.project if project is None else project)
        self.assertEqual(list(Path(self.project).iterdir()), [], 'no project records or Git initialized')
        return result, read

    def test_same_formatter_as_native_brain_and_no_private_candidate_fields(self):
        self.assertIs(status.build_signal, brain._preflight_signal)
        data, read = self.call()
        self.assertEqual(data['user_signal']['state'], 'ready')
        self.assertIn('Qwen', data['user_signal']['text'])
        self.assertEqual(data['project'], str(Path(self.project).resolve()))
        self.assertFalse(data['host_verified'])
        self.assertNotIn('DO_NOT_EXPORT', json.dumps(data))
        self.assertFalse(data['model_called'])
        self.assertFalse(data['task_dispatched'])
        self.assertFalse(data['service_started'])
        read.assert_called_once_with(None, {})

    def test_disabled_and_unreadable_do_not_probe_execution_agents(self):
        for settings, state in [({**self.settings, 'enabled': False}, 'disabled'),
                                ({**self.settings, 'readable': False}, 'unavailable')]:
            data, read = self.call(settings=settings)
            self.assertEqual(data['user_signal']['state'], state)
            read.assert_not_called()

    def test_project_never_falls_back_to_cwd_or_installation(self):
        for project in ['', 'relative-project', str(Path(self.project) / 'not-created')]:
            data, read = self.call(project=project)
            self.assertIsNone(data['project'])
            self.assertEqual(data['user_signal']['state'], 'unavailable')
            self.assertIn('缺少可信', data['user_signal']['text'])
            read.assert_not_called()

    def test_enabled_without_dispatchable_model_is_not_green(self):
        data, _ = self.call(candidates={'workflow': self.settings, 'models': [], 'dispatch_ready_model_ids': []})
        self.assertEqual(data['user_signal']['state'], 'no_executor')
        self.assertNotIn('已就绪', data['user_signal']['text'])

    def test_candidate_snapshot_wins_when_switch_changes_during_preflight(self):
        data, _ = self.call(candidates={**self.candidates, 'workflow': {**self.settings, 'enabled': False}})
        self.assertEqual(data['user_signal']['state'], 'disabled')

    def test_candidate_failure_does_not_expose_raw_exception_or_claim_ready(self):
        with mock.patch.object(status.workflow_settings, 'snapshot', return_value=self.settings), \
             mock.patch.object(status.brain_gateway, '_candidates', side_effect=RuntimeError('DO_NOT_EXPORT')):
            data = status.snapshot(self.project)
        self.assertEqual(data['user_signal']['state'], 'unavailable')
        self.assertNotIn('DO_NOT_EXPORT', json.dumps(data))

    def test_two_projects_use_only_each_explicit_project(self):
        with tempfile.TemporaryDirectory() as other:
            a, _ = self.call()
            b, _ = self.call(project=other)
            self.assertNotEqual(a['project'], b['project'])
            self.assertIn(b['project'], b['user_signal']['text'])
            self.assertEqual(list(Path(other).iterdir()), [])


if __name__ == '__main__':
    unittest.main()
