"""Actual stdio/HTTP plan operations; temporary records, captured executors."""
from http.server import ThreadingHTTPServer
import json
import threading
import unittest

import test_brain_plan as fixture
from test_brain_mcp_server import StdioClient, visible_payload
import approved_plan as plans
import brain_gateway as gateway
import model_registry as registry
import panel_server as panel


class PlanProtocolTests(unittest.TestCase):
    def setUp(self):
        self.f=fixture.BrainPlanTests();self.f.setUp();self.addCleanup(self.f.doCleanups)
        self.server=ThreadingHTTPServer(('127.0.0.1',0),panel.Handler)
        self.server.daemon_threads=True
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        def cleanup():
            self.server.shutdown();self.server.server_close();self.thread.join(5)
        self.addCleanup(cleanup)

    def test_both_declared_hosts_use_complete_json_and_original_approval_over_stdio(self):
        for host in ('codex','dsh'):
            with self.subTest(host=host):
                client=StdioClient(['--project',self.f.f.project,'--host',host,'--owner','protocol-'+host,
                    '--role','brain','--session-binding','user_bound','--session-id','protocol-'+host,
                    '--panel-url','http://127.0.0.1:'+str(self.server.server_port)],registry_home=registry.HERE)
                try:
                    client.initialize()
                    tool=next(t for t in client.list_tools() if t['name']=='brain_plan')
                    self.assertNotIn('approve',tool['inputSchema']['properties']['action']['enum'])
                    connected=visible_payload(client.call('brain_connect'))
                    context=connected['context']
                    cap=next(key for key,row in gateway._CAPABILITIES.items()
                        if row['context'].binding_key==context['owner']['binding_key'])
                    def call(action,**values):
                        result=client.call('brain_plan',{'action':action,**values})
                        self.assertFalse(result.get('isError'),result)
                        return visible_payload(result)
                    draft=call('draft',request_id='protocol-draft-'+host,title='Protocol fixture',
                        objective='No model requests',stages=[{'key':'a','title':'Fixture',
                            'objective':'Fixture only','kind':'chat','model_id':'home','acceptance':['Works']}])
                    goal=draft['receipt']['goal_id']
                    self.assertIn('checks',tool['inputSchema']['properties'])
                    preview=call('preview',goal_id=goal,checks=[])['result']
                    self.assertEqual(preview['document']['definition']['responsibility']['host'],host)
                    early=client.call('brain_plan',{'action':'resume','request_id':'early-'+host,
                        'goal_id':goal,'digest':preview['digest']})
                    self.assertTrue(early['isError']);self.assertEqual(self.f.scheduled,[])
                    digest=self.f.approve(goal)
                    self.assertFalse(plans.view(goal,self.f.f.project)['attached'])
                    call('resume',request_id='protocol-resume-'+host,goal_id=goal,digest=digest)
                    self.f.drain();task_id=self.f.task_ids[-1]
                    task=registry.read_task(registry.task_dir(task_id,self.f.f.project))
                    self.assertEqual(task['brain']['owner'],context['owner']['id'])
                    self.assertEqual(task['brain']['host'],host)
                    self.f.complete_and_accept(task_id,cap,request='protocol-review-'+host)
                    handover=call('handover',request_id='protocol-handover-'+host,task_id=task_id,
                        summary='Independently checked fixture')
                    self.assertTrue(handover['receipt']['handover_created'])
                    self.f.drain()
                    self.assertEqual(call('status',goal_id=goal)['result']['plan']['status'],'completed')
                    lookup=visible_payload(client.call('brain_request_status',{'request_id':'protocol-draft-'+host}))
                    self.assertEqual(lookup['request_state'],'completed')
                    self.assertEqual(lookup['plan']['goal_id'],goal)
                    visible=json.dumps(client.tool_responses,ensure_ascii=False)
                    for capability in gateway._CAPABILITIES:self.assertNotIn(capability,visible)
                finally:client.close()


if __name__=='__main__':unittest.main()
