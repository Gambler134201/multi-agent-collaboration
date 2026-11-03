"""Native hook configuration and controller evidence, no native model calls."""
import hashlib
import json
from pathlib import Path
import sys
import subprocess
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import zcode_native_tools as native
import model_registry as registry
import task_workspace
import zcode_worker as worker
import test_zcode_integration as integration
from test_task_workspace import git
import panel_server as panel


class NativeConfiguration(unittest.TestCase):
    def test_protected_path_snapshot_cannot_be_weakened_in_job(self):
        self.state['task_policy']={'schema':2,'write_scope':'workspace',
            'home':str(self.project/'synthetic-home'),'deny_paths':[str(self.project/'private-account')]}
        registry.write_json(self.run/'task.json',self.state)
        file=self.prepare()
        self.assertEqual(native.load(file)['task_policy'],self.state['task_policy'])
        data=registry.read_json(file)
        data['task_policy']['deny_paths']=[]
        registry.write_json(file,data)
        with self.assertRaisesRegex(ValueError,'写入范围与任务快照'):
            native.load(file)

    def test_task_contract_is_bound_to_authoritative_record(self):
        self.state['task_policy']={'schema':1,'write_scope':'workspace'}
        registry.write_json(self.run/'task.json',self.state)
        file=self.prepare()
        self.assertEqual(native.load(file)['task_policy'],self.state['task_policy'])
        state={**self.state,'task_policy':{'schema':1,'write_scope':'declared_files'}}
        registry.write_json(self.run/'task.json',state)
        with self.assertRaisesRegex(ValueError,'写入范围与任务快照'):
            native.load(file)

    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='zcode-native-');self.addCleanup(self.temp.cleanup)
        self.project=Path(self.temp.name).resolve();self.run=self.project/'.model-desk/runs/native-fixture';self.run.mkdir(parents=True)
        self.state={'id':'native-fixture','project':str(self.project),'kind':'edit','files':['a.py'],
                    'zcode_access_mode':'project'}
        self.sid='modeldesk-zcode-native-fixture'
        self.enterContext(patch.object(native,'revision',return_value='fixture-revision'))

    def prepare(self):return native.prepare(self.run,self.state,[self.project/'profile'],self.sid)

    def test_profile_uses_native_process_hooks_and_preserves_existing_settings(self):
        directory=self.project/'.zcode';directory.mkdir()
        old={'theme':'fixture','hooks':{'enabled':True,'events':{'Stop':[{'hooks':[{'type':'process','command':'fixture-hook'}]}]}}}
        registry.write_json(directory/'config.json',old)
        profile=native.install_profile(self.project)
        data=registry.read_json(directory/'config.json')
        self.assertEqual(data['theme'],'fixture');self.assertEqual(data['hooks']['events']['Stop'],old['hooks']['events']['Stop'])
        self.assertEqual(set(data['hooks']['events']),{'Stop',*native.EVENTS})
        self.assertEqual(native.install_profile(self.project)['sha256'],profile['sha256'])
        self.assertNotIn('mcp',json.dumps(data).lower())

    def test_existing_disabled_hooks_are_not_enabled_silently(self):
        directory=self.project/'.zcode';directory.mkdir()
        data={'hooks':{'enabled':False,'events':{'Stop':[{'hooks':[{'type':'process','command':'fixture'}]}]}}}
        registry.write_json(directory/'config.json',data);before=(directory/'config.json').read_bytes()
        with self.assertRaisesRegex(ValueError,'未自动启用'):native.install_profile(self.project)
        self.assertEqual((directory/'config.json').read_bytes(),before)

    def test_configured_native_node_is_used_even_without_path_lookup(self):
        node=self.project/'configured-node.exe';node.write_bytes(b'fixture not executed')
        with patch.object(native.runtime_paths,'node_executable',return_value=node):
            profile=native.install_profile(self.project)
        self.assertEqual(profile['command'],str(node))

    def test_prepare_binds_index_job_and_native_tools_without_mcp(self):
        file=self.prepare();job=native.load(file)
        self.assertEqual(job['tools'],['Read','Glob','Grep','WebFetch','WebSearch','Write','Edit','Bash'])
        self.assertEqual(job['native_session_id'],self.sid)
        index=registry.read_json(self.project/'.model-desk/zcode-native-sessions.json')
        self.assertEqual(index['sessions'][self.sid]['job_hash'],hashlib.sha256(file.read_bytes()).hexdigest())
        self.assertNotIn('mcpServers',job)

    def test_chat_cannot_gain_native_write_or_terminal(self):
        self.state.update(kind='chat',files=[])
        job=native.load(self.prepare());self.assertEqual(job['tools'],list(native.READS))

    def test_task_temp_dir_requires_isolation_and_edit_kind(self):
        # A task running directly in the user's tree declares no scratch
        # directory, and read-only tasks never get one.
        self.assertIsNone(native.task_temp_dir(self.state))
        self.assertIsNone(native.task_temp_dir({**self.state,'kind':'chat'}))
        self.assertNotIn('temp_dir',native.load(self.prepare()))

    def test_tampered_temp_dir_value_is_rejected(self):
        file=self.prepare()
        data=json.loads(file.read_text(encoding='utf-8'));data['temp_dir']='../escape'
        file.write_text(json.dumps(data),encoding='utf-8')
        index=self.project/'.model-desk/zcode-native-sessions.json'
        rows=registry.read_json(index);rows['sessions'][self.sid]['job_hash']=hashlib.sha256(file.read_bytes()).hexdigest()
        registry.write_json(index,rows)
        with self.assertRaisesRegex(ValueError,'身份或工具版本变化'):native.load(file)

    def test_temp_path_check_is_the_shared_host_neutral_helper(self):
        # The ZCode-local name now delegates to the host-neutral check with the
        # same anchor, error text and return value; no second validator exists.
        self.assertEqual(native._validate_temp_path(self.project),task_workspace.task_temp_path(self.project))
        occupied=self.project/task_workspace.TASK_TEMP_DIR;occupied.write_text('fixture',encoding='utf-8')
        for check in (native._validate_temp_path,task_workspace.task_temp_path):
            with self.subTest(check=check.__name__):
                with self.assertRaisesRegex(ValueError,'同名文件'):
                    check(self.project)

    def test_scratch_root_junction_is_rejected_during_prepare_and_load(self):
        target=self.project/'ordinary-source';target.mkdir()
        scratch=self.project/task_workspace.TASK_TEMP_DIR
        def link():
            subprocess.run([str(native.runtime_paths.node_executable()),'-e',
                            'require("fs").symlinkSync(process.argv[1],process.argv[2],"junction")',
                            str(target),str(scratch)],check=True,capture_output=True)
        link()
        try:
            with patch.object(native,'task_temp_dir',return_value=task_workspace.TASK_TEMP_DIR):
                with self.assertRaisesRegex(ValueError,'临时目录.*别名'):self.prepare()
            self.assertFalse((self.run/'zcode-native-job.json').exists())
        finally:
            scratch.unlink()
        with patch.object(native,'task_temp_dir',return_value=task_workspace.TASK_TEMP_DIR):
            file=self.prepare()
        scratch.rmdir();link()
        try:
            with self.assertRaisesRegex(ValueError,'临时目录.*别名'):native.load(file)
        finally:
            scratch.unlink()

    def test_tampered_profile_or_index_fails_before_model(self):
        file=self.prepare()
        profile=self.project/'.zcode/config.json';profile.write_text('{}',encoding='utf-8')
        with self.assertRaisesRegex(ValueError,'配置变化'):native.load(file)

    def test_wrong_session_or_missing_real_hook_proof_never_passes(self):
        file=self.prepare()
        with self.assertRaises(ValueError):native.verify(file,'modeldesk-zcode-other')
        with self.assertRaises(FileNotFoundError):native.verify(file,self.sid)

    def test_native_tool_progress_merges_pre_post_without_mislabeling_nonzero_as_permission(self):
        proof={'events':[{'tool':'Bash','call_id':'call-1','event':'PreToolUse','allowed':True,'input_hash':'fixture'},
                         {'tool':'Bash','call_id':'call-1','event':'PostToolUse','allowed':True,'ok':False,'exit_code':1,'input_hash':'fixture'}]}
        calls=native.public_calls(proof)
        self.assertEqual(len(calls),1);self.assertEqual(calls[0]['status'],'failed')
        self.assertEqual(calls[0]['exit_code'],1);self.assertIsNone(calls[0]['denial_rule'])


class NativeToolPlanBinding(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='zcode-native-plan-');self.addCleanup(self.temp.cleanup)
        self.project=Path(self.temp.name).resolve();self.run=self.project/'.model-desk/runs/native-plan';self.run.mkdir(parents=True)
        self.state={'id':'native-plan','project':str(self.project),'kind':'edit','files':['a.py'],
                    'zcode_access_mode':'project'}
        self.sid='modeldesk-zcode-native-plan'

    def test_tool_plan_change_rejects_prepared_job(self):
        file=native.prepare(self.run,self.state,[self.project/'profile'],self.sid)
        job=native.load(file)
        self.assertEqual(job['revision'],native.revision())
        self.assertEqual(job['guard_revision'],hashlib.sha256(
            native.GUARD.read_bytes()+native.SHARED.read_bytes()+native.POLICY.read_bytes()+
            native.GUARD.with_name('endpoint-access.cjs').read_bytes()).hexdigest())
        self.assertNotEqual(job['guard_revision'],job['revision'])
        for attr,value in (('TOOLS',native.TOOLS+('FutureTool',)),('READS',native.READS+('FutureRead',)),
                           ('EVENTS',native.EVENTS+('FutureEvent',))):
            with self.subTest(binding=attr):
                before=native.revision()
                with patch.object(native,attr,value):
                    self.assertNotEqual(native.revision(),before)
                    with self.assertRaisesRegex(ValueError,'未自动升级'):native.load(file)
                self.assertEqual(native.load(file)['revision'],before)

        before=native.revision()
        with patch.object(task_workspace,'TASK_TEMP_DIR',task_workspace.TASK_TEMP_DIR+'-elsewhere'):
            self.assertNotEqual(native.revision(),before)
            with self.assertRaisesRegex(ValueError,'未自动升级'):native.load(file)


class NativeController(unittest.TestCase):
    def setUp(self):
        self.fixture=integration.ZCodeTests('test_native_identity_and_default_preserved')
        self.fixture.setUp();self.addCleanup(self.fixture.doCleanups);self.addCleanup(self.fixture.tearDown)
        self.enterContext(patch.object(native,'revision',return_value='fixture-native-revision'))

    def test_new_dispatch_snapshots_native_and_existing_parent_stays_legacy(self):
        row,_=self.fixture.dispatch(legacy=False)
        self.assertEqual(row['zcode_tool_mode'],native.MODE)
        self.assertEqual(row['zcode_tool_revision'],'fixture-native-revision')
        self.assertIs(worker.tool_adapter({}),worker.zcode_tools)
        self.assertIs(worker.tool_adapter(row),native)

    def execute(self,job,on_event):
        self.assertNotIn('tools_script',job);self.assertNotIn('python',job)
        self.assertEqual(job['native_tools'],list(native.TOOLS))
        data=native.load(job['tool_job']);run=Path(job['tool_job']).parent
        proof={key:data[key] for key in ('task_id','project','nonce','revision','mode','access_mode')}
        if 'record_project' in data:proof['record_project']=data['record_project']
        proof.update(source='zcode-native-hooks',job_hash=hashlib.sha256(Path(job['tool_job']).read_bytes()).hexdigest(),
                     session_id=job['session_id'],probe='ok')
        registry.write_json(run/'zcode-native-proof.json',proof)
        (Path(job['project'])/'a.py').write_text('native fixture',encoding='utf-8')
        on_event({'type':'session_creating'});on_event({'type':'connected'});on_event({'type':'prompt_attempted'});on_event({'type':'closed'})
        return [{'type':'result','terminal_event':'turn.completed','value':{'settings':{'model':job['selection']},
                'messages':[{'info':{'role':'assistant'},'parts':[{'type':'text','text':'done'}]}]}}]

    def test_controller_passes_native_setup_and_requires_bound_proof(self):
        row,run=self.fixture.dispatch(legacy=False)
        self.assertEqual(worker.run_task(run,row,registry.get_model(row['model_id']),execute=self.execute),0)
        result=registry.read_task(run)
        self.assertEqual(result['sandbox_mode'],'native-hooks-tool-argument-guard')
        self.assertTrue(result['record_protection']);self.assertEqual(result['changed_files'],['a.py'])

    def test_isolated_native_controller_maps_absolute_files_and_preserves_original(self):
        project=self.fixture.project
        git(project,'init','-b','main');git(project,'config','user.name','fixture')
        git(project,'config','user.email','fixture@example.com');git(project,'add','a.py','b.py')
        git(project,'commit','-m','baseline')
        absolute=str(project/'a.py');before=(project/'a.py').read_bytes()
        row,run=self.fixture.dispatch(legacy=False,files=[absolute])
        plan=panel.prepare_workspace_for_run(run,row)
        row=registry.read_task(run)
        self.assertEqual(worker.run_task(run,row,registry.get_model(row['model_id']),execute=self.execute),0)
        result=registry.read_task(run)
        self.assertEqual(result['project'],str(project))
        self.assertEqual(result['changed_files'],[absolute])
        self.assertEqual((project/'a.py').read_bytes(),before)
        self.assertEqual((Path(plan['path'])/'a.py').read_text(),'native fixture')

    def test_isolated_edit_task_declares_the_scratch_temp_directory(self):
        project=self.fixture.project
        git(project,'init','-b','main');git(project,'config','user.name','fixture')
        git(project,'config','user.email','fixture@example.com');git(project,'add','a.py','b.py')
        git(project,'commit','-m','baseline')
        row,run=self.fixture.dispatch(legacy=False)
        panel.prepare_workspace_for_run(run,row)
        row=registry.read_task(run)
        seen={}
        def execute(job,on_event):
            data=native.load(job['tool_job']);seen.update(job=job,data=data,copy=Path(data['project']))
            (Path(data['project'])/task_workspace.TASK_TEMP_DIR/'scratch.log').write_text('fixture\n',encoding='utf-8')
            return self.execute(job,on_event)
        self.assertEqual(worker.run_task(run,row,registry.get_model(row['model_id']),execute=execute),0)
        self.assertEqual(seen['data']['temp_dir'],task_workspace.TASK_TEMP_DIR)
        self.assertTrue((seen['copy']/task_workspace.TASK_TEMP_DIR).is_dir())
        self.assertIn('任务临时目录：'+task_workspace.TASK_TEMP_DIR,seen['job']['prompt'])
        self.assertIn('长期维护的回归测试属于业务成果',seen['job']['prompt'])
        # Scratch material is not a deliverable: only the declared file counts.
        result=registry.read_task(run)
        self.assertEqual(result['changed_files'],['a.py'])
        self.assertFalse(any(task_workspace.TASK_TEMP_DIR in name for name in result['changed_files']))

    def test_non_isolated_native_task_declares_no_temp_directory(self):
        row,run=self.fixture.dispatch(legacy=False)
        seen={}
        def execute(job,on_event):
            seen.update(job=job,data=native.load(job['tool_job']))
            return self.execute(job,on_event)
        self.assertEqual(worker.run_task(run,row,registry.get_model(row['model_id']),execute=execute),0)
        self.assertNotIn('temp_dir',seen['data'])
        self.assertNotIn('任务临时目录',seen['job']['prompt'])

    def test_changed_tool_plan_blocks_queued_task_before_model(self):
        row,run=self.fixture.dispatch(legacy=False)
        with patch.object(native,'revision',return_value='changed-native-revision'):
            self.assertEqual(worker.run_task(run,row,registry.get_model(row['model_id']),
                execute=lambda job,on_event:[{'type':'error','message':'must not run'}]),1)
        result=registry.read_task(run)
        self.assertEqual(result['status'],'failed')
        self.assertIn('保护版本已变化',result['error'])

    def test_false_native_completion_without_hooks_fails(self):
        row,run=self.fixture.dispatch(legacy=False)
        def execute(job,on_event):
            on_event({'type':'closed'})
            return [{'type':'result','value':{'settings':{'model':job['selection']},
                'messages':[{'info':{'role':'assistant'},'parts':[{'type':'text','text':'done'}]}]}}]
        self.assertEqual(worker.run_task(run,row,registry.get_model(row['model_id']),execute=execute),1)
        self.assertEqual(registry.read_task(run)['status'],'failed')

    def test_native_selftest_needs_matching_post_exit_evidence(self):
        for code in (0,1):
            with self.subTest(exit_code=code):
                command='echo NATIVE_FIXTURE'
                row,run=self.fixture.dispatch(legacy=False,test_command=command)
                def execute(job,on_event):
                    result=self.execute(job,on_event);data=native.load(job['tool_job'])
                    event={'event':'PostToolUse','session_id':job['session_id'],'nonce':data['nonce'],
                           'tool':'Bash','call_id':'fixture-command','input_hash':'fixture',
                           'allowed':True,'ok':code==0,'exit_code':code,
                           'command_hash':hashlib.sha256(command.encode()).hexdigest()}
                    (Path(job['tool_job']).parent/'zcode-native-events.jsonl').write_text(json.dumps(event)+'\n',encoding='utf-8')
                    return result
                self.assertEqual(worker.run_task(run,row,registry.get_model(row['model_id']),execute=execute),0 if code==0 else 1)
                result=registry.read_task(run)
                self.assertEqual(result['test_result']['verified'],code==0)

    def test_selftest_diagnostic_opt_in_job_and_delivered_prompt(self):
        command='node check.cjs'
        row,run=self.fixture.dispatch(legacy=False,test_command=command)
        seen={}
        def execute(job,on_event):
            data=native.load(job['tool_job'])
            event={'event':'PostToolUse','session_id':job['session_id'],'nonce':data['nonce'],
                   'tool':'Bash','call_id':'c1','input_hash':'fixture','allowed':True,'ok':True,'exit_code':0,
                   'command_hash':hashlib.sha256(command.encode()).hexdigest()}
            (Path(job['tool_job']).parent/'zcode-native-events.jsonl').write_text(json.dumps(event)+'\n',encoding='utf-8')
            seen.update(job=job,data=data)
            return self.execute(job,on_event)
        self.assertEqual(worker.run_task(run,row,registry.get_model(row['model_id']),execute=execute),0)
        self.assertIs(seen['data']['selftest_diagnostic'],True)
        final=(run/'prompt-final.txt').read_text(encoding='utf-8')
        self.assertIn('必须原样执行的自测命令：'+command,final)
        # Without a declared test command nothing changes.
        row,run=self.fixture.dispatch(legacy=False)
        seen.clear()
        def execute_without(job,on_event):
            seen.update(data=native.load(job['tool_job']))
            return self.execute(job,on_event)
        self.assertEqual(worker.run_task(run,row,registry.get_model(row['model_id']),execute=execute_without),0)
        self.assertNotIn('selftest_diagnostic',seen['data'])
        self.assertFalse((run/'prompt-final.txt').exists())

    def test_selftest_diagnostic_field_tamper_is_rejected(self):
        row,run=self.fixture.dispatch(legacy=False,test_command='node check.cjs')
        captured={}
        def execute(job,on_event):
            captured['file']=Path(job['tool_job'])
            return self.execute(job,on_event)
        worker.run_task(run,row,registry.get_model(row['model_id']),execute=execute)
        file=captured['file'];data=json.loads(file.read_text(encoding='utf-8'))
        index=self.fixture.project/'.model-desk/zcode-native-sessions.json'
        rows=registry.read_json(index)
        for bad in ('yes',0,1,None,[],{}):
            with self.subTest(bad=bad):
                data['selftest_diagnostic']=bad
                file.write_text(json.dumps(data,ensure_ascii=False),encoding='utf-8')
                rows['sessions'][data['native_session_id']]['job_hash']=hashlib.sha256(file.read_bytes()).hexdigest()
                registry.write_json(index,rows)
                with self.assertRaisesRegex(ValueError,'身份或工具版本变化'):native.load(file)

    def test_selftest_miss_is_unconfirmed_without_falsifying_native_completion(self):
        command='node check.cjs'
        expected=hashlib.sha256(command.encode()).hexdigest()
        def diag_for(typed):
            groups=[g.strip() for g in typed.split('&&')]
            return {'len':len(typed),'trimmed':hashlib.sha256(typed.strip().encode()).hexdigest(),
                    'line_endings_normalized':hashlib.sha256(typed.replace('\r\n','\n').encode()).hexdigest(),
                    'groups':[hashlib.sha256(g.encode()).hexdigest() for g in groups],'group_count':len(groups)}
        def classified(typed):
            row,run=self.fixture.dispatch(legacy=False,test_command=command)
            def execute(job,on_event):
                data=native.load(job['tool_job'])
                event={'event':'PostToolUse','session_id':job['session_id'],'nonce':data['nonce'],
                       'tool':'Bash','call_id':'c1','input_hash':'fixture','allowed':True,'ok':True,'exit_code':0,
                       'command_hash':hashlib.sha256(typed.encode()).hexdigest(),'command_diag':diag_for(typed)}
                (Path(job['tool_job']).parent/'zcode-native-events.jsonl').write_text(json.dumps(event)+'\n',encoding='utf-8')
                return self.execute(job,on_event)
            self.assertEqual(worker.run_task(run,row,registry.get_model(row['model_id']),execute=execute),0)
            result=registry.read_task(run)
            self.assertFalse(result['test_result']['verified'])
            import execution_result
            failure=execution_result.project(result)['failure']
            self.assertEqual(failure['kind'],'verification_unconfirmed')
            self.assertIsNone(result['error'])
            self.assertIn('自测待核实',result['phase'])
            return result['test_result']['diag'],failure['message']
        # Compound wrap: the expected command appears verbatim as a group.
        diag,error=classified('cd somewhere && '+command)
        self.assertTrue(diag['group_match']);self.assertFalse(diag['trimmed_match'])
        self.assertIn('复合命令的分组',error)
        # Format difference: a trailing newline only fires the trimmed match;
        # the line-endings variant needs real CRLF inside the command.
        diag,error=classified(command+'\n')
        self.assertTrue(diag['trimmed_match']);self.assertFalse(diag['line_endings_match'])
        self.assertIn('尾部空白或换行符差异',error)
        # A genuinely different command: no normalized match at all.
        diag,error=classified('python other.py')
        self.assertFalse(diag['group_match']);self.assertFalse(diag['trimmed_match'])
        self.assertFalse(diag['native_result_hits'])
        self.assertIn('键入命令与指定命令不同',error)

    def test_selftest_native_result_hit_points_at_parameter_mapping(self):
        command='node check.cjs'
        row,run=self.fixture.dispatch(legacy=False,test_command=command)
        def execute(job,on_event):
            data=native.load(job['tool_job'])
            event={'event':'PostToolUse','session_id':job['session_id'],'nonce':data['nonce'],
                   'tool':'Bash','call_id':'c1','input_hash':'fixture','allowed':True,'ok':True,'exit_code':0,
                   'command_hash':hashlib.sha256('python other.py'.encode()).hexdigest(),
                   'command_diag':{'len':15,'trimmed':hashlib.sha256('python other.py'.encode()).hexdigest(),
                                   'line_endings_normalized':hashlib.sha256('python other.py'.encode()).hexdigest(),
                                   'groups':[hashlib.sha256('python other.py'.encode()).hexdigest()],'group_count':1}}
            (Path(job['tool_job']).parent/'zcode-native-events.jsonl').write_text(json.dumps(event)+'\n',encoding='utf-8')
            result=self.execute(job,on_event)
            result[0]['value']['messages'].append({'parts':[{'command':command}]})
            return result
        self.assertEqual(worker.run_task(run,row,registry.get_model(row['model_id']),execute=execute),0)
        result=registry.read_task(run)
        self.assertTrue(result['test_result']['diag']['native_result_hits'])
        import execution_result
        self.assertIn('原生会话结果含匹配命令而钩子未见',execution_result.project(result)['failure']['message'])

    def test_older_task_contract_still_requires_the_declared_exact_test(self):
        row,run=self.fixture.dispatch(legacy=False,test_command='node check.cjs')
        row['task_policy']={'schema':1,'write_scope':'workspace'}
        registry.write_json(run/'task.json',row)
        self.assertEqual(worker.run_task(run,row,registry.get_model(row['model_id']),execute=self.execute),1)
        result=registry.read_task(run)
        self.assertEqual(result['status'],'failed')
        self.assertFalse(result['test_result']['verified'])

    def test_selftest_summary_and_native_hashes_are_hash_only(self):
        self.assertEqual(native.selftest_diagnostic_summary([],'x'*40),{'bash_diag_events':0})
        event={'tool':'Bash','command_hash':'a'*64,
               'command_diag':{'len':3,'trimmed':'b'*64,'line_endings_normalized':'c'*64,
                               'groups':['d'*64],'group_count':1}}
        summary=native.selftest_diagnostic_summary([event],'d'*64)
        self.assertTrue(summary['group_match']);self.assertEqual(summary['typed_len'],3)
        self.assertFalse(summary['trimmed_match'])
        result=native.native_result_command_hashes(
            {'messages':[{'parts':[{'command':'node check.cjs'},{'other':1}]},{'command':'node check.cjs'}]})
        self.assertEqual(len(result),1)
        self.assertEqual(result[0]['hash'],hashlib.sha256('node check.cjs'.encode()).hexdigest())
        self.assertEqual(result[0]['len'],len('node check.cjs'))
        self.assertEqual(native.native_result_command_hashes({'messages':'no shape'}),[])


if __name__=='__main__':unittest.main()
