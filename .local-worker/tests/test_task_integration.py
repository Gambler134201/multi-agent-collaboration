"""Controlled integration acceptance: ordered merges, real check commands, records."""
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import model_registry as registry
import task_integration as integration
import task_leases


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
    (path / 'a.txt').write_text('base a\n', encoding='utf-8')
    (path / 'shared.txt').write_text('base shared\n', encoding='utf-8')
    git(path, 'add', '-A')
    git(path, 'commit', '-m', 'baseline')
    return path


def make_task_branch(repo, branch, edits):
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


PY = sys.executable


class IntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='parallel-integration-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.enterContext(patch.object(registry, 'HERE', self.root))
        self.project = make_repo(self.root / 'project')
        self.baseline = git(self.project, 'rev-parse', 'HEAD').strip()

    def project_status(self):
        return git(self.project, 'status', '--porcelain')

    def test_passed_integration_merges_runs_checks_and_cleans_up(self):
        make_task_branch(self.project, 't1', {'a.txt': 'from t1\n'})
        make_task_branch(self.project, 't2', {'b.txt': 'from t2\n'})
        check = [PY, '-c',
                 'from pathlib import Path;'
                 'assert Path("a.txt").read_text() == "from t1\\n";'
                 'assert Path("b.txt").read_text() == "from t2\\n";'
                 'print("INTEGRATION_OK")']
        record = integration.run_integration(self.project, ['t1', 't2'], name='ok-run',
                                             baseline=self.baseline, checks=[check])
        self.assertEqual(record['verdict'], 'passed')
        self.assertTrue(all(step['merged'] for step in record['merges']))
        self.assertEqual(len(record['checks']), 1)
        self.assertEqual(record['checks'][0]['exit_code'], 0)
        self.assertIn('INTEGRATION_OK', record['checks'][0]['output_tail'])
        self.assertEqual(record['integration_copy']['disposition'], 'removed')
        self.assertIsNone(record['integration_copy']['path'])
        stored = integration.latest(self.project, 'ok-run')
        self.assertEqual(stored['verdict'], 'passed')
        self.assertEqual(stored['schema'], 1)
        self.assertEqual(task_leases.holders(), {})
        status = self.project_status()
        self.assertTrue(all(row.endswith('.model-desk/') for row in status.splitlines()))
        self.assertEqual((self.project / 'a.txt').read_text(encoding='utf-8'), 'base a\n')

    def test_check_failure_keeps_copy_and_records_evidence(self):
        make_task_branch(self.project, 't1', {'a.txt': 'from t1\n'})
        check = [PY, '-c', 'print("DEEP_CONFLICT_MARK"); import sys; sys.exit(3)']
        record = integration.run_integration(self.project, ['t1'], name='fail-run',
                                             baseline=self.baseline, checks=[check])
        self.assertEqual(record['verdict'], 'check_failed')
        self.assertEqual(record['checks'][0]['exit_code'], 3)
        self.assertIn('DEEP_CONFLICT_MARK', record['checks'][0]['output_tail'])
        self.assertEqual(record['integration_copy']['disposition'], 'kept_for_review')
        kept = Path(record['integration_copy']['path'])
        self.assertTrue(kept.is_dir())
        self.assertEqual(integration.latest(self.project, 'fail-run')['verdict'], 'check_failed')
        self.assertEqual(task_leases.holders(), {})
        self.assertEqual(git(kept, 'rev-parse', '--abbrev-ref', 'HEAD').strip(), 'HEAD')

    def test_record_false_does_not_create_project_records(self):
        make_task_branch(self.project, 't1', {'a.txt': 'from t1\n'})
        result = integration.run_integration(self.project, ['t1'], name='ephemeral',
                                             checks=[[PY, '-c', 'pass']], record=False)
        self.assertEqual(result['verdict'], 'passed')
        self.assertFalse((self.project / '.model-desk').exists())

    def test_merge_conflict_stops_sequence_and_skips_checks(self):
        make_task_branch(self.project, 't1', {'shared.txt': 'from t1\n'})
        make_task_branch(self.project, 't2', {'shared.txt': 'from t2\n'})
        check = [PY, '-c', 'raise SystemExit("checks must not run on a partial merge")']
        record = integration.run_integration(self.project, ['t1', 't2'], name='conflict-run',
                                             baseline=self.baseline, checks=[check])
        self.assertEqual(record['verdict'], 'merge_conflict')
        self.assertEqual(record['checks'], [])
        self.assertFalse(record['merges'][-1]['merged'])
        self.assertEqual(record['merges'][-1]['conflicts'], ['shared.txt'])
        self.assertEqual(record['integration_copy']['disposition'], 'kept_for_review')
        self.assertTrue(Path(record['integration_copy']['path']).is_dir())
        self.assertEqual(task_leases.holders(), {})

    def test_check_artifacts_keep_a_passed_copy_instead_of_forced_delete(self):
        make_task_branch(self.project, 't1', {'a.txt': 'from t1\n'})
        check = [PY, '-c', 'from pathlib import Path; Path("build-artifact.txt").write_text("obj")']
        record = integration.run_integration(self.project, ['t1'], name='artifact-run',
                                             baseline=self.baseline, checks=[check])
        self.assertEqual(record['verdict'], 'passed')
        self.assertEqual(record['integration_copy']['disposition'], 'kept_check_artifacts')
        self.assertTrue(Path(record['integration_copy']['path']).is_dir())

    def test_integrations_serialize_per_project(self):
        task_leases.acquire('integration', str(self.project.resolve()), 'someone-else')
        make_task_branch(self.project, 't1', {'a.txt': 'from t1\n'})
        with self.assertRaisesRegex(ValueError, '占用'):
            integration.run_integration(self.project, ['t1'], name='second',
                                        baseline=self.baseline, checks=[[PY, '-c', 'pass']])
        self.assertEqual(task_leases.holders()['integration:' + str(self.project.resolve())]['task_id'],
                         'someone-else')

    def test_inputs_are_validated_before_any_lease(self):
        make_task_branch(self.project, 't1', {'a.txt': 'from t1\n'})
        with self.assertRaisesRegex(ValueError, '分支不存在'):
            integration.run_integration(self.project, ['ghost'], name='ghost-run',
                                        baseline=self.baseline)
        with self.assertRaisesRegex(ValueError, '检查命令'):
            integration.run_integration(self.project, ['t1'], name='bad-checks',
                                        baseline=self.baseline, checks=['not-a-vector'])
        with self.assertRaisesRegex(ValueError, '集成名称'):
            integration.run_integration(self.project, ['t1'], name='bad name',
                                        baseline=self.baseline)
        self.assertEqual(task_leases.holders(), {})
        self.assertFalse((self.root / 'project-task-workspaces').exists())

    def test_check_modifying_tracked_results_cannot_claim_frozen_commit_passed(self):
        make_task_branch(self.project,'t1',{'a.txt':'from t1\n'})
        result=integration.run_integration(self.project,['t1'],name='changed-during-check',keep=True,
            checks=[[PY,'-c','from pathlib import Path; Path("a.txt").write_text("changed by check")']])
        self.assertEqual(result['verdict'],'check_failed')
        self.assertIn('修改了受版本管理',result['checks'][-1]['output_tail'])
        self.assertTrue(Path(result['integration_copy']['path']).exists())

    def test_clean_checkout_to_another_commit_during_check_is_not_a_pass(self):
        make_task_branch(self.project,'t1',{'a.txt':'from t1\n'})
        result=integration.run_integration(self.project,['t1'],name='head-changed',keep=True,
            checks=[['git','checkout','--detach',self.baseline]])
        self.assertEqual(result['verdict'],'check_failed')
        self.assertIn('HEAD',result['checks'][-1]['output_tail'])


if __name__ == '__main__':
    unittest.main()
