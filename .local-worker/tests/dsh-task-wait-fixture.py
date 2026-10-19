"""Called by Node's private-pipe fixture: real MCP/HTTP/core, mocked executor."""
import json
import os
from pathlib import Path
import sys
import time
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parent))
from test_brain_mcp_server import EndToEndLoopTests,StdioClient,payload


def main():
    project=Path.cwd()
    environment=dict(os.environ)
    # The base fixture initially creates an unrelated generic client; do not
    # leak this DSH private pipe into that intentionally unbound host.
    clean={k:v for k,v in environment.items() if not k.startswith('MODEL_DESK_DSH_WAKE_') and k!='MODEL_DESK_COMPLETION_HOST'}
    case=EndToEndLoopTests('runTest')
    with patch.dict(os.environ,clean,clear=True):case.setUp()
    try:
        case.client.close();case.project=project
        case.client=StdioClient(['--project',str(project),'--host','dsh','--owner','dsh-formal-fixture',
            '--role','brain','--session-binding','user_bound','--session-id','operator-fixture',
            '--native-session-id','session-formal-fixture','--launcher','dsh-brain-plugin',
            '--panel-url','http://127.0.0.1:%d'%case.server.server_address[1]],
            cwd=project,env=environment,registry_home=case.tmp.name)
        case.client.initialize()
        tools={t['name'] for t in case.client.list_tools()}
        assert {'brain_wait','brain_wakeup_probe'}<=tools
        assert not case.client.call('brain_connect')['isError']
        task_ids=[]
        for index in range(2):
            result=case.client.call('brain_dispatch',{'request_id':'formal-'+str(index),
                'prompt':'Fixture only','kind':'edit','model_id':'mock','files':['a.py'],
                'acceptance':['criterion one']})
            assert not result['isError'],result
            task_ids.append(payload(result)['task']['task_id'])
            assert payload(result)['completion_wait']['tool']=='brain_wait'
        result=case.client.call('brain_wait',{'action':'start','task_ids':task_ids})
        assert not result['isError'],result
        subscription=payload(result);assert subscription['waiting'],subscription
        wait_id=subscription['wait_id']
        duplicate=case.client.call('brain_wait',{'action':'start','task_ids':task_ids[::-1]})
        assert payload(duplicate)['wait_id']==wait_id
        case.complete(task_ids[0]);time.sleep(1.2)
        waiting=payload(case.client.call('brain_wait',{'action':'status','wait_id':wait_id}))
        assert waiting['waiting'] and waiting['send_attempts']==0,waiting
        case.complete(task_ids[1])
        record=project/'.model-desk/notifications'/wait_id/'subscription.json'
        deadline=time.monotonic()+8
        while time.monotonic()<deadline:
            data=json.loads(record.read_text(encoding='utf-8'))
            if data['listener_status']=='delivered':break
            time.sleep(.1)
        assert data['listener_status']=='delivered',data['listener_status']
        assert data['send_attempts']==1
        result=case.client.call('brain_wait',{'action':'received','wait_id':wait_id})
        receipt=payload(result)
        assert receipt['received'] and receipt['phase']=='reviewing',result
        assert receipt['records_acceptance'] is False
        for task_id in task_ids:
            delivery=payload(case.client.call('brain_delivery',{'task_id':task_id}))
            assert delivery['accepted'] is False
        assert case.submitted==task_ids
        public=json.dumps(receipt)
        assert environment['MODEL_DESK_DSH_WAKE_TOKEN'] not in public
        print(json.dumps({'formal_mcp':True,'mock_dispatches':len(task_ids),
            'completion_attempts':data['send_attempts'],'receipt':True,'accepted':False,'real_models':False}))
    finally:case.tearDown()


if __name__=='__main__':main()
