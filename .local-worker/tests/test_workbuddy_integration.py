"""Formal queue/route/account isolation with a fake native client only."""
from copy import deepcopy
from pathlib import Path
import io
import queue
import sys
from unittest.mock import Mock, patch
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import brain_gateway
import brain_context
import delegation_policy
import execution_endpoints as ep
import execution_engines as engines
import model_agent_defaults
import model_registry as registry
import model_router
import panel_server as panel
import workbuddy_executor as wb
import workbuddy_tools as tools
import workbuddy_worker as worker
import workflow_settings
from test_stage_workflow import Base


class IntegrationTests(Base):
    def setUp(self):
        super().setUp()
        self.settings = workflow_settings.save_settings({'enabled': True, 'paid_allowed': True,
            'model_scope': 'all', 'enforce_paid_budget': False, 'enforce_model_call_limit': False})
        self.model = registry.upsert_model({'provider': 'workbuddy', 'model': worker.MODEL})
        ep.update('workbuddy', {'enabled': True}, self.settings)
        self.info = {'home': str(self.project), 'executable': 'fixture.exe', 'cli': 'fixture-cli',
                     'product_name': 'WorkBuddy', 'version': wb.SUPPORTED_VERSION, 'fingerprint': 'fixture'}
        self.config = {'home': str(self.project), 'profile': str(self.project)}
        self.enterContext(patch.object(worker, 'configuration', return_value=self.config))
        self.enterContext(patch.object(worker, 'installation', return_value=self.info))
        self.enterContext(patch.object(engines, 'availability', return_value={'ready': True, 'name': 'fixture'}))

    def body(self, **kw):
        return {'kind': 'edit', 'model_id': self.model['id'], 'route_mode': 'manual',
                'project': str(self.project), 'files': ['a.py'], 'prompt': 'fixture native write',
                'routing': {'acceptance': ['file changed']}, 'source': 'codex', **kw}

    def dispatch(self, **kw):
        with patch.object(panel.EXECUTOR, 'submit') as queue:
            task = panel.submit_task(self.body(**kw))
        queue.assert_called_once()
        return task, registry.task_dir(task['id'])

    def test_native_registry_has_no_api_key_or_default_migration(self):
        self.assertEqual(registry.list_models()['default_model'], 'home')
        with patch.object(registry, 'protect', side_effect=AssertionError('must not decrypt')):
            model = registry.get_model(self.model['id'])
        self.assertEqual(model['api_key'], '')
        self.assertEqual(model['default_agent'], 'workbuddy')
        for body in [{'provider': 'workbuddy', 'api_key': 'not-a-real-key'},
                     {'provider': 'workbuddy', 'model': 'Auto'},
                     {'provider': 'workbuddy', 'default_agent': 'dsh'}]:
            with self.subTest(body=body), self.assertRaises(ValueError):
                registry.upsert_model(body)

    def test_model_and_explicit_engine_cannot_switch_billing_source(self):
        api = registry.list_models()['models'][0]
        for model, engine in [(api, 'workbuddy'), (self.model, 'pi'), (self.model, 'dsh'),
                              (self.model, 'direct'), (self.model, 'aider')]:
            with self.subTest(engine=engine), self.assertRaisesRegex(ValueError, '互换'):
                engines.resolution({'engine': engine}, self.settings, model)
        with self.assertRaises(ValueError):
            model_agent_defaults.update(api['id'], 'workbuddy')
        with self.assertRaises(ValueError):
            model_agent_defaults.update(self.model['id'], 'follow_global')

    def test_native_queue_records_exact_model_agent_account_and_project(self):
        task, run = self.dispatch()
        self.assertEqual((task['engine'], task['model']), ('workbuddy', worker.MODEL))
        self.assertEqual(task['workbuddy_connection'], self.config)
        self.assertTrue(run.is_relative_to(self.project / '.model-desk'))
        self.assertIsNone(task['route']['estimated_cost_cny'])
        self.assertEqual(task['route']['cost']['prediction']['kind'], 'unknown')

    def test_scope_paid_permission_and_each_optional_cap_refuse_without_queue(self):
        for patch_settings in [{'model_scope': 'local_only'}, {'paid_allowed': False},
                               {'enforce_paid_budget': True}, {'enforce_model_call_limit': True}]:
            settings = {**self.settings, **patch_settings}
            with self.subTest(patch_settings=patch_settings), patch.object(workflow_settings, 'snapshot', return_value=settings), \
                 patch.object(panel.EXECUTOR, 'submit') as queue:
                with self.assertRaises(ValueError):
                    panel.submit_task(self.body())
                queue.assert_not_called()

    def test_disabling_native_endpoint_blocks_manual_and_explicit(self):
        ep.update('workbuddy', {'enabled': False}, self.settings)
        for body in [self.body(), self.body(engine='workbuddy')]:
            with self.assertRaisesRegex(ValueError, '已禁用'):
                model_router.choose(body, [self.model], self.settings)

    def test_auto_native_needs_brain_eligibility_and_unknown_is_not_cheapest(self):
        with self.assertRaisesRegex(ValueError, 'eligible_model_ids'):
            model_router.choose(self.body(route_mode='auto'), [self.model], self.settings)
        body = self.body(route_mode='auto', routing={'eligible_model_ids': [self.model['id']]})
        self.assertEqual(model_router.choose(body, [self.model], self.settings)['engine'], 'workbuddy')
        api = registry.list_models()['models'][0]
        body['routing']['eligible_model_ids'].append(api['id'])
        self.assertEqual(model_router.choose(body, [self.model, api], self.settings)['model_id'], api['id'])

    def test_terminal_output_override_and_reuse_are_explicitly_unsupported(self):
        for kw in [{'test_command': 'python test.py'}, {'request_output_tokens': 100},
                   {'reuse_session': True}, {'continue_task': 'old'}]:
            with self.subTest(kw=kw), self.assertRaises(ValueError):
                worker.validate_task(self.body(**kw), self.settings)

    def client(self, scenario='ok'):
        outer = self
        class Native:
            def __init__(self, info, project, profile, model, *, tool_job, on_lease):
                self.job, self.closed = tool_job, False
                outer.native = self
                on_lease({'state': 'created', 'session_id': 'modeldesk-wb-' + 'a'*32,
                          'runtime_id': 'b'*8+'-'+ 'b'*4+'-'+ 'b'*4+'-'+ 'b'*4+'-'+ 'b'*12,
                          'channel': 'fixture', 'pid': 99999})
            def connect(self):
                if self.job:
                    server = tools.FileTools(self.job);server.list_tools()
                return {'session_id': 'fixture-native-session'}
            def chat(self, model, prompt, settings, timeout):
                outer.prompts += 1
                self.on_prompt()
                if scenario == 'unknown':
                    raise wb.OutcomeUnknown('fixture result unknown; no resend')
                if scenario == 'cancel':
                    (Path(self.job).parent / 'cancelled.flag').touch()
                    raise wb.Cancelled('fixture cancelled')
                proof = None
                if self.job:
                    server = tools.FileTools(self.job)
                    server.call('write_file', {'path': 'a.py', 'content': 'native fixture changed'})
                    proof = tools.verify(self.job)
                return {'status': 'completed', 'answer': 'fixture done', 'usage': None, 'file_tool_proof': proof}
            def close(self):
                self.closed = True
                if scenario == 'cleanup_unknown':
                    raise wb.OutcomeUnknown('fixture cleanup unknown')
        self.prompts = 0
        return Native

    def test_successful_native_worker_records_real_files_without_auto_acceptance(self):
        task, run = self.dispatch()
        code = worker.run_task(run, task, registry.get_model(task['model_id']), client_factory=self.client())
        self.assertEqual(code, 0)
        row = registry.read_task(run)
        self.assertEqual(row['status'], 'completed')
        self.assertEqual(row['changed_files'], ['a.py'])
        self.assertEqual((self.project / 'a.py').read_text(), 'native fixture changed')
        self.assertEqual(row['workbuddy']['cleanup'], 'closed')
        self.assertTrue(self.native.closed)
        self.assertEqual(self.prompts, 1)
        self.assertIsNone(row['cost_settled_cny'])
        self.assertIsNone(row['model_calls'])
        self.assertIsNone(row['native_credits_used'])
        self.assertEqual(row['billing_source'], 'workbuddy-native-account')
        self.assertNotIn(delegation_policy.load(run)['state'], {'accepted', 'final_accepted'})

    def test_unknown_result_preserves_identity_and_never_reprompts(self):
        task, run = self.dispatch()
        worker.run_task(run, task, registry.get_model(task['model_id']), client_factory=self.client('unknown'))
        row = registry.read_task(run)
        self.assertEqual(row['status'], 'failed')
        self.assertTrue(row['workbuddy']['outcome_unknown'])
        self.assertEqual(self.prompts, 1)
        with self.assertRaises(ValueError):
            worker.recovery_guard(row)

    def test_cancel_closes_only_owned_client_before_terminal_status(self):
        task, run = self.dispatch()
        worker.run_task(run, task, registry.get_model(task['model_id']), client_factory=self.client('cancel'))
        row = registry.read_task(run)
        self.assertEqual(row['status'], 'cancelled')
        self.assertEqual(row['workbuddy']['cleanup'], 'closed')
        self.assertTrue(self.native.closed)

    def test_cleanup_failure_cannot_be_reported_as_success(self):
        task, run = self.dispatch()
        worker.run_task(run, task, registry.get_model(task['model_id']), client_factory=self.client('cleanup_unknown'))
        row = registry.read_task(run)
        self.assertEqual(row['status'], 'failed')
        self.assertEqual(row['workbuddy']['cleanup'], 'unknown')

    def test_changed_account_binding_refuses_before_native_session_creation(self):
        task, run = self.dispatch()
        task['workbuddy_connection'] = {'home': 'different', 'profile': 'different'}
        factory = Mock()
        worker.run_task(run, task, registry.get_model(task['model_id']), client_factory=factory)
        factory.assert_not_called()
        self.assertIn('配置已变', registry.read_task(run)['error'])

    def test_controller_cancel_sets_flag_without_taskkill_or_false_completion(self):
        task, run = self.dispatch()
        registry.patch_task(run, status='running')
        with patch.object(panel.subprocess, 'run') as kill:
            result = panel.cancel_task(task['id'])
        kill.assert_not_called()
        self.assertTrue(result['cancellation_pending'])
        self.assertTrue((run / 'cancelled.flag').exists())
        self.assertEqual(registry.read_task(run)['status'], 'running')

    def test_native_candidates_explain_caps_without_hiding_model(self):
        with patch.object(brain_gateway, '_settings', return_value={**self.settings, 'enforce_paid_budget': True}):
            data = brain_gateway._candidates(None, {})
        row = next(m for m in data['models'] if m['model_id'] == self.model['id'])
        self.assertTrue(row['selectable'])
        self.assertFalse(row['dispatch_ready'])
        self.assertIn('不能保证', row['authorization_note'])

    def test_native_configuration_does_not_call_model_or_change_defaults(self):
        with patch.object(worker.bridge, 'create_client') as call:
            worker.configure(self.config)
            worker.configure(self.config)
        call.assert_not_called()
        data = registry.list_models()
        self.assertEqual(data['default_model'], 'home')
        self.assertEqual(sum(m['provider'] == 'workbuddy' for m in data['models']), 1)

    def test_unlimited_native_wait_observes_cancel_without_new_wire_requests(self):
        client = wb.NativeChatClient.__new__(wb.NativeChatClient)
        client.counter, client.tainted = 0, False
        client.process = Mock(stdin=io.StringIO())
        client.inbox = queue.Queue()
        checks = []
        def cancel():
            checks.append(True)
            return len(checks) >= 3
        client.cancel_check = cancel
        with self.assertRaises(wb.Cancelled):
            client.request('session/prompt', {}, timeout=0)
        self.assertEqual(client.process.stdin.getvalue().count('session/prompt'), 1)

    def test_cost_presentation_never_claims_native_credits_are_api_usage_or_zero(self):
        facts = panel._cost_facts({'engine': 'workbuddy', 'cost_settled_cny': 0, 'model_calls': 0})
        self.assertFalse(facts['known'])
        self.assertIsNone(facts['settled_cny'])
        self.assertIsNone(facts['unknown_calls'])
        self.assertIn('原生积分', facts['label'])

    def test_both_brain_hosts_dispatch_and_read_native_delivery_without_accepting_it(self):
        for host in ('codex', 'dsh'):
            context = brain_context.build_context(project=str(self.project), host=host,
                owner='fixture-'+host, role='brain', operator_session='fixture-'+host, session_binding='user_bound')
            capability = brain_gateway.register_connection(context.declaration())['capability']
            try:
                with patch.object(panel.EXECUTOR, 'submit') as queue:
                    receipt = brain_gateway.handle('dispatch', {'request_id': 'native-'+host,
                        'prompt': 'fixture file change', 'kind': 'edit', 'files': ['a.py'],
                        'model_id': self.model['id'], 'acceptance': ['file changed']}, capability)
                queue.assert_called_once()
                tid = receipt['task']['task_id']
                run = registry.task_dir(tid)
                task = registry.read_task(run)
                worker.run_task(run, task, registry.get_model(task['model_id']), client_factory=self.client())
                status = brain_gateway.handle('status', {'task_id': tid}, capability)
                delivery = brain_gateway.handle('delivery', {'task_id': tid}, capability)
                self.assertEqual(status['engine'], 'workbuddy')
                self.assertEqual(status['native_execution']['session_id'], 'fixture-native-session')
                self.assertIsNone(delivery['native_execution']['credits_used'])
                self.assertFalse(delivery['accepted'])
            finally:
                brain_gateway._CAPABILITIES.pop(capability, None)

    def test_workbuddy_executor_cannot_review_itself(self):
        task, run = self.dispatch()
        worker.run_task(run, task, registry.get_model(task['model_id']), client_factory=self.client())
        with self.assertRaises(ValueError):
            delegation_policy.accept(run, 'workbuddy', [], 'self report')

    def test_recovery_is_allowed_only_before_prompt_and_after_known_cleanup(self):
        worker.recovery_guard({'engine': 'workbuddy', 'status': 'recovery_pending'})
        worker.recovery_guard({'workbuddy': {'prompt_attempted': False, 'cleanup': 'closed'}})
        for native in [{}, {'prompt_attempted': True, 'cleanup': 'closed'},
                       {'prompt_attempted': False, 'cleanup': 'unknown'}]:
            with self.assertRaises(ValueError):
                worker.recovery_guard({'workbuddy': native})


if __name__ == '__main__':
    unittest.main()
