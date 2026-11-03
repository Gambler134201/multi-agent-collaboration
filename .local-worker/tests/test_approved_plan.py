"""Approval/queue boundary tests, isolated records and mock executors only."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import approved_plan as plans
import stage_store as stages
import model_registry as registry
import delegation_policy as policy
import task_handover
import workflow_settings


class PlanTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        self.root=Path(temp.name);self.project=self.root/'project';self.project.mkdir();self.project=str(self.project)
        self.enterContext(patch.object(registry,'HERE',self.root))
        self.enterContext(patch.object(registry,'REGISTRY',self.root/'models.json'))
        self.enterContext(patch.object(registry,'RUNS',self.root/'runs'))
        self.enterContext(patch.object(workflow_settings,'snapshot',return_value={**workflow_settings.DEFAULTS,'enabled':True,'readable':True}))
        self.enterContext(patch.object(plans,'_context',side_effect=self.context))
        self.enterContext(patch.object(plans,'_external_notify',return_value=False))
        self.goal=stages.create_goal('Goal','Deliver a small feature',self.project,store='project')['id']
        self.a=self.stage('A');self.b=self.stage('B');self.c=self.stage('C',[self.a,self.b])
        self.sent=[];self.policy_version=1
        with plans._LOCK:plans._BINDINGS.clear();plans._ACTIVE.clear();plans._AGAIN.clear()

    def context(self,definition):
        return {'models':{s['id']:{'model_id':'home','model_name':'Fixture','engine':'pi','access_mode':'project'}
                          for s in definition['stages']},'policy':{'fixture_version':self.policy_version},'baseline':None}

    def stage(self,title,deps=()):
        return stages.add_stage(self.goal,title,'Implement '+title,depends_on=deps,acceptance=['Works'],
                                fixed_model='home',kind='chat',project=self.project)['id']

    def approve(self):
        preview=plans.preview(self.goal,self.project)
        return plans.change(self.goal,self.project,'approve',expected=preview['digest'])

    def dispatch(self,stage,choice,proof):
        name='task-'+stage['title'].lower()
        stages.claim_dispatch(stage['id'],name,project=self.project,plan_proof=proof,model_id=choice['model_id'],engine=choice['engine'])
        run=registry.prepare_task_dir(name,self.project)
        registry.patch_task(run,id=name,status='queued',kind='chat',engine='pi',model_id='home',project=self.project,
            stage_id=stage['id'],goal_id=self.goal,files=[],changed_files=[],answer='fixture',
            route={'requirements':{'acceptance':['Works']}},delegation={'schema':1,'mode':'outcome','rework_count':0})
        self.sent.append(name)
        return {'task':{'id':name}}

    def accept(self,name):
        run=registry.task_dir(name,self.project);registry.patch_task(run,status='completed')
        policy.record_executor(run,{'completed':True,'changed_files':[],'test':{'exit_code':0}})
        record=policy.load(run)
        policy.accept(run,'main',[{'id':r['id'],'status':'pass','evidence':[{'source':'reviewer_test','detail':'fixture independent check','exit_code':0}]} for r in record['criteria']])

    def test_preview_is_read_only_and_does_not_initialize_an_empty_project(self):
        fresh=self.root/'empty';fresh.mkdir()
        with self.assertRaises(ValueError):plans.preview('missing',str(fresh))
        self.assertFalse((fresh/'.model-desk').exists())
        result=plans.preview(self.goal,self.project)
        self.assertTrue(result['read_only']);self.assertEqual(len(result['document']['definition']['stages']),3)
        self.assertIsNone(plans.view(self.goal,self.project)['plan'])
        self.assertEqual(self.sent,[])

    def test_unapproved_never_dispatches(self):
        plans.advance(self.goal,self.project,self.dispatch);self.assertEqual(self.sent,[])

    def test_approve_requires_exact_preview_and_is_idempotent(self):
        with self.assertRaises(ValueError):plans.change(self.goal,self.project,'approve',expected='wrong')
        first=self.approve();second=self.approve()
        self.assertEqual(first['plan']['revision'],second['plan']['revision'])
        self.assertEqual(self.sent,[])

    def test_two_independent_stages_submit_once_and_dependent_waits(self):
        self.approve()
        for _ in range(3):plans.advance(self.goal,self.project,self.dispatch)
        self.assertEqual(self.sent,['task-a','task-b'])
        self.assertEqual(plans.view(self.goal,self.project)['plan']['observations'][self.c],'waiting_prerequisites')

    def test_concurrent_notifications_never_double_submit(self):
        self.approve()
        with ThreadPoolExecutor(max_workers=3) as pool:
            list(pool.map(lambda _:plans.advance(self.goal,self.project,self.dispatch),range(6)))
        self.assertEqual(sorted(self.sent),['task-a','task-b'])

    def test_executor_completion_is_not_acceptance(self):
        self.approve();plans.advance(self.goal,self.project,self.dispatch)
        for name in self.sent:
            run=registry.task_dir(name,self.project);registry.patch_task(run,status='completed')
            policy.record_executor(run,{'completed':True,'changed_files':[],'test':{'exit_code':0}})
        plans.advance(self.goal,self.project,self.dispatch)
        self.assertNotIn('task-c',self.sent)

    def test_dependencies_need_real_acceptance_and_valid_handover(self):
        self.approve();plans.advance(self.goal,self.project,self.dispatch)
        self.accept('task-a');self.accept('task-b')
        plans.advance(self.goal,self.project,self.dispatch)
        self.assertNotIn('task-c',self.sent)
        with patch.object(task_handover,'prerequisite_handovers',return_value=[]),\
             patch.object(task_handover,'prerequisite_baseline',return_value=None):
            plans.advance(self.goal,self.project,self.dispatch)
        self.assertEqual(self.sent,['task-a','task-b','task-c'])

    def test_stale_handover_does_not_dispatch(self):
        self.approve();plans.advance(self.goal,self.project,self.dispatch);self.accept('task-a');self.accept('task-b')
        with patch.object(task_handover,'prerequisite_handovers',side_effect=ValueError('stale')):
            plans.advance(self.goal,self.project,self.dispatch)
        self.assertNotIn('task-c',self.sent)

    def test_pause_revoke_and_goal_cancel_prevent_new_work(self):
        for action in ('pause','revoke'):
            value=self.approve()['plan']
            plans.change(self.goal,self.project,action,expected=value['digest'])
            plans.advance(self.goal,self.project,self.dispatch);self.assertEqual(self.sent,[])
        self.approve();stages.set_goal_status(self.goal,'cancelled',project=self.project)
        plans.advance(self.goal,self.project,self.dispatch);self.assertEqual(self.sent,[])

    def test_switch_off_prevents_dispatch_without_changing_settings(self):
        self.approve()
        with patch.object(workflow_settings,'snapshot',return_value={'enabled':False}):
            plans.advance(self.goal,self.project,self.dispatch)
        self.assertEqual(self.sent,[])

    def test_definition_change_invalidates_approval(self):
        self.approve();self.stage('D')
        plans.advance(self.goal,self.project,self.dispatch)
        self.assertEqual(plans.view(self.goal,self.project)['plan']['status'],'needs_approval');self.assertEqual(self.sent,[])

    def test_model_permissions_or_baseline_change_invalidates_approval(self):
        self.approve();self.policy_version+=1
        plans.advance(self.goal,self.project,self.dispatch)
        self.assertEqual(plans.view(self.goal,self.project)['plan']['status'],'needs_approval');self.assertEqual(self.sent,[])

    def test_unknown_after_intent_is_never_retried_even_after_new_approval(self):
        self.approve();called=[]
        def broken(*args):called.append(args[0]['id']);raise OSError('lost response')
        plans.advance(self.goal,self.project,broken)
        self.policy_version+=1;self.approve()
        plans.advance(self.goal,self.project,self.dispatch)
        self.assertEqual(set(called),{self.a,self.b});self.assertEqual(self.sent,[])
        self.assertEqual(plans.view(self.goal,self.project)['plan']['attempts'][self.a]['state'],'unknown')
        record=plans.view(self.goal,self.project)['plan']
        self.assertEqual(record['revision'],2)
        self.assertEqual(record['attempts'][self.a]['approval_revision'],1)
        self.assertEqual(record['revisions'][0]['document']['context']['policy']['fixture_version'],1)

    def test_proven_unsubmitted_needs_explicit_resume_and_keeps_history(self):
        plan=self.approve()['plan']
        def refused(*_):raise plans.NotSubmitted()
        plans.advance(self.goal,self.project,refused)
        plans.advance(self.goal,self.project,self.dispatch);self.assertEqual(self.sent,[])
        plans.change(self.goal,self.project,'resume',expected=plan['digest'])
        plans.advance(self.goal,self.project,self.dispatch)
        self.assertEqual(self.sent,['task-a','task-b'])
        self.assertEqual(len(plans.view(self.goal,self.project)['plan']['attempt_history']),2)

    def test_claim_rechecks_plan_after_concurrent_stage_edit(self):
        self.approve()
        def changed(stage,choice,proof):
            self.stage('Extra')
            return self.dispatch(stage,choice,proof)
        plans.advance(self.goal,self.project,changed)
        self.assertEqual(self.sent,[])

    def test_wrong_project_and_unknown_goal_are_rejected(self):
        other=self.root/'other';other.mkdir()
        with self.assertRaises(ValueError):plans.view(self.goal,str(other))
        with self.assertRaises(ValueError):plans.preview(self.goal,'relative')

    def test_restart_does_not_auto_reattach_or_dispatch(self):
        self.approve();self.assertFalse(plans.view(self.goal,self.project)['attached'])
        plans.wake(self.project,self.goal);self.assertEqual(self.sent,[])

    def test_wrong_actual_model_or_engine_is_rejected_at_atomic_claim(self):
        self.approve()
        def wrong(stage,choice,proof):
            stages.claim_dispatch(stage['id'],'wrong',project=self.project,plan_proof=proof,model_id='other',engine='zcode')
        plans.advance(self.goal,self.project,wrong)
        self.assertEqual(stages.get_stage(self.a,self.project)['task_ids'],[])

    def test_revoke_serializes_with_a_dispatch_already_in_admission(self):
        import threading
        value=self.approve()['plan'];entered=threading.Event();finish=threading.Event();revoked=threading.Event()
        def blocked(stage,choice,proof):
            entered.set();self.assertTrue(finish.wait(5));return self.dispatch(stage,choice,proof)
        with ThreadPoolExecutor(max_workers=2) as pool:
            first=pool.submit(plans.advance,self.goal,self.project,blocked)
            self.assertTrue(entered.wait(5))
            def revoke():
                plans.change(self.goal,self.project,'revoke',expected=value['digest']);revoked.set()
            second=pool.submit(revoke)
            self.assertFalse(revoked.wait(.05));finish.set();first.result();second.result()
        before=list(self.sent);plans.advance(self.goal,self.project,self.dispatch)
        self.assertEqual(self.sent,before);self.assertTrue(revoked.is_set())

    def test_actual_model_policy_snapshot_and_permissions_invalidate_approval(self):
        # Exercise the unmocked context builder using fixture-only configuration.
        registry.write_json(registry.REGISTRY,{'default_model':'home','models':[{'id':'home','name':'Local fixture',
            'provider':'local','model':'fixture','enabled':True,'default_agent':'pi','api_base':'http://fixture.invalid',
            'max_input_tokens':32768,'max_output_tokens':4096}]})
        with patch.object(plans,'_context',wraps=REAL_CONTEXT):
            first=plans.preview(self.goal,self.project)
            plans.change(self.goal,self.project,'approve',expected=first['digest'])
            import endpoint_access
            endpoint_access.update('pi','project')
            later=plans.preview(self.goal,self.project)
            self.assertNotEqual(first['digest'],later['digest'])
        self.assertEqual(self.sent,[])

    def test_real_controller_queue_and_atomic_plan_claim_without_model_calls(self):
        import panel_server as panel
        import pi_worker
        registry.write_json(registry.REGISTRY,{'default_model':'home','models':[{'id':'home','name':'Local fixture',
            'provider':'local','model':'fixture','enabled':True,'default_agent':'pi','api_base':'http://fixture.invalid',
            'max_input_tokens':32768,'max_output_tokens':4096}]})
        stages.cancel_stage(self.b,'fixture cancelled branch',project=self.project)
        self.approve()
        with patch.object(panel.EXECUTOR,'submit') as submitted,patch.object(pi_worker,'availability',return_value={'ready':True}):
            result=plans.advance(self.goal,self.project,panel._approved_stage_dispatch)
        attempt=result['plan']['attempts'][self.a]
        self.assertEqual(attempt['state'],'created',attempt)
        self.assertEqual(submitted.call_count,1)
        task=registry.read_task(registry.task_dir(attempt['task_id'],self.project))
        self.assertEqual(task['engine'],'pi');self.assertEqual(task['status'],'queued')
        self.assertEqual(task['stage_id'],self.a)

    def test_event_wakeup_is_coalesced_and_reading_does_not_wake(self):
        scheduled=[]
        self.approve()
        plans.attach(self.goal,self.project,lambda fn,*args:scheduled.append((fn,args)),self.dispatch)
        plans.view(self.goal,self.project)
        plans.wake(self.project,self.goal);plans.wake(self.project,self.goal)
        self.assertEqual(len(scheduled),1)
        fn,args=scheduled.pop(0);fn(*args)
        self.assertEqual(self.sent,['task-a','task-b'])
        for fn,args in scheduled:fn(*args)
        self.assertEqual(self.sent,['task-a','task-b'])

    def test_lost_reply_after_real_task_creation_keeps_original_task(self):
        self.approve()
        def lost(stage,choice,proof):
            self.dispatch(stage,choice,proof);raise OSError('lost receipt')
        plans.advance(self.goal,self.project,lost)
        self.policy_version+=1;self.approve();plans.advance(self.goal,self.project,self.dispatch)
        self.assertEqual(self.sent,['task-a','task-b'])
        self.assertEqual(stages.get_stage(self.a,self.project)['task_ids'],['task-a'])
        self.assertEqual(registry.read_task(registry.task_dir('task-a',self.project))['status'],'queued')

    def test_task_review_event_wakes_original_plan_without_read_polling(self):
        scheduled=[];self.approve()
        plans.attach(self.goal,self.project,lambda fn,*args:scheduled.append((fn,args)),self.dispatch)
        fn,args=scheduled.pop(0);fn(*args)
        self.accept('task-a');self.accept('task-b')
        self.assertTrue(scheduled)
        with patch.object(task_handover,'prerequisite_handovers',return_value=[]),patch.object(task_handover,'prerequisite_baseline',return_value=None):
            while scheduled:
                fn,args=scheduled.pop(0);fn(*args)
        self.assertEqual(self.sent,['task-a','task-b','task-c'])
        self.accept('task-c')
        while scheduled:
            fn,args=scheduled.pop(0);fn(*args)
        self.assertEqual(plans.view(self.goal,self.project)['plan']['status'],'completed')

    def test_regular_goal_views_keep_only_compact_approval_summary(self):
        self.approve()
        for goal in (stages.get_goal(self.goal,self.project),stages.list_goals(self.project)[0]):
            summary=goal['data']['approved_plan']
            self.assertEqual(summary['status'],'approved');self.assertNotIn('document',summary)
        self.assertIn('document',plans.view(self.goal,self.project)['plan'])

    def test_failed_executor_is_visible_and_not_automatically_reworked(self):
        self.approve();plans.advance(self.goal,self.project,self.dispatch)
        run=registry.task_dir('task-a',self.project);registry.patch_task(run,status='failed')
        policy.record_executor(run,{'completed':False,'changed_files':[],'test':{'exit_code':1}})
        result=plans.advance(self.goal,self.project,self.dispatch)
        self.assertEqual(result['plan']['observations'][self.a],'manual_attention')
        self.assertEqual(self.sent,['task-a','task-b'])

    def test_modified_approval_document_is_rejected_without_erasing_intents(self):
        self.approve()
        def corrupt(plan):plan['document']['context']['baseline']='wrong'
        plans._mutate(self.project,self.goal,corrupt)
        with self.assertRaisesRegex(ValueError,'摘要不符'):plans.advance(self.goal,self.project,self.dispatch)
        self.assertEqual(self.sent,[])

    def test_edit_plan_requires_and_pins_real_git_baseline_without_initializing_it(self):
        import subprocess
        self.stage('Edit')
        with stages.connection(self.project) as conn:
            conn.execute("UPDATE stages SET kind='edit' WHERE title='Edit'")
        registry.write_json(registry.REGISTRY,{'default_model':'home','models':[{'id':'home','name':'Fixture',
            'provider':'local','model':'fixture','enabled':True,'default_agent':'pi','api_base':'http://fixture.invalid',
            'max_input_tokens':32768,'max_output_tokens':4096}]})
        with patch.object(plans,'_context',wraps=REAL_CONTEXT):
            with self.assertRaisesRegex(ValueError,'Git基线'):plans.preview(self.goal,self.project)
            self.assertFalse((Path(self.project)/'.git').exists())
            def git(*args):return subprocess.run(['git','-C',self.project,*args],check=True,capture_output=True)
            git('init','--quiet')
            git('-c','user.name=Fixture','-c','user.email=fixture@example.invalid','commit','--allow-empty','-m','fixture')
            preview=plans.preview(self.goal,self.project)
            self.assertEqual(preview['document']['context']['baseline'],git('rev-parse','HEAD').stdout.decode().strip())
            plans.change(self.goal,self.project,'approve',expected=preview['digest'])
            git('-c','user.name=Fixture','-c','user.email=fixture@example.invalid','commit','--allow-empty','-m','advance')
            plans.advance(self.goal,self.project,self.dispatch)
            self.assertEqual(plans.view(self.goal,self.project)['plan']['status'],'needs_approval')
        self.assertEqual(self.sent,[])


REAL_CONTEXT=plans._context


if __name__=='__main__':unittest.main()
