"""Offline fixture installs; no user configuration or real inference."""
import os
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import runtime_paths
import preset_runtime as presets


class ExpandedLayoutTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.exe = self.root / runtime_paths.DSH_EXECUTABLE_NAME
        self.exe.write_bytes(b'fixture')
        self.archive = self.root / 'resources/app.asar'
        self.app = self.root / 'resources/app'
        for name, value in [(presets.STANDARD_ENTRY, b'standard'), (presets.WEB_ENTRY, b'overlay')]:
            target = self.app / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(value)
        self.cli = self.app / Path(runtime_paths.DSH_CLI).relative_to('resources/app.asar')
        self.cli.parent.mkdir(parents=True, exist_ok=True)
        self.cli.write_bytes(b'// fixture')

    def tearDown(self):
        self.tmp.cleanup()

    def test_expanded_cli_is_selected_only_when_archive_missing(self):
        self.assertEqual(runtime_paths.dsh_cli(self.exe), self.cli)
        self.archive.write_bytes(b'invalid archive')
        self.assertEqual(runtime_paths.dsh_cli(self.exe), self.root / runtime_paths.DSH_CLI)

    def test_expanded_yaml_bytes_and_content_identity_track_changes(self):
        self.assertEqual(presets._standard_sources(self.archive), (b'standard', b'overlay'))
        before = presets.content_revision('standard', self.exe)
        self.assertIsNotNone(before)
        target = self.app / presets.STANDARD_ENTRY
        stamp = target.stat()
        target.write_bytes(b'changed!')
        os.utime(target, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
        self.assertNotEqual(before, presets.content_revision('standard', self.exe))

    def test_corrupt_archive_does_not_fall_back_to_other_install_content(self):
        self.archive.write_bytes(b'invalid archive')
        self.assertIsNone(presets.content_revision('standard', self.exe))
        with self.assertRaises(ValueError):
            presets._standard_sources(self.archive)

    def test_missing_required_overlay_keeps_identity_unknown(self):
        (self.app / presets.WEB_ENTRY).unlink()
        self.assertIsNone(presets.content_revision('standard', self.exe))


if __name__ == '__main__':
    unittest.main()
