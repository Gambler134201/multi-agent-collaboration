"""Native hooks bind real task worktrees to original records; no model calls."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import model_registry as registry
import task_workspace as workspace
import workbuddy_native_tools as wb
import zcode_native_tools as zcode
import dsh_worker
import pi_worker
from test_task_workspace import make_repo

NODE = shutil.which('node')


class NativeWorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='native-workspace-')
        self.addCleanup(self.temp.cleanup)
        self.project = make_repo(Path(self.temp.name) / 'project')
        self.task_id = 'native-isolated'
        self.plan = {**workspace.prepare(self.project, self.task_id), 'created': True, 'eligible': True}
        self.execution = Path(self.plan['path'])
        self.run = self.project / '.model-desk/runs' / self.task_id
        self.run.mkdir(parents=True)
        self.state = {'id': self.task_id, 'project': str(self.project), 'kind': 'edit',
                      'files': [str(self.project / 'a.txt')], 'readonly_files': ['shared.txt'],
                      'workspace': self.plan, 'zcode_access_mode': 'project'}
        registry.write_json(self.run / 'task.json', self.state)
        self.sid = 'modeldesk-zcode-isolated-fixture'

    def hook(self, module, job, tool='Read', target=None, event='PreToolUse', response=None):
        if NODE is None:
            self.skipTest('Node is required for native hook checks')
        value = {'hook_event_name': event, 'session_id': self.sid, 'tool_name': tool,
                 'tool_input': {'file_path': str(target or self.execution / 'a.txt')},
                 'tool_use_id': 'fixture-call', 'tool_response': response}
        argument = str(self.project) if module is zcode else str(job)
        result = subprocess.run([NODE, str(module.GUARD), argument], input=json.dumps(value),
                                capture_output=True, text=True, encoding='utf-8')
        return result.returncode, json.loads(result.stdout)

    def prepare_wb(self, access='project'):
        self.state.update(workbuddy_tool_mode=wb.MODE, workbuddy_tool_revision=wb.revision())
        registry.write_json(self.run/'task.json',self.state)
        return wb.prepare(self.run, self.execution, self.task_id, workspace.execution_files(self.state),
                          workspace.execution_files(self.state, 'readonly_files'), 'edit',
                          record_project=self.project, access=access)

    def test_original_absolute_paths_map_into_task_copy(self):
        self.assertEqual(workspace.execution_files(self.state), ['a.txt'])
        self.assertEqual(workspace.execution_files(self.state, 'readonly_files'), ['shared.txt'])
        self.assertEqual(self.state['files'], [str(self.project / 'a.txt')])
        self.assertEqual(workspace.native_roots(self.run, self.state), (self.project, self.execution))

    def test_dsh_and_pi_prepare_keep_record_identity_and_map_execution_targets(self):
        self.state.update(timeout=0, route={'mode': 'manual'})
        model={'id':'fixture','name':'fixture','model':'fixture','provider':'local',
               'api_base':'http://127.0.0.1:1/v1','max_input_tokens':32768,'max_output_tokens':1024}
        relay=SimpleNamespace(token='fixture-not-used',url='http://127.0.0.1:1/v1')
        preset={'id':'native','revision':'fixture','source':'fixture','policy_overrides':[]}
        (self.run/'prompt.txt').write_text('fixture')
        real_popen=subprocess.Popen
        def spawn(command,*args,**kwargs):
            return real_popen(command,*args,**kwargs) if command[0]=='git' else Mock(pid=424242)
        for module,prepare in ((dsh_worker,dsh_worker._prepare_dsh),(pi_worker,pi_worker.prepare)):
            rows=[{}, {}, {}, {'config':{}}, {'insert':[{'id':'model-desk-record-protection','config':{}}]}]
            with self.subTest(engine=module.__name__), patch.object(module,'availability',return_value={'ready':True,'version':'fixture'}), \
                    patch.object(module.subprocess,'Popen',side_effect=spawn), \
                    patch.object(dsh_worker,'HERE',Path(self.temp.name)/'controller'), \
                    patch.object(dsh_worker.preset_runtime,'load',return_value=preset), \
                    patch.object(dsh_worker,'subagent_decision',return_value={'decision':'deny'}), \
                    patch.object(dsh_worker,'build_patch',return_value=rows), \
                    patch.object(dsh_worker,'build_agent_prompt',return_value=('fixture','fixture')), \
                    patch.object(module.context_manager,'ensure_index',side_effect=ValueError('no fixture index')) as index:
                result=prepare(self.run,self.state,model,relay)
            self.assertEqual(index.call_args.args[:2],(self.execution,['a.txt']))
            self.assertEqual(result['project'],self.execution)
            self.assertEqual(set(result['before']),{'a.txt'})
            self.assertEqual(registry.read_task(self.run)['project'],str(self.project))
            if module is dsh_worker:
                self.assertEqual(rows[-1]['insert'][0]['config']['record_project'],str(self.project))
                self.assertEqual(rows[-1]['insert'][0]['config']['context_root'],str(self.run/'context'))
            else:
                self.assertEqual(registry.read_json(self.run/'pi-job.json')['record_project'],str(self.project))
                self.assertEqual(registry.read_json(self.run/'pi-job.json')['context_root'],str(self.run/'context'))

    def test_outside_file_and_forged_branch_are_rejected(self):
        self.state['files'] = [str(self.project.parent / 'outside.txt')]
        with self.assertRaisesRegex(ValueError, '越出原项目'):
            workspace.execution_files(self.state)
        self.state['workspace'] = {**self.plan, 'branch': 'modeldesk-task/other'}
        with self.assertRaisesRegex(ValueError, '身份不匹配'):
            workspace.native_roots(self.run, self.state)

    def test_missing_execution_copy_never_falls_back_to_original(self):
        workspace.release(self.project, self.task_id)
        with self.assertRaisesRegex(ValueError, '工作区缺失'):
            workspace.native_roots(self.run, self.state)

    def test_workbuddy_job_and_proof_keep_original_record_identity(self):
        job = self.prepare_wb()
        data = wb.load(job)
        self.assertEqual(data['project'], str(self.execution))
        self.assertEqual(data['record_project'], str(self.project))
        self.assertEqual(data['files'], ['a.txt'])
        self.assertEqual(self.hook(wb, job, event='SessionStart'), (0, {}))
        code, result = self.hook(wb, job)
        self.assertEqual(code, 0)
        self.assertNotEqual((result.get('hookSpecificOutput') or {}).get('permissionDecision'), 'deny')
        proof = wb.verify(job, self.sid)
        self.assertEqual(proof['record_project'], str(self.project))
        self.assertFalse((self.execution / '.model-desk').exists())

    def test_workbuddy_full_access_still_protects_original_tree(self):
        job = self.prepare_wb(access='full')
        self.hook(wb, job, event='SessionStart')
        code, result = self.hook(wb, job, tool='Write', target=self.project / 'a.txt')
        self.assertEqual(code, 0)
        self.assertEqual(result['hookSpecificOutput']['permissionDecision'], 'deny')
        self.assertEqual((self.project / 'a.txt').read_text(), 'one\n')

    def test_workbuddy_isolated_edit_declares_the_task_temp_directory(self):
        job = self.prepare_wb()
        data = wb.load(job)
        self.assertEqual(data['temp_dir'], workspace.TASK_TEMP_DIR)
        self.assertTrue((self.execution / workspace.TASK_TEMP_DIR).is_dir())
        self.hook(wb, job, event='SessionStart')
        scratch = self.execution / workspace.TASK_TEMP_DIR / 'check.cjs'
        code, result = self.hook(wb, job, tool='Write', target=scratch)
        self.assertEqual(code, 0)
        self.assertNotEqual((result.get('hookSpecificOutput') or {}).get('permissionDecision'), 'deny')
        self.assertFalse(wb.verify(job, self.sid)['writes_paused'])
        # The deliverable declaration rules stay intact outside the directory.
        code, result = self.hook(wb, job, tool='Write', target=self.execution / 'undeclared.py')
        self.assertEqual(result['hookSpecificOutput']['permissionDecision'], 'deny')
        self.assertEqual(result['hookSpecificOutput']['permissionDecisionReason'], '文件未列入本任务可写范围')
        self.assertTrue(wb.verify(job, self.sid)['writes_paused'])

    def test_workbuddy_temp_declaration_is_pinned_in_both_directions(self):
        job = self.prepare_wb()
        data = json.loads(job.read_text(encoding='utf-8'))
        del data['temp_dir']
        job.write_text(json.dumps(data), encoding='utf-8')
        with self.assertRaisesRegex(ValueError, '身份或代码已变化'):
            wb.load(job)
        # The guard declares nothing without the exact field, so the same path
        # stays an undeclared write.
        self.hook(wb, job, event='SessionStart')
        code, result = self.hook(wb, job, tool='Write',
                                 target=self.execution / workspace.TASK_TEMP_DIR / 'late.txt')
        self.assertEqual(result['hookSpecificOutput']['permissionDecision'], 'deny')

    def test_workbuddy_forged_temp_value_declares_nothing_and_is_refused(self):
        job = self.prepare_wb()
        data = json.loads(job.read_text(encoding='utf-8'))
        data['temp_dir'] = '../outside'
        job.write_text(json.dumps(data), encoding='utf-8')
        with self.assertRaisesRegex(ValueError, '身份或代码已变化'):
            wb.load(job)

    def acceptance_task(self):
        registry.patch_task(self.run, engine='workbuddy', workbuddy_session_id=self.sid,
                            workbuddy_tool_mode=wb.MODE, workbuddy_tool_revision=wb.revision())
        return registry.read_task(self.run)

    def test_workbuddy_acceptance_ignores_only_bound_untracked_scratch(self):
        job = self.prepare_wb()
        self.hook(wb, job, event='SessionStart')
        task = self.acceptance_task()
        scratch = self.execution / workspace.TASK_TEMP_DIR
        (scratch / 'check.cjs').write_text('fixture scratch\n', encoding='utf-8')
        self.assertEqual(workspace.acceptance_dirty_rows(self.run, task), [])
        # Business dirt stays fatal even with a bound declaration.
        (self.execution / 'a.txt').write_text('changed\n', encoding='utf-8')
        self.assertEqual(workspace.acceptance_dirty_rows(self.run, task), [' M a.txt'])
        (self.execution / 'a.txt').write_text('one\n', encoding='utf-8')
        # Staged scratch material is an index edit, never a retention candidate.
        subprocess.run(['git', 'add', '--', workspace.TASK_TEMP_DIR + '/check.cjs'],
                       cwd=self.execution, capture_output=True)
        self.assertIn('A  ' + workspace.TASK_TEMP_DIR + '/check.cjs',
                      workspace.acceptance_dirty_rows(self.run, task))
        subprocess.run(['git', 'reset', '-q', '--', workspace.TASK_TEMP_DIR],
                       cwd=self.execution, capture_output=True)
        # Missing proof, wrong session, tampered job and drifted engines all
        # keep the scratch rows in the fatal set.
        (self.run / 'workbuddy-native-proof.json').unlink()
        self.assertEqual(len(workspace.acceptance_dirty_rows(self.run, task)), 1)
        self.hook(wb, job, event='SessionStart')
        registry.patch_task(self.run, workbuddy_session_id='modeldesk-wb-foreign')
        self.assertEqual(len(workspace.acceptance_dirty_rows(self.run, registry.read_task(self.run))), 1)
        registry.patch_task(self.run, workbuddy_session_id=self.sid)
        data = json.loads(job.read_text(encoding='utf-8'))
        del data['temp_dir']
        job.write_text(json.dumps(data), encoding='utf-8')
        self.assertEqual(len(workspace.acceptance_dirty_rows(self.run, task)), 1)
        job.write_text(json.dumps({**data, 'temp_dir': workspace.TASK_TEMP_DIR}), encoding='utf-8')
        self.hook(wb, job, event='SessionStart')
        for engine in ('zcode', 'dsh'):
            registry.patch_task(self.run, engine=engine)
            self.assertEqual(len(workspace.acceptance_dirty_rows(self.run, registry.read_task(self.run))), 1)
        registry.patch_task(self.run, engine='workbuddy')
        self.assertEqual(workspace.acceptance_dirty_rows(self.run, registry.read_task(self.run)), [])

    def test_workbuddy_scratch_alias_stays_fatal_in_acceptance(self):
        job = self.prepare_wb()
        self.hook(wb, job, event='SessionStart')
        task = self.acceptance_task()
        scratch = self.execution / workspace.TASK_TEMP_DIR
        (scratch / 'check.cjs').write_text('fixture scratch\n', encoding='utf-8')
        outside = self.execution.parent / 'outside-wb-scratch'
        outside.mkdir(exist_ok=True)
        alias = scratch / 'alias'
        try:
            os.symlink(str(outside), str(alias), target_is_directory=True)
        except (OSError, NotImplementedError):
            made = subprocess.run(['cmd', '/c', 'mklink', '/J', str(alias), str(outside)], capture_output=True)
            self.assertEqual(made.returncode, 0)
        try:
            self.assertEqual(workspace.acceptance_dirty_rows(self.run, task),
                             ['?? ' + workspace.TASK_TEMP_DIR + '/'])
        finally:
            alias.unlink()

    def test_workbuddy_rejects_record_workspace_drift_in_python_and_node(self):
        job = self.prepare_wb()
        registry.patch_task(self.run, workspace={**self.plan, 'path': str(self.project)})
        with self.assertRaises(ValueError):
            wb.load(job)
        code, result = self.hook(wb, job, event='SessionStart')
        self.assertEqual(code, 2)
        self.assertEqual(result['hookSpecificOutput']['permissionDecision'], 'deny')

    def test_zcode_index_and_locks_stay_with_records_profile_uses_execution_copy(self):
        job = zcode.prepare(self.run, self.state, [], self.sid)
        data = zcode.load(job)
        self.assertEqual(data['project'], str(self.execution))
        self.assertEqual(data['record_project'], str(self.project))
        self.assertEqual(data['profile']['path'], str(self.execution / '.zcode/config.json'))
        self.assertEqual(data['profile']['args'][-1], str(self.project))
        self.assertTrue((self.project / '.model-desk/zcode-native-sessions.json').is_file())
        self.assertFalse((self.execution / '.model-desk').exists())
        self.assertEqual(self.hook(zcode, job, event='SessionStart'), (0, {}))
        self.assertEqual(self.hook(zcode, job), (0, {}))
        proof = zcode.verify(job, self.sid)
        self.assertEqual(proof['record_project'], str(self.project))

    def test_zcode_rejects_worktree_record_drift_before_native_tools(self):
        job = zcode.prepare(self.run, self.state, [], self.sid)
        registry.patch_task(self.run, workspace={**self.plan, 'branch': 'modeldesk-task/other'})
        with self.assertRaises(ValueError):
            zcode.load(job)
        code, result = self.hook(zcode, job, event='SessionStart')
        self.assertEqual(code, 2)
        self.assertEqual(result['hookSpecificOutput']['permissionDecision'], 'deny')

    def test_zcode_execution_root_junction_drift_is_refused_before_native_profile(self):
        # The record's workspace plan is unchanged, but the execution copy at
        # the planned path was swapped for a junction pointing elsewhere:
        # native_roots must refuse it before any native profile is installed.
        moved = self.execution.with_name('moved-zcode-copy')
        self.execution.rename(moved)
        try:
            import os
            try:
                os.symlink(str(moved), str(self.execution), target_is_directory=True)
            except OSError:
                # Windows junction creation does not require developer-mode symlinks.
                result = subprocess.run(['cmd', '/c', 'mklink', '/J', str(self.execution), str(moved)],
                                        capture_output=True)
                self.assertEqual(result.returncode, 0)
            registry.patch_task(self.run, workspace={**self.plan, 'path': str(self.execution)})
            state = registry.read_task(self.run)
            with self.assertRaisesRegex(ValueError, '任务工作区路径或分支身份不匹配'):
                workspace.native_roots(self.run, state)
            with self.assertRaisesRegex(ValueError, '任务工作区路径或分支身份不匹配'):
                zcode.prepare(self.run, state, [], self.sid)
            self.assertFalse((moved / '.zcode').exists())
            self.assertFalse((self.run / 'zcode-native-job.json').exists())
        finally:
            self.execution.rmdir() if self.execution.is_junction() else self.execution.unlink()

    def test_zcode_project_scope_rejects_original_tree(self):
        job = zcode.prepare(self.run, self.state, [], self.sid)
        self.hook(zcode, job, event='SessionStart')
        code, result = self.hook(zcode, job, target=self.project / 'a.txt')
        self.assertEqual(code, 0)
        self.assertEqual(result['hookSpecificOutput']['permissionDecision'], 'deny')

    def test_missing_copy_is_refused_by_node_even_after_session_binding(self):
        job = self.prepare_wb()
        self.hook(wb, job, event='SessionStart')
        workspace.release(self.project, self.task_id)
        code, result = self.hook(wb, job)
        self.assertEqual(code, 2)
        self.assertEqual(result['hookSpecificOutput']['permissionDecision'], 'deny')

    def test_node_rejects_actual_branch_drift_with_unchanged_task_record(self):
        job = self.prepare_wb()
        result = subprocess.run(['git', 'checkout', '--detach'], cwd=self.execution, capture_output=True)
        self.assertEqual(result.returncode, 0)
        code, response = self.hook(wb, job, event='SessionStart')
        self.assertEqual(code, 2)
        self.assertEqual(response['hookSpecificOutput']['permissionDecision'], 'deny')

    def test_worktree_junction_is_rejected_before_installing_native_profile(self):
        moved = self.execution.with_name('moved-copy')
        self.execution.rename(moved)
        try:
            import os
            os.symlink(str(moved), str(self.execution), target_is_directory=True)
        except OSError:
            # Windows junction creation does not require developer-mode symlinks.
            result = subprocess.run(['cmd', '/c', 'mklink', '/J', str(self.execution), str(moved)],
                                    capture_output=True)
            self.assertEqual(result.returncode, 0)
        try:
            with self.assertRaises(ValueError):
                zcode.prepare(self.run, self.state, [], self.sid)
            self.assertFalse((moved / '.zcode').exists())
        finally:
            self.execution.rmdir() if self.execution.is_junction() else self.execution.unlink()

    def test_full_native_guard_preserves_isolation_for_reads_writes_and_shell_paths(self):
        if NODE is None:
            self.skipTest('Node is required for native hook checks')
        job = self.prepare_wb(access='full')
        script = """const fs=require('fs'),guard=require(process.argv[1]);
const job=JSON.parse(fs.readFileSync(process.argv[2],'utf8'));
const inputs=JSON.parse(fs.readFileSync(0,'utf8'));
process.stdout.write(JSON.stringify(inputs.map(input=>guard.decide(job,input))));"""
        foreign = self.execution.parent / 'other-task/delivery.txt'
        cases = [
            {'tool_name': 'Read', 'tool_input': {'file_path': str(self.execution / 'a.txt')}},
            {'tool_name': 'Write', 'tool_input': {'file_path': str(self.execution / 'a.txt')}},
            {'tool_name': 'Read', 'tool_input': {'file_path': str(foreign)}},
            {'tool_name': 'Write', 'tool_input': {'file_path': str(foreign)}},
            {'tool_name': 'Bash', 'tool_input': {'command': 'cat ../other-task/delivery.txt'}},
            {'tool_name': 'Read', 'tool_input': {'file_path': str(Path(self.temp.name) / 'temporary.png')}},
        ]
        result = subprocess.run([NODE, '-e', script, str(wb.GUARD), str(job)], input=json.dumps(cases),
                                capture_output=True, text=True, encoding='utf-8')
        self.assertEqual(result.returncode, 0, result.stderr)
        reasons = json.loads(result.stdout)
        self.assertEqual(reasons[:2], [None, None])
        self.assertTrue(all('任务隔离' in reason for reason in reasons[2:5]), reasons)
        self.assertIsNone(reasons[-1])


if __name__ == '__main__':
    unittest.main()
