"""Production worker with synthetic process/proof; no installed DSH or model."""
import contextlib
import io
import json
from pathlib import Path
import tempfile
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))

import dsh_worker as worker
import execution_result
import model_registry as registry
import record_protection


class DshGuardOutcomes(unittest.TestCase):
    def run_case(self, denied=False, native_status='completed', invalid=False, test_code=None, attempted=False):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            run=root/'.model-desk/runs/synthetic'
            run.mkdir(parents=True)
            (root/'source.txt').write_text('original')
            state={'id':'synthetic','engine':'dsh','project':str(root),'kind':'edit','files':['source.txt']}
            if test_code is not None:state['test_command']='fixture selftest'
            registry.write_json(run/'task.json',state)
            proof={'schema':1,'source':record_protection.SOURCE,'revision':record_protection.revision(),
                   'root':str(root),'scope':'global','probe':'ok','session_id':'synthetic-parent'}
            if denied:
                proof['guard_denial']={'schema':1,'code':'TOOL_PERMISSION_DENIED','tool':'write',
                                      'reason_code':'protected_data','rule_id':'record_target'}
            if invalid:proof['guard_denial']={'raw_message':'not evidence'}
            registry.write_json(run/record_protection.PROOF_NAME,proof)
            events=[{'type':'session','session_id':'synthetic-parent'},
                    {'type':'tool_call','toolCallId':'ordinary-error','title':'read','status':'failed'}]
            if attempted:events.append({'type':'prompt_attempted'})
            if native_status is not None:events.append({'type':'result','result':{'status':native_status,'stopReason':'end_turn'}})
            class Process:
                returncode=0 if native_status=='completed' else 1
                pid=1
                stdout=io.StringIO(''.join(json.dumps(e)+'\n' for e in events))
                stderr=io.StringIO('')
                def poll(self):return self.returncode
            prepared={'project':root,'before':{'source.txt':worker.digest(root/'source.txt')},
                      'process':Process(),'preset':{},'allowed_subagent':False}
            relay=type('Relay',(),{'token':'synthetic-relay'})()
            with patch.object(worker,'_prepare_dsh',return_value=prepared), \
                 patch.object(worker.preset_runtime,'verify',return_value={}), \
                 patch.object(worker.subprocess,'run',return_value=type('Test',(),{'returncode':test_code,'stdout':'fixture','stderr':''})()), \
                 patch.object(worker,'record_review') as review,contextlib.redirect_stdout(io.StringIO()):
                code=worker._run_task(run,state,{'model':'synthetic'},relay)
            task=registry.read_task(run)
            self.assertEqual(review.call_args.args[1],task['status']=='completed')
            return code,task,execution_result.project(task)

    def test_real_refusal_is_preserved_when_native_reports_completed_or_failed(self):
        for native_status in ['completed','failed',None]:
            with self.subTest(native_status=native_status):
                code,task,view=self.run_case(denied=True,native_status=native_status,attempted=True)
                self.assertEqual(code,1);self.assertEqual(task['status'],'failed')
                self.assertEqual(view['failure']['kind'],'permission_denied')
                self.assertEqual(view['failure']['rule_id'],'record_target')
                self.assertEqual(view['cleanup']['session'],'completed')
                self.assertEqual(len(view['issues']),1)

    def test_ordinary_tool_error_with_completed_turn_does_not_fail_task(self):
        code,task,view=self.run_case()
        self.assertEqual(code,0);self.assertEqual(task['status'],'completed')
        self.assertIsNone(view['failure'])

    def test_malformed_denial_is_evidence_failure_not_success_or_permission_inference(self):
        code,task,view=self.run_case(invalid=True)
        self.assertEqual(code,1);self.assertEqual(task['status'],'failed')
        self.assertEqual(view['failure']['kind'],'verification_unconfirmed')

    def test_failed_controller_test_does_not_create_an_executor_completed_review(self):
        code,task,view=self.run_case(test_code=3)
        self.assertEqual(code,1)
        self.assertEqual(task['status'],'failed')
        self.assertEqual(view['failure']['kind'],'verification_failed')

    def test_missing_terminal_after_positive_send_attempt_is_unknown_not_resent(self):
        import executor_contract
        code,task,view=self.run_case(native_status=None,attempted=True)
        self.assertEqual(code,1)
        self.assertTrue(task['executor_prompt_attempted'])
        self.assertEqual(view['failure']['kind'],'outcome_unknown')
        self.assertNotEqual(executor_contract.resend_decision('dsh',task)['verdict'],'allowed')
        _,_,before_send=self.run_case(native_status=None)
        self.assertEqual(before_send['failure']['kind'],'execution_error')
