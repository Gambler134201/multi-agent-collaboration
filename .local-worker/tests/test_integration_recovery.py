"""Recovery from durable facts, isolated Git/leases and owned child processes."""
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import integration_queue as queue
import model_registry as registry
import task_integration as integration
import task_leases
from test_task_workspace_handover import Base, git


class IntegrationRecoveryTests(Base):
    def setUp(self):
        super().setUp()
        for item in (queue._AUTHORITY,queue._SUBMIT,queue._ACTIVE,queue._AGAIN):
            item.clear();self.addCleanup(item.clear)

    def intent(self,checks=None):
        run,project,plan=self.isolated_task()
        row=queue.enqueue(project,[run.name],checks or [[sys.executable,'-c','pass']],request_id='recovery')
        return project,row

    def completed_but_held(self,checks=None):
        project,row=self.intent(checks)
        # An effect-complete controller cannot release after its final receipt.
        with patch.object(task_leases,'release',side_effect=OSError('controller exited before release')):
            with self.assertRaises(OSError):queue.run_one(project,row['name'])
        return project,queue.get(project,row['name'])

    def persist(self,project,row):
        registry.write_json(project/'.model-desk/integrations'/(row['name']+'.json'),row)

    def test_terminal_receipt_recovers_only_own_lease_without_checks_or_acceptance(self):
        project,row=self.completed_but_held();name=row['name']
        before=copy.deepcopy(row)
        task_leases.acquire('model','other-model','other-task')
        foreign=task_leases.holders()[task_leases.key('model','other-model')]
        file=project/'.model-desk/integrations'/(name+'.json');raw=file.read_bytes()
        with patch.object(queue,'_runner_state',return_value=False):
            self.assertTrue(queue.observe(row)['recovery']['can_settle'])
            self.assertEqual(file.read_bytes(),raw)
            with patch.object(integration,'run_integration',side_effect=AssertionError('no effects')),patch.object(integration,'_wake_queue',side_effect=AssertionError('no scheduling during recovery')):
                result=queue.reconcile(project,name)
                repeat=queue.reconcile(project,name)
        self.assertEqual(result['queue']['reconciliation']['state'],'settled')
        self.assertEqual(repeat['queue']['reconciliation']['state'],'settled')
        self.assertFalse(result['accepted']);self.assertEqual(result['checks'],before['checks'])
        self.assertEqual(result['verdict'],before['verdict'])
        self.assertEqual(result['integrated_commit'],before['integrated_commit'])
        self.assertEqual(task_leases.holders(),{task_leases.key('model','other-model'):foreign})
        self.assertIn('return 1',(project/'a.py').read_text())

    def test_alive_and_unknown_runner_preserve_exact_occupancy(self):
        project,row=self.completed_but_held();held=task_leases.holders()
        for verdict,state in ((True,'running'),(None,'unknown')):
            with patch.object(queue,'_runner_state',return_value=verdict):
                self.assertEqual(queue.observe(row)['recovery']['state'],state)
                result=queue.reconcile(project,row['name'])
                self.assertEqual(result['queue']['reconciliation']['state'],state)
            self.assertEqual(task_leases.holders(),held)

    def test_partial_result_even_with_git_commit_never_becomes_success_or_releases(self):
        project,row=self.intent()
        with patch.object(integration,'_run_check',side_effect=OSError('lost result')):
            with self.assertRaises(OSError):queue.run_one(project,row['name'])
        row=queue.get(project,row['name']);held=task_leases.holders()
        self.assertTrue(row['integrated_commit'])
        self.assertTrue(Path(row['integration_copy']['path']).is_dir())
        with patch.object(queue,'_runner_state',return_value=False):result=queue.reconcile(project,row['name'])
        self.assertEqual(result['queue']['reconciliation']['state'],'needs_evidence')
        self.assertEqual(queue.state(result),'recovery_required')
        self.assertIsNone(result['verdict']);self.assertFalse(result['accepted'])
        self.assertEqual(task_leases.holders(),held)

    def test_legacy_and_modified_result_do_not_supply_completion_proof(self):
        project,row=self.completed_but_held();held=task_leases.holders()
        for field in ('completion','checks','runner','accepted'):
            altered=copy.deepcopy(row)
            if field=='completion':altered['queue'].pop('completion')
            elif field=='checks':altered['checks'][0]['exit_code']=9
            elif field=='runner':altered['queue']['runner']={'pid':999999}
            else:altered.update(accepted=True,accepted_commit='0'*40)
            self.persist(project,altered)
            with patch.object(queue,'_runner_state',return_value=False):result=queue.reconcile(project,row['name'])
            self.assertEqual(result['queue']['reconciliation']['state'],'needs_evidence')
            self.assertEqual(task_leases.holders(),held)

    def test_incomplete_check_process_result_is_not_proof_of_child_exit(self):
        project,row=self.completed_but_held()
        row['checks'][0].update(exit_code=None,ok=False)
        row.update(verdict='check_failed')
        row['queue']['state']='check_failed'
        row['queue']['completion']['result_sha256']=integration.completion_digest(row)
        self.persist(project,row);held=task_leases.holders()
        with patch.object(queue,'_runner_state',return_value=False):result=queue.reconcile(project,row['name'])
        self.assertEqual(result['queue']['reconciliation']['state'],'needs_evidence')
        self.assertEqual(task_leases.holders(),held)

    def test_later_foreign_lease_is_not_freed_or_relabelled_as_original(self):
        project,row=self.completed_but_held()
        task_leases.release('integration',str(project),row['queue']['lease_owner'])
        task_leases.acquire('integration',str(project),'new-job',project=str(project))
        held=task_leases.holders()
        with patch.object(queue,'_runner_state',return_value=False):result=queue.reconcile(project,row['name'])
        self.assertEqual(result['queue']['reconciliation']['state'],'settled')
        self.assertEqual(task_leases.holders(),held)

    def test_claim_snapshot_change_cannot_be_released_even_under_same_owner(self):
        project,row=self.completed_but_held();owner=row['queue']['lease_owner']
        task_leases.release('integration',str(project),owner)
        task_leases.acquire('integration',str(project),owner,project=str(project))
        held=task_leases.holders()
        with patch.object(queue,'_runner_state',return_value=False):result=queue.reconcile(project,row['name'])
        self.assertEqual(result['queue']['reconciliation']['state'],'unknown')
        self.assertEqual(task_leases.holders(),held)
        with self.assertRaisesRegex(ValueError,'快照'):
            task_leases.release('integration',str(project),owner,expected=row['queue']['lease'])
        self.assertEqual(task_leases.holders(),held)

    def test_failed_checks_remain_failed_after_occupancy_recovery(self):
        project,row=self.completed_but_held([[sys.executable,'-c','raise SystemExit(7)']])
        with patch.object(queue,'_runner_state',return_value=False):result=queue.reconcile(project,row['name'])
        self.assertEqual(queue.state(result),'check_failed')
        self.assertEqual(result['checks'][0]['exit_code'],7)
        self.assertFalse(result['accepted']);self.assertEqual(task_leases.holders(),{})

    def test_recovery_read_never_creates_a_store_or_lock_file(self):
        project,row=self.intent()
        before=sorted(str(p) for p in self.root.rglob('*'))
        queue.observe(row);queue.snapshot(project)
        self.assertEqual(sorted(str(p) for p in self.root.rglob('*')),before)

    def test_old_unheld_receipt_is_not_reported_as_a_new_stuck_lease(self):
        project,row=self.intent();row=queue.run_one(project,row['name'])
        row['queue'].pop('completion');row['queue'].pop('lease')
        self.persist(project,row)
        observed=queue.observe(row)
        self.assertEqual(observed['state'],'awaiting_review')
        self.assertEqual(observed['recovery']['state'],'not_held')
        self.assertFalse(observed['recovery']['can_settle'])
        self.assertFalse(row['accepted'])

    def crash_child(self,project,row,phase):
        # Only this test's interpreter exits; no desktop/service process is touched.
        source=Path(__file__).resolve().parents[1]
        code='''import os,sys
from pathlib import Path
sys.path.insert(0,sys.argv[1])
import model_registry as r
r.HERE=Path(sys.argv[2]);r.REGISTRY=r.HERE/'models.json';r.RUNS=r.HERE/'runs'
import integration_queue as q,task_integration as i,task_leases as l
if sys.argv[5]=='terminal':l.release=lambda *a,**k:os._exit(37)
else:i._run_check=lambda *a,**k:os._exit(38)
q.run_one(sys.argv[3],sys.argv[4])
'''
        child=subprocess.run([sys.executable,'-c',code,str(source),str(self.root),str(project),row['name'],phase],
                             capture_output=True,text=True,timeout=45)
        self.assertEqual(child.returncode,37 if phase=='terminal' else 38,child.stderr)

    def test_real_child_crash_after_receipt_can_be_settled_after_restart(self):
        project,row=self.intent();self.crash_child(project,row,'terminal')
        original=queue.get(project,row['name'])
        self.assertEqual(queue._runner_state(original['queue']['runner']),False)
        self.assertTrue(queue.observe(original)['recovery']['can_settle'])
        result=queue.reconcile(project,row['name'])
        self.assertEqual(result['queue']['reconciliation']['state'],'settled')
        self.assertEqual(queue.state(result),'awaiting_review')
        self.assertFalse(result['accepted']);self.assertEqual(task_leases.holders(),{})

    def test_real_child_crash_during_checks_preserves_unknown_and_lease(self):
        project,row=self.intent();self.crash_child(project,row,'checking')
        held=task_leases.holders();original=queue.get(project,row['name'])
        self.assertEqual(queue._runner_state(original['queue']['runner']),False)
        self.assertEqual(queue.observe(original)['state'],'recovery_required')
        result=queue.reconcile(project,row['name'])
        self.assertEqual(result['queue']['reconciliation']['state'],'needs_evidence')
        self.assertEqual(task_leases.holders(),held)
        with patch.object(integration,'run_integration',side_effect=AssertionError('no replay')):
            self.assertEqual(queue.state(queue.run_one(project,row['name'])),'recovery_required')

    def test_acceptance_after_lease_release_cannot_be_overwritten_by_reconciliation(self):
        project,row=self.completed_but_held();released=threading.Event();reviewing=threading.Event();results=[]
        original_release=task_leases.release
        original_accept=integration._accept_integration_locked
        def release(*args,**kwargs):
            result=original_release(*args,**kwargs)
            if args[2]==row['queue']['lease_owner']:
                released.set();self.assertTrue(reviewing.wait(5))
            return result
        def accept(*args,**kwargs):
            # Acquiring the project lease must not bypass the record lock.
            self.assertTrue(released.is_set())
            return original_accept(*args,**kwargs)
        evidence=[{'id':'i','status':'pass','evidence':[{'source':'reviewer_test','detail':'independent fixture','exit_code':0}]}]
        def reviewer():
            try:
                self.assertTrue(released.wait(5));reviewing.set()
                results.append(integration.accept_integration(project,row['name'],evidence))
            except BaseException as error:results.append(error)
        with patch.object(queue,'_runner_state',return_value=False),patch.object(task_leases,'release',side_effect=release),patch.object(integration,'_accept_integration_locked',side_effect=accept):
            thread=threading.Thread(target=reviewer);thread.start()
            queue.reconcile(project,row['name']);thread.join(10)
            self.assertFalse(thread.is_alive())
        self.assertEqual(len(results),1);self.assertIsInstance(results[0],dict,results)
        current=queue.get(project,row['name'])
        self.assertTrue(current['accepted']);self.assertEqual(current['queue']['reconciliation']['state'],'settled')
        self.assertEqual(task_leases.holders(),{})
