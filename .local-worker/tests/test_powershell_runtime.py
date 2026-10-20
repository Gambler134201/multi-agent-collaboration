from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import powershell_runtime as runtime
import codex_mcp_server
import folder_picker
import panel_service


class PowerShellResolutionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.environment = mock.patch.dict(runtime.os.environ, {'USERPROFILE': str(self.home), 'PATH': ''}, clear=True)
        self.environment.start(); self.addCleanup(self.environment.stop)
        self.which = mock.patch.object(runtime.shutil, 'which', return_value=None).start()
        self.addCleanup(mock.patch.stopall)

    def executable(self, relative):
        file = self.home / relative
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_bytes(b'not executed')
        return str(file.resolve())

    def test_no_codex_process_or_path_required_for_existing_cached_native_runtime(self):
        expected = self.executable('.cache/codex-runtimes/codex-primary-runtime/dependencies/native/powershell/pwsh.exe')
        self.assertEqual(runtime.find_pwsh(), expected)
        self.assertEqual(panel_service.find_pwsh(), expected)
        self.assertEqual(codex_mcp_server.find_pwsh(), expected)
        self.assertEqual(folder_picker.find_pwsh(), expected)

    def test_system_installation_works_without_any_codex_installation(self):
        expected = self.executable('programs/PowerShell/7/pwsh.exe')
        runtime.os.environ['ProgramFiles'] = str(self.home / 'programs')
        self.assertEqual(runtime.find_pwsh(), expected)

    def test_explicit_existing_native_override_takes_precedence(self):
        expected = self.executable('explicit/pwsh.exe')
        runtime.os.environ['MODEL_DESK_PWSH'] = expected
        self.assertEqual(runtime.find_pwsh(), expected)
        self.which.assert_not_called()

    def test_bad_explicit_override_never_silently_uses_another_program(self):
        for value in ['relative/pwsh.exe', self.executable('legacy/powershell.exe'), str(self.home / 'missing/pwsh.exe')]:
            runtime.os.environ['MODEL_DESK_PWSH'] = value
            with self.assertRaisesRegex(ValueError, 'MODEL_DESK_PWSH'):
                runtime.find_pwsh()
        self.which.assert_not_called()

    def test_path_native_executable_is_returned_as_absolute(self):
        expected = self.executable('path/pwsh.exe')
        self.which.return_value = expected
        self.assertEqual(runtime.find_pwsh(), expected)

    def test_missing_dependency_does_not_fall_back_to_windows_powershell(self):
        with self.assertRaisesRegex(ValueError, '未找到原生 PowerShell 7'):
            runtime.find_pwsh()
        self.assertEqual([call.args[0] for call in self.which.call_args_list], ['pwsh.exe', 'pwsh'])


if __name__ == '__main__':
    unittest.main()
