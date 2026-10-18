"""Offline tests for the local DSH brain plugin config generator.

The generator is deliberately independent of the live workbench: these tests
use only temporary files and assert that exactly one user-named output is
created, never overwritten, and never contains a fixed project/owner/session.
"""
import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import dsh_brain_plugin  # noqa: E402


class GeneratorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.dir = Path(self.temp.name)
        self.python = self.dir / 'python.exe'
        self.brain = self.dir / 'brain_mcp_server.py'
        self.workflow = self.dir / 'workflow_settings.py'
        self.mcp_client = self.dir / 'mcp-client.cjs'
        self.python.write_text('fixture python\n', encoding='utf-8')
        self.brain.write_text('fixture brain\n', encoding='utf-8')
        self.workflow.write_text('fixture workflow\n', encoding='utf-8')
        self.mcp_client.write_text('fixture mcp\n', encoding='utf-8')
        self.output = self.dir / 'dsh-brain.json'

    def tearDown(self):
        self.temp.cleanup()

    def generate(self, output=None, **overrides):
        values = {
            'output': str(output or self.output),
            'python': str(self.python),
            'brain_mcp_server': str(self.brain),
            'workflow_settings': str(self.workflow),
        }
        values.update(overrides)
        return dsh_brain_plugin.generate(**values)

    def test_generate_writes_only_the_explicit_output(self):
        before = {path.relative_to(self.dir) for path in self.dir.rglob('*')}
        result = self.generate(mcp_client_path=str(self.mcp_client))
        after = {path.relative_to(self.dir) for path in self.dir.rglob('*')}
        self.assertEqual(after - before, {Path('dsh-brain.json')})
        self.assertEqual(result['ok'], True)
        self.assertEqual(result['output'], str(self.output))
        self.assertGreater(result['bytes'], 0)

        raw = self.output.read_bytes()
        self.assertTrue(raw.endswith(b'\n'))
        self.assertNotIn(b'\r\n', raw)
        payload = json.loads(raw.decode('utf-8'))
        self.assertEqual(payload['schema'], 1)
        self.assertEqual(payload['python'], str(self.python))
        self.assertEqual(payload['brainMcpServer'], str(self.brain))
        self.assertEqual(payload['workflowSettings'], str(self.workflow))
        self.assertEqual(payload['panelUrl'], 'http://127.0.0.1:8765')
        self.assertEqual(payload['serverName'], 'model_desk_brain')
        self.assertEqual(payload['ownerPrefix'], 'dsh')
        self.assertEqual(payload['mcpClientPath'], str(self.mcp_client))
        for forbidden in (
            'project', 'projectPath', 'cwd', 'owner', 'session', 'sessionId',
            'nativeSessionId', 'role', 'host',
        ):
            self.assertNotIn(forbidden, payload)

    def test_existing_output_is_refused_and_unchanged(self):
        self.output.write_text('keep me\n', encoding='utf-8')
        with self.assertRaises(FileExistsError):
            self.generate()
        self.assertEqual(self.output.read_text(encoding='utf-8'), 'keep me\n')

    def test_output_symlink_cannot_target_records(self):
        records = self.dir / '.model-desk'
        records.mkdir()
        alias = self.dir / 'alias'
        try:
            alias.symlink_to(records, target_is_directory=True)
        except OSError:
            self.skipTest('directory symlinks unavailable')
        with self.assertRaises(dsh_brain_plugin.ConfigError):
            self.generate(output=alias / 'config.json')
        self.assertEqual(list(records.iterdir()), [])

    def test_cli_existing_output_returns_3_and_keeps_file(self):
        self.output.write_text('keep me\n', encoding='utf-8')
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            code = dsh_brain_plugin.main([
                '--python', str(self.python),
                '--brain-mcp-server', str(self.brain),
                '--workflow-settings', str(self.workflow),
                '--output', str(self.output),
            ])
        self.assertEqual(code, 3)
        self.assertIn('拒绝覆盖', stderr.getvalue())
        self.assertEqual(self.output.read_text(encoding='utf-8'), 'keep me\n')

    def test_cli_success_creates_only_requested_config(self):
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = dsh_brain_plugin.main([
                '--python', str(self.python),
                '--brain-mcp-server', str(self.brain),
                '--workflow-settings', str(self.workflow),
                '--output', str(self.output),
                '--server-name', 'custom_brain',
                '--owner-prefix', 'me',
                '--panel-url', 'http://localhost:8765',
            ])
        self.assertEqual(code, 0)
        summary = json.loads(stdout.getvalue())
        self.assertEqual(summary['output'], str(self.output))
        payload = json.loads(self.output.read_text(encoding='utf-8'))
        self.assertEqual(payload['serverName'], 'custom_brain')
        self.assertEqual(payload['ownerPrefix'], 'me')
        self.assertEqual(payload['panelUrl'], 'http://localhost:8765')

    def test_relative_missing_and_wrong_type_paths_are_rejected(self):
        cases = [
            {'python': 'python.exe'},
            {'brain_mcp_server': str(self.dir / 'missing.py')},
            {'workflow_settings': str(self.dir)},
            {'mcp_client_path': str(self.dir / 'missing.cjs')},
        ]
        for overrides in cases:
            with self.subTest(overrides=overrides), self.assertRaises(dsh_brain_plugin.ConfigError):
                self.generate(**overrides)
            self.assertFalse(self.output.exists())

    def test_output_must_be_explicit_absolute_and_in_an_existing_parent(self):
        for output in ('relative.json', str(self.dir / 'missing-dir' / 'x.json')):
            with self.subTest(output=output), self.assertRaises(dsh_brain_plugin.ConfigError):
                self.generate(output=output)
            self.assertFalse(self.output.exists())

    def test_output_inside_workbench_records_is_refused(self):
        records = self.dir / '.model-desk' / 'run'
        records.mkdir(parents=True)
        output = records / 'dsh-brain.json'
        with self.assertRaises(dsh_brain_plugin.ConfigError):
            self.generate(output=str(output))
        self.assertFalse(output.exists())

    def test_panel_and_identity_names_are_validated(self):
        bad = [
            {'panel_url': 'https://127.0.0.1:8765'},
            {'panel_url': 'http://example.com:8765'},
            {'panel_url': 'http://127.0.0.1:8765/path'},
            {'panel_url': 'http://user:pass@127.0.0.1:8765'},
            {'panel_url': 'http://127.0.0.1:not-a-port'},
            {'server_name': 'bad name'},
            {'owner_prefix': 'bad/prefix'},
            {'tool_call_timeout_ms': 10},
            {'status_timeout_ms': 600_000},
        ]
        for overrides in bad:
            with self.subTest(overrides=overrides), self.assertRaises(dsh_brain_plugin.ConfigError):
                self.generate(**overrides)
            self.assertFalse(self.output.exists())

    def test_ipv6_loopback_origin_keeps_brackets(self):
        config = dsh_brain_plugin.build_config(
            python=str(self.python),
            brain_mcp_server=str(self.brain),
            workflow_settings=str(self.workflow),
            panel_url='http://[::1]:8765',
        )
        self.assertEqual(config['panelUrl'], 'http://[::1]:8765')

    def test_failed_write_leaves_no_partial_output(self):
        with mock.patch.object(dsh_brain_plugin.os, 'fdopen', side_effect=OSError('fixture write failure')):
            with self.assertRaises(OSError):
                self.generate()
        self.assertFalse(self.output.exists())

    def test_config_contains_no_fixed_chat_binding(self):
        config = dsh_brain_plugin.build_config(
            python=str(self.python),
            brain_mcp_server=str(self.brain),
            workflow_settings=str(self.workflow),
        )
        encoded = json.dumps(config, ensure_ascii=False)
        for value in ('project', 'owner', 'session', 'role', 'host', 'cwd'):
            self.assertNotIn(f'"{value}"', encoded)


if __name__ == '__main__':
    unittest.main()
