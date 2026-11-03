"""Approved fan-in -> existing queue -> independent acceptance -> downstream.

Only temporary records, local Git and local Python checks. No model/service.
"""
import copy
from pathlib import Path
import shutil
import sys
import unittest
from unittest.mock import patch

import test_approved_plan as fixture
import approved_plan as plans
import integration_queue as queue
import model_registry as registry
import task_handover as handover
import task_integration as integration
import task_workspace as workspace
import delegation_policy as policy
from test_task_workspace_handover import git


class PlanIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.f=fixture.PlanTests();self.f.setUp();self.addCleanup(self.f.doCleanups)
        self.checks=[[sys.executable,'-c','print("JOINT_CHECK")']]
        self.jobs={};self.calls=[]
        self.handovers=[{'task_id':'task-a'},{'task_id':'task-b'}]

    def approve(self,checks=None):
        checks=self.checks if checks is None else checks
        p=plans.preview(self.f.goal,self.f.project,checks)
        return plans.change(self.f.goal,self.f.project,'approve',expected=p['digest'],checks=checks)['plan']

    def start(self,checks=None):
        self.approve(checks);plans.advance(self.f.goal,self.f.project,self.f.dispatch)
        self.f.accept('task-a');self.f.accept('task-b')

    def integrate(self,ids,checks,request_id):
        self.calls.append((ids,checks,request_id))
        old=plans.view(self.f.goal,self.f.project)['plan']['integrations'][self.f.c]
        self.jobs.setdefault(old['name'],{'name':old['name'],'request_reference':old['request_reference'],
            'goal_id':self.f.goal,'task_ids':ids,'queue':{'state':'queued','check_commands':checks}})

    def advance(self,*,baseline=False):
        with patch.object(handover,'prerequisite_handovers',return_value=self.handovers),\
             patch.object(handover,'prerequisite_baseline',**({'return_value':'accepted-baseline'} if baseline else {'side_effect':handover.HandoverError('independent branches')})),\
             patch.object(queue,'get',side_effect=lambda p,n:copy.deepcopy(self.jobs.get(n))):
            return plans.advance(self.f.goal,self.f.project,self.f.dispatch,self.integrate)['plan']

    def job(self):return next(iter(self.jobs.values()))

    def test_checks_are_explicit_argv_and_change_the_approval_digest(self):
        plain=plans.preview(self.f.goal,self.f.project)
        value=self.approve()
        self.assertNotEqual(plain['digest'],value['digest'])
        self.assertEqual(plans.preview(self.f.goal,self.f.project)['digest'],value['digest'])
        for invalid in ('python test.py',['python test.py'],[['']],[[1]],None):
            if invalid is None:continue  # None means reuse the stored version.
            with self.subTest(value=invalid),self.assertRaises(ValueError):
                plans.preview(self.f.goal,self.f.project,invalid)
        self.assertEqual(self.f.sent,[])

    def test_no_checks_preserves_manual_integration_and_no_new_effect(self):
        self.start([]);p=self.advance()
        self.assertEqual(p['observations'][self.f.c],'waiting_integration')
        self.assertEqual(self.calls,[])

    def test_missing_handover_never_enters_integration_queue(self):
        self.start()
        with patch.object(handover,'prerequisite_handovers',side_effect=ValueError('missing')):
            p=plans.advance(self.f.goal,self.f.project,self.f.dispatch,self.integrate)['plan']
        self.assertEqual(p['observations'][self.f.c],'waiting_handover');self.assertEqual(self.calls,[])

    def test_first_reserves_once_running_and_passed_are_not_acceptance(self):
        self.start();p=self.advance()
        self.assertEqual(len(self.calls),1)
        intent=p['integrations'][self.f.c]
        self.assertEqual(intent['task_ids'],['task-a','task-b'])
        self.assertEqual(intent['checks'],self.checks)
        for state in ('preparing','merging','checking'):
            self.job()['queue']['state']=state
            self.assertEqual(self.advance()['observations'][self.f.c],'integration_running')
        self.job().update(verdict='passed',integrated_commit='a'*40,accepted=False)
        self.assertEqual(self.advance(baseline=True)['observations'][self.f.c],'integration_awaiting_review')
        self.assertNotIn('task-c',self.f.sent);self.assertEqual(len(self.calls),1)
        self.job().update(accepted=True,accepted_commit='a'*40)
        self.advance(baseline=True)
        self.assertIn('task-c',self.f.sent);self.assertEqual(len(self.calls),1)

    def test_missing_reply_never_recreates_even_after_reapproval(self):
        self.start()
        with patch.object(self,'integrate',side_effect=OSError('unknown')):p=self.advance()
        original=p['integrations'][self.f.c]
        self.assertEqual(p['observations'][self.f.c],'integration_unknown')
        self.f.policy_version+=1;self.approve()
        for _ in range(3):self.advance()
        self.assertEqual(self.calls,[])
        self.assertEqual(plans.view(self.f.goal,self.f.project)['plan']['integrations'][self.f.c],original)

    def test_concurrent_completion_events_reserve_one_original_integration(self):
        from concurrent.futures import ThreadPoolExecutor
        self.start();original=self.integrate
        def submit(*args):
            original(*args);self.job()['queue']['state']='checking'
        with patch.object(handover,'prerequisite_handovers',return_value=self.handovers),\
             patch.object(handover,'prerequisite_baseline',side_effect=handover.HandoverError('branches')),\
             patch.object(queue,'get',side_effect=lambda p,n:copy.deepcopy(self.jobs.get(n))):
            with ThreadPoolExecutor(max_workers=3) as pool:
                list(pool.map(lambda _:plans.advance(self.f.goal,self.f.project,self.f.dispatch,submit),range(6)))
        self.assertEqual(len(self.calls),1);self.assertEqual(len(self.jobs),1)
        self.assertNotIn('task-c',self.f.sent)

    def test_ambiguous_baseline_evidence_does_not_create_another_integration(self):
        self.start()
        with patch.object(handover,'prerequisite_handovers',return_value=self.handovers),\
             patch.object(handover,'prerequisite_baseline',side_effect=ValueError('ambiguous accepted commits')):
            p=plans.advance(self.f.goal,self.f.project,self.f.dispatch,self.integrate)['plan']
        self.assertEqual(p['observations'][self.f.c],'integration_needs_attention')
        self.assertEqual(self.calls,[])

    def test_lost_receipt_observes_real_job_without_reenqueueing(self):
        self.start();original=self.integrate
        def lost(*args):
            original(*args);self.job()['queue']['state']='checking';raise OSError('lost reply')
        with patch.object(self,'integrate',side_effect=lost):self.advance()
        for _ in range(3):self.advance()
        self.assertEqual(len(self.calls),1);self.assertEqual(len(self.jobs),1)

    def test_conflict_failure_cancel_and_unknown_never_retry_or_dispatch(self):
        self.start();self.advance()
        for state in ('blocked','cancelled','recovery_required'):
            self.job()['queue']['state']=state
            self.assertEqual(self.advance(baseline=True)['observations'][self.f.c],'integration_needs_attention')
        for verdict in ('check_failed','merge_conflict'):
            self.job()['verdict']=verdict
            self.assertEqual(self.advance(baseline=True)['observations'][self.f.c],'integration_needs_attention')
        self.assertEqual(len(self.calls),1);self.assertNotIn('task-c',self.f.sent)

    def test_changed_checks_or_foreign_receipt_cannot_bypass_original_intent(self):
        self.start();self.advance()
        original=copy.deepcopy(self.job())
        for field,bad in (('request_reference','other'),('goal_id','other'),('task_ids',['foreign'])):
            self.job()[field]=bad
            self.assertEqual(self.advance(baseline=True)['observations'][self.f.c],'integration_needs_attention')
            self.jobs[original['name']]=copy.deepcopy(original)
        self.approve([[sys.executable,'-c','print("changed")']])
        self.assertEqual(self.advance()['observations'][self.f.c],'integration_needs_attention')
        self.assertEqual(len(self.calls),1)

    def test_queued_reattach_uses_only_original_job(self):
        self.start();self.advance();before=copy.deepcopy(self.calls[0])
        self.advance()
        self.assertEqual(self.calls,[before,before]);self.assertEqual(len(self.jobs),1)
        self.job()['queue']['state']='recovery_required';self.advance()
        self.assertEqual(len(self.calls),2)

    def test_pause_and_switch_off_do_not_start_integration(self):
        import workflow_settings
        self.start();p=plans.view(self.f.goal,self.f.project)['plan']
        plans.change(self.f.goal,self.f.project,'pause',expected=p['digest'])
        self.advance();self.assertEqual(self.calls,[])
        plans.change(self.f.goal,self.f.project,'resume',expected=p['digest'])
        with patch.object(workflow_settings,'snapshot',return_value={'enabled':False}):self.advance()
        self.assertEqual(self.calls,[])

    def test_real_git_fan_in_requires_independent_review_then_adopts_commit(self):
        project=Path(self.f.project)
        git(project,'init','-b','main');git(project,'config','user.name','Fixture')
        git(project,'config','user.email','fixture@example.invalid')
        (project/'keep.txt').write_text('original');git(project,'add','keep.txt');git(project,'commit','-m','base')
        base=git(project,'rev-parse','HEAD')
        checks=[[sys.executable,'-c','from pathlib import Path; assert Path("a.py").read_text()=="A"; assert Path("b.py").read_text()=="B"; print("FAN_IN_OK")']]
        self.approve(checks);plans.advance(self.f.goal,self.f.project,self.f.dispatch)
        for name,filename,content in [('task-a','a.py','A'),('task-b','b.py','B')]:
            run=registry.task_dir(name,self.f.project);prepared=workspace.prepare(project,name)
            self.addCleanup(lambda p=prepared['root']:shutil.rmtree(p,ignore_errors=True))
            (Path(prepared['path'])/filename).write_text(content)
            committed=workspace.commit_task_results(prepared['path'],name)
            plan={**prepared,'eligible':True,'created':True,'commit':committed['commit']}
            registry.patch_task(run,workspace=plan,kind='edit',status='completed',files=[filename],changed_files=[filename])
            self.f.accept(name)
            handover.create(run,files=[filename],test_evidence=[{'source':'reviewer_test','detail':'fixture independent check','exit_code':0}])
        merges=[]
        def submit(ids,commands,request_id):
            row=queue.enqueue(self.f.project,ids,commands,request_id=request_id)
            merges.append(row['name']);queue.run_one(self.f.project,row['name'])
        p=plans.advance(self.f.goal,self.f.project,self.f.dispatch,submit)['plan']
        self.assertEqual(len(merges),1)
        record=queue.get(self.f.project,merges[0])
        self.addCleanup(lambda p=record['integration_copy']['path']:shutil.rmtree(p,ignore_errors=True))
        self.assertEqual(queue.state(record),'awaiting_review')
        self.assertIn('FAN_IN_OK',record['checks'][0]['output_tail'])
        self.assertNotIn('task-c',self.f.sent)
        integration.accept_integration(self.f.project,merges[0],[{'id':'i1','status':'pass','evidence':[
            {'source':'reviewer_test','detail':'Independent fixture inspection','exit_code':0}]}])
        observed=[];original=self.f.dispatch
        def downstream(stage,choice,proof):
            observed.append(handover.prerequisite_baseline(handover.prerequisite_handovers(stage['id'],self.f.project),self.f.project))
            return original(stage,choice,proof)
        plans.advance(self.f.goal,self.f.project,downstream,submit)
        self.assertEqual(observed,[record['integrated_commit']])
        self.assertEqual(merges,[record['name']]);self.assertIn('task-c',self.f.sent)
        self.assertEqual(git(project,'rev-parse','HEAD'),base)
        self.assertFalse((project/'a.py').exists());self.assertFalse((project/'b.py').exists())


if __name__=='__main__':unittest.main()
