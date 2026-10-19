"""Host adapter selection and original-responsibility checks; no real pipe/model."""
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
import dsh_notify


class HostTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.project=str(Path(self.temp.name).resolve())
        self.env={'MODEL_DESK_COMPLETION_HOST':'dsh',
            'MODEL_DESK_DSH_WAKE_PIPE':r'\\.\pipe\model-desk-dsh-wake-'+'a'*32,
            'MODEL_DESK_DSH_WAKE_TOKEN':'a'*64,'MODEL_DESK_DSH_WAKE_SESSION':'session-fixture',
            'MODEL_DESK_DSH_WAKE_OWNER':'dsh-fixture','MODEL_DESK_DSH_WAKE_BINDING':'b'*64}
        p=patch.dict(os.environ,self.env);p.start();self.addCleanup(p.stop)

    def test_dsh_child_environment_excludes_codex_and_provider_credentials(self):
        with patch.dict(os.environ,{'CODEX_THREAD_ID':'private-codex','CODEX_APP_TOOLS_PIPE_PATH':'private-pipe',
                                   'OPENAI_API_KEY':'private-key','DEEPSEEK_API_KEY':'private-key'}):
            env=host.child_environment()
        self.assertEqual(env['MODEL_DESK_COMPLETION_HOST'],'dsh')
        for key in ('CODEX_THREAD_ID','CODEX_APP_TOOLS_PIPE_PATH','OPENAI_API_KEY','DEEPSEEK_API_KEY'):
            self.assertNotIn(key,env)

    def test_missing_or_unknown_host_never_falls_back_to_codex(self):
        with patch.dict(os.environ,{'MODEL_DESK_DSH_WAKE_PIPE':''}),patch('desktop_notify.read_current') as codex:
            with self.assertRaises(ValueError):host.calling_thread()
            codex.assert_not_called()
        with patch.dict(os.environ,{'MODEL_DESK_COMPLETION_HOST':'unknown'}):
            with self.assertRaises(ValueError):host.read_current()

    def test_shared_waiter_requires_original_owner_and_responsibility(self):
        observed={'thread_id':'session-fixture','project':self.project,'turns':[{'id':'turn-one','status':'in_progress'}]}
        run=Path(self.project)/'.model-desk/runs/task-a';run.mkdir(parents=True)
        data={'id':'task-a','project':self.project,'status':'running',
              'brain':{'owner':'other','binding_key':'b'*64}}
        wait.registry.write_json(run/'task.json',data)
        with patch.object(Path,'cwd',return_value=Path(self.project)),patch.object(host,'read_current',return_value=observed), \
             patch.object(wait,'_start',side_effect=lambda value:value):
            context=wait.preflight(self.project)
            with self.assertRaisesRegex(ValueError,'原主脑'):
                wait.arm(self.project,['task-a'],context)
            data['brain']['owner']='dsh-fixture'
            wait.registry.write_json(run/'task.json',data)
            subscription=wait.arm(self.project,['task-a'],context)
            self.assertEqual(subscription['host'],'dsh')
            data['brain']['binding_key']='c'*64
            wait.registry.write_json(run/'task.json',data)
            with self.assertRaisesRegex(ValueError,'责任绑定已变化'):
                wait.owned_task(subscription,'task-a')

    def test_fixed_reference_does_not_forward_extra_prompt_text(self):
        reference={'project':self.project,'wait_id':'wait-'+'a'*24,'task_ids':['task-a']}
        with patch.object(dsh_notify,'call',return_value={'host_accepted':True}) as call:
            result=dsh_notify.send_current('session-fixture',
                '[Model Desk completion event] '+json.dumps(reference)+' ARBITRARY_TRAILING_TEXT',
                'model-desk-'+reference['wait_id'])
        self.assertTrue(result['host_accepted'])
        self.assertEqual(call.call_args.args,('send',{'reference':reference}))

    def test_probe_stop_uses_original_probe_reference(self):
        probe_id='probe-'+'a'*32
        with patch.object(dsh_notify,'call',return_value={'listener_status':'stopped'}) as call:
            self.assertEqual(dsh_notify.probe('stop',probe_id)['listener_status'],'stopped')
        call.assert_called_once_with('probe',{'action':'stop','probe_id':probe_id})

    def test_probe_tool_is_native_bound_and_rejects_target_or_prompt_arguments(self):
        context=brain_context.build_context(self.project,'dsh','dsh-fixture',role='brain',
            native_session='session-fixture',operator_session='operator-fixture',session_binding='user_bound')
        with patch.object(brain,'_CONTEXT',context),patch.object(brain,'_call',return_value={}), \
             patch.object(dsh_notify,'probe',return_value={'listener_status':'listening'}) as probe:
            self.assertIn(brain.TOOL_WAKE_PROBE,brain.available_tools())
            result=brain._run(brain.TOOL_WAKE_PROBE,{'action':'start'})
            self.assertEqual(result.structuredContent['listener_status'],'listening')
            probe.assert_called_once_with('start',None)
            for extra in ({'prompt':'arbitrary'},{'project':self.project},{'thread_id':'other'},{'delay':0}):
                with self.assertRaises(ValueError):brain._run(brain.TOOL_WAKE_PROBE,{'action':'start',**extra})
            with self.assertRaises(ValueError):brain._run(brain.TOOL_WAKE_PROBE,{'action':'received'})
        with patch.object(brain,'_CONTEXT',brain_context.build_context(self.project,'other-host','dsh-fixture')):
            self.assertNotIn(brain.TOOL_WAKE_PROBE,brain.available_tools())


if __name__=='__main__':unittest.main()
