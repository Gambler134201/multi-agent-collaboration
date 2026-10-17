"""Offline tests for the executor capability adapter.

These tests never start DSH, never call a model and never create a subagent.
The DSH installation is observed read-only, and a missing installation is an
acceptable outcome that must degrade to "unverified" instead of pretending.
"""
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import executor_adapter as adapter
import model_capabilities as caps


ASAR = adapter.executor_path().parent / 'resources' / 'app.asar'


class CapabilityShapeTests(unittest.TestCase):
    def test_dsh_methods_are_declared_and_subagent_is_not_supported(self):
        engine = adapter.engine_capabilities('dsh')
        self.assertTrue(engine['implemented'])
        self.assertEqual(set(engine['methods']), set(adapter.CAPABILITY_KEYS))
        for key in ('dispatch', 'session', 'discover', 'events', 'cancel', 'outcome', 'usage'):
            self.assertEqual(engine['methods'][key], caps.STATE_SUPPORTED, key)
            self.assertTrue(engine['notes'][key])
        self.assertEqual(engine['methods']['subagent'], caps.STATE_UNVERIFIED)

    def test_other_executors_are_explicitly_unimplemented(self):
        for name in ('aider', 'direct', 'unknown-engine'):
            engine = adapter.engine_capabilities(name)
            self.assertFalse(engine['implemented'], name)
            self.assertEqual(engine['known'], name in adapter.ENGINE_METHODS, name)
            for key, state in engine['methods'].items():
                self.assertEqual(state, caps.STATE_UNSUPPORTED, (name, key))
            report = adapter.executor_report(name)
            self.assertFalse(report['subagent_ready'])
            self.assertIn('尚未实现', report['reason'])

    def test_every_supported_claim_names_a_real_transport_call(self):
        for key in adapter.DshCapabilityAdapter.TRANSPORT:
            self.assertEqual(adapter.engine_capabilities('dsh')['methods'][key], caps.STATE_SUPPORTED, key)
        script = (ROOT / 'agent-runtime' / 'run-dsh.mjs').read_text(encoding='utf-8')
        for marker in ('ensureSession', 'getStatus', 'startTurn', 'turn.events', 'turn.cancel', 'turn.result'):
            self.assertIn(marker, script, marker)

    def test_report_is_secret_free(self):
        row = {'id': 'm1', 'name': 'worker', 'provider': 'custom', 'model': 'some-model',
               'agent_preset': 'standard', 'api_key': 'sk-private-test', 'secret': 'blob'}
        report = adapter.executor_report('dsh', model=row)
        text = json.dumps(report, ensure_ascii=False)
        self.assertNotIn('sk-private-test', text)
        self.assertNotIn('blob', text)
        self.assertFalse(report['subagent_ready'])
        self.assertEqual(report['subagent']['decision'], caps.VERDICT_DENY)


class GateTests(unittest.TestCase):
    def test_unknown_model_cannot_start_subagents(self):
        row = {'id': 'm1', 'model': 'some-model', 'agent_preset': 'standard'}
        with self.assertRaises(adapter.SubagentDenied):
            adapter.subagent_gate(row)
        self.assertFalse(adapter.subagent_enabled(row))

    def test_local_model_cannot_start_subagents(self):
        row = {'id': 'home', 'provider': 'local', 'model': 'qwen', 'agent_preset': 'native',
               'subagent': {'policy': 'allow', 'evidence': [{
                   'state': 'supported', 'measured': True, 'tested_at': caps._now().isoformat(),
                   'identity': caps.evidence_identity(executor='dsh', model='qwen', preset='native', version='0.2.0-rc.2')}]}}
        with self.assertRaises(adapter.SubagentDenied):
            adapter.subagent_gate(row)

    def test_denied_policy_cannot_start_subagents(self):
        row = {'id': 'm1', 'model': 'some-model', 'agent_preset': 'standard',
               'subagent': {'policy': 'deny', 'evidence': [{
                   'state': 'supported', 'measured': True, 'tested_at': caps._now().isoformat(),
                   'identity': caps.evidence_identity(executor='dsh', model='some-model', preset='standard', version='0.2.0-rc.2')}]}}
        with self.assertRaises(adapter.SubagentDenied):
            adapter.subagent_gate(row)

    def test_other_executor_cannot_start_subagents(self):
        row = {'id': 'm1', 'model': 'some-model', 'subagent': {'policy': 'allow'}}
        with self.assertRaises(adapter.SubagentDenied):
            adapter.subagent_gate(row, engine='aider')

    def test_matching_measured_grant_is_the_only_allow_path(self):
        identity = caps.evidence_identity(executor='dsh', model='some-model', preset='standard', version='0.2.0-rc.2')
        row = {'id': 'm1', 'model': 'some-model', 'agent_preset': 'standard',
               'subagent': {'policy': 'allow', 'evidence': [{
                   'state': 'supported', 'measured': True, 'tested_at': caps._now().isoformat(), 'identity': identity}]}}
        decision = adapter.subagent_gate(row, identity=identity)
        self.assertEqual(decision['decision'], caps.VERDICT_ALLOW)
        self.assertTrue(adapter.subagent_enabled(row, identity=identity))

    def test_version_change_revokes_the_grant(self):
        row = {'id': 'm1', 'model': 'some-model', 'agent_preset': 'standard',
               'subagent': {'policy': 'allow', 'evidence': [{
                   'state': 'supported', 'measured': True, 'tested_at': caps._now().isoformat(),
                   'identity': caps.evidence_identity(executor='dsh', model='some-model', preset='standard', version='0.1.0')}]}}
        with self.assertRaises(adapter.SubagentDenied):
            adapter.subagent_gate(row)

    def test_read_only_grant_cannot_start_an_edit_subagent(self):
        identity = caps.evidence_identity(executor='dsh', model='some-model', preset='standard',
                                          version='0.2.0-rc.2')
        row = {'id': 'm1', 'model': 'some-model', 'agent_preset': 'standard',
               'subagent': {'policy': 'allow', 'evidence': [{
                   'state': 'supported', 'measured': True, 'tested_at': caps._now().isoformat(),
                   'identity': identity, 'read_only_probe': True}]}}
        self.assertEqual(adapter.subagent_gate(row, identity=identity, mode='chat')['decision'],
                         caps.VERDICT_ALLOW)
        with self.assertRaises(adapter.SubagentDenied):
            adapter.subagent_gate(row, identity=identity, mode='edit')


class IdentityTests(unittest.TestCase):
    def test_identity_has_model_executor_preset_version(self):
        info = adapter.dsh_identity(version='9.9.9', model='some-model', preset='native')
        for key in ('executor', 'model', 'agent_preset', 'version', 'model_version'):
            self.assertIn(key, info)
        self.assertEqual(info['executor'], 'dsh')
        self.assertEqual(info['version'], '9.9.9')
        tuple_identity = adapter.model_identity({'model': 'm', 'agent_preset': 'standard'}, version='1.2.3')
        self.assertEqual(tuple_identity['executor'], 'dsh')
        self.assertEqual(tuple_identity['version'], '1.2.3')

    def test_missing_installation_degrades_without_raising(self):
        with tempfile.TemporaryDirectory() as temp:
            fake = Path(temp) / 'DeepSeek Harness.exe'
            info = adapter.dsh_identity(executable=fake)
            self.assertEqual(info['executor'], 'dsh')
            self.assertIn(info['version'], (caps.UNVERSIONED, None, ''))
            self.assertIsNone(info['executable_fingerprint'])

    def test_small_file_identity_is_hashed(self):
        with tempfile.TemporaryDirectory() as temp:
            fake = Path(temp) / 'runner.exe'
            fake.write_bytes(b'offline-fixture')
            info = adapter.dsh_identity(version='x', executable=fake)
            self.assertEqual(len(info['executable_fingerprint']), 64)

    @unittest.skipUnless(ASAR.is_file(), 'DSH installation not present on this machine')
    def test_installed_version_is_read_not_invented(self):
        info = adapter.dsh_identity()
        self.assertTrue(info['version'], 'packaged DSH release version should be readable')
        self.assertNotEqual(info['version'], caps.UNVERSIONED)
        self.assertEqual(info['version_source'], 'app.asar:' + adapter.DSH_RUNTIME_MANIFEST)


class AdapterBoundaryTests(unittest.TestCase):
    def test_base_adapter_operations_are_not_implemented(self):
        base = adapter.adapter_for('aider', model={'model': 'x'})
        for call in ('dispatch', 'new_session', 'stream_events', 'cancel', 'outcome', 'usage'):
            with self.assertRaises(NotImplementedError):
                getattr(base, call)()
        self.assertFalse(base.supports('dispatch'))

    def test_declared_capabilities_name_their_binding_and_delegate(self):
        """A ``supported`` declaration must not hide an unimplemented method."""
        engine = adapter.engine_capabilities('dsh')
        for key in ('dispatch', 'session'):
            self.assertEqual(engine['methods'][key], caps.STATE_SUPPORTED, key)
            self.assertEqual(engine['bindings'][key], adapter.BINDING_DELEGATED, key)
            delegate = engine['delegates'][key]
            self.assertTrue(delegate.startswith('dsh_worker.'), delegate)
            module_name, func_name = delegate.split('.', 1)
            module = __import__(module_name)
            self.assertTrue(callable(getattr(module, func_name)), delegate)
        for key in ('discover', 'events', 'cancel', 'outcome', 'usage'):
            self.assertEqual(engine['bindings'][key], adapter.BINDING_DIRECT, key)

        dsh = adapter.adapter_for('dsh', model={'model': 'x', 'agent_preset': 'standard'})
        caps_now = dsh.capabilities()
        self.assertFalse(caps_now['ready']['dispatch'])
        self.assertTrue(caps_now['ready']['events'])
        self.assertIs(adapter.resolve('dsh'), adapter.DshCapabilityAdapter)
        self.assertIs(adapter.resolve('aider'), adapter.ExecutorCapabilityAdapter)

    def test_delegated_capability_forwards_once_when_transport_is_bound(self):
        calls = []

        def fake_dispatch(**kwargs):
            calls.append(kwargs)
            return {'dispatched': True}

        dsh = adapter.adapter_for('dsh', transport={'dispatch': fake_dispatch})
        self.assertTrue(dsh.capabilities()['ready']['dispatch'])
        self.assertEqual(dsh.dispatch(run='r1', state={}), {'dispatched': True})
        self.assertEqual(len(calls), 1)

    def test_unbound_delegate_is_explicit_and_never_silent(self):
        dsh = adapter.adapter_for('dsh')
        with self.assertRaises(adapter.CapabilityNotWired) as caught:
            dsh.dispatch(run='r1')
        self.assertEqual(caught.exception.capability, 'dispatch')
        self.assertEqual(caught.exception.delegate, 'dsh_worker.run_task')
        self.assertFalse(isinstance(caught.exception, NotImplementedError))
        self.assertIn('dsh_worker.run_task', str(caught.exception))
        with self.assertRaises(adapter.CapabilityNotWired):
            dsh.new_session(run='r1')

    def test_cancel_writes_the_flag_and_never_dispatches(self):
        dispatched = []
        with tempfile.TemporaryDirectory() as temp:
            run = Path(temp)
            dsh = adapter.adapter_for('dsh', transport={'dispatch': lambda **kw: dispatched.append(kw)})
            result = dsh.cancel(run=run, reason='user')
            self.assertTrue(result['cancelled'])
            self.assertFalse(result['redispatched'])
            self.assertTrue((run / 'cancelled.flag').is_file())
            self.assertFalse((run / 'result.json').exists())
            self.assertEqual(dispatched, [])
            self.assertIsNone(dsh.outcome(run=run))
            self.assertEqual(dispatched, [])

    def test_cancel_requires_an_existing_run_and_never_creates_one(self):
        with tempfile.TemporaryDirectory() as temp:
            missing = Path(temp) / 'nope'
            with self.assertRaises(ValueError):
                adapter.request_cancel(missing)
            self.assertFalse(missing.exists())

    def test_events_read_the_recorded_transcript_without_starting_a_turn(self):
        with tempfile.TemporaryDirectory() as temp:
            run = Path(temp)
            (run / 'task.json').write_text(json.dumps({
                'status': 'completed', 'phase': '执行完成', 'answer': 'done',
                'ended_at': '2026-10-13T10:00:00+00:00', 'api_key': 'sk-private',
                'tool_calls': [{'title': 'edit', 'status': 'completed', 'file': 'a.py'}]}), encoding='utf-8')
            transcript = adapter.read_events(run)
            self.assertEqual(transcript['status'], 'completed')
            kinds = [event['type'] for event in transcript['events']]
            self.assertIn('tool', kinds)
            self.assertIn('text', kinds)
            self.assertNotIn('sk-private', json.dumps(transcript, ensure_ascii=False))
            self.assertIsNone(adapter.read_events(Path(temp) / 'missing'))

    def test_worker_transport_binds_the_real_engine_entrypoints(self):
        import dsh_worker
        transport = dsh_worker.worker_transport()
        self.assertIs(transport['dispatch'], dsh_worker.run_task)
        self.assertIs(transport['session'], dsh_worker._run_task)
        dsh = adapter.adapter_for('dsh', transport=transport)
        caps_now = dsh.capabilities()
        for key in ('dispatch', 'session', 'events', 'cancel', 'outcome', 'usage'):
            self.assertTrue(caps_now['ready'][key], key)

    def test_cancel_does_not_resend_work(self):
        """A cancelled run yields no outcome and no second dispatch."""
        with tempfile.TemporaryDirectory() as temp:
            run = Path(temp)
            (run / 'cancelled.flag').write_text('1', encoding='utf-8')
            self.assertIsNone(adapter.DshCapabilityAdapter().outcome(run=run))
            self.assertFalse((run / 'result.json').exists())

    def test_outcome_reads_recorded_result_without_rerun(self):
        with tempfile.TemporaryDirectory() as temp:
            run = Path(temp)
            (run / 'result.json').write_text(json.dumps({
                'task_id': 't1', 'status': 'failed', 'engine': 'dsh', 'model': 'm',
                'changed_files': ['a.py'], 'summary': 'boom', 'api_key': 'sk-private'}), encoding='utf-8')
            outcome = adapter.read_outcome(run)
            self.assertEqual(outcome['status'], 'failed')
            self.assertEqual(outcome['changed_files'], ['a.py'])
            self.assertNotIn('sk-private', json.dumps(outcome, ensure_ascii=False))

    def test_usage_unknown_instead_of_estimated(self):
        usage = adapter.read_usage(None)
        self.assertTrue(usage['unknown'])
        self.assertFalse(usage['estimated'])
        self.assertEqual(usage['total_tokens'], 0)

    def test_usage_sums_upstream_records(self):
        rows = [{'usage': {'prompt_tokens': 10, 'completion_tokens': 5}},
                {'usage': {'prompt_tokens': 1, 'completion_tokens': 2}}]
        usage = adapter.read_usage(None, record=rows)
        self.assertEqual(usage['requests'], 2)
        self.assertEqual(usage['prompt_tokens'], 11)
        self.assertEqual(usage['completion_tokens'], 7)
        self.assertEqual(usage['total_tokens'], 18)
        self.assertFalse(usage['unknown'])

    def test_adapter_error_does_not_redispatch(self):
        calls = []

        def boom(**kwargs):
            calls.append(kwargs)
            raise RuntimeError('transport error')

        dsh = adapter.adapter_for('dsh', transport={'dispatch': boom})
        with self.assertRaises(RuntimeError):
            dsh.dispatch(run='r1')
        self.assertEqual(len(calls), 1)

    def test_unknown_engine_adapter_has_no_binding(self):
        unknown = adapter.adapter_for('mystery')
        levels = unknown.capabilities()
        operations = {'dispatch': 'dispatch', 'session': 'new_session', 'events': 'stream_events',
                      'cancel': 'cancel', 'outcome': 'outcome', 'usage': 'usage'}
        for key in adapter.CAPABILITY_KEYS:
            self.assertEqual(levels['bindings'][key], adapter.BINDING_UNAVAILABLE, key)
            self.assertFalse(levels['ready'][key], key)
        for key, call in operations.items():
            with self.assertRaises(NotImplementedError):
                getattr(unknown, call)()
        with self.assertRaises(adapter.SubagentDenied):
            unknown.subagent()

    def test_capabilities_view_reports_every_model(self):
        rows = [{'id': 'home', 'provider': 'local', 'model': 'qwen', 'agent_preset': 'native'},
                {'id': 'cloud', 'provider': 'deepseek', 'model': 'deepseek-chat', 'agent_preset': 'standard'}]
        view = adapter.capabilities_view(rows)
        self.assertEqual(view['executor'], 'dsh')
        self.assertEqual(view['allowed_model_ids'], [])
        self.assertEqual(len(view['models']), 2)
        self.assertEqual(view['engine']['engine'], 'dsh')
        self.assertEqual(view['engine']['bindings']['dispatch'], adapter.BINDING_DELEGATED)


if __name__ == '__main__':
    unittest.main(verbosity=2)
