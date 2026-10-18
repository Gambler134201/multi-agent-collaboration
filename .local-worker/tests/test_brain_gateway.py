"""Broker operation tests with a real temp project and mocked executor only."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import brain_context
import brain_gateway
import delegation_policy as policy
import model_registry as registry
import panel_server as panel
import project_storage

PROJECT = None


class BrokerTests(unittest.TestCase):
    def setUp(self):
        global PROJECT
        self.tmp = tempfile.TemporaryDirectory()
        self.saved = registry.HERE, registry.REGISTRY, registry.RUNS
        registry.HERE = Path(self.tmp.name)
        registry.REGISTRY = registry.HERE / 'models.json'
        registry.RUNS = registry.HERE / 'runs'
        registry.write_json(registry.HERE / 'workflow.json', {'enabled': True})
        self.project = registry.HERE / 'project'
        self.project.mkdir()
        self.other_project = registry.HERE / 'other'
        self.other_project.mkdir()
        PROJECT = str(self.project)
        registry.write_json(registry.REGISTRY, {'default_model': 'mock', 'models': [{
            'id': 'mock', 'name': 'Mock', 'provider': 'local', 'model': 'mock',
            'enabled': True, 'max_input_tokens': 32768, 'max_output_tokens': 4096,
            'preferred_tasks': [], 'max_task_complexity': 'simple',
        }]})
        brain_gateway._CAPABILITIES.clear()
        self.submitted = []
        self.patch_engine = mock.patch.object(panel.dsh_worker, 'availability',
                                              return_value={'ready': True, 'message': ''})
        self.patch_engine.start()
        self.addCleanup(self.patch_engine.stop)
        self.patch_submit = mock.patch.object(
            panel.EXECUTOR, 'submit', side_effect=lambda fn, task_id: self.submitted.append(task_id))
        self.patch_submit.start()
        self.addCleanup(self.patch_submit.stop)

    def tearDown(self):
        brain_gateway._CAPABILITIES.clear()
        registry.HERE, registry.REGISTRY, registry.RUNS = self.saved
        self.tmp.cleanup()

    def context(self, owner='main-a', project=None, role='brain', session='s-a'):
        return brain_context.build_context(
            project=str(project or self.project), host='test-host', owner=owner, role=role,
            operator_session=session, session_binding='user_bound')

    def capability(self, **kwargs):
        registered = brain_gateway.register_connection(self.context(**kwargs).declaration())
        return registered['capability']

    def dispatch(self, capability, request_id='dispatch-1', **extra):
        payload = {'request_id': request_id, 'prompt': 'Implement the requested change',
                   'kind': 'edit', 'files': ['a.py'],
                   'acceptance': ['criterion one', 'criterion two'], **extra}
        return brain_gateway.handle('dispatch', payload, capability)

    def complete(self, task_id, project=None):
        run = registry.task_dir(task_id, str(project or self.project))
        registry.patch_task(run, status='completed', phase='已完成', ended_at=registry.now())
        (run / 'answer.txt').write_text('mock deliverable', encoding='utf-8')
        policy.record_executor(run, {'completed': True, 'changed_files': ['a.py'],
                                     'test': {'exit_code': 0, 'output': 'ok'}})
        return run

    def evidence(self):
        return [{'id': 'c1', 'status': 'pass',
                 'evidence': [{'source': 'reviewer_test', 'detail': 'reran the test',
                               'exit_code': 0}]},
                {'id': 'c2', 'status': 'pass',
                 'evidence': [{'source': 'reviewer_file', 'detail': 'inspected a.py'}]}]

    # ------------------------------------------------------------------ tests

    def test_registration_returns_private_capability_and_public_context_only(self):
        registered = brain_gateway.register_connection(self.context().declaration())
        self.assertIn('capability', registered)
        self.assertNotIn(registered['capability'], json.dumps(registered['context']))
        self.assertEqual(registered['context']['owner']['id'], 'main-a')
        self.assertTrue(registered['limits']['getters_wait'] is False)
        described = brain_gateway.handle('describe', {}, registered['capability'])
        self.assertEqual(described['context']['project'], str(self.project))
        self.assertTrue(described['context']['mutations_allowed'])

    def test_unknown_capability_is_auth_error_and_unknown_operation_is_refused(self):
        with self.assertRaises(brain_gateway.AuthError):
            brain_gateway.handle('describe', {}, 'not-a-capability')
        capability = self.capability()
        with self.assertRaises(brain_gateway.BrokerError):
            brain_gateway.handle('arbitrary_http', {}, capability)
        with self.assertRaises(brain_gateway.BrokerError):
            brain_gateway.handle('status', {'task_id': 'x', 'owner': 'main-b'}, capability)

    def test_dispatch_persists_owner_and_request_before_queueing(self):
        capability = self.capability()
        result = self.dispatch(capability)
        task_id = result['task']['task_id']
        self.assertEqual(self.submitted, [task_id])
        task = registry.read_task(registry.task_dir(task_id, str(self.project)))
        self.assertEqual(task['source'], 'brain')
        self.assertEqual(task['owner'], 'main-a')
        self.assertEqual(task['request_id'], 'dispatch-1')
        self.assertEqual(task['brain']['owner'], 'main-a')
        self.assertEqual(task['brain']['project'], str(self.project))
        self.assertEqual(task['brain']['session']['binding'], 'user_bound')
        self.assertFalse(task['brain']['adopted'])
        self.assertEqual(task['delegation']['mode'], 'outcome')
        self.assertEqual(task['status'], 'queued')
        # No controller credential/private capability is written into the record.
        blob = json.dumps(task, ensure_ascii=False)
        self.assertNotIn(capability, blob)
        self.assertNotIn('panel_token', blob)
        self.assertNotIn('secret', blob.lower())

    def test_duplicate_dispatch_is_at_most_once_and_changed_payload_refused(self):
        capability = self.capability()
        first = self.dispatch(capability)
        second = self.dispatch(capability)
        self.assertTrue(second['reused'])
        self.assertEqual(first['task']['task_id'], second['task']['task_id'])
        self.assertEqual(len(self.submitted), 1)
        with self.assertRaises(brain_gateway.BrokerError):
            self.dispatch(capability, prompt='A different task with the same id')
        self.assertEqual(len(self.submitted), 1)

    def test_concurrent_duplicate_dispatch_creates_one_task(self):
        from concurrent.futures import ThreadPoolExecutor
        capability = self.capability()

        def call(_):
            try:
                return self.dispatch(capability, request_id='concurrent-dispatch')
            except brain_gateway.BrokerError as error:
                return error

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(call, range(2)))
        self.assertEqual(len(self.submitted), 1)
        task_ids = {row['task']['task_id'] for row in results if isinstance(row, dict)}
        self.assertEqual(task_ids, {self.submitted[0]})
        self.assertTrue(any(isinstance(row, brain_gateway.BrokerError) for row in results) or
                        all(isinstance(row, dict) for row in results))

    def test_owner_project_and_legacy_tasks_are_never_adopted(self):
        task_id = self.dispatch(self.capability())['task']['task_id']
        other_owner = self.capability(owner='main-b', session='s-b')
        with self.assertRaisesRegex(brain_gateway.BrokerError, '另一个主脑'):
            brain_gateway.handle('status', {'task_id': task_id}, other_owner)
        other_project = self.capability(owner='main-a', project=self.other_project, session='s-c')
        with self.assertRaisesRegex(brain_gateway.BrokerError, '不属于当前项目'):
            brain_gateway.handle('status', {'task_id': task_id}, other_project)
        legacy = registry.prepare_task_dir('legacy-task', str(self.project))
        registry.patch_task(legacy, id='legacy-task', status='completed', engine='dsh',
                            model_id='mock', model='mock', model_name='Mock', kind='edit',
                            project=str(self.project), files=['a.py'],
                            route={'requirements': {'acceptance': 'old'}})
        with self.assertRaisesRegex(brain_gateway.BrokerError, '旧任务没有主脑归属'):
            brain_gateway.handle('status', {'task_id': 'legacy-task'}, self.capability())

    def test_executor_role_can_read_describe_but_cannot_dispatch(self):
        capability = self.capability(role='executor')
        described = brain_gateway.handle('describe', {}, capability)
        self.assertEqual(described['context']['role'], 'executor')
        with self.assertRaisesRegex(brain_gateway.BrokerError, '执行器角色'):
            self.dispatch(capability)
        self.assertEqual(self.submitted, [])

    def test_brain_review_accepts_only_with_independent_evidence_and_records_request(self):
        task_id = self.dispatch(self.capability())['task']['task_id']
        run = self.complete(task_id)
        delivery = brain_gateway.handle('delivery', {'task_id': task_id}, self.capability())
        self.assertEqual(delivery['delivery_state'], 'executor_completed_unaccepted')
        self.assertFalse(delivery['accepted'])
        self.assertIn('mock deliverable', delivery['answer'])
        with self.assertRaises(brain_gateway.BrokerError):
            brain_gateway.handle('review', {
                'request_id': 'review-1', 'task_id': task_id, 'action': 'accept',
                'criteria': [{'id': 'c1', 'status': 'pass',
                              'evidence': [{'source': 'executor_test', 'detail': 'self reported',
                                            'exit_code': 0}]},
                             {'id': 'c2', 'status': 'pass',
                              'evidence': [{'source': 'executor_report', 'detail': 'self reported'}]}]},
                self.capability())
        accepted = brain_gateway.handle('review', {
            'request_id': 'review-2', 'task_id': task_id, 'action': 'accept',
            'criteria': self.evidence()}, self.capability())
        self.assertEqual(accepted['receipt']['summary']['state'], policy.ACCEPTED)
        again = brain_gateway.handle('review', {
            'request_id': 'review-2', 'task_id': task_id, 'action': 'accept',
            'criteria': self.evidence()}, self.capability())
        self.assertTrue(again['reused'])
        record = policy.load(run)
        self.assertEqual(record['last_request_id'], 'review-2')
        self.assertTrue(any(row.get('request_id') == 'review-2' for row in record['decisions']))

    def test_plain_panel_review_route_cannot_approve_a_brain_task(self):
        task_id = self.dispatch(self.capability())['task']['task_id']
        self.complete(task_id)
        with self.assertRaisesRegex(ValueError, '仅填写 actor'):
            panel.update_review(task_id, {'action': 'accept', 'actor': 'main',
                                          'criteria': self.evidence()})

    def test_rework_is_one_new_session_round_same_model_engine(self):
        capability = self.capability()
        task_id = self.dispatch(capability)['task']['task_id']
        self.complete(task_id)
        returned = brain_gateway.handle('review', {
            'request_id': 'return-1', 'task_id': task_id, 'action': 'return',
            'failed_criteria': ['c1'], 'problems': 'observed wrong result',
            'boundaries': 'a.py only'}, capability)
        self.assertEqual(returned['receipt']['summary']['state'], policy.RETURNED)
        child = brain_gateway.handle('rework', {
            'request_id': 'rework-1', 'parent_task_id': task_id}, capability)
        child_id = child['task']['task_id']
        child_task = registry.read_task(registry.task_dir(child_id, str(self.project)))
        parent = registry.read_task(registry.task_dir(task_id, str(self.project)))
        self.assertEqual(child_task['model_id'], parent['model_id'])
        self.assertEqual(child_task['engine'], parent['engine'])
        self.assertEqual(child_task['rework_of'], task_id)
        self.assertIsNone(child_task.get('dsh_session_id'))
        self.assertIsNone(child_task.get('dsh_session_key'))
        self.assertEqual(child_task['delegation']['rework_count'], 1)
        # Same request id returns the original child, no second slot.
        again = brain_gateway.handle('rework', {
            'request_id': 'rework-1', 'parent_task_id': task_id}, capability)
        self.assertTrue(again['reused'])
        self.assertEqual(again['task']['task_id'], child_id)
        # A new request id is refused because the single slot is already used.
        with self.assertRaises(brain_gateway.BrokerError):
            brain_gateway.handle('rework', {
                'request_id': 'rework-2', 'parent_task_id': task_id}, capability)
        self.assertEqual(self.submitted.count(child_id), 1)


    def test_same_owner_different_session_or_host_cannot_read_original(self):
        from dataclasses import replace
        original = self.context()
        cap = brain_gateway.register_connection(original.declaration())['capability']
        task = self.dispatch(cap)['task']['task_id']
        for other in (replace(original, operator_session='other-session'),
                      replace(original, host='different-host')):
            other_cap = brain_gateway.register_connection(other.declaration())['capability']
            with self.subTest(host=other.host, session=other.session_reference):
                with self.assertRaises(brain_gateway.BrokerError):
                    brain_gateway.handle('status', {'task_id': task}, other_cap)

    def test_duplicate_ids_are_scoped_to_responsible_conversation(self):
        first = self.dispatch(self.capability(session='first'))
        second = self.dispatch(self.capability(session='second'))
        self.assertNotEqual(first['task']['task_id'], second['task']['task_id'])
        self.assertEqual(len(self.submitted), 2)

    def test_review_write_then_valueerror_recovers_actual_acceptance(self):
        import brain_requests
        cap = self.capability()
        task_id = self.dispatch(cap)['task']['task_id']
        self.complete(task_id)
        actual = policy._mark_task
        def after_write(*args, **kwargs):
            actual(*args, **kwargs)
            raise ValueError('failure after durable review')
        with mock.patch.object(policy, '_mark_task', side_effect=after_write):
            recovered = brain_gateway.handle('review', {'request_id':'after-write', 'task_id':task_id,
                'action':'accept', 'criteria':self.evidence()}, cap)
        self.assertEqual(recovered['receipt']['review']['state'], policy.ACCEPTED)
        self.assertEqual(policy.load(registry.task_dir(task_id))['state'], policy.ACCEPTED)


    def test_disabled_switch_refuses_new_dispatch_but_preserves_original_receipt(self):
        cap = self.capability()
        original = self.dispatch(cap)
        registry.write_json(registry.HERE / 'workflow.json', {'enabled':False})
        replay = self.dispatch(cap)
        self.assertEqual(original['task']['task_id'], replay['task']['task_id'])
        with self.assertRaises(brain_gateway.BrokerError):
            self.dispatch(cap, request_id='new-after-off')
        self.assertEqual(len(self.submitted), 1)


    def test_scope_gate_is_reused_and_raw_commands_are_not_a_tool_parameter(self):
        cap = self.capability()
        with self.assertRaises(brain_gateway.BrokerError):
            self.dispatch(cap, test_command='arbitrary command')
        registry.write_json(registry.HERE / 'workflow.json',
                            {'enabled':True, 'model_scope':'paid_only'})
        with self.assertRaises(brain_gateway.BrokerError):
            self.dispatch(cap)
        self.assertEqual(self.submitted, [])


    def test_paid_scope_keeps_deepseek_and_explains_out_of_scope_default(self):
        data = registry.read_json(registry.REGISTRY)
        data['models'].append({
            'id': 'ds', 'name': 'DeepSeek', 'provider': 'deepseek',
            'model': 'deepseek-v4-flash', 'api_base': 'https://example.test/v1',
            'secret': 'fixture-secret', 'enabled': True, 'api_key_set': True,
            'max_input_tokens': 65536, 'max_output_tokens': 8192,
            'preferred_tasks': ['analysis'], 'max_task_complexity': 'complex',
            'pricing_cny': {'cache_hit_input': '0.1', 'cache_miss_input': '1', 'output': '2'},
        })
        registry.write_json(registry.REGISTRY, data)
        registry.write_json(registry.HERE / 'workflow.json', {
            'enabled': True, 'paid_allowed': True, 'model_scope': 'paid_only',
            'enforce_paid_budget': False,
        })
        cap = self.capability()
        result = brain_gateway.handle('candidates', {}, cap)
        rows = {row['model_id']: row for row in result['models']}
        self.assertFalse(rows['mock']['scope_ok'])
        self.assertTrue(rows['ds']['scope_ok'])
        self.assertTrue(rows['ds']['selectable'])
        self.assertIn('ds', result['selectable_model_ids'])
        self.assertIn('ds', result['in_scope_model_ids'])
        self.assertFalse(result['default_model_scope_ok'])
        self.assertIn('手动默认模型', result['default_model_note'])
        self.assertIn('不按同名或同 provider', result['note'])

        with self.assertRaises(brain_gateway.BrokerError) as caught:
            self.dispatch(cap)
        message = str(caught.exception)
        self.assertIn('手动默认模型', message)
        self.assertIn('ds', message)
        self.assertIn('不会自动更换模型', message)
        self.assertNotIn('没有可用模型', message)
        self.assertEqual(self.submitted, [])

        preview = brain_gateway.handle('route_preview', {
            'model_id': 'ds', 'prompt': 'Preview only', 'kind': 'chat',
        }, cap)
        self.assertEqual(preview['preview']['model_id'], 'ds')
        self.assertEqual(preview['preview']['excluded'], [])
        self.assertEqual(preview['preview']['model_scope']['value'], 'paid_only')

    def test_trusted_dispatch_uses_local_models_saved_pi_agent(self):
        import pi_worker
        data = registry.read_json(registry.REGISTRY)
        data['models'][0]['default_agent'] = 'pi'
        data['models'][0]['capability_notes'] = 'declared fixture capability'
        registry.write_json(registry.REGISTRY, data)
        with mock.patch.object(pi_worker, 'availability', return_value={'ready':True}):
            cap = self.capability()
            models = brain_gateway.handle('candidates', {}, cap)['models']
            self.assertEqual(models[0]['engine'], 'pi')
            self.assertEqual(models[0]['capability_notes'], 'declared fixture capability')
            self.assertEqual(models[0]['identity_state'], 'declared')
            task = self.dispatch(cap)['task']
        self.assertEqual(task['engine'], 'pi')


if __name__ == '__main__':
    unittest.main()
