"""Adapter-specific settings are independent of model connections and routing."""
import json
from http.server import ThreadingHTTPServer
from pathlib import Path
import sys
import threading
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, build_opener, ProxyHandler

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import execution_engines as engines
import model_registry as registry
import panel_server as panel
import workflow_settings
import codex_mcp_server as desk
from test_stage_workflow import Base


class AgentSettingsTests(Base):
    def test_each_adapter_declares_only_its_own_controls(self):
        fields=engines.configuration('dsh')['model_fields']
        self.assertEqual([f['key'] for f in fields],['agent_preset'])
        self.assertEqual(engines.configuration('pi')['model_fields'],[])
        fields[0]['options'].clear()
        self.assertEqual(len(engines.configuration('dsh')['model_fields'][0]['options']),2)
        with patch.object(engines,'availability',return_value={'ready':True}):
            public=engines.public_agents()
        self.assertEqual(public['pi']['configuration']['model_fields'],[])
        self.assertIn('agent_preset',str(public['dsh']['configuration']))

    def test_agent_patch_preserves_every_connection_and_policy_field(self):
        data=registry.read_registry()
        row=data['models'][0]
        row['secret']='opaque-fixture-not-to-be-decrypted'
        row['agent_preset']='native'
        registry.write_json(registry.REGISTRY,data)
        original=json.loads(registry.REGISTRY.read_text(encoding='utf-8'))
        workflow_settings.save_settings({'execution_agent':'pi','paid_allowed':True,'model_scope':'paid_only',
                         'enforce_paid_budget':False,'enforce_model_call_limit':False})
        workflow_before=(registry.HERE/'workflow.json').read_bytes()
        result=registry.update_agent_settings(row['id'],'dsh',{'agent_preset':'standard'})
        updated=json.loads(registry.REGISTRY.read_text(encoding='utf-8'))
        expected=json.loads(json.dumps(original))
        expected['models'][0]['agent_preset']='standard'
        expected['models'][0]['updated_at']=updated['models'][0]['updated_at']
        self.assertEqual(updated,expected)
        self.assertEqual(result['agent_preset'],'standard')
        self.assertNotIn('opaque-fixture',json.dumps(result))
        self.assertEqual((registry.HERE/'workflow.json').read_bytes(),workflow_before)

    def test_model_connection_edit_without_agent_fields_preserves_preset(self):
        registry.update_agent_settings('home','dsh',{'agent_preset':'standard'})
        body=registry.list_models()['models'][0]
        body.pop('agent_preset',None)
        body['name']='connection rename'
        result=registry.upsert_model(body,'home')
        self.assertEqual(result['agent_preset'],'standard')
        self.assertEqual(result['name'],'connection rename')

    def test_parallel_connection_and_agent_edits_preserve_both_changes(self):
        from concurrent.futures import ThreadPoolExecutor
        body=registry.list_models()['models'][0]
        body.pop('agent_preset',None);body['name']='parallel connection edit'
        with ThreadPoolExecutor(max_workers=2) as pool:
            edits=[pool.submit(registry.upsert_model,body,'home'),
                   pool.submit(registry.update_agent_settings,'home','dsh',{'agent_preset':'standard'})]
            for edit in edits:edit.result()
        row=registry.list_models()['models'][0]
        self.assertEqual(row['name'],'parallel connection edit')
        self.assertEqual(row['agent_preset'],'standard')

    def test_invalid_cross_agent_or_connection_patches_write_nothing(self):
        before=registry.REGISTRY.read_bytes()
        for engine,values in [('dsh',{'agent_preset':'bogus'}),('pi',{'agent_preset':'native'}),
                              ('unknown',{'agent_preset':'native'}),('dsh',{}),('dsh',[]),
                              ('dsh',{'api_base':'http://bad.invalid'}),('dsh',{'agent_preset':True})]:
            with self.assertRaises(ValueError): registry.update_agent_settings('home',engine,values)
            self.assertEqual(registry.REGISTRY.read_bytes(),before)
        with self.assertRaisesRegex(ValueError,'模型不存在'):
            registry.update_agent_settings('missing','dsh',{'agent_preset':'native'})
        self.assertEqual(registry.REGISTRY.read_bytes(),before)

    def test_existing_mcp_whitelist_and_http_route_accept_the_scoped_patch(self):
        path='/api/models/home/save'
        self.assertEqual(desk.validate_request(path,'POST'),path)
        server=ThreadingHTTPServer(('127.0.0.1',0),panel.Handler)
        threading.Thread(target=server.serve_forever,daemon=True).start()
        opener=build_opener(ProxyHandler({}))
        def post(body):
            request=Request(f'http://127.0.0.1:{server.server_port}'+path,data=json.dumps(body).encode(),
                            headers={'Content-Type':'application/json','X-Panel-Token':panel.TOKEN})
            with opener.open(request,timeout=5) as response:return json.load(response)
        try:
            with patch.object(panel.EXECUTOR,'submit') as start:
                result=post({'agent_settings':{'engine':'dsh','values':{'agent_preset':'standard'}}})
            self.assertEqual(result['agent_preset'],'standard')
            start.assert_not_called()
            before=registry.REGISTRY.read_bytes()
            with self.assertRaises(HTTPError):
                post({'agent_settings':{'engine':'dsh','values':{'agent_preset':'native'}},'api_base':'http://bad.invalid'})
            self.assertEqual(registry.REGISTRY.read_bytes(),before)
            with patch.object(panel,'public_tasks',return_value=[{'model_id':'home','status':'running'}]):
                with self.assertRaises(HTTPError):
                    post({'agent_settings':{'engine':'dsh','values':{'agent_preset':'native'}}})
            self.assertEqual(registry.REGISTRY.read_bytes(),before)
        finally:server.shutdown();server.server_close()


if __name__=='__main__':unittest.main()
