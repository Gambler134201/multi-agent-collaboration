"""Worktree isolation, dirty protection and controlled integration, offline only."""
import subprocess
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import task_workspace as workspace


def git(repo, *args):
    result = subprocess.run(['git', *args], cwd=str(repo), capture_output=True, text=True,
                            encoding='utf-8', errors='replace')
    assert result.returncode == 0, (args, result.stderr)
    return result.stdout


def make_repo(path):
    path = Path(path)
    path.mkdir(parents=True)
    git(path, 'init', '-b', 'main')
    git(path, 'config', 'user.name', 'fixture')
    git(path, 'config', 'user.email', 'fixture@example.com')
    (path / 'a.txt').write_text('one\n', encoding='utf-8')
    (path / 'shared.txt').write_text('base\n', encoding='utf-8')
    git(path, 'add', '-A')
    git(path, 'commit', '-m', 'baseline')
    return path


def make_task_branch(repo, branch, edits):
    """Fixture branch commits built in a throwaway worktree; the branch stays detached-safe."""
    tmp = repo.parent / ('fixture-' + branch.replace('/', '_'))
    git(repo, 'worktree', 'add', '--detach', str(tmp), 'HEAD')
    try:
        git(tmp, 'checkout', '-q', '--detach')
        for name, content in edits.items():
            target = tmp / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding='utf-8')
        git(tmp, 'add', '-A')
        git(tmp, 'commit', '-q', '-m', 'fixture ' + branch)
        git(tmp, 'branch', 'modeldesk-task/' + branch, 'HEAD')
    finally:
        git(repo, 'worktree', 'remove', str(tmp))


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='parallel-workspace-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.project = make_repo(self.root / 'project')

    def project_status(self):
        return workspace._git(self.project, 'status', '--porcelain').stdout

    def test_non_git_and_subdir_projects_stay_serial(self):
        plain = self.root / 'plain'
        plain.mkdir()
        info = workspace.inspect(plain)
        self.assertFalse(info['eligible'])
        self.assertIn('不自动初始化', info['reason'])
        nested = self.root / 'project' / 'sub'
        nested.mkdir()
        info = workspace.inspect(nested)
        self.assertFalse(info['eligible'])
        self.assertIn('仓库根目录', info['reason'])
        with self.assertRaises(ValueError):
            workspace.prepare(plain, 'task-plain')

    def test_prepare_isolates_at_baseline_and_spares_user_tree(self):
        (self.project / 'draft.txt').write_text('user draft\n', encoding='utf-8')
        info = workspace.inspect(self.project)
        self.assertTrue(info['eligible'])
        self.assertTrue(info['dirty'])
        prepared = workspace.prepare(self.project, '20261025-fixture-task')
        self.addCleanup(lambda p=prepared['root']: shutil.rmtree(p, ignore_errors=True))
        self.assertTrue(Path(prepared['path']).is_dir())
        self.assertEqual(prepared['root'], str(self.root / 'project-task-workspaces'))
        self.assertEqual(prepared['baseline'], info['baseline'])
        self.assertEqual(git(prepared['path'], 'rev-parse', 'HEAD').strip(), info['baseline'])
        self.assertEqual(git(self.project, 'rev-parse', '--verify', '-q',
                             'refs/heads/modeldesk-task/20261025-fixture-task').strip(), info['baseline'])
        self.assertIn('draft.txt', self.project_status())
        self.assertFalse((self.project / 'project-task-workspaces').exists())

    def test_double_prepare_branch_reuse_and_bad_identity_refused(self):
        first = workspace.prepare(self.project, '20261025-fixture-task')
        self.addCleanup(lambda p=first['root']: shutil.rmtree(p, ignore_errors=True))
        with self.assertRaisesRegex(ValueError, '工作区已存在'):
            workspace.prepare(self.project, '20261025-fixture-task')
        self.assertTrue(workspace.release(self.project, '20261025-fixture-task')['removed'])
        # The deliverable branch is kept after release, so the same task id
        # cannot silently open a second workspace.
        with self.assertRaisesRegex(ValueError, '分支已存在'):
            workspace.prepare(self.project, '20261025-fixture-task')
        for bad in ('../escape', 'space id', ''):
            with self.assertRaises(ValueError):
                workspace.prepare(self.project, bad)
        with self.assertRaisesRegex(ValueError, '基线'):
            workspace.prepare(self.project, '20261025-ghost', baseline='deadbeef' * 5)

    def test_files_dirty_limits_to_declared_paths(self):
        (self.project / 'a.txt').write_text('user wip\n', encoding='utf-8')
        (self.project / 'other.txt').write_text('other wip\n', encoding='utf-8')
        rows = workspace.files_dirty(self.project, ['a.txt'])
        self.assertEqual(len(rows), 1)
        self.assertIn('a.txt', rows[0])
        self.assertEqual(workspace.files_dirty(self.project, []), [])
        self.assertEqual(workspace.files_dirty(self.project, ['missing-and-clean.txt']), [])

    def test_execution_roots_split_record_from_workspace(self):
        prepared = workspace.prepare(self.project, '20261025-fixture-task')
        self.addCleanup(lambda p=prepared['root']: shutil.rmtree(p, ignore_errors=True))
        state = {'project': str(self.project), 'workspace': {'created': True, 'path': prepared['path']}}
        record, execution = workspace.execution_roots(state)
        self.assertEqual(record, self.project.resolve())
        self.assertEqual(execution, Path(prepared['path']).resolve())
        self.assertEqual(workspace.execution_roots({'project': str(self.project)}),
                         (self.project.resolve(), self.project.resolve()))
        with self.assertRaisesRegex(ValueError, '缺失'):
            workspace.execution_roots({'project': str(self.project),
                                       'workspace': {'created': True, 'path': str(self.root / 'gone')}})
        with self.assertRaisesRegex(ValueError, '假隔离'):
            workspace.execution_roots({'project': str(self.project),
                                       'workspace': {'created': True, 'path': str(self.project)}})

    def test_release_refuses_dirty_and_keeps_branch(self):
        prepared = workspace.prepare(self.project, '20261025-fixture-task')
        self.addCleanup(lambda p=prepared['root']: shutil.rmtree(p, ignore_errors=True))
        (Path(prepared['path']) / 'result.txt').write_text('deliverable\n', encoding='utf-8')
        state = workspace.status(self.project, '20261025-fixture-task')
        self.assertTrue(state['dirty'])
        with self.assertRaisesRegex(ValueError, '未提交成果'):
            workspace.release(self.project, '20261025-fixture-task')
        self.assertTrue(Path(prepared['path']).exists())
        self.assertTrue((Path(prepared['path']) / 'result.txt').exists())
        (Path(prepared['path']) / 'result.txt').unlink()
        released = workspace.release(self.project, '20261025-fixture-task')
        self.assertTrue(released['removed'])
        self.assertFalse(Path(prepared['path']).exists())
        self.assertEqual(git(self.project, 'rev-parse', '--verify', '-q',
                             'refs/heads/modeldesk-task/20261025-fixture-task').strip(),
                         prepared['baseline'])

    def test_stale_metadata_is_reported_then_pruned(self):
        prepared = workspace.prepare(self.project, '20261025-fixture-task')
        self.addCleanup(lambda p=prepared['root']: shutil.rmtree(p, ignore_errors=True))
        shutil.rmtree(prepared['path'])
        state = workspace.status(self.project, '20261025-fixture-task')
        self.assertFalse(state['exists'])
        self.assertTrue(state['stale'])
        released = workspace.release(self.project, '20261025-fixture-task')
        self.assertTrue(released['pruned'])
        self.assertFalse(workspace.status(self.project, '20261025-fixture-task')['stale'])

    def test_precheck_finds_conflicts_without_touching_any_tree(self):
        make_task_branch(self.project, 'fixture-t1', {'shared.txt': 'from t1\n'})
        make_task_branch(self.project, 'fixture-t2', {'shared.txt': 'from t2\n'})
        make_task_branch(self.project, 'fixture-t3', {'a.txt': 'from t3\n', 'new.txt': 'ok\n'})
        before = self.project_status()
        report = workspace.precheck_merge(self.project, ['fixture-t1', 'fixture-t2', 'fixture-t3'])
        self.assertEqual(self.project_status(), before)
        self.assertFalse((self.root / 'project-task-workspaces').exists())
        self.assertFalse(report['clean'])
        pairs = {tuple(pair['branches']): pair for pair in report['pairs']}
        self.assertEqual(len(pairs), 3)
        self.assertFalse(pairs[('fixture-t1', 'fixture-t2')]['clean'])
        self.assertEqual(pairs[('fixture-t1', 'fixture-t2')]['conflicts'], ['shared.txt'])
        self.assertTrue(pairs[('fixture-t1', 'fixture-t3')]['clean'])
        self.assertTrue(pairs[('fixture-t2', 'fixture-t3')]['clean'])
        with self.assertRaisesRegex(ValueError, '分支不存在'):
            workspace.precheck_merge(self.project, ['fixture-ghost', 'fixture-t1'])

    def test_trial_sequence_merges_clean_stops_on_conflict_and_cleans_up(self):
        make_task_branch(self.project, 'fixture-t1', {'a.txt': 'from t1\n', 'shared.txt': 't1 shared\n'})
        make_task_branch(self.project, 'fixture-t2', {'b.txt': 'from t2\n'})
        make_task_branch(self.project, 'fixture-t3', {'shared.txt': 't3 shared\n'})
        clean = workspace.trial_sequence(self.project, ['fixture-t1', 'fixture-t2'], name='seq-ok')
        self.assertTrue(clean['clean'])
        self.assertTrue(all(step['merged'] for step in clean['steps']))
        self.assertIsNone(clean['integration_path'])
        self.assertFalse((self.root / 'project-task-workspaces' / 'integration-seq-ok').exists())
        blocking = workspace.trial_sequence(self.project, ['fixture-t1', 'fixture-t3'], name='seq-bad')
        self.assertFalse(blocking['clean'])
        self.assertTrue(blocking['steps'][0]['merged'])
        self.assertFalse(blocking['steps'][1]['merged'])
        self.assertEqual(blocking['steps'][1]['conflicts'], ['shared.txt'])
        kept = workspace.trial_sequence(self.project, ['fixture-t1'], name='seq-keep', keep=True)
        self.assertTrue(kept['clean'])
        kept_path = Path(kept['integration_path'])
        self.assertTrue(kept_path.is_dir())
        (kept_path / 'stray.txt').write_text('pending\n', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, '未处理的合并状态'):
            workspace.trial_merge(kept_path, 'fixture-t2')
        with self.assertRaisesRegex(ValueError, '未提交内容'):
            workspace.remove_integration(self.project, 'seq-keep')
        (kept_path / 'stray.txt').unlink()
        removed = workspace.remove_integration(self.project, 'seq-keep')
        self.assertTrue(removed['removed'])

    def test_remove_integration_refuses_uncommitted_content(self):
        make_task_branch(self.project, 'fixture-t1', {'a.txt': 'from t1\n'})
        area = workspace.integration_workspace(self.project, 'dirty-area')
        self.addCleanup(lambda p=workspace.workspace_root(self.project): shutil.rmtree(p, ignore_errors=True))
        (Path(area['path']) / 'notes.txt').write_text('pending\n', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, '未提交内容'):
            workspace.remove_integration(self.project, 'dirty-area')
        self.assertTrue(Path(area['path']).exists())


if __name__ == '__main__':
    unittest.main()
