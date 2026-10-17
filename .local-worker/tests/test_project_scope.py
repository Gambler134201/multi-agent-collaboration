"""project_scope 的隔离测试：只用临时目录，不碰真实项目数据。

运行：
  .local-worker\\venv\\Scripts\\python.exe -m unittest discover -s .local-worker\\tests -p test_project_scope.py -v
"""
import base64
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import project_scope as scope


class TempTree(unittest.TestCase):
    """公共夹具：temp 根 + 中文空格项目 A、前缀兄弟 A2、子目录 A\\sub、普通文件。"""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix='ps_scope_')
        self.addCleanup(tmp.cleanup)
        self.root = tmp.name
        self.dir_a = os.path.join(self.root, '项目 A')
        self.dir_a2 = os.path.join(self.root, '项目 A2')
        self.dir_sub = os.path.join(self.dir_a, '子目录 sub')
        self.dir_missing = os.path.join(self.root, '不存在 目录')
        self.file_path = os.path.join(self.root, '文件.txt')
        for path in (self.dir_a, self.dir_a2, self.dir_sub):
            os.makedirs(path)
        with open(self.file_path, 'w', encoding='utf-8') as handle:
            handle.write('x')

    def slashes(self, path):
        return path.replace('\\', '/')

    def trailing(self, path):
        return path + os.sep


class NormalizeProjectTests(TempTree):
    def test_returns_resolved_display_str(self):
        got = scope.normalize_project(self.dir_a)
        self.assertIsInstance(got, str)
        self.assertEqual(got, os.path.realpath(self.dir_a))
        self.assertFalse(got.endswith(('/', '\\')))

    def test_separator_and_trailing_variants_same_display(self):
        base = scope.normalize_project(self.dir_a)
        for variant in (self.slashes(self.dir_a), self.trailing(self.dir_a),
                        os.path.join(self.dir_a, '.'), self.slashes(self.trailing(self.dir_a))):
            with self.subTest(variant=variant):
                self.assertEqual(scope.normalize_project(variant), base)

    def test_case_variants_follow_os(self):
        base = scope.normalize_project(self.dir_a)
        if os.name == 'nt':
            self.assertEqual(scope.normalize_project(self.dir_a.upper()), base)
            self.assertEqual(scope.normalize_project(self.dir_a.lower()), base)
        else:
            other = os.path.join(self.root, '项目 A-UPPER')
            os.makedirs(other)
            self.assertNotEqual(scope.normalize_project(other), base)

    def test_rejects_bad_values(self):
        bad = [None, 123, 1.5, True, [], {}, b'E:\\x', '', '   ', 'relative/dir', './x', '项目 A',
               'C:relative', 'http://127.0.0.1:8765/', 'https://example.com/path', 'file:///E:/x',
               'ftp://host/path', self.dir_a + '\x00', '\x00', self.dir_missing, self.file_path,
               self.dir_a + '\\后来 不存在']
        for value in bad:
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    scope.normalize_project(value)

    def test_does_not_create_missing_dir(self):
        with self.assertRaises(ValueError):
            scope.normalize_project(self.dir_missing)
        self.assertFalse(os.path.exists(self.dir_missing))

    def test_relative_path_never_defaults_to_cwd(self):
        old = os.getcwd()
        os.chdir(self.root)
        self.addCleanup(os.chdir, old)
        with self.assertRaises(ValueError):
            scope.normalize_project('项目 A')

    def test_url_like_path_is_not_created_or_accepted(self):
        with self.assertRaises(ValueError):
            scope.normalize_project('http://127.0.0.1:8765/')
        self.assertFalse(os.path.exists('http:'))


class ProjectKeyTests(TempTree):
    def test_identity_variants_share_one_key(self):
        key = scope.project_key(self.dir_a)
        self.assertIsInstance(key, str)
        variants = [self.dir_a, self.slashes(self.dir_a), self.trailing(self.dir_a),
                    os.path.join(self.dir_a, '.'), self.dir_a + os.sep + os.sep]
        if os.name == 'nt':
            variants += [self.dir_a.upper(), self.dir_a.lower(), self.slashes(self.dir_a.upper())]
        for variant in variants:
            with self.subTest(variant=variant):
                self.assertEqual(scope.project_key(variant), key)

    def test_sibling_prefix_and_parent_are_different_keys(self):
        key_a = scope.project_key(self.dir_a)
        for other in (self.dir_a2, self.dir_sub, self.root):
            with self.subTest(other=other):
                self.assertNotEqual(scope.project_key(other), key_a)

    def test_key_is_idempotent(self):
        key = scope.project_key(self.dir_a)
        self.assertEqual(scope.project_key(key), key)

    def test_rejects_invalid_project(self):
        for value in (self.dir_missing, 'relative/x', self.file_path, '', None, 5):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    scope.project_key(value)

    def test_real_alias_resolves_to_same_key(self):
        tmp = tempfile.TemporaryDirectory(prefix='ps_alias_')
        self.addCleanup(tmp.cleanup)
        base = os.path.join(tmp.name, '真实 目录')
        os.makedirs(base)
        link = os.path.join(tmp.name, 'alias')
        made = None
        try:
            os.symlink(base, link, target_is_directory=True)
            made = 'symlink'
        except OSError:
            if os.name == 'nt':
                try:
                    import _winapi
                    _winapi.CreateJunction(base, link)
                    made = 'junction'
                except Exception:
                    made = None
        if not made:
            self.skipTest('当前环境无法创建目录别名（符号链接/junction）')
        self.addCleanup(lambda: os.path.exists(link) and os.rmdir(link))
        self.assertEqual(scope.project_key(link), scope.project_key(base))
        self.assertEqual(scope.normalize_project(link), scope.normalize_project(base))


class ParseViewTests(TempTree):
    def test_none_defaults_to_unbound(self):
        self.assertEqual(scope.parse_view(None), {'mode': 'unbound', 'project': None})

    def test_project_view_canonicalizes_display(self):
        view = scope.parse_view({'mode': 'project', 'project': self.slashes(self.dir_a) + os.sep})
        self.assertEqual(sorted(view), ['mode', 'project'])
        self.assertEqual(view, {'mode': 'project', 'project': scope.normalize_project(self.dir_a)})

    def test_all_and_unbound_drop_project(self):
        for mode in ('all', 'unbound'):
            for raw in ({'mode': mode}, {'mode': mode, 'project': None}, {'mode': mode, 'project': ''}):
                with self.subTest(mode=mode, raw=raw):
                    self.assertEqual(scope.parse_view(raw), {'mode': mode, 'project': None})

    def test_rejects_bad_views(self):
        a = self.dir_a
        bad = [{'mode': 'project'}, {'mode': 'project', 'project': None}, {'mode': 'project', 'project': ''},
               {'mode': 'project', 'project': 123}, {'mode': 'project', 'project': ['x']},
               {'mode': 'project', 'project': {'p': a}}, {'mode': 'project', 'project': 'relative/x'},
               {'mode': 'project', 'project': self.dir_missing}, {'mode': 'project', 'project': self.file_path},
               {'mode': 'project', 'project': 'http://127.0.0.1:8765/'},
               {'mode': 'all', 'project': a}, {'mode': 'all', 'project': 'x'},
               {'mode': 'unbound', 'project': a}, {'mode': 'ALL'}, {'mode': ''}, {'mode': None},
               {'mode': 1}, {'mode': True}, {'mode': 'everything'}, {}, {'project': a},
               {'mode': 'all', 'extra': 1}, {'mode': 'all', 'Mode': 'project'},
               'all', ['all'], 5, 0, True, ('mode', 'all'), b'{"mode":"all"}']
        for raw in bad:
            with self.subTest(raw=raw):
                with self.assertRaises(ValueError):
                    scope.parse_view(raw)

    def test_input_not_mutated_and_result_independent(self):
        raw = {'mode': 'project', 'project': self.dir_a}
        view = scope.parse_view(raw)
        raw['mode'] = 'all'
        raw['project'] = self.dir_a2
        self.assertEqual(view, {'mode': 'project', 'project': scope.normalize_project(self.dir_a)})
        again = scope.parse_view({'mode': 'project', 'project': self.dir_a})
        self.assertEqual(view, again)
        self.assertIsNot(view, again)

    def test_two_views_share_no_state(self):
        view_a = scope.parse_view({'mode': 'project', 'project': self.dir_a})
        view_b = scope.parse_view({'mode': 'project', 'project': self.dir_a2})
        view_a['mode'] = 'all'
        view_a['project'] = 'tampered'
        self.assertEqual(view_b, {'mode': 'project', 'project': scope.normalize_project(self.dir_a2)})
        self.assertEqual(scope.parse_view({'mode': 'project', 'project': self.dir_a}),
                         {'mode': 'project', 'project': scope.normalize_project(self.dir_a)})
        self.assertIsNot(scope.parse_view(None), scope.parse_view(None))

    def test_module_has_no_current_project_state(self):
        leaked = [name for name in vars(scope)
                  if not name.startswith('_')
                  and any(token in name.lower() for token in ('current', 'default_project', 'cache'))]
        self.assertEqual(leaked, [])


class ViewHeaderTests(TempTree):
    def b64(self, payload):
        return base64.urlsafe_b64encode(payload).decode('ascii')

    def test_round_trip_all_modes(self):
        for raw in (None, {'mode': 'all'}, {'mode': 'unbound'}, {'mode': 'project', 'project': self.dir_a}):
            with self.subTest(raw=raw):
                expected = scope.parse_view(raw)
                header = scope.encode_view_header(raw)
                self.assertIsInstance(header, str)
                self.assertEqual(scope.decode_view_header(header), expected)

    def test_header_is_ascii_urlsafe_json(self):
        header = scope.encode_view_header({'mode': 'project', 'project': self.dir_a})
        self.assertTrue(header.isascii())
        self.assertNotIn('+', header)
        self.assertNotIn('/', header)
        decoded = json.loads(base64.b64decode(header, altchars=b'-_', validate=True).decode('utf-8'))
        self.assertEqual(decoded, {'mode': 'project', 'project': scope.normalize_project(self.dir_a)})

    def test_decode_none_is_legacy_no_header(self):
        self.assertIsNone(scope.decode_view_header(None))

    def test_encode_none_is_unbound(self):
        self.assertEqual(scope.decode_view_header(scope.encode_view_header(None)),
                         {'mode': 'unbound', 'project': None})

    def test_rejects_bad_headers(self):
        good = scope.encode_view_header({'mode': 'all'})
        bad = ['', '   ', '!!!!', 'a b', 'not-a-header', good[:-2], good + '!!', good[:-1] + 'A',
               self.b64(b'\xff\xfe'), self.b64(b'"all"'), self.b64(b'[]'), self.b64(b'null'),
               self.b64(b'{"mode":"all","project":"E:/x"}'), self.b64(b'{"mode":"bogus"}'),
               self.b64(b'{"mode":"all","extra":1}'), self.b64(b'{"mode":"project"}'),
               self.b64(b'{"mode":"project","project":"relative/x"}'), self.b64(b'{'),
               123, b'eyJtb2RlIjoiYWxsIn0=', {'mode': 'all'}]
        for value in bad:
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    scope.decode_view_header(value)

    def test_never_falls_back_to_all(self):
        for value in ('!!!!', 'garbage', self.b64(b'[]')):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    got = scope.decode_view_header(value)
                    self.assertNotIsInstance(got, dict)

    def test_length_limit_is_8192(self):
        self.assertEqual(scope.MAX_HEADER_CHARS, 8192)
        with self.assertRaises(ValueError):
            scope.decode_view_header('A' * (scope.MAX_HEADER_CHARS + 1))
        original = scope.MAX_HEADER_CHARS
        scope.MAX_HEADER_CHARS = 8
        try:
            with self.assertRaises(ValueError):
                scope.encode_view_header({'mode': 'project', 'project': self.dir_a})
        finally:
            scope.MAX_HEADER_CHARS = original
        self.assertLessEqual(len(scope.encode_view_header({'mode': 'project', 'project': self.dir_a})),
                             scope.MAX_HEADER_CHARS)

    def test_header_round_trip_survives_unicode_and_spaces(self):
        header = scope.encode_view_header({'mode': 'project', 'project': self.dir_sub})
        self.assertFalse(any(ch.isspace() for ch in header))
        self.assertEqual(scope.decode_view_header(header),
                         {'mode': 'project', 'project': scope.normalize_project(self.dir_sub)})


class BelongsToProjectTests(TempTree):
    def setUp(self):
        super().setUp()
        self.view_a = scope.parse_view({'mode': 'project', 'project': self.dir_a})
        self.view_all = scope.parse_view({'mode': 'all'})
        self.view_unbound = scope.parse_view({'mode': 'unbound'})

    def test_project_mode_exact_identity_only(self):
        hits = [{'project': self.dir_a}, {'project': self.slashes(self.dir_a)},
                {'project': self.trailing(self.dir_a)}]
        if os.name == 'nt':
            hits.append({'project': self.dir_a.upper()})
        for record in hits:
            with self.subTest(record=record):
                self.assertTrue(scope.belongs_to_project(record, self.view_a))

    def test_project_mode_excludes_prefix_parent_child(self):
        misses = [{'project': self.dir_a2}, {'project': self.dir_sub}, {'project': self.root},
                  {'project': self.dir_a + '-extra'}, {'project': self.dir_a[:-1]},
                  {'project': self.dir_missing}, {'project': 'relative/x'},
                  {'project': 'http://127.0.0.1:8765/'}, {'project': self.file_path}]
        for record in misses:
            with self.subTest(record=record):
                self.assertFalse(scope.belongs_to_project(record, self.view_a))

    def test_project_mode_excludes_broken_records(self):
        for record in ({}, {'project': None}, {'project': ''}, {'project': '   '}, {'project': 123},
                       {'project': ['x']}, {'project': {}}, {'PROJECT': self.dir_a}, [], 'x', None, 7):
            with self.subTest(record=record):
                self.assertFalse(scope.belongs_to_project(record, self.view_a))

    def test_all_includes_unbound_records(self):
        for record in ({'project': self.dir_a}, {'project': self.dir_a2}, {}, {'project': None}):
            with self.subTest(record=record):
                self.assertTrue(scope.belongs_to_project(record, self.view_all))
        for record in (None, 'x', 7, []):
            with self.subTest(record=record):
                self.assertFalse(scope.belongs_to_project(record, self.view_all))

    def test_unbound_is_always_false(self):
        for record in ({'project': self.dir_a}, {}, {'project': None}, None, 'x'):
            with self.subTest(record=record):
                self.assertFalse(scope.belongs_to_project(record, self.view_unbound))

    def test_view_and_record_accept_parsed_view(self):
        self.assertTrue(scope.belongs_to_project({'project': self.dir_a}, self.view_a))
        self.assertTrue(scope.belongs_to_project({'project': self.dir_a},
                                                 scope.parse_view(self.view_a)))

    def test_stale_view_project_is_rejected_not_guessed(self):
        tmp = tempfile.TemporaryDirectory(prefix='ps_gone_')
        self.addCleanup(tmp.cleanup)
        gone = os.path.join(tmp.name, '消失 项目')
        os.makedirs(gone)
        view = scope.parse_view({'mode': 'project', 'project': gone})
        shutil.rmtree(gone)
        with self.assertRaises(ValueError):
            scope.belongs_to_project({'project': gone}, view)
        with self.assertRaises(ValueError):
            scope.filter_records([{'project': gone}], view)


class FilterRecordsTests(TempTree):
    def records(self):
        return [{'id': 'a', 'project': self.dir_a},
                {'id': 'a-slash', 'project': self.slashes(self.dir_a)},
                {'id': 'a-child', 'project': self.dir_sub},
                {'id': 'a-prefix', 'project': self.dir_a2},
                {'id': 'a-parent', 'project': self.root},
                {'id': 'unbound'},
                {'id': 'none', 'project': None},
                {'id': 'broken', 'project': 'relative/x'}]

    def test_project_filter_keeps_exact_identity(self):
        view = scope.parse_view({'mode': 'project', 'project': self.dir_a})
        kept = scope.filter_records(self.records(), view)
        self.assertEqual([r['id'] for r in kept], ['a', 'a-slash'])

    def test_all_filter_keeps_every_record_dict(self):
        kept = scope.filter_records(self.records(), {'mode': 'all'})
        self.assertEqual(len(kept), len(self.records()))

    def test_unbound_filter_keeps_nothing(self):
        self.assertEqual(scope.filter_records(self.records(), {'mode': 'unbound'}), [])
        self.assertEqual(scope.filter_records(self.records(), None), [])

    def test_original_records_untouched(self):
        records = self.records()
        snapshot = json.dumps(records, sort_keys=True, ensure_ascii=False, default=str)
        view = scope.parse_view({'mode': 'project', 'project': self.dir_a})
        kept = scope.filter_records(records, view)
        self.assertEqual(json.dumps(records, sort_keys=True, ensure_ascii=False, default=str), snapshot)
        self.assertIsNot(kept, records)
        self.assertIs(kept[0], records[0])
        kept.append({'id': 'injected'})
        del kept[0]
        self.assertEqual(len(records), 8)
        self.assertEqual(json.dumps(records, sort_keys=True, ensure_ascii=False, default=str), snapshot)

    def test_accepts_iterables_and_rejects_non_iterables(self):
        view = {'mode': 'all'}
        self.assertEqual(len(scope.filter_records(tuple(self.records()), view)), 8)
        self.assertEqual(len(scope.filter_records(iter(self.records()), view)), 8)
        for bad in (None, 5, 'abc', b'abc'):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    scope.filter_records(bad, view)

    def test_two_views_no_shared_state(self):
        view_a = scope.parse_view({'mode': 'project', 'project': self.dir_a})
        view_b = scope.parse_view({'mode': 'project', 'project': self.dir_a2})
        records = self.records()
        first_a = scope.filter_records(records, view_a)
        first_b = scope.filter_records(records, view_b)
        view_a['mode'] = 'all'
        view_a['project'] = 'tampered'
        self.assertEqual([r['id'] for r in first_b], ['a-prefix'])
        self.assertEqual([r['id'] for r in scope.filter_records(records, view_b)], ['a-prefix'])
        self.assertEqual([r['id'] for r in scope.filter_records(records, view_b)], ['a-prefix'])
        self.assertEqual([r['id'] for r in scope.filter_records(records, {'mode': 'project', 'project': self.dir_a})],
                         ['a', 'a-slash'])

    def test_interleaved_projects_stay_stable(self):
        records = self.records()
        view_a = {'mode': 'project', 'project': self.dir_a}
        view_b = {'mode': 'project', 'project': self.dir_a2}
        for _ in range(3):
            self.assertEqual([r['id'] for r in scope.filter_records(records, view_a)], ['a', 'a-slash'])
            self.assertEqual([r['id'] for r in scope.filter_records(records, view_b)], ['a-prefix'])


# --------------------------------------------------- c3/c5: db scope and MCP

class DatabaseScopeCase(unittest.TestCase):
    """临时安装目录 + 两个项目；不接触真实记录、配置或费用总账。"""

    def setUp(self):
        import model_registry as registry
        self.registry = registry
        tmp = tempfile.TemporaryDirectory(prefix='ps_db_')
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self._saved = (registry.HERE, registry.REGISTRY, registry.RUNS)
        registry.HERE = self.root / 'install'
        registry.HERE.mkdir()
        registry.REGISTRY = registry.HERE / 'models.json'
        registry.RUNS = registry.HERE / 'runs'
        registry.RUNS.mkdir()
        self.project_a = self.root / '项目 A'
        self.project_b = self.root / '项目 B'
        self.project_a.mkdir()
        self.project_b.mkdir()
        self.addCleanup(self._restore)

    def _restore(self):
        self.registry.HERE, self.registry.REGISTRY, self.registry.RUNS = self._saved


class DatabaseScopeTests(DatabaseScopeCase):
    def setUp(self):
        super().setUp()
        import panel_server
        import project_storage
        import stage_store
        self.panel = panel_server
        self.storage = project_storage
        self.stage_store = stage_store

    def _task(self, task_id, project, status='completed'):
        run = self.registry.prepare_task_dir(task_id, project)
        self.registry.patch_task(run, id=task_id, project=str(project), status=status,
                                 kind='chat')
        return run

    def test_space_reads_only_the_explicit_project(self):
        self._task('t-a', self.project_a)
        self._task('t-b', self.project_b)
        report = self.panel.database_report({'mode': 'project', 'project': str(self.project_a)})
        self.assertEqual(report['scope'], 'project')
        self.assertEqual([row['path'] for row in report['databases']],
                         [str(self.storage.records_db(self.project_a))])
        self.assertTrue(report['databases'][0]['initialized'])

    def test_unbound_space_lists_only_the_legacy_store(self):
        self._task('t-a', self.project_a)
        report = self.panel.database_report({'mode': 'unbound'})
        self.assertEqual(report['scope'], 'unbound')
        for row in report['databases']:
            self.assertTrue(row['scope'] == 'legacy')
            self.assertTrue(row['path'].endswith('stages.db'))
        self.assertNotIn(str(self.storage.records_db(self.project_a)),
                         [row['path'] for row in report['databases']])

    def test_all_is_the_explicit_machine_wide_scope(self):
        self._task('t-a', self.project_a)
        self._task('t-b', self.project_b)
        report = self.panel.database_report({'mode': 'all'})
        self.assertEqual(report['scope'], 'all')
        self.assertEqual(sorted(row['path'] for row in report['databases']),
                         sorted([str(self.storage.records_db(self.project_a)),
                                 str(self.storage.records_db(self.project_b))]))

    def test_uninitialized_project_reports_state_and_is_never_created(self):
        report = self.panel.database_report({'mode': 'project', 'project': str(self.project_b)})
        row = report['databases'][0]
        self.assertFalse(row['initialized'])
        self.assertFalse(row['exists'])
        self.assertIn('尚未初始化', row['reason'])
        result = self.panel.compact_databases({'mode': 'project', 'project': str(self.project_b)})
        self.assertEqual(result['compacted'], [])
        self.assertFalse(result['initialized'])
        self.assertFalse((self.project_b / '.model-desk').exists())
        self.assertEqual(list(self.project_b.iterdir()), [])

    def test_unbound_compact_is_refused_instead_of_compacting_everything(self):
        self._task('t-a', self.project_a)
        with self.assertRaises(ValueError):
            self.panel.compact_databases({'mode': 'unbound'})
        with self.assertRaises(ValueError):
            self.panel.compact_databases(None)

    def _fill(self, project):
        import sqlite3
        db = self.storage.records_db(project)
        conn = sqlite3.connect(str(db), isolation_level=None)
        try:
            conn.execute('CREATE TABLE junk (value TEXT)')
            conn.executemany('INSERT INTO junk VALUES (?)', [('x' * 400,)] * 400)
            conn.execute('DELETE FROM junk')
        finally:
            conn.close()
        return db

    def test_project_compact_touches_only_the_bound_project(self):
        self._task('t-a', self.project_a)
        self._task('t-b', self.project_b)
        db_a = self._fill(self.project_a)
        db_b = self._fill(self.project_b)
        self.assertGreater(self.storage.db_space(db_b)['freelist_count'], 0)
        result = self.panel.compact_databases({'mode': 'project', 'project': str(self.project_a)})
        self.assertEqual([row['path'] for row in result['compacted']], [str(db_a)])
        self.assertEqual(self.storage.db_space(db_a)['freelist_count'], 0)
        self.assertGreater(self.storage.db_space(db_b)['freelist_count'], 0,
                           '未绑定的项目库不得被顺手压缩')

    def test_compact_respects_a_running_task_in_the_bound_project(self):
        self._task('t-a', self.project_a, status='running')
        self._task('t-b', self.project_b)
        with self.assertRaises(ValueError):
            self.panel.compact_databases({'mode': 'project', 'project': str(self.project_a)})
        result = self.panel.compact_databases({'mode': 'project', 'project': str(self.project_b)})
        self.assertEqual([row['path'] for row in result['compacted']],
                         [str(self.storage.records_db(self.project_b))])

    def test_responses_are_labelled_with_their_scope(self):
        import project_views
        view = {'mode': 'project', 'project': str(self.project_a)}
        shaped = project_views.project_response('/api/db/space', {'databases': []}, view)
        self.assertEqual(shaped['scope'], 'project')
        self.assertEqual(shaped['scoped_project'], str(self.storage.normalize(self.project_a)))
        shaped = project_views.project_response('/api/db/compact', {'compacted': []},
                                                {'mode': 'unbound'})
        self.assertEqual(shaped['scope'], 'unbound')
        self.assertIsNone(shaped['scoped_project'])


class McpDatabasePathTests(DatabaseScopeCase):
    def setUp(self):
        super().setUp()
        import codex_mcp_server
        self.desk = codex_mcp_server

    def test_db_paths_are_on_the_concrete_whitelist(self):
        self.assertEqual(self.desk.validate_request('/api/db/space', 'GET'), '/api/db/space')
        self.assertEqual(self.desk.validate_request('/api/db/compact', 'POST'), '/api/db/compact')
        rejected = [('POST', '/api/db/space'), ('GET', '/api/db/compact'),
                    ('GET', '/api/db/space/../compact'), ('GET', '/api/db/*'),
                    ('GET', '/api/db/space?x=1'), ('PUT', '/api/db/compact'),
                    ('GET', '/api/db'), ('POST', '/api/db/compact/../space')]
        for method, path in rejected:
            with self.subTest(path=path):
                with self.assertRaises(self.desk.PanelError):
                    self.desk.validate_request(path, method)

    def test_panel_call_forwards_the_project_view_offline(self):
        from unittest import mock
        seen = {}

        def fake_http(method, path, body=None, token=None, timeout=None, view=None):
            seen.update(method=method, path=path, view=view, token=token, body=body)
            return 200, {'ok': True}

        view = {'mode': 'project', 'project': str(self.project_a)}
        with mock.patch.object(self.desk, 'http_call', side_effect=fake_http), \
             mock.patch.object(self.desk, 'panel_token', return_value='tok'):
            get_result = self.desk.panel_call('/api/db/space', 'GET', view=view)
            post_result = self.desk.panel_call('/api/db/compact', 'POST', {}, view=view)
        self.assertEqual(get_result['status'], 200)
        self.assertEqual(post_result['status'], 200)
        self.assertEqual(seen['path'], '/api/db/compact')
        self.assertEqual(seen['method'], 'POST')
        self.assertEqual(seen['token'], 'tok')
        self.assertEqual(seen['view'], view)


if __name__ == '__main__':
    unittest.main(verbosity=2)
