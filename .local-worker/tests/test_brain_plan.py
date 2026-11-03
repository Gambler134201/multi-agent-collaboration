"""Signed plan lifecycle and controller claims, no real executor/model calls."""
from pathlib import Path
import json
import unittest
from unittest.mock import patch

import test_approved_plan as fixture
import approved_plan as plans
import brain_plan
import brain_context
import brain_gateway as gateway
import brain_requests
import brain_transport
import model_registry as registry
import stage_store as stages
import delegation_policy as policy
import task_finalization
import task_handover
import panel_server as panel
import pi_worker


class BrainPlanTests(unittest.TestCase):
    def test_draft_checks_are_visible_to_operator_and_owner_resume_keeps_exact_digest(self):
        checks=[['fixture-python','joint_check.py']]
        result=self.call('draft',{'request_id':'checks-draft','title':'Plan','objective':'Fixture',
            'stages':[{'key':'a','title':'A','objective':'Fixture only','model_id':'home','kind':'chat','acceptance':['Works']}],
            'checks':checks})
        goal=result['receipt']['goal_id']
        preview=self.call('preview',{'goal_id':goal})['result']
        self.assertEqual(preview['document']['integration_checks'],checks)
        plans.change(goal,self.f.project,'approve',expected=preview['digest'])
        self.call('resume',{'request_id':'checks-resume','goal_id':goal,'digest':preview['digest']})
        self.assertTrue(plans.view(goal,self.f.project)['attached'])
        self.assertEqual(self.task_ids,[])  # Enqueuing the local pump isn't a model call.

    def test_original_brain_integration_adapter_uses_signed_existing_queue(self):
        for host in ('codex','dsh'):
            _,context=self.connection(host=host,session='integration-'+host)
            with patch.object(gateway,'_integrate',return_value={'integration':{}}) as integrate,\
                 patch.object(brain_plan,'require_owner',return_value=({}, {'digest':'digest'})),\
                 patch.object(plans,'preview',return_value={'digest':'digest'}):
                brain_plan._attach(context,self.f.goal,'digest')
                binding=plans._BINDINGS[(context.project,self.f.goal)]
                binding[2](['original-a','original-b'],[['python','check.py']],'original-request')
                actual=integrate.call_args
                self.assertIs(actual.args[0],context)
                self.assertEqual(actual.args[1],{'task_ids':['original-a','original-b'],
                    'checks':[['python','check.py']],'request_id':'original-request','enqueue':True})

    def setUp(self):
        self.f=fixture.PlanTests();self.f.setUp();self.addCleanup(self.f.doCleanups)
        self.enterContext(patch.dict(gateway._CAPABILITIES,clear=True))
        self.enterContext(patch.object(pi_worker,'availability',return_value={'ready':True}))
        registry.write_json(registry.REGISTRY,{'default_model':'home','models':[{'id':'home','name':'Local fixture',
            'provider':'local','model':'fixture','enabled':True,'default_agent':'pi','api_base':'http://fixture.invalid',
            'max_input_tokens':32768,'max_output_tokens':4096}]})
        self.scheduled=[];self.task_ids=[]
        self.enterContext(patch.object(panel.EXECUTOR,'submit',side_effect=lambda fn,*args,**kw:self.scheduled.append((fn,args,kw))))
        self.cap,self.context=self.connection()
        self.addCleanup(self.clear_bindings)

    def clear_bindings(self):
        with plans._LOCK:plans._BINDINGS.clear();plans._ACTIVE.clear();plans._AGAIN.clear()

    def connection(self,*,host='codex',session='a',role='brain',project=None):
        context=brain_context.build_context(project=project or self.f.project,host=host,owner='owner-'+session,
            role=role,operator_session=session,session_binding='user_bound')
        result=gateway.register_connection(context.declaration())
        return result['capability'],context

    def call(self,action,payload=None,cap=None):return gateway.handle('plan_'+action,payload or {},cap or self.cap)

    def draft(self,*,request='draft',cap=None,two=False):
        rows=[{'key':'a','title':'A','objective':'Return a fixture result','kind':'chat','model_id':'home','acceptance':['Works']}]
        if two:rows.append({'key':'b','title':'B','objective':'Read the accepted upstream summary','kind':'chat',
                           'model_id':'home','acceptance':['Works'],'depends_on':['a']})
        return self.call('draft',{'request_id':request,'title':'Plan','objective':'Fixture only','stages':rows},cap=cap)

    def approve(self,goal):
        preview=plans.preview(goal,self.f.project)
        panel.approved_plan_action({'mode':'project','project':self.f.project},goal,'approve',
            {'digest':preview['digest'],'confirm':True})
        return preview['digest']

    def drain(self):
        turns=0
        while self.scheduled:
            fn,args,kw=self.scheduled.pop(0);turns+=1;self.assertLess(turns,50)
            if fn is panel.execute_task:self.task_ids.append(args[0])
            else:fn(*args,**kw)

    def complete_and_accept(self,task_id,cap=None,request='accept'):
        run=registry.task_dir(task_id,self.f.project)
        registry.patch_task(run,status='completed',phase='fixture done',answer='fixture result',ended_at=registry.now())
        (run/'answer.txt').write_text('fixture result',encoding='utf-8')
        policy.record_executor(run,{'completed':True,'changed_files':[],'test':{'exit_code':0}})
        task_finalization.settle(run)
        review=policy.load(run)
        criteria=[{'id':c['id'],'status':'pass','evidence':[{'source':'reviewer_inspection','detail':'Independent fixture answer and state assertions'}]} for c in review['criteria']]
        gateway.handle('review',{'request_id':request,'task_id':task_id,'action':'accept','criteria':criteria},cap or self.cap)

    def test_draft_is_not_approval_or_execution_and_is_replayed(self):
        first=self.draft();again=self.draft()
        self.assertEqual(first['receipt'],again['receipt']);self.assertTrue(again['reused'])
        goal=first['receipt']['goal_id'];result=self.call('status',{'goal_id':goal})['result']
        self.assertIsNone(result['plan']);self.assertEqual(self.scheduled,[])
        self.assertEqual(result['responsibility']['binding_key'],self.context.binding_key)
        self.assertEqual(len(stages.list_stages(goal,self.f.project)),1)
        with self.assertRaises(gateway.BrokerError):self.call('resume',{'request_id':'early','goal_id':goal,'digest':'wrong'})

    def test_both_hosts_approve_resume_review_handover_and_advance_original_tasks(self):
        for host in ('codex','dsh'):
            with self.subTest(host=host):
                cap,context=self.connection(host=host,session=host)
                goal=self.draft(request='draft-'+host,cap=cap,two=True)['receipt']['goal_id']
                digest=self.approve(goal)
                self.assertEqual(self.scheduled,[])  # UI cannot submit unsigned tasks.
                self.call('resume',{'request_id':'resume-'+host,'goal_id':goal,'digest':digest},cap=cap);self.drain()
                initial=list(self.task_ids);task_id=initial[-1]
                state=registry.read_task(registry.task_dir(task_id,self.f.project))
                self.assertEqual(state['brain']['owner'],context.owner);self.assertEqual(state['engine'],'pi')
                status=self.call('status',{'goal_id':goal},cap=cap)['result']
                attempt=next(iter(status['plan']['attempts'].values()))
                self.assertNotIn('token',attempt);self.assertEqual(attempt['request_id'],state['brain']['request_id'])
                self.assertNotIn('document',status['plan']);self.assertNotIn('revisions',status['plan'])
                lookup=gateway.handle('request_status',{'request_id':attempt['request_id']},cap)
                self.assertEqual(lookup['task']['task_id'],task_id)
                self.complete_and_accept(task_id,cap,request='review-'+host);self.drain()
                self.assertEqual(self.task_ids,initial)  # accepted alone is no handover.
                self.call('handover',{'request_id':'handover-'+host,'task_id':task_id,'summary':'Verified fixture'},cap=cap)
                self.drain();self.assertEqual(len(self.task_ids),len(initial)+1)
                downstream=self.task_ids[-1]
                self.assertIn('fixture', (registry.task_dir(downstream,self.f.project)/'prompt.txt').read_text(encoding='utf-8'))
                self.complete_and_accept(downstream,cap,request='review-final-'+host);self.drain()
                self.assertEqual(self.call('status',{'goal_id':goal},cap=cap)['result']['plan']['status'],'completed')

    def test_only_original_brain_can_resume_and_operator_pause_is_not_overridden(self):
        goal=self.draft()['receipt']['goal_id'];digest=self.approve(goal)
        other,_=self.connection(session='other')
        with self.assertRaises(gateway.BrokerError):self.call('resume',{'request_id':'steal','goal_id':goal,'digest':digest},cap=other)
        plans.change(goal,self.f.project,'pause',expected=digest)
        with self.assertRaises(gateway.BrokerError):self.call('resume',{'request_id':'unpause','goal_id':goal,'digest':digest})
        self.assertEqual(self.scheduled,[])

    def test_plain_controller_cannot_dispatch_a_brain_owned_plan(self):
        goal=self.draft()['receipt']['goal_id'];self.approve(goal)
        plans.advance(goal,self.f.project,panel._approved_stage_dispatch)
        self.assertEqual(self.scheduled,[])
        self.assertTrue(all(not s['task_ids'] for s in stages.list_stages(goal,self.f.project)))

    def test_unknown_dispatch_keeps_original_task_and_original_request_query(self):
        goal=self.draft()['receipt']['goal_id'];digest=self.approve(goal)
        original=brain_requests.complete
        def fail_dispatch(project,owner,request_id,receipt,task_id=''):
            if request_id.startswith('stage-'):raise OSError('fixture lost receipt')
            return original(project,owner,request_id,receipt,task_id)
        with patch.object(brain_requests,'complete',side_effect=fail_dispatch):
            self.call('resume',{'request_id':'resume','goal_id':goal,'digest':digest});self.drain()
        status=self.call('status',{'goal_id':goal})['result']
        attempt=next(iter(status['plan']['attempts'].values()))
        self.assertEqual(attempt['state'],'unknown');self.assertEqual(len(self.task_ids),1)
        lookup=gateway.handle('request_status',{'request_id':attempt['request_id']},self.cap)
        self.assertEqual(lookup['request_state'],'task_found')
        self.call('resume',{'request_id':'resume-explicit','goal_id':goal,'digest':digest});self.drain()
        self.assertEqual(len(self.task_ids),1)

    def test_lost_draft_receipt_has_readonly_plan_facts_and_never_duplicates_goal(self):
        with patch.object(brain_requests,'complete',side_effect=OSError('fixture disk full')):
            with self.assertRaises(brain_requests.UnknownOutcome):self.draft()
        before=stages.list_goals(self.f.project)
        result=gateway.handle('request_status',{'request_id':'draft'},self.cap)
        self.assertEqual(result['request_state'],'plan_found');self.assertTrue(result['read_only'])
        self.assertFalse(result['safe_to_redispatch'])
        with self.assertRaises(brain_requests.UnknownOutcome):self.draft()
        self.assertEqual(len(stages.list_goals(self.f.project)),len(before));self.assertEqual(self.scheduled,[])

    def test_partial_draft_cannot_be_approved_or_completed_by_prepare(self):
        original=stages.add_stage;count=0
        def partial(*args,**kw):
            nonlocal count
            count+=1
            if count==2:raise OSError('fixture interrupted draft')
            return original(*args,**kw)
        with patch.object(stages,'add_stage',side_effect=partial):
            with self.assertRaises(brain_requests.UnknownOutcome):self.draft(two=True)
        goal=next(g for g in stages.list_goals(self.f.project) if g['title']=='Plan')['id']
        self.assertFalse(self.call('status',{'goal_id':goal})['result']['responsibility']['ready'])
        with self.assertRaises(ValueError):plans.preview(goal,self.f.project)
        with self.assertRaises(gateway.BrokerError):self.call('prepare',{'request_id':'no-repair','goal_id':goal})
        self.assertEqual(self.scheduled,[])

    def test_draft_cycle_unknown_fields_and_executor_role_are_rejected_before_writes(self):
        before=len(stages.list_goals(self.f.project))
        payload={'request_id':'bad','title':'bad','objective':'bad','stages':[{'key':'a','title':'A','objective':'A','model_id':'home','acceptance':['Works'],'depends_on':['a']}]}
        with self.assertRaises(gateway.BrokerError):self.call('draft',payload)
        with self.assertRaises(gateway.BrokerError):self.call('status',{'goal_id':self.f.goal,'owner':'spoof'})
        cap,_=self.connection(role='executor',session='executor')
        with self.assertRaises((gateway.BrokerError,brain_context.ContextError)):self.draft(cap=cap)
        self.assertEqual(len(stages.list_goals(self.f.project)),before)

    def test_prepare_refuses_legacy_tasks_and_another_brain_proposal(self):
        self.call('prepare',{'request_id':'prepare','goal_id':self.f.goal})
        cap,_=self.connection(session='other')
        with self.assertRaises(gateway.BrokerError):self.call('prepare',{'request_id':'other','goal_id':self.f.goal},cap=cap)
        self.assertEqual(self.scheduled,[])

    def test_transport_rejects_wrong_owner_or_goal_receipts_without_retry(self):
        transport=brain_transport.BrainTransport();transport._capability='synthetic'
        transport._context={'context':self.context.public()}
        data={'operation':'plan_resume','request_id':'resume','owner':'other','project':self.f.project,
              'receipt':{'action':'resume','goal_id':'goal','request_reference':self.context.binding_key+'/resume'}}
        with patch.object(transport,'_request',return_value=(200,data)) as request:
            with self.assertRaises(brain_transport.OutcomeUnknown):transport.call('plan_resume',{'request_id':'resume','goal_id':'goal','digest':'a'})
            request.assert_called_once()
        data={'operation':'plan_status','goal_id':'other','result':{},'read_only':True}
        with patch.object(transport,'_request',return_value=(200,data)):
            with self.assertRaises(brain_transport.TransportError):transport.call('plan_status',{'goal_id':'goal'})
        reference=self.context.binding_key+'/original'
        data={'request_id':'original','request_kind':'plan','request_state':'plan_found',
            'read_only':True,'automatic_retry':False,'project':self.f.project,'owner':self.context.owner,
            'request_reference':reference,'plan':{'action':'draft','goal_id':'goal','request_reference':reference}}
        with patch.object(transport,'_request',return_value=(200,data)):
            self.assertEqual(transport.call('request_status',{'request_id':'original'})['plan']['goal_id'],'goal')
        for altered in ({**data,'owner':'other'},{**data,'plan':{}},
                {**data,'plan':{**data['plan'],'request_reference':'other/original'}}):
            with patch.object(transport,'_request',return_value=(200,altered)) as request:
                with self.assertRaises(brain_transport.TransportError):transport.call('request_status',{'request_id':'original'})
                request.assert_called_once()

    def test_explicit_other_project_cannot_read_or_resume_this_plan(self):
        goal=self.draft()['receipt']['goal_id'];digest=self.approve(goal)
        other=Path(self.f.root)/'other-project';other.mkdir()
        cap,_=self.connection(project=str(other),session='other-project')
        for action,payload in [('status',{'goal_id':goal}),('preview',{'goal_id':goal}),
                ('resume',{'goal_id':goal,'digest':digest,'request_id':'foreign-project'})]:
            with self.subTest(action=action),self.assertRaises(gateway.BrokerError):self.call(action,payload,cap=cap)
        self.assertEqual(self.scheduled,[])

    def test_handover_requires_acceptance_and_recovers_only_original_persisted_reference(self):
        goal=self.draft()['receipt']['goal_id'];digest=self.approve(goal)
        self.call('resume',{'request_id':'resume','goal_id':goal,'digest':digest});self.drain()
        task_id=self.task_ids[-1]
        with self.assertRaises(gateway.BrokerError):
            self.call('handover',{'request_id':'too-early','task_id':task_id})
        self.assertIsNone(brain_requests.lookup(self.f.project,self.context.binding_key,'too-early'))
        self.assertIsNone(task_handover.load(registry.task_dir(task_id,self.f.project)))
        self.complete_and_accept(task_id)
        # The handover saved its own original reference, but the compact goal
        # receipt and journal completion are lost. Lookup may report facts only.
        with patch.object(brain_plan,'_record',side_effect=OSError('fixture lost receipt')),\
                patch.object(brain_plan,'recover',return_value=None):
            with self.assertRaises(brain_requests.UnknownOutcome):
                self.call('handover',{'request_id':'lost-handover','task_id':task_id,'summary':'Checked'})
        before=list(self.task_ids)
        found=gateway.handle('request_status',{'request_id':'lost-handover'},self.cap)
        self.assertEqual(found['request_state'],'plan_found')
        self.assertEqual(found['plan']['task_id'],task_id)
        self.assertFalse(found['safe_to_redispatch']);self.assertEqual(self.task_ids,before)
        other,_=self.connection(session='foreign-handover')
        with self.assertRaises(gateway.BrokerError):
            self.call('handover',{'request_id':'steal-handover','task_id':task_id},cap=other)


if __name__=='__main__':unittest.main()
