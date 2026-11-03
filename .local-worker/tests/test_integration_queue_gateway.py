"""Two-brain queued integration and actual project-bound HTTP, mock models only."""
import base64
import json
from http.server import ThreadingHTTPServer
from pathlib import Path
import sys
import threading
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request,ProxyHandler,build_opener

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import brain_context
import brain_gateway as gateway
import brain_requests
import brain_transport
import integration_queue as queue
import model_registry as registry
import panel_server as panel
import task_integration as integration
import task_leases
from test_task_workspace_handover import Base

EVIDENCE=[{'id':'i1','status':'pass','evidence':[{'source':'reviewer_test','exit_code':0,'detail':'independent fixture inspection'}]}]


class QueueGatewayTests(Base):
    def test_recovery_transport_rejects_misattributed_receipts_without_retry(self):
        body={'request_id':'recover','name':'original'}
        receipt={'name':'original','queue':{'reconciliation':{'state':'settled','request_reference':'binding/recover'}}}
        response={'operation':'reconcile_integration','request_id':'recover','integration':receipt,'executor_replayed':False}
        for change in ({'request_id':'different'},{'executor_replayed':True},
                {'integration':{**receipt,'name':'different'}},
                {'integration':{**receipt,'queue':{'reconciliation':{'state':'settled','request_reference':'other/recover'}}}}):
            transport=brain_transport.BrainTransport();transport._capability='test-only'
            transport._context={'context':{'owner':{'binding_key':'binding'}}}
            with patch.object(transport,'_request',return_value=(200,{**response,**change})) as send:
                with self.assertRaises(brain_transport.OutcomeUnknown):transport.call('reconcile_integration',body)
                self.assertEqual(send.call_count,1)

    def test_queued_transport_results_are_not_accepted_integration_receipts(self):
        record={'name':'fixture','accepted':False,'verdict':None,'queue':{'schema':1,'state':'queued'}}
        result={'operation':'integrate','request_id':'original','integration':record}
        for state in ('queued','preparing','merging','checking','blocked','recovery_required','cancelled'):
            record['queue']['state']=state
            self.assertTrue(brain_transport._valid_result('integrate',result))
            self.assertFalse(brain_transport._valid_result('accept_integration',{**result,'operation':'accept_integration'}))
        record['queue']['state']='imagined'
        self.assertFalse(brain_transport._valid_result('integrate',result))
        record['queue']['state']=[]
        self.assertFalse(brain_transport._valid_result('integrate',result))
        record['queue'].update(state='queued',schema=True)
        self.assertFalse(brain_transport._valid_result('integrate',result))
        record['queue']['schema']=1
        record['queue']['state']='queued';record['accepted']=True
        self.assertFalse(brain_transport._valid_result('integrate',result))

    def setUp(self):
        super().setUp()
        for value in (queue._AUTHORITY,queue._SUBMIT,queue._ACTIVE,queue._AGAIN):
            value.clear();self.addCleanup(value.clear)

    def owned(self,host):
        run,project,_=self.isolated_task(name='owner-'+host)
        ctx=brain_context.build_context(project=str(project),host=host,owner='owner-'+host,role='brain',operator_session='fixture-'+host,session_binding='user_bound')
        registry.patch_task(run,source='brain',owner=ctx.owner,brain={'owner':ctx.owner,'project':str(project),'binding_key':ctx.binding_key})
        cap=gateway.register_connection(ctx.declaration())['capability']
        payload={'request_id':'queued-'+host,'task_ids':[run.name],'checks':[[sys.executable,'-c','pass']],'enqueue':True}
        return project,ctx,cap,payload

    def test_both_hosts_enqueue_query_execute_accept_and_replay(self):
        for host in ('codex','dsh'):
            project,ctx,cap,payload=self.owned(host);jobs=[]
            with patch.object(panel.EXECUTOR,'submit',side_effect=lambda f,*a:jobs.append((f,a))):
                row=gateway.handle('integrate',payload,cap)
                self.assertEqual(queue.state(row['integration']),'queued')
                self.assertFalse(row['integration']['accepted'])
                self.assertEqual(len(jobs),1)
                self.assertEqual(gateway.handle('request_status',{'request_id':payload['request_id']},cap)['integration_status']['state'],'queued')
                fn,args=jobs.pop(0);fn(*args)
                ready=gateway.handle('request_status',{'request_id':payload['request_id']},cap)
                self.assertEqual(ready['integration']['verdict'],'passed')
                self.assertEqual(ready['integration_status']['state'],'awaiting_review')
                with patch.object(integration,'run_integration',side_effect=AssertionError('must not remerge')):
                    replay=gateway.handle('integrate',payload,cap)
                    self.assertTrue(replay['reused'])
                    self.assertEqual(replay['integration'],ready['integration'])
                accepted=gateway.handle('accept_integration',{'request_id':'accept-'+host,'name':row['integration']['name'],'criteria':EVIDENCE},cap)
                self.assertTrue(accepted['integration']['accepted'])

    def test_reconnected_original_owner_resumes_only_never_started_intent(self):
        project,ctx,cap,payload=self.owned('dsh');jobs=[]
        with patch.object(panel.EXECUTOR,'submit',side_effect=lambda f,*a:jobs.append((f,a))):
            original=gateway.handle('integrate',payload,cap)
        for value in (queue._AUTHORITY,queue._SUBMIT,queue._ACTIVE,queue._AGAIN):value.clear()
        jobs.clear()
        self.assertEqual(queue.snapshot(project)['records'][0]['state'],'queued')
        self.assertEqual(jobs,[])
        next_cap=gateway.register_connection(ctx.declaration())['capability']
        with patch.object(panel.EXECUTOR,'submit',side_effect=lambda f,*a:jobs.append((f,a))):
            replay=gateway.handle('integrate',payload,next_cap)
        self.assertEqual(replay['integration']['name'],original['integration']['name'])
        self.assertEqual(len(jobs),1)
        fn,args=jobs.pop(0);fn(*args)
        self.assertEqual(queue.state(queue.get(project,original['integration']['name'])),'awaiting_review')

    def test_queue_mode_payload_and_foreign_owner_cannot_rebind_original_request(self):
        project,ctx,cap,payload=self.owned('codex')
        with patch.object(panel.EXECUTOR,'submit'):
            gateway.handle('integrate',payload,cap)
            with self.assertRaises((gateway.BrokerError,ValueError)):
                gateway.handle('integrate',{**payload,'enqueue':False},cap)
            other=brain_context.build_context(project=str(project),host='dsh',owner='foreign',role='brain',operator_session='other',session_binding='user_bound')
            other_cap=gateway.register_connection(other.declaration())['capability']
            with self.assertRaises(gateway.BrokerError):gateway.handle('integrate',payload,other_cap)

    def test_both_hosts_real_stdio_accept_enqueue_and_query_original_job(self):
        from test_brain_mcp_server import StdioClient,payload as visible
        for host in ('codex','dsh'):
            project,context,_,request=self.owned(host)
            registry.write_json(registry.HERE/'workflow.json',{'enabled':True})
            server=ThreadingHTTPServer(('127.0.0.1',0),panel.Handler);server.daemon_threads=True
            threading.Thread(target=server.serve_forever,daemon=True).start()
            client=StdioClient(['--project',str(project),'--host',host,'--owner',context.owner,
                '--role','brain','--session-binding','user_bound','--session-id','fixture-'+host,
                '--panel-url','http://127.0.0.1:'+str(server.server_port)],registry_home=registry.HERE)
            try:
                client.initialize()
                schema=next(t for t in client.list_tools() if t['name']=='brain_integrate')['inputSchema']
                self.assertEqual(schema['properties']['enqueue']['type'],'boolean')
                self.assertFalse(client.call('brain_connect')['isError'])
                jobs=[]
                with patch.object(panel.EXECUTOR,'submit',side_effect=lambda f,*a:jobs.append((f,a))):
                    response=client.call('brain_integrate',request)
                    self.assertFalse(response['isError'],response)
                    receipt=visible(response)['integration'];self.assertEqual(receipt['queue']['state'],'queued')
                    self.assertIn('入队不代表',response['content'][0]['text'])
                    self.assertEqual(len(jobs),1)
                    status=client.call('brain_request_status',{'request_id':request['request_id']})
                    self.assertEqual(visible(status)['integration_status']['state'],'queued')
                    self.assertFalse(visible(status)['safe_to_redispatch'])
                    fn,args=jobs.pop(0);fn(*args)
                    done=client.call('brain_request_status',{'request_id':request['request_id']})
                    self.assertEqual(visible(done)['integration_status']['state'],'awaiting_review')
                    accepted=client.call('brain_accept_integration',{'request_id':'stdio-accept-'+host,
                        'name':receipt['name'],'criteria':EVIDENCE})
                    self.assertFalse(accepted['isError'],accepted);self.assertTrue(visible(accepted)['integration']['accepted'])
                    replay=client.call('brain_integrate',request)
                    self.assertTrue(visible(replay)['reused']);self.assertEqual(jobs,[])
                    params={'request_id':'stdio-reconcile-'+host,'name':receipt['name']}
                    reconciled=client.call('brain_reconcile_integration',params)
                    self.assertFalse(reconciled['isError'],reconciled)
                    result=visible(reconciled)
                    self.assertEqual(result['integration']['queue']['reconciliation']['state'],'settled')
                    self.assertTrue(result['integration']['accepted']);self.assertFalse(result['executor_replayed'])
                    again=client.call('brain_reconcile_integration',params)
                    self.assertFalse(again['isError'],again);self.assertTrue(visible(again)['reused'])
                    queried=client.call('brain_request_status',{'request_id':params['request_id']})
                    self.assertEqual(visible(queried)['request_state'],'completed')
                    self.assertEqual(visible(queried)['integration'],result['integration'])
            finally:
                client.close();server.shutdown();server.server_close()

    def test_both_hosts_recover_completed_claim_and_refuse_foreign_owner(self):
        for host in ('codex','dsh'):
            project,ctx,cap,payload=self.owned(host)
            with patch.object(panel.EXECUTOR,'submit'):
                row=gateway.handle('integrate',payload,cap)['integration']
            auth=brain_context.issue_authority(ctx)
            with patch.object(task_leases,'release',side_effect=OSError('crash before release')):
                with self.assertRaises(OSError):queue.run_one(project,row['name'],authority=auth)
            held=task_leases.holders()
            other=brain_context.build_context(project=str(project),host=host,owner='foreign',role='brain',
                operator_session='foreign',session_binding='user_bound')
            foreign=gateway.register_connection(other.declaration())['capability']
            params={'request_id':'settle-'+host,'name':row['name']}
            with self.assertRaises(gateway.BrokerError):gateway.handle('reconcile_integration',params,foreign)
            self.assertEqual(task_leases.holders(),held)
            with patch.object(queue,'_runner_state',return_value=False),patch.object(integration,'run_integration',side_effect=AssertionError('no effects')):
                result=gateway.handle('reconcile_integration',params,cap)
                self.assertEqual(result['integration']['queue']['reconciliation']['state'],'settled')
                self.assertFalse(result['integration']['accepted']);self.assertFalse(result['executor_replayed'])
                again=gateway.handle('reconcile_integration',params,cap)
                self.assertTrue(again['reused']);self.assertEqual(again['integration'],result['integration'])
            self.assertEqual(task_leases.holders(),{})
            self.assertEqual(gateway.handle('request_status',{'request_id':params['request_id']},cap)['integration'],result['integration'])

    def test_unknown_reconciliation_journal_never_runs_recovery_again(self):
        project,ctx,cap,payload=self.owned('dsh')
        with patch.object(panel.EXECUTOR,'submit'):
            row=gateway.handle('integrate',payload,cap)['integration']
        params={'request_id':'unknown-reconcile','name':row['name']}
        brain_requests.reserve(project,ctx.binding_key,'reconcile_integration',params['request_id'],{'name':row['name']})
        brain_requests.mark_unknown(project,ctx.binding_key,params['request_id'],'lost receipt')
        with patch.object(queue,'reconcile') as action:
            with self.assertRaises(brain_requests.UnknownOutcome):gateway.handle('reconcile_integration',params,cap)
            action.assert_not_called()
        self.assertEqual(gateway.handle('request_status',{'request_id':params['request_id']},cap)['request_state'],'unknown')


class QueueHttpTests(Base):
    def setUp(self):
        super().setUp()
        self.server=ThreadingHTTPServer(('127.0.0.1',0),panel.Handler)
        threading.Thread(target=self.server.serve_forever,daemon=True).start()
        self.addCleanup(self.server.server_close);self.addCleanup(self.server.shutdown)
        self.url='http://127.0.0.1:'+str(self.server.server_port)
        self.opener=build_opener(ProxyHandler({}))
        for value in (queue._AUTHORITY,queue._SUBMIT,queue._ACTIVE,queue._AGAIN):self.addCleanup(value.clear)

    def request(self,path,project=None,body=None):
        view={'mode':'project','project':str(project)} if project else {'mode':'unbound','project':None}
        headers={'X-Model-Desk-View':base64.urlsafe_b64encode(json.dumps(view).encode()).decode()}
        if body is not None:headers.update({'Content-Type':'application/json','X-Panel-Token':panel.TOKEN})
        req=Request(self.url+path,headers=headers,data=None if body is None else json.dumps(body).encode())
        with self.opener.open(req) as r:return json.load(r)

    def test_project_read_only_list_detail_empty_and_route_boundaries(self):
        run,project,_=self.isolated_task()
        row=queue.enqueue(project,[run.name],[[sys.executable,'-c','pass']],request_id='view')
        file=project/'.model-desk/integrations'/(row['name']+'.json');before=file.read_bytes()
        self.assertEqual(self.request('/api/integrations',project)['records'][0]['state'],'queued')
        self.assertEqual(self.request('/api/integrations/'+row['name'],project)['integration']['name'],row['name'])
        self.assertEqual(file.read_bytes(),before)
        other=self.root/'empty';other.mkdir()
        self.assertEqual(self.request('/api/integrations',other)['records'],[])
        self.assertFalse((other/'.model-desk').exists())
        self.assertFalse(self.request('/api/integrations')['available'])
        with self.assertRaises(HTTPError):self.request('/api/integrations/'+row['name'],other)
        with self.assertRaises(HTTPError):self.request('/api/integrations/'+row['name'])

    def test_post_queues_only_after_scope_and_original_payload_checks(self):
        run,project,_=self.isolated_task();body={'request_id':'post','task_ids':[run.name],'checks':[[sys.executable,'-c','pass']]}
        with patch.object(panel.EXECUTOR,'submit') as submit:
            result=self.request('/api/integrations',project,body)
            self.assertEqual(queue.state(result['integration']),'queued');submit.assert_called_once()
        name=result['integration']['name']
        with self.assertRaises(HTTPError):self.request('/api/integrations',project,{**body,'project':str(project)})
        with self.assertRaises(HTTPError):self.request('/api/integrations/'+name+'/resume',project,{'checks':[]})
        cancelled=self.request('/api/integrations/'+name+'/cancel',project,{})
        self.assertEqual(queue.state(cancelled['integration']),'cancelled')

    def test_new_shared_assets_are_served_without_account_or_task_calls(self):
        for name in ('integration-queue.js','integration-queue.css'):
            with self.opener.open(self.url+'/'+name) as response:self.assertEqual(response.read(),(panel.WEB/name).read_bytes())

    def test_legacy_dotted_name_details_remain_accessible(self):
        run,project,_=self.isolated_task(name='legacy')
        row=queue.enqueue(project,[run.name],[[sys.executable,'-c','pass']],request_id='legacy',name='release.1')
        response=self.request('/api/integrations/release.1',project)
        self.assertEqual(response['integration'],row)
        self.assertTrue(response['read_only'])
