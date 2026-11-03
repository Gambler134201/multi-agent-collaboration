"""Offline native hooks, permissions and real mock-ACP subprocess tests."""
import io
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import workbuddy_native_tools as native
import workbuddy_executor as wb
import model_registry as registry
import task_workspace

NODE = os.environ.get('MODEL_DESK_TEST_NODE') or shutil.which('node')


@unittest.skipUnless(NODE, 'Node is required for native hook subprocess checks')
class NativeToolsTests(unittest.TestCase):
    def test_v3_hook_preserves_native_input_and_denial_boundary(self):
        self.run=self.root/'.model-desk/runs/direct-contract'
        contract={'schema':3,'write_scope':'workspace','home':str(self.root/'synthetic-home'),'deny_paths':[]}
        registry.write_json(self.run/'task.json',{'id':'direct-contract','kind':'edit','task_policy':contract})
        self.job=native.prepare(self.run,self.root,'direct-contract',['a.py'],[],'edit')
        self.hook('SessionStart')
        command="$d='.'; Get-ChildItem -Exclude '.git' $d | Where-Object { $_.Name -ne '.model-desk' }"
        _,result=self.hook('PreToolUse','PowerShell',{'command':command})
        self.assertNotEqual(result.get('hookSpecificOutput',{}).get('permissionDecision'),'deny')
        self.assertNotIn('updatedInput',result.get('hookSpecificOutput',{}))
        row=json.loads((self.run/'workbuddy-native-events.jsonl').read_text(encoding='utf-8').splitlines()[-1])
        self.assertEqual(row['command_hash'],hashlib.sha256(command.encode()).hexdigest())
        _,result=self.hook('PreToolUse','Bash',{'command':'cat .git/config'},call='denied')
        self.assertEqual(result['hookSpecificOutput']['permissionDecision'],'deny')
        _,result=self.hook('PreToolUse','Write',{'file_path':'a.py','content':'no retry'},call='after')
        self.assertEqual(result['hookSpecificOutput']['permissionDecision'],'deny')
        data=registry.read_json(self.job);data['task_policy']['schema']=2;registry.write_json(self.job,data)
        with self.assertRaisesRegex(ValueError,'写入范围与任务快照'):
            native.load(self.job)

    def test_declared_private_paths_use_native_hooks_and_cannot_be_removed(self):
        self.run=self.root/'.model-desk/runs/path-contract'
        private=self.root/'private-account'
        contract={'schema':2,'write_scope':'workspace','home':str(self.root/'synthetic-home'),
                  'deny_paths':[str(private)]}
        registry.write_json(self.run/'task.json',{'id':'path-contract','kind':'edit','task_policy':contract})
        self.job=native.prepare(self.run,self.root,'path-contract',[],[],'edit')
        self.hook('SessionStart')
        for name,args in [('Read',{'file_path':'tokens.json'}),('Bash',{'command':'node auth-token.test.js'})]:
            code,result=self.hook('PreToolUse',name,args)
            self.assertEqual(code,0)
            self.assertNotEqual(result.get('hookSpecificOutput',{}).get('permissionDecision'),'deny')
        code,result=self.hook('PreToolUse','Read',{'file_path':str(private/'auth.json')})
        self.assertEqual(code,0)
        self.assertEqual(result['hookSpecificOutput']['permissionDecision'],'deny')
        data=registry.read_json(self.job)
        data['task_policy']['deny_paths']=[]
        registry.write_json(self.job,data)
        with self.assertRaisesRegex(ValueError,'写入范围与任务快照'):
            native.load(self.job)

    def test_task_contract_allows_helper_and_cannot_be_removed_from_job(self):
        self.run=self.root/'.model-desk/runs/contract'
        contract={'schema':1,'write_scope':'workspace'}
        registry.write_json(self.run/'task.json',{'id':'contract','kind':'edit','task_policy':contract})
        self.job=native.prepare(self.run,self.root,'contract',['a.py'],[],'edit')
        self.hook('SessionStart')
        code,result=self.hook('PreToolUse','Write',{'file_path':'helper.py','content':'fixture'})
        self.assertEqual(code,0)
        self.assertNotEqual(result.get('hookSpecificOutput',{}).get('permissionDecision'),'deny')
        self.assertEqual(native.load(self.job)['task_policy'],contract)
        data=registry.read_json(self.job);data.pop('task_policy');registry.write_json(self.job,data)
        with self.assertRaisesRegex(ValueError,'写入范围与任务快照'):
            native.load(self.job)

    def test_literal_output_does_not_pause_or_rewrite_native_command(self):
        command = 'Write-Output "git commit; rm -rf .model-desk"'
        code, result = self.hook('PreToolUse', 'PowerShell', {'command': command})
        self.assertEqual(code, 0)
        self.assertNotEqual(result.get('hookSpecificOutput', {}).get('permissionDecision'), 'deny')
        self.assertNotIn('updatedInput', result.get('hookSpecificOutput', {}))
        self.hook('PostToolUse', 'PowerShell', {'command': command}, response={'rawResponse': {'exitCode': 0}})
        proof = native.verify(self.job, self.sid)
        self.assertFalse(proof['writes_paused'])
        self.assertTrue(proof['events'][-1]['ok'])
        self.assertEqual(proof['events'][-1]['command_hash'], hashlib.sha256(command.encode()).hexdigest())
        code, result = self.hook('PreToolUse', 'Write', {'file_path': 'a.py', 'content': 'fixture'})
        self.assertEqual(code, 0)
        self.assertNotEqual(result.get('hookSpecificOutput', {}).get('permissionDecision'), 'deny')

    def test_missing_search_path_is_a_native_error_not_a_policy_denial(self):
        code, result = self.hook('PreToolUse', 'Grep', {'path': 'missing/path', 'pattern': 'fixture'})
        self.assertEqual(code, 0)
        self.assertNotIn('permissionDecision', result.get('hookSpecificOutput', {}))
        self.hook('PostToolUseFailure', 'Grep', {'path': 'missing/path'}, response={'isError': True})
        (self.root/'a.py').write_text('fixture', encoding='utf-8')
        code, result = self.hook('PreToolUse', 'Grep', {'path': 'a.py', 'pattern': 'fixture'}, call='corrected')
        self.assertEqual(code, 0)
        self.assertNotIn('permissionDecision', result.get('hookSpecificOutput', {}))
        self.assertFalse(native.verify(self.job, self.sid)['writes_paused'])
        _, result = self.hook('PreToolUse', 'Read', {'file_path': '.model-desk/records.db'}, call='protected')
        self.assertEqual(result['hookSpecificOutput']['permissionDecision'], 'deny')
        _, result = self.hook('PreToolUse', 'Read', {'file_path': 'a.py'}, call='after-denial')
        self.assertEqual(result['hookSpecificOutput']['permissionDecision'], 'deny')
        self.assertEqual(native.verify(self.job, self.sid)['events'][-1]['denial_rule'], 'prior_denial')

    def test_native_nonzero_exit_is_a_tool_failure_without_sticky_denial(self):
        self.hook('PreToolUse', 'PowerShell', {'command': 'fixture test'})
        self.hook('PostToolUse', 'PowerShell', {'command': 'fixture test'}, response={'rawResponse': {'exitCode': 7}})
        proof = native.verify(self.job, self.sid)
        self.assertFalse(proof['events'][-1]['ok'])
        self.assertEqual(native.public_calls(proof)[-1]['status'], 'failed')
        self.assertFalse(proof['writes_paused'])
        code, result = self.hook('PreToolUse', 'PowerShell', {'command': 'echo corrected'}, call='corrected')
        self.assertEqual(code, 0)
        self.assertNotIn('permissionDecision', result.get('hookSpecificOutput', {}))

    def test_literal_source_search_keeps_native_bash_and_private_path_boundaries(self):
        (self.root/'a.py').write_text('source fixture\n',encoding='utf-8')
        code,result=self.hook('PreToolUse','Bash',{'command':"rg -n '.zcode/config' a.py"},call='query')
        self.assertEqual(code,0)
        self.assertNotEqual(result.get('hookSpecificOutput',{}).get('permissionDecision'),'deny')
        private=self.root/'.model-desk/records.db';private.write_text('private fixture')
        _,result=self.hook('PreToolUse','Bash',{'command':"rg -n '.zcode' .model-desk/records.db"},call='private-target')
        self.assertEqual(result['hookSpecificOutput']['permissionDecision'],'deny')

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.run = self.root / '.model-desk/runs/native-fixture'
        self.job = native.prepare(self.run, self.root, 'native-fixture', ['a.py'], [], 'edit')
        self.sid = 'fixture-parent-session'
        self.hook('SessionStart')

    def hook(self, event, tool=None, args=None, call='fixture-call', response=None, sid=None):
        value = {'hook_event_name': event, 'session_id': sid or self.sid, 'tool_name': tool,
                 'tool_input': args or {}, 'tool_use_id': call, 'cwd': str(self.root),
                 'tool_response': response}
        result = subprocess.run([NODE, str(native.GUARD), str(self.job)], input=json.dumps(value),
                                text=True, encoding='utf-8', capture_output=True)
        return result.returncode, json.loads(result.stdout)

    def test_new_profile_keeps_native_permissions_and_no_external_mcp(self):
        args = wb.command({'executable': NODE, 'cli': 'fixture'}, 'glm-test', native_job=self.job)
        self.assertIn('PowerShell', args[args.index('--tools') + 1])
        self.assertNotIn('Agent', args[args.index('--tools') + 1].split(','))
        self.assertIn('--strict-mcp-config', args)
        self.assertEqual(args[args.index('--setting-sources') + 1], '')
        self.assertEqual(args[args.index('--permission-mode') + 1], 'default')
        self.assertNotIn('--dangerously-skip-permissions', args)
        self.assertEqual(wb.command({'executable': NODE, 'cli': 'fixture'})[6], '')

    def new_access_job(self, name, *, access='full', kind='edit', protected_roots=(), readonly=()):
        self.run = self.root / '.model-desk/runs' / name
        self.job = native.prepare(self.run, self.root, name, ['a.py'] if kind == 'edit' else [],
                                  list(readonly), kind, access=access, protected_roots=protected_roots)
        self.hook('SessionStart')
        return self.job

    def test_access_setting_is_explicit_local_and_invalid_config_fails_closed(self):
        with patch.object(registry, 'HERE', self.root):
            path = self.root / native.ACCESS_CONFIG
            self.assertEqual(native.access_mode(), 'project')
            self.assertFalse(path.exists())
            native.set_access_mode('full')
            self.assertEqual(native.access_mode(), 'full')
            before = path.read_bytes()
            with self.assertRaises(ValueError):
                native.set_access_mode('ignore')
            self.assertEqual(path.read_bytes(), before)
            for value in [{'schema': 1, 'mode': 'unknown'}, {'schema': 1, 'mode': 'full', 'extra': True}, []]:
                path.write_text(json.dumps(value), encoding='utf-8')
                with self.assertRaises(ValueError):
                    native.access_mode()

    def test_full_access_uses_native_mode_and_keeps_hooks_and_empty_mcp(self):
        self.new_access_job('full-command')
        args = wb.command({'executable': NODE, 'cli': 'fixture'}, 'glm-test', native_job=self.job)
        self.assertEqual(args[args.index('--permission-mode') + 1], 'fullAccess')
        options = json.loads(args[args.index('--settings') + 1])
        self.assertIn('PreToolUse', options['hooks'])
        self.assertIn('--strict-mcp-config', args)
        self.assertEqual(args[args.index('--mcp-config') + 1], '{"mcpServers":{}}')
        self.assertNotIn('Agent', args[args.index('--tools') + 1].split(','))

    def test_full_access_allows_temporary_image_read_and_ordinary_file_write(self):
        outside = tempfile.TemporaryDirectory()
        self.addCleanup(outside.cleanup)
        self.new_access_job('full-temp')
        target = str(Path(outside.name) / 'screenshot.png')
        for tool, args in [('Read', {'file_path': target}),
                           ('Write', {'file_path': target, 'content': 'fixture'}),
                           ('Bash', {'command': 'echo fixture', 'cwd': outside.name})]:
            _, result = self.hook('PreToolUse', tool, args)
            self.assertNotEqual(result.get('hookSpecificOutput', {}).get('permissionDecision'), 'deny')
        self.assertFalse(native.verify(self.job, self.sid)['writes_paused'])

    def test_full_access_allows_specific_temporary_cleanup_without_executing_it(self):
        self.new_access_job('specific-cleanup')
        outside = self.root.parent / (self.root.name + '-ordinary')
        commands = ['rm -f "' + str(outside / 'pelican-shot.png') + '" "' + str(outside / 'pelican-test.py') + '"',
                    'Remove-Item -LiteralPath "' + str(outside / 'pelican-shot.png') + '" -Force']
        for command in commands:
            _, result = self.hook('PreToolUse', 'Bash', {'command': command})
            self.assertNotEqual(result.get('hookSpecificOutput', {}).get('permissionDecision'), 'deny')
        self.assertFalse(native.verify(self.job, self.sid)['writes_paused'])

    def test_literal_cleanup_then_readonly_listing_is_not_root_deletion(self):
        self.new_access_job('cleanup-readonly-tail')
        outside = str(self.root.parent / (self.root.name + '-ordinary'))
        command = 'rm -rf "' + outside + '"; ls "' + str(self.root) + '" | grep -c fixture || true'
        _, result = self.hook('PreToolUse', 'Bash', {'command': command})
        self.assertNotEqual(result.get('hookSpecificOutput', {}).get('permissionDecision'), 'deny')

    def test_literal_cd_cleanup_and_safe_cwd_do_not_pause_native_tools(self):
        self.new_access_job('literal-cd-cleanup')
        for command in ['cd "' + str(self.root) + '" && rm -f scratch-check.cjs',
                        'Set-Location -LiteralPath "' + str(self.root) + '"; Remove-Item -LiteralPath cache.png',
                        'echo copy .']:
            _, result = self.hook('PreToolUse', 'Bash', {'command': command})
            self.assertNotEqual(result.get('hookSpecificOutput', {}).get('permissionDecision'), 'deny')
        proof = native.verify(self.job, self.sid)
        self.assertFalse(proof['writes_paused'])
        self.assertTrue(proof['events'][0]['analysis_context']['leading_location'])
        self.assertNotIn(str(self.root), json.dumps(proof['events']))

    def test_cwd_alias_cannot_hide_record_ancestor(self):
        self.new_access_job('cwd-ancestor')
        _, result = self.hook('PreToolUse', 'Bash', {'command': 'rm -rf "' + self.root.name + '"', 'cwd': str(self.root.parent)})
        self.assertEqual(result['hookSpecificOutput']['permissionDecision'], 'deny')
        proof = native.verify(self.job, self.sid)
        self.assertEqual(native.denial_summary(proof)['rule'], 'record_ancestor')
        self.assertEqual(proof['events'][0]['analysis_context']['cwd_source'], 'cwd')

    def test_cd_target_cannot_enter_native_profile_even_with_failed_branch(self):
        profile = self.root.parent / (self.root.name + '-native-profile')
        self.new_access_job('cd-private-profile', protected_roots=[profile])
        _, result = self.hook('PreToolUse', 'Bash', {'command': 'cd "' + str(profile) + '" && cat storage.json'})
        self.assertEqual(result['hookSpecificOutput']['permissionDecision'], 'deny')
        self.assertEqual(native.denial_summary(native.verify(self.job, self.sid))['rule'], 'native_profile')

    def test_dynamic_cd_has_accurate_location_rule(self):
        self.new_access_job('dynamic-cd-rule')
        _, result = self.hook('PreToolUse', 'Bash', {'command': 'cd "$TARGET" && rm -f scratch.png'})
        self.assertEqual(result['hookSpecificOutput']['permissionDecision'], 'deny')
        self.assertEqual(native.denial_summary(native.verify(self.job, self.sid))['rule'], 'unverified_location')

    def test_variable_wildcard_recursive_cleanup_has_accurate_noncredential_rule(self):
        self.new_access_job('cleanup-variable-rule')
        command = 'rm -f "$TEMP"/fixture-*.png && rm -rf "$TEMP"/browser-profile-* 2>/dev/null; ls "$TEMP" | grep -c fixture || true'
        _, result = self.hook('PreToolUse', 'Bash', {'command': command})
        self.assertEqual(result['hookSpecificOutput']['permissionDecision'], 'deny')
        summary = native.denial_summary(native.verify(self.job, self.sid))
        self.assertEqual(summary['rule'], 'unverified_recursive_cleanup')
        self.assertNotEqual(summary['reason_code'], 'protected_data')
        self.assertNotIn('认证', summary['detail'])

    def test_denial_diagnostics_distinguish_profile_reference_and_record_ancestor(self):
        profile = self.root.parent / (self.root.name + '-profile')
        cases = [
            ('profile-rule', 'rm -f "' + str(profile / 'ordinary.png') + '"', 'native_profile'),
            ('record-reference-rule', 'rm -f .model-desk/records.db', 'shell_protected_reference'),
            ('ancestor-rule', 'Remove-Item -LiteralPath . -Recurse -Force', 'record_ancestor')]
        for name, command, expected in cases:
            self.new_access_job(name, protected_roots=[profile])
            _, result = self.hook('PreToolUse', 'Bash', {'command': command})
            self.assertEqual(result['hookSpecificOutput']['permissionDecision'], 'deny')
            proof = native.verify(self.job, self.sid)
            summary = native.denial_summary(proof)
            self.assertEqual(summary['rule'], expected)
            self.assertNotEqual(summary['detail'], native.DENIAL_RULES['unknown'])
            self.assertEqual(native.public_calls(proof)[-1]['denial_detail']['rule'], expected)
            events = (self.run / 'workbuddy-native-events.jsonl').read_text(encoding='utf-8')
            self.assertNotIn(command, events)
            self.assertNotIn(str(profile), events)
            self.assertTrue(proof['writes_paused'])

    def test_legacy_and_unknown_rule_remain_unknown_without_claiming_path(self):
        for rule in [None, 'untrusted-secret-value']:
            proof = {'events': [{'event': 'PreToolUse', 'allowed': False, 'tool': 'Bash',
                                 'reason_code': 'protected_data', 'denial_rule': rule}]}
            summary = native.denial_summary(proof)
            self.assertEqual(summary['rule'], 'unknown')
            self.assertIn('不能还原', summary['detail'])
            self.assertNotIn('untrusted-secret-value', json.dumps(summary))

    def test_full_access_requires_native_ack_before_any_prompt(self):
        self.new_access_job('full-ack')
        for reported, option in [('fullAccess', 'fullAccess'), ('default', 'fullAccess'),
                                 ('fullAccess', 'default'), (None, None)]:
            client = wb.NativeChatClient.__new__(wb.NativeChatClient)
            client.session_id = None
            client.native_job = self.job
            client.requested_model_id = 'glm-test'
            client.project = str(self.root)
            client.mcp_servers = []
            client.wait_native_tools = Mock()
            client.request = Mock(side_effect=[{'protocolVersion': 1},
                {'sessionId': self.sid, 'models': {'currentModelId': 'glm-test',
                    'availableModels': [{'modelId': 'glm-test'}]},
                 'modes': {'currentModeId': reported},
                 'configOptions': [{'id': 'mode', 'currentValue': option}]}])
            if reported == option == 'fullAccess':
                self.assertEqual(client.connect()['permission_mode'], 'fullAccess')
            else:
                with self.assertRaisesRegex(wb.ProtocolError, '未确认原生完全访问'):
                    client.connect()
            self.assertEqual([call.args[0] for call in client.request.call_args_list], ['initialize', 'session/new'])

    def test_full_access_keeps_records_auth_profile_readonly_and_pause_guards(self):
        outside = tempfile.TemporaryDirectory()
        self.addCleanup(outside.cleanup)
        foreign = Path(outside.name)
        profile = foreign / 'custom-profile'; profile.mkdir()
        readonly = str(self.root / 'context.txt')
        variants = [
            ('Read', {'file_path': str(foreign / '.model-desk/records.db')}),
            ('Read', {'file_path': str(foreign / '.codex/history.json')}),
            ('Read', {'file_path': str(foreign / '.env')}),
            ('Read', {'file_path': str(profile / 'storage.json')}),
            ('Bash', {'command': 'cat "' + str(profile / 'storage.json') + '"'}),
            ('Grep', {'path': str(foreign), 'pattern': 'fixture'}),
            ('Write', {'file_path': readonly, 'content': 'bad'})]
        for index, (tool, args) in enumerate(variants):
            self.new_access_job('full-protected-' + str(index), protected_roots=[str(profile)], readonly=[readonly])
            _, result = self.hook('PreToolUse', tool, args)
            self.assertEqual(result['hookSpecificOutput']['permissionDecision'], 'deny')
            proof = native.verify(self.job, self.sid)
            summary = native.denial_summary(proof)
            self.assertIn(summary['reason_code'], ('protected_data', 'readonly_file'))
            self.assertNotIn(str(foreign), json.dumps(proof))
            _, result = self.hook('PreToolUse', 'Bash', {'command': 'echo no retry'})
            self.assertEqual(result['hookSpecificOutput']['permissionDecision'], 'deny')
            self.assertEqual(native.verify(self.job, self.sid)['events'][-1]['reason_code'], 'prior_denial')
            self.assertEqual(native.denial_summary(native.verify(self.job, self.sid)), summary)

    def test_readonly_full_access_has_no_write_or_shell_tools(self):
        self.new_access_job('full-chat', kind='chat')
        self.assertNotIn('Write', native.load(self.job)['tools'])
        _, result = self.hook('PreToolUse', 'Bash', {'command': 'echo no'})
        self.assertEqual(result['hookSpecificOutput']['permissionDecision'], 'deny')

    def test_scope_snapshot_cannot_be_changed_after_hook_proof(self):
        data = json.loads(self.job.read_text(encoding='utf-8'))
        data['access_mode'] = 'full'
        self.job.write_text(json.dumps(data), encoding='utf-8')
        with self.assertRaises(ValueError):
            native.verify(self.job, self.sid)
        code, result = self.hook('PreToolUse', 'Read', {'file_path': 'a.py'})
        self.assertNotEqual(code, 0)
        self.assertEqual(result['hookSpecificOutput']['permissionDecision'], 'deny')

    def test_parent_session_and_code_revision_are_verified(self):
        self.assertEqual(native.verify(self.job, self.sid)['probe'], 'ok')
        with self.assertRaises(ValueError):
            native.verify(self.job, 'other-session')
        data = json.loads(self.job.read_text(encoding='utf-8'))
        data['revision'] = 'stale'
        self.job.write_text(json.dumps(data), encoding='utf-8')
        with self.assertRaises(ValueError):
            native.load(self.job)

    def test_write_is_scoped_and_read_is_project_bound(self):
        for tool, args in [('Read', {'file_path': 'a.py'}), ('Glob', {'pattern': '*.py'}),
                           ('Write', {'file_path': 'a.py', 'content': 'fixture'})]:
            code, result = self.hook('PreToolUse', tool, args)
            self.assertEqual(code, 0)
            self.assertNotEqual(result.get('hookSpecificOutput', {}).get('permissionDecision'), 'deny')
        self.hook('PreToolUse', 'Write', {'file_path': 'outside.py', 'content': 'bad'})
        self.assertTrue(native.verify(self.job, self.sid)['writes_paused'])
        _, result = self.hook('PreToolUse', 'PowerShell', {'command': 'echo retry'})
        self.assertEqual(result['hookSpecificOutput']['permissionDecision'], 'deny')

    def test_fresh_project_accepts_absolute_write_and_readonly_files(self):
        self.root = self.root / '全新项目 with spaces'
        self.root.mkdir()
        self.assertEqual(list(self.root.iterdir()), [])
        self.run = self.root / '.model-desk/runs/absolute-files'
        writable = str(self.root / 'new file.py')
        readonly = str(self.root / 'context.txt')
        (self.root / 'context.txt').write_text('fixture context', encoding='utf-8')
        self.job = native.prepare(self.run, self.root, 'absolute-files', [writable, 'relative.py'], [readonly], 'edit')
        data = native.load(self.job)
        self.assertEqual(data['files'], [writable, 'relative.py'])
        self.assertEqual(data['readonly_files'], [readonly])
        self.hook('SessionStart')
        for tool, args in [('Write', {'file_path': writable, 'content': 'fixture'}),
                           ('Write', {'file_path': str(self.root / 'relative.py'), 'content': 'fixture'}),
                           ('Read', {'file_path': readonly})]:
            _, result = self.hook('PreToolUse', tool, args)
            self.assertNotEqual(result.get('hookSpecificOutput', {}).get('permissionDecision'), 'deny')
        _, result = self.hook('PreToolUse', 'Write', {'file_path': str(self.root / 'undeclared.py'), 'content': 'bad'})
        self.assertEqual(result['hookSpecificOutput']['permissionDecision'], 'deny')

    def test_outside_absolute_relative_and_record_scopes_are_refused_before_job(self):
        outside = str(self.root.parent / (self.root.name + '-sibling') / 'a.py')
        variants = [outside, str(self.root.parent / 'foreign.py'), '../foreign.py',
                    str(self.root / '.model-desk/records.db')]
        if os.name == 'nt':
            other_drive = 'Z:' if self.root.drive.upper() != 'Z:' else 'Y:'
            variants.append(other_drive + r'\foreign.py')
        for index, name in enumerate(variants):
            for readonly in (False, True):
                task = 'invalid-%s-%s' % (index, readonly)
                run = self.root / '.model-desk/runs' / task
                with self.subTest(name=name, readonly=readonly), self.assertRaises(ValueError):
                    native.prepare(run, self.root, task, [] if readonly else [name], [name] if readonly else [], 'edit')
                self.assertFalse(run.exists())

    def test_absolute_and_relative_aliases_outside_project_are_refused(self):
        foreign = tempfile.TemporaryDirectory()
        self.addCleanup(foreign.cleanup)
        link = self.root / 'outside-alias'
        try:
            os.symlink(foreign.name, link, target_is_directory=True)
        except (OSError, NotImplementedError):
            if os.name != 'nt':
                self.skipTest('directory symlink unavailable')
            made = subprocess.run(['cmd', '/c', 'mklink', '/J', str(link), foreign.name],
                                  capture_output=True, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            if made.returncode:
                self.skipTest('directory symlink/junction unavailable')
        for index, name in enumerate((str(link / 'a.py'), 'outside-alias/a.py')):
            run = self.root / '.model-desk/runs' / ('alias-%s' % index)
            with self.assertRaisesRegex(ValueError, '文件范围越出当前项目'):
                native.prepare(run, self.root, run.name, [name], [], 'edit')
            self.assertFalse(run.exists())

    def test_records_auth_paths_nested_agents_and_sandbox_escape_are_denied(self):
        variants = [('Write', {'file_path': '.model-desk/task.json', 'content': 'bad'}),
                    ('Read', {'file_path': '.env'}), ('Read', {'file_path': '../foreign.py'}),
                    ('Agent', {'prompt': 'child'}), ('Skill', {'skill': 'unreviewed'}),
                    ('Artifact', {'path': 'a.py'}),
                    ('PowerShell', {'command': 'echo x', 'dangerouslyDisableSandbox': True}),
                    ('PowerShell', {'command': 'Remove-Item -Recurse .'}),
                    ('Bash', {'command': 'git push origin main'}),
                    ('WebFetch', {'url': 'http://127.0.0.1:8765/api/state'})]
        for tool, args in variants:
            with self.subTest(tool=tool, args=args):
                flag = self.run / 'workbuddy-native-denied.flag'
                if flag.exists():
                    flag.unlink()  # isolated fixtures only; never reset a live task
                _, result = self.hook('PreToolUse', tool, args)
                self.assertEqual(result['hookSpecificOutput']['permissionDecision'], 'deny')

    def test_readonly_task_never_gets_write_or_terminal_tools(self):
        run = self.root / '.model-desk/runs/readonly'
        job = native.prepare(run, self.root, 'readonly', [], [], 'chat')
        self.assertNotIn('PowerShell', native.load(job)['tools'])
        self.assertNotIn('Write', native.load(job)['tools'])

    def test_search_rewrites_exclusions_and_filters_record_metadata(self):
        _, result = self.hook('PreToolUse', 'Grep', {'pattern': 'fixture'})
        self.assertIn('.model-desk', result['hookSpecificOutput']['updatedInput']['glob'])
        _, result = self.hook('PostToolUse', 'Glob', {'pattern': '**/*'}, response={
            'content': [str(self.root / 'a.py'), str(self.job), str(self.root / '.env')]})
        self.assertEqual(result['hookSpecificOutput']['updatedToolOutput']['content'], [str(self.root / 'a.py')])

    def test_permission_matches_session_call_tool_and_exact_input(self):
        args = {'command': 'python -B -m unittest', 'description': '中文测试'}
        self.hook('PreToolUse', 'PowerShell', args)
        call = {'toolCallId': 'fixture-call', '_meta': {'codebuddy.ai/toolName': 'PowerShell'}, 'rawInput': args}
        self.assertTrue(native.permission(self.job, self.sid, call))
        for change in [{'toolCallId': 'foreign'}, {'rawInput': {'command': 'other'}},
                       {'_meta': {'codebuddy.ai/toolName': 'SandboxIntercept'}}]:
            self.assertFalse(native.permission(self.job, self.sid, {**call, **change}))

    def test_permission_approves_once_and_denial_does_not_retry(self):
        self.hook('PreToolUse', 'PowerShell', {'command': 'echo fixture'})
        client = object.__new__(wb.NativeChatClient)
        client.native_job, client.session_id, client.diagnostics, client.tainted = self.job, self.sid, [], False
        client.process = Mock(stdin=io.StringIO())
        request = {'id': 8, 'params': {'sessionId': self.sid,
            'options': [{'kind': 'allow_once', 'optionId': 'allow'}],
            'toolCall': {'toolCallId': 'fixture-call', '_meta': {'codebuddy.ai/toolName': 'PowerShell'},
                         'rawInput': {'command': 'echo fixture'}}}}
        client._native_permission(request)
        self.assertEqual(json.loads(client.process.stdin.getvalue())['result']['outcome']['optionId'], 'allow')
        request['params']['sessionId'] = 'foreign'
        with self.assertRaises(wb.ProtocolError):
            client._native_permission(request)
        self.assertTrue(client.tainted)

    def test_terminal_selftest_evidence_never_persists_output_or_credentials(self):
        args = {'command': 'python -B -m unittest'}
        self.hook('PreToolUse', 'PowerShell', args)
        self.hook('PostToolUse', 'PowerShell', args, response={
            'content': 'private output never copied', 'rawResponse': {'exitCode': 0}})
        proof = native.verify(self.job, self.sid)
        self.assertEqual(proof['events'][-1]['exit_code'], 0)
        self.assertTrue(proof['events'][-1]['ok'])
        self.assertNotIn('private output', json.dumps(proof))

    def test_glob_can_browse_own_artifact_directory_but_not_other_tasks(self):
        scope=self.root/'.local-worker/tasks/own-smoke'
        scope.mkdir(parents=True)
        names=[str((scope/n).relative_to(self.root)) for n in ['a.py','test_a.py']]
        self.job=native.prepare(self.root/'.model-desk/runs/own-dir',self.root,'own-dir',names,[],'edit')
        self.hook('SessionStart')
        _,result=self.hook('PreToolUse','Glob',{'path':str(scope),'pattern':'*'})
        self.assertNotEqual(result.get('hookSpecificOutput',{}).get('permissionDecision'),'deny')
        self.assertFalse(native.verify(self.job,self.sid)['writes_paused'])
        _,result=self.hook('PreToolUse','Glob',{'path':str(scope.parent/'other-smoke'),'pattern':'*'})
        self.assertEqual(result['hookSpecificOutput']['permissionDecision'],'deny')

    def test_mode_and_job_cannot_silently_upgrade_old_tasks(self):
        self.assertEqual(native.mode({}), native.LEGACY)
        with self.assertRaises(ValueError):
            native.mode({'workbuddy_tool_mode': 'unrestricted'})
        with self.assertRaises(FileExistsError):
            native.prepare(self.run, self.root, 'native-fixture', ['a.py'], [], 'edit')

    def test_protection_plan_is_pinned_inside_revision(self):
        file = native.prepare(self.root / '.model-desk/runs/plan-pin', self.root, 'plan-pin', ['a.py'], [], 'edit')
        job = native.load(file)
        self.assertEqual(job['revision'], native.revision())
        self.assertEqual(job['guard_revision'], native.guard_revision())
        self.assertNotEqual(job['guard_revision'], job['revision'])
        cases = [('TOOLS', native, native.TOOLS + ('FutureTool',)),
                 ('READS', native, native.READS + ('FutureRead',)),
                 ('ACCESS_MODES', native, native.ACCESS_MODES + ('write-all',)),
                 ('TASK_TEMP_DIR', task_workspace, task_workspace.TASK_TEMP_DIR + '-elsewhere')]
        for attr, module, value in cases:
            before = native.revision()
            with self.subTest(binding=attr), patch.object(module, attr, value):
                self.assertNotEqual(native.revision(), before)
                with self.assertRaisesRegex(ValueError, '身份或代码已变化'):
                    native.load(file)
            self.assertEqual(native.load(file)['revision'], before)

    def test_direct_project_tasks_declare_no_scratch_directory(self):
        # A task running directly in the user's own tree gets no declared temp
        # directory, and a forged declaration on such a job is refused.
        self.assertNotIn('temp_dir', native.load(self.job))
        data = json.loads(self.job.read_text(encoding='utf-8'))
        data['temp_dir'] = task_workspace.TASK_TEMP_DIR
        self.job.write_text(json.dumps(data), encoding='utf-8')
        with self.assertRaisesRegex(ValueError, '身份或代码已变化'):
            native.load(self.job)

    def test_guard_rejects_jobs_frozen_before_the_plan_revision(self):
        # Old-style jobs carry only the bare guard digest as revision: the real
        # guard refuses them before any session binding is written.
        run = self.root / '.model-desk/runs/pre-plan-job'
        self.job = native.prepare(run, self.root, 'pre-plan-job', ['a.py'], [], 'edit')
        data = json.loads(self.job.read_text(encoding='utf-8'))
        data['revision'] = data.pop('guard_revision')
        self.job.write_text(json.dumps(data), encoding='utf-8')
        code, result = self.hook('SessionStart')
        self.assertEqual(code, 2)
        self.assertEqual(result['hookSpecificOutput']['permissionDecision'], 'deny')
        self.assertFalse((run / 'workbuddy-native-proof.json').exists())

    def test_real_mock_acp_wire_runs_hooks_permission_and_shell_exit(self):
        # Python stands in for ACP only. The hooks are the real Node guard;
        # the local command is harmless and no native host/model is contacted.
        self.job = native.prepare(self.root / '.model-desk/runs/wire', self.root, 'wire', ['a.py'], [], 'edit')
        script = self.root / 'wire.py'
        script.write_text(r'''
import json,sys,shlex,subprocess
sys.stdin.reconfigure(encoding='utf-8');sys.stdout.reconfigure(encoding='utf-8')
settings=json.loads(sys.argv[sys.argv.index('--settings')+1])
node=__NODE__
sid='wire-native-parent'
def hook(event,**kw):
 command=settings['hooks'][event][0]['hooks'][0]['command']
 args=shlex.split(command);args[0]=node
 result=subprocess.run(args,input=json.dumps({'hook_event_name':event,'session_id':sid,**kw}),capture_output=True,text=True,encoding='utf-8')
 assert result.returncode==0,result.stderr
 return json.loads(result.stdout)
def send(v):print(json.dumps(v),flush=True)
for line in sys.stdin:
 q=json.loads(line);method=q.get('method')
 if method=='initialize':r={'protocolVersion':1}
 elif method=='session/new':
  hook('SessionStart')
  r={'sessionId':sid,'models':{'currentModelId':'glm-test','availableModels':[{'modelId':'glm-test'}]}}
 elif method=='session/set_config_option':r={'configOptions':[{'id':'model','currentValue':'glm-test'}]}
 elif method=='session/prompt':
  args={'command':'python -B -m unittest'}
  hook('PreToolUse',tool_name='PowerShell',tool_input=args,tool_use_id='wire-shell')
  send({'jsonrpc':'2.0','id':'approval','method':'session/request_permission','params':{
   'sessionId':sid,'options':[{'kind':'allow_once','optionId':'allow'}],
   'toolCall':{'toolCallId':'wire-shell','rawInput':args,'_meta':{'codebuddy.ai/toolName':'PowerShell'}}}})
  approval=json.loads(sys.stdin.readline());assert approval['result']['outcome']['optionId']=='allow'
  result=subprocess.run([sys.executable,'-B','-c','print("fixture-command-ok")'],capture_output=True)
  hook('PostToolUse',tool_name='PowerShell',tool_input=args,tool_use_id='wire-shell',tool_response={'rawResponse':{'exitCode':result.returncode}})
  send({'jsonrpc':'2.0','method':'session/update','params':{'sessionId':sid,'update':{
   'sessionUpdate':'agent_message_chunk','content':{'type':'text','text':'fixture done'}}}})
  r={'stopReason':'end_turn'}
 else:raise RuntimeError('unexpected wire method')
 send({'jsonrpc':'2.0','id':q['id'],'result':r})
'''.replace('__NODE__', repr(str(NODE))), encoding='utf-8')
        client = wb.NativeChatClient({'executable': sys.executable, 'cli': str(script)}, self.root, self.root,
                                     isolated=True, model_id='glm-test', native_job=self.job)
        try:
            client.connect()
            result = client.chat('glm-test', 'offline fixture', {'paid_allowed': True, 'model_scope': 'all',
                'enforce_paid_budget': False, 'enforce_model_call_limit': False})
            self.assertEqual(result['status'], 'completed')
            self.assertEqual(result['native_tool_proof']['events'][-1]['exit_code'], 0)
            self.assertEqual(len(native.public_calls(result['native_tool_proof'])), 1)
            self.assertEqual(native.public_calls(result['native_tool_proof'])[0]['status'], 'completed')
            self.assertFalse(result['accepted'])
        finally:
            client.close()


class ScratchWorkerTests(unittest.TestCase):
    def test_isolated_worker_explains_scratch_and_counts_only_business_files(self):
        import test_workbuddy_integration as integration
        import panel_server as panel
        import workbuddy_worker as worker
        from test_task_workspace import git
        fixture=integration.IntegrationTests('test_successful_native_worker_records_real_files_without_auto_acceptance')
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.addCleanup(fixture.tearDown)
        git(fixture.project,'init','-b','main')
        git(fixture.project,'config','user.name','fixture')
        git(fixture.project,'config','user.email','fixture@example.com')
        git(fixture.project,'add','a.py','b.py')
        git(fixture.project,'commit','-m','baseline')
        original=(fixture.project/'a.py').read_bytes()
        state,run=fixture.dispatch()
        plan=panel.prepare_workspace_for_run(run,state)
        state=registry.read_task(run)
        parent=fixture.client()
        class Client(parent):
            def chat(self,*args,**kwargs):
                result=super().chat(*args,**kwargs)
                data=native.load(self.native_job)
                self.scratch=self.project/data['temp_dir']/'check.cjs'
                self.scratch.write_text('fixture script\n',encoding='utf-8')
                return result
        self.assertEqual(worker.run_task(run,state,registry.get_model(state['model_id']),client_factory=Client),0)
        self.assertIn('任务临时目录：'+task_workspace.TASK_TEMP_DIR,fixture.last_prompt)
        self.assertIn('长期维护的回归测试属于业务成果',fixture.last_prompt)
        self.assertTrue(fixture.native.scratch.is_file())
        result=registry.read_task(run)
        self.assertEqual(result['changed_files'],['a.py'])
        self.assertEqual(result['workbuddy']['cleanup'],'closed')
        self.assertEqual((fixture.project/'a.py').read_bytes(),original)
        self.assertEqual(fixture.prompts,1)
        self.assertTrue(fixture.native.scratch.is_relative_to(Path(plan['path'])))


if __name__ == '__main__':
    unittest.main()
