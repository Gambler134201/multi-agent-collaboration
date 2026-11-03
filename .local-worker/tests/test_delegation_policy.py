import json
import os
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import delegation_policy as policy
import model_registry as registry
import panel_server as panel


class DelegationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old = registry.HERE, registry.REGISTRY, registry.RUNS
        registry.HERE = Path(self.tmp.name)
        registry.REGISTRY = registry.HERE / 'models.json'
        registry.RUNS = registry.HERE / 'runs'
        self.run = self.task('parent')

    def tearDown(self):
        registry.HERE, registry.REGISTRY, registry.RUNS = self.old
        self.tmp.cleanup()

    def task(self, name, count=0, delegated=True):
        run = registry.task_dir(name)
        registry.patch_task(run, id=name, status='completed', model='test', model_calls=3,
                            route={'requirements': {'acceptance': 'A；B'}},
                            delegation={'schema': 1, 'inherited_rework_count': count, 'rework_count': count} if delegated else None)
        return run

    def complete(self, run=None):
        return policy.record_executor(run or self.run, {'completed': True, 'changed_files': ['a.py'], 'test': {'exit_code': 0}})

    def evidence(self, source='reviewer_test', code=0):
        return [{'id': c, 'status': 'pass', 'evidence': [{'source': source, 'detail': 'independent fixture verification', 'exit_code': code}]} for c in ('c1', 'c2')]

    def test_executor_completion_remains_pending_and_stats_do_not_crash(self):
        record = self.complete()
        self.assertEqual(record['state'], policy.EXECUTOR_COMPLETED)
        stats = policy.aggregate()
        self.assertEqual(stats['delegated'], 1)
        self.assertEqual(stats[policy.ACCEPTED], 0)
        row = panel.public_tasks()[0]
        self.assertIn('待独立验收', row['review']['label'])

    def test_missing_native_test_evidence_requires_independent_review_explanation(self):
        registry.patch_task(self.run,test_command='node check.cjs')
        policy.record_executor(self.run,{'completed':True,'test':{'verified':False,'exit_code':None},'changed_files':[]})
        with self.assertRaisesRegex(ValueError,'证据不足'):
            policy.accept(self.run,'main',self.evidence())
        record=policy.accept(self.run,'main',self.evidence(),notes='主脑独立复跑并核实全部测试；未宣称执行器自测通过')
        self.assertEqual(record['state'],policy.ACCEPTED)
        self.assertFalse(record['executor']['test']['verified'])

    def test_acceptance_needs_independent_evidence_for_every_condition(self):
        self.complete()
        for evidence in [self.evidence('executor_test'), self.evidence(code=None), self.evidence()[:1]]:
            with self.assertRaises(ValueError):
                policy.accept(self.run, 'main', evidence)
        mixed = self.evidence()
        mixed[1]['evidence'][0]['source'] = 'executor_report'
        with self.assertRaises(ValueError):
            policy.accept(self.run, 'main', mixed)
        with self.assertRaises(ValueError):
            policy.accept(self.run, 'worker', self.evidence())
        self.assertEqual(policy.accept(self.run, 'main', self.evidence())['state'], policy.ACCEPTED)

    def test_legacy_and_unfinished_results_cannot_be_approved(self):
        legacy = self.task('legacy', delegated=False)
        self.assertIsNone(policy.record_executor(legacy, {'completed': True}))
        with self.assertRaises(ValueError):
            policy.initialize(legacy)
        policy.record_executor(self.run, {'completed': False})
        with self.assertRaises(ValueError):
            policy.accept(self.run, 'main', self.evidence())

    def test_single_rework_is_inherited_and_cannot_be_dispatched_twice(self):
        self.complete()
        policy.request_rework(self.run, 'main', ['c1'], 'Observed wrong result', 'a.py only')
        first = policy.mark_rework_dispatched(self.run, 'child-one')
        self.assertEqual(policy.mark_rework_dispatched(self.run, 'child-one'), first)
        with self.assertRaises(ValueError):
            policy.mark_rework_dispatched(self.run, 'child-two')
        child = self.task('child-one', count=1)
        self.complete(child)
        with self.assertRaises(ValueError):
            policy.request_rework(child, 'main', ['c1'], 'Still wrong', 'a.py only')

    def test_concurrent_rework_claims_have_only_one_winner(self):
        self.complete()
        policy.request_rework(self.run, 'main', ['c1'], 'wrong', 'a.py')
        def claim(name):
            try:
                policy.mark_rework_dispatched(self.run, name)
                return True
            except ValueError:
                return False
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(claim, ['child-a', 'child-b']))
        self.assertEqual(sum(results), 1)

    def test_corrupt_and_negative_counter_records_do_not_reset(self):
        self.complete()
        path = policy.review_path(self.run)
        path.write_text('{invalid', encoding='utf-8')
        with self.assertRaises(ValueError):
            policy.initialize(self.run)
        self.assertTrue(policy.safe_load(self.run)[1])
        path.write_text(json.dumps({'schema': 1, 'rework_count': -1}), encoding='utf-8')
        with self.assertRaises(ValueError):
            policy.load(self.run)

    def test_acceptance_array_is_kept_as_separate_criteria(self):
        run = registry.task_dir('array-task')
        registry.patch_task(run, id='array-task', status='completed', model='test',
                            route={'requirements': {'acceptance': ['alpha', 'beta', 'gamma']}},
                            delegation={'schema': 1})
        record = policy.record_executor(run, {'completed': True})
        self.assertEqual([item['text'] for item in record['criteria']], ['alpha', 'beta', 'gamma'])
        self.assertEqual(len(record['criteria']), 3)

    def test_final_acceptance_preserves_failure_and_rework_count(self):
        self.complete()
        policy.request_rework(self.run, 'main', ['c1'], 'Observed wrong result', 'a.py only')
        child = self.task('child-final', count=1)
        policy.record_executor(child, {'completed': False, 'test': {'exit_code': 1, 'output': 'boom'}})
        record = policy.load(child)
        rows = [{'id': item['id'], 'status': 'pass',
                 'evidence': [{'source': 'reviewer_test', 'detail': 'main reran', 'exit_code': 0}]}
                for item in record['criteria']]
        with self.assertRaises(ValueError):
            policy.accept(child, 'main', rows)
        final = policy.final_accept(child, 'main', rows, 'main took over and reran the test')
        self.assertEqual(final['state'], policy.FINAL_ACCEPTED)
        self.assertEqual(final['executor']['status'], 'failed')
        self.assertEqual(final['rework_count'], 1)
        self.assertEqual(final['final']['executor_status_preserved'], 'failed')
        self.assertTrue(policy.panel_summary(final)['final_accepted'])
        self.assertEqual(policy.aggregate()[policy.FINAL_ACCEPTED], 1)


class ScratchRowClassificationTests(unittest.TestCase):
    def test_only_untracked_reserved_scratch_rows_are_retention_candidates(self):
        import task_workspace as workspace
        for row in ('?? .modeldesk-task-tmp/','?? .modeldesk-task-tmp/nested/check.cjs'):
            self.assertTrue(workspace.untracked_scratch_row(row))
        for row in (' M .modeldesk-task-tmp/old.txt',' D .modeldesk-task-tmp/old.txt',
                    'A  .modeldesk-task-tmp/new.txt','R  a.txt -> .modeldesk-task-tmp/a.txt',
                    '?? .modeldesk-task-tmp','?? .modeldesk-task-tmp-other/a.txt','?? business.txt'):
            self.assertFalse(workspace.untracked_scratch_row(row),row)


class FrozenScratchAcceptanceTests(unittest.TestCase):
    """Real Git/job bindings, isolated records, no inference or service."""
    def setUp(self):
        import task_workspace as workspace
        import zcode_native_tools as native
        from test_task_workspace import make_repo,git
        self.workspace=workspace;self.native=native;self.git=git
        self.temp=tempfile.TemporaryDirectory(prefix='frozen-scratch-')
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name).resolve()
        self.enterContext(patch.object(registry,'HERE',self.root/'controller'))
        self.enterContext(patch.object(registry,'REGISTRY',self.root/'controller/models.json'))
        self.enterContext(patch.object(registry,'RUNS',self.root/'controller/runs'))
        self.project=make_repo(self.root/'project')
        old=self.project/workspace.TASK_TEMP_DIR/'historical.txt'
        old.parent.mkdir();old.write_text('historical\n',encoding='utf-8')
        git(self.project,'add','--',workspace.TASK_TEMP_DIR+'/historical.txt')
        git(self.project,'commit','-m','fixture tracked historical scratch name')
        self.task_id='scratch-review';self.sid='modeldesk-zcode-scratch-review'
        plan={**workspace.prepare(self.project,self.task_id),'created':True,'eligible':True}
        self.execution=Path(plan['path']);self.run=self.project/'.model-desk/runs'/self.task_id
        self.run.mkdir(parents=True)
        self.state={'id':self.task_id,'engine':'zcode','kind':'edit','status':'completed','model':'fixture',
                    'project':str(self.project),'files':['a.txt'],'readonly_files':[],
                    'workspace':plan,'zcode_access_mode':'project','zcode_session_id':self.sid,
                    'delegation':{'schema':1,'rework_count':0},
                    'controller_finalization':{'schema':1,'state':'settled','at':registry.now()},
                    'route':{'requirements':{'acceptance':['business']}}}
        registry.write_json(self.run/'task.json',self.state)
        self.node=native.runtime_paths.node_executable()
        fake=self.root/'metadata-only-node.exe';fake.write_bytes(b'not executed')
        with patch.object(native.runtime_paths,'node_executable',return_value=fake):
            self.job=native.prepare(self.run,self.state,[],self.sid)
        self.scratch=self.execution/workspace.TASK_TEMP_DIR
        (self.scratch/'check.cjs').write_text('fixture scratch\n',encoding='utf-8')
        (self.execution/'a.txt').write_text('changed business\n',encoding='utf-8')
        workspace.commit_task_results(self.execution,self.task_id)
        plan['commit']=git(self.execution,'rev-parse','HEAD').strip()
        self.state=registry.patch_task(self.run,workspace=plan)

    def reject(self):
        with self.assertRaises(ValueError):policy.freeze_acceptance_result(self.run,self.state)

    def test_bound_scratch_passes_formal_acceptance_but_not_worktree_reclamation(self):
        policy.record_executor(self.run,{'completed':True,'changed_files':['a.txt']})
        result=policy.accept(self.run,'main',[{'id':'c1','status':'pass','evidence':[
            {'source':'reviewer_test','detail':'fixture independent business evidence','exit_code':0}]}])
        self.assertEqual(result['state'],policy.ACCEPTED)
        self.assertEqual(self.git(self.execution,'diff-tree','--no-commit-id','--name-only','-r','HEAD').strip(),'a.txt')
        self.assertTrue((self.scratch/'check.cjs').is_file())
        with self.assertRaisesRegex(ValueError,'未提交成果'):
            self.workspace.release(self.project,self.task_id)

    def test_business_changes_new_files_and_tracked_scratch_stay_dirty(self):
        for name in ('a.txt','new-business.txt',self.workspace.TASK_TEMP_DIR+'/historical.txt'):
            path=self.execution/name;before=path.read_bytes() if path.exists() else None
            path.write_text('unreviewed\n',encoding='utf-8');self.reject()
            if before is None:path.unlink()
            else:path.write_bytes(before)
        self.git(self.execution,'add','--',self.workspace.TASK_TEMP_DIR+'/check.cjs')
        self.reject()

    def test_old_jobs_wrong_executor_or_missing_session_get_no_exemption(self):
        for patch_values in ({'engine':'workbuddy'},{'engine':'dsh'},{'engine':'pi'},
                             {'kind':'chat'},{'zcode_session_id':'modeldesk-zcode-other'}):
            prior=self.state;self.state={**prior,**patch_values}
            registry.write_json(self.run/'task.json',self.state);self.reject()
            self.state=prior;registry.write_json(self.run/'task.json',prior)
        data=json.loads(self.job.read_text(encoding='utf-8'));data.pop('temp_dir')
        self.job.write_text(json.dumps(data),encoding='utf-8')
        import hashlib
        index=self.project/'.model-desk/zcode-native-sessions.json';rows=registry.read_json(index)
        rows['sessions'][self.sid]['job_hash']=hashlib.sha256(self.job.read_bytes()).hexdigest()
        registry.write_json(index,rows)
        self.reject()

    def test_missing_tampered_and_cross_task_jobs_never_ignore_scratch(self):
        import hashlib
        original=self.job.read_bytes()
        self.job.unlink();self.reject();self.job.write_bytes(original)
        index=self.project/'.model-desk/zcode-native-sessions.json'
        rows=registry.read_json(index)
        for field,value in (('task_id','foreign-task'),('temp_dir','../outside'),('nonce','tampered')):
            data=json.loads(original);data[field]=value
            self.job.write_text(json.dumps(data),encoding='utf-8')
            if field!='nonce':
                rows['sessions'][self.sid]['job_hash']=hashlib.sha256(self.job.read_bytes()).hexdigest()
                registry.write_json(index,rows)
            self.reject()

    def test_commit_drift_remains_fatal(self):
        self.state['workspace']={**self.state['workspace'],'commit':'0'*40}
        self.reject()

    def test_child_directory_alias_does_not_become_scratch_evidence(self):
        if self.node is None:self.skipTest('Node required for portable junction fixture')
        outside=self.root/'outside';outside.mkdir()
        alias=self.scratch/'alias'
        subprocess.run([str(self.node),'-e','require("fs").symlinkSync(process.argv[1],process.argv[2],"junction")',
                        str(outside),str(alias)],check=True,capture_output=True)
        try:self.reject()
        finally:alias.unlink()

    def test_hardlink_scratch_does_not_become_retention_evidence(self):
        outside=self.root/'outside.txt';outside.write_text('outside\n')
        alias=self.scratch/'linked.txt';os.link(outside,alias)
        try:self.reject()
        finally:alias.unlink()


class WorkBuddyScratchAcceptanceTests(unittest.TestCase):
    """Real Git/job/proof bindings for the WorkBuddy native scratch exemption."""
    def setUp(self):
        import task_workspace as workspace
        import workbuddy_native_tools as native
        from test_task_workspace import make_repo,git
        self.workspace=workspace;self.native=native;self.git=git
        self.temp=tempfile.TemporaryDirectory(prefix='wb-scratch-')
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name).resolve()
        self.enterContext(patch.object(registry,'HERE',self.root/'controller'))
        self.enterContext(patch.object(registry,'REGISTRY',self.root/'controller/models.json'))
        self.enterContext(patch.object(registry,'RUNS',self.root/'controller/runs'))
        self.project=make_repo(self.root/'project')
        self.node=os.environ.get('MODEL_DESK_TEST_NODE') or shutil.which('node')
        self.task_id='wb-scratch-review';self.sid='modeldesk-wb-scratch-review'
        plan={**workspace.prepare(self.project,self.task_id),'created':True,'eligible':True}
        self.execution=Path(plan['path']);self.run=self.project/'.model-desk/runs'/self.task_id
        self.run.mkdir(parents=True)
        self.state={'id':self.task_id,'engine':'workbuddy','kind':'edit','status':'completed','model':'fixture',
                    'project':str(self.project),'files':['a.txt'],'readonly_files':[],
                    'workbuddy_tool_mode':native.MODE,'workbuddy_tool_revision':native.revision(),
                    'workbuddy_access_mode':'project','workbuddy_session_id':self.sid,
                    'workspace':plan,'delegation':{'schema':1,'rework_count':0},
                    'controller_finalization':{'schema':1,'state':'settled','at':registry.now()},
                    'route':{'requirements':{'acceptance':['business']}}}
        registry.write_json(self.run/'task.json',self.state)
        self.job=native.prepare(self.run,self.execution,self.task_id,['a.txt'],[],'edit',
                                record_project=self.project)
        self.scratch=self.execution/workspace.TASK_TEMP_DIR
        (self.scratch/'check.cjs').write_text('fixture scratch\n',encoding='utf-8')
        (self.execution/'a.txt').write_text('changed business\n',encoding='utf-8')
        workspace.commit_task_results(self.execution,self.task_id)
        plan['commit']=self.git(self.execution,'rev-parse','HEAD').strip()
        self.state=registry.patch_task(self.run,workspace=plan)
        self.write_proof()

    def write_proof(self):
        # SessionStart through the real Node guard: the proof binds job hash,
        # nonce, task/project and this parent session, like a native run.
        if self.node is None:self.skipTest('Node required for the real guard proof fixture')
        result=subprocess.run([self.node,str(self.native.GUARD),str(self.job)],
                              input=json.dumps({'hook_event_name':'SessionStart','session_id':self.sid}),
                              capture_output=True,text=True,encoding='utf-8')
        self.assertEqual(result.returncode,0,result.stderr or result.stdout)

    def reject(self):
        with self.assertRaises(ValueError):policy.freeze_acceptance_result(self.run,self.state)

    def test_bound_scratch_passes_acceptance_but_not_worktree_reclamation(self):
        policy.record_executor(self.run,{'completed':True,'changed_files':['a.txt']})
        result=policy.accept(self.run,'main',[{'id':'c1','status':'pass','evidence':[
            {'source':'reviewer_test','detail':'fixture independent business evidence','exit_code':0}]}])
        self.assertEqual(result['state'],policy.ACCEPTED)
        self.assertEqual(self.git(self.execution,'diff-tree','--no-commit-id','--name-only','-r','HEAD').strip(),'a.txt')
        self.assertTrue((self.scratch/'check.cjs').is_file())
        with self.assertRaisesRegex(ValueError,'未提交成果'):
            self.workspace.release(self.project,self.task_id)

    def test_missing_or_foreign_proof_and_dirty_business_stay_fatal(self):
        policy.record_executor(self.run,{'completed':True,'changed_files':['a.txt']})
        (self.run/'workbuddy-native-proof.json').unlink();self.reject()
        self.write_proof()
        registry.patch_task(self.run,workbuddy_session_id='modeldesk-wb-foreign')
        self.state=registry.read_task(self.run);self.reject()
        registry.patch_task(self.run,workbuddy_session_id=self.sid)
        self.state=registry.read_task(self.run)
        events=self.run/'workbuddy-native-events.jsonl'
        events.write_text(json.dumps({'event':'PreToolUse','session_id':self.sid,'nonce':'tampered',
            'tool':'Bash','call_id':'c1','input_hash':'fixture','allowed':False,
            'reason_code':'guard_denied','denial_rule':'guard_denied'})+'\n',encoding='utf-8')
        self.reject()
        events.unlink()
        (self.execution/'new-business.txt').write_text('unreviewed\n',encoding='utf-8');self.reject()
        (self.execution/'new-business.txt').unlink()
        self.git(self.execution,'add','--',self.workspace.TASK_TEMP_DIR+'/check.cjs');self.reject()

    def test_foreign_engines_kinds_and_tampered_jobs_get_no_exemption(self):
        for patch_values in ({'engine':'zcode'},{'engine':'dsh'},{'kind':'chat'}):
            prior=self.state;self.state={**prior,**patch_values}
            registry.write_json(self.run/'task.json',self.state);self.reject()
            self.state=prior;registry.write_json(self.run/'task.json',prior)
        original=self.job.read_bytes()
        for change in ({'temp_dir':'../outside'},None):
            data=json.loads(original)
            if change is None:data.pop('temp_dir')
            else:data.update(change)
            self.job.write_text(json.dumps(data),encoding='utf-8');self.reject()
        self.job.write_bytes(original)

    def test_legacy_or_stale_task_snapshot_never_gains_scratch_exemption(self):
        original=dict(self.state)
        for values in ({'workbuddy_tool_mode':self.native.LEGACY},
                       {'workbuddy_tool_mode':None},
                       {'workbuddy_tool_revision':'old-version'},
                       {'workbuddy_tool_revision':None}):
            with self.subTest(values=values):
                self.state={**original,**values}
                registry.write_json(self.run/'task.json',self.state)
                self.assertIsNone(self.native.task_temp_dir(self.state))
                self.reject()
        self.state=original
        registry.write_json(self.run/'task.json',original)
        policy.freeze_acceptance_result(self.run,self.state)

    def test_parent_session_must_be_nonempty_even_if_corrupt_proof_agrees(self):
        file=self.run/'workbuddy-native-proof.json'
        original=registry.read_json(file)
        for sid in (None,'','   ',False,7,[]):
            with self.subTest(session_id=sid):
                self.state={**self.state,'workbuddy_session_id':sid}
                registry.write_json(self.run/'task.json',self.state)
                registry.write_json(file,{**original,'session_id':sid})
                self.reject()

    def test_commit_drift_remains_fatal(self):
        self.state['workspace']={**self.state['workspace'],'commit':'0'*40}
        self.reject()

    def test_child_directory_alias_does_not_become_scratch_evidence(self):
        if self.node is None:self.skipTest('Node required for portable junction fixture')
        outside=self.root/'outside';outside.mkdir()
        alias=self.scratch/'alias'
        subprocess.run([str(self.node),'-e','require("fs").symlinkSync(process.argv[1],process.argv[2],"junction")',
                        str(outside),str(alias)],check=True,capture_output=True)
        try:self.reject()
        finally:alias.unlink()

    def test_hardlink_scratch_does_not_become_retention_evidence(self):
        outside=self.root/'outside.txt';outside.write_text('outside\n')
        alias=self.scratch/'linked.txt';os.link(outside,alias)
        try:self.reject()
        finally:alias.unlink()


if __name__ == '__main__':
    unittest.main()
