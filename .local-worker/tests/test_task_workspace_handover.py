"""Handover evidence for isolated task worktrees, offline only.

Every scenario runs against throwaway Git repositories and temporary task
records under a patched registry: no model call, no real task records, no
panel service and no shared runtime state.  The original project tree and the
task worktree deliberately hold different content for the same file, so any
silent fallback to the original tree would show up as a wrong digest.
"""
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import delegation_policy as policy
import model_registry as registry
import stage_store
import task_handover
import task_workspace as workspace
import task_finalization

ORIGINAL = 'def one():\n    return 1\n'
DELIVERABLE = 'def one():\n    return 42\n'
ORIGINAL_SHA = hashlib.sha256(ORIGINAL.encode()).hexdigest()
DELIVERABLE_SHA = hashlib.sha256(DELIVERABLE.encode()).hexdigest()


def git(repo, *args):
    result = subprocess.run(['git', *args], cwd=str(repo), capture_output=True, text=True,
                            encoding='utf-8', errors='replace')
    assert result.returncode == 0, (args, result.stderr)
    return result.stdout


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class Base(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='parallel-handover-')
        self.addCleanup(self.temp.cleanup)
        self.saved = registry.HERE, registry.REGISTRY, registry.RUNS
        registry.HERE = Path(self.temp.name)
        registry.REGISTRY = registry.HERE / 'models.json'
        registry.RUNS = registry.HERE / 'runs'
        self.addCleanup(self.restore_registry)
        self.root = Path(self.temp.name)

    def restore_registry(self):
        registry.HERE, registry.REGISTRY, registry.RUNS = self.saved

    def make_repo(self, name='project'):
        path = self.root / name
        if (path / '.git').is_dir():
            return path
        path.mkdir(parents=True)
        git(path, 'init', '-b', 'main')
        git(path, 'config', 'user.name', 'fixture')
        git(path, 'config', 'user.email', 'fixture@example.com')
        (path / 'a.py').write_bytes(ORIGINAL.encode('utf-8'))
        git(path, 'add', '-A')
        git(path, 'commit', '-m', 'baseline')
        return path

    def task(self, name, project, files=('a.py',), **fields):
        run = registry.prepare_task_dir(name, project)
        row = {'id': name, 'status': 'completed', 'kind': 'edit', 'engine': 'dsh',
               'model_id': 'home', 'model': 'qwen-test', 'project': str(project),
               'files': list(files), 'changed_files': list(files), 'model_calls': 0,
               'route': {'mode': 'manual', 'requirements': {'acceptance': ['A']}}}
        row.update(fields)
        row.setdefault('delegation', {'schema': 1, 'mode': 'outcome', 'rework_count': 0,
                                      'inherited_rework_count': 0})
        registry.patch_task(run, **row)
        return run

    def accept(self, run):
        policy.record_executor(run, {'completed': True, 'changed_files': ['a.py'],
                                     'test': {'exit_code': 0, 'output': 'ok'}})
        record = policy.load(run)
        rows = [{'id': item['id'], 'status': 'pass',
                 'evidence': [{'source': 'reviewer_test', 'detail': 'independent fixture run',
                               'exit_code': 0}]}
                for item in record['criteria']]
        return policy.accept(run, 'main', rows, 'independent verification')

    def isolated_task(self, name='iso-task', project=None, deliverable=DELIVERABLE,
                      with_commit=True, task_fields=None, **plan_overrides):
        """An accepted task whose plan points at a real worktree (and commit)."""
        project = project or self.make_repo()
        run = self.task(name, project,**(task_fields or {}))
        prepared = workspace.prepare(project, name)
        self.addCleanup(lambda p=prepared['root']: shutil.rmtree(p, ignore_errors=True))
        if deliverable is not None:
            (Path(prepared['path']) / 'a.py').write_bytes(deliverable.encode('utf-8'))
        plan = {'eligible': True, 'reason': '', 'baseline': prepared['baseline'],
                'created': True, 'path': prepared['path'], 'branch': prepared['branch']}
        committed = workspace.commit_task_results(prepared['path'], name)
        plan['commit'] = committed.get('commit') or git(prepared['path'],'rev-parse','HEAD').strip()
        registry.patch_task(run, workspace=plan)
        self.accept(run)
        if not with_commit:plan.pop('commit',None)
        plan.update(plan_overrides)
        registry.patch_task(run,workspace=plan)
        return run, project, plan


class IsolatedHandoverTests(Base):
    def test_crlf_live_bytes_and_absolute_scope_remain_verifiable(self):
        project=self.make_repo()
        (project/'.gitattributes').write_bytes(b'*.py text eol=crlf\n')
        git(project,'add','.gitattributes')
        git(project,'commit','-m','line ending fixture')
        run,_,plan=self.isolated_task(name='crlf',project=project,deliverable=DELIVERABLE.replace('\n','\r\n'),
            task_fields={'files':[str(project/'a.py')]})
        registry.patch_task(run,files=[str(project/'a.py')],changed_files=[str(project/'a.py')])
        self.assertEqual(git(Path(plan['path']),'status','--porcelain','--','a.py'),'')
        record=task_handover.create(run,files=[str(project/'a.py')])
        self.assertEqual(record['files'][0]['path'],'a.py')
        self.assertEqual(record['files'][0]['sha256'],sha(Path(plan['path'])/'a.py'))
        self.assertTrue(task_handover.verify(run)['valid'])

    def test_forged_anchor_and_stale_commit_and_identity_are_rejected(self):
        run,project,plan=self.isolated_task(name='anchor')
        record=task_handover.create(run)
        foreign=self.root/'foreign'
        foreign.mkdir()
        (foreign/'a.py').write_bytes(DELIVERABLE.encode())
        for field,value in [('path',str(foreign)),('branch','modeldesk-task/foreign'),('commit',plan['baseline'])]:
            payload={**record,'workspace':{**record['workspace'],field:value}}
            self.assertFalse(task_handover.verify(run,payload)['valid'])
        for field in ('task_id','project','stage_id','goal_id'):
            self.assertFalse(task_handover.verify(run,{**record,field:'forged'})['valid'])
        registry.patch_task(run,workspace={**plan,'commit':plan['baseline']})
        with self.assertRaisesRegex(ValueError,'提交'):
            task_handover.create(run)

    def test_uncommitted_change_and_unsettled_controller_block_handover(self):
        run,_,plan=self.isolated_task(name='settlement')
        task_handover.create(run)
        registry.patch_task(run,controller_finalization=task_finalization.pending())
        with self.assertRaisesRegex(ValueError,'收尾尚未确认'):task_handover.create(run)
        with self.assertRaisesRegex(ValueError,'收尾尚未确认'):task_handover.verify(run)
        task_finalization.settle(run)
        (Path(plan['path'])/'a.py').write_text('changed after commit\n',encoding='utf-8')
        with self.assertRaisesRegex(ValueError,'成果提交与真实文件不一致'):task_handover.create(run)

    def test_create_and_verify_use_deliverable_commit_not_original_tree(self):
        run, project, plan = self.isolated_task()
        record = task_handover.create(run, summary='task rewrote one()',
                                      test_evidence=[{'name': 'ut', 'source': 'reviewer_test',
                                                      'exit_code': 0}])
        # Evidence comes from the deliverable copy, never from the original tree.
        self.assertEqual(record['files'][0]['sha256'], DELIVERABLE_SHA)
        self.assertNotEqual(record['files'][0]['sha256'], sha(project / 'a.py'))
        self.assertTrue(record['verification']['files_verified'])
        # The handover binds the original project, the task and the workspace identity.
        self.assertEqual(record['project'], str(project))
        self.assertEqual(record['task_id'], 'iso-task')
        self.assertEqual(record['workspace']['path'], plan['path'])
        self.assertEqual(record['workspace']['branch'], 'modeldesk-task/iso-task')
        self.assertEqual(record['workspace']['commit'], plan['commit'])
        self.assertEqual(record['verification']['evidence_tree'], 'workspace')
        self.assertEqual(record['verification']['deliverable_commit'], plan['commit'])
        # The user's own tree was never touched by the whole flow.
        self.assertEqual((project / 'a.py').read_bytes().decode('utf-8'), ORIGINAL)
        # Verification re-checks the live worktree copy.
        fresh = task_handover.verify(run)
        self.assertTrue(fresh['valid'])
        self.assertFalse(fresh['stale'])
        self.assertEqual(fresh['message'], '交接文件与记录一致')
        # A caller cannot widen the file scope beyond the task record.
        with self.assertRaisesRegex(ValueError, '范围'):
            task_handover.create(run, summary='scope creep', files=['b.py'])

    def test_verify_detects_tampered_deliverable_and_refuses_missing_copy(self):
        run, project, plan = self.isolated_task()
        task_handover.create(run, summary='deliverable')
        worktree = Path(plan['path'])
        (worktree / 'a.py').write_text('tampered\n', encoding='utf-8')
        stale = task_handover.verify(run)
        self.assertTrue(stale['stale'])
        self.assertFalse(stale['valid'])
        self.assertEqual(stale['changed'], ['a.py'])
        self.assertIn('不能悄悄沿用旧摘要', stale['message'])
        # Restoring the deliverable content makes the handover fresh again.
        (worktree / 'a.py').write_bytes(DELIVERABLE.encode('utf-8'))
        self.assertTrue(task_handover.verify(run)['valid'])
        # Once the disposable worktree is gone, the recorded deliverable commit
        # is the durable evidence.  The handover stays valid even though the
        # untouched original tree holds different content, so this pass cannot
        # have come from the original tree.
        shutil.rmtree(worktree)
        released = task_handover.verify(run)
        self.assertFalse(released['valid'])
        self.assertIn('工作区缺失', released['message'])
        self.assertNotEqual(task_handover.load(run)['files'][0]['sha256'],
                            sha(project / 'a.py'))

    def test_missing_workspace_missing_commit_and_bad_commit_refused(self):
        # A workspace path that does not exist is refused at create time.
        run, _project, _plan = self.isolated_task(name='gone-task', deliverable=None,
                                                 with_commit=False,
                                                 path=str(self.root / 'vanished'))
        with self.assertRaises(ValueError):
            task_handover.create(run, summary='no workspace')
        self.assertIsNone(task_handover.load(run))
        # A worktree without the controller-owned deliverable commit is refused.
        run2, _project2, _plan2 = self.isolated_task(name='nocommit-task', with_commit=False)
        with self.assertRaisesRegex(ValueError, '成果提交'):
            task_handover.create(run2, summary='no deliverable commit')
        self.assertIsNone(task_handover.load(run2))
        # A commit anchor that does not resolve is refused too.
        run3, _project3, _plan3 = self.isolated_task(name='badcommit-task', commit='f' * 40)
        with self.assertRaisesRegex(ValueError, '提交'):
            task_handover.create(run3, summary='ghost commit')
        self.assertIsNone(task_handover.load(run3))
        # Verification with neither a worktree nor a usable commit never falls
        # back to the original project tree; the record is driven by its own
        # workspace anchor, so a kept record is checked as it was written.
        run4, _project4, _plan4 = self.isolated_task(name='keep-task')
        payload = task_handover.create(run4, summary='kept record')
        payload['workspace']['path'] = str(self.root / 'gone-dir')
        fresh = task_handover.verify(run4, payload)
        self.assertFalse(fresh['valid'])
        payload['workspace']['commit'] = None
        stale = task_handover.verify(run4, payload)
        self.assertFalse(stale['valid'])
        self.assertTrue(stale['stale'])
        self.assertIn('不回退原项目树', stale['message'])

    def test_identity_mismatch_between_workspace_and_task_is_refused(self):
        # A workspace branch that does not belong to this task id is refused.
        run, _project, _plan = self.isolated_task(name='id-task', branch='modeldesk-task/other')
        with self.assertRaisesRegex(ValueError, '不一致|已变化'):
            task_handover.create(run, summary='borrowed branch')
        self.assertIsNone(task_handover.load(run))
        # A worktree from a different repository is refused even when the
        # branch name and the commit would both resolve.
        other = self.make_repo(name='other-repo')
        foreign = workspace.prepare(other, 'repo-task')
        self.addCleanup(lambda p=foreign['root']: shutil.rmtree(p, ignore_errors=True))
        (Path(foreign['path']) / 'a.py').write_bytes(DELIVERABLE.encode('utf-8'))
        committed = workspace.commit_task_results(foreign['path'], 'repo-task')
        run2, _project2, _plan2 = self.isolated_task(
            name='repo-task', deliverable=None, with_commit=False,
            path=foreign['path'], branch=foreign['branch'], commit=committed['commit'])
        with self.assertRaisesRegex(ValueError, '不一致|已变化'):
            task_handover.create(run2, summary='foreign worktree')
        self.assertIsNone(task_handover.load(run2))

    def test_fake_isolation_pointing_at_the_project_itself_is_refused(self):
        run, project, plan = self.isolated_task(name='fake-iso')
        registry.patch_task(run, workspace={**plan, 'path': str(project)})
        with self.assertRaisesRegex(ValueError, '已变化'):
            task_handover.create(run, summary='same tree')


class LegacyHandoverTests(Base):
    def test_non_isolated_git_project_keeps_original_rules(self):
        project = self.make_repo()
        run = self.task('legacy-task', project)
        self.accept(run)
        record = task_handover.create(run, summary='serial work on the project tree')
        self.assertNotIn('workspace', record)
        self.assertNotIn('evidence_tree', record['verification'])
        self.assertEqual(record['files'][0]['sha256'], sha(project / 'a.py'))
        self.assertTrue(task_handover.verify(run)['valid'])
        (project / 'a.py').write_text('def one():\n    return 99\n', encoding='utf-8')
        stale = task_handover.verify(run)
        self.assertTrue(stale['stale'])
        self.assertEqual(stale['changed'], ['a.py'])

    def test_isolated_task_cannot_drop_workspace_anchor_to_claim_legacy(self):
        run, project, _plan = self.isolated_task(name='old-format')
        legacy = {'schema': 1, 'task_id': 'old-format', 'project': str(project),
                  'files': [{'path': 'a.py', 'exists': True, 'sha256': sha(project / 'a.py'),
                             'bytes': len(ORIGINAL)}],
                  'verification': {'source': 'handover_files_sha256', 'digest_algorithm': 'sha256'}}
        (run / 'handover.json').write_text(json.dumps(legacy, ensure_ascii=False),
                                           encoding='utf-8')
        fresh = task_handover.verify(run)
        self.assertFalse(fresh['valid'])
        self.assertTrue(fresh['stale'])


class WorkspaceStageHandoverTests(Base):
    def test_prerequisite_prompt_carries_deliverable_commit_digests(self):
        project = self.make_repo(name='stage-project')
        goal = stage_store.create_goal('Goal iso', 'persistent goal', str(project))
        source_stage = stage_store.add_stage(goal['id'], 'first', 'deliver first',
                                             files=['a.py'], acceptance=['criterion'],
                                             candidates=['home'])
        consumer_stage = stage_store.add_stage(goal['id'], 'second', 'depends on first',
                                               depends_on=[source_stage['id']], files=['a.py'],
                                               acceptance=['criterion'], candidates=['home'])
        run, _project, _plan = self.isolated_task(name='stage-source', project=project,
            task_fields={'stage_id':source_stage['id'],'goal_id':goal['id']})
        stage_store.claim_dispatch(source_stage['id'], 'stage-source')
        task_handover.create(run, summary='isolated stage deliverable',
                             stage_id=source_stage['id'], goal_id=goal['id'])
        prompt = task_handover.prerequisite_prompt(consumer_stage['id'])
        self.assertIn('前置阶段成果交接', prompt)
        self.assertIn('a.py（sha256:' + DELIVERABLE_SHA[:12], prompt)
        self.assertNotIn('a.py（sha256:' + ORIGINAL_SHA[:12], prompt)
        blocks=task_handover.prerequisite_handovers(consumer_stage['id'])
        self.assertEqual(task_handover.prerequisite_baseline(blocks,project),_plan['commit'])

    def test_independent_prerequisite_commits_need_deliberate_integration(self):
        first,project,a=self.isolated_task(name='prereq-a')
        second,_,b=self.isolated_task(name='prereq-b',project=project,deliverable='def one():\n    return 43\n')
        with self.assertRaisesRegex(ValueError,'尚无包含全部成果'):
            task_handover.prerequisite_baseline([{'workspace':a},{'workspace':b}],project)

    def test_reference_between_plain_tasks_carries_deliverable_evidence(self):
        project = self.make_repo()
        source, _p, _plan = self.isolated_task(name='ref-source', project=project)
        task_handover.create(source, summary='unstaged isolated deliverable')
        consumer = self.task('ref-consumer', project)
        resolved = task_handover.resolve_reference(consumer, 'ref-source')
        self.assertEqual(resolved['status'], 'ok')
        self.assertEqual(resolved['files'][0]['sha256'], DELIVERABLE_SHA)
        self.assertTrue(resolved['freshness']['valid'])
        self.assertEqual(resolved['project'], str(project))


if __name__ == '__main__':
    unittest.main()
