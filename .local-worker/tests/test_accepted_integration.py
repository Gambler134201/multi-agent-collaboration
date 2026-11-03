"""Accepted task integration, pinned commits, ownership and real checks, offline."""
import shutil
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import brain_context
import brain_gateway
import delegation_policy as policy
import model_registry as registry
import task_handover
import task_integration as integration
import task_finalization
import task_workspace as workspace
from test_task_workspace_handover import Base, git


class AcceptedIntegrationTests(Base):
    def test_both_brain_hosts_integrate_once_and_keep_review_copy(self):
        for host in ('codex','dsh'):
            project=self.make_repo('project-'+host)
            run,_,plan=self.isolated_task(name='accepted-'+host,project=project)
            context=brain_context.build_context(project=str(project),host=host,owner='owner-'+host,
                role='brain',operator_session='fixture-'+host,session_binding='user_bound')
            registry.patch_task(run,source='brain',owner=context.owner,
                                brain={'owner':context.owner,'project':str(project),'binding_key':context.binding_key})
            profile=Path(plan['path'])/'.zcode'
            profile.mkdir()
            (profile/'config.json').write_text('{}')
            cap=brain_gateway.register_connection(context.declaration())['capability']
            checks=[[sys.executable,'-c','from pathlib import Path; assert "42" in Path("a.py").read_text(); print("REAL_CHECK_RAN")']]
            request={'request_id':'integrate-'+host,'task_ids':[run.name],'checks':checks}
            result=brain_gateway.handle('integrate',request,cap)
            evidence=result['integration']
            self.assertEqual(evidence['verdict'],'passed')
            self.assertIn('REAL_CHECK_RAN',evidence['checks'][0]['output_tail'])
            self.assertEqual(evidence['commits'][run.name],plan['commit'])
            self.assertFalse(evidence['accepted'])
            self.assertTrue(Path(evidence['integration_copy']['path']).is_dir())
            self.assertIn('return 1',(project/'a.py').read_text())
            with patch.object(integration,'run_integration',side_effect=AssertionError('must not rerun')):
                replay=brain_gateway.handle('integrate',request,cap)
            self.assertTrue(replay['reused'])
            self.assertEqual(replay['integration'],evidence)
            recovered=brain_gateway.handle('request_status',{'request_id':request['request_id']},cap)
            self.assertEqual(recovered['request_state'],'completed')
            self.assertEqual(recovered['integration'],evidence)

    def test_unaccepted_unsettled_dirty_and_stale_head_refuse_before_commands(self):
        run,project,plan=self.isolated_task()
        original=registry.read_task(run)
        record=policy.load(run)
        for kind in ('not-accepted','unsettled','head-mismatch','dirty'):
            registry.write_json(run/'review.json',record)
            registry.write_json(run/'task.json',original)
            if kind=='not-accepted':registry.write_json(run/'review.json',{**record,'state':'executor_completed'})
            elif kind=='unsettled':registry.patch_task(run,controller_finalization=task_finalization.pending())
            elif kind=='head-mismatch':registry.patch_task(run,workspace={**plan,'commit':plan['baseline']})
            else:(Path(plan['path'])/'a.py').write_text('dirty\n')
            with self.subTest(kind=kind),patch.object(integration,'run_integration') as execute:
                with self.assertRaises(ValueError):
                    integration.integrate_tasks(project,[run.name],name='refused',checks=[[sys.executable,'-c','pass']])
                execute.assert_not_called()

    def test_real_check_failure_is_not_a_pass_and_copy_is_kept(self):
        run,project,_=self.isolated_task()
        result=integration.integrate_tasks(project,[run.name],name='check-fails',
            checks=[[sys.executable,'-c','raise SystemExit(3)']])
        self.assertEqual(result['verdict'],'check_failed')
        self.assertEqual(result['checks'][0]['exit_code'],3)
        self.assertFalse(result['accepted'])
        self.assertTrue(Path(result['integration_copy']['path']).exists())

    def test_latest_rejects_traversal(self):
        with self.assertRaises(ValueError):integration.latest(self.root,'../outside')

    def test_another_tasks_review_and_post_acceptance_commit_are_rejected(self):
        first,project,plan=self.isolated_task(name='review-a')
        second,_,_=self.isolated_task(name='review-b',project=project)
        original=(first/'review.json').read_bytes()
        (first/'review.json').write_bytes((second/'review.json').read_bytes())
        with patch.object(integration,'run_integration') as check:
            with self.assertRaisesRegex(ValueError,'任务身份'):
                integration.integrate_tasks(project,[first.name],name='foreign-review',checks=[[sys.executable,'-c','pass']])
        check.assert_not_called()
        (first/'review.json').write_bytes(original)
        (Path(plan['path'])/'a.py').write_text('changed after acceptance\n')
        git(plan['path'],'add','a.py');git(plan['path'],'commit','-m','new unchecked result')
        registry.patch_task(first,workspace={**plan,'commit':git(plan['path'],'rev-parse','HEAD').strip()})
        with patch.object(integration,'run_integration') as check:
            with self.assertRaisesRegex(ValueError,'沿用旧验收'):
                integration.integrate_tasks(project,[first.name],name='stale-review',checks=[[sys.executable,'-c','pass']])
        check.assert_not_called()

    def test_main_takeover_freezes_fixes_but_preserves_executor_failure(self):
        run,project,plan=self.isolated_task(name='takeover')
        record=policy.load(run)
        record['state']='executor_failed';record['executor']['status']='failed'
        registry.write_json(run/'review.json',record)
        registry.patch_task(run,status='failed',error='original executor failure')
        (Path(plan['path'])/'a.py').write_text('def one():\n    return 77\n')
        review=policy.final_accept(run,'main',[{'id':'c1','status':'pass',
            'evidence':[{'source':'reviewer_test','detail':'main verified 77 in fixture','exit_code':0}]}],
            'main took over and independently checked fixed result')
        task=registry.read_task(run)
        self.assertEqual(task['status'],'failed')
        self.assertEqual(task['error'],'original executor failure')
        self.assertEqual(review['executor']['status'],'failed')
        self.assertNotEqual(task['workspace']['commit'],plan['commit'])
        self.assertEqual(review['result_binding']['workspace']['commit'],task['workspace']['commit'])
        self.assertIn('return 1',(project/'a.py').read_text())

    def _own_task(self, name, project, filename, content):
        run = self.task(name, project)
        prepared = workspace.prepare(project, name)
        self.addCleanup(lambda p=prepared['root']: shutil.rmtree(p, ignore_errors=True))
        (Path(prepared['path']) / filename).write_text(content, encoding='utf-8')
        plan = {'eligible': True, 'reason': '', 'baseline': prepared['baseline'],
                'created': True, 'path': prepared['path'], 'branch': prepared['branch']}
        committed = workspace.commit_task_results(prepared['path'], name)
        plan['commit'] = committed.get('commit') or git(prepared['path'], 'rev-parse', 'HEAD').strip()
        registry.patch_task(run, workspace=plan)
        self.accept(run)
        return run, plan

    def test_accepted_integration_becomes_prerequisite_baseline(self):
        project = self.make_repo('project-baseline')
        run_a, plan_a = self._own_task('base-a', project, 'a_mod.py', 'A_DELIVERABLE = 1\n')
        run_b, plan_b = self._own_task('base-b', project, 'b_mod.py', 'B_DELIVERABLE = 2\n')
        handovers = [{'workspace': {'commit': plan_a['commit']}},
                     {'workspace': {'commit': plan_b['commit']}}]
        with self.assertRaises(task_handover.HandoverError):
            task_handover.prerequisite_baseline(handovers, project)
        checks = [[sys.executable, '-c',
                   'from pathlib import Path; '
                   'assert Path("a_mod.py").read_text().startswith("A_DELIVERABLE = 1"); '
                   'assert Path("b_mod.py").read_text().startswith("B_DELIVERABLE = 2")']]
        result = integration.integrate_tasks(project, [run_a.name, run_b.name],
                                             name='int-baseline', checks=checks)
        self.assertEqual(result['verdict'], 'passed')
        self.assertTrue(result['merge_passed'])
        self.assertFalse(result['accepted'])
        evidence = [{'id': 'i1', 'status': 'pass',
                     'evidence': [{'source': 'reviewer_inspection', 'detail': 'merged copy verified'}]}]
        copy = Path(result['integration_copy']['path'])
        (copy / 'a_mod.py').write_text('tampered\n', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, '集成副本'):
            integration.accept_integration(project, 'int-baseline', evidence)
        (copy / 'a_mod.py').write_text('A_DELIVERABLE = 1\n', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, '独立证据'):
            integration.accept_integration(project, 'int-baseline',
                [{'id': 'i1', 'status': 'pass', 'evidence': [{'source': 'self_report', 'detail': 'claim'}]}])
        record = integration.accept_integration(project, 'int-baseline', evidence)
        self.assertTrue(record['accepted'])
        self.assertEqual(record['accepted_commit'], result['integrated_commit'])
        with self.assertRaisesRegex(ValueError, '已独立验收'):
            integration.accept_integration(project, 'int-baseline', evidence)
        self.assertEqual(task_handover.prerequisite_baseline(handovers, project),
                         result['integrated_commit'])
        self.assertEqual(integration.integration_baseline(project, {plan_a['commit'], plan_b['commit']}),
                         result['integrated_commit'])

    def test_reworked_prerequisite_never_uses_stale_integration(self):
        project = self.make_repo('project-stale')
        run_a, plan_a = self._own_task('stale-a', project, 'a_mod.py', 'A = 1\n')
        run_b, plan_b = self._own_task('stale-b', project, 'b_mod.py', 'B = 2\n')
        integration.integrate_tasks(project, [run_a.name, run_b.name], name='int-stale',
                                    checks=[[sys.executable, '-c', 'print("ok")']])
        (Path(plan_a['path']) / 'a_mod.py').write_text('A = 1\nCHANGED\n', encoding='utf-8')
        committed = workspace.commit_task_results(plan_a['path'], run_a.name)
        registry.patch_task(run_a, workspace={**plan_a, 'commit': committed['commit']})
        with self.assertRaises(ValueError):
            integration.accept_integration(project, 'int-stale',
                [{'id': 'i1', 'status': 'pass', 'evidence': [{'source': 'reviewer_inspection', 'detail': 'x'}]}])
        handovers = [{'workspace': {'commit': committed['commit']}},
                     {'workspace': {'commit': plan_b['commit']}}]
        with self.assertRaises(task_handover.HandoverError):
            task_handover.prerequisite_baseline(handovers, project)

    def test_whole_shell_string_check_gets_argv_hint(self):
        project = self.make_repo('project-hint')
        outcome = integration._run_check(project, ['git log --oneline -n 1'], 10)
        self.assertFalse(outcome['ok'])
        self.assertIsNone(outcome['exit_code'])
        self.assertIn('参数向量', outcome['output_tail'])

    def test_recycled_worktree_still_integrates_via_branch(self):
        project = self.make_repo('project-recycle')
        run_a, plan_a = self._own_task('recyc-a', project, 'a_mod.py', 'A = 1\n')
        run_b, plan_b = self._own_task('recyc-b', project, 'b_mod.py', 'B = 2\n')
        rows = {row['task_id']: row for row in workspace.stale_worktrees(project)}
        self.assertTrue(rows[run_a.name]['releasable'])
        self.assertTrue(rows[run_b.name]['releasable'])
        removed = workspace.release_checked(project, run_a.name)
        self.assertTrue(removed['removed'])
        self.assertFalse(Path(plan_a['path']).exists())
        self.assertTrue(workspace.branch_exists(project, run_a.name))
        result = integration.integrate_tasks(project, [run_a.name, run_b.name], name='int-recycle',
                                             checks=[[sys.executable, '-c',
                                                      'from pathlib import Path; '
                                                      'assert Path("a_mod.py").read_text().startswith("A = 1"); '
                                                      'assert Path("b_mod.py").read_text().startswith("B = 2")']])
        self.assertEqual(result['verdict'], 'passed')

    def test_recycling_refuses_unsettled_or_unaccepted(self):
        project = self.make_repo('project-recycle-guard')
        run = self.task('guard-a', project, status='queued')
        prepared = workspace.prepare(project, 'guard-a')
        self.addCleanup(lambda p=prepared['root']: shutil.rmtree(p, ignore_errors=True))
        with self.assertRaisesRegex(ValueError, '尚未结束'):
            workspace.release_checked(project, 'guard-a')
        registry.patch_task(run, status='completed', controller_finalization={'schema': 1, 'state': 'pending'})
        with self.assertRaisesRegex(ValueError, '收尾尚未确认'):
            workspace.release_checked(project, 'guard-a')
        registry.patch_task(run, controller_finalization={'schema': 1, 'state': 'settled'})
        with self.assertRaisesRegex(ValueError, '验收结论未定'):
            workspace.release_checked(project, 'guard-a')
        self.assertTrue(Path(prepared['path']).exists())

    def test_same_file_conflict_verdict_keeps_copy(self):
        project = self.make_repo('project-conflict')
        run_a, plan_a = self._own_task('conf-a', project, 'shared.txt', 'from A\n')
        run_b, plan_b = self._own_task('conf-b', project, 'shared.txt', 'from B\n')
        result = integration.integrate_tasks(project, [run_a.name, run_b.name], name='int-conflict',
                                             checks=[[sys.executable, '-c', 'print("unused")']])
        self.assertEqual(result['verdict'], 'merge_conflict')
        self.assertFalse(result['merge_passed'])
        self.assertTrue(Path(result['integration_copy']['path']).is_dir())
        self.assertIn('return 1', (project / 'a.py').read_text())
