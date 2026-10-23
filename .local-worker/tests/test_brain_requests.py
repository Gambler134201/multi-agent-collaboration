"""Durable at-most-once request-journal tests (temp project, no service)."""
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import brain_requests as journal
import model_registry as registry
import project_storage


class RequestJournalTests(unittest.TestCase):
    def test_receipt_save_failure_after_effect_is_unknown_and_effect_is_not_replayed(self):
        calls = []
        effect = lambda: calls.append(1) or {'task_id': 'original-task'}
        with patch.object(journal, 'complete', side_effect=OSError('fixture disk full')):
            with self.assertRaises(journal.UnknownOutcome) as caught:
                journal.run_once(str(self.project), 'main-a', 'dispatch', 'save-failed', {}, effect)
        self.assertIn('main-a/save-failed', str(caught.exception))
        self.assertEqual(journal.lookup(str(self.project), 'main-a', 'save-failed')['state'], 'unknown')
        with self.assertRaises(journal.UnknownOutcome):
            journal.run_once(str(self.project), 'main-a', 'dispatch', 'save-failed', {}, effect)
        self.assertEqual(calls, [1])

    def test_unknown_marker_failure_cannot_hide_unknown_or_leave_active_claim(self):
        calls = []
        with patch.object(journal, 'complete', side_effect=OSError('fixture save failed')), \
             patch.object(journal, 'mark_unknown', side_effect=OSError('fixture journal unavailable')):
            with self.assertRaises(journal.UnknownOutcome):
                journal.run_once(str(self.project), 'main-a', 'dispatch', 'both-failed', {},
                                 lambda: calls.append(1) or {'task_id': 'original'})
        self.assertFalse(journal._ACTIVE)
        with self.assertRaises(journal.UnknownOutcome):
            journal.run_once(str(self.project), 'main-a', 'dispatch', 'both-failed', {},
                             lambda: calls.append(2) or {'task_id': 'duplicate'})
        self.assertEqual(calls, [1])

    def test_failure_after_commit_preserves_original_receipt(self):
        original = journal.complete
        calls = []
        def committed_then_lost(*args, **kwargs):
            original(*args, **kwargs)
            raise OSError('fixture response after commit lost')
        with patch.object(journal, 'complete', side_effect=committed_then_lost):
            with self.assertRaises(journal.UnknownOutcome):
                journal.run_once(str(self.project), 'main-a', 'dispatch', 'committed', {},
                                 lambda: calls.append(1) or {'task_id': 'original'})
        row = journal.lookup(str(self.project), 'main-a', 'committed')
        self.assertEqual(row['state'], 'completed')
        self.assertEqual(row['receipt']['task_id'], 'original')
        replay = journal.run_once(str(self.project), 'main-a', 'dispatch', 'committed', {},
                                  lambda: calls.append(2))
        self.assertTrue(replay['reused'])
        self.assertEqual(calls, [1])

    def test_recovered_receipt_save_failure_and_rejection_save_failure_are_unknown(self):
        with patch.object(journal, 'complete', side_effect=OSError('fixture save failed')):
            with self.assertRaises(journal.UnknownOutcome):
                journal.run_once(str(self.project), 'main-a', 'dispatch', 'recovery-save', {},
                                 lambda: (_ for _ in ()).throw(RuntimeError('lost')),
                                 recover=lambda: {'task_id': 'found'})
        with patch.object(journal, 'reject', side_effect=OSError('fixture reject save failed')):
            with self.assertRaises(journal.UnknownOutcome):
                journal.run_once(str(self.project), 'main-a', 'dispatch', 'reject-save', {},
                                 lambda: (_ for _ in ()).throw(ValueError('proven refusal')),
                                 reject_on=(ValueError,))

    def test_lookup_does_not_initialize_empty_project_or_change_reservation(self):
        self.assertIsNone(journal.lookup(str(self.project), 'main-a', 'absent'))
        self.assertEqual(list(self.project.iterdir()), [])
        journal.reserve(str(self.project), 'main-a', 'dispatch', 'pending', {})
        before = journal.lookup(str(self.project), 'main-a', 'pending')
        self.assertEqual(before['state'], 'reserved')
        self.assertEqual(journal.lookup(str(self.project), 'main-a', 'pending'), before)
        self.assertIsNone(journal.lookup(str(self.project), 'other-owner', 'pending'))

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.saved = registry.HERE, registry.REGISTRY, registry.RUNS
        registry.HERE = Path(self.tmp.name)
        registry.REGISTRY = registry.HERE / 'models.json'
        registry.RUNS = registry.HERE / 'runs'
        self.project = registry.HERE / 'project'
        self.project.mkdir()
        journal._ACTIVE.clear()

    def tearDown(self):
        journal._ACTIVE.clear()
        registry.HERE, registry.REGISTRY, registry.RUNS = self.saved
        self.tmp.cleanup()

    def test_same_payload_returns_original_receipt_and_changed_payload_is_refused(self):
        first = journal.reserve(str(self.project), 'main-a', 'dispatch', 'req-1',
                                {'b': 2, 'a': 1})
        self.assertFalse(first['reused'])
        self.assertEqual(first['state'], 'reserved')
        completed = journal.complete(str(self.project), 'main-a', 'req-1',
                                     {'task_id': 'task-1', 'status': 'queued'}, 'task-1')
        self.assertEqual(completed['state'], 'completed')
        again = journal.reserve(str(self.project), 'main-a', 'dispatch', 'req-1',
                                {'a': 1, 'b': 2})
        self.assertTrue(again['reused'])
        self.assertEqual(again['receipt']['task_id'], 'task-1')
        with self.assertRaises(journal.RequestConflict):
            journal.reserve(str(self.project), 'main-a', 'dispatch', 'req-1',
                            {'a': 999, 'b': 2})
        with self.assertRaises(journal.RequestConflict):
            journal.reserve(str(self.project), 'main-a', 'review', 'req-1',
                            {'a': 1, 'b': 2})

    def test_concurrent_duplicate_runs_the_effect_once(self):
        calls = []

        def effect():
            calls.append('start')
            time.sleep(0.2)
            calls.append('end')
            return {'task_id': 'task-once'}

        def worker():
            try:
                return journal.run_once(str(self.project), 'main-a', 'dispatch', 'concurrent',
                                        {'prompt': 'x'}, effect)
            except journal.RequestInFlight as error:
                return error

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: worker(), range(2)))
        self.assertEqual(calls, ['start', 'end'])
        self.assertTrue(any(isinstance(row, journal.RequestInFlight) for row in results))
        status = journal.status(str(self.project), 'main-a', 'concurrent')
        self.assertEqual(status['state'], 'completed')
        self.assertEqual(status['receipt']['task_id'], 'task-once')

    def test_leftover_reservation_after_restart_becomes_unknown_and_is_not_replayed(self):
        journal.reserve(str(self.project), 'main-a', 'dispatch', 'lost', {'prompt': 'x'})
        journal._ACTIVE.clear()  # simulate a new process/restart
        with self.assertRaises(journal.UnknownOutcome) as caught:
            journal.reserve(str(self.project), 'main-a', 'dispatch', 'lost', {'prompt': 'x'})
        self.assertIn('main-a/lost', str(caught.exception))
        self.assertEqual(journal.status(str(self.project), 'main-a', 'lost')['state'], 'unknown')
        calls = []
        with self.assertRaises(journal.UnknownOutcome):
            journal.run_once(str(self.project), 'main-a', 'dispatch', 'lost', {'prompt': 'x'},
                             lambda: calls.append(1) or {'task_id': 'x'})
        self.assertEqual(calls, [])

    def test_exception_after_reservation_is_unknown_and_never_replayed(self):
        calls = []

        def effect():
            calls.append(1)
            raise RuntimeError('response lost')

        with self.assertRaises(journal.UnknownOutcome) as caught:
            journal.run_once(str(self.project), 'main-a', 'dispatch', 'uncertain',
                             {'prompt': 'x'}, effect)
        self.assertIn('main-a/uncertain', str(caught.exception))
        self.assertEqual(calls, [1])
        with self.assertRaises(journal.UnknownOutcome):
            journal.run_once(str(self.project), 'main-a', 'dispatch', 'uncertain',
                             {'prompt': 'x'}, lambda: calls.append(2) or {'task_id': 'y'})
        self.assertEqual(calls, [1])
        row = journal.status(str(self.project), 'main-a', 'uncertain')
        self.assertEqual(row['state'], 'unknown')
        self.assertIn('main-a/uncertain', row['reference'])

    def test_attributable_existing_record_recovers_without_replay(self):
        def effect():
            raise RuntimeError('response lost after the task record was written')

        row = journal.run_once(str(self.project), 'main-a', 'dispatch', 'recover',
                               {'prompt': 'x'}, effect,
                               recover=lambda: {'task_id': 'existing-task'})
        self.assertEqual(row['state'], 'completed')
        self.assertEqual(row['receipt']['task_id'], 'existing-task')

    def test_proven_rejection_is_recorded_and_not_replayed(self):
        class ProvenRefusal(ValueError):
            pass

        calls = []

        def effect():
            calls.append(1)
            raise ProvenRefusal('claim already used')

        with self.assertRaises(journal.RequestRejected):
            journal.run_once(str(self.project), 'main-a', 'rework', 'rejected',
                             {'parent': 'p'}, effect, reject_on=(ProvenRefusal,))
        self.assertEqual(calls, [1])
        with self.assertRaises(journal.RequestRejected):
            journal.run_once(str(self.project), 'main-a', 'rework', 'rejected',
                             {'parent': 'p'}, lambda: calls.append(2) or {'task_id': 'x'},
                             reject_on=(ProvenRefusal,))
        self.assertEqual(calls, [1])
        self.assertEqual(journal.status(str(self.project), 'main-a', 'rejected')['state'], 'rejected')

    def test_credentials_are_refused_and_never_persisted(self):
        with self.assertRaises(journal.RequestError):
            journal.reserve(str(self.project), 'main-a', 'dispatch', 'secret-1',
                            {'capability': 'private-value'})
        with self.assertRaises(journal.RequestError):
            journal.reserve(str(self.project), 'main-a', 'dispatch', 'secret-2',
                            {'nested': {'api_key': 'private-value'}})
        with self.assertRaises(journal.RequestError):
            journal.reserve(str(self.project), 'main-a', 'dispatch', 'secret-3', 'not-an-object')
        path = project_storage.records_db(str(self.project))
        conn = project_storage.connect(path, create=False)
        try:
            rows = conn.execute('SELECT * FROM brain_requests').fetchall()
        finally:
            conn.close()
        self.assertEqual(rows, [])

    def test_journal_is_project_owned_and_fingerprint_is_canonical(self):
        other = registry.HERE / 'other'
        other.mkdir()
        journal.reserve(str(self.project), 'main-a', 'dispatch', 'same-id', {'x': 1})
        journal.complete(str(self.project), 'main-a', 'same-id', {'task_id': 'a'})
        second = journal.reserve(str(other), 'main-a', 'dispatch', 'same-id', {'x': 2})
        self.assertFalse(second['reused'])
        self.assertTrue(project_storage.records_db(str(self.project)).is_file())
        self.assertTrue(project_storage.records_db(str(other)).is_file())
        self.assertEqual(journal.fingerprint('dispatch', {'a': 1, 'b': 2}),
                         journal.fingerprint('dispatch', {'b': 2, 'a': 1}))
        self.assertNotEqual(journal.fingerprint('dispatch', {'a': 1}),
                            journal.fingerprint('review', {'a': 1}))
        self.assertNotEqual(journal.fingerprint('dispatch', {'a': 1}),
                            journal.fingerprint('dispatch', {'a': 2}))

    def test_unknown_records_are_readable_json_without_secrets(self):
        journal.reserve(str(self.project), 'main-a', 'review', 'r-2', {'action': 'accept'})
        journal.mark_unknown(str(self.project), 'main-a', 'r-2', 'response lost')
        row = journal.status(str(self.project), 'main-a', 'r-2')
        blob = json.dumps(row, ensure_ascii=False)
        self.assertNotIn('capability', blob.lower())
        self.assertNotIn('token', blob.lower())
        self.assertEqual(row['state'], 'unknown')


if __name__ == '__main__':
    unittest.main()
