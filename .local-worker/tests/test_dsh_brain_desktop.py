"""Desktop installation tests: isolated profiles, fake User environment and CLI."""
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import dsh_brain_desktop as setup


class Environment:
    def __init__(self):
        self.value = None
        self.writes = []

    def get(self):
        return self.value

    def set(self, value):
        self.writes.append(value)
        self.value = value


class DesktopTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.root = self.base / 'workbench'
        (self.root / '.local-worker').mkdir(parents=True)
        (self.root / '打开模型工作台.ps1').write_bytes(b'# fixture native opener\n')
        (self.root / '启动模型面板.ps1').write_bytes(b'# fixture native starter\n')
        for name in ('brain_mcp_server.py', 'brain_context.py', 'brain_transport.py', 'brain_gateway.py', 'workflow_settings.py', 'host_balance.py',
                     'completion_wait.py','desktop_notify.py','completion_host.py','dsh_notify.py','panel_service.py'):
            (self.root / '.local-worker' / name).write_bytes(b'# temporary fixture\n')
        self.home = self.base / 'dsh-home'
        self.profile = self.home / 'profiles' / 'desktop'
        self.profile.mkdir(parents=True)
        self.manifest = {'name': 'dsh-profile-desktop', 'private': True,
                         'dependencies': {'user-plugin': '1.0.0'},
                         'dsh': {'profile': {'bundles': ['@deepseek-ai/dsh-base', '@deepseek-ai/dsh-web-app', 'user-plugin']}}}
        self.write_manifest(self.manifest)
        (self.profile / 'cordis.patch.yml').write_bytes(b'# user patch must remain untouched\n')
        self.executable = self.base / 'DSH.exe'
        self.executable.write_bytes(b'fixture only, never launched')
        self.folder = self.base / 'prepared'
        self.environment = Environment()
        self.commands = []
        self.addCleanup(mock.patch.stopall)
        self.probe = mock.patch.object(setup, 'probe_dsh', return_value={'version': setup.SUPPORTED_DSH, 'cli': 'fake-native-cli.js'}).start()
        self.controller = mock.patch.object(setup, 'controller_check', return_value={'brain_interface': True, 'task_mutation_authority': False}).start()

    def write_manifest(self, value):
        (self.profile / 'package.json').write_bytes(setup.json_bytes(value))

    def read_manifest(self):
        return setup.read_json(self.profile / 'package.json')

    def prepare(self, panel_url=None):
        arguments = [str(self.folder), str(self.root), sys.executable,
                     str(self.executable), str(self.home)]
        if panel_url is not None:
            return setup.prepare(*arguments, panel_url=panel_url)
        return setup.prepare(*arguments)

    def runner(self, plan, arguments):
        self.commands.append(copy.deepcopy(arguments))
        manifest = self.read_manifest()
        if arguments[0] == 'add':
            manifest.setdefault('dependencies', {})[setup.PACKAGE] = 'file:' + arguments[1]
            manifest['dsh']['profile']['bundles'].append(setup.PACKAGE)
        elif arguments[0] == 'remove':
            manifest['dependencies'].pop(setup.PACKAGE)
            manifest['dsh']['profile']['bundles'].remove(setup.PACKAGE)
        else:
            raise AssertionError('unexpected CLI')
        self.write_manifest(manifest)

    def install(self, runner=None):
        return setup.install(self.folder, environment=self.environment, runner=runner or self.runner)

    def uninstall(self, runner=None):
        return setup.uninstall(self.folder, environment=self.environment, runner=runner or self.runner)

    def test_prepare_only_fixed_source_package_and_explicit_output(self):
        before = (self.profile / 'package.json').read_bytes()
        result = self.prepare()
        self.assertFalse(result['installed'])
        self.assertEqual(before, (self.profile / 'package.json').read_bytes())
        self.assertEqual({item.name for item in self.folder.iterdir()}, {'plugin.tgz', 'config.json', 'plan.json'})
        with tarfile.open(self.folder / 'plugin.tgz') as archive:
            self.assertEqual(set(archive.getnames()), {'package/' + name for name in setup.FILES})
            self.assertTrue(all(item.isfile() for item in archive.getmembers()))
            self.assertNotIn(str(self.base).encode(), archive.extractfile('package/index.cjs').read())
            self.assertNotIn(str(self.base).encode(), archive.extractfile('package/client.mjs').read())
        self.controller.assert_not_called()
        self.assertEqual(self.environment.writes, [])

    def test_prepared_client_half_declares_native_sidebar_and_embeds_panel_origin(self):
        self.prepare(panel_url='http://localhost:9123')
        with tarfile.open(self.folder / 'plugin.tgz') as archive:
            manifest = json.loads(archive.extractfile('package/package.json').read())
            client = archive.extractfile('package/client.mjs').read()
        self.assertEqual(manifest['version'], '0.2.8')
        self.assertEqual(set(manifest['files']), set(setup.FILES) - {'package.json'})
        self.assertEqual(manifest['exports']['./client'], './client.mjs')
        self.assertEqual(manifest['exports']['./account'], './account.cjs')
        self.assertIn('client.mjs', manifest['files'])
        self.assertIn('account.cjs', manifest['files'])
        self.assertEqual(manifest['dsh']['client']['platform'], 'web')
        self.assertEqual(manifest['dsh']['client']['immediately'], True)
        self.assertIn('@deepseek-ai/dsh-client-ui-sidebar-right', manifest['dsh']['client']['inject'])
        self.assertIn(b'"http://localhost:9123"', client)
        self.assertNotIn(setup.CLIENT_ORIGIN_MARKER, client)
        self.assertIn('模型工作台'.encode(), client)
        self.assertNotIn(b'brain_connect', client)
        self.assertNotIn(b'postMessage', client)
        self.assertNotIn(b'localStorage', client)
        # Preparing a byte copy must not rewrite the tracked source package.
        self.assertIn(setup.CLIENT_ORIGIN_MARKER,
                      (setup.BUNDLE / 'client.mjs').read_bytes())
        config = setup.read_json(self.folder / 'config.json')
        self.assertEqual(config['panelUrl'], 'http://localhost:9123')

    def test_client_origin_template_rejects_missing_or_duplicated_marker(self):
        with self.assertRaises(setup.SetupError):
            setup.client_bundle(b'const x = 1;\n', 'http://127.0.0.1:8765')
        with self.assertRaises(setup.SetupError):
            setup.client_bundle(setup.CLIENT_ORIGIN_MARKER * 2, 'http://127.0.0.1:8765')
        rendered = setup.client_bundle(b'const x = ' + setup.CLIENT_ORIGIN_MARKER + b';\n',
                                       'http://[::1]:8765')
        self.assertEqual(rendered, b'const x = "http://[::1]:8765";\n')

    def test_prepare_refuses_existing_or_record_directory(self):
        self.prepare()
        with self.assertRaises(setup.SetupError):
            self.prepare()
        records = self.base / '.model-desk'
        records.mkdir()
        with self.assertRaises(setup.SetupError):
            setup.prepare(str(records / 'new'), str(self.root), sys.executable, str(self.executable), str(self.home))
        self.assertEqual(list(records.iterdir()), [])

    def test_install_is_explicit_and_repeated_success_does_not_run_cli(self):
        self.prepare()
        patch = (self.profile / 'cordis.patch.yml').read_bytes()
        self.assertFalse(self.install()['reused'])
        self.assertTrue(self.install()['reused'])
        self.assertEqual(len(self.commands), 1)
        self.assertEqual(self.environment.value, str(self.folder / 'config.json'))
        self.assertEqual(setup.without_ours(self.read_manifest()), setup.without_ours(self.manifest))
        self.assertEqual((self.profile / 'cordis.patch.yml').read_bytes(), patch)

    def test_missing_brain_interface_refuses_before_profile_and_environment_effects(self):
        self.prepare()
        self.controller.side_effect = setup.SetupError('controller unavailable')
        with self.assertRaises(setup.SetupError):
            self.install()
        self.assertEqual(self.read_manifest(), self.manifest)
        self.assertEqual(self.environment.writes, [])
        self.assertFalse((self.folder / 'receipt.json').exists())
        self.assertEqual(self.commands, [])

    def test_artifact_or_installation_drift_refuses(self):
        self.prepare()
        (self.folder / 'plugin.tgz').write_bytes(b'changed')
        with self.assertRaises(setup.SetupError):
            self.install()
        self.assertEqual(self.commands, [])

    def test_target_code_drift_refuses(self):
        self.prepare()
        (self.root / '.local-worker' / 'workflow_settings.py').write_bytes(b'changed')
        with self.assertRaises(setup.SetupError):
            self.install()
        self.assertEqual(self.environment.writes, [])

    def test_existing_foreign_plugin_or_environment_is_not_adopted(self):
        self.prepare()
        self.environment.value = 'another-user-config'
        with self.assertRaises(setup.SetupError):
            self.install()
        self.assertEqual(self.environment.value, 'another-user-config')
        self.environment.value = None
        self.manifest['dependencies'][setup.PACKAGE] = '^9.0.0'
        self.write_manifest(self.manifest)
        with self.assertRaises(setup.SetupError):
            self.install()
        self.assertEqual(self.commands, [])

    def test_cli_timeout_records_unknown_and_does_not_repeat_or_blindly_undo(self):
        self.prepare()
        def uncertain(plan, args):
            self.runner(plan, args)
            raise subprocess.TimeoutExpired('fake CLI', 1)
        with self.assertRaises(setup.SetupError):
            self.install(uncertain)
        self.assertEqual(setup.receipt_for(self.folder)['state'], 'attention')
        self.assertTrue(setup.receipt_for(self.folder)['effect_unknown'])
        with self.assertRaises(setup.SetupError):
            self.install()
        with self.assertRaises(setup.SetupError):
            self.uninstall()
        self.assertEqual(len(self.commands), 1)
        self.assertEqual(self.environment.writes, [])

    def test_uninstall_only_ours_and_preserves_later_user_plugin(self):
        self.prepare()
        self.install()
        manifest = self.read_manifest()
        manifest['dependencies']['later-plugin'] = '2.0.0'
        manifest['dsh']['profile']['bundles'].append('later-plugin')
        self.write_manifest(manifest)
        self.uninstall()
        self.assertEqual(self.read_manifest()['dependencies']['later-plugin'], '2.0.0')
        self.assertEqual(self.read_manifest()['dependencies']['user-plugin'], '1.0.0')
        self.assertFalse(setup.has_ours(self.read_manifest()))
        self.assertIsNone(self.environment.value)
        self.assertTrue(self.uninstall()['reused'])
        self.assertEqual(len(self.commands), 2)

    def failed_without_package(self):
        self.prepare()
        def fail(plan, arguments):
            self.commands.append(arguments)
            raise setup.PackageCommandFailed(1, 'ERR_PNPM_NO_OFFLINE_META')
        with self.assertRaises(setup.SetupError):
            self.install(fail)

    def reconcile(self, stopped=None):
        return setup.reconcile_absent(self.folder, environment=self.environment,
                                      stopped=stopped or (lambda plan: None))

    def test_reconcile_preserves_failure_and_never_invokes_cli(self):
        self.failed_without_package()
        original = setup.receipt_for(self.folder)
        result = self.reconcile()
        self.assertFalse(result['retry_automatic'])
        record = setup.receipt_for(self.folder)
        self.assertEqual(record['attempt_history'], [original])
        self.assertEqual(record['state'], 'verified_absent')
        self.assertEqual(len(self.commands), 1)
        with self.assertRaises(setup.SetupError):
            self.install()
        setup.install(self.folder, environment=self.environment, runner=self.runner,
                      retry_verified=True, stopped=lambda plan: None)
        self.assertEqual(setup.receipt_for(self.folder)['attempt_history'], [original])
        self.assertEqual(len(self.commands), 2)

    def test_reconciliation_refuses_host_still_running_without_receipt_change(self):
        self.failed_without_package()
        original = (self.folder / 'receipt.json').read_bytes()
        def running(plan):
            raise setup.SetupError('host running')
        with self.assertRaises(setup.SetupError):
            self.reconcile(running)
        self.assertEqual((self.folder / 'receipt.json').read_bytes(), original)

    def test_reconciliation_refuses_native_marker_package_link_env_and_metadata_drift(self):
        self.failed_without_package()
        marker = self.profile / '.plugin-manager' / 'run.json'
        marker.parent.mkdir()
        marker.write_bytes(b'{}')
        with self.assertRaises(setup.SetupError):
            self.reconcile()
        self.assertTrue(marker.exists())
        marker.unlink()
        link = self.profile / 'node_modules' / setup.PACKAGE
        link.mkdir(parents=True)
        with self.assertRaises(setup.SetupError):
            self.reconcile()
        link.rmdir()
        self.environment.value = 'user config'
        with self.assertRaises(setup.SetupError):
            self.reconcile()
        self.environment.value = None
        manifest = self.read_manifest()
        manifest['user_setting'] = 'changed'
        self.write_manifest(manifest)
        with self.assertRaises(setup.SetupError):
            self.reconcile()
        self.assertEqual(setup.receipt_for(self.folder)['state'], 'attention')

    def test_explicit_retry_rechecks_absence_and_cannot_correct_twice(self):
        self.failed_without_package()
        self.reconcile()
        def fail(plan, arguments):
            self.commands.append(arguments)
            raise setup.PackageCommandFailed(1, 'ERR_PNPM_NO_OFFLINE_META')
        with self.assertRaises(setup.SetupError):
            setup.install(self.folder, environment=self.environment, runner=fail,
                          retry_verified=True, stopped=lambda plan: None)
        with self.assertRaises(setup.SetupError):
            self.reconcile()
        with self.assertRaises(setup.SetupError):
            setup.install(self.folder, environment=self.environment, runner=self.runner,
                          retry_verified=True, stopped=lambda plan: None)
        self.assertEqual(len(self.commands), 2)

    def test_retry_requires_original_receipt_and_fresh_absence_check(self):
        self.prepare()
        with self.assertRaises(setup.SetupError):
            setup.install(self.folder, environment=self.environment, runner=self.runner, retry_verified=True)
        self.assertEqual(self.commands, [])

    def test_process_probe_is_read_only_and_unknown_or_running_refuses(self):
        self.prepare()
        _, plan = setup.load_plan(self.folder)
        for result in (mock.Mock(returncode=0, stdout=b'123'), mock.Mock(returncode=1, stdout=b'')):
            with mock.patch.object(setup.subprocess, 'run', return_value=result):
                with self.assertRaises(setup.SetupError):
                    setup.desktop_stopped(plan)
        with mock.patch.object(setup.subprocess, 'run', return_value=mock.Mock(returncode=0, stdout=b'')) as run:
            setup.desktop_stopped(plan)
        self.assertIn('Get-CimInstance', run.call_args.args[0][-1])
        self.assertNotIn('Stop-Process', run.call_args.args[0][-1])
        self.assertFalse(run.call_args.kwargs['shell'])

    def test_network_repair_is_explicit_only_for_diagnosed_offline_cache_failure(self):
        self.failed_without_package()
        original = setup.receipt_for(self.folder)
        with self.assertRaises(setup.SetupError):
            self.install()
        setup.install(self.folder, environment=self.environment, runner=self.runner,
                      resolve_offline_cache=True, stopped=lambda plan: None)
        self.assertIn('--config.offline=false', self.commands[-1])
        self.assertIn('--config.ignore-scripts=true', self.commands[-1])
        self.assertIn('--config.auto-install-peers=false', self.commands[-1])
        record = setup.receipt_for(self.folder)
        self.assertEqual(record['attempt_history'], [original])
        self.assertTrue(record['package_network'])

    def test_network_repair_refuses_unknown_auth_and_changed_profile(self):
        self.failed_without_package()
        record = setup.receipt_for(self.folder)
        for codes in ([], ['ERR_PNPM_FETCH_401'], ['ERR_PNPM_NO_OFFLINE_META', 'ERR_PNPM_FETCH_403']):
            record['cli_diagnostic']['error_codes'] = codes
            setup.save_receipt(self.folder, record)
            with self.assertRaises(setup.SetupError):
                setup.install(self.folder, environment=self.environment, runner=self.runner,
                              resolve_offline_cache=True, stopped=lambda plan: None)
        record['cli_diagnostic']['error_codes'] = ['ERR_PNPM_NO_OFFLINE_META']
        setup.save_receipt(self.folder, record)
        self.environment.value = 'changed-user-config'
        with self.assertRaises(setup.SetupError):
            setup.install(self.folder, environment=self.environment, runner=self.runner,
                          resolve_offline_cache=True, stopped=lambda plan: None)
        self.assertEqual(len(self.commands), 1)

    def test_network_repair_failure_is_not_repeated_and_preserves_all_attempts(self):
        self.failed_without_package()
        self.reconcile()
        def fail(plan, args):
            self.commands.append(args)
            raise setup.PackageCommandFailed(1, 'ERR_PNPM_NO_OFFLINE_META')
        with self.assertRaises(setup.SetupError):
            setup.install(self.folder, environment=self.environment, runner=fail,
                          retry_verified=True, stopped=lambda plan: None)
        original = setup.receipt_for(self.folder)
        with self.assertRaises(setup.SetupError):
            setup.install(self.folder, environment=self.environment, runner=fail,
                          resolve_offline_cache=True, stopped=lambda plan: None)
        after = setup.receipt_for(self.folder)
        self.assertEqual(len(after['attempt_history']), 2)
        self.assertEqual(after['attempt_history'][1]['cli_diagnostic'], original['cli_diagnostic'])
        with self.assertRaises(setup.SetupError):
            setup.install(self.folder, environment=self.environment, runner=self.runner,
                          resolve_offline_cache=True, stopped=lambda plan: None)
        self.assertEqual(len(self.commands), 3)

    def test_user_environment_change_blocks_uninstall_without_overwrite(self):
        self.prepare()
        self.install()
        self.environment.value = 'user changed this after install'
        with self.assertRaises(setup.SetupError):
            self.uninstall()
        self.assertEqual(len(self.commands), 1)
        self.assertEqual(self.environment.value, 'user changed this after install')

    def test_package_change_blocks_uninstall(self):
        self.prepare()
        self.install()
        manifest = self.read_manifest()
        manifest['dependencies'][setup.PACKAGE] = 'file:some-other-package.tgz'
        self.write_manifest(manifest)
        with self.assertRaises(setup.SetupError):
            self.uninstall()
        self.assertEqual(len(self.commands), 1)

    def test_local_relative_dependency_spec_is_bound_to_explicit_profile(self):
        self.prepare()
        self.install()
        manifest = self.read_manifest()
        manifest['dependencies'][setup.PACKAGE] = 'file:' + os.path.relpath(self.folder / 'plugin.tgz', self.profile)
        self.write_manifest(manifest)
        self.assertTrue(self.install()['reused'])
        self.uninstall()

    def test_uninstall_survives_removed_workbench_and_removes_no_source_files(self):
        self.prepare()
        self.install()
        (self.root / '.local-worker' / 'brain_mcp_server.py').unlink()
        self.uninstall()
        self.assertTrue((self.folder / 'config.json').is_file())
        self.assertIsNone(self.environment.value)

    def test_cli_unexpected_other_metadata_change_is_preserved_and_flagged(self):
        self.prepare()
        def changes_other_metadata(plan, args):
            self.runner(plan, args)
            manifest = self.read_manifest()
            manifest['user_setting'] = 'keep-new-user-value'
            self.write_manifest(manifest)
        with self.assertRaises(setup.SetupError):
            self.install(changes_other_metadata)
        self.assertEqual(self.read_manifest()['user_setting'], 'keep-new-user-value')
        self.assertEqual(setup.receipt_for(self.folder)['state'], 'attention')
        self.assertEqual(self.environment.writes, [])

    def test_environment_write_fault_allows_owned_package_rollback(self):
        self.prepare()
        with mock.patch.object(self.environment, 'set', side_effect=OSError('write fault')):
            with self.assertRaises(setup.SetupError):
                self.install()
        self.assertFalse(setup.receipt_for(self.folder)['effect_unknown'])
        self.uninstall()
        self.assertFalse(setup.has_ours(self.read_manifest()))

    def test_existing_operation_lock_is_not_removed_or_bypassed(self):
        self.prepare()
        lock = self.folder / 'operation.lock'
        lock.write_bytes(b'other operation')
        with self.assertRaises(setup.SetupError):
            self.install()
        self.assertEqual(lock.read_bytes(), b'other operation')
        self.assertEqual(self.commands, [])

    def test_another_prepared_directory_cannot_bypass_the_same_profile_operation(self):
        self.prepare()
        second = self.base / 'other-preparation'
        setup.prepare(str(second), str(self.root), sys.executable, str(self.executable), str(self.home))
        lock = self.profile / '.model-desk-brain-install.lock'
        lock.write_bytes(b'other installer')
        with self.assertRaises(setup.SetupError):
            setup.install(second, environment=self.environment, runner=self.runner)
        self.assertEqual(self.commands, [])
        self.assertEqual(lock.read_bytes(), b'other installer')
        self.assertFalse((second / 'operation.lock').exists())

    def test_official_cli_uses_argv_desktop_scope_and_hidden_non_shell_process(self):
        self.prepare()
        _, plan = setup.load_plan(self.folder)
        with mock.patch.object(setup.subprocess, 'run', return_value=mock.Mock(returncode=0)) as run:
            setup.official_cli(plan, ['add', 'literal path with spaces.tgz', '--ignore-scripts'])
        args, kwargs = run.call_args
        self.assertEqual(args[0][3:6], ['plugin', '--profile', 'desktop'])
        self.assertIn('literal path with spaces.tgz', args[0])
        self.assertFalse(kwargs['shell'])
        self.assertEqual(kwargs['env']['DSH_HOME'], str(self.home))
        self.assertEqual(kwargs['env']['ELECTRON_RUN_AS_NODE'], '1')

    def test_package_manager_diagnostic_never_persists_raw_output(self):
        self.prepare()
        _, plan = setup.load_plan(self.folder)
        result = mock.Mock(returncode=1, stdout=b'ERR_PNPM_NO_OFFLINE_TARBALL',
                           stderr=b'Authorization: fixture-not-a-real-secret')
        with mock.patch.object(setup.subprocess, 'run', return_value=result):
            with self.assertRaises(setup.PackageCommandFailed) as caught:
                setup.official_cli(plan, ['add', str(self.folder / 'plugin.tgz')])
        public = json.dumps(caught.exception.diagnostic)
        self.assertNotIn('fixture-not-a-real-secret', public)
        self.assertEqual(caught.exception.diagnostic['error_codes'], ['ERR_PNPM_NO_OFFLINE_TARBALL'])

    def test_check_does_not_report_an_unresolved_receipt_as_ready(self):
        self.prepare()
        setup.save_receipt(self.folder, {'schema': 1, 'state': 'attention'})
        result = setup.check(self.folder)
        self.assertFalse(result['ready'])
        self.assertEqual(result['receipt_state'], 'attention')

    def test_check_reports_verified_install_and_refuses_changed_pointer(self):
        self.prepare()
        self.install()
        result = setup.check(self.folder, environment=self.environment)
        self.assertTrue(result['installed'])
        self.assertTrue(result['ready'])
        self.environment.value = 'changed-user-config'
        result = setup.check(self.folder, environment=self.environment)
        self.assertFalse(result['installed'])
        self.assertFalse(result['ready'])

    def test_controller_check_uses_unknown_read_only_context_and_disconnects(self):
        # Retain the original before setUp replaces the public helper.
        transport = mock.Mock()
        context = setup.brain_context.build_context(str(self.root), 'dsh-setup', 'desktop-installer',
                session_binding='unknown', launcher='dsh-desktop-check')
        transport.call.return_value = {'context': context.public()}
        transport._request.return_value = (200, {'schema': 1, 'source': 'dsh-account-controller', 'refresh': 'snapshot-only'})
        with mock.patch.object(setup.brain_transport, 'BrainTransport', return_value=transport):
            result = ORIGINAL_CONTROLLER_CHECK({'workbench': str(self.root), 'panel_url': 'http://127.0.0.1:8765'})
        self.assertFalse(result['task_mutation_authority'])
        self.assertIsInstance(transport.connect.call_args.args[0], dict)
        self.assertEqual(transport.connect.call_args.args[0]['session_binding'], 'unknown')
        transport.call.assert_called_once_with('describe', {})
        transport.disconnect.assert_called_once()

    def test_preflight_uses_real_transport_contract_and_keeps_capability_private(self):
        context = setup.brain_context.build_context(str(self.root), 'dsh-setup', 'desktop-installer',
                session_binding='unknown', launcher='dsh-desktop-check')
        calls = []
        def request(method, route, body=None, **kwargs):
            calls.append(route)
            if route == '/api/session':
                return 200, {'token': 'fixture-panel-only'}
            if route == '/api/brain/connect':
                self.assertIsInstance(body, dict)
                self.assertEqual(body['session_binding'], 'unknown')
                return 201, {'capability': 'fixture-capability-only', 'context': context.public()}
            if route == '/api/brain/call':
                self.assertEqual(body, {'operation': 'describe', 'payload': {}})
                return 200, {'context': context.public()}
            if route == '/api/host-account':
                return 200, {'schema': 1, 'source': 'dsh-account-controller', 'refresh': 'snapshot-only'}
            raise AssertionError('unexpected route')
        with mock.patch.object(setup.brain_transport.BrainTransport, '_request', side_effect=request):
            result = ORIGINAL_CONTROLLER_CHECK({'workbench': str(self.root), 'panel_url': 'http://127.0.0.1:8765'})
        self.assertEqual(calls, ['/api/session', '/api/brain/connect', '/api/brain/call', '/api/host-account'])
        self.assertFalse(result['task_mutation_authority'])
        self.assertNotIn('fixture-', json.dumps(result))

    def test_unexpected_controller_identity_cannot_become_an_install_permission(self):
        transport = mock.Mock()
        transport.call.return_value = {'context': {'owner': 'bad shape', 'session': {}, 'mutations_allowed': True}}
        with mock.patch.object(setup.brain_transport, 'BrainTransport', return_value=transport):
            with self.assertRaises(setup.SetupError):
                ORIGINAL_CONTROLLER_CHECK({'workbench': str(self.root), 'panel_url': 'http://127.0.0.1:8765'})
        transport.disconnect.assert_called_once()

    def test_old_controller_without_account_endpoint_is_not_install_ready(self):
        context = setup.brain_context.build_context(str(self.root), 'dsh-setup', 'desktop-installer',
            session_binding='unknown', launcher='dsh-desktop-check')
        transport = mock.Mock()
        transport.call.return_value = {'context': context.public()}
        transport._request.return_value = (404, {})
        with mock.patch.object(setup.brain_transport, 'BrainTransport', return_value=transport):
            with self.assertRaisesRegex(setup.SetupError, '余额接口'):
                ORIGINAL_CONTROLLER_CHECK({'workbench': str(self.root), 'panel_url': 'http://127.0.0.1:8765'})
        transport.disconnect.assert_called_once()

    @unittest.skipUnless(os.name == 'nt', 'Windows User environment adapter')
    def test_user_environment_touches_only_its_owned_name(self):
        registry = mock.MagicMock()
        registry.REG_SZ = 1
        registry.HKEY_CURRENT_USER = 'HKCU'
        registry.QueryValueEx.return_value = ('owned-config-path', registry.REG_SZ)
        import ctypes
        with mock.patch.dict(sys.modules, {'winreg': registry}), mock.patch.object(ctypes.windll.user32, 'SendMessageTimeoutW'):
            environment = setup.UserEnvironment()
            self.assertEqual(environment.get(), 'owned-config-path')
            environment.set('owned-config-path')
            environment.set(None)
        self.assertEqual(registry.OpenKey.call_args.args, ('HKCU', 'Environment'))
        self.assertEqual(registry.QueryValueEx.call_args.args[1], setup.ENV_NAME)
        self.assertEqual(registry.SetValueEx.call_args.args[1], setup.ENV_NAME)
        self.assertEqual(registry.DeleteValue.call_args.args[1], setup.ENV_NAME)


ORIGINAL_CONTROLLER_CHECK = setup.controller_check

if __name__ == '__main__':
    unittest.main()
