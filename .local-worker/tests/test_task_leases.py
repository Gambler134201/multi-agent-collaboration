"""Atomic lease semantics: single winner, owner-only release, fail-closed registry."""
import threading
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import task_leases as leases


class LeaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='parallel-leases-')
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'task-leases.json'

    def test_acquire_refuses_second_holder_and_reown_is_idempotent(self):
        first = leases.acquire('model', 'gpu-a', 'task-1', model_id='m1', path=self.path)
        self.assertTrue(first['acquired'])
        self.assertEqual(first['lease']['model_id'], 'm1')
        with self.assertRaisesRegex(ValueError, 'task-1 占用'):
            leases.acquire('model', 'gpu-a', 'task-2', path=self.path)
        again = leases.acquire('model', 'gpu-a', 'task-1', path=self.path)
        self.assertFalse(again['acquired'])
        self.assertEqual(again['lease']['task_id'], 'task-1')
        other = leases.acquire('model', 'gpu-b', 'task-2', path=self.path)
        self.assertTrue(other['acquired'])
        self.assertEqual(leases.holders(self.path)['model:gpu-a']['task_id'], 'task-1')

    def test_release_is_owner_only_and_owner_release_is_idempotent(self):
        leases.acquire('integration', 'proj-a', 'task-1', project='E:/proj', path=self.path)
        with self.assertRaisesRegex(ValueError, '不能释放其他任务'):
            leases.release('integration', 'proj-a', 'task-2', path=self.path)
        freed = leases.release('integration', 'proj-a', 'task-1', path=self.path)
        self.assertTrue(freed['released'])
        self.assertEqual(leases.release('integration', 'proj-a', 'task-1', path=self.path),
                         {'resource': 'integration:proj-a', 'released': False, 'reason': 'not_held'})
        self.assertEqual(leases.holders(self.path), {})
        again = leases.acquire('integration', 'proj-a', 'task-2', path=self.path)
        self.assertTrue(again['acquired'])

    def test_occupancy_lists_only_own_resources(self):
        leases.acquire('model', 'm1', 'task-1', path=self.path)
        leases.acquire('integration', 'p1', 'task-1', path=self.path)
        leases.acquire('model', 'm2', 'task-9', path=self.path)
        self.assertEqual(leases.occupancy('task-1', self.path), ['integration:p1', 'model:m1'])
        self.assertEqual(leases.occupancy('task-none', self.path), [])

    def test_checked_release_compares_entire_claim_under_the_registry_lock(self):
        claim=leases.acquire('integration','project','original',project='E:/project',path=self.path)['lease']
        for altered in ({**claim,'acquired_at':'different'}, {**claim,'pid':-1}):
            before=self.path.read_bytes()
            with self.assertRaisesRegex(ValueError,'快照'):
                leases.release('integration','project','original',expected=altered,path=self.path)
            self.assertEqual(self.path.read_bytes(),before)
        self.assertTrue(leases.release('integration','project','original',expected=claim,path=self.path)['released'])

    def test_identity_and_kind_validation(self):
        with self.assertRaises(ValueError):
            leases.acquire('ports', 'x', 'task-1', path=self.path)
        for bad in ('', '  ', 'a\nb', 'x' * 201):
            with self.assertRaises(ValueError):
                leases.acquire('model', bad, 'task-1', path=self.path)
        with self.assertRaises(ValueError):
            leases.acquire('model', 'ok', '   ', path=self.path)

    def test_concurrent_acquire_has_exactly_one_winner(self):
        barrier = threading.Barrier(8)
        results = {'won': [], 'refused': []}
        lock = threading.Lock()

        def attempt(index):
            barrier.wait()
            try:
                outcome = leases.acquire('model', 'contended', 'task-%d' % index, path=self.path)
                with lock:
                    results['won'].append(outcome['lease']['task_id'])
            except ValueError as error:
                with lock:
                    results['refused'].append(str(error))

        threads = [threading.Thread(target=attempt, args=(index,)) for index in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(len(results['won']), 1)
        self.assertEqual(len(results['refused']), 7)
        self.assertTrue(all('占用' in message for message in results['refused']))
        holder = leases.holders(self.path)['model:contended']
        self.assertEqual(holder['task_id'], results['won'][0])
        self.assertIsInstance(holder['pid'], int)

    def test_recover_is_attributable_and_holder_matched(self):
        leases.acquire('model', 'gpu-a', 'task-old', path=self.path)
        with self.assertRaisesRegex(ValueError, '必须记录原因'):
            leases.recover('model', 'gpu-a', 'task-new', from_task='task-old',
                           reason='   ', path=self.path)
        with self.assertRaisesRegex(ValueError, '接管声明与持有者不符'):
            leases.recover('model', 'gpu-a', 'task-new', from_task='task-other',
                           reason='stale_holder_terminal:completed', path=self.path)
        outcome = leases.recover('model', 'gpu-a', 'task-new', from_task='task-old',
                                 reason='stale_holder_terminal:completed', model_id='m1', path=self.path)
        self.assertTrue(outcome['recovered'])
        lease = leases.holders(self.path)['model:gpu-a']
        self.assertEqual(lease['task_id'], 'task-new')
        self.assertEqual(lease['recovered_from'], 'task-old')
        self.assertEqual(lease['recover_reason'], 'stale_holder_terminal:completed')
        # Taking over an absent lease is a plain acquisition: no fabricated lineage.
        fresh = leases.recover('model', 'gpu-b', 'task-new', from_task='task-gone',
                               reason='stale_holder_terminal:failed', path=self.path)
        self.assertFalse(fresh['recovered'])
        self.assertNotIn('recovered_from', fresh['lease'])
        self.assertNotIn('recover_reason', fresh['lease'])

    def test_corrupt_registry_fails_closed_without_reset(self):
        self.path.write_bytes(b'not-json-at-all')
        before = self.path.read_bytes()
        with self.assertRaisesRegex(ValueError, '租约注册表无效'):
            leases.acquire('model', 'gpu-a', 'task-1', path=self.path)
        with self.assertRaisesRegex(ValueError, '租约注册表无效'):
            leases.holders(self.path)
        self.assertEqual(self.path.read_bytes(), before)


if __name__ == '__main__':
    unittest.main()
