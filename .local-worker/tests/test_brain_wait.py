"""Bound completion tool: temporary records and mock broker/host, no model."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import brain_context
import brain_mcp_server as brain
import completion_host as host
import completion_wait as wait
import workflow_settings


class WaitTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.project=str(Path(self.temp.name).resolve())
        self.context=brain_context.build_context(self.project,'dsh','dsh-wait-fixture',role='brain',
            native_session='session-wait-fixture',operator_session='operator-fixture',session_binding='user_bound')
        env={'MODEL_DESK_COMPLETION_HOST':'dsh',
             'MODEL_DESK_DSH_WAKE_PIPE':r'\\.\pipe\model-desk-dsh-wake-'+'a'*32,
             'MODEL_DESK_DSH_WAKE_TOKEN':'a'*64,'MODEL_DESK_DSH_WAKE_SESSION':'session-wait-fixture',
             'MODEL_DESK_DSH_WAKE_OWNER':self.context.owner,'MODEL_DESK_DSH_WAKE_BINDING':self.context.binding_key}
        self.patches=[patch.dict(os.environ,env),patch.object(brain,'_CONTEXT',self.context),
            patch.object(brain,'_ACTIVE_WAIT',None),patch.object(Path,'cwd',return_value=Path(self.project)),
            patch.object(host,'read_current',return_value={'thread_id':'session-wait-fixture','project':self.project,
                      'turns':[{'id':'fixture-turn','status':'in_progress'}]}),
            patch.object(workflow_settings,'snapshot',return_value={'enabled':True,'readable':True})]
        for p in self.patches:p.start();self.addCleanup(p.stop)
        self.calls=[]
        p=patch.object(brain,'_call',side_effect=self.broker);p.start();self.addCleanup(p.stop)
        p=patch.object(wait,'_start',side_effect=self.start);p.start();self.addCleanup(p.stop)
        for task_id in ('task-a','task-b'):self.task(task_id)

    def broker(self,operation,payload):
        self.calls.append((operation,payload))
        return {'access':{'mode':'original_owner'}} if operation=='status' else {}

    def task(self,task_id,**changes):
        run=Path(self.project)/'.model-desk/runs'/task_id;run.mkdir(parents=True,exist_ok=True)
        data={'id':task_id,'project':self.project,'status':'running','timeout':0,
              'brain':{'owner':self.context.owner,'binding_key':self.context.binding_key},**changes}
        wait.registry.write_json(run/'task.json',data)

    def start(self,data):
        data.update(listener_status='listening',watcher_pid=os.getpid(),heartbeat_at=wait.now())
        wait.save(data);wait.project_status(data);return data

    def call(self,**arguments):return brain._run(brain.TOOL_WAIT,arguments).structuredContent

    def test_start_public_fields_and_same_batch_idempotence(self):
        result=self.call(action='start',task_ids=['task-b','task-a'])
        self.assertTrue(result['waiting']);self.assertFalse(result['records_acceptance'])
        self.assertFalse(result['cancels_tasks']);self.assertEqual(result['task_ids'],['task-a','task-b'])
        self.assertEqual(result['wait_id'],self.call(action='start',task_ids=['task-a','task-b'])['wait_id'])
        self.assertEqual([name for name,_ in self.calls],['describe','status','status']*2)
        for private in ('source_hashes','watcher_pid','binding_key','MODEL_DESK_DSH_WAKE_TOKEN'):
            self.assertNotIn(private,json.dumps(result))

    def test_rejects_another_active_batch_without_dispatching(self):
        first=self.call(action='start',task_ids=['task-a'])
        with self.assertRaisesRegex(ValueError,'已有等待批次'):self.call(action='start',task_ids=['task-b'])
        self.assertEqual(self.call(action='status',wait_id=first['wait_id'])['task_ids'],['task-a'])

    def test_closed_switch_rejects_start_but_allows_status_stop_and_receipt(self):
        result=self.call(action='start',task_ids=['task-a']);wait_id=result['wait_id']
        with patch.object(workflow_settings,'snapshot',return_value={'enabled':False,'readable':True}):
            with self.assertRaisesRegex(ValueError,'开关'):self.call(action='start',task_ids=['task-a'])
            self.assertTrue(self.call(action='status',wait_id=wait_id)['waiting'])
            stopped=self.call(action='stop',wait_id=wait_id)
        self.assertEqual(stopped['listener_status'],'stopped');self.assertFalse(stopped['waiting'])
        self.assertEqual(wait.task_record(self.project,'task-a')['status'],'running')

    def test_history_read_access_is_not_notification_authority(self):
        with patch.object(brain,'_call',return_value={'access':{'mode':'project_read_only'}}):
            with self.assertRaisesRegex(ValueError,'历史只读'):self.call(action='start',task_ids=['task-a'])
        self.assertFalse((Path(self.project)/'.model-desk/notifications').exists())

    def test_core_also_checks_exact_original_binding(self):
        self.task('task-a',brain={'owner':self.context.owner,'binding_key':'b'*64})
        with self.assertRaisesRegex(ValueError,'原主脑'):self.call(action='start',task_ids=['task-a'])

    def test_no_foreign_host_or_missing_connection(self):
        with patch.dict(os.environ,{'MODEL_DESK_COMPLETION_HOST':'codex'}):
            self.assertNotIn(brain.TOOL_WAIT,brain.available_tools())
            with self.assertRaises(ValueError):self.call(action='start',task_ids=['task-a'])
        with patch.object(brain,'_call',side_effect=brain.brain_transport.NotConnected('connect first')):
            with self.assertRaises(brain.brain_transport.NotConnected):self.call(action='start',task_ids=['task-a'])

    def test_strict_fixed_arguments(self):
        for arguments in ({'action':'start','task_ids':['task-a'],'project':self.project},
                          {'action':'start','task_ids':['task-a'],'prompt':'arbitrary'},
                          {'action':'start','task_ids':['task-a','task-a']},
                          {'action':'start','task_ids':[]},
                          {'action':'start','task_ids':['../task-a']},
                          {'action':'status','task_ids':['task-a']},
                          {'action':'received','wait_id':'../other'},
                          {'action':'stop'}, {'action':'resume'}):
            with self.subTest(arguments=arguments):
                with self.assertRaises(ValueError):self.call(**arguments)

    def test_receipt_requires_send_claim_and_is_not_acceptance(self):
        result=self.call(action='start',task_ids=['task-a']);wait_id=result['wait_id']
        with self.assertRaisesRegex(ValueError,'没有匹配'):self.call(action='received',wait_id=wait_id)
        self.task('task-a',status='completed',ended_at=wait.now())
        data=wait.load(self.project,wait_id);data['send_attempts']=1;wait.save(data)
        (wait.paths(self.project,wait_id)/'send.claim').touch()
        first=self.call(action='received',wait_id=wait_id)
        second=self.call(action='received',wait_id=wait_id)
        self.assertTrue(first['received']);self.assertFalse(first['records_acceptance'])
        self.assertEqual(first['receipt_at'],second['receipt_at'])
        self.assertEqual(first['phase'],'reviewing')
        self.assertFalse((Path(self.project)/'.model-desk/runs/task-a/review.json').exists())

    def test_dead_listener_is_not_confirmed_waiting_or_automatically_restarted(self):
        result=self.call(action='start',task_ids=['task-a'])
        with patch.object(wait,'process_alive',return_value=False):
            status=self.call(action='status',wait_id=result['wait_id'])
            repeated=self.call(action='start',task_ids=['task-a'])
        self.assertFalse(status['waiting']);self.assertFalse(repeated['waiting'])
        self.assertEqual(status['listener_health'],'unconfirmed')

    def test_stale_heartbeat_does_not_claim_confirmed_waiting(self):
        result=self.call(action='start',task_ids=['task-a'])
        data=wait.load(self.project,result['wait_id']);data['heartbeat_at']='2000-01-01T00:00:00+00:00';wait.save(data)
        status=self.call(action='status',wait_id=result['wait_id'])
        self.assertFalse(status['waiting']);self.assertEqual(status['listener_health'],'unconfirmed')

    def test_startup_unknown_retains_reference_and_does_not_start_again(self):
        with patch.object(wait,'_start',side_effect=ValueError('fixture startup unknown')) as start:
            result=brain._run(brain.TOOL_WAIT,{'action':'start','task_ids':['task-a']})
            self.assertTrue(result.isError)
            unknown=result.structuredContent
            self.assertEqual(unknown['outcome'],'unknown');self.assertFalse(unknown['waiting'])
            self.assertEqual(unknown['task_ids'],['task-a'])
            repeated=self.call(action='start',task_ids=['task-a'])
            self.assertEqual(repeated['wait_id'],unknown['wait_id'])
            self.assertFalse(repeated['waiting']);start.assert_called_once()


if __name__=='__main__':unittest.main()
