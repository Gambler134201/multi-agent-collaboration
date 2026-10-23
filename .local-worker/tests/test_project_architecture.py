import copy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import project_map as maps
import project_architecture as explanations


class ArchitectureTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / 'entry.py').write_text('def enter(): pass\n', encoding='utf-8')
        maps._CACHE.clear()
        self.index = maps.query(str(self.root))
        self.doc = {'schema': 1, 'title': '问答项目', 'summary': '输入问题，返回有依据的回答。',
                    'snapshot_hash': self.index['index']['hash'],
                    'nodes': [{'id': 'entry', 'title': '接收问题', 'role': '用户入口',
                               'description': '接收输入并交给处理模块。', 'output': '待处理的问题',
                               'evidence': [{'path': 'entry.py', 'sha256': self.index['nodes'][0]['sha256']}]}],
                    'edges': [{'source': 'entry', 'target': 'entry', 'label': '再次提问', 'confidence': 'inferred'}]}

    def tearDown(self):
        maps._CACHE.clear()
        self.tmp.cleanup()

    def save(self, data=None):
        (self.root / explanations.FILENAME).write_text(json.dumps(data or self.doc, ensure_ascii=False), encoding='utf-8')

    def result(self, **kwargs):
        return maps.query(str(self.root), **kwargs)['architecture']

    def test_missing_is_read_only(self):
        before = list(self.root.iterdir())
        self.assertEqual(self.result()['state'], 'missing')
        self.assertEqual(before, list(self.root.iterdir()))

    def test_valid_explanation_is_project_authored_not_runtime_verification(self):
        self.save()
        result = self.result()
        self.assertEqual(result['state'], 'matched')
        self.assertEqual(result['provenance'], 'project_authored')
        self.assertEqual(result['nodes'][0]['evidence'][0]['state'], 'unchanged')
        self.assertEqual(result['edges'][0]['confidence'], 'inferred')
        self.assertNotIn('accepted', result)

    def test_same_time_size_code_change_marks_node_and_snapshot_stale(self):
        self.save()
        target = self.root / 'entry.py'
        stamp = target.stat()
        target.write_text('def other(): pass\n', encoding='utf-8')
        import os
        os.utime(target, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
        result = self.result()
        self.assertEqual(result['state'], 'stale')
        self.assertEqual(result['nodes'][0]['state'], 'stale')
        self.assertEqual(result['nodes'][0]['evidence'][0]['state'], 'changed')

    def test_metadata_only_update_bypasses_code_cache(self):
        self.save()
        first = self.result()
        self.doc['title'] = '新的中文标题'
        self.save()
        result = maps.query(str(self.root))
        self.assertTrue(result['index']['reused'])
        self.assertEqual(result['architecture']['title'], '新的中文标题')
        self.assertNotEqual(first['content_hash'], result['architecture']['content_hash'])
        (self.root / explanations.FILENAME).unlink()
        self.assertEqual(self.result()['state'], 'missing')

    def test_unindexed_reference_is_unknown_and_never_opened(self):
        self.doc['nodes'][0]['evidence'] = [{'path': 'not-scanned.py', 'sha256': 'a' * 64}]
        self.save()
        result = self.result()
        self.assertEqual(result['state'], 'unknown')
        self.assertEqual(result['nodes'][0]['evidence'][0]['state'], 'unindexed')
        self.assertFalse((self.root / 'not-scanned.py').exists())

    def test_query_selection_does_not_falsify_evidence_freshness(self):
        self.save()
        result = self.result(query='nothing', limit=1)
        self.assertEqual(result['state'], 'matched')

    def test_bad_schema_paths_counts_and_edges_fail_closed(self):
        variants = []
        for value in [True, 2, '1']:
            changed = copy.deepcopy(self.doc); changed['schema'] = value; variants.append(changed)
        for value in ['../entry.py', 'E:/entry.py', '/entry.py', 'a\\entry.py', './entry.py', 'entry.py:secret']:
            changed = copy.deepcopy(self.doc); changed['nodes'][0]['evidence'][0]['path'] = value; variants.append(changed)
        changed = copy.deepcopy(self.doc); changed['nodes'] *= 25; variants.append(changed)
        changed = copy.deepcopy(self.doc); changed['nodes'][0]['id'] = '<script>'; variants.append(changed)
        changed = copy.deepcopy(self.doc); changed['edges'][0]['target'] = 'unknown'; variants.append(changed)
        changed = copy.deepcopy(self.doc); changed['edges'][0]['confidence'] = 'verified'; variants.append(changed)
        changed = copy.deepcopy(self.doc); changed['nodes'][0]['evidence'] = []; variants.append(changed)
        for variant in variants:
            with self.subTest(variant=variant):
                self.save(variant)
                self.assertEqual(self.result()['state'], 'invalid')

    def test_duplicate_keys_oversize_and_unsafe_file_do_not_escape_errors(self):
        target = self.root / explanations.FILENAME
        for body in ['{"schema":1,"schema":1}', 'x' * (explanations.MAX_BYTES + 1), '{']:
            target.write_text(body, encoding='utf-8')
            self.assertEqual(self.result()['state'], 'invalid')
        self.save()
        original = maps._safe
        with mock.patch.object(maps, '_safe', side_effect=lambda root, path: False if path.name == explanations.FILENAME else original(root, path)):
            self.assertEqual(self.result()['state'], 'invalid')

    def test_other_project_cannot_inherit_explanation(self):
        self.save()
        with tempfile.TemporaryDirectory() as other:
            self.assertEqual(maps.query(other)['architecture']['state'], 'missing')

    def test_unknown_fields_are_not_returned_or_executed(self):
        self.doc['command'] = 'create surprise.py'
        self.doc['nodes'][0]['script'] = 'alert(1)'
        self.save()
        result = self.result()
        self.assertNotIn('command', result)
        self.assertNotIn('script', result['nodes'][0])
        self.assertFalse((self.root / 'surprise.py').exists())


if __name__ == '__main__':
    unittest.main()
