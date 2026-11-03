"""Real Git, isolated records, no models/services/accounts."""
import json
from pathlib import Path
import sys
import threading
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import brain_context
import delegation_policy as policy
import integration_queue as queue
import integration_status
import model_registry as registry
import process_identity
import task_integration as integration
import task_leases
from test_task_workspace_handover import Base, git


class IntegrationQueueTests(Base):
    def test_inconsistent_receipts_never_claim_acceptance(self):
        for row in ({'verdict':'passed','accepted':True},
                    {'verdict':'passed','accepted':True,'integrated_commit':'b'*40,'accepted_commit':'c'*40},
                    {'verdict':'passed','accepted':'true','integrated_commit':'b'*40},
                    {'queue':{'state':[]}}):
            self.assertEqual(queue.state(row),'recovery_required')

    def setUp(self):
        super().setUp()
        self.addCleanup(queue._AUTHORITY.clear)
        self.addCleanup(queue._SUBMIT.clear)
        self.addCleanup(queue._ACTIVE.clear)
        self.addCleanup(queue._AGAIN.clear)

    def case(self,name='task'):
        run,project,plan=self.isolated_task(name=name)
        checks=[[sys.executable,'-c','from pathlib import Path; assert "42" in Path("a.py").read_text()']]
        return run,project,plan,checks

    def test_enqueue_is_durable_but_does_not_merge_or_check(self):
        run,project,plan,checks=self.case()
        with patch.object(integration,'run_integration',side_effect=AssertionError('not during enqueue')):
            row=queue.enqueue(project,[run.name],checks,request_id='first')
        self.assertEqual(queue.state(row),'queued')
        self.assertEqual(row['commits'][run.name],plan['commit'])
        self.assertFalse((project.parent/(project.name+'-task-workspaces')/('integration-'+row['name'])).exists())
        self.assertEqual(queue.get(project,row['name']),row)
        self.assertFalse(row['accepted'])

    def test_same_request_replays_and_changed_parameters_refuse(self):
        run,project,_,checks=self.case()
        row=queue.enqueue(project,[run.name],checks,request_id='same')
        self.assertEqual(queue.enqueue(project,[run.name],checks,request_id='same'),row)
        with self.assertRaisesRegex(ValueError,'参数不同'):
            queue.enqueue(project,[run.name],[[sys.executable,'-c','pass']],request_id='same')

    def test_execute_retains_receipt_then_repeated_run_never_remerges(self):
        run,project,_,checks=self.case()
        row=queue.enqueue(project,[run.name],checks,request_id='run')
        phases=[];original=registry.write_json
        def record(file,data):
            if str(file).endswith(row['name']+'.json'):
                phases.append((data.get('queue') or {}).get('state'))
                if data.get('verdict')=='passed':
                    self.assertIn(task_leases.key('integration',str(project)),task_leases.holders())
            return original(file,data)
        with patch.object(registry,'write_json',side_effect=record):result=queue.run_one(project,row['name'])
        self.assertEqual(queue.state(result),'awaiting_review');self.assertFalse(result['accepted'])
        self.assertTrue({'preparing','merging','checking','awaiting_review'}.issubset(set(phases)))
        self.assertEqual(task_leases.holders(),{})
        with patch.object(integration,'run_integration',side_effect=AssertionError('no second merge')):
            self.assertEqual(queue.run_one(project,row['name']),result)
        accepted=integration.accept_integration(project,row['name'],[{'id':'i1','status':'pass','evidence':[
            {'source':'reviewer_test','detail':'independent fixture check','exit_code':0}]}])
        self.assertEqual(queue.state(accepted),'accepted')
        self.assertIn('return 1',(project/'a.py').read_text())

    def test_conflict_and_check_failure_are_terminal_not_retried(self):
        run,project,_,_=self.case()
        row=queue.enqueue(project,[run.name],[[sys.executable,'-c','raise SystemExit(4)']],request_id='fail')
        result=queue.run_one(project,row['name'])
        self.assertEqual(queue.state(result),'check_failed')
        self.assertTrue(Path(result['integration_copy']['path']).exists())
        with patch.object(integration,'run_integration',side_effect=AssertionError('no retry')):
            self.assertEqual(queue.run_one(project,row['name']),result)

    def test_task_and_plan_must_still_match_before_effects(self):
        run,project,plan,checks=self.case()
        row=queue.enqueue(project,[run.name],checks,request_id='stale')
        (Path(plan['path'])/'a.py').write_text('post acceptance dirty')
        with patch.object(integration,'run_integration') as effect,self.assertRaises(ValueError):
            queue.run_one(project,row['name'])
        effect.assert_not_called();self.assertEqual(queue.state(queue.get(project,row['name'])),'blocked')
        self.assertEqual(task_leases.holders(),{})

    def test_claim_cancel_and_project_lock_preserve_single_effect(self):
        run,project,_,checks=self.case()
        row=queue.enqueue(project,[run.name],checks,request_id='cancel')
        task_leases.acquire('integration',str(project),'foreign')
        with self.assertRaises(task_leases.LeaseConflict):queue.run_one(project,row['name'])
        self.assertEqual(queue.state(queue.get(project,row['name'])),'queued')
        self.assertEqual(queue.state(queue.cancel(project,row['name'])),'cancelled')
        self.assertEqual(task_leases.holders()[task_leases.key('integration',str(project))]['task_id'],'foreign')
        task_leases.release('integration',str(project),'foreign')
        with patch.object(integration,'run_integration',side_effect=AssertionError('cancelled')):
            self.assertEqual(queue.state(queue.run_one(project,row['name'])),'cancelled')

    def test_crash_after_claim_keeps_unknown_work_and_lease(self):
        run,project,_,checks=self.case()
        row=queue.enqueue(project,[run.name],checks,request_id='crash')
        with patch.object(integration,'run_integration',side_effect=OSError('lost operation')):
            with self.assertRaises(OSError):queue.run_one(project,row['name'])
        unknown=queue.get(project,row['name'])
        self.assertEqual(queue.state(unknown),'recovery_required')
        held=task_leases.holders()
        with patch.object(queue,'_runner_state',return_value=False):queue.reconcile(project,row['name'])
        self.assertEqual(task_leases.holders(),held)
        with patch.object(integration,'run_integration',side_effect=AssertionError('must not replay')):
            queue.run_one(project,row['name'])

    def test_bad_identity_never_starts_and_unreadable_record_is_not_empty(self):
        run,project,_,checks=self.case()
        row=queue.enqueue(project,[run.name],checks,request_id='identity')
        with patch.object(process_identity,'capture',return_value=None),patch.object(integration,'run_integration') as effect:
            with self.assertRaises(ValueError):queue.run_one(project,row['name'])
        effect.assert_not_called();self.assertEqual(task_leases.holders(),{})
        (project/'.model-desk/integrations'/ (row['name']+'.json')).write_text('{')
        self.assertFalse(queue.snapshot(project)['available'])
        with self.assertRaises(ValueError):queue.enqueue(project,[run.name],checks,request_id='identity')

    def test_original_owner_required_even_after_restart(self):
        run,project,_,checks=self.case()
        ctx=brain_context.build_context(project=str(project),host='dsh',owner='original',role='brain',operator_session='s',session_binding='user_bound')
        registry.patch_task(run,source='brain',owner=ctx.owner,brain={'owner':ctx.owner,'project':str(project),'binding_key':ctx.binding_key})
        auth=brain_context.issue_authority(ctx)
        row=queue.enqueue(project,[run.name],checks,request_id='owned',authority=auth)
        for action in [lambda:queue.run_one(project,row['name']),lambda:queue.cancel(project,row['name']),lambda:queue.reconcile(project,row['name'])]:
            with self.assertRaises(ValueError):action()
        self.assertEqual(queue.state(queue.run_one(project,row['name'],authority=auth)),'awaiting_review')

    def test_restart_reads_existing_intent_and_preserves_frozen_baseline(self):
        run,project,_,checks=self.case()
        row=queue.enqueue(project,[run.name],checks,request_id='restart')
        self.assertEqual(queue.snapshot(project)['records'][0]['state'],'queued')
        (project/'new.txt').write_text('new source baseline');git(project,'add','new.txt');git(project,'commit','-m','advance unrelated original')
        result=queue.run_one(project,row['name'])
        self.assertEqual(result['baseline'],row['baseline'])
        self.assertTrue((project/'new.txt').is_file())

    def test_two_project_jobs_use_existing_executor_and_fifo(self):
        run,project,_,checks=self.case()
        first=queue.enqueue(project,[run.name],checks,request_id='first')
        second=queue.enqueue(project,[run.name],checks,request_id='second')
        submitted=[]
        queue.schedule(project,first['name'],lambda fn,*args:submitted.append((fn,args)))
        queue.schedule(project,second['name'],lambda fn,*args:submitted.append((fn,args)))
        self.assertEqual(len(submitted),1)
        with patch.object(queue,'run_one',wraps=queue.run_one) as effect:
            submitted.pop(0)[0](str(project))
        self.assertEqual([call.args[1] for call in effect.call_args_list],[first['name'],second['name']])
        self.assertEqual(submitted,[])
        for row in [first,second]:self.assertEqual(queue.state(queue.get(project,row['name'])),'awaiting_review')

    def test_invalid_requests_never_create_integration_record(self):
        run,project,_,checks=self.case()
        for ids,commands,key in [([],checks,'x'),([run.name,run.name],checks,'x'),([run.name],[],'x'),([run.name],checks,'../bad')]:
            with self.assertRaises(ValueError):queue.enqueue(project,ids,commands,request_id=key)
        self.assertFalse((project/'.model-desk/integrations').exists())

    def test_real_conflict_is_preserved_as_integration_failure(self):
        first,project,_,checks=self.case('first')
        second,_,_=self.isolated_task(name='second',project=project,deliverable='def value():\n    return 99\n')
        row=queue.enqueue(project,[first.name,second.name],checks,request_id='conflict')
        result=queue.run_one(project,row['name'])
        self.assertEqual(queue.state(result),'conflict')
        self.assertEqual(result['checks'],[])
        self.assertTrue(any(m['conflicts'] for m in result['merges']))
        self.assertFalse(result['accepted'])

    def test_mutated_checks_or_frozen_baseline_never_execute(self):
        run,project,_,checks=self.case()
        for index,field in enumerate(('checks','baseline')):
            row=queue.enqueue(project,[run.name],checks,request_id='mutated'+str(index))
            file=project/'.model-desk/integrations'/(row['name']+'.json')
            if field=='checks':row['queue']['check_commands']=[[sys.executable,'-c','raise SystemExit(9)']]
            else:row['baseline']='0'*40
            registry.write_json(file,row)
            with patch.object(integration,'run_integration') as effect,self.assertRaises(ValueError):queue.run_one(project,row['name'])
            effect.assert_not_called()

    def test_dead_or_unreadable_runner_projection_never_mutates_record_or_lease(self):
        run,project,_,checks=self.case()
        row=queue.enqueue(project,[run.name],checks,request_id='observed')
        row['queue'].update(state='checking',runner={'invalid':'identity'})
        file=project/'.model-desk/integrations'/(row['name']+'.json');registry.write_json(file,row)
        before=file.read_bytes()
        for verdict in (None,False):
            with patch.object(queue,'_runner_state',return_value=verdict):
                self.assertEqual(queue.snapshot(project)['records'][0]['state'],'recovery_required')
        self.assertEqual(file.read_bytes(),before)
        task=registry.read_task(run)
        self.assertEqual(integration_status.for_task(integration_status.read_project(str(project)),task)['state'],'recovery_required')
        row['queue']['state']=[];registry.write_json(file,row)
        self.assertFalse(queue.snapshot(project)['available'])
        self.assertEqual(integration_status.for_task(integration_status.read_project(str(project)),task)['state'],'unavailable')

    def test_simultaneous_claims_do_not_share_a_reentrant_lease_owner(self):
        run,project,_,checks=self.case()
        row=queue.enqueue(project,[run.name],checks,request_id='race')
        entered=threading.Event();release=threading.Event();result=[]
        original=integration.run_integration
        def pause(*args,**kwargs):entered.set();release.wait(10);return original(*args,**kwargs)
        def execute():
            try:result.append(queue.run_one(project,row['name']))
            except Exception as e:result.append(e)
        with patch.object(integration,'run_integration',side_effect=pause):
            worker=threading.Thread(target=execute);worker.start();self.assertTrue(entered.wait(10))
            # A repeated call may read the running receipt, but can never execute it.
            again=queue.run_one(project,row['name']);self.assertEqual(queue.state(again),'preparing')
            with self.assertRaises(ValueError):queue.cancel(project,row['name'])
            release.set();worker.join(20);self.assertFalse(worker.is_alive())
        self.assertEqual(queue.state(result[0]),'awaiting_review')
