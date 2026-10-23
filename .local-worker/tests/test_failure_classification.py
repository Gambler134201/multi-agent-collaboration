"""Negative tests for the public failure-classification interface (schema 1).

The strings used here are the real persisted ones (``task.json`` ``error`` and
review text) plus English/Chinese variants, so the classifier is pinned to what
the system actually recorded rather than to invented examples.
"""
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import failure_classification as fc

class ReviewerFallbackTests(unittest.TestCase):
    def test_failed_summary_fallback_is_reachable_without_error_field(self):
        result=fc.classify({'id':'summary-only','status':'failed','summary':'DSH 提前结束：max_tokens'})
        self.assertEqual(result['reason'],'output_truncated')
        self.assertEqual(result['source'],'summary_text')
        self.assertFalse(result['counts_as_model_quality'])


REAL_SYSTEM_TEXTS = (
    ('已达到本任务模型请求次数上限；需要主脑验收或重新拆分', 'user_call_limit'),
    ('本次请求的保守费用会超出单任务或今日预算，已阻止发送', 'user_budget_limit'),
    ('输入与输出的保守估计超过上下文窗口，请缩小上下文或降低输出上限', 'token_estimate_gate'),
    ('Internal error: turn failed: Connection error.', 'network_error'),
    ('DSH 未公布所选模型，停止派发', 'local_resource'),
    ('DSH 未返回正文，请检查工具记录和文件改动。', 'local_resource'),
    ('DSH 提前结束：max_tokens', 'output_truncated'),
    ('429 Too Many Requests', 'server_rate_limited'),
    ('Rate limit exceeded, please retry later', 'server_rate_limited'),
    ('Request timed out after 600 seconds', 'network_error'),
    ('ECONNRESET', 'network_error'),
    ('permission denied: cannot write file', 'permission_denied'),
    ('Access is denied.', 'permission_denied'),
    ('The operation was canceled by the user.', 'cancelled'),
    ('maximum context length is 65536 tokens', 'server_context_overflow'),
    ('context_length_exceeded', 'server_context_overflow'),
    ('finish_reason=length after max_tokens', 'output_truncated'),
    ('no space left on device', 'local_resource'),
    ('Out of memory while loading the project index', 'local_resource'),
    ('sharing violation while writing task.json', 'windows_task_save'),
    ('WinError 32 文件被占用', 'windows_task_save'),
    ('budget exceeded', 'user_budget_limit'),
    ('call limit reached: max_model_calls', 'user_call_limit'),
    ('用户取消', 'cancelled'),
    ('拒绝访问', 'permission_denied'),
    ('磁盘空间不足', 'local_resource'),
    ('任务保存失败', 'windows_task_save'),
    ('输出被截断', 'output_truncated'),
    ('服务端错误：500', 'server_error'),
    ('用户金额上限已到', 'user_budget_limit'),
)


class ClassificationTests(unittest.TestCase):
    def test_business_permission_and_budget_words_are_not_system_faults(self):
        for text in ('权限配置功能实现不符合需求，断言失败', '费用预算计算逻辑错误，断言失败',
                     'Permission configuration assertion failed', 'Budget calculation assertion failed'):
            with self.subTest(text=text):
                task = {'id':'business-bug', 'status':'failed', 'error':text}
                self.assertEqual(fc.classify(task)['outcome'], fc.OUTCOME_UNKNOWN)
                task['review'] = {'state':'returned', 'executor_status':'completed', 'returned_criteria':['c1']}
                found = fc.classify(task)
                self.assertEqual(found['outcome'], fc.OUTCOME_QUALITY)
                self.assertTrue(found['verified'])
                self.assertFalse(found['system_excluded'])

    def test_authoritative_completed_review_wins_over_legacy_text_and_text_stays_inferred(self):
        found = fc.classify({'id':'reviewed', 'status':'completed', 'error':'permission denied in fixture',
            'review':{'state':'returned', 'executor_status':'completed', 'returned_criteria':['c1']}})
        self.assertEqual(found['source'], 'independent_review')
        self.assertEqual(found['outcome'], fc.OUTCOME_QUALITY)
        inferred = fc.classify({'id':'old', 'status':'failed', 'error':'permission denied'})
        self.assertEqual(inferred['reason'], 'permission_denied')
        self.assertFalse(inferred['verified'])
        self.assertEqual(inferred['confidence'], 'medium')
        self.assertFalse(fc.classify({'failure_class':'quality', 'status':'failed'})['verified'])

    def finding(self, record):
        return fc.classify(record)

    def test_real_and_variant_system_texts_are_never_model_quality(self):
        for text, expected in REAL_SYSTEM_TEXTS:
            record = {'id': 't', 'status': 'failed', 'model_id': 'm', 'error': text}
            found = self.finding(record)
            self.assertEqual(found['reason'], expected, text)
            self.assertFalse(found['counts_as_model_quality'], text)
            self.assertTrue(found['system_excluded'], text)
            self.assertIn(found['source'], ('executor_field', 'task_status', 'summary_text'))
            self.assertTrue(found['evidence'], text)
            self.assertTrue(found['read_only'])

    def test_output_truncation_and_server_overflow_are_distinguished(self):
        truncated = self.finding({'id': 'a', 'status': 'failed', 'error': 'DSH 提前结束：max_tokens'})
        overflow = self.finding({'id': 'b', 'status': 'failed', 'error': 'maximum context length is 65536 tokens'})
        estimate_gate = self.finding({'id': 'c', 'status': 'failed',
                                      'error': '输入与输出的保守估计超过上下文窗口，请缩小输出上限'})
        self.assertEqual(truncated['reason'], 'output_truncated')
        self.assertEqual(overflow['reason'], 'server_context_overflow')
        self.assertEqual(estimate_gate['reason'], 'token_estimate_gate')
        self.assertEqual(len({truncated['reason'], overflow['reason'], estimate_gate['reason']}), 3)
        for found in (truncated, overflow, estimate_gate):
            self.assertFalse(found['counts_as_model_quality'])

    def test_completed_but_independently_returned_is_a_verified_quality_gap(self):
        # Real review record 20261014-000656-0458ec: executor completed, review
        # returned three criteria, and the reviewer prose happens to mention an
        # injected PermissionError in the *test harness*.  That prose must not turn
        # a verified quality gap into a system failure.
        record = {'id': 'q1', 'status': 'completed', 'model_id': 'm',
                  'route': {'requirements': {'category': 'code_multi'}},
                  'changed_files': ['model_router.py'],
                  'review': {'state': 'returned', 'executor_status': 'completed',
                             'returned_criteria': ['离线结算按实际用量', '预算按已结算到达上限'],
                             'problems': 'Independent tests fail; injected PermissionError after writing daily settlement'}}
        found = self.finding(record)
        self.assertEqual(found['outcome'], fc.OUTCOME_QUALITY)
        self.assertTrue(found['counts_as_model_quality'])
        self.assertTrue(found['verified'])
        self.assertEqual(found['source'], 'independent_review')
        self.assertFalse(found['system_excluded'])

    def test_returned_without_completed_delivery_is_not_quality(self):
        # Real record 20261014-042418-b87a64: the previous E1 attempt was killed by
        # max_tokens, delivered nothing, and was still marked returned.
        record = {'id': 'q2', 'status': 'failed', 'model_id': 'm', 'error': 'DSH 提前结束：max_tokens',
                  'changed_files': [],
                  'review': {'state': 'returned', 'executor_status': 'failed',
                             'returned_criteria': ['c1', 'c2', 'c3', 'c4'],
                             'problems': '输出额度不足/输出截断，并非模型能力失败'}}
        found = self.finding(record)
        self.assertEqual(found['outcome'], fc.OUTCOME_SYSTEM)
        self.assertEqual(found['reason'], 'output_truncated')
        self.assertFalse(found['counts_as_model_quality'])

        pending = self.finding({'id': 'q3', 'status': 'failed', 'model_id': 'm',
                                'review': {'state': 'returned', 'executor_status': 'failed',
                                           'returned_criteria': ['c1']}})
        self.assertEqual(pending['outcome'], fc.OUTCOME_UNKNOWN)
        self.assertFalse(pending['counts_as_model_quality'])

    def test_executor_completion_alone_is_not_quality_success(self):
        found = self.finding({'id': 'c1', 'status': 'completed', 'model_id': 'm'})
        self.assertEqual(found['outcome'], fc.OUTCOME_SUCCESS)
        self.assertFalse(found['verified'])
        self.assertFalse(found['counts_as_model_quality'])
        self.assertEqual(found['reason'], 'completed_unverified')

    def test_accepted_review_is_a_verified_success(self):
        found = self.finding({'id': 'a1', 'status': 'completed', 'model_id': 'm',
                              'review': {'state': 'accepted', 'executor_status': 'completed'}})
        self.assertEqual(found['outcome'], fc.OUTCOME_SUCCESS)
        self.assertTrue(found['verified'])
        self.assertEqual(found['source'], 'independent_review')
        self.assertFalse(found['counts_as_model_quality'])

    def test_unknown_failure_is_never_assumed_to_be_model_quality(self):
        for record in ({'id': 'u1', 'status': 'failed'},
                       {'id': 'u2', 'status': 'failed', 'error': ''},
                       {'id': 'u3', 'status': 'failed', 'error': '执行未完成。'}):
            found = self.finding(record)
            self.assertEqual(found['outcome'], fc.OUTCOME_UNKNOWN, record)
            self.assertFalse(found['counts_as_model_quality'], record)

    def test_cancelled_and_blocked_are_excluded(self):
        cancelled = self.finding({'id': 'x1', 'status': 'cancelled'})
        self.assertEqual(cancelled['outcome'], fc.OUTCOME_CANCELLED)
        self.assertTrue(cancelled['system_excluded'])
        blocked = self.finding({'id': 'x2', 'status': 'failed', 'review': {'state': 'blocked'}})
        self.assertEqual(blocked['outcome'], fc.OUTCOME_UNKNOWN)
        self.assertFalse(blocked['counts_as_model_quality'])

    def test_explicit_structured_classification_wins_and_self_reported_quality_is_unverified(self):
        system = self.finding({'id': 'e1', 'status': 'completed', 'failure_class': 'system'})
        self.assertEqual(system['outcome'], fc.OUTCOME_SYSTEM)
        self.assertEqual(system['source'], 'executor_field')
        quality = self.finding({'id': 'e2', 'status': 'failed', 'failure': {'kind': 'quality'}})
        self.assertEqual(quality['outcome'], fc.OUTCOME_UNKNOWN)
        self.assertFalse(quality['counts_as_model_quality'])

    def test_reading_history_never_mutates_it(self):
        history = [{'id': 'A', 'status': 'completed', 'model_id': 'm', 'review': {'state': 'accepted'}},
                   {'id': 'B', 'status': 'failed', 'parent_task_id': 'A', 'error': '429 Too Many Requests'}]
        before = json.dumps(history, ensure_ascii=False, sort_keys=True)
        report = fc.classify_history(history)
        self.assertEqual(json.dumps(history, ensure_ascii=False, sort_keys=True), before)
        self.assertTrue(report['read_only'])
        self.assertEqual(len(report['findings']), 2)


class ReworkFamilyTests(unittest.TestCase):
    def test_quality_rework_family_is_counted_once(self):
        history = [
            {'id': 'A', 'status': 'completed', 'model_id': 'm',
             'route': {'requirements': {'category': 'code_multi'}},
             'review': {'state': 'returned', 'executor_status': 'completed', 'returned_criteria': ['c1']}},
            {'id': 'B', 'status': 'failed', 'parent_task_id': 'A', 'model_id': 'm', 'error': 'DSH 提前结束：max_tokens'},
            {'id': 'C', 'status': 'failed', 'parent_task_id': 'B', 'model_id': 'm', 'error': '429 Too Many Requests'},
        ]
        report = fc.classify_history(history)
        self.assertEqual(report['families_total'], 1)
        self.assertEqual(report['quality_rework_families'], 1)
        self.assertEqual(report['families']['A']['outcome'], fc.OUTCOME_QUALITY)
        self.assertEqual(sorted(report['families']['A']['members']), ['A', 'B', 'C'])
        self.assertEqual(report['records'], 3)

    def test_family_index_resolves_a_multi_level_parent_chain(self):
        history = [{'id': 'A'}, {'id': 'B', 'parent_task_id': 'A'},
                   {'id': 'C', 'delegation': {'parent_task_id': 'B'}}, {'id': 'D'}]
        index = fc.family_index(history)
        self.assertEqual(index['A'], 'A')
        self.assertEqual(index['B'], 'A')
        self.assertEqual(index['C'], 'A')
        self.assertEqual(index['D'], 'D')

    def test_system_only_family_is_excluded_from_quality_counts(self):
        history = [
            {'id': 'S1', 'status': 'failed', 'model_id': 'm', 'error': '已达到本任务模型请求次数上限'},
            {'id': 'S2', 'status': 'failed', 'model_id': 'm', 'error': '本次请求的保守费用会超出单任务或今日预算'},
            {'id': 'S3', 'status': 'failed', 'model_id': 'm', 'error': 'Internal error: turn failed: Connection error.'},
            {'id': 'S4', 'status': 'failed', 'model_id': 'm', 'error': '输入与输出的保守估计超过上下文窗口'},
            {'id': 'S5', 'status': 'cancelled', 'model_id': 'm'},
        ]
        report = fc.classify_history(history)
        self.assertEqual(report['quality_rework_families'], 0)
        self.assertEqual(report['quality_records'], 0)
        self.assertEqual(report['system_excluded_records'], 5)
        self.assertEqual(report['system_excluded_families'], 5)
        self.assertFalse(any(f['counts_as_model_quality'] for f in report['findings']))


if __name__ == '__main__':
    unittest.main()
