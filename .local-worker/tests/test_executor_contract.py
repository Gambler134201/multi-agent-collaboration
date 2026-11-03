"""Executor lifecycle/capability contract cases, one suite for all four engines.

The 2026-10-25 plan (P1) requires the same contract cases to cover the shared
controller used by both brain hosts (Codex/DSH) and every executor, while the
original worker implementations and old records stay untouched.  These tests
therefore pin the contract to what the workers really do:

* lifecycle steps map onto the shared execution_result stages,
* declared capabilities/bindings name real worker entrypoints and evidence
  files that exist in the repository,
* the canonical native_view projection agrees with each engine's recorded
  fields, and
* the shared resend gate agrees with each worker's own recovery guard:
  wherever a guard refuses, the contract never says ``allowed``; wherever the
  contract refuses outright, the guard refuses too.

Everything runs offline on temporary or fabricated records; nothing starts a
service or calls a model.
"""
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import execution_engines as engines
import executor_adapter as adapter
import executor_contract as contract
import model_capabilities as caps

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = {'dsh': 'dsh_worker.py', 'pi': 'pi_worker.py',
           'workbuddy': 'workbuddy_worker.py', 'zcode': 'zcode_worker.py'}
NATIVE_ACCOUNT_ENGINES = ('workbuddy', 'zcode')


class DescribeTests(unittest.TestCase):
    def test_contract_covers_exactly_the_supported_execution_agents(self):
        self.assertEqual(set(contract.engines()), set(engines.AGENTS))
        self.assertEqual(set(adapter.ENGINE_METHODS) & set(engines.AGENTS), set(engines.AGENTS))

    def test_describe_is_valid_for_every_engine(self):
        for engine in contract.engines():
            data = contract.describe(engine)
            self.assertEqual(data['engine'], engine)
            self.assertEqual(data['capabilities']['engine'], engine)
            self.assertTrue(data['capabilities']['implemented'], engine)
            self.assertTrue(data['worker'].startswith(engine + '_worker.'))
            # The honesty rule: no description may claim an OS sandbox.
            self.assertIn('不是操作系统沙箱', data['isolation']['boundary'], engine)
            self.assertTrue(data['session'].get('id_fields') or data['session'].get('native_state_field'))
            self.assertTrue(data['evidence'])

    def test_lifecycle_steps_use_shared_execution_result_stages(self):
        import execution_result
        for step in contract.LIFECYCLE:
            self.assertTrue(step['step'] and step['label'])
            if step['stage'] is not None:
                self.assertIn(step['stage'], execution_result.STAGES, step['step'])
        self.assertEqual([s['step'] for s in contract.LIFECYCLE][-1], 'acceptance')
        self.assertIsNone(contract.LIFECYCLE[-1]['stage'])
        self.assertEqual([s['step'] for s in contract.LIFECYCLE][3], 'prompt_send')

    def test_describe_refuses_unknown_engine(self):
        with self.assertRaises(ValueError):
            contract.describe('mystery')
        with self.assertRaises(ValueError):
            contract.native_view('mystery', {})
        with self.assertRaises(ValueError):
            contract.resend_decision('mystery', {})

    def test_recovery_semantics_are_described_per_engine(self):
        # dsh/pi need no native-close entrypoint (the runtime child process is
        # the session); the native-account engines name their real ones.
        for engine in contract.engines():
            recovery = contract.describe(engine)['recovery']
            self.assertEqual(set(recovery), {'close', 'evidence', 'instance_change'}, engine)
            self.assertTrue(recovery['evidence'] and recovery['instance_change'])
        self.assertIsNone(contract.describe('dsh')['recovery']['close'])
        self.assertIsNone(contract.describe('pi')['recovery']['close'])

    def test_targeted_finalize_entries_are_pinned_to_real_sources(self):
        zcode_source = (ROOT / 'zcode_worker.py').read_text(encoding='utf-8')
        self.assertIn('def finalize_session', zcode_source)
        self.assertIn('--finalize-session', zcode_source)
        self.assertIn('def cleanup_lease', (ROOT / 'workbuddy_worker.py').read_text(encoding='utf-8'))
        bridge = (ROOT / 'agent-runtime' / 'run-zcode.mjs').read_text(encoding='utf-8')
        self.assertIn("action==='finalize'", bridge)
        self.assertIn('closeSession', bridge)
        self.assertIn("sid.startsWith('modeldesk-zcode-')", bridge)


class CapabilityDeclarationTests(unittest.TestCase):
    def test_every_capability_row_is_honest_tri_state_with_notes(self):
        for engine in contract.engines():
            table = adapter.engine_capabilities(engine)
            self.assertEqual(set(table['methods']), set(adapter.CAPABILITY_KEYS), engine)
            for key, state in table['methods'].items():
                self.assertIn(state, (caps.STATE_SUPPORTED, caps.STATE_UNSUPPORTED, caps.STATE_UNVERIFIED))
                self.assertTrue(table['notes'].get(key), (engine, key))
            for key in ('dispatch', 'session', 'events', 'cancel', 'outcome'):
                self.assertEqual(table['methods'][key], caps.STATE_SUPPORTED, (engine, key))
            # Native accounts cannot meter per-task cost; subagents stay denied.
            if engine in NATIVE_ACCOUNT_ENGINES:
                self.assertEqual(table['methods']['usage'], caps.STATE_UNSUPPORTED, engine)
            self.assertNotEqual(table['methods']['subagent'], caps.STATE_SUPPORTED, engine)
            if engine != 'dsh':
                self.assertEqual(table['methods']['subagent'], caps.STATE_UNSUPPORTED, engine)

    def test_delegates_and_bindings_name_real_worker_entrypoints(self):
        for engine in contract.engines():
            table = adapter.engine_capabilities(engine)
            for key, delegate in table['delegates'].items():
                module_name, attribute = delegate.rsplit('.', 1)
                module = __import__(module_name)
                self.assertTrue(callable(getattr(module, attribute)), delegate)
                self.assertEqual(table['bindings'].get(key), adapter.BINDING_DELEGATED)
            if engine in ('dsh', 'pi'):
                for key, state in table['methods'].items():
                    if state == caps.STATE_SUPPORTED and key != 'subagent':
                        self.assertNotEqual(table['bindings'].get(key), adapter.BINDING_UNAVAILABLE,
                                            (engine, key, 'supported capability needs a binding'))
            if engine in ('dsh', 'pi'):
                for key, state in table['methods'].items():
                    if state == caps.STATE_SUPPORTED and key != 'subagent':
                        self.assertNotEqual(table['bindings'].get(key), adapter.BINDING_UNAVAILABLE,
                                            (engine, key, 'supported capability needs a binding'))
            elif engine == 'workbuddy':
                # P4 boundary, pinned: the workbuddy chain supports these
                # operations, but the adapter exposes only task-level dispatch;
                # nothing claims a per-operation transport it does not have.
                self.assertEqual(table['bindings']['dispatch'], adapter.BINDING_DELEGATED, engine)
                self.assertEqual(table['bindings']['subagent'], adapter.BINDING_GATED, engine)
                for key in ('session', 'discover', 'events', 'cancel', 'outcome', 'usage'):
                    self.assertEqual(table['methods'][key], caps.STATE_SUPPORTED if key != 'usage' else caps.STATE_UNSUPPORTED)
                    self.assertEqual(table['bindings'][key], adapter.BINDING_UNAVAILABLE, (engine, key))
            else:
                # ZCode (P2 seam): record operations are adapter-direct, the
                # rest stays refused instead of pretended.
                self.assertEqual(table['bindings']['dispatch'], adapter.BINDING_DELEGATED, engine)
                for key in ('events', 'cancel', 'outcome'):
                    self.assertEqual(table['bindings'][key], adapter.BINDING_DIRECT, (engine, key))
                for key in ('session', 'discover', 'usage'):
                    self.assertEqual(table['bindings'][key], adapter.BINDING_UNAVAILABLE, (engine, key))

    def test_task_adapter_forwards_dispatch_once_and_refuses_the_rest(self):
        dispatched = []
        worker = adapter.adapter_for('zcode', transport={'dispatch': lambda **kw: dispatched.append(kw) or 'ok'})
        self.assertIsInstance(worker, adapter.ZcodeTaskAdapter)
        self.assertEqual(worker.dispatch(task='x'), 'ok')
        self.assertEqual(len(dispatched), 1)
        # ZCode keeps honest refusals for the operations that have no
        # adapter-level call; the direct record operations answer from the
        # (absent) run record instead of pretending a session interface.
        with self.assertRaises(adapter.CapabilityNotWired):
            worker.new_session()
        with self.assertRaises(adapter.CapabilityNotWired):
            worker.usage()
        self.assertIsNone(worker.stream_events(run=None))
        self.assertIsNone(worker.outcome(run=None))
        with self.assertRaises(ValueError):
            worker.cancel(run=None)
        unbound = adapter.adapter_for('workbuddy')
        with self.assertRaisesRegex(adapter.CapabilityNotWired, 'workbuddy_worker.run_task'):
            unbound.dispatch()
        for call in (unbound.new_session, unbound.stream_events, unbound.cancel, unbound.outcome, unbound.usage):
            with self.assertRaises(adapter.CapabilityNotWired):
                call()
        self.assertIs(adapter.resolve('aider'), adapter.ExecutorCapabilityAdapter)

    def test_reports_for_native_account_executors_stay_secret_free_and_deny_subagents(self):
        for engine in ('workbuddy', 'zcode'):
            report = adapter.executor_report(engine)
            self.assertEqual(report['engine'], engine)
            self.assertFalse(report['subagent_ready'])
            self.assertIsNone(report['subagent_lifecycle_schema'])
            self.assertIsNone(report['probe_command'])
            self.assertIn('子代理未开放', report['reason'])
            self.assertEqual((report['identity'] or {}).get('executor'), engine)
            self.assertNotIn('token', __import__('json').dumps(report))


class EvidencePinTests(unittest.TestCase):
    """The evidence file names are part of the contract: a silent rename of a
    worker's job/proof/event files must fail here before records diverge."""

    def test_native_account_evidence_files_exist_in_worker_sources(self):
        expectations = {
            'zcode': ('zcode_native_tools.py',
                      ('zcode-native-job.json', 'zcode-native-proof.json',
                       'zcode-native-events.jsonl', 'zcode-native-denied.flag')),
            'workbuddy': ('workbuddy_native_tools.py',
                          ('workbuddy-native-job.json', 'workbuddy-native-proof.json',
                           'workbuddy-native-events.jsonl', 'workbuddy-native-denied.flag')),
        }
        for engine, (relative, names) in expectations.items():
            source = (ROOT / relative).read_text(encoding='utf-8')
            for name in names:
                self.assertIn(name, source, (engine, name))
                self.assertIn(name, contract.describe(engine)['evidence'].values())

    def test_worker_runtime_and_legacy_evidence_names_are_pinned(self):
        pairs = {
            'dsh': (('dsh_worker.py', ('dsh-job.json', 'cancelled.flag')),
                    ('record_protection.py', ('record-proof.json',))),
            'pi': (('pi_worker.py', ('pi-job.json', 'pi-proof.json', 'cancelled.flag')),),
            'zcode': (('zcode_tools.py', ('zcode-tools-job.json', 'zcode-tools-proof.json')),),
            'workbuddy': (('workbuddy_tools.py', ('workbuddy-tools-job.json', 'workbuddy-tools-proof.json')),),
        }
        for engine, groups in pairs.items():
            for relative, names in groups:
                source = (ROOT / relative).read_text(encoding='utf-8')
                for name in names:
                    self.assertIn(name, source, (engine, name))
            evidence = contract.describe(engine)['evidence']
            if engine == 'dsh':
                self.assertEqual(evidence['record_proof'], 'record-proof.json')
            if engine in ('zcode', 'workbuddy'):
                self.assertEqual(evidence['legacy_proof'], engine + '-tools-proof.json')

    def test_shared_dispatch_wiring_reaches_every_engine_worker(self):
        import panel_server
        for engine, script in SCRIPTS.items():
            self.assertIn(script, panel_server.WORKER_SCRIPTS, engine)
            module = __import__(engine + '_worker')
            self.assertTrue(callable(getattr(module, 'run_task', None)), engine)
            self.assertTrue(callable(getattr(module, 'main', None)), engine)


class NativeViewTests(unittest.TestCase):
    def test_native_account_projection_reads_recorded_fields_only(self):
        state = {'zcode_session_id': 'modeldesk-zcode-abc',
                 'zcode': {'prompt_attempted': True, 'cleanup': 'closed', 'outcome_unknown': False}}
        view = contract.native_view('zcode', state)
        self.assertEqual(view, {'schema': contract.SCHEMA, 'engine': 'zcode',
                                'session_id': 'modeldesk-zcode-abc', 'cleanup': 'closed',
                                'prompt_attempted': True, 'outcome_unknown': False,
                                'prompt_certificate': contract.CERT_RECORDED})
        missing = contract.native_view('zcode', {'id': 'queued'})
        self.assertEqual(missing['session_id'], None)
        self.assertEqual(missing['cleanup'], contract.UNKNOWN)
        self.assertEqual(missing['prompt_certificate'], contract.CERT_ABSENT)

    def test_workbuddy_projection_uses_its_native_state_field(self):
        state = {'workbuddy_session_id': 'modeldesk-wb-' + '0' * 32,
                 'workbuddy': {'prompt_attempted': False, 'cleanup': 'not_created'}}
        view = contract.native_view('workbuddy', state)
        self.assertEqual(view['cleanup'], contract.NOT_CREATED)
        self.assertEqual(view['prompt_attempted'], False)
        self.assertEqual(view['session_id'], state['workbuddy_session_id'])

    def test_dsh_and_pi_project_cleanup_from_controller_evidence(self):
        evidence = {'stages': {'session_close': {'state': 'completed'}}}
        view = contract.native_view('dsh', {'dsh_session_id': 's1', 'dsh_session_key': 'k1',
                                            'execution_evidence': evidence})
        self.assertEqual(view['session_id'], 's1')
        self.assertEqual(view['cleanup'], contract.CLOSED)
        self.assertIsNone(view['prompt_attempted'])
        self.assertEqual(view['prompt_certificate'], contract.CERT_NOT_RECORDED)
        failed = contract.native_view('pi', {'pi_session_id': 's2', 'pi_session_file': 'f',
                                             'execution_evidence': {'stages': {'session_close': {'state': 'failed'}}}})
        self.assertEqual(failed['cleanup'], contract.UNKNOWN)
        self.assertEqual(failed['session_id'], 's2')
        self.assertEqual(contract.native_view('dsh', {})['cleanup'], contract.UNKNOWN)


class ResendGateTests(unittest.TestCase):
    def test_malformed_explicit_send_credentials_never_allow_resend(self):
        for engine in ('zcode', 'workbuddy'):
            for field in ('prompt_attempted', 'outcome_unknown'):
                for bad in (0, 1, 'true', 'false', None, [], {}):
                    with self.subTest(engine=engine, field=field, bad=bad):
                        native = {'prompt_attempted': False, 'outcome_unknown': False, 'cleanup': 'closed'}
                        native[field] = bad
                        result = contract.resend_decision(engine, {engine: native})
                        self.assertNotEqual(result['verdict'], contract.RESEND_ALLOWED)
                        self.assertIsNone(result['view'][field])

    def test_native_account_gate_agrees_with_worker_recovery_guards(self):
        import workbuddy_worker
        import zcode_worker
        cleanups = ('not_created', 'pending', 'closed', 'unknown')
        for engine, worker, field in (('zcode', zcode_worker, 'zcode'),
                                      ('workbuddy', workbuddy_worker, 'workbuddy')):
            for prompt in (False, True):
                for cleanup in cleanups:
                    state = {field: {'prompt_attempted': prompt, 'cleanup': cleanup}}
                    try:
                        worker.recovery_guard(state)
                        guard_refused = False
                    except ValueError:
                        guard_refused = True
                    verdict = contract.resend_decision(engine, state)['verdict']
                    if guard_refused:
                        self.assertNotEqual(verdict, contract.RESEND_ALLOWED, (engine, prompt, cleanup))
                    else:
                        self.assertNotEqual(verdict, contract.RESEND_DENIED, (engine, prompt, cleanup))
                    if verdict == contract.RESEND_DENIED:
                        self.assertTrue(guard_refused, (engine, prompt, cleanup))

    def test_missing_native_state_is_unproven_and_never_allowed(self):
        import workbuddy_worker
        # The worker guard allows records from before the field existed; the
        # contract answers "unproven" so the controller claim gate decides.
        try:
            workbuddy_worker.recovery_guard({})
            allowed_by_guard = True
        except ValueError:
            allowed_by_guard = False
        self.assertTrue(allowed_by_guard)
        self.assertEqual(contract.resend_decision('workbuddy', {})['verdict'], contract.RESEND_UNPROVEN)
        self.assertEqual(contract.resend_decision('zcode', {})['verdict'], contract.RESEND_UNPROVEN)

    def test_unknown_send_is_never_resent_by_the_contract(self):
        for engine, field in (('zcode', 'zcode'), ('workbuddy', 'workbuddy')):
            for state in ({field: {'prompt_attempted': True, 'cleanup': 'closed'}},
                          {field: {'prompt_attempted': True, 'cleanup': 'not_created'}},
                          {field: {'prompt_attempted': False, 'cleanup': 'pending'}},
                          {field: {'prompt_attempted': False, 'cleanup': 'unknown'}},
                          {field: {'prompt_attempted': False, 'cleanup': 'closed', 'outcome_unknown': True}}):
                self.assertEqual(contract.resend_decision(engine, state)['verdict'],
                                 contract.RESEND_DENIED, (engine, state))
        for engine in ('dsh', 'pi'):
            self.assertEqual(contract.resend_decision(engine, {'status': 'queued'})['verdict'],
                             contract.RESEND_UNPROVEN)
            self.assertEqual(contract.resend_decision(engine, {'status': 'completed'})['verdict'],
                             contract.RESEND_UNPROVEN)

    def test_proven_unsent_and_safe_cleanup_is_the_only_allowed_verdict(self):
        for engine, field in (('zcode', 'zcode'), ('workbuddy', 'workbuddy')):
            for cleanup in ('not_created', 'closed'):
                state = {field: {'prompt_attempted': False, 'cleanup': cleanup}}
                decision = contract.resend_decision(engine, state)
                self.assertEqual(decision['verdict'], contract.RESEND_ALLOWED, (engine, cleanup))
                self.assertEqual(decision['view']['prompt_certificate'], contract.CERT_RECORDED)


class ZcodeDirectRecordTests(unittest.TestCase):
    """The P2 seam: events/cancel/outcome read the worker's real records."""

    def test_events_cancel_and_outcome_read_zcode_task_records(self):
        with tempfile.TemporaryDirectory() as folder:
            run = Path(folder)
            record = {'id': 'zcode-fixture', 'status': 'completed', 'engine': 'zcode',
                      'model': 'account:zai-start-plan/GLM-5.3-Flash', 'phase': '执行完成',
                      'summary': 'done', 'changed_files': ['a.py'],
                      'test_result': {'source': 'zcode-mcp-command', 'exit_code': 0, 'verified': True},
                      'tool_calls': [{'title': 'Write', 'status': 'completed', 'input_hash': 'h1'}],
                      'answer': 'answer text', 'ended_at': '2026-10-25T00:00:00Z'}
            (run / 'task.json').write_text(json.dumps(record), encoding='utf-8')
            worker = adapter.adapter_for('zcode')
            events = worker.stream_events(run=run)
            self.assertEqual(events['source'], 'zcode_worker task.json recorded transcript')
            self.assertTrue(any(event['type'] == 'tool' and event['title'] == 'Write'
                                for event in events['events']))
            outcome = worker.outcome(run=run)
            self.assertEqual(outcome['status'], 'completed')
            self.assertEqual(outcome['changed_files'], ['a.py'])
            self.assertEqual(outcome['test']['exit_code'], 0)
            # Native-account usage stays unknown, never an estimate.
            self.assertTrue(outcome['usage']['unknown'])
            cancelled = worker.cancel(run=run, reason='fixture')
            self.assertTrue(cancelled['cancelled'])
            self.assertFalse(cancelled['redispatched'])
            self.assertIn('cancelled.flag', cancelled['cancelled_flag'])


if __name__ == '__main__':
    unittest.main()
