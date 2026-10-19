"""Independent HTTP and MCP context integration; only temporary project records."""
import asyncio
import json
import types as pytypes
import unittest
from pathlib import Path
from unittest import mock
from urllib.error import HTTPError
from urllib.request import Request

from test_panel_observability import Base
import panel_server as panel
import project_scope as scope
import stage_store
import model_registry as registry
import codex_mcp_server as desk

class ProjectHttpTests(Base):
    def setUp(self):
        super().setUp()
        self.other=Path(self.temp.name)/'projectB';self.other.mkdir()
        self.empty=Path(self.temp.name)/'empty';self.empty.mkdir()
        self.task('a',project=str(self.project))
        self.task('b',project=str(self.other))
        self.task('orphan')
        self.ga=stage_store.create_goal('A','A',str(self.project))
        self.gb=stage_store.create_goal('B','B',str(self.other))
        self.sa=stage_store.add_stage(self.ga['id'],'A stage','scope fixture',files=['a.py'],acceptance=['fixture passed'],candidates=['home'])

    def call(self,path,view,body=None):
        headers={'X-Model-Desk-View':scope.encode_view_header(view),'X-Panel-Token':panel.TOKEN,'Content-Type':'application/json'}
        req=Request(self.url+path,headers=headers,data=None if body is None else json.dumps(body).encode())
        with mock.patch.object(panel.deepseek_billing,'enrich_models',side_effect=lambda rows:rows),mock.patch.object(panel.codex_usage,'usage_state',return_value={'status':'unavailable'}):
            with self.opener.open(req,timeout=15) as response:return json.load(response)

    def test_project_empty_unbound_all_and_global_contract(self):
        a={'mode':'project','project':str(self.project)}
        result=self.call('/api/state',a)
        self.assertEqual([t['id'] for t in result['tasks']],['a'])
        self.assertEqual(result['stages']['stats']['goals'],1)
        self.assertEqual(result['workflow']['daily_scope'],'global')
        self.assertEqual([g['id'] for g in self.call('/api/tree',a)['goals']],[self.ga['id']])
        self.assertEqual(len(self.call('/api/state',{'mode':'all'})['tasks']),3)
        for v in ({'mode':'unbound'},{'mode':'project','project':str(self.empty)}):
            self.assertEqual(self.call('/api/state',v)['tasks'],[])
            self.assertEqual(self.call('/api/tree',v)['goals'],[])
        self.assertEqual(len(panel.public_tasks()),3,'internal global queue listing remains complete')

    def test_other_project_read_cancel_and_creation_are_denied(self):
        a={'mode':'project','project':str(self.project)}
        for path,body in [('/api/tasks/b',None),('/api/tasks/b/log',None),('/api/tasks/b/cancel',{}),
                          ('/api/goals/'+self.gb['id'],None),('/api/goals',{'title':'bad','objective':'bad','project':str(self.other)})]:
            with self.subTest(path=path),self.assertRaises(HTTPError) as e:self.call(path,a,body)
            self.assertEqual(e.exception.code,403)
        self.assertEqual(registry.read_task(registry.task_dir('b'))['status'],'completed')
        with self.assertRaises(HTTPError):self.call('/api/goals',{'mode':'all'},{'title':'bad','objective':'bad'})

    def test_invalid_header_is_not_a_global_fallback(self):
        req=Request(self.url+'/api/state',headers={'X-Model-Desk-View':'not-valid-***'})
        with self.assertRaises(HTTPError) as e:self.opener.open(req,timeout=10)
        self.assertEqual(e.exception.code,400)

    def test_missing_scope_cannot_read_global_history_or_by_id(self):
        for query in ('','?project='+str(self.other)):
            with mock.patch.object(panel.deepseek_billing,'enrich_models',side_effect=lambda rows:rows), \
                 mock.patch.object(panel.codex_usage,'usage_state',return_value={'status':'unavailable'}):
                with self.opener.open(self.url+'/api/state'+query,timeout=10) as response:
                    result=json.load(response)
            self.assertEqual(result['view']['mode'],'unbound')
            self.assertEqual(result['tasks'],[])
            self.assertIsNone(result['current_project'])
        for path in ('/api/tasks/a','/api/tasks/b/log','/api/stages/'+self.sa['id']):
            with self.assertRaises(HTTPError) as error:
                self.opener.open(self.url+path,timeout=10)
            self.assertEqual(error.exception.code,403)
        self.assertEqual(len(self.call('/api/state',{'mode':'all'})['tasks']),3)

    def test_model_occupancy_is_global_but_other_project_activity_details_are_not(self):
        active=[{'id':'active-a','title':'private A','project':str(self.project),'model_id':'home','status':'running'},
                {'id':'active-b','title':'private B','project':str(self.other),'model_id':'home','status':'queued'}]
        activity={'scope':'global','total':2,'complete':True,'tasks':active}
        with mock.patch.object(panel,'execution_activity',return_value=activity):
            for view in ({'mode':'unbound'},{'mode':'project','project':str(self.empty)}):
                result=self.call('/api/state',view)['execution_activity']
                self.assertEqual(result['tasks'],[])
                self.assertEqual(result['total'],2)
                self.assertEqual(result['running'],1)
                self.assertEqual(result['model_counts'],{'home':2})
            result=self.call('/api/state',{'mode':'project','project':str(self.project)})
            self.assertEqual([row['id'] for row in result['execution_activity']['tasks']],['active-a'])
            self.assertNotIn('private B',json.dumps(result,ensure_ascii=False))
            self.assertEqual(len(self.call('/api/state',{'mode':'all'})['execution_activity']['tasks']),2)

    def test_existing_cli_binds_known_task_and_explicit_new_work(self):
        import dispatch_task
        runtime=Path(self.temp.name)/'panel-runtime.json'
        runtime.write_text(json.dumps({'url':self.url}),encoding='utf-8')
        with mock.patch.object(dispatch_task,'HERE',Path(self.temp.name)):
            request=dispatch_task.connect_panel()
            detail=request('/api/tasks/a')
            self.assertEqual(detail['task']['id'],'a')
            self.assertEqual(detail['view']['project'],str(self.project))
            self.assertEqual(dispatch_task.request_view('/api/tasks',{'project':str(self.other)})['project'],str(self.other))
            self.assertEqual(dispatch_task.request_view('/api/session')['mode'],'unbound')
            self.assertEqual(dispatch_task.request_view('/api/tasks',{'rework_of':'a'})['project'],str(self.project))
            self.assertEqual(dispatch_task.request_view('/api/stages/'+self.sa['id'])['project'],str(self.project))

    def test_folder_picker_is_explicit_authenticated_and_does_not_modify_view(self):
        import folder_picker
        view={'mode':'unbound'}
        with mock.patch.object(folder_picker.JOBS,'start',return_value={'id':'a'*32,'status':'pending'}) as start:
            req=Request(self.url+'/api/projects/pick',data=b'{}',headers={'Content-Type':'application/json'})
            with self.assertRaises(HTTPError) as failure:self.opener.open(req,timeout=10)
            self.assertEqual(failure.exception.code,403);start.assert_not_called()
            self.assertEqual(self.call('/api/projects/pick',view,{})['status'],'pending')
            start.assert_called_once_with(None)
        with mock.patch.object(folder_picker.JOBS,'get',return_value={'id':'a'*32,'status':'selected','project':str(self.other)}):
            self.assertEqual(self.call('/api/projects/pick/'+'a'*32,view)['project'],str(self.other))
        self.assertEqual(self.call('/api/state',view)['tasks'],[])
        self.assertEqual(self.call('/api/state',{'mode':'project','project':str(self.project)})['tasks'][0]['id'],'a')

    def test_picker_mcp_whitelist_only_concrete_routes(self):
        self.assertEqual(desk.validate_request('/api/projects/pick','POST'),'/api/projects/pick')
        self.assertEqual(desk.validate_request('/api/projects/pick/'+'b'*32,'GET'),'/api/projects/pick/'+'b'*32)
        for method,path in [('GET','/api/projects/pick'),('POST','/api/projects/pick/'+'b'*32),('GET','/api/projects/pick/../x'),('GET','/api/projects/pick/abc')]:
            with self.assertRaises(desk.PanelError):desk.validate_request(path,method)

    def test_project_bookmarks_persist_metadata_without_changing_scope(self):
        view={'mode':'project','project':str(self.other)}
        result=self.call('/api/projects/remember',view,{})
        self.assertEqual(result['projects'][0]['path'],str(self.other))
        catalog=self.call('/api/projects',{'mode':'unbound'})
        self.assertEqual(catalog['recent_projects'][0]['path'],str(self.other))
        self.assertEqual(self.call('/api/state',{'mode':'unbound'})['tasks'],[])
        self.assertEqual(self.call('/api/state',{'mode':'project','project':str(self.project)})['tasks'][0]['id'],'a')
        with self.assertRaises(HTTPError):self.call('/api/projects/remember',{'mode':'all'},{})
        self.assertEqual(desk.validate_request('/api/projects/remember','POST'),'/api/projects/remember')

class OpeningContextTests(unittest.IsolatedAsyncioTestCase):
    async def test_root_optional_explicit_and_ambiguous(self):
        import tempfile
        with tempfile.TemporaryDirectory() as folder:
            async def roots():return pytypes.SimpleNamespace(roots=[pytypes.SimpleNamespace(uri=Path(folder).as_uri())])
            session=pytypes.SimpleNamespace(check_client_capability=lambda _:True,list_roots=roots)
            server=pytypes.SimpleNamespace(request_context=pytypes.SimpleNamespace(session=session))
            result=await desk.opening_view(server,{})
            self.assertEqual(result['mode'],'project')
            self.assertEqual(scope.project_key(result['project']),scope.project_key(folder))
            session.check_client_capability=lambda _:False
            self.assertEqual((await desk.opening_view(server,{}))['mode'],'unbound')
            self.assertEqual((await desk.opening_view(server,{'project':folder}))['mode'],'project')
            session.check_client_capability=lambda _:True
            async def many():return pytypes.SimpleNamespace(roots=[pytypes.SimpleNamespace(uri=Path(folder).as_uri())]*2)
            session.list_roots=many
            self.assertEqual((await desk.opening_view(server,{}))['mode'],'unbound')
            async def slow():await asyncio.sleep(3)
            session.list_roots=slow
            self.assertEqual((await desk.opening_view(server,{}))['mode'],'unbound')

