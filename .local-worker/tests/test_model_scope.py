"""Global execution-model scope: enum, routing gate, queue guard and migration.

Fully offline.  Every test points ``model_registry.HERE`` at a temporary
directory and uses mocked providers: no real model is called, no real
``models.json``/``workflow.json``/ledger is read or written, and no money or call
limit is changed.  The scope reuses the registry's real ``provider``/``is_paid``
classification; a remotely connected home service with ``provider='local'``
stays local and is never inferred from a URL, a key or a price.
"""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import model_registry as registry
import model_router as router
import model_scope
import panel_server as panel
import workflow_settings as settings
import delegation_policy as policy


LOCAL_MODEL = {'id': 'local', 'name': 'local', 'provider': 'local', 'model': 'qwen',
               'api_base': 'http://127.0.0.1:1234/v1', 'api_key_set': False, 'enabled': True,
               'max_input_tokens': 262144, 'max_output_tokens': 8192,
               'preferred_tasks': ['单文件修改', '信息整理'], 'max_task_complexity': 'simple',
               'cost_preference': 'economy'}

PAID_MODEL = {'id': 'paid', 'name': 'paid', 'provider': 'deepseek',
              'api_base': 'https://api.deepseek.com', 'model': 'deepseek-flash',
              'enabled': True, 'api_key_set': True, 'max_input_tokens': 1000000,
              'max_output_tokens': 64000,
              'preferred_tasks': ['单文件修改', '多文件修改', '复杂逻辑分析', '疑难问题排查'],
              'max_task_complexity': 'complex', 'cost_preference': 'balanced',
              'billing': {'balance': {'status': 'ready', 'is_available': True,
                                      'balance_infos': [{'currency': 'CNY', 'total_balance': '10'}]}}}


def routing_body(**overrides):
    body = {'kind': 'edit', 'engine': 'dsh', 'route_mode': 'auto', 'prompt': 'task', 'files': ['a.py'],
            'routing': {'category': 'code_small', 'complexity': 'simple', 'input_tokens': 8192,
                        'output_tokens': 4096, 'expected_calls': 1}}
    body.update(overrides)
    return body


def manual_body(model_id, **overrides):
    body = routing_body(route_mode='manual', model_id=model_id)
    body.update(overrides)
    return body


class ScopeBase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.saved = registry.HERE, registry.REGISTRY, registry.RUNS
        registry.HERE = Path(self.temp.name)
        registry.REGISTRY = registry.HERE / 'models.json'
        registry.RUNS = registry.HERE / 'runs'
        self.enabled = {**settings.DEFAULTS, 'enabled': True, 'paid_allowed': True}

    def tearDown(self):
        registry.HERE, registry.REGISTRY, registry.RUNS = self.saved
        self.temp.cleanup()

    def choose(self, body, scope, models=None, **kwargs):
        values = {**self.enabled, 'model_scope': scope}
        return router.choose(body, models if models is not None else [LOCAL_MODEL, PAID_MODEL],
                             values, **kwargs)


class ScopeEnumTests(ScopeBase):
    def test_default_and_labels_are_the_three_agreed_modes(self):
        self.assertEqual(model_scope.DEFAULT_SCOPE, 'all')
        self.assertEqual(model_scope.SCOPES, ('all', 'paid_only', 'local_only'))
        self.assertEqual(settings.DEFAULTS['model_scope'], 'all')
        self.assertIn('model_scope', settings.FIELDS)

    def test_normalize_refuses_illegal_enums_and_types(self):
        for value in ('paid', 'paid-only', 'all_models', 1, True, ['all'], ''):
            with self.assertRaises(ValueError, msg=repr(value)):
                model_scope.normalize(value)
        self.assertEqual(model_scope.normalize(' Paid_Only '), 'paid_only')
        self.assertEqual(model_scope.normalize('LOCAL_ONLY'), 'local_only')

    def test_effective_maps_missing_or_unknown_values_to_all(self):
        for value in (None, '', 'nonsense', 3):
            self.assertEqual(model_scope.effective(value), 'all')
        self.assertEqual(model_scope.effective('local_only'), 'local_only')

    def test_authoritative_gate_rejects_null_and_invalid_scope(self):
        for value in (None, '', 'unknown', True, ['all']):
            for paid in (False, True):
                with self.assertRaises(ValueError, msg=repr(value)):
                    model_scope.check(value, paid)

    def test_classification_reuses_provider_semantics_not_url_or_key(self):
        remote_home = {'id': 'home', 'provider': 'local', 'api_base': 'http://home.example:1234/v1',
                       'api_key_set': True}
        unknown_paid = {'id': 'x', 'provider': 'glm', 'api_base': 'http://127.0.0.1:9/v1',
                        'api_key_set': False}
        self.assertFalse(router.is_paid(remote_home))
        self.assertTrue(router.is_paid(unknown_paid))
        self.assertTrue(model_scope.matches('local_only', router.is_paid(remote_home)))
        self.assertFalse(model_scope.matches('paid_only', router.is_paid(remote_home)))

    def test_describe_payload_carries_label_and_note_without_secrets(self):
        view = model_scope.describe('local_only')
        self.assertEqual(view['value'], 'local_only')
        self.assertEqual(view['label'], '仅本地/自建模型')
        self.assertIn('本地', view['note'])
        self.assertNotIn('key', json.dumps(view, ensure_ascii=False).lower())


class ScopeRoutingTests(ScopeBase):
    def test_all_keeps_the_original_routing_behaviour(self):
        route = self.choose(routing_body(), 'all')
        self.assertEqual(route['model_id'], 'local')
        self.assertEqual(route['model_scope']['value'], 'all')
        self.assertTrue(route['model_scope']['is_default'])
        self.assertEqual(route['model_scope']['candidate'], 'local')

    def test_paid_only_filters_an_automatic_local_candidate(self):
        route = self.choose(routing_body(), 'paid_only')
        self.assertEqual(route['model_id'], 'paid')
        self.assertEqual(route['model_scope']['candidate'], 'paid')
        self.assertIn('执行模型范围', route['reason'])
        excluded = {row['model_id']: row['reason'] for row in route['excluded']}
        self.assertIn('本地/自建', excluded['local'])

    def test_local_only_never_selects_a_paid_candidate(self):
        body = routing_body(routing={'category': 'code_multi', 'complexity': 'complex',
                                     'input_tokens': 8192, 'output_tokens': 4096, 'expected_calls': 1})
        with self.assertRaisesRegex(ValueError, '本地/自建'):
            self.choose(body, 'local_only')
        route = self.choose(routing_body(), 'local_only')
        self.assertEqual(route['model_id'], 'local')

    def test_manual_out_of_scope_model_is_refused_not_replaced(self):
        for scope, model_id in (('local_only', 'paid'), ('paid_only', 'local')):
            with self.assertRaises(ValueError) as error:
                self.choose(manual_body(model_id), scope)
            message = str(error.exception)
            self.assertIn('执行模型范围', message)
            self.assertIn(model_id, message)
            # The refusal is the scope reason; no substitute model is offered.
            self.assertIn('已排除', message)
            self.assertNotIn('/api/', message)

    def test_manual_model_inside_the_scope_still_routes(self):
        self.assertEqual(self.choose(manual_body('paid'), 'paid_only')['model_id'], 'paid')
        self.assertEqual(self.choose(manual_body('local'), 'local_only')['model_id'], 'local')

    def test_no_candidate_inside_the_scope_gives_a_concrete_reason(self):
        for scope, models in (('local_only', [PAID_MODEL]), ('paid_only', [LOCAL_MODEL])):
            with self.assertRaises(ValueError) as error:
                self.choose(routing_body(), scope, models=models)
            message = str(error.exception)
            self.assertIn('需要主脑重新拆分任务或人工调整', message)
            self.assertIn(models[0]['id'], message)
            self.assertIn('执行模型范围', message)

    def test_illegal_stored_scope_refuses_instead_of_widening(self):
        for value in ('paid', '', 2, True):
            with self.assertRaisesRegex(ValueError, '执行模型范围'):
                self.choose(routing_body(), value)

    def test_paid_still_needs_paid_authorization_inside_paid_only(self):
        body = manual_body('paid')
        values = {**self.enabled, 'model_scope': 'paid_only', 'paid_allowed': False}
        with self.assertRaisesRegex(ValueError, '付费执行未开启'):
            router.choose(body, [LOCAL_MODEL, PAID_MODEL], values)
        allowed = {**values, 'paid_allowed': True}
        self.assertEqual(router.choose(body, [LOCAL_MODEL, PAID_MODEL], allowed)['model_id'], 'paid')

    def test_scope_does_not_weaken_capacity_or_complexity_gates(self):
        # A local candidate inside the scope still fails the declared minimum.
        with self.assertRaises(ValueError):
            self.choose(manual_body('local', routing={'category': 'code_small', 'complexity': 'simple',
                                                      'min_output_tokens': 9000, 'expected_calls': 1}),
                        'local_only')
        # Under local_only the complexity gate still blocks the only local
        # candidate; the paid model is excluded by the scope, and the refusal
        # keeps the capability reason instead of silently widening the scope.
        body = routing_body(routing={'category': 'code_multi', 'complexity': 'complex',
                                     'input_tokens': 8192, 'output_tokens': 4096, 'expected_calls': 1})
        with self.assertRaises(ValueError) as error:
            self.choose(body, 'local_only')
        self.assertIn('复杂度', str(error.exception))
        self.assertIn('执行模型范围', str(error.exception))
        route = self.choose(routing_body(routing={'category': 'code_multi', 'complexity': 'complex',
                                                 'input_tokens': 8192, 'output_tokens': 4096,
                                                 'expected_calls': 1}), 'paid_only')
        self.assertEqual(route['model_id'], 'paid')
        # The complexity gate still excludes a capable local model for that category,
        # even though the paid model is then selected inside the scope.
        self.assertTrue(any(row['model_id'] == 'local' for row in route['excluded']))


class SettingsMigrationTests(ScopeBase):
    def seeded(self):
        settings.save_settings({'enabled': True, 'paid_allowed': True, 'enforce_paid_budget': False,
                                'enforce_model_call_limit': False, 'task_budget_cny': 5, 'daily_budget_cny': 20,
                                'max_model_calls': 30})
        return settings.snapshot()

    def test_legacy_file_without_the_field_migrates_to_all(self):
        before = self.seeded()
        path = registry.HERE / 'workflow.json'
        raw = json.loads(path.read_text(encoding='utf-8'))
        del raw['model_scope']
        registry.write_json(path, raw)
        after = settings.snapshot()
        self.assertTrue(after['readable'])
        self.assertEqual(after['model_scope'], 'all')
        self.assertEqual({k: v for k, v in after.items() if k != 'model_scope'},
                         {k: v for k, v in before.items() if k != 'model_scope'})

    def test_saving_only_the_scope_keeps_every_other_setting(self):
        before = self.seeded()
        after = settings.save_settings({'model_scope': 'local_only'})
        self.assertEqual(after['model_scope'], 'local_only')
        self.assertEqual({k: v for k, v in after.items() if k != 'model_scope'},
                         {k: v for k, v in before.items() if k != 'model_scope'})
        self.assertFalse(after['enforce_paid_budget'])
        self.assertFalse(after['enforce_model_call_limit'])
        self.assertEqual(after['task_budget_cny'], 5.0)
        self.assertEqual(after['max_model_calls'], 30)
        self.assertEqual(settings.snapshot()['model_scope'], 'local_only')

    def test_illegal_scope_values_are_refused_and_keep_the_file(self):
        self.seeded()
        for body in ({'model_scope': 'paid'}, {'model_scope': 1}, {'model_scope': True},
                     {'model_scope': None}, {'model_scope': ['all']}):
            with self.assertRaises(ValueError):
                settings.save_settings(body)
        self.assertEqual(settings.snapshot()['model_scope'], 'all')
        self.assertTrue(settings.snapshot()['paid_allowed'])

    def test_a_corrupt_stored_scope_fails_closed_and_is_reported(self):
        self.seeded()
        registry.write_json(registry.HERE / 'workflow.json',
                            {**json.loads((registry.HERE / 'workflow.json').read_text(encoding='utf-8')),
                             'model_scope': 'paid'})
        state = settings.snapshot()
        self.assertFalse(state['readable'])
        self.assertFalse(state['enabled'])
        self.assertFalse(state['paid_allowed'])
        self.assertIn('model_scope', state['error'])
        self.assertEqual(state['model_scope'], 'paid')
        with self.assertRaisesRegex(ValueError, '执行模型范围设置无效'):
            router.choose(manual_body('local'), [LOCAL_MODEL, PAID_MODEL], state)

    def test_unknown_fields_are_still_refused(self):
        with self.assertRaisesRegex(ValueError, '不支持的设置字段'):
            settings.save_settings({'model_scope_v2': 'all'})

    def test_paid_only_does_not_turn_on_paid_authorization(self):
        settings.save_settings({'model_scope': 'paid_only'})
        state = settings.snapshot()
        self.assertFalse(state['paid_allowed'])
        with self.assertRaisesRegex(ValueError, '付费执行未开启'):
            router.choose(manual_body('paid'), [LOCAL_MODEL, PAID_MODEL], {**state, 'readable': True})


class QueuedStartGuardTests(ScopeBase):
    def setUp(self):
        super().setUp()
        self.project = registry.HERE / 'project'
        self.project.mkdir()
        (self.project / 'a.py').write_text('def one():\n    return 1\n', encoding='utf-8')
        registry.write_json(registry.HERE / 'settings.json', {
            'model': 'qwen', 'api_base': 'http://127.0.0.1:1234/v1', 'api_key_env': '',
            'max_input_tokens': 32768, 'max_output_tokens': 4096})
        registry.list_models()
        # Register both classes directly in the temporary registry: one local
        # candidate and one paid candidate, without touching the real file.
        data = json.loads(registry.REGISTRY.read_text(encoding='utf-8'))
        data['default_model'] = 'local'
        data['models'] = [dict(LOCAL_MODEL), dict(PAID_MODEL)]
        registry.write_json(registry.REGISTRY, data)

    def queued(self, name='queued-local', model_id='local', engine='direct'):
        run = registry.RUNS / name
        run.mkdir(parents=True, exist_ok=True)
        (run / 'prompt.txt').write_text('只读任务', encoding='utf-8')
        registry.patch_task(run, id=name, status='queued', kind='chat', engine=engine,
                            model_id=model_id, model='qwen', project='', files=[],
                            route={'mode': 'manual', 'budget_cny': 0})
        return run

    def test_a_queued_local_task_gate_passes_when_it_is_in_scope(self):
        self.queued()
        model = registry.get_model('local')
        self.assertEqual(panel.scope_gate(model, {'model_scope': 'local_only'})['scope'], 'local_only')
        self.assertEqual(panel.scope_gate(model, {'model_scope': 'all'})['scope'], 'all')

    def test_a_queued_paid_task_is_refused_before_any_launch(self):
        run = self.queued('queued-paid', model_id='paid')
        settings.save_settings({'enabled': True, 'paid_allowed': True, 'model_scope': 'local_only'})
        with patch.object(panel.subprocess, 'Popen') as process:
            panel.execute_task('queued-paid')
        process.assert_not_called()
        state = json.loads((run / 'task.json').read_text(encoding='utf-8'))
        self.assertEqual(state['status'], 'failed')
        self.assertIn('执行模型范围', state['error'])
        self.assertIn('paid', state['error'])
        self.assertNotIn('completed', state['status'])

    def test_the_queued_guard_reads_the_latest_scope_not_the_one_at_queue_time(self):
        run = self.queued('queued-paid-2', model_id='paid')
        settings.save_settings({'enabled': True, 'paid_allowed': True,
                                'enforce_paid_budget': False, 'enforce_model_call_limit': False,
                                'model_scope': 'all'})
        settings.save_settings({'model_scope': 'local_only'})
        self.assertEqual(settings.snapshot()['model_scope'], 'local_only')
        with patch.object(panel.subprocess, 'Popen') as process:
            panel.execute_task('queued-paid-2')
        process.assert_not_called()
        self.assertEqual(json.loads((run / 'task.json').read_text(encoding='utf-8'))['status'], 'failed')

    def test_an_in_scope_queued_task_still_starts_and_is_not_re_routed(self):
        run = self.queued('queued-ok')
        settings.save_settings({'enabled': True, 'paid_allowed': True, 'model_scope': 'local_only'})
        with patch.object(panel.subprocess, 'Popen') as process:
            process.return_value.pid = 4242
            process.return_value.wait.return_value = 0
            panel.execute_task('queued-ok')
        process.assert_called_once()
        self.assertEqual(registry.read_task(run)['model_id'], 'local')

    def test_a_cancelled_task_is_not_turned_into_a_scope_failure(self):
        run = self.queued('queued-cancelled')
        (run / 'cancelled.flag').touch()
        with patch.object(panel.subprocess, 'Popen') as process:
            panel.execute_task('queued-cancelled')
        process.assert_not_called()
        state = json.loads((run / 'task.json').read_text(encoding='utf-8'))
        self.assertEqual(state['status'], 'queued')
        self.assertNotIn('error', state)

    def test_recovery_refuses_original_model_outside_latest_scope_without_writes(self):
        run = self.queued('recover-paid', model_id='paid')
        registry.patch_task(run, status='recovery_pending')
        before = (run / 'task.json').read_bytes()
        settings.save_settings({'enabled': True, 'paid_allowed': True, 'model_scope': 'local_only'})
        with patch.object(panel.EXECUTOR, 'submit') as queued:
            with self.assertRaisesRegex(ValueError, '执行模型范围'):
                panel.recover_task('recover-paid', {'confirm': True})
        queued.assert_not_called()
        self.assertEqual((run / 'task.json').read_bytes(), before)

    def test_finally_accepted_task_cannot_recover_its_interrupted_executor(self):
        run = self.queued('main-takeover')
        registry.patch_task(run, status='recovery_pending', delegation={'schema': 1, 'mode': 'outcome'})
        record = policy.initialize(run, ['independently checked'])
        policy.block(run, 'main', 'executor interrupted; main takeover')
        criteria = [{'id': c['id'], 'status': 'pass', 'evidence': [
            {'source': 'reviewer_test', 'detail': 'isolated acceptance fixture', 'exit_code': 0}]
        } for c in record['criteria']]
        policy.final_accept(run, 'main', criteria, 'independent main takeover fixture')
        before = (run / 'task.json').read_bytes()
        with patch.object(panel.EXECUTOR, 'submit') as queued:
            with self.assertRaisesRegex(ValueError, '已通过独立验收'):
                panel.recover_task('main-takeover', {'confirm': True})
        queued.assert_not_called()
        self.assertEqual((run / 'task.json').read_bytes(), before)
        self.assertEqual(registry.read_task(run)['status'], 'recovery_pending')

    def test_direct_aider_cli_keeps_scope_and_paid_guards_before_any_process(self):
        import run_worker
        model = {**PAID_MODEL, 'api_key': 'mock-only'}
        argv = ['run_worker.py', '--project', str(self.project), '--files', 'a.py', '--task', 'fixture']
        for scope, allowed, reason in [('local_only', True, '执行模型范围'),
                                        ('paid_only', False, '付费执行未开启'),
                                        ('paid_only', True, '付费任务请使用 DSH')]:
            with self.subTest(scope=scope, allowed=allowed):
                settings.save_settings({'enabled': True, 'paid_allowed': allowed, 'model_scope': scope})
                with patch.object(sys, 'argv', argv), patch.object(run_worker, 'get_model', return_value=model), \
                     patch.object(run_worker.subprocess, 'Popen') as process:
                    with self.assertRaisesRegex(ValueError, reason):
                        run_worker.main()
                process.assert_not_called()


class WorkerEntryGuardTests(ScopeBase):
    """The scope is enforced at the real worker entry, not only in the panel UI."""

    def worker_model(self, paid):
        model = dict(PAID_MODEL if paid else LOCAL_MODEL)
        model['id'] = 'candidate'
        model['name'] = 'candidate'
        model['thinking_level'] = 'default'
        model['api_key'] = 'not-a-real-key'
        return model

    def thinking_task(self, run, model, name):
        (run / 'prompt.txt').write_text('只读任务', encoding='utf-8')
        registry.patch_task(run, id=name, status='queued', kind='chat', engine='dsh',
                            model_id=model['id'], model=model['model'], project='', files=[],
                            timeout=0, route={'mode': 'manual', 'budget_cny': 0})
        return registry.read_task(run)

    def test_dsh_worker_refuses_a_paid_candidate_under_local_only(self):
        import dsh_worker
        settings.save_settings({'enabled': True, 'paid_allowed': True, 'model_scope': 'local_only'})
        run = registry.RUNS / 'worker-paid'
        run.mkdir(parents=True, exist_ok=True)
        model = self.worker_model(paid=True)
        state = self.thinking_task(run, model, 'worker-paid')
        with self.assertRaisesRegex(ValueError, '执行模型范围') as error:
            dsh_worker.run_task(run, state, model)
        self.assertIn('付费', str(error.exception))

    def test_dsh_worker_refuses_a_local_candidate_under_paid_only(self):
        import dsh_worker
        settings.save_settings({'enabled': True, 'paid_allowed': True, 'model_scope': 'paid_only'})
        run = registry.RUNS / 'worker-local'
        run.mkdir(parents=True, exist_ok=True)
        model = self.worker_model(paid=False)
        state = self.thinking_task(run, model, 'worker-local')
        with self.assertRaisesRegex(ValueError, '执行模型范围') as error:
            dsh_worker.run_task(run, state, model)
        self.assertIn('本地/自建', str(error.exception))

    def test_chat_path_refuses_before_any_provider_request(self):
        settings.save_settings({'enabled': True, 'paid_allowed': True, 'model_scope': 'local_only'})
        run = registry.RUNS / 'chat-paid'
        run.mkdir(parents=True, exist_ok=True)
        model = self.worker_model(paid=True)
        state = self.thinking_task(run, model, 'chat-paid')
        before = registry.read_task(run)
        with patch.object(panel, 'remote_request') as remote:
            with self.assertRaisesRegex(ValueError, '执行模型范围'):
                panel.run_chat(run, state, model)
        remote.assert_not_called()
        after = registry.read_task(run)
        # Refused before any status/phase write: the original record is untouched.
        self.assertEqual((after['status'], after.get('phase')), (before['status'], before.get('phase')))
        self.assertNotIn('usage', after)

    def test_direct_chat_cannot_bypass_paid_accounting_with_paid_only_scope(self):
        settings.save_settings({'enabled': True, 'paid_allowed': True, 'model_scope': 'paid_only'})
        run = registry.RUNS / 'direct-paid'
        run.mkdir(parents=True, exist_ok=True)
        model = self.worker_model(paid=True)
        state = self.thinking_task(run, model, 'direct-paid')
        with patch.object(panel, 'remote_request') as remote:
            with self.assertRaisesRegex(ValueError, '付费任务请使用 DSH'):
                panel.run_chat(run, state, model)
        remote.assert_not_called()

    def test_all_scope_keeps_the_worker_behaviour_unchanged(self):
        settings.save_settings({'enabled': True, 'paid_allowed': True, 'model_scope': 'all'})
        run = registry.RUNS / 'worker-all'
        run.mkdir(parents=True, exist_ok=True)
        model = self.worker_model(paid=True)
        state = self.thinking_task(run, model, 'worker-all')
        self.assertTrue(model_scope.matches(settings.snapshot()['model_scope'], router.is_paid(model)))
        # The reasoning gate is unchanged: an unauthorized paid run is still
        # refused by the existing paid check, not by the scope.
        settings.save_settings({'paid_allowed': False})
        import dsh_worker
        with self.assertRaisesRegex(ValueError, '付费执行未开启'):
            dsh_worker.run_task(run, state, model)

    def test_changing_scope_during_a_running_stream_does_not_interrupt_or_switch_it(self):
        settings.save_settings({'enabled': True, 'model_scope': 'all'})
        run = registry.RUNS / 'running-stream'
        run.mkdir(parents=True, exist_ok=True)
        model = self.worker_model(paid=False)
        state = self.thinking_task(run, model, 'running-stream')

        class Stream:
            headers = {'Content-Type': 'text/event-stream'}
            def __enter__(self): return self
            def __exit__(self, *_): pass
            def __iter__(self):
                yield b'data: {"choices":[{"delta":{"content":"before"}}]}\n'
                settings.save_settings({'model_scope': 'paid_only'})
                yield b'data: {"choices":[{"delta":{"content":"after"}}]}\n'
                yield b'data: [DONE]\n'

        with patch.object(panel, 'remote_request', return_value=Stream()) as remote:
            panel.run_chat(run, state, model)
        remote.assert_called_once()
        after = registry.read_task(run)
        self.assertEqual(after['status'], 'completed')
        self.assertEqual(after['answer'], 'beforeafter')
        self.assertEqual(after['model_id'], model['id'])
        self.assertEqual(settings.snapshot()['model_scope'], 'paid_only')

    def test_manual_worker_refuses_corrupt_null_scope_before_provider_start(self):
        import dsh_worker
        settings.save_settings({'enabled': True, 'model_scope': 'all'})
        path = registry.HERE / 'workflow.json'
        registry.write_json(path, {**json.loads(path.read_text(encoding='utf-8')), 'model_scope': None})
        run = registry.RUNS / 'null-scope'
        run.mkdir(parents=True, exist_ok=True)
        model = self.worker_model(paid=False)
        state = self.thinking_task(run, model, 'null-scope')
        with self.assertRaisesRegex(ValueError, '执行模型范围不能为空'):
            dsh_worker.run_task(run, state, model)


if __name__ == '__main__':
    unittest.main(verbosity=2)
