"""Result meaning and negative evidence tests; no provider, filesystem or service."""
from copy import deepcopy
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import execution_result as result
import failure_classification


class ResultTests(unittest.TestCase):
    def task(self, engine='pi', **values):
        tracker = result.Tracker(engine)
        tracker.mark('prepare', 'completed')
        tracker.mark('execute', 'completed')
        return {'engine': engine, 'status': 'completed', 'changed_files': ['src/a.py'],
                'execution_evidence': tracker.snapshot(), **values}, tracker

    def test_all_engines_share_schema_and_no_auto_acceptance(self):
        for engine in ('pi', 'dsh', 'workbuddy'):
            task, _ = self.task(engine)
            view = result.project(task)
            self.assertEqual(view['schema_version'], 1)
            self.assertEqual(view['execution']['engine'], engine)
            self.assertFalse(view['legacy'])
            self.assertFalse(view['review']['accepted'])
            self.assertEqual(view['verification']['state'], 'not_requested')
            self.assertEqual(view['cleanup']['temporary_files'], 'unknown')

    def test_generated_files_and_tests_survive_cleanup_failure(self):
        task, tracker = self.task('workbuddy', status='failed',
                                  test_command='fixture test', test_result={
                                      'source': 'workbuddy-native-tools', 'exit_code': 0, 'verified': True})
        tracker.mark('verify', 'completed', source='adapter_tool')
        tracker.fail('session_close', 'SESSION_CLOSE_UNCONFIRMED', 'fixture close failure', source='adapter')
        task['execution_evidence'] = tracker.snapshot()
        before = deepcopy(task)
        view = result.project(task, {'state': 'executor_failed'})
        self.assertEqual(task, before)
        self.assertEqual(view['failure']['stage'], 'session_close')
        self.assertEqual(view['verification']['state'], 'passed')
        self.assertFalse(view['verification']['independent'])
        self.assertEqual(view['artifacts']['files'], ['src/a.py'])
        self.assertEqual(view['execution']['status'], 'failed')
        self.assertFalse(view['review']['accepted'])

    def test_failed_test_is_not_generation_failure_or_acceptance(self):
        task, tracker = self.task(test_command='fixture test', test_result={'exit_code': 4})
        tracker.fail('verify', 'TEST_FAILED', 'exit 4', source='controller_test')
        task.update(status='failed', execution_evidence=tracker.snapshot())
        view = result.project(task)
        self.assertEqual(view['failure']['stage'], 'verify')
        self.assertEqual(view['verification']['state'], 'failed')
        self.assertEqual(next(s for s in view['stages'] if s['id'] == 'execute')['state'], 'completed')

    def test_legacy_keywords_do_not_invent_stage_or_permission_proof(self):
        for text in ('清理截图失败', '权限管理模块测试失败', '预算功能有 bug', '文件已生成'):
            task = {'engine': 'pi', 'status': 'failed', 'error': text, 'phase': text,
                    'test_command': 'fixture test', 'review_state': 'accepted',
                    'review': {'state': 'accepted', 'acceptance_valid': True}}
            view = result.project(task)
            self.assertTrue(view['legacy'])
            self.assertEqual(view['failure']['stage'], 'unknown')
            self.assertEqual(view['verification']['state'], 'unknown')
            self.assertFalse(view['review']['accepted'])

    def test_native_exit_zero_without_validated_tool_evidence_stays_unknown(self):
        task, _ = self.task(test_result={'source': 'workbuddy-native-tools', 'exit_code': 0, 'verified': False})
        self.assertEqual(result.project(task)['verification']['state'], 'unknown')
        task['test_result']['exit_code'] = False
        self.assertIsNone(result.project(task)['verification']['exit_code'])

    def test_review_acceptance_requires_authoritative_validated_evidence(self):
        task, _ = self.task(status='failed')
        for state in ('accepted', 'final_accepted'):
            self.assertFalse(result.project(task, {'state': state})['review']['accepted'])
            view = result.project(task, {'state': state, 'acceptance_valid': True})
            self.assertTrue(view['review']['accepted'])
            self.assertEqual(view['execution']['status'], 'failed')
            self.assertEqual(view['review']['state'], state)
            self.assertFalse(result.project(task, {'state': state, 'acceptance_valid': True},
                                            review_error='corrupt')['review']['accepted'])

    def test_guard_rule_refines_generic_error_but_preserves_other_issues(self):
        task, tracker = self.task('workbuddy', status='failed')
        tracker.fail('execute', 'NATIVE_EXECUTION_FAILED', 'unknown', source='adapter')
        tracker.fail('temporary_cleanup', 'TOOL_PERMISSION_DENIED', 'blocked', source='adapter_tool',
                     rule_id='unverified_recursive_cleanup')
        tracker.fail('session_close', 'SESSION_CLOSE_UNCONFIRMED', 'closed unknown', source='adapter')
        task['execution_evidence'] = tracker.snapshot()
        view = result.project(task)
        self.assertEqual(view['failure']['stage'], 'temporary_cleanup')
        self.assertEqual(view['failure']['rule_id'], 'unverified_recursive_cleanup')
        self.assertEqual(len(view['issues']), 3)

    def test_corrupt_version_engine_or_enum_never_promotes_evidence(self):
        task, _ = self.task()
        for evidence in (None, [], {'schema_version': True}, {'schema_version': 2},
                         {'schema_version': 1, 'engine': 'workbuddy'},
                         {'schema_version': 1, 'engine': 'pi', 'stages': {'execute': {'state': [], 'source': {}}},
                          'issues': [{'stage': [], 'source': 'adapter'}]}):
            with self.subTest(evidence=evidence):
                task['execution_evidence'] = evidence
                view = result.project(task)
                self.assertEqual(next(s for s in view['stages'] if s['id'] == 'execute')['state'], 'unknown')

    def test_truncated_or_invalid_artifact_list_does_not_claim_no_changes(self):
        task, tracker = self.task(changed_files=['../private', '/absolute', 'C:\\private', '.model-desk/records.db'])
        self.assertEqual(result.project(task)['artifacts']['state'], 'unknown')
        tracker.fail('artifacts', 'ARTIFACT_SCAN_FAILED', 'fixture')
        task.update(changed_files=[], execution_evidence=tracker.snapshot())
        self.assertEqual(result.project(task)['artifacts']['state'], 'unknown')

    def test_terminal_task_with_unfinished_stage_is_unknown_not_still_running(self):
        task, tracker = self.task(status='failed')
        tracker.mark('execute', 'running')
        task['execution_evidence'] = tracker.snapshot()
        view = result.project(task)
        self.assertEqual(next(s for s in view['stages'] if s['id'] == 'execute')['state'], 'unknown')
        task['status'] = 'running'
        self.assertEqual(next(s for s in result.project(task)['stages'] if s['id'] == 'execute')['state'], 'running')

    def test_new_stage_evidence_does_not_classify_business_prose_as_system_fault(self):
        task, tracker = self.task(status='failed', error='权限管理模块测试失败 budget limit')
        tracker.fail('verify', 'TEST_FAILED', '权限管理模块测试失败', source='controller_test')
        task['execution_evidence'] = tracker.snapshot()
        finding = failure_classification.classify(task)
        self.assertFalse(finding['system_excluded'])
        self.assertFalse(finding['verified'])
        tracker.fail('record', 'RECORD_WRITE_FAILED', 'fixture save', source='persistence')
        task['execution_evidence'] = tracker.snapshot()
        # A later system problem must not replace the first unclassified failure.
        self.assertFalse(failure_classification.classify(task)['system_excluded'])

    def test_structured_guard_rule_is_distinct_from_legacy_keyword_inference(self):
        task, tracker = self.task(status='failed')
        tracker.fail('execute', 'TOOL_PERMISSION_DENIED', 'blocked', source='adapter_tool')
        task['execution_evidence'] = tracker.snapshot()
        finding = failure_classification.classify(task)
        self.assertTrue(finding['verified'])
        self.assertEqual(finding['reason'], 'permission_denied')
        self.assertEqual(finding['source'], 'controller_stage')


if __name__ == '__main__':
    unittest.main()
