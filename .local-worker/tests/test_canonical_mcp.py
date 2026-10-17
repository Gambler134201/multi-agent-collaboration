"""Offline tests for the panel/MCP capability entry points.

Every panel and executor call is mocked; no service is restarted, no model is
called, no secret file is read and no subagent is created.
"""
import json
from pathlib import Path
import sys
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import codex_mcp_server as desk
import executor_adapter
import model_capabilities as caps
import model_registry as registry


class WhitelistTests(unittest.TestCase):
    def test_capability_route_is_read_only_and_whitelisted(self):
        self.assertEqual(desk.validate_request('/api/executor/capabilities', 'GET'), '/api/executor/capabilities')
        with self.assertRaises(desk.PanelError):
            desk.validate_request('/api/executor/capabilities', 'POST')

    def test_other_unknown_routes_stay_rejected(self):
        for path in ('/api/executor', '/api/executor/capabilities/log', '/api/subagent', '/api/../api/state',
                     '/api/state?x=1', '/api/models/../secret', 'http://evil/api/state'):
            with self.assertRaises(desk.PanelError, msg=path):
                desk.validate_request(path, 'GET')

    def test_existing_routes_are_untouched(self):
        for path in ('/api/state', '/api/workflow', '/api/reviews', '/api/goals', '/api/stages', '/api/recovery',
                     '/api/tasks/abc123/log'):
            self.assertEqual(desk.validate_request(path, 'GET'), path)
        self.assertEqual(desk.validate_request('/api/models', 'POST'), '/api/models')
        self.assertEqual(desk.validate_request('/api/models/home/save', 'POST'), '/api/models/home/save')


class PanelCallTests(unittest.TestCase):
    def test_capability_proxy_makes_one_read_only_call(self):
        payload = {'schema': 1, 'models': [], 'allowed_model_ids': []}
        with mock.patch.object(desk, 'http_call', return_value=(200, payload)) as call:
            result = desk.panel_call('/api/executor/capabilities', 'GET')
        self.assertEqual(result, {'status': 200, 'data': payload})
        self.assertEqual(call.call_count, 1)
        self.assertEqual(call.call_args[0][:2], ('GET', '/api/executor/capabilities'))

    def test_unavailable_panel_is_not_retried(self):
        with mock.patch.object(desk, 'http_call', side_effect=desk.PanelError('refused')) as call:
            result = desk.panel_call('/api/executor/capabilities', 'GET')
        self.assertEqual(result['status'], 503)
        self.assertEqual(call.call_count, 1)
        self.assertNotIn('token', json.dumps(result, ensure_ascii=False))

    def test_denied_write_is_not_retried_or_forwarded(self):
        with mock.patch.object(desk, 'http_call') as call:
            with self.assertRaises(desk.PanelError):
                desk.panel_call('/api/executor/capabilities', 'POST', body={'policy': 'allow'})
        call.assert_not_called()

    def test_public_capability_payload_has_no_secret(self):
        row = {'id': 'm1', 'name': 'worker', 'provider': 'custom', 'model': 'some-model',
               'agent_preset': 'standard', 'secret': 'blob-value', 'api_key': 'sk-private-test'}
        public = registry.public_model(row)
        view = executor_adapter.capabilities_view([public])
        text = json.dumps(view, ensure_ascii=False)
        self.assertNotIn('sk-private-test', text)
        self.assertNotIn('blob-value', text)
        self.assertNotIn('secret', view['models'][0])
        self.assertEqual(view['allowed_model_ids'], [])
        self.assertEqual(view['models'][0]['decision'], caps.VERDICT_DENY)


class CapabilityViewIntegrationTests(unittest.TestCase):
    def test_view_and_gate_agree_on_every_model(self):
        rows = [
            {'id': 'home', 'provider': 'local', 'model': 'qwen', 'agent_preset': 'native'},
            {'id': 'cloud', 'provider': 'deepseek', 'model': 'deepseek-chat', 'agent_preset': 'standard',
             'subagent': {'policy': 'allow', 'evidence': [{
                 'state': 'supported', 'measured': True, 'tested_at': caps._now().isoformat(),
                 'identity': caps.evidence_identity(executor='dsh', model='deepseek-chat', preset='standard',
                                                    version=caps.UNVERSIONED)}]}},
        ]
        view = executor_adapter.capabilities_view(rows, version=caps.UNVERSIONED)
        by_id = {row['model_id']: row for row in view['models']}
        for row in rows:
            # The gate must be asked with the row's own versioned identity, the
            # same one the view uses; otherwise the comparison is meaningless.
            gate_identity = executor_adapter.model_identity(row, version=caps.UNVERSIONED)
            allowed = executor_adapter.subagent_enabled(row, identity=gate_identity)
            self.assertEqual(allowed, by_id[row['id']]['decision'] == caps.VERDICT_ALLOW, row['id'])
            # The view is the public mirror of the same decision, never a grant.
            self.assertFalse(allowed and by_id[row['id']]['policy'] != 'allow', row['id'])
        self.assertEqual(view['allowed_model_ids'], ['cloud'])
        self.assertEqual(by_id['home']['decision'], caps.VERDICT_DENY)

    def test_dsh_report_is_secret_free_and_never_claims_ready(self):
        report = executor_adapter.executor_report('dsh', model={'id': 'home', 'provider': 'local', 'model': 'qwen',
                                                                'agent_preset': 'native', 'secret': 'blob'})
        self.assertFalse(report['subagent_ready'])
        self.assertNotIn('blob', json.dumps(report, ensure_ascii=False))
        self.assertEqual(report['identity']['executor'], 'dsh')
        # The declaration is honest about *how* each capability is exposed.
        self.assertEqual(report['bindings']['dispatch'], executor_adapter.BINDING_DELEGATED)
        self.assertEqual(report['delegates']['dispatch'], 'dsh_worker.run_task')
        self.assertEqual(report['bindings']['cancel'], executor_adapter.BINDING_DIRECT)
        self.assertEqual(report['capabilities']['subagent'], caps.STATE_UNVERIFIED)

    def test_public_model_and_aggregate_view_agree_on_a_synthetic_grant(self):
        """The panel surfaces must not disagree about the same evidence.

        This is an offline fixture: it proves the decision path, it does not
        grant anything in the real models.json and starts no subagent.
        """
        identity = executor_adapter.model_identity({'model': 'deepseek-chat', 'agent_preset': 'standard'})
        raw = {'id': 'cloud', 'name': 'worker', 'provider': 'deepseek', 'model': 'deepseek-chat',
               'agent_preset': 'standard', 'enabled': True, 'secret': 'blob-value', 'api_key': 'sk-private-test',
               'subagent': {'policy': 'allow', 'evidence': [{
                   'state': 'supported', 'measured': True, 'tested_at': caps._now().isoformat(),
                   'identity': identity}]}}
        public = registry.public_model(raw)
        view = executor_adapter.capabilities_view([raw])
        self.assertEqual(public['subagent']['decision'], caps.VERDICT_ALLOW)
        self.assertEqual(view['models'][0]['decision'], caps.VERDICT_ALLOW)
        self.assertEqual(view['allowed_model_ids'], ['cloud'])
        self.assertEqual(public['subagent']['identity']['version'], identity['version'])
        for payload in (public, view):
            text = json.dumps(payload, ensure_ascii=False)
            self.assertNotIn('blob-value', text)
            self.assertNotIn('sk-private-test', text)

    def test_public_row_never_carries_raw_evidence(self):
        raw = {'id': 'cloud', 'model': 'deepseek-chat', 'agent_preset': 'standard',
               'subagent': {'policy': 'allow', 'evidence': [{'state': 'supported'}]}}
        public = registry.public_model(raw)
        self.assertNotIn('evidence', public['subagent'])
        self.assertIn('digest', public['subagent'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
