import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import dsh_worker
import preset_runtime as presets
import model_registry as registry


class PresetTests(unittest.TestCase):
    def test_named_definitions_and_shared_restrictions(self):
        native = presets.load({'provider': 'local'}, dsh_worker.EXECUTABLE)
        standard = presets.load({'provider': 'deepseek'}, dsh_worker.EXECUTABLE)
        self.assertEqual(native['id'], 'native')
        self.assertEqual(standard['id'], 'standard')
        native_rows = native['declaration']['config']['plugins']
        standard_rows = standard['declaration']['config']['plugins']
        persona = next(r for r in native_rows if r['id'] == 'persona')
        self.assertTrue(persona['config']['complete'])
        self.assertNotIn('planning', [r['id'] for r in native_rows])
        self.assertIn('planning', [r['id'] for r in standard_rows])
        self.assertTrue(next(r for r in standard_rows if r['id'] == 'delegation')['disabled'])
        for rows in (native_rows, standard_rows):
            self.assertTrue(next(r for r in rows if r['id'] == 'tool-web')['disabled'])
            self.assertIsInstance(next(r for r in rows if r['id'] == 'tool-pwsh')['disabled'], presets.JS)

    def test_revision_or_preset_change_requires_new_session(self):
        model = {'id': 'home', 'api_base': 'http://localhost/v1', 'model': 'qwen', 'max_input_tokens': 262144, 'max_output_tokens': 8192}
        a = dsh_worker.fingerprint(model, 'edit', 'project', {'id': 'native', 'revision': 'a'})
        b = dsh_worker.fingerprint(model, 'edit', 'project', {'id': 'standard', 'revision': 'a'})
        c = dsh_worker.fingerprint(model, 'edit', 'project', {'id': 'native', 'revision': 'b'})
        self.assertEqual(len({a, b, c}), 3)

    def test_runtime_proof_required_and_must_match(self):
        preset = {'id': 'native', 'revision': 'a'}
        with tempfile.TemporaryDirectory() as folder:
            run = Path(folder)
            with self.assertRaises(ValueError):
                presets.verify(run, preset)
            registry.write_json(run / 'preset-proof.json', {'preset': 'standard', 'revision': 'a', 'session_id': 'session'})
            with self.assertRaises(ValueError):
                presets.verify(run, preset)
            registry.write_json(run / 'preset-proof.json', {'preset': 'native', 'revision': 'a', 'session_id': 'session'})
            self.assertEqual(presets.verify(run, preset, 'session')['preset'], 'native')
            with self.assertRaises(ValueError):
                presets.verify(run, preset, 'other-session')


if __name__ == '__main__':
    unittest.main(verbosity=2)
