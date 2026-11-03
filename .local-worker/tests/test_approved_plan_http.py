"""Actual scoped HTTP route and both host proxy paths; mock dispatch only."""
from http.server import ThreadingHTTPServer
import json
import threading
import unittest
from unittest.mock import patch
from urllib.request import Request,build_opener,ProxyHandler
from urllib.error import HTTPError

import test_approved_plan as fixture
import panel_server as panel
import project_scope
import approved_plan
import codex_mcp_server as codex


class HttpTests(unittest.TestCase):
    def setUp(self):
        self.fixture=fixture.PlanTests();self.fixture.setUp();self.addCleanup(self.fixture.doCleanups)
        self.server=ThreadingHTTPServer(('127.0.0.1',0),panel.Handler);self.server.daemon_threads=True
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        def close():self.server.shutdown();self.server.server_close();self.thread.join(5)
        self.addCleanup(close)
        self.opener=build_opener(ProxyHandler({}))

    def request(self,action,body=None,*,project=None,token=True):
        view={'mode':'project','project':project or self.fixture.project}
        path='/api/goals/'+self.fixture.goal+'/plan'+('/'+action if action else '')
        headers={'Content-Type':'application/json','X-Model-Desk-View':project_scope.encode_view_header(view)}
        if token:headers['X-Panel-Token']=panel.TOKEN
        request=Request('http://127.0.0.1:%d%s'%(self.server.server_port,path),
                        data=json.dumps(body).encode() if body is not None else None,headers=headers)
        with self.opener.open(request,timeout=10) as response:return json.load(response)

    def test_preview_and_read_never_attach_or_dispatch(self):
        with patch.object(panel.EXECUTOR,'submit') as submit:
            self.assertIsNone(self.request('')['plan'])
            self.assertTrue(self.request('preview',{})['read_only'])
            self.assertIsNone(self.request('view',{})['plan'])
        submit.assert_not_called()

    def test_confirmation_digest_token_and_project_are_required(self):
        preview=self.request('preview',{})
        for body in ({'digest':preview['digest']},{'confirm':True,'digest':'wrong'}):
            with self.assertRaises(HTTPError):self.request('approve',body)
        with self.assertRaises(HTTPError) as error:self.request('approve',{'confirm':True,'digest':preview['digest']},token=False)
        self.assertEqual(error.exception.code,403)
        other=self.fixture.root/'other';other.mkdir()
        with self.assertRaises(HTTPError):self.request('preview',{},project=str(other))
        self.assertIsNone(self.request('')['plan'])

    def test_approve_pause_and_revoke_use_original_version_without_native_calls(self):
        preview=self.request('preview',{})
        with patch.object(panel.EXECUTOR,'submit') as submit:
            self.request('approve',{'confirm':True,'digest':preview['digest']})
            self.assertEqual(submit.call_count,1)
            self.assertTrue(self.request('')['attached'])
            self.request('pause',{'confirm':True,'digest':preview['digest']})
            self.assertFalse(self.request('')['attached'])
            self.assertEqual(self.request('')['plan']['status'],'paused')
            self.request('revoke',{'confirm':True,'digest':preview['digest']})
            self.assertEqual(self.request('')['plan']['status'],'revoked')

    def test_codex_whitelist_and_dsh_static_resource_are_wired(self):
        base='/api/goals/'+self.fixture.goal+'/plan'
        self.assertEqual(codex.validate_request(base,'GET'),base)
        for action in ('view','preview','approve','pause','resume','revoke'):
            self.assertEqual(codex.validate_request(base+'/'+action,'POST'),base+'/'+action)
        for path,method in ((base+'/approve','GET'),(base+'/dispatch','POST'),(base+'/../approve','POST')):
            with self.assertRaises(codex.PanelError):codex.validate_request(path,method)
        with self.opener.open('http://127.0.0.1:%d/approved-plan.js'%self.server.server_port) as response:
            self.assertIn(b'ModelDeskApprovedPlan',response.read())
        html=codex.build_workbench_html()
        self.assertIn('ModelDeskApprovedPlan',html)
        self.assertNotIn('src="/approved-plan.js"',html)


if __name__=='__main__':unittest.main()
