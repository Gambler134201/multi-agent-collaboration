"""Whole-task capacity, physical aliases and project barriers, no model calls."""
import concurrent.futures
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import execution_resources as resources
import model_registry as registry
import workflow_settings


class ResourceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='execution-resources-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.project = self.root / 'project'
        self.project.mkdir()
        self.path = self.root / 'reservations.json'
        self.settings = {'enforce_paid_budget': False}

    def state(self, name, *, isolated=True, project=None, engine='pi', **fields):
        return {'id': name, 'model_id': name, 'engine': engine, 'kind': 'edit',
                'project': str(project or self.project), 'workspace': {'eligible': isolated},
                'workbuddy_tool_mode':'native-development-v1', 'zcode_tool_mode':'zcode-native-v1', **fields}

    def model(self, endpoint='http://127.0.0.1:1234/v1', provider='local'):
        return {'provider': provider, 'api_base': endpoint}

    def take(self, state, model=None, *, settings=None, capacity=8):
        rows = resources.requests_for(state, model if model is not None else self.model(),
                                      settings if settings is not None else self.settings)
        return resources.acquire(state['id'], state['project'], rows, capacity=capacity, path=self.path)

    def test_backend_aliases_share_capacity_even_with_different_model_ids(self):
        self.take(self.state('model-a'))
        for endpoint in ('http://localhost:1234/another-path', 'https://[::1]:1234/v1'):
            with self.assertRaises(resources.ResourceBusy):
                self.take(self.state('model-b'), self.model(endpoint))
        self.assertEqual(set(resources.snapshot(self.path)), {'model-a'})

    def test_parallel_setting_defaults_to_serial_and_does_not_change_budgets(self):
        with patch.object(registry, 'HERE', self.root):
            before=workflow_settings.snapshot()
            self.assertEqual(before['max_parallel_tasks'],1)
            saved=workflow_settings.save_settings({'max_parallel_tasks':3})
            self.assertEqual(saved['max_parallel_tasks'],3)
            for field in ('paid_allowed','enforce_paid_budget','enforce_model_call_limit','daily_budget_cny','max_model_calls'):
                self.assertEqual(saved[field],before[field])
            for value in (0,9,True,2.0,'2'):
                with self.assertRaises(ValueError):workflow_settings.save_settings({'max_parallel_tasks':value})

    def test_isolated_worktrees_share_project_but_original_tree_writer_waits(self):
        self.take(self.state('a'))
        self.take(self.state('b'), self.model('http://127.0.0.1:1235'))
        with self.assertRaises(resources.ResourceBusy):
            self.take(self.state('original-writer', isolated=False), self.model('http://127.0.0.1:1236'))
        self.assertEqual(set(resources.snapshot(self.path)), {'a', 'b'})
        resources.release('a', path=self.path)
        self.assertEqual(set(resources.snapshot(self.path)), {'b'})
        resources.release('b', path=self.path)
        self.take(self.state('original-writer', isolated=False))
        with self.assertRaises(resources.ResourceBusy):
            self.take(self.state('reader', kind='chat'), self.model('http://127.0.0.1:1238'))

    def test_unisolated_tasks_in_different_projects_can_use_different_backends(self):
        other = self.root / 'other'
        other.mkdir()
        self.take(self.state('a', isolated=False))
        self.take(self.state('b', isolated=False, project=other), self.model('http://localhost:1235'))
        self.assertEqual(set(resources.snapshot(self.path)), {'a', 'b'})

    def test_native_models_of_one_profile_or_host_are_one_resource(self):
        for engine, field, config in (
                ('workbuddy', 'workbuddy_connection', {'profile': str(self.root / 'profile')}),
                ('zcode', 'zcode_connection', {'home': str(self.root / 'install'), 'port': 19765})):
            first = self.state(engine + '-a', engine=engine, **{field: config})
            second = self.state(engine + '-b', engine=engine, **{field: {**config, 'port': 19766}})
            self.take(first)
            with self.assertRaises(resources.ResourceBusy):
                self.take(second)
            resources.release(first['id'], path=self.path)

    def test_capacity_shrink_does_not_cancel_existing_tasks(self):
        a, b = self.state('a'), self.state('b')
        self.take(a, capacity=2)
        self.take(b, self.model('http://localhost:1235'), capacity=2)
        with self.assertRaises(resources.ResourceBusy) as caught:
            self.take(self.state('c'), self.model('http://localhost:1236'), capacity=1)
        self.assertTrue(caught.exception.capacity)
        self.assertEqual(set(resources.snapshot(self.path)), {'a', 'b'})
        self.assertFalse(self.take(a, capacity=1)['acquired'])

    def test_paid_budget_cap_is_shared_only_when_enforced(self):
        self.take(self.state('a'), self.model('https://a.invalid', 'deepseek'),
                  settings={'enforce_paid_budget': True})
        with self.assertRaises(resources.ResourceBusy):
            self.take(self.state('b'), self.model('https://b.invalid', 'custom'),
                      settings={'enforce_paid_budget': True})
        self.take(self.state('c'), self.model('https://c.invalid', 'custom'))
        self.assertEqual(set(resources.snapshot(self.path)), {'a', 'c'})

    def test_concurrent_competing_bundles_have_one_whole_winner(self):
        barrier = threading.Barrier(8)
        def attempt(number):
            state = self.state('t-' + str(number))
            rows = resources.requests_for(state, self.model(), self.settings)
            barrier.wait(5)
            try:
                resources.acquire(state['id'], state['project'], list(reversed(rows)), capacity=8, path=self.path)
                return state['id']
            except resources.ResourceBusy:
                return None
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(attempt, range(8)))
        winners = [value for value in results if value]
        self.assertEqual(len(winners), 1)
        self.assertEqual(set(resources.snapshot(self.path)), set(winners))

    def test_corruption_unknown_identity_and_immutable_existing_bundle(self):
        self.take(self.state('a'), {})
        with self.assertRaises(resources.ResourceBusy):
            self.take(self.state('unknown-alias'), {})
        with self.assertRaises(ValueError):
            self.take(self.state('a'), self.model('http://localhost:1239'))
        self.assertFalse(resources.release('foreign', path=self.path))
        self.path.write_bytes(b'broken registry')
        with self.assertRaisesRegex(ValueError, '注册表无效'):
            resources.snapshot(self.path)
        self.assertEqual(self.path.read_bytes(), b'broken registry')

    def test_unknown_backend_blocks_known_backend_in_both_directions(self):
        self.take(self.state('unknown'), {})
        with self.assertRaises(resources.ResourceBusy):
            self.take(self.state('known'))
        resources.release('unknown',path=self.path)
        self.take(self.state('known'))
        with self.assertRaises(resources.ResourceBusy):
            self.take(self.state('unknown'), {})

    def test_declared_group_is_additional_not_a_physical_backend_override(self):
        self.take(self.state('a'), {**self.model(),'resource_group':'one'})
        with self.assertRaises(resources.ResourceBusy):
            self.take(self.state('b'), {**self.model(),'resource_group':'two'})
        resources.release('a',path=self.path)
        self.take(self.state('a'), {**self.model(),'resource_group':'same'})
        with self.assertRaises(resources.ResourceBusy):
            self.take(self.state('b'), {**self.model('http://localhost:1235'),'resource_group':'same'})

    def test_semantic_corruption_and_compare_release_fail_closed(self):
        self.take(self.state('a'))
        old=resources.snapshot(self.path)['a']
        with self.assertRaises(ValueError):
            resources.release('a',path=self.path,expected={**old,'pid':999999})
        original=self.path.read_bytes()
        for kind in ('model','project','backend','backend_pool'):
            value=registry.read_json(self.path)
            value['tasks']['a']['requests']=[row for row in old['requests'] if not row['resource'].startswith(kind+':')]
            registry.write_json(self.path,value)
            with self.assertRaises(ValueError):resources.snapshot(self.path)
            self.path.write_bytes(original)
        value=registry.read_json(self.path)
        value['tasks']['a']['project']=str(self.root/'wrong-project')
        registry.write_json(self.path,value)
        with self.assertRaises(ValueError):resources.snapshot(self.path)


if __name__ == '__main__':
    unittest.main()
