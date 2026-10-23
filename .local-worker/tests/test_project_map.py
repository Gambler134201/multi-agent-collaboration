import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import project_map as maps


class MapTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='project-map-')
        self.root = Path(self.temp.name)
        maps._CACHE.clear()

    def tearDown(self):
        maps._CACHE.clear()
        self.temp.cleanup()

    def write(self, name, text):
        target = self.root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding='utf-8')
        return target

    def test_actual_symbols_relative_aliases_and_test_evidence(self):
        self.write('pkg/__init__.py', '')
        self.write('pkg/a.py', 'from . import b as helper\nfrom .b import run as r\nimport os\n')
        self.write('pkg/b.py', 'def run(value=3):\n return value\n')
        self.write('tests/test_a.py', 'from pkg import a\n')
        data = maps.query(str(self.root))
        edges = {(edge['source'], edge['target']) for edge in data['edges']}
        self.assertIn(('pkg/a.py', 'pkg/b.py'), edges)
        self.assertIn(('tests/test_a.py', 'pkg/a.py'), edges)
        nodes = {node['path']:node for node in data['nodes']}
        self.assertEqual(nodes['pkg/b.py']['symbols'][0]['name'], 'run')
        self.assertEqual(nodes['pkg/b.py']['symbols'][0]['line'], 1)
        self.assertEqual(nodes['pkg/a.py']['tests'], ['tests/test_a.py'])
        self.assertEqual(nodes['pkg/b.py']['tests'], [])
        self.assertIn('os', [item['module'] for item in data['unresolved']])
        self.assertFalse((self.root / '.model-desk').exists())

    def test_empty_project_has_no_disk_effect_and_caches_are_isolated(self):
        result = maps.query(str(self.root))
        self.assertEqual(result['nodes'], [])
        self.assertEqual(list(self.root.iterdir()), [])
        a = self.root / 'a'
        b = self.root / 'b'
        a.mkdir(); b.mkdir()
        (a / 'a.py').write_text('def only_a(): pass')
        (b / 'b.py').write_text('def only_b(): pass')
        self.assertEqual([node['path'] for node in maps.query(str(a))['nodes']], ['a.py'])
        self.assertEqual([node['path'] for node in maps.query(str(b))['nodes']], ['b.py'])

    def test_hash_revalidation_handles_same_size_same_mtime_add_and_delete(self):
        file = self.write('one.py', 'def first(): pass')
        stamp = file.stat()
        original = maps.query(str(self.root))
        self.assertTrue(maps.query(str(self.root))['index']['reused'])
        file.write_text('def other(): pass')
        os.utime(file, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
        changed = maps.query(str(self.root))
        self.assertNotEqual(original['index']['hash'], changed['index']['hash'])
        self.assertEqual(changed['nodes'][0]['symbols'][0]['name'], 'other')
        second = self.write('two.py', '')
        self.assertEqual(len(maps.query(str(self.root))['nodes']), 2)
        second.unlink()
        self.assertEqual(len(maps.query(str(self.root))['nodes']), 1)
        self.assertFalse(maps.query(str(self.root), refresh=True)['index']['reused'])

    def test_exclusions_and_links_are_never_read(self):
        self.write('safe.py', 'def safe(): pass')
        for path in ('node_modules/a.py', '.git/a.py', '.model-desk/a.py', '.workbuddy/a.py', '.GIT/hidden.py',
                     '.local-worker/tasks/a.py', '.local-worker/desktop-integrations/a.py',
                     'credentials.py', '.env.py', 'secrets/a.py'):
            self.write(path, 'TOP_SECRET = "fixture"')
        target = self.write('outside/real.py', 'def linked(): pass')
        try:
            (self.root / 'linked.py').symlink_to(target)
        except OSError:
            pass
        result = maps.query(str(self.root))
        names = [node['path'] for node in result['nodes']]
        self.assertIn('safe.py', names)
        self.assertNotIn('linked.py', names)
        self.assertTrue(all(not name.startswith(('.git/', '.model-desk/', '.workbuddy/', 'secrets/', 'node_modules/', '.local-worker/')) for name in names))
        self.assertNotIn('TOP_SECRET', json.dumps(result))

    def test_unsafe_input_and_protected_roots_refuse_without_fallback(self):
        self.write('a.py', '')
        for file in ('../a.py', 'C:\\outside.py', '/outside.py', '..\\a.py', 'a\0.py'):
            with self.subTest(file=file), self.assertRaises(ValueError):
                maps.query(str(self.root), file=file)
        for values in ({'refresh':1}, {'limit':True}, {'limit':0}, {'query':[]}, {'limit':301}):
            with self.assertRaises(ValueError):
                maps.query(str(self.root), **values)
        protected = self.root / '.model-desk'
        protected.mkdir()
        with self.assertRaises(ValueError):
            maps.query(str(protected))
        with self.assertRaises(ValueError):
            maps.query('relative')

    def test_dynamic_syntax_failure_and_other_languages_are_explicit(self):
        self.write('dynamic.py', 'import importlib\nimportlib.import_module("private_value")\n')
        self.write('bad.py', 'def broken(')
        self.write('web.ts', 'export function view(value="PRIVATE_LITERAL") {}')
        self.write('tools.ps1', 'function Invoke-Probe { Write-Output "PRIVATE_PS_LITERAL" }')
        result = maps.query(str(self.root))
        nodes = {node['path']:node for node in result['nodes']}
        self.assertTrue(nodes['bad.py']['degraded'])
        self.assertEqual(nodes['bad.py']['reason'], 'parse_failed')
        self.assertEqual(nodes['web.ts']['extraction'], 'regex')
        self.assertEqual(nodes['tools.ps1']['symbols'][0]['name'], 'Invoke-Probe')
        self.assertIn('dynamic_import_not_resolved', [item['reason'] for item in result['unresolved']])
        self.assertNotIn('private_value', json.dumps(result))
        self.assertNotIn('PRIVATE_LITERAL', json.dumps(result))
        self.assertNotIn('PRIVATE_PS_LITERAL', json.dumps(result))

    def test_ambiguous_module_and_sibling_candidate_are_labelled(self):
        self.write('both.py', '')
        self.write('both/__init__.py', '')
        self.write('main.py', 'import both\n')
        self.write('tools/cli.py', 'import helper\n')
        self.write('tools/helper.py', '')
        result = maps.query(str(self.root))
        self.assertIn('ambiguous', [item['reason'] for item in result['unresolved']])
        self.assertTrue(any(edge['resolution'] == 'sibling_candidate' for edge in result['edges']))

    def test_bounded_scans_symbol_search_and_neighbors(self):
        self.write('a.py', 'import b\ndef find_me(): pass')
        self.write('b.py', '')
        self.write('c.py', '')
        self.assertEqual([node['path'] for node in maps.query(str(self.root), query='find_me')['nodes']], ['a.py'])
        self.assertEqual({node['path'] for node in maps.query(str(self.root), file='a.py')['nodes']}, {'a.py','b.py'})
        self.assertTrue(maps.query(str(self.root), limit=1)['selection']['truncated'])
        with mock.patch.object(maps, 'MAX_FILES', 1):
            limited = maps.query(str(self.root), limit=1)
        self.assertEqual(len(limited['nodes']), 1)
        self.assertTrue(limited['limits']['files_truncated'])
        with mock.patch.object(maps, 'MAX_FILE_BYTES', 2):
            limited = maps.query(str(self.root))
        self.assertTrue(limited['limits']['bytes_truncated'])

    def test_no_project_code_execution_or_mutable_cache_leak(self):
        self.write('danger.py', 'raise RuntimeError("DO NOT EXECUTE")\ndef f(): pass')
        result = maps.query(str(self.root))
        result['nodes'].clear()
        self.assertEqual(len(maps.query(str(self.root))['nodes']), 1)


if __name__ == '__main__':
    unittest.main()
