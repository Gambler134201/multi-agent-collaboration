import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import workbuddy_executor as wb


PERMITTED = {'paid_allowed': True, 'model_scope': 'all',
             'enforce_paid_budget': False, 'enforce_model_call_limit': False}

FAKE_AGENT = r'''
import json, os, sys
from pathlib import Path
scenario = Path('scenario').read_text() if Path('scenario').exists() else 'success'
if scenario == 'banner':
    print('private startup banner: secret-value-must-not-escape', flush=True)
for line in sys.stdin:
    q = json.loads(line)
    with open('requests.jsonl', 'a', encoding='utf-8') as f:
        f.write(json.dumps(q) + '\n')
    method = q['method']
    if method == 'initialize':
        r = {'protocolVersion': 1, 'agentCapabilities': {}}
    elif method == 'session/new':
        r = {'sessionId': 'test-session', 'models': {'availableModels': [{'modelId': 'glm-test', 'name': 'Test'}]}}
    elif method == 'session/set_config_option':
        r = {'configOptions': [{'id': 'model', 'currentValue': 'other' if scenario == 'wrong_model' else 'glm-test'}]}
    elif method == 'session/prompt':
        if scenario == 'auth_required':
            r = {'stopReason': 'refusal', '_meta': {'codebuddy.ai/errorMessage': json.dumps({
                'code': -32000, 'message': 'private-native-detail', 'data': {'category': 'auth'}})}}
            print(json.dumps({'jsonrpc': '2.0', 'id': q['id'], 'result': r}), flush=True)
            continue
        if scenario == 'unknown':
            sys.exit(0)
        if scenario == 'error':
            print(json.dumps({'jsonrpc': '2.0', 'id': q['id'], 'error': {'code': -32000, 'message': 'secret-value-must-not-escape'}}), flush=True)
            continue
        if scenario == 'interaction':
            print(json.dumps({'jsonrpc': '2.0', 'id': 'approve-me', 'method': 'session/request_permission', 'params': {}}), flush=True)
            continue
        print(json.dumps({'jsonrpc': '2.0', 'method': 'session/update', 'params': {'sessionId': 'wrong' if scenario == 'wrong_session' else 'test-session', 'update': {'sessionUpdate': 'agent_message_chunk', 'content': {'type': 'text', 'text': 'OK'}}}}), flush=True)
        r = {'stopReason': 'max_tokens' if scenario == 'truncated' else 'end_turn', 'usage': {'inputTokens': 3, 'outputTokens': 1, 'secret': 'no'}}
    else:
        sys.exit(5)
    print(json.dumps({'jsonrpc': '2.0', 'id': q['id'], 'result': r}), flush=True)
'''


class WorkBuddyExecutorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.script = self.root / 'agent.py'
        self.script.write_text(FAKE_AGENT, encoding='utf-8')

    def client(self, scenario='success'):
        (self.root / 'scenario').write_text(scenario)
        c = wb.NativeChatClient({'executable': sys.executable, 'cli': str(self.script)},
                                self.root, self.root, isolated=True)
        self.addCleanup(c.close)
        c.connect()
        return c

    def requests(self):
        path = self.root / 'requests.jsonl'
        return [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines() if line.strip()]

    def test_missing_installation_is_not_ready(self):
        info = wb.availability(self.root)
        self.assertFalse(info['ready'])
        self.assertFalse(info['installed'])

    def test_explicit_invalid_install_does_not_fall_back(self):
        with patch.dict(os.environ, {wb.HOME_ENV: str(self.root)}):
            with self.assertRaises(ValueError):
                wb.installation()

    def test_only_reviewed_patch_versions_pass_installation_identity(self):
        for name in ['WorkBuddy.exe', 'resources/app.asar',
                     'resources/app.asar.unpacked/cli/bin/codebuddy',
                     'resources/app.asar.unpacked/cli/dist/codebuddy-lite-wb.mjs']:
            p=self.root/name; p.parent.mkdir(parents=True,exist_ok=True); p.write_bytes(b'fixture')
        (self.root/'resources/app.asar.unpacked/cli/product.json').write_text(json.dumps({'productName':'WorkBuddy'}))
        for version in ('5.8.2','5.8.3','5.8.4'):
            with self.subTest(version=version), patch.object(wb,'_asar_package',return_value={
                    'name':'@genie/workbuddy-desktop','version':version}):
                if version in wb.SUPPORTED_VERSIONS:
                    self.assertEqual(wb.installation(self.root)['version'],version)
                else:
                    with self.assertRaises(ValueError):wb.installation(self.root)

    def test_native_command_has_no_tools_mcp_permissions_bypass_or_model_fallback(self):
        args = wb.command({'executable': 'wb.exe', 'cli': 'cli'})
        self.assertIn('--serve', args)
        self.assertIn('--acp', args)
        self.assertEqual(args[args.index('--tools') + 1], '')
        self.assertNotIn('--fallback-model', args)
        self.assertNotIn('--dangerously-skip-permissions', args)
        self.assertNotIn('--auth', args)
        with self.assertRaises(ValueError):
            wb.command({'executable': 'wb.exe', 'cli': 'cli'}, '--help')

    def test_environment_does_not_copy_caller_secrets_or_bridges(self):
        with patch.dict(os.environ, {'ANTHROPIC_API_KEY': 'private', 'CODEX_THREAD_ID': 'private',
                                   'MODEL_DESK_DSH_BRAIN_CONFIG': 'private', 'ACC_PRODUCT_CONFIG_V3': 'private'}):
            env = wb.process_environment(self.root, isolated=True)
        self.assertNotIn('ANTHROPIC_API_KEY', env)
        self.assertNotIn('CODEX_THREAD_ID', env)
        self.assertNotIn('ACC_PRODUCT_CONFIG_V3', env)
        self.assertNotIn('MODEL_DESK_DSH_BRAIN_CONFIG', env)
        self.assertEqual(env['USERPROFILE'], str(self.root))

    def test_parent_product_and_machine_context_is_not_inherited(self):
        with patch.dict(os.environ, {'CLIENT_INFO_PRODUCT_NAME': 'another-product',
                                   'CLIENT_INFO_MACHINE_ID': 'private-device',
                                   'CLIENT_INFO_PLUGIN_NAME': 'another-caller'}):
            env = wb.process_environment(self.root, isolated=True)
        self.assertNotIn('CLIENT_INFO_PRODUCT_NAME', env)
        self.assertNotIn('CLIENT_INFO_MACHINE_ID', env)
        self.assertNotIn('CLIENT_INFO_PLUGIN_NAME', env)

    def test_public_product_identity_keeps_adapter_identity_and_authentication_defaults(self):
        env = wb.process_environment(self.root, isolated=True,
              installation_info={'product_name': 'WorkBuddy', 'version': wb.SUPPORTED_VERSION})
        self.assertEqual(env['CLIENT_INFO_PRODUCT_NAME'], 'WorkBuddy')
        self.assertEqual(env['CLIENT_INFO_PRODUCT_VERSION'], wb.SUPPORTED_VERSION)
        self.assertEqual(env['CLIENT_INFO_PLUGIN_NAME'], 'model-desk-workbuddy-executor')
        self.assertNotIn('CODEBUDDY_HOST', env)
        self.assertNotIn('CLIENT_INFO_MACHINE_ID', env)
        self.assertNotIn('CODEBUDDY_DISABLE_REQUEST_VALIDATION', env)
        self.assertNotIn('CODEBUDDY_SIDECAR_CREDENTIAL_BOOTSTRAP_SOCKET', env)

    def test_spawned_client_receives_public_product_context(self):
        captured = {}
        def spawn(args, **kwargs):
            captured.update(kwargs['env'])
            return subprocess.Popen(args, **kwargs)
        c = wb.NativeChatClient({'executable': sys.executable, 'cli': str(self.script),
              'product_name': 'WorkBuddy', 'version': wb.SUPPORTED_VERSION},
              self.root, self.root, isolated=True, popen=spawn)
        self.addCleanup(c.close)
        c.connect()
        self.assertEqual(captured['CLIENT_INFO_PRODUCT_NAME'], 'WorkBuddy')
        self.assertEqual(captured['CLIENT_INFO_PLUGIN_NAME'], 'model-desk-workbuddy-executor')
        self.assertFalse(c.prompt_sent)

    def test_unknown_product_identity_cannot_start_process(self):
        for info in [{'product_name': 'CodeBuddy', 'version': wb.SUPPORTED_VERSION},
                     {'product_name': 'WorkBuddy', 'version': 'unknown'}]:
            with self.subTest(info=info), self.assertRaises(ValueError):
                wb.NativeChatClient({**info, 'executable': sys.executable, 'cli': str(self.script)},
                                   self.root, self.root, popen=lambda *a, **k: self.fail('spawned'))

    def test_authorization_preserves_local_only(self):
        with self.assertRaises(ValueError):
            wb.authorize_native_chat({**PERMITTED, 'model_scope': 'local_only'})

    def test_permission_and_enforced_limits_refuse_unknown_native_cost(self):
        for changes in [{'paid_allowed': False}, {'enforce_paid_budget': True},
                        {'enforce_model_call_limit': True}, {'enforce_paid_budget': None}]:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                wb.authorize_native_chat({**PERMITTED, **changes})

    def test_paid_only_is_allowed_without_changing_settings(self):
        settings = {**PERMITTED, 'model_scope': 'paid_only'}
        before = dict(settings)
        wb.authorize_native_chat(settings)
        self.assertEqual(settings, before)

    def test_bad_model_catalogue_is_rejected(self):
        for rows in [None, ['model'], [{'modelId': '../bad'}], [{'modelId': 'x'}, {'modelId': 'x'}]]:
            with self.subTest(rows=rows), self.assertRaises(wb.ProtocolError):
                wb.model_catalog({'models': {'availableModels': rows}})

    def test_unknown_project_does_not_start_process(self):
        with self.assertRaises(ValueError):
            wb.NativeChatClient({}, 'relative', self.root, popen=lambda *a, **k: self.fail('spawned'))

    def test_native_product_handover_is_not_copied_into_isolated_probe(self):
        cache = self.root / 'cache/acc-product-config-v3.json'
        cache.parent.mkdir()
        cache.write_text('not JSON; only the native client may parse it')
        self.assertNotIn('ACC_PRODUCT_CONFIG_PATH', wb.process_environment(self.root, isolated=True))
        self.assertEqual(wb.process_environment(self.root)['ACC_PRODUCT_CONFIG_PATH'], str(cache))

    def test_catalogue_excludes_unforwardable_entry_without_replacing_target(self):
        rows = wb.model_catalog({'models': {'availableModels': [{'modelId': 'bad/model'}, {'modelId': 'deepseek-v4.1-flash'}]}})
        self.assertEqual([m['model_id'] for m in rows], ['deepseek-v4.1-flash'])

    def test_startup_banner_is_discarded_not_treated_as_protocol_result(self):
        c = self.client('banner')
        self.assertEqual(c.chat('glm-test', 'test', PERMITTED, timeout=3)['answer'], 'OK')

    def test_failure_meta_classification_does_not_copy_secret_details(self):
        result = {'stopReason': 'refusal', '_meta': {'codebuddy.ai/errorMessage':
            json.dumps({'error': {'code': 401, 'message': 'Unauthorized secret-value'}})}}
        error = wb.prompt_failure(result)
        self.assertEqual(error['category'], 'authentication_required')
        self.assertEqual(error['native_error_code'], 401)
        self.assertNotIn('secret-value', json.dumps(error))

    def test_structured_native_error_preserves_only_whitelisted_diagnostics(self):
        result = {'stopReason': 'refusal', '_meta': {'codebuddy.ai/errorMessage': json.dumps({
            'code': -32000, 'message': 'opaque', 'data': {'category': 'auth', 'code': 1234,
                'statusCode': 401, 'details': 'private-data-never-returned'}})}}
        error = wb.prompt_failure(result)
        self.assertEqual(error['category'], 'authentication_required')
        self.assertEqual(error['business_code'], 1234)
        self.assertEqual(error['http_status'], 401)
        self.assertNotIn('private-data', json.dumps(error))

    def test_unknown_or_private_native_category_is_not_copied(self):
        result = {'stopReason': 'refusal', '_meta': {'codebuddy.ai/errorMessage': json.dumps({
            'code': 'private-code', 'data': {'category': 'private-category', 'code': 'private'}})}}
        error = wb.prompt_failure(result)
        self.assertIsNone(error['native_error_code'])
        self.assertIsNone(error['business_code'])
        self.assertIsNone(error['native_category'])

    def test_auth_required_stays_failed_without_login_retry_or_model_fallback(self):
        c = self.client('auth_required')
        r = c.chat('glm-test', 'test', PERMITTED, timeout=3)
        self.assertEqual(r['status'], 'failed')
        self.assertEqual(r['execution_error']['category'], 'authentication_required')
        self.assertEqual(r['execution_error']['native_error_code'], -32000)
        self.assertFalse(r['accepted'])
        self.assertNotIn('private-native-detail', json.dumps(r))
        with self.assertRaises(ValueError):
            c.chat('glm-test', 'retry', PERMITTED, timeout=3)
        self.assertEqual([x['method'] for x in self.requests()],
                         ['initialize', 'session/new', 'session/set_config_option', 'session/prompt'])

    def test_authenticate_cannot_trigger_native_logout_or_login(self):
        c = self.client()
        before = self.requests()
        with self.assertRaises(ValueError):
            c.request('authenticate', {'methodId': 'external'})
        self.assertEqual(self.requests(), before)

    def test_real_stdio_mock_connect_never_prompts_or_proves_account(self):
        c = self.client()
        self.assertFalse(c.tainted)
        self.assertEqual([x['method'] for x in self.requests()], ['initialize', 'session/new'])

    def test_success_has_real_stop_and_unknown_money_not_free_or_accepted(self):
        c = self.client()
        r = c.chat('glm-test', 'test', PERMITTED, timeout=3)
        self.assertEqual(r['answer'], 'OK')
        self.assertEqual(r['status'], 'completed')
        self.assertEqual(r['usage'], {'inputTokens': 3, 'outputTokens': 1})
        self.assertIsNone(r['cost_cny'])
        self.assertIsNone(r['credits_used'])
        self.assertFalse(r['accepted'])
        self.assertEqual(sum(x['method'] == 'session/prompt' for x in self.requests()), 1)

    def test_unknown_model_is_never_replaced(self):
        c = self.client()
        with self.assertRaises(ValueError):
            c.chat('different', 'test', PERMITTED)
        self.assertEqual(len(self.requests()), 2)

    def test_model_confirmation_mismatch_never_sends_prompt(self):
        c = self.client('wrong_model')
        with self.assertRaises(wb.ProtocolError):
            c.chat('glm-test', 'test', PERMITTED)
        self.assertNotIn('session/prompt', [x['method'] for x in self.requests()])

    def test_local_only_rejects_before_model_selection_or_prompt(self):
        c = self.client()
        with self.assertRaises(ValueError):
            c.chat('glm-test', 'test', {**PERMITTED, 'model_scope': 'local_only'})
        self.assertEqual(len(self.requests()), 2)

    def test_channel_loss_is_unknown_not_completed_and_never_retried(self):
        c = self.client('unknown')
        with self.assertRaises(wb.OutcomeUnknown):
            c.chat('glm-test', 'test', PERMITTED, timeout=3)
        with self.assertRaises(ValueError):
            c.chat('glm-test', 'test', PERMITTED)
        self.assertEqual(sum(x['method'] == 'session/prompt' for x in self.requests()), 1)

    def test_foreign_session_and_interaction_are_not_approved(self):
        for scenario in ['wrong_session', 'interaction']:
            with self.subTest(scenario=scenario):
                c = self.client(scenario)
                with self.assertRaises(wb.OutcomeUnknown):
                    c.chat('glm-test', 'test', PERMITTED, timeout=3)
                c.close()

    def test_rpc_error_does_not_expose_native_error_payload(self):
        c = self.client('error')
        with self.assertRaises(wb.ProtocolError) as caught:
            c.chat('glm-test', 'test', PERMITTED, timeout=3)
        self.assertNotIn('secret-value', str(caught.exception))
        self.assertIn('-32000', str(caught.exception))

    def test_truncation_and_duplicate_chat_are_not_success_or_resend(self):
        c = self.client('truncated')
        self.assertEqual(c.chat('glm-test', 'test', PERMITTED, timeout=3)['status'], 'failed')
        with self.assertRaises(ValueError):
            c.chat('glm-test', 'test', PERMITTED)


if __name__ == '__main__':
    unittest.main()
