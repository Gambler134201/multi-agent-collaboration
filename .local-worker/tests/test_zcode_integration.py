"""ZCode queue, account isolation and tool-boundary regression, no inference."""
from pathlib import Path
import hashlib
import json
import sys
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import execution_endpoints as endpoints
import execution_engines as engines
import model_registry as registry
import panel_server as panel
import workflow_settings
import zcode_worker as worker
import zcode_tools as tools
from test_stage_workflow import Base


class ZCodeTests(Base):
    def setUp(self):
        super().setUp()
        self.settings=workflow_settings.save_settings({'enabled':True,'paid_allowed':True,'model_scope':'all',
            'enforce_paid_budget':False,'enforce_model_call_limit':False})
        self.config={'home':str(self.project/'installed'),'port':19765,'access_mode':'project'}
        self.enterContext(patch.object(worker,'configuration',return_value=self.config))
        self.enterContext(patch.object(worker,'installation',return_value={'version':'3.16.3','fingerprint':'fixture'}))
        self.enterContext(patch.object(worker,'ensure_connection'))
        registry.write_json(Path(registry.HERE)/worker.CATALOG,{'models':[{'providerId':'account:zai-start-plan','modelId':'GLM-5.3-Flash',
            'reasoning':['low','high','max'],'contextWindow':1000000}],
            'preferred':{'providerId':'account:zai-start-plan','modelId':'GLM-5.3-Flash','options':{'reasoningLevel':'max'}}})
        self.default=registry.list_models()['default_model']
        self.model=worker.select_model(worker.MODEL)
        endpoints.update('zcode',{'enabled':True},self.settings)
        self.enterContext(patch.object(engines,'availability',return_value={'ready':True}))

    def dispatch(self,**kw):
        body={'kind':'edit','model_id':self.model['id'],'route_mode':'manual','project':str(self.project),
              'files':['a.py'],'prompt':'Write a.py and test','routing':{'acceptance':['fixture file']},'source':'codex',**kw}
        with patch.object(panel.EXECUTOR,'submit'):
            row=panel.submit_task(body)
        return registry.read_task(registry.task_dir(row['id'])),registry.task_dir(row['id'])

    def test_native_identity_and_default_preserved(self):
        import delegation_policy
        self.assertIn('zcode',delegation_policy.EXECUTOR_ACTORS)
        self.assertEqual(registry.list_models()['default_model'],self.default)
        self.assertEqual(registry.get_model(self.model['id'])['api_key'],'')
        self.assertEqual(self.model['native_selection']['options']['reasoningLevel'],'max')
        for engine,provider in [('pi','zcode'),('zcode','local'),('workbuddy','zcode')]:
            with self.assertRaises(ValueError):engines.validate_model(engine,{'provider':provider})
        with self.assertRaises(ValueError):registry.upsert_model({'provider':'zcode','model':worker.MODEL,'api_key':'fixture'})

    def test_dispatch_snapshots_model_permission_and_guard(self):
        row,run=self.dispatch()
        self.assertEqual(row['engine'],'zcode')
        self.assertEqual(row['zcode_connection'],self.config)
        self.assertEqual(row['zcode_selection'],self.model['native_selection'])
        self.assertEqual(row['zcode_tool_revision'],tools.revision())
        self.assertEqual(row['zcode_access_mode'],'project')

    def test_disabled_scope_and_budget_block_before_dispatch(self):
        endpoints.update('zcode',{'enabled':False},self.settings)
        with self.assertRaisesRegex(ValueError,'禁用'):self.dispatch()
        endpoints.update('zcode',{'enabled':True},self.settings)
        workflow_settings.save_settings({'model_scope':'local_only'})
        with self.assertRaises(ValueError):self.dispatch()
        for field in ('enforce_paid_budget','enforce_model_call_limit'):
            with self.assertRaises(ValueError):worker.validate_task({'kind':'edit'},{**self.settings,field:True})

    def test_tools_write_read_command_and_record_refusal_are_sticky(self):
        row,run=self.dispatch();job=tools.prepare(run,row,[self.project/'profile'])
        client=tools.Tools(job)
        with self.assertRaises(ValueError):client.guard('Read',{'file_path':str(self.project.parent/'outside.txt')})
        client.call('write_file',{'path':str(self.project/'a.py'),'content':'print(42)\n'})
        self.assertIn('42',client.call('read_file',{'path':'a.py'})['content'])
        self.assertEqual(client.call('run_command',{'command':'Write-Output ZCODE_FIXTURE'})['exit_code'],0)
        with self.assertRaises(ValueError):client.call('write_file',{'path':'.model-desk/forged.json','content':'{}'})
        with self.assertRaises(ValueError):client.call('write_file',{'path':'a.py','content':'bad'})
        self.assertTrue(tools.Tools(job).proof['stopped'])

    def test_chat_cannot_write_or_run_and_project_escape_is_denied(self):
        row,run=self.dispatch(kind='chat',files=[]);job=tools.prepare(run,row,[self.project/'profile'])
        for name,args in [('run_command',{'command':'Write-Output denied'}),('write_file',{'path':'a.py','content':'bad'})]:
            with self.assertRaises(ValueError):tools.Tools(job).call(name,args)

    def test_completed_worker_needs_real_matching_test_evidence_and_close(self):
        row,run=self.dispatch(test_command='Write-Output FIXTURE')
        def execute(job,on_event):
            for event in [{'type':'connected'},{'type':'prompt_attempted'}]:on_event(event)
            client=tools.Tools(job['tool_job']);client.proof['listed']=True;client.save()
            client.call('write_file',{'path':'a.py','content':'print(42)\n'})
            client.call('run_command',{'command':'Write-Output FIXTURE'})
            on_event({'type':'closed'})
            return [{'type':'result','value':{'settings':{'model':self.model['native_selection']},'messages':[{'info':{'role':'assistant'},'parts':[{'type':'text','text':'Done'}]}]}}]
        self.assertEqual(worker.run_task(run,row,registry.get_model(row['model_id']),execute=execute),0)
        result=registry.read_task(run)
        self.assertEqual(result['status'],'completed');self.assertEqual(result['changed_files'],['a.py'])
        self.assertTrue(result['test_result']['verified']);self.assertIsNone(result['cost_cny'])

    def test_unknown_request_or_stale_guard_never_resubmits(self):
        row,run=self.dispatch();calls=[]
        def execute(job,on_event):
            calls.append(job);on_event({'type':'prompt_attempted'})
            return [{'type':'error','message':'unknown'}]
        self.assertEqual(worker.run_task(run,row,registry.get_model(row['model_id']),execute=execute),1)
        self.assertEqual(len(calls),1)
        with self.assertRaises(ValueError):worker.recovery_guard(registry.read_task(run))
        self.assertEqual(panel._cost_facts(registry.read_task(run))['billing_source'],'zcode-native-account')

    def test_both_brain_hosts_dispatch_with_fixed_native_identity(self):
        import brain_context,brain_gateway
        for host in ('codex','dsh'):
            context=brain_context.build_context(project=str(self.project),host=host,owner='zcode-fixture-'+host,
                role='brain',operator_session='fixture-'+host,session_binding='user_bound')
            capability=brain_gateway.register_connection(context.declaration())['capability']
            try:
                with patch.object(panel.EXECUTOR,'submit'):
                    receipt=brain_gateway.handle('dispatch',{'request_id':'zcode-'+host,'prompt':'fixture',
                        'kind':'edit','files':['a.py'],'model_id':self.model['id'],'acceptance':['fixture result']},capability)
                tid=receipt['task']['task_id']
                status=brain_gateway.handle('status',{'task_id':tid},capability)
                self.assertEqual(status['engine'],'zcode')
                self.assertEqual(status['native_execution']['billing_source'],'zcode-native-account')
                self.assertFalse(brain_gateway.handle('delivery',{'task_id':tid},capability)['accepted'])
            finally:brain_gateway._CAPABILITIES.pop(capability,None)

    def test_partial_answer_from_failed_native_turn_is_not_completed(self):
        row,run=self.dispatch()
        def execute(job,on_event):
            on_event({'type':'prompt_attempted'});on_event({'type':'closed'})
            return [{'type':'result','terminal_event':'turn.failed','value':{'settings':{'model':self.model['native_selection']},
                'session':{'status':'error'},'messages':[{'info':{'role':'assistant'},'parts':[{'type':'text','text':'partial answer'}]}]}}]
        self.assertEqual(worker.run_task(run,row,registry.get_model(row['model_id']),execute=execute),1)
        self.assertEqual(registry.read_task(run)['status'],'failed')

if __name__=='__main__':unittest.main()
