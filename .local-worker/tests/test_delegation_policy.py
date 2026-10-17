import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import delegation_policy as policy
import model_registry as registry
import panel_server as panel


class DelegationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old = registry.HERE, registry.REGISTRY, registry.RUNS
        registry.HERE = Path(self.tmp.name)
        registry.REGISTRY = registry.HERE / 'models.json'
        registry.RUNS = registry.HERE / 'runs'
        self.run = self.task('parent')

    def tearDown(self):
        registry.HERE, registry.REGISTRY, registry.RUNS = self.old
        self.tmp.cleanup()

    def task(self, name, count=0, delegated=True):
        run = registry.task_dir(name)
        registry.patch_task(run, id=name, status='completed', model='test', model_calls=3,
                            route={'requirements': {'acceptance': 'A；B'}},
                            delegation={'schema': 1, 'inherited_rework_count': count, 'rework_count': count} if delegated else None)
        return run

    def complete(self, run=None):
        return policy.record_executor(run or self.run, {'completed': True, 'changed_files': ['a.py'], 'test': {'exit_code': 0}})

    def evidence(self, source='reviewer_test', code=0):
        return [{'id': c, 'status': 'pass', 'evidence': [{'source': source, 'detail': 'independent fixture verification', 'exit_code': code}]} for c in ('c1', 'c2')]

    def test_executor_completion_remains_pending_and_stats_do_not_crash(self):
        record = self.complete()
        self.assertEqual(record['state'], policy.EXECUTOR_COMPLETED)
        stats = policy.aggregate()
        self.assertEqual(stats['delegated'], 1)
        self.assertEqual(stats[policy.ACCEPTED], 0)
        row = panel.public_tasks()[0]
        self.assertIn('待独立验收', row['review']['label'])

    def test_acceptance_needs_independent_evidence_for_every_condition(self):
        self.complete()
        for evidence in [self.evidence('executor_test'), self.evidence(code=None), self.evidence()[:1]]:
            with self.assertRaises(ValueError):
                policy.accept(self.run, 'main', evidence)
        mixed = self.evidence()
        mixed[1]['evidence'][0]['source'] = 'executor_report'
        with self.assertRaises(ValueError):
            policy.accept(self.run, 'main', mixed)
        with self.assertRaises(ValueError):
            policy.accept(self.run, 'worker', self.evidence())
        self.assertEqual(policy.accept(self.run, 'main', self.evidence())['state'], policy.ACCEPTED)

    def test_legacy_and_unfinished_results_cannot_be_approved(self):
        legacy = self.task('legacy', delegated=False)
        self.assertIsNone(policy.record_executor(legacy, {'completed': True}))
        with self.assertRaises(ValueError):
            policy.initialize(legacy)
        policy.record_executor(self.run, {'completed': False})
        with self.assertRaises(ValueError):
            policy.accept(self.run, 'main', self.evidence())

    def test_single_rework_is_inherited_and_cannot_be_dispatched_twice(self):
        self.complete()
        policy.request_rework(self.run, 'main', ['c1'], 'Observed wrong result', 'a.py only')
        first = policy.mark_rework_dispatched(self.run, 'child-one')
        self.assertEqual(policy.mark_rework_dispatched(self.run, 'child-one'), first)
        with self.assertRaises(ValueError):
            policy.mark_rework_dispatched(self.run, 'child-two')
        child = self.task('child-one', count=1)
        self.complete(child)
        with self.assertRaises(ValueError):
            policy.request_rework(child, 'main', ['c1'], 'Still wrong', 'a.py only')

    def test_concurrent_rework_claims_have_only_one_winner(self):
        self.complete()
        policy.request_rework(self.run, 'main', ['c1'], 'wrong', 'a.py')
        def claim(name):
            try:
                policy.mark_rework_dispatched(self.run, name)
                return True
            except ValueError:
                return False
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(claim, ['child-a', 'child-b']))
        self.assertEqual(sum(results), 1)

    def test_corrupt_and_negative_counter_records_do_not_reset(self):
        self.complete()
        path = policy.review_path(self.run)
        path.write_text('{invalid', encoding='utf-8')
        with self.assertRaises(ValueError):
            policy.initialize(self.run)
        self.assertTrue(policy.safe_load(self.run)[1])
        path.write_text(json.dumps({'schema': 1, 'rework_count': -1}), encoding='utf-8')
        with self.assertRaises(ValueError):
            policy.load(self.run)

    def test_acceptance_array_is_kept_as_separate_criteria(self):
        run = registry.task_dir('array-task')
        registry.patch_task(run, id='array-task', status='completed', model='test',
                            route={'requirements': {'acceptance': ['alpha', 'beta', 'gamma']}},
                            delegation={'schema': 1})
        record = policy.record_executor(run, {'completed': True})
        self.assertEqual([item['text'] for item in record['criteria']], ['alpha', 'beta', 'gamma'])
        self.assertEqual(len(record['criteria']), 3)

    def test_final_acceptance_preserves_failure_and_rework_count(self):
        self.complete()
        policy.request_rework(self.run, 'main', ['c1'], 'Observed wrong result', 'a.py only')
        child = self.task('child-final', count=1)
        policy.record_executor(child, {'completed': False, 'test': {'exit_code': 1, 'output': 'boom'}})
        record = policy.load(child)
        rows = [{'id': item['id'], 'status': 'pass',
                 'evidence': [{'source': 'reviewer_test', 'detail': 'main reran', 'exit_code': 0}]}
                for item in record['criteria']]
        with self.assertRaises(ValueError):
            policy.accept(child, 'main', rows)
        final = policy.final_accept(child, 'main', rows, 'main took over and reran the test')
        self.assertEqual(final['state'], policy.FINAL_ACCEPTED)
        self.assertEqual(final['executor']['status'], 'failed')
        self.assertEqual(final['rework_count'], 1)
        self.assertEqual(final['final']['executor_status_preserved'], 'failed')
        self.assertTrue(policy.panel_summary(final)['final_accepted'])
        self.assertEqual(policy.aggregate()[policy.FINAL_ACCEPTED], 1)


if __name__ == '__main__':
    unittest.main()
