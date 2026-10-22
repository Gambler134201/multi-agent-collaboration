"""Offline policy persistence, task snapshots and production sandbox patches."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import endpoint_access as access
import execution_endpoints as endpoints
import model_registry as registry
import dsh_worker
import pi_worker


class AccessTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.enterContext(patch.object(registry, 'HERE', self.root))

    def test_defaults_do_not_write_and_independent_endpoint_updates(self):
        self.assertEqual(access.saved('pi'), 'full')
        self.assertEqual(access.saved('dsh'), 'full')
        self.assertEqual(list(self.root.iterdir()), [])
        endpoints.update('pi', {'access_mode': 'project'}, {})
        self.assertEqual(access.saved('pi'), 'project')
        self.assertEqual(access.saved('dsh'), 'full')
        endpoints.update('dsh', {'access_mode': 'project'}, {})
        endpoints.update('pi', {'access_mode': 'full'}, {})
        self.assertEqual(access.saved('dsh'), 'project')
        self.assertEqual(access.saved('pi'), 'full')
        self.assertFalse((self.root / endpoints.CONFIG_NAME).exists())

    def test_corrupt_and_invalid_modes_fail_closed(self):
        for value in ['bypass', None, {}, [], 1]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                endpoints.update('pi', {'access_mode': value}, {})
        path = self.root / access.CONFIG
        for data in [[], {'schema': 1, 'endpoints': {'pi': 'bypass'}}, {'schema': 1, 'endpoints': {'unknown': 'full'}}]:
            path.write_text(json.dumps(data), encoding='utf-8')
            with self.assertRaises(ValueError):
                access.for_dispatch('pi')
        path.write_text('{broken', encoding='utf-8')
        with self.assertRaises(ValueError):
            access.saved('dsh')

    def test_rework_continuation_and_legacy_keep_snapshot(self):
        old = {'execution_access_mode': 'project'}
        access.update('pi', 'full')
        self.assertEqual(access.for_dispatch('pi', old), 'project')
        self.assertEqual(access.for_dispatch('pi', {}), 'full')
        access.update('pi', 'project')
        self.assertEqual(access.for_dispatch('pi'), 'project')
        self.assertEqual(access.for_dispatch('pi', {}), 'full')
        self.assertEqual(access.sandbox('chat', 'full'), 'read-only')
        self.assertEqual(access.sandbox('chat', 'project'), 'read-only')

    def test_dsh_production_patch_and_session_identity(self):
        model = dict(id='fixture', name='fixture', api_base='http://fixture.invalid', model='fixture')
        with patch.object(dsh_worker, 'availability', return_value={'ready': True}):
            for mode, sandbox in [('project', 'workspace-write'), ('full', 'danger-full-access')]:
                rows = dsh_worker.build_patch(model, 'edit', access=mode)
                self.assertEqual(next(r for r in rows if r.get('id') == 'sandbox-policy')['config']['mode'], sandbox)
        self.assertNotEqual(dsh_worker.fingerprint(model, 'edit', self.root, access='full'),
                            dsh_worker.fingerprint(model, 'edit', self.root, access='project'))

    def test_pi_session_identity_includes_frozen_mode(self):
        model = dict(id='fixture', api_base='http://fixture.invalid', model='fixture')
        with patch.object(pi_worker, 'availability', return_value={'version': 'fixture'}):
            full = pi_worker.fingerprint(model, {'kind': 'edit', 'execution_access_mode': 'full'}, self.root)
            project = pi_worker.fingerprint(model, {'kind': 'edit', 'execution_access_mode': 'project'}, self.root)
        self.assertNotEqual(full, project)

    def test_both_host_endpoint_rows_publish_permission_without_enabling(self):
        access.update('pi', 'project')
        for host in ['codex', 'dsh']:
            result = endpoints.public_rows([], {}, {'pi': {}, 'dsh': {}})
            rows = {r['id']: r for r in result['rows']}
            self.assertEqual(rows['pi']['runtime']['access_mode'], 'project')
            self.assertEqual(rows['dsh']['runtime']['access_mode'], 'full')


if __name__ == '__main__':
    unittest.main()
