import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import model_registry as registry


class RecordIOTests(unittest.TestCase):
    def test_transient_read_denial_recovers_without_reissuing_a_model_request(self):
        with tempfile.TemporaryDirectory() as root:
            run = Path(root)
            registry.write_json(run / 'task.json', {'model_calls': 7})
            original = Path.read_text
            attempts = []
            def read(path, *args, **kwargs):
                attempts.append(path)
                if len(attempts) == 1:
                    raise PermissionError('sharing violation')
                return original(path, *args, **kwargs)
            with patch.object(Path, 'read_text', read), patch.object(registry.time, 'sleep'):
                self.assertEqual(registry.read_task(run)['model_calls'], 7)
            self.assertEqual(len(attempts), 2)

    def test_persistent_denial_is_bounded_and_never_treated_as_an_empty_record(self):
        with tempfile.TemporaryDirectory() as root:
            run = Path(root)
            registry.write_json(run / 'task.json', {'model_calls': 7})
            with patch.object(Path, 'read_text', side_effect=PermissionError()) as read, patch.object(registry.time, 'sleep'):
                with self.assertRaises(PermissionError):
                    registry.read_task(run)
            self.assertEqual(read.call_count, 6)

    def test_atomic_write_recovers_and_preserves_previous_record_on_permanent_failure(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'task.json'
            registry.write_json(path, {'value': 'old'})
            replace = registry.os.replace
            calls = []
            def transient(source, target):
                calls.append(target)
                if len(calls) == 1:
                    raise PermissionError()
                return replace(source, target)
            with patch.object(registry.os, 'replace', transient), patch.object(registry.time, 'sleep'):
                registry.write_json(path, {'value': 'new'})
            self.assertEqual(json.loads(path.read_text())['value'], 'new')
            with patch.object(registry.os, 'replace', side_effect=PermissionError()) as denied, patch.object(registry.time, 'sleep'):
                with self.assertRaises(PermissionError):
                    registry.write_json(path, {'value': 'lost'})
            self.assertEqual(denied.call_count, 6)
            self.assertEqual(json.loads(path.read_text())['value'], 'new')
            self.assertFalse(list(Path(root).glob('*.tmp')))


class TaskDirectoryResolutionTests(unittest.TestCase):
    def test_task_dir_can_resolve_to_a_project_store_without_touching_global_runs(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            install = root / 'install'
            install.mkdir()
            project = root / 'project'
            project.mkdir()
            runs = install / 'runs'
            runs.mkdir()
            with patch.object(registry, 'HERE', install), patch.object(registry, 'RUNS', runs):
                run = registry.prepare_task_dir('task-1', project)
                self.assertEqual(run, project / '.model-desk' / 'runs' / 'task-1')
                self.assertEqual(registry.task_dir('task-1'), run)
                self.assertEqual(list(runs.glob('*')), [])

    def test_unindexed_task_keeps_the_legacy_global_location(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            install = root / 'install'
            install.mkdir()
            runs = install / 'runs'
            runs.mkdir()
            with patch.object(registry, 'HERE', install), patch.object(registry, 'RUNS', runs):
                self.assertEqual(registry.task_dir('old-task'), runs / 'old-task')


if __name__ == '__main__':
    unittest.main()
