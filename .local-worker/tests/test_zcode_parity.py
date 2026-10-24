"""ZCode experience parity: temporary records and fake native services only."""
import base64
import hashlib
import json
from pathlib import Path
import time
import unittest
from unittest.mock import patch
import test_zcode_integration as integration
import execution_endpoints as endpoints
import execution_engines as engines
import model_registry as registry
import panel_server as panel
import zcode_tools as tools
import zcode_worker as worker
_ensure_connection=worker.ensure_connection


class ParityTests(integration.ZCodeTests):
    # Inherit the existing nine checks as direct compatibility coverage.
    def test_reasoning_save_does_not_mutate_queued_selection(self):
        row,run=self.dispatch()
        saved=endpoints.update('zcode',{'native_selection':{'model':worker.MODEL,'reasoning':'high'}},self.settings)
        self.assertTrue(saved['ok'])
        self.assertEqual(registry.get_model(row['model_id'])['native_selection']['options']['reasoningLevel'],'high')
        self.assertEqual(registry.read_task(run)['zcode_selection']['options']['reasoningLevel'],'max')
        with self.assertRaises(ValueError):worker.select_model(worker.MODEL,'imaginary')
        self.assertEqual(registry.list_models()['default_model'],self.default)

    def test_preflight_never_launches_but_can_report_auto_connect_pending(self):
        self.config['auto_connect']=True
        data=worker.catalog();data.update(installation='fixture',connection=self.config)
        registry.write_json(Path(registry.HERE)/worker.CATALOG,data)
        with patch.object(worker,'bridge_connected',return_value=False),patch.object(worker,'desktop_running',return_value=False),patch.object(worker,'launch') as launch:
            info=worker.availability()
        launch.assert_not_called()
        self.assertTrue(info['ready']);self.assertFalse(info['connected']);self.assertTrue(info['auto_connect_pending'])
        self.assertEqual(info['connection_state'],'closed')

    def test_running_without_bridge_is_not_restarted(self):
        self.config['auto_connect']=True
        # Restore the implementation hidden by the base's no-network test patch.
        with patch.object(worker,'bridge_connected',return_value=False),patch.object(worker,'desktop_running',return_value=True),patch.object(worker,'launch') as launch:
            with self.assertRaisesRegex(ValueError,'未重启'):worker.connect()
        launch.assert_not_called()

    def test_explicit_connect_loads_catalog_without_model(self):
        with patch.object(worker,'bridge_connected',return_value=True),patch.object(worker,'refresh_catalog',return_value={'model_called':False}) as refresh,patch.object(worker,'launch') as launch:
            result=worker.connect()
        self.assertFalse(result['model_called']);launch.assert_not_called();refresh.assert_called_once()

    def test_task_auto_connect_launches_once_only_when_closed(self):
        self.config['auto_connect']=True
        with patch.object(worker,'bridge_connected',side_effect=[False,False,True]),patch.object(worker,'desktop_running',return_value=False),patch.object(worker,'launch') as launch,patch.object(worker.time,'sleep'):
            _ensure_connection()
        launch.assert_called_once()
        with patch.object(worker,'bridge_connected',return_value=False),patch.object(worker,'desktop_running',return_value=True),patch.object(worker,'launch') as launch:
            with self.assertRaisesRegex(ValueError,'未重启'):_ensure_connection()
        launch.assert_not_called()

    def test_cancelled_before_connect_never_starts_native_or_model(self):
        flag=self.project/'cancelled.flag';flag.touch()
        with patch.object(worker,'launch') as launch:
            with self.assertRaisesRegex(ValueError,'已取消'):_ensure_connection(cancel_file=flag)
        launch.assert_not_called()

    def test_old_task_cannot_gain_auto_launch_from_new_setting(self):
        self.config['auto_connect']=True
        with patch.object(worker,'bridge_connected',return_value=False),patch.object(worker,'launch') as launch:
            with self.assertRaisesRegex(ValueError,'未允许'):_ensure_connection(allow_auto=False)
        launch.assert_not_called()
        self.config['auto_connect']=False
        with patch.object(worker,'bridge_connected',return_value=False),patch.object(worker,'launch') as launch:
            with self.assertRaises(ValueError):_ensure_connection(allow_auto=True)
        launch.assert_not_called()

    def test_queued_permission_is_preserved_after_card_change(self):
        row,run=self.dispatch()
        self.config['access_mode']='full'
        def execute(job,on_event):
            self.assertEqual(job['access_mode'],'project')
            self.assertEqual(json.loads(Path(job['tool_job']).read_text(encoding='utf-8'))['access_mode'],'project')
            client=tools.Tools(job['tool_job']);client.proof['listed']=True;client.save()
            client.call('write_file',{'path':'a.py','content':'print(42)'})
            on_event({'type':'prompt_attempted'});on_event({'type':'closed'})
            return [{'type':'result','terminal_event':'turn.completed','value':{'settings':{'model':row['zcode_selection']},'messages':[{'info':{'role':'assistant'},'parts':[{'type':'text','text':'done'}]}]}}]
        self.assertEqual(worker.run_task(run,row,registry.get_model(row['model_id']),execute=execute),0)

    def test_public_web_reader_blocks_private_addresses_and_private_redirect(self):
        for url in ('file:///private','http://user:pass@public.example/','http://127.0.0.1/'):
            with patch.object(tools.socket,'getaddrinfo',return_value=[(2,1,6,'',('127.0.0.1',80))]):
                with self.assertRaises(ValueError):tools.fetch_url(url)
        class Reply:
            status=302
            def getheader(self,name,default=None):return 'http://127.0.0.1/private' if name=='Location' else default
        class Connection:
            def __init__(self,*a,**kw):pass
            def request(self,*a,**kw):pass
            def getresponse(self):return Reply()
            def close(self):pass
        with patch.object(tools.socket,'getaddrinfo',side_effect=[[(2,1,6,'',('93.184.216.34',80))],[(2,1,6,'',('127.0.0.1',80))]]),patch.object(tools.http.client,'HTTPConnection',Connection):
            with self.assertRaises(ValueError):tools.fetch_url('http://public.example/')

    def test_public_web_reader_is_bounded_and_does_not_forward_auth(self):
        requests=[]
        class Reply:
            status=200
            def getheader(self,name,default=None):return 'text/plain' if name=='Content-Type' else default
            def read(self,limit):return b'x'*limit
        class Connection:
            def __init__(self,*a,**kw):pass
            def request(self,*a,**kw):requests.append((a,kw))
            def getresponse(self):return Reply()
            def close(self):pass
        with patch.object(tools.socket,'getaddrinfo',return_value=[(2,1,6,'',('93.184.216.34',80))]),patch.object(tools.http.client,'HTTPConnection',Connection):
            result=tools.fetch_url('http://public.example/page')
        self.assertTrue(result['truncated']);self.assertEqual(len(result['content']),50000)
        self.assertNotIn('Authorization',requests[0][1]['headers']);self.assertNotIn('Cookie',requests[0][1]['headers'])

    def test_account_snapshot_is_provider_bound_and_failure_preserves_cache(self):
        snapshot={'type':'quota','provider':'account:zai-start-plan','generated_at':123,
                  'limits':[{'unit':'tokens','remaining':42,'total':100,'models':['GLM-5.3-Flash']}],'unavailable_reason':None}
        with patch.object(worker,'driver',return_value=[snapshot]):
            result=endpoints.update('zcode',{'refresh_native_usage':True},self.settings)
        self.assertFalse(result['model_called']);self.assertEqual(result['usage']['limits'][0]['remaining'],42)
        before=(Path(registry.HERE)/worker.USAGE).read_bytes()
        with patch.object(worker,'driver',return_value=[{'type':'quota','provider':'other','limits':[]}]):
            with self.assertRaises(ValueError):worker.refresh_usage('account:zai-start-plan')
        self.assertEqual((Path(registry.HERE)/worker.USAGE).read_bytes(),before)
        with self.assertRaises(ValueError):worker.refresh_usage('custom')

    def test_search_and_images_respect_existing_record_protection(self):
        row,run=self.dispatch();job=tools.prepare(run,row,[]);client=tools.Tools(job)
        (self.project/'a.py').write_text('needle\n',encoding='utf-8')
        (run/'secret.py').write_text('needle must not appear',encoding='utf-8')
        result=client.call('search_files',{'path':'.','pattern':'*.py','text':'needle'})
        self.assertEqual([m['file'] for m in result['matches']],['a.py'])
        image=base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII=')
        (self.project/'image.png').write_bytes(image)
        result=client.call('read_image',{'path':'image.png'})
        self.assertEqual(result['mime_type'],'image/png');self.assertEqual(base64.b64decode(result['image_data']),image)
        with self.assertRaises(ValueError):client.call('read_image',{'path':'.model-desk/runs/'+row['id']+'/secret.py'})

    def test_in_progress_tool_is_visible_before_command_finishes(self):
        import concurrent.futures
        row,run=self.dispatch();job=tools.prepare(run,row,[]);client=tools.Tools(job)
        with concurrent.futures.ThreadPoolExecutor() as executor:
            future=executor.submit(client.call,'run_command',{'command':'Start-Sleep -Milliseconds 600; Write-Output FIXTURE'})
            deadline=time.monotonic()+5;seen=False
            while time.monotonic()<deadline:
                if client.proof_file.exists() and tools.Tools(job).proof['events'][-1]['status']=='in_progress':seen=True;break
                time.sleep(.02)
            self.assertTrue(seen)
            self.assertEqual(future.result(timeout=10)['exit_code'],0)
        self.assertEqual(tools.Tools(job).proof['events'][-1]['status'],'completed')

    def test_continue_keeps_identity_and_scope_in_a_new_session_handoff(self):
        row,run=self.dispatch()
        registry.patch_task(run,status='completed',zcode_session_id='modeldesk-zcode-fixture',
            zcode={'cleanup':'closed','prompt_attempted':True},answer='Previous implementation complete')
        endpoints.update('zcode',{'native_selection':{'model':worker.MODEL,'reasoning':'high'}},self.settings)
        child,child_run=self.dispatch(continue_task=row['id'],prompt='Add one boundary test')
        self.assertEqual(child['parent_task_id'],row['id'])
        self.assertEqual(child['session_policy'],'fresh_with_handoff')
        self.assertNotIn('zcode_session_id',child)
        self.assertEqual(child['zcode_selection'],row['zcode_selection'])
        data=(child_run/'continuation.txt').read_bytes()
        self.assertEqual(hashlib.sha256(data).hexdigest(),child['zcode_handoff']['sha256'])
        self.assertIn('Previous implementation complete',data.decode())
        with self.assertRaises(ValueError):self.dispatch(continue_task=row['id'],files=['other.py'])
        with self.assertRaises(ValueError):self.dispatch(continue_task=row['id'],reuse_session=True)

    def test_failed_unknown_and_stale_parent_cannot_continue(self):
        row,run=self.dispatch()
        registry.patch_task(run,status='failed',zcode_session_id='owned',zcode={'cleanup':'closed','outcome_unknown':True})
        self.assertFalse(engines.has_session(registry.read_task(run)))
        with self.assertRaises(ValueError):self.dispatch(continue_task=row['id'])
        registry.patch_task(run,status='completed',zcode={'cleanup':'closed'},zcode_tool_revision='stale')
        with self.assertRaisesRegex(ValueError,'保护版本'):self.dispatch(continue_task=row['id'])

    def test_worker_keeps_queued_thought_and_reports_live_tool_progress(self):
        row,run=self.dispatch();worker.select_model(worker.MODEL,'high')
        def execute(job,on_event):
            self.assertEqual(job['selection']['options']['reasoningLevel'],'max')
            on_event({'type':'connected'});on_event({'type':'prompt_attempted'})
            client=tools.Tools(job['tool_job']);client.proof['listed']=True;client.save()
            client.call('write_file',{'path':'a.py','content':'print(42)'})
            on_event({'type':'heartbeat'})
            progress=registry.read_task(run)
            self.assertEqual(progress['status'],'running');self.assertEqual(progress['tool_calls'][0]['title'],'write_file')
            on_event({'type':'closed'})
            return [{'type':'result','terminal_event':'turn.completed','value':{'settings':{'model':row['zcode_selection']},'messages':[{'info':{'role':'assistant'},'parts':[{'type':'text','text':'done'}]}]}}]
        self.assertEqual(worker.run_task(run,row,registry.get_model(row['model_id']),execute=execute),0)


if __name__=='__main__':unittest.main()
