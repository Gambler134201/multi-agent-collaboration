"""Offline checks for the open-source candidate's installability.

Everything here runs with the plain project Python and touches only the
operating system's temporary directory plus mocks.  No provider is called, no
service is started, no MCP is registered, no real rule directory is written, and
no real model, key or session is read.

Covered acceptance points:
  * a clean directory with neither ``models.json`` nor legacy ``settings.json``
    initializes to an empty model list without creating configuration;
  * an existing registry (models and encrypted keys) is never overwritten by the
    legacy import;
  * DSH / Pi / Node locations come from explicit environment variables, and a
    missing or mismatched runtime is reported unavailable instead of being
    replaced by another engine;
  * ``setup.ps1`` performs no global change unless explicitly asked;
  * the shipped source carries no author-specific install location;
  * installing the optional global rule binds ``@ROOT@`` in the rule *and* in the
    linked operation guides, and preserves unmanaged instructions.
"""
from contextlib import contextmanager
from pathlib import Path
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import dsh_worker
import execution_engines as engines
import executor_adapter as adapter
import install_workflow_rules as rules
import model_registry as registry
import pi_worker
import runtime_paths
import workflow_settings

PROJECT_ROOT = ROOT.parent
GUIDES = ROOT / 'workflow-guides'
LOCATION_VARIABLES = (runtime_paths.DSH_EXE_ENV, runtime_paths.PI_HOME_ENV, runtime_paths.NODE_EXE_ENV)


@contextmanager
def unconfigured_runtimes():
    """Pretend the user has not configured any external runtime yet."""
    saved = {name: os.environ.pop(name, None) for name in LOCATION_VARIABLES}
    try:
        yield
    finally:
        for name, value in saved.items():
            if value is not None:
                os.environ[name] = value


class CleanRegistryTests(unittest.TestCase):
    """A fresh checkout has no model, no key and no remote endpoint."""

    def test_empty_directory_yields_empty_model_list_without_writing(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            with patch.object(registry, 'HERE', home), patch.object(registry, 'REGISTRY', home / 'models.json'):
                data = registry.read_registry()
                self.assertEqual([], data['models'])
                self.assertIsNone(data['default_model'])
                # Reading must not create user configuration on a first run.
                self.assertFalse((home / 'models.json').exists())
                self.assertFalse((home / 'models.lock').exists())

    def test_collaboration_and_paid_defaults_stay_off(self):
        self.assertFalse(workflow_settings.DEFAULTS['enabled'])
        self.assertFalse(workflow_settings.DEFAULTS['paid_allowed'])

    def test_legacy_settings_import_happens_once_and_keeps_existing_rows(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            (home / 'settings.json').write_text('{"api_base": "http://127.0.0.1:1234/v1", "model": "local-qwen"}',
                                                encoding='utf-8')
            with patch.object(registry, 'HERE', home), patch.object(registry, 'REGISTRY', home / 'models.json'):
                first = registry.read_registry()
                self.assertEqual(1, len(first['models']))
                self.assertEqual('home', first['default_model'])
                self.assertTrue((home / 'models.json').is_file())
                # A later edit wins: the legacy file must never overwrite it.
                stored = registry.read_json(home / 'models.json')
                stored['models'].append({'id': 'mine', 'name': 'My model', 'provider': 'custom',
                                         'api_base': 'https://api.example.test/v1', 'model': 'theirs',
                                         'secret': 'encrypted-keep-me', 'enabled': True})
                stored['default_model'] = 'mine'
                registry.write_json(home / 'models.json', stored)
                again = registry.read_registry()
                self.assertEqual({'home', 'mine'}, {row['id'] for row in again['models']})
                self.assertEqual('encrypted-keep-me',
                                 next(row for row in again['models'] if row['id'] == 'mine')['secret'])


class PortableRuntimePathTests(unittest.TestCase):
    """Locations come from the user's own configuration, never from the repo."""

    def test_environment_declares_the_locations(self):
        with tempfile.TemporaryDirectory() as directory:
            install = Path(directory)
            environment = {runtime_paths.DSH_EXE_ENV: str(install / 'DeepSeek Harness.exe'),
                           runtime_paths.PI_HOME_ENV: str(install),
                           runtime_paths.NODE_EXE_ENV: str(install / 'node.exe')}
            with patch.dict(os.environ, environment):
                self.assertEqual(install / 'DeepSeek Harness.exe', runtime_paths.dsh_executable())
                self.assertEqual(install / runtime_paths.DSH_CLI, runtime_paths.dsh_cli())
                self.assertEqual(install / 'node.exe', runtime_paths.node_executable())
                self.assertEqual(install, runtime_paths.pi_home())
                self.assertEqual(install / runtime_paths.PI_PACKAGE_DIR, runtime_paths.pi_package())
                self.assertEqual(install / runtime_paths.PI_CLI, runtime_paths.pi_cli())
                self.assertEqual(install / runtime_paths.PI_NODE, runtime_paths.pi_node())

    def test_path_lookup_returns_paths_not_strings(self):
        """A PATH hit must stay a Path: workers call ``.is_file()`` on it."""
        with unconfigured_runtimes(), patch.object(runtime_paths.shutil, 'which', lambda name: 'X:/fixture/node.exe'):
            node = runtime_paths.node_executable()
        self.assertIsInstance(node, Path)
        self.assertEqual(Path('X:/fixture/node.exe'), node)

    def test_unconfigured_or_relative_values_are_not_guessed(self):
        with unconfigured_runtimes(), patch.object(runtime_paths.shutil, 'which', lambda name: None):
            self.assertIsNone(runtime_paths.dsh_executable())
            self.assertIsNone(runtime_paths.dsh_cli())
            self.assertIsNone(runtime_paths.node_executable())
            self.assertIsNone(runtime_paths.pi_home())
            self.assertIsNone(runtime_paths.pi_cli())
            self.assertEqual(('', ''), runtime_paths.pi_release())
            with patch.dict(os.environ, {runtime_paths.DSH_EXE_ENV: 'relative', runtime_paths.PI_HOME_ENV: '   '}):
                self.assertIsNone(runtime_paths.dsh_executable())
                self.assertIsNone(runtime_paths.pi_home())

    def test_worker_and_capability_adapter_read_the_same_dsh_location(self):
        with tempfile.TemporaryDirectory() as directory:
            executable = Path(directory) / runtime_paths.DSH_EXECUTABLE_NAME
            executable.write_text('fixture', encoding='utf-8')
            with patch.dict(os.environ, {runtime_paths.DSH_EXE_ENV: str(executable)}):
                self.assertEqual(executable, dsh_worker.runtime_paths.dsh_executable())
                self.assertEqual(executable, adapter.executor_path())
            with unconfigured_runtimes(), \
                    patch.object(runtime_paths.shutil, 'which', lambda name: None):
                self.assertEqual(runtime_paths.UNSET, adapter.executor_path())

    def test_missing_dsh_is_reported_and_never_replaced(self):
        with patch.object(dsh_worker, 'EXECUTABLE', runtime_paths.UNSET), \
                patch.object(dsh_worker, 'NODE', runtime_paths.UNSET):
            info = dsh_worker.availability()
        self.assertFalse(info['ready'])
        self.assertIn(runtime_paths.DSH_EXE_ENV, info['message'])
        self.assertIn(runtime_paths.NODE_EXE_ENV, info['message'])
        self.assertIn('不会改用其他执行器', info['message'])

    def test_configured_dsh_only_needs_the_declared_pieces(self):
        with tempfile.TemporaryDirectory() as directory:
            install = Path(directory)
            executable = install / runtime_paths.DSH_EXECUTABLE_NAME
            executable.write_text('fixture', encoding='utf-8')
            node = install / 'node.exe'
            node.write_text('fixture', encoding='utf-8')
            acpx = install / 'acpx-runtime.js'
            acpx.write_text('fixture', encoding='utf-8')
            with patch.object(dsh_worker, 'EXECUTABLE', executable), patch.object(dsh_worker, 'NODE', node), \
                    patch.object(dsh_worker.runtime_paths, 'acpx_runtime', lambda runtime_dir=None: acpx):
                info = dsh_worker.availability()
        self.assertTrue(info['ready'], info['message'])
        self.assertEqual(runtime_paths.ACPX_VERSION, info['acpx_version'])

    def test_pi_requires_its_own_root_and_the_verified_version(self):
        with unconfigured_runtimes():
            info = pi_worker.availability()
        self.assertFalse(info['ready'])
        self.assertIn(runtime_paths.PI_HOME_ENV, info['message'])

        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            package = home / runtime_paths.PI_PACKAGE_DIR
            package.mkdir(parents=True)
            node = home / runtime_paths.PI_NODE
            node.parent.mkdir(parents=True, exist_ok=True)
            node.write_text('fixture', encoding='utf-8')
            cli = home / runtime_paths.PI_CLI
            cli.parent.mkdir(parents=True, exist_ok=True)
            cli.write_text('fixture', encoding='utf-8')
            for version, expected in ((runtime_paths.PI_VERSION, True), ('9.9.9', False)):
                (package / 'package.json').write_text(
                    '{"name": "%s", "version": "%s"}' % (runtime_paths.PI_PACKAGE, version), encoding='utf-8')
                with patch.dict(os.environ, {runtime_paths.PI_HOME_ENV: str(home)}), \
                        patch.object(pi_worker, 'PI_HOME', home), patch.object(pi_worker, 'PACKAGE', package), \
                        patch.object(pi_worker, 'NODE', node), patch.object(pi_worker, 'CLI', cli):
                    info = pi_worker.availability()
                    # A later process-environment edit must not make readiness
                    # describe another runtime than prepare()/spawn will use.
                    with patch.dict(os.environ, {runtime_paths.PI_HOME_ENV: str(home / 'other')}):
                        after = pi_worker.availability()
                    self.assertEqual(info['ready'], after['ready'])
                    self.assertEqual(str(cli), after['executable'])
                    self.assertEqual(str(node), after['node'])
                self.assertEqual(expected, info['ready'], info['message'])
                self.assertEqual(version, info['version'])
                if not expected:
                    self.assertIn('9.9.9', info['message'])
                    self.assertIn('不会改用其他版本或执行器', info['message'])

    def test_unready_engine_stays_the_selected_engine(self):
        """The selection never quietly moves to another agent (for example aider)."""
        self.assertEqual('pi', engines.selected({'execution_agent': 'pi'}))
        with unconfigured_runtimes():
            selected, info = engines.public_selection({'execution_agent': 'pi'})
        self.assertEqual('pi', selected)
        self.assertFalse(info['ready'])
        with self.assertRaises(ValueError):
            engines.availability('aider')


class SetupScriptTests(unittest.TestCase):
    """``setup.ps1`` only touches this installation unless explicitly told."""

    def setUp(self):
        self.text = (PROJECT_ROOT / 'setup.ps1').read_text(encoding='utf-8-sig')

    def test_encoding_survives_windows_powershell(self):
        """Windows PowerShell 5.1 reads a BOM-less file as ANSI and mangles CJK."""
        self.assertTrue((PROJECT_ROOT / 'setup.ps1').read_bytes().startswith(b'\xef\xbb\xbf'),
                        'setup.ps1 must be saved as UTF-8 with BOM')

    def test_global_actions_sit_behind_explicit_switches(self):
        for marker, guarded in (('if ($RegisterMcp)', 'install_codex_workbench.py'),
                                ('if ($InstallRules)', 'install_workflow_rules.py')):
            self.assertIn(marker, self.text)
            self.assertEqual(-1, self.text.find(guarded, 0, self.text.index(marker)),
                             guarded + ' must never run without its own switch')

    def test_default_run_never_starts_a_service_or_a_worker(self):
        for forbidden in ('Start-Process', 'start_panel.py', 'panel_server.py', 'dispatch_task.py',
                          'run_worker.py', 'setx ', 'Invoke-RestMethod'):
            self.assertNotIn(forbidden, self.text, forbidden)

    def test_it_installs_only_declared_dependencies_in_this_directory(self):
        self.assertIn("Join-Path $root '.local-worker'", self.text)
        self.assertIn('requirements.txt', self.text)
        self.assertIn('-m pip install', self.text)
        self.assertIn('--frozen-lockfile', self.text)
        self.assertIn('-Check', self.text)


class NoAuthorLocationTests(unittest.TestCase):
    """Nothing shipped may hard-code one machine's layout."""

    PORTABLE = ('.local-worker/runtime_paths.py', '.local-worker/dsh_worker.py', '.local-worker/pi_worker.py',
                '.local-worker/executor_adapter.py', '.local-worker/install_workflow_rules.py', 'setup.ps1',
                'README.md', 'docs/INSTALL.md', 'requirements.txt')
    FORBIDDEN = ('E:/dsh', 'E:\\dsh', 'E:/pi', 'E:\\pi', 'C:/Users', 'C:\\Users', 'codex-runtimes',
                 '.cache/codex', 'AIjob')

    def test_portable_files_have_no_baked_in_machine_paths(self):
        for name in self.PORTABLE:
            text = (PROJECT_ROOT / name).read_text(encoding='utf-8')
            for token in self.FORBIDDEN:
                self.assertNotIn(token, text, name + ' contains ' + token)

    def test_guide_templates_stay_generic(self):
        templates = sorted(GUIDES.glob('*.md'))
        self.assertTrue(templates)
        for template in templates:
            text = template.read_text(encoding='utf-8')
            for token in ('C:\\Users', 'AIjob'):
                self.assertNotIn(token, text, template.name)


class RuleInstallBindingTests(unittest.TestCase):
    """The optional global rule must be bound to the real installation."""

    def test_rule_and_linked_guides_are_bound_and_unmanaged_text_survives(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            (home / 'AGENTS.md').write_text('我的规则保留。\n', encoding='utf-8')
            path = rules.install(home)
            text = path.read_text(encoding='utf-8')
            self.assertTrue(text.startswith('我的规则保留。\n'))
            self.assertIn(rules.START, text)
            self.assertIn(str(rules.ROOT), text)
            self.assertNotIn('@ROOT@', text)
            self.assertIn(rules.GUIDES_DIR_NAME, text)

            copies = rules.guides_dir(home)
            self.assertTrue(copies.is_dir())
            names = sorted(item.name for item in copies.glob('*.md'))
            self.assertEqual(sorted(item.name for item in GUIDES.glob('*.md')), names)
            for name in names:
                template = (GUIDES / name).read_text(encoding='utf-8')
                body = (copies / name).read_text(encoding='utf-8')
                self.assertNotIn('@ROOT@', body, name)
                if '@ROOT@' in template:
                    self.assertIn(str(rules.ROOT), body, name)

            first = path.read_text(encoding='utf-8')
            rules.install(home)
            self.assertEqual(first, path.read_text(encoding='utf-8'))

    def test_incomplete_markers_still_preserve_the_file(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            path = home / 'AGENTS.md'
            original = 'unmanaged\n' + rules.START + '\nbroken'
            path.write_text(original, encoding='utf-8')
            with self.assertRaises(ValueError):
                rules.install(home)
            self.assertEqual(original, path.read_text(encoding='utf-8'))


if __name__ == '__main__':
    unittest.main(verbosity=2)
