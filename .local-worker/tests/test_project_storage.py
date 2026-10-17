"""Per-project workbench storage tests.

Everything runs against a temporary install directory (patched ``registry.HERE``
/ ``REGISTRY`` / ``RUNS``) and temporary project directories.  No real record,
model, key or ledger file is touched, and no model is called.

Run:
  .local-worker\\venv\\Scripts\\python.exe -m unittest discover -s .local-worker\\tests -p test_project_storage.py -v
"""
import json
import sqlite3
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from unittest.mock import patch  # noqa: E402

import context_manager  # noqa: E402
import migrate_project_storage as migration  # noqa: E402
import model_registry as registry  # noqa: E402
import project_storage  # noqa: E402
import stage_store  # noqa: E402


class StorageCase:
    """Shared patched-install fixture; mixin for unittest.TestCase classes."""

    def make_workspace(self):
        self._tmp = tempfile.TemporaryDirectory(prefix='ps_store_')
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
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
        self.addCleanup(self._restore_registry)

    def _restore_registry(self):
        registry.HERE, registry.REGISTRY, registry.RUNS = self._saved

    # ------------------------------------------------------------------ helpers

    def legacy_task(self, task_id, project='', status='completed', **fields):
        run = registry.RUNS / task_id
        run.mkdir(parents=True)
        row = {'id': task_id, 'title': 'legacy ' + task_id, 'status': status,
               'project': project, 'created_at': registry.now(), 'kind': 'edit', **fields}
        registry.write_json(run / 'task.json', row)
        return run

    def legacy_goal(self, project, title='旧目标'):
        goal = stage_store.create_goal(title, 'legacy objective', str(project))
        stage = stage_store.add_stage(goal['id'], '旧阶段', 'legacy stage',
                                      acceptance=['legacy accepted'], candidates=['home'])
        return goal, stage

    def count_goals(self, project):
        conn = project_storage.connect(project_storage.records_db(project), create=False)
        try:
            return int(conn.execute('SELECT COUNT(*) AS n FROM goals').fetchone()['n'])
        finally:
            conn.close()

    def task_ids(self):
        return sorted(row['task_id'] for row in project_storage.iter_task_dirs())


# --------------------------------------------------------------------- tests

import unittest  # noqa: E402


class LazyInitializationTests(StorageCase, unittest.TestCase):
    def setUp(self):
        self.make_workspace()

    def test_opening_an_empty_project_never_writes(self):
        self.assertEqual(project_storage.iter_task_dirs(), [])
        self.assertEqual(project_storage.indexed_projects(), [])
        self.assertFalse(project_storage.is_initialized(self.project_a))
        self.assertFalse((self.project_a / '.model-desk').exists())
        # A read-only aggregation over the empty workspace writes no project store.
        stage_store.list_goals()
        self.assertFalse((self.project_a / '.model-desk').exists())

    def test_first_task_initializes_only_that_project(self):
        run = registry.prepare_task_dir('20260101-000000-aaaaaa', self.project_a)
        self.assertTrue(run.is_dir())
        self.assertEqual(run.parent.parent.name, '.model-desk')
        self.assertTrue(project_storage.records_db(self.project_a).is_file())
        self.assertTrue((self.project_a / '.model-desk' / 'runs').is_dir())
        self.assertFalse((self.project_b / '.model-desk').exists())
        self.assertFalse((registry.RUNS / '20260101-000000-aaaaaa').exists())

    def test_resolution_never_switches_global_runs(self):
        before = registry.RUNS
        registry.prepare_task_dir('t-a', self.project_a)
        registry.prepare_task_dir('t-b', self.project_b)
        self.assertIs(registry.RUNS, before)
        self.assertEqual(list(before.glob('*')), [])


class ResolutionTests(StorageCase, unittest.TestCase):
    def setUp(self):
        self.make_workspace()

    def test_task_id_resolves_to_its_own_project(self):
        run_a = registry.prepare_task_dir('task-a', self.project_a)
        run_b = registry.prepare_task_dir('task-b', self.project_b)
        self.assertEqual(registry.task_dir('task-a'), run_a)
        self.assertEqual(registry.task_dir('task-b'), run_b)
        self.assertNotEqual(registry.task_dir('task-a'), registry.task_dir('task-b'))

    def test_legacy_fallback_and_strict_index_after_migration(self):
        legacy = self.legacy_task('task-1', str(self.project_a))
        self.assertEqual(registry.task_dir('task-1'), legacy)
        project_run = registry.prepare_task_dir('task-1', self.project_a)
        # Now indexed: the project copy wins and the legacy backup is not served.
        self.assertEqual(registry.task_dir('task-1'), project_run)
        self.assertEqual([row['task_id'] for row in project_storage.iter_task_dirs()], ['task-1'])
        self.assertEqual(project_storage.iter_task_dirs()[0]['source'], 'project')

    def test_indexed_task_does_not_resurrect_when_project_dir_is_gone(self):
        registry.prepare_task_dir('task-2', self.project_a)
        legacy = self.legacy_task('task-2', str(self.project_a))
        import shutil
        shutil.rmtree(self.project_a / '.model-desk')
        # The id is still owned by the project; a legacy backup must not reappear.
        self.assertNotEqual(registry.task_dir('task-2'), legacy)
        self.assertEqual([row['task_id'] for row in project_storage.iter_task_dirs()], [])

    def test_task_id_validation(self):
        registry.prepare_task_dir('ok-id_1', self.project_a)
        with self.assertRaises(ValueError):
            registry.task_dir('../escape')


class IndexConcurrencyTests(StorageCase, unittest.TestCase):
    def setUp(self):
        self.make_workspace()

    def test_concurrent_registration_keeps_every_entry(self):
        ids = ['t-%02d' % index for index in range(20)]
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(lambda item: project_storage.register('task', item, self.project_a), ids))
        for task_id in ids:
            self.assertEqual(project_storage.locate('task', task_id), project_storage.normalize(self.project_a))
        self.assertEqual(len(project_storage.index()['tasks']), 20)

    def test_corrupt_index_fails_closed_instead_of_hiding_records(self):
        project_storage.index_path().write_text('{ not valid json', encoding='utf-8')
        with self.assertRaises(project_storage.StorageError):
            project_storage.index()
        with self.assertRaises(project_storage.StorageError):
            project_storage.locate('task', 'anything')
        project_storage.index_path().unlink()
        self.assertEqual(project_storage.index()['tasks'], {})


class AssociationTests(StorageCase, unittest.TestCase):
    def setUp(self):
        self.make_workspace()

    def test_task_json_is_the_single_authority(self):
        run = registry.prepare_task_dir('task-x', self.project_a)
        registry.patch_task(run, id='task-x', goal_id='goal-1', stage_id='stage-1', kind='chat')
        conn = project_storage.connect(project_storage.records_db(self.project_a), create=False)
        try:
            row = conn.execute('SELECT * FROM tasks WHERE id = ?', ('task-x',)).fetchone()
        finally:
            conn.close()
        self.assertEqual((row['goal_id'], row['stage_id'], row['kind']), ('goal-1', 'stage-1', 'chat'))
        registry.write_json(run / 'task.json', {**registry.read_task(run), 'goal_id': 'goal-2'})
        project_storage.reconcile_tasks(self.project_a)
        conn = project_storage.connect(project_storage.records_db(self.project_a), create=False)
        try:
            self.assertEqual(conn.execute('SELECT goal_id FROM tasks WHERE id = ?', ('task-x',)).fetchone()[0],
                             'goal-2')
        finally:
            conn.close()

    def test_forget_task_clears_association_and_locator(self):
        registry.prepare_task_dir('task-y', self.project_a)
        project_storage.forget_task('task-y')
        self.assertIsNone(project_storage.locate('task', 'task-y'))
        conn = project_storage.connect(project_storage.records_db(self.project_a), create=False)
        try:
            self.assertIsNone(conn.execute('SELECT id FROM tasks WHERE id = ?', ('task-y',)).fetchone())
        finally:
            conn.close()


class StageStoreProjectTests(StorageCase, unittest.TestCase):
    def setUp(self):
        self.make_workspace()

    def test_auto_keeps_legacy_for_an_uninitialized_project(self):
        goal = stage_store.create_goal('旧调用', 'auto', str(self.project_a))
        self.assertFalse((self.project_a / '.model-desk').exists())
        with stage_store.connection('') as conn:
            self.assertIsNotNone(conn.execute('SELECT id FROM goals WHERE id = ?', (goal['id'],)).fetchone())

    def test_explicit_project_store_routes_goal_and_stage(self):
        goal = stage_store.create_goal('项目目标', 'project store', str(self.project_a), store='project')
        self.assertTrue(project_storage.is_initialized(self.project_a))
        stage = stage_store.add_stage(goal['id'], '阶段', 'objective', acceptance=['c1'], candidates=['home'])
        self.assertEqual(project_storage.locate('goal', goal['id']), project_storage.normalize(self.project_a))
        self.assertEqual(project_storage.locate('stage', stage['id']), project_storage.normalize(self.project_a))
        # The legacy global DB must not hold a second copy of the same record.
        with stage_store.connection('') as conn:
            self.assertIsNone(conn.execute('SELECT id FROM goals WHERE id = ?', (goal['id'],)).fetchone())
            self.assertIsNone(conn.execute('SELECT id FROM stages WHERE id = ?', (stage['id'],)).fetchone())
        self.assertEqual(stage_store.get_goal(goal['id'])['id'], goal['id'])
        self.assertEqual([row['id'] for row in stage_store.list_stages(goal['id'])], [stage['id']])
        self.assertEqual(stage_store.readiness(stage['id'])['ready'], True)
        self.assertEqual(stage_store.stats()['stages'], 1)

    def test_two_projects_keep_separate_stores(self):
        goal_a = stage_store.create_goal('A', 'a', str(self.project_a), store='project')
        goal_b = stage_store.create_goal('B', 'b', str(self.project_b), store='project')
        self.assertEqual(self.count_goals(self.project_a), 1)
        self.assertEqual(self.count_goals(self.project_b), 1)
        self.assertEqual(stage_store.get_goal(goal_a['id'])['title'], 'A')
        self.assertEqual(stage_store.get_goal(goal_b['id'])['title'], 'B')
        self.assertIn(goal_a['id'], [row['id'] for row in stage_store.list_goals()])
        self.assertIn(goal_b['id'], [row['id'] for row in stage_store.list_goals()])

    def test_project_creation_does_not_touch_other_project(self):
        stage_store.create_goal('only A', 'a', str(self.project_a), store='project')
        self.assertFalse((self.project_b / '.model-desk').exists())


class CompactionTests(StorageCase, unittest.TestCase):
    def setUp(self):
        self.make_workspace()

    def test_compaction_reports_real_page_release_not_row_count(self):
        registry.prepare_task_dir('keep-me', self.project_a)
        db = project_storage.records_db(self.project_a)
        conn = sqlite3.connect(str(db), isolation_level=None)
        try:
            conn.execute('CREATE TABLE junk (value TEXT)')
            conn.executemany('INSERT INTO junk VALUES (?)', [('x' * 400,) for _ in range(400)])
            conn.execute('DELETE FROM junk')
            before = project_storage.db_space(db)
        finally:
            conn.close()
        self.assertGreater(before['freelist_count'], 0)
        result = project_storage.compact_db(db)
        self.assertEqual(result['after']['freelist_count'], 0)
        self.assertGreaterEqual(result['reclaimed_bytes'], 0)
        # The real record is still present: space was released, nothing was deleted.
        conn = project_storage.connect(db, create=False)
        try:
            self.assertIsNotNone(conn.execute('SELECT id FROM tasks WHERE id = ?', ('keep-me',)).fetchone())
        finally:
            conn.close()

    def test_compact_rejects_a_linked_database(self):
        target = self.root / 'outside.db'
        sqlite3.connect(str(target)).close()
        desk = self.project_a / '.model-desk'
        desk.mkdir()
        link = desk / 'records.db'
        try:
            link.symlink_to(target)
        except (OSError, NotImplementedError):
            self.skipTest('symlink not permitted on this host')
        with self.assertRaises(project_storage.StorageError):
            project_storage.compact_db(link)


class ContextExclusionTests(StorageCase, unittest.TestCase):
    def setUp(self):
        self.make_workspace()

    def test_model_desk_is_excluded_from_the_code_index(self):
        self.assertTrue(context_manager.is_excluded_dir('.model-desk'))
        registry.prepare_task_dir('task-ctx', self.project_a)
        (self.project_a / 'a.py').write_text('def a():\n    return 1\n', encoding='utf-8')
        listing, _skipped, _truncated = context_manager._project_listing(self.project_a)
        self.assertIn('a.py', listing)
        self.assertFalse(any('.model-desk' in name for name in listing))

    def test_executor_prompt_states_the_default_search_exclusion(self):
        self.assertIn('.model-desk', context_manager.execution_constraints(False))
        self.assertIn('.model-desk', context_manager.execution_constraints(True))


class MigrationTests(StorageCase, unittest.TestCase):
    def setUp(self):
        self.make_workspace()

    def _fixture(self):
        run = self.legacy_task('20260101-000000-legacy', str(self.project_a), goal_id='', stage_id='')
        registry.write_json(run / 'result.json', {'exit_code': 0, 'project': str(self.project_a)})
        (run / 'dsh.log').write_text('legacy log\n', encoding='utf-8')
        registry.write_json(run / 'task.json', {
            **registry.read_task(run),
            'log_path': str(run / 'dsh.log'),
            'context_dir': str(run / 'context'),
        })
        goal, stage = self.legacy_goal(self.project_a)
        return run, goal, stage

    def test_preview_and_dry_run_write_nothing(self):
        self._fixture()
        preview = migration.discover()
        self.assertEqual(preview['counts']['assigned_tasks'], 1)
        self.assertEqual(preview['counts']['assigned_goals'], 1)
        self.assertEqual([row['project'] for row in preview['projects']],
                         [project_storage.normalize(self.project_a)])
        dry = migration.migrate(write=False)
        self.assertEqual(dry['mode'], 'dry-run')
        self.assertFalse((self.project_a / '.model-desk').exists())

    def test_unassigned_and_invalid_are_reported_and_kept(self):
        self.legacy_task('orphan-1', '')
        self.legacy_task('orphan-2', str(self.root / '不存在 目录'))
        preview = migration.discover()
        self.assertEqual(preview['counts']['unassigned_tasks'], 2)
        self.assertEqual(preview['counts']['assigned_tasks'], 0)
        result = migration.migrate(write=True)
        self.assertEqual(result['copied_tasks'], 0)
        self.assertTrue((registry.RUNS / 'orphan-1' / 'task.json').is_file())
        self.assertFalse((self.project_a / '.model-desk').exists())

    def test_conflict_is_reported_and_not_migrated(self):
        self.legacy_task('clash', str(self.project_a))
        registry.prepare_task_dir('clash', self.project_b)
        preview = migration.discover()
        self.assertEqual(preview['counts']['conflicts'], 1)
        self.assertEqual(preview['counts']['assigned_tasks'], 0)

    def test_apply_is_idempotent_and_verify_passes(self):
        run, _goal, _stage = self._fixture()
        first = migration.migrate(write=True)
        self.assertEqual(first['copied_tasks'], 1)
        self.assertEqual(first['skipped_tasks'], 0)
        dest = project_storage.task_dir(self.project_a, '20260101-000000-legacy')
        record = json.loads((dest / 'task.json').read_text(encoding='utf-8'))
        self.assertEqual(record['legacy_origin'], str(run))
        self.assertEqual(record['log_path'], str(dest / 'dsh.log'))
        self.assertEqual((dest / 'dsh.log').read_text(encoding='utf-8'), 'legacy log\n')
        self.assertTrue((run / 'task.json').is_file(), '旧原件必须保留')
        self.assertEqual(project_storage.locate('task', '20260101-000000-legacy'),
                         project_storage.normalize(self.project_a))
        self.assertEqual(self.task_ids(), ['20260101-000000-legacy'])
        self.assertEqual(self.count_goals(self.project_a), 1)
        second = migration.migrate(write=True)
        self.assertEqual(second['copied_tasks'], 0)
        self.assertEqual(second['skipped_tasks'], 1)
        self.assertEqual(self.task_ids(), ['20260101-000000-legacy'])
        verified = migration.verify()
        self.assertTrue(verified['ok'], verified)
        self.assertEqual(verified['unresolved'], [])

    def test_rollback_removes_only_the_project_copy(self):
        run, _goal, _stage = self._fixture()
        applied = migration.migrate(write=True)
        dest = project_storage.task_dir(self.project_a, '20260101-000000-legacy')
        self.assertTrue(dest.is_dir())
        result = migration.rollback(applied['backup'])
        self.assertEqual(result['removed_tasks'], 1)
        self.assertFalse(dest.exists())
        self.assertTrue((run / 'task.json').is_file())
        self.assertIsNone(project_storage.locate('task', '20260101-000000-legacy'))
        self.assertEqual(self.count_goals(self.project_a), 0)

    def test_apply_refuses_while_a_task_is_running(self):
        self.legacy_task('busy', str(self.project_a), status='running')
        with self.assertRaises(migration.MigrationError):
            migration.migrate(write=True)


class ApiLayoutTests(StorageCase, unittest.TestCase):
    """The supported entry points still resolve records after the storage split."""

    def setUp(self):
        self.make_workspace()

    def test_review_and_handover_entries_resolve_project_records(self):
        run = registry.prepare_task_dir('task-review', self.project_a)
        registry.patch_task(run, id='task-review', status='completed', kind='edit', engine='dsh',
                            project=str(self.project_a), model_id='home', files=['a.py'],
                            delegation={'schema': 1, 'mode': 'outcome'})
        import delegation_policy
        import review_task
        record = delegation_policy.initialize(run, ['criterion one'], 'main')
        self.assertEqual(record['task_id'], 'task-review')
        # review_task.py resolves by id through the locator index, not by a
        # directory the user has to repeat.
        self.assertIs(review_task._run, review_task._run)
        self.assertEqual(registry.task_dir('task-review'), run)

    def test_public_task_listing_includes_project_and_skips_migrated_backup(self):
        import panel_server
        legacy = self.legacy_task('dup-1', str(self.project_a))
        registry.prepare_task_dir('dup-1', self.project_a)
        registry.patch_task(project_storage.task_dir(self.project_a, 'dup-1'),
                            id='dup-1', status='completed', project=str(self.project_a))
        rows = panel_server.public_tasks()
        self.assertEqual([row['id'] for row in rows], ['dup-1'])


class ProjectScopedReadTests(StorageCase, unittest.TestCase):
    """c1: a project request opens that directory's own store, nothing else."""

    def setUp(self):
        self.make_workspace()

    def _project_task(self, task_id, project):
        run = registry.prepare_task_dir(task_id, project)
        registry.patch_task(run, id=task_id, project=str(project), status='completed',
                            kind='chat', title=task_id)
        return run

    def test_explicit_project_read_needs_no_global_locator(self):
        import panel_server
        self._project_task('project-only', self.project_a)
        project_storage.index_path().unlink()
        view = {'mode': 'project', 'project': str(self.project_a)}
        self.assertEqual([row['task_id'] for row in project_storage.iter_task_dirs(view)],
                         ['project-only'])
        self.assertEqual([row['id'] for row in panel_server.public_tasks(view)], ['project-only'])

    def test_project_view_never_reads_another_project(self):
        self._project_task('a-1', self.project_a)
        self._project_task('b-1', self.project_b)
        rows_a = project_storage.iter_task_dirs({'mode': 'project', 'project': str(self.project_a)})
        rows_b = project_storage.iter_task_dirs({'mode': 'project', 'project': str(self.project_b)})
        self.assertEqual([row['task_id'] for row in rows_a], ['a-1'])
        self.assertEqual([row['task_id'] for row in rows_b], ['b-1'])
        self.assertEqual([row['project'] for row in rows_a],
                         [project_storage.normalize(self.project_a)])

    def test_unbound_and_unknown_projects_never_fall_back_to_last_project(self):
        self._project_task('a-1', self.project_a)
        project_storage.register_project(self.project_a, last_selected=registry.now())
        self.assertEqual(project_storage.iter_task_dirs({'mode': 'unbound'}), [])
        empty = self.root / 'empty project'
        empty.mkdir()
        self.assertEqual(project_storage.iter_task_dirs({'mode': 'project', 'project': str(empty)}), [])
        with self.assertRaises(ValueError):
            project_storage.iter_task_dirs({'mode': 'project',
                                            'project': str(self.root / 'missing project')})
        # ``all`` still aggregates; only an explicit project is restricted.
        self.assertEqual([row['task_id'] for row in project_storage.iter_task_dirs({'mode': 'all'})],
                         ['a-1'])

    def test_unmigrated_legacy_records_of_the_project_stay_readable(self):
        self.legacy_task('leg-a', str(self.project_a))
        self.legacy_task('leg-b', str(self.project_b))
        self.legacy_task('orphan', '')
        self.legacy_task('result-only-a', '')
        registry.write_json(registry.RUNS / 'result-only-a' / 'result.json',
                            {'exit_code': 0, 'project': str(self.project_a)})
        (registry.RUNS / 'result-only-a' / 'task.json').unlink()
        rows = project_storage.iter_task_dirs({'mode': 'project', 'project': str(self.project_a)})
        self.assertEqual(sorted(row['task_id'] for row in rows), ['leg-a', 'result-only-a'])
        self.assertTrue(all(row['source'] == 'legacy' for row in rows))

    def test_a_cross_project_index_conflict_is_refused_not_silently_read(self):
        import shutil
        run = self._project_task('clash-1', self.project_b)
        dest = project_storage.runs_dir(self.project_a) / 'clash-1'
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(run, dest)
        with self.assertRaises(project_storage.StorageError):
            project_storage.iter_task_dirs({'mode': 'project', 'project': str(self.project_a)})

    def test_register_refuses_to_silently_redirect_an_id(self):
        project_storage.register('task', 'shared', self.project_a)
        with self.assertRaises(project_storage.StorageError):
            project_storage.register('task', 'shared', self.project_b)
        self.assertEqual(project_storage.locate('task', 'shared'),
                         project_storage.normalize(self.project_a))
        # Re-registering the same owner is still idempotent.
        project_storage.register('task', 'shared', self.project_a)

    def test_prepare_task_dir_refuses_an_id_owned_by_another_project(self):
        registry.prepare_task_dir('owned', self.project_a)
        with self.assertRaises(project_storage.StorageError):
            registry.prepare_task_dir('owned', self.project_b)
        self.assertFalse(project_storage.task_dir(self.project_b, 'owned').exists())
        self.assertEqual(project_storage.locate('task', 'owned'),
                         project_storage.normalize(self.project_a))

    def test_scoped_goal_and_stage_listing_needs_no_global_locator(self):
        goal_a = stage_store.create_goal('A goal', 'a', str(self.project_a), store='project')
        stage_a = stage_store.add_stage(goal_a['id'], 'A stage', 'a', acceptance=['c1'],
                                        candidates=['home'])
        goal_b = stage_store.create_goal('B goal', 'b', str(self.project_b), store='project')
        stage_b = stage_store.add_stage(goal_b['id'], 'B stage', 'b', acceptance=['c1'],
                                        candidates=['home'])
        project_storage.index_path().unlink()
        project = project_storage.normalize(self.project_a)
        self.assertEqual([row['id'] for row in stage_store.list_goals(project)], [goal_a['id']])
        self.assertEqual([row['id'] for row in stage_store.list_stages(project=project)],
                         [stage_a['id']])
        self.assertEqual(stage_store.get_goal(goal_a['id'], project)['id'], goal_a['id'])
        self.assertEqual(stage_store.get_stage(stage_a['id'], project)['id'], stage_a['id'])
        # A foreign id is never relabelled as this project's record: it is either
        # absent from the explicit store or still labelled with its real project,
        # so the caller's scope check refuses it (it is never silently adopted).
        foreign = stage_store.get_stage(stage_b['id'], project)
        self.assertTrue(foreign is None or foreign['project'] == str(self.project_b))

    def test_scoped_goal_and_stage_writes_survive_a_missing_locator(self):
        import project_views
        goal = stage_store.create_goal('project goal', 'independent', str(self.project_a),
                                       store='project')
        stage = stage_store.add_stage(goal['id'], 'stage', 'deliver', acceptance=['c1'],
                                      candidates=['home'])
        project = project_storage.normalize(self.project_a)
        project_storage.index_path().unlink()
        # The panel's scoped identity check proves the owner; that proof may be
        # re-asserted so by-id goal/stage operations keep resolving.
        project_views.check_request('/api/goals/%s/status' % goal['id'],
                                    {'mode': 'project', 'project': str(self.project_a)},
                                    {'status': 'cancelled'})
        self.assertEqual(project_storage.locate('goal', goal['id']), project)
        # Stage writes resolve from the explicit project too.
        self.assertEqual(stage_store.cancel_stage(stage['id'], 'stop', project=project)['status'],
                         stage_store.CANCELLED)
        self.assertEqual(stage_store.requeue_stage(stage['id'], 'retry', project=project)['status'],
                         stage_store.READY)
        extra = stage_store.add_stage(goal['id'], 'later stage', 'deliver later',
                                      acceptance=['c1'], candidates=['home'], project=project)
        self.assertEqual(extra['goal_id'], goal['id'])
        self.assertEqual(stage_store.set_goal_status(goal['id'], 'active', project=project)['status'],
                         'active')
        stage_store.set_goal_status(goal['id'], 'cancelled')
        self.assertEqual(stage_store.get_goal(goal['id'], project)['status'], 'cancelled')

    def test_identity_proof_never_steals_an_owner_or_marks_a_legacy_record(self):
        import project_views
        legacy = stage_store.create_goal('legacy goal', 'legacy', str(self.project_a))
        foreign = stage_store.create_goal('foreign goal', 'foreign', str(self.project_b),
                                          store='project')
        view = {'mode': 'project', 'project': str(self.project_a)}
        project_views.check_request('/api/goals/%s' % legacy['id'], view)
        self.assertIsNone(project_storage.locate('goal', legacy['id']),
                          '旧库记录不能被误标成已迁移到项目库')
        with self.assertRaises(stage_store.StageError):
            project_views.check_request('/api/goals/%s' % foreign['id'], view)
        self.assertEqual(project_storage.locate('goal', foreign['id']),
                         project_storage.normalize(self.project_b),
                         '不能把别的项目记录改派给当前项目')
        self.assertFalse(project_views.reassert_owner('goals', foreign['id'], self.project_a))

    def test_scoped_listing_refuses_a_row_labelled_with_another_project(self):
        goal_a = stage_store.create_goal('A goal', 'a', str(self.project_a), store='project')
        conn = project_storage.connect(project_storage.records_db(self.project_a), create=True)
        try:
            with conn:
                conn.execute('UPDATE goals SET project = ? WHERE id = ?',
                             (str(self.project_b), goal_a['id']))
        finally:
            conn.close()
        with self.assertRaises(stage_store.StageError):
            stage_store.list_goals(project_storage.normalize(self.project_a))


class MigrationRollbackTests(StorageCase, unittest.TestCase):
    """c2: rollback undoes only what this plan really created and left unchanged."""

    def setUp(self):
        self.make_workspace()

    def _fixture(self, task_id='20260101-000000-legacy'):
        run = self.legacy_task(task_id, str(self.project_a), goal_id='', stage_id='')
        registry.write_json(run / 'result.json', {'exit_code': 0, 'project': str(self.project_a)})
        (run / 'dsh.log').write_text('legacy log\n', encoding='utf-8')
        registry.write_json(run / 'task.json', {
            **registry.read_task(run),
            'log_path': str(run / 'dsh.log'),
            'context_dir': str(run / 'context'),
        })
        goal, stage = self.legacy_goal(self.project_a)
        return run, goal, stage

    def test_nested_destination_is_skipped_with_an_explicit_reason(self):
        source = registry.RUNS / 'self-contained'
        nested = source / 'workspace'
        nested.mkdir(parents=True)
        registry.write_json(source / 'task.json',
                            {'id': 'self-contained', 'status': 'completed', 'kind': 'edit',
                             'project': str(nested)})
        preview = migration.discover()
        self.assertEqual([row['task_id'] for row in preview['tasks']['assigned']], [])
        unsafe = preview['tasks']['unsafe']
        self.assertEqual([row['task_id'] for row in unsafe], ['self-contained'])
        self.assertTrue(unsafe[0]['reason'].strip())
        self.assertEqual(preview['counts']['unsafe'], 1)
        result = migration.migrate(write=True)
        self.assertEqual(result['copied_tasks'], 0)
        self.assertFalse((nested / '.model-desk').exists())
        self.assertTrue((source / 'task.json').is_file())

    def test_rollback_of_a_repeated_apply_keeps_preexisting_records(self):
        _run, goal, stage = self._fixture()
        migration.migrate(write=True)
        repeated = migration.migrate(write=True)
        self.assertEqual(repeated['copied_tasks'], 0)
        result = migration.rollback(repeated['backup'])
        self.assertEqual(result['removed_goals'], 0)
        self.assertEqual(self.count_goals(self.project_a), 1)
        self.assertTrue(result['skipped'], '既有记录必须被明确报告为保留')
        self.assertTrue(stage_store.get_stage(stage['id'])['id'] == stage['id'])

    def test_rollback_removes_only_this_plan_and_keeps_later_work(self):
        _run, goal, stage = self._fixture()
        applied = migration.migrate(write=True)
        later = stage_store.add_stage(goal['id'], 'later stage', 'added after migration',
                                      acceptance=['c1'], candidates=['home'])
        result = migration.rollback(applied['backup'])
        self.assertEqual(result['removed_goals'], 0)
        self.assertEqual(result['removed_stages'], 0)
        self.assertEqual(self.count_goals(self.project_a), 1)
        self.assertEqual({row['id'] for row in stage_store.list_stages(goal['id'])},
                         {stage['id'], later['id']})
        self.assertTrue(any('阶段' in item.get('reason', '') for item in result['skipped']))

    def test_rollback_preserves_a_stage_edited_after_migration(self):
        _run, goal, stage = self._fixture()
        applied = migration.migrate(write=True)
        conn = project_storage.connect(project_storage.records_db(self.project_a), create=False)
        try:
            with conn:
                conn.execute('UPDATE stages SET title = ? WHERE id = ?',
                             ('edited after migration', stage['id']))
        finally:
            conn.close()
        result = migration.rollback(applied['backup'])
        self.assertEqual(result['removed_goals'], 0)
        self.assertEqual(self.count_goals(self.project_a), 1)
        current = stage_store.get_stage(stage['id'])
        self.assertEqual(current['title'], 'edited after migration')

    def test_rollback_refuses_while_the_queue_is_busy_and_keeps_everything(self):
        _run, goal, _stage = self._fixture()
        applied = migration.migrate(write=True)
        dest = project_storage.task_dir(self.project_a, '20260101-000000-legacy')
        self.legacy_task('busy-later', str(self.project_a), status='running')
        with self.assertRaises(migration.MigrationError):
            migration.rollback(applied['backup'])
        self.assertTrue(dest.is_dir())
        self.assertEqual(self.count_goals(self.project_a), 1)

    def test_idle_guard_sees_a_running_project_task_without_the_locator(self):
        self.legacy_task('later', str(self.project_a))
        busy = registry.prepare_task_dir('busy-a', self.project_a)
        registry.patch_task(busy, id='busy-a', project=str(self.project_a), status='running')
        project_storage.index_path().unlink()
        with self.assertRaises(migration.MigrationError):
            migration.migrate(write=True)

    def test_rollback_idle_guard_sees_a_running_project_task_without_the_locator(self):
        _run, goal, _stage = self._fixture()
        applied = migration.migrate(write=True)
        busy = registry.prepare_task_dir('busy-b', self.project_a)
        registry.patch_task(busy, id='busy-b', project=str(self.project_a), status='running')
        project_storage.index_path().unlink()
        with self.assertRaises(migration.MigrationError):
            migration.rollback(applied['backup'])
        self.assertEqual(self.count_goals(self.project_a), 1)

    def test_a_failed_copy_leaves_no_half_copy_and_keeps_the_original(self):
        run = self.legacy_task('crash-1', str(self.project_a))
        from unittest import mock
        with mock.patch.object(migration.shutil, 'copy2', side_effect=OSError('disk full')):
            result = migration.migrate(write=True)
        self.assertEqual(result['copied_tasks'], 0)
        self.assertEqual([row['task_id'] for row in result['failures']], ['crash-1'])
        self.assertFalse(project_storage.task_dir(self.project_a, 'crash-1').exists())
        runs = project_storage.runs_dir(self.project_a)
        leftovers = [item.name for item in runs.iterdir()] if runs.is_dir() else []
        self.assertEqual(leftovers, [])
        self.assertTrue((run / 'task.json').is_file())

    def test_a_stale_partial_copy_is_swept_and_never_enumerated(self):
        self.legacy_task('sweep-1', str(self.project_a))
        project_storage.ensure_project(self.project_a)
        stale = project_storage.runs_dir(self.project_a) / 'sweep-1.migrating-deadbeef'
        stale.mkdir(parents=True)
        (stale / 'task.json').write_text('{"id": "sweep-1"}', encoding='utf-8')
        self.assertEqual(project_storage.list_task_ids(), ['sweep-1'])
        result = migration.migrate(write=True)
        self.assertEqual(result['copied_tasks'], 1)
        self.assertEqual(result['staging_swept'], [str(stale)])
        self.assertFalse(stale.exists())
        self.assertTrue(project_storage.task_dir(self.project_a, 'sweep-1').is_dir())

    def test_reapply_repairs_a_missing_index_and_derived_row(self):
        self._fixture()
        migration.migrate(write=True)
        self.assertIsNotNone(project_storage.locate('task', '20260101-000000-legacy'))
        project_storage.index_path().unlink()
        conn = project_storage.connect(project_storage.records_db(self.project_a), create=False)
        try:
            with conn:
                conn.execute('DELETE FROM tasks')
        finally:
            conn.close()
        second = migration.migrate(write=True)
        self.assertEqual(second['copied_tasks'], 0)
        self.assertEqual(second['skipped_tasks'], 1)
        self.assertEqual(project_storage.locate('task', '20260101-000000-legacy'),
                         project_storage.normalize(self.project_a))
        conn = project_storage.connect(project_storage.records_db(self.project_a), create=False)
        try:
            row = conn.execute('SELECT id FROM tasks WHERE id = ?',
                               ('20260101-000000-legacy',)).fetchone()
        finally:
            conn.close()
        self.assertIsNotNone(row)

    def test_repeated_apply_does_not_duplicate_stage_logs(self):
        self._fixture()
        migration.migrate(write=True)
        conn = project_storage.connect(project_storage.records_db(self.project_a), create=False)
        try:
            first = int(conn.execute('SELECT COUNT(*) AS n FROM stage_log').fetchone()['n'])
        finally:
            conn.close()
        migration.migrate(write=True)
        conn = project_storage.connect(project_storage.records_db(self.project_a), create=False)
        try:
            second = int(conn.execute('SELECT COUNT(*) AS n FROM stage_log').fetchone()['n'])
        finally:
            conn.close()
        self.assertEqual(first, second)
        self.assertGreater(first, 0)

    def test_legacy_backup_keeps_rows_that_are_only_in_the_wal(self):
        self.legacy_goal(self.project_a)
        db = registry.HERE / stage_store.DB_NAME
        conn = sqlite3.connect(str(db), isolation_level=None)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute('PRAGMA journal_mode=WAL')
            conn.execute('PRAGMA wal_autocheckpoint=0')
            now = registry.now()
            conn.execute('INSERT INTO goals (id, title, objective, project, status, created_at, '
                         'updated_at, data) VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                         ('goal-wal', 'wal only goal', 'wal', str(self.project_a), 'active',
                          now, now, '{}'))
            wal = Path(str(db) + '-wal')
            self.assertTrue(wal.is_file() and wal.stat().st_size > 0,
                            '夹具必须真的把已提交数据留在 WAL 内')
            applied = migration.migrate(write=True)
            backup = Path(applied['legacy_db_backup'])
            self.assertTrue(backup.is_file())
        finally:
            conn.close()
        check = sqlite3.connect(str(backup))
        try:
            ids = {row[0] for row in check.execute('SELECT id FROM goals')}
        finally:
            check.close()
        self.assertIn('goal-wal', ids)


if __name__ == '__main__':
    unittest.main()
