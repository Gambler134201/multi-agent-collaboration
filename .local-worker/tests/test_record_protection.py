"""Offline tests for the workbench-record write protection.

Covers the Python half of the boundary (content identity, patch row, dispatch
scope refusal, runtime proof verification) without ever touching a real
workbench record: every project, run and model registry lives in a temporary
directory.  The real-DSH end-to-end test lives in
``tests/test_record_protection_runtime.cjs``.
"""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import context_manager
import dsh_worker
import model_registry as registry
import panel_server as panel
import preset_runtime as presets
import record_protection as protection

FIXTURE = ROOT / 'tests' / 'dsh-fixture'


def sha256_of(paths):
    digest = hashlib.sha256()
    for path in paths:
        digest.update(Path(path).read_bytes())
    return digest.hexdigest()


class RevisionTests(unittest.TestCase):
    def test_revision_is_the_content_identity_of_both_runtime_files(self):
        self.assertEqual(protection.revision(),
                         sha256_of([protection.POLICY_FILE, protection.PLUGIN_FILE]))
        self.assertRegex(protection.revision(), r'^[0-9a-f]{64}$')

    def test_revision_changes_when_the_runtime_code_changes(self):
        with tempfile.TemporaryDirectory() as temp:
            policy = Path(temp) / 'record-policy.cjs'
            plugin = Path(temp) / 'record-protection.cjs'
            policy.write_bytes(protection.POLICY_FILE.read_bytes())
            plugin.write_bytes(protection.PLUGIN_FILE.read_bytes())
            before = protection.revision()
            with patch.object(protection, 'POLICY_FILE', policy), patch.object(protection, 'PLUGIN_FILE', plugin):
                self.assertEqual(protection.revision(), before)
                policy.write_bytes(policy.read_bytes() + b'\n// changed\n')
                self.assertNotEqual(protection.revision(), before)


class PathPolicyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.project = Path(self.temp.name) / 'proj'
        (self.project / '.model-desk' / 'runs' / 'task-1').mkdir(parents=True)
        (self.project / '.model-desk' / 'records.db').write_bytes(b'real-bytes')
        (self.project / 'src').mkdir()
        (self.project / 'src' / 'ok.py').write_text('print(1)\n', encoding='utf-8')
        (self.project / '.model-desk-backup').mkdir()

    def tearDown(self):
        self.temp.cleanup()

    def test_record_paths_are_detected_in_every_spelling(self):
        for name in ['.model-desk/records.db',
                     '.model-desk\\runs\\task-1\\task.json',
                     '.MODEL-DESK/records.db',
                     '.model-desk/../.model-desk/records.db',
                     str(self.project / '.model-desk' / 'records.db')]:
            candidate = Path(name)
            if not candidate.is_absolute():
                candidate = self.project / name
            self.assertTrue(protection.is_record_path(candidate, self.project), name)

    def test_similar_directory_names_and_sources_are_not_record_paths(self):
        for name in ['.model-desk-backup/records.db', '.model-desktop/notes.txt',
                     '.model-desk-notes.md', 'src/ok.py']:
            self.assertFalse(protection.is_record_path(self.project / name, self.project), name)

    def test_any_model_desk_directory_is_a_workbench_record_directory(self):
        # A reserved workbench name is protected wherever it appears; the test
        # project name does not matter.
        self.assertTrue(protection.is_record_path(self.project / '.model-desk' / 'records.db',
                                                  self.project / 'other'))

    def test_alias_into_the_record_directory_is_detected(self):
        link = self.project / 'alias-records'
        target = self.project / '.model-desk'
        try:
            os.symlink(target, link, target_is_directory=True)
        except (OSError, NotImplementedError):
            # A directory junction needs no elevation on Windows.
            made = subprocess.run(['cmd', '/c', 'mklink', '/J', str(link), str(target)],
                                  capture_output=True, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            if made.returncode != 0:
                self.skipTest('symlink/junction unavailable: ' + made.stderr.decode('utf-8', 'replace'))
        self.assertTrue(protection.is_record_path(link / 'records.db', self.project))
        self.assertFalse(protection.is_record_path(self.project / 'src' / 'ok.py', self.project))

    def test_validate_files_refuses_only_real_record_targets(self):
        with self.assertRaisesRegex(ValueError, '由工作台管理'):
            protection.validate_files(['src/ok.py', '.model-desk/records.db'], self.project)
        self.assertEqual(protection.validate_files(['.model-desk-backup/records.db'], self.project),
                         ['.model-desk-backup/records.db'])
        self.assertEqual(protection.record_files(['src/ok.py'], self.project), [])


class PatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.saved = registry.HERE, registry.REGISTRY, registry.RUNS
        registry.HERE = Path(self.temp.name)
        registry.REGISTRY = registry.HERE / 'models.json'
        registry.RUNS = registry.HERE / 'runs'
        registry.write_json(registry.HERE / 'settings.json', {'model': 'qwen-test', 'api_base': 'http://127.0.0.1:1/v1',
            'api_key_env': '', 'max_input_tokens': 32768, 'max_output_tokens': 4096})
        registry.list_models()
        self.project = registry.HERE

    def tearDown(self):
        registry.HERE, registry.REGISTRY, registry.RUNS = self.saved
        self.temp.cleanup()

    def test_patch_mounts_the_guard_with_its_content_identity(self):
        model = registry.get_model('home')
        preset = {'id': 'standard', 'revision': 'r', 'source': 's', 'overrides': [], 'policy_overrides': [],
                  'declaration': {'id': 'agent-preset', 'config': {'plugins': []}}}
        rows = dsh_worker.build_patch(model, 'edit', preset, record_root=self.project)
        # The guard travels as an insert row next to the preset declaration.
        insert = next(r for r in rows if 'insert' in r)['insert']
        row = next(entry for entry in insert if entry.get('id') == 'model-desk-record-protection')
        self.assertEqual(row['name'], str(protection.PLUGIN_FILE))
        self.assertEqual(row['config']['root'], str(self.project.resolve()))
        self.assertEqual(row['config']['revision'], protection.revision())

    def test_default_build_patch_keeps_its_previous_shape(self):
        model = registry.get_model('home')
        preset = {'id': 'standard', 'revision': 'r', 'source': 's', 'overrides': [], 'policy_overrides': [],
                  'declaration': {'id': 'agent-preset', 'config': {'plugins': []}}}
        rows = dsh_worker.build_patch(model, 'chat', preset)
        self.assertNotIn('model-desk-record-protection', [r.get('id') for r in rows])

    @unittest.skipUnless((dsh_worker.EXECUTABLE.parent / 'resources/app.asar').is_file(),
                         'requires a configured DSH installation')
    def test_preset_identity_and_revision_are_untouched(self):
        native = presets.load({'provider': 'local'}, dsh_worker.EXECUTABLE)
        standard = presets.load({'provider': 'deepseek'}, dsh_worker.EXECUTABLE)
        for preset in (native, standard):
            before = json.dumps(preset, sort_keys=True)
            rows = presets.patch_rows(preset, record_root=self.project)
            self.assertEqual(json.dumps(preset, sort_keys=True), before)
            self.assertEqual(preset['id'], preset['declaration']['config']['id'])
            insert = next(r for r in rows if 'insert' in r)['insert']
            self.assertEqual(insert[0]['id'], 'agent-preset-registry')

    def test_edit_fingerprint_tracks_the_guard_content(self):
        model = registry.get_model('home')
        preset = {'id': 'native', 'revision': 'a'}
        with patch.object(protection, 'revision', return_value='guard-one'):
            first = dsh_worker.fingerprint(model, 'edit', self.project, preset)
        with patch.object(protection, 'revision', return_value='guard-two'):
            second = dsh_worker.fingerprint(model, 'edit', self.project, preset)
            chat = dsh_worker.fingerprint(model, 'chat', self.project, preset)
        self.assertNotEqual(first, second)
        self.assertEqual(chat, dsh_worker.fingerprint(model, 'chat', self.project, preset))


class DispatchScopeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.saved = registry.HERE, registry.REGISTRY, registry.RUNS
        registry.HERE = Path(self.temp.name)
        registry.REGISTRY = registry.HERE / 'models.json'
        registry.RUNS = registry.HERE / 'runs'
        registry.write_json(registry.HERE / 'settings.json', {'model': 'qwen-test', 'api_base': 'http://127.0.0.1:1/v1',
            'api_key_env': '', 'max_input_tokens': 32768, 'max_output_tokens': 4096})
        registry.list_models()
        self.project = registry.HERE
        (self.project / '.model-desk').mkdir(exist_ok=True)

    def tearDown(self):
        registry.HERE, registry.REGISTRY, registry.RUNS = self.saved
        self.temp.cleanup()

    def test_panel_refuses_an_edit_scope_that_names_the_records(self):
        body = {'model_id': 'home', 'kind': 'edit', 'engine': 'dsh', 'project': str(self.project),
                'files': ['sample.py'], 'prompt': 'test'}
        with patch.object(panel.EXECUTOR, 'submit'), \
                patch.object(panel.execution_engines, 'availability', return_value={'ready': True}):
            with self.assertRaisesRegex(ValueError, '由工作台管理'):
                panel.submit_task({**body, 'files': ['.model-desk/records.db']})
            with self.assertRaisesRegex(ValueError, '由工作台管理'):
                panel.submit_task({**body, 'files': ['sample.py', '.model-desk/runs/x/task.json']})
            task = panel.submit_task(body)
            self.assertEqual(task['files'], ['sample.py'])

    def test_chat_read_only_context_is_not_a_writable_scope(self):
        # Reads of the records are allowed; only the writable scope is refused.
        context = self.project / 'notes.py'
        context.write_text('x\n', encoding='utf-8')
        with patch.object(panel.EXECUTOR, 'submit'), \
                patch.object(panel.execution_engines, 'availability', return_value={'ready': True}):
            task = panel.submit_task({'model_id': 'home', 'kind': 'chat', 'engine': 'dsh',
                                      'project': str(self.project), 'files': ['notes.py'], 'prompt': 'read'})
        self.assertEqual(task['files'], ['notes.py'])

    def test_stage_creation_refuses_a_record_file_in_an_edit_range(self):
        with self.assertRaisesRegex(ValueError, '由工作台管理'):
            panel.create_stage({'goal': 'g', 'title': 't', 'objective': 'o', 'project': str(self.project),
                                'kind': 'edit', 'files': ['.model-desk/records.db']})

    def test_stage_resolves_record_aliases_from_its_goal_when_project_is_omitted(self):
        goal = panel.stage_store.create_goal('test goal', 'offline goal', str(self.project))
        link = self.project / 'alias-store'
        try:
            os.symlink(self.project / '.model-desk', link, target_is_directory=True)
        except OSError:
            made = subprocess.run(['cmd','/c','mklink','/J',str(link),str(self.project / '.model-desk')],capture_output=True)
            if made.returncode:
                self.skipTest('directory alias unavailable')
        with self.assertRaisesRegex(ValueError, '由工作台管理'):
            panel.create_stage({'goal_id':goal['id'],'title':'alias stage','objective':'offline',
                                'kind':'edit','files':['alias-store/records.db'],'acceptance':['check']})

    def test_worker_refuses_a_record_file_before_spawning_anything(self):
        run = registry.HERE / 'runs' / 'record-scope'
        run.mkdir(parents=True)
        model = registry.get_model('home')
        state = {'id': 'record-scope', 'kind': 'edit', 'project': str(self.project),
                 'files': ['.model-desk/records.db'], 'timeout': 0}
        with patch.object(dsh_worker, 'availability', return_value={'ready': True}):
            with self.assertRaisesRegex(ValueError, '由工作台管理'):
                dsh_worker._run_task(run, state, model, object())
        self.assertFalse((run / 'dsh-job.json').exists())


class PromptTests(unittest.TestCase):
    def test_agent_prompt_states_the_boundary_without_claiming_isolation(self):
        with tempfile.TemporaryDirectory() as temp:
            run = Path(temp)
            (run / 'prompt.txt').write_text('任务正文', encoding='utf-8')
            prompt, _ = dsh_worker.build_agent_prompt(run, {'kind': 'edit', 'files': []})
            self.assertIn('由工作台管理', prompt)
            self.assertIn('.model-desk', prompt)
            chat, _ = dsh_worker.build_agent_prompt(run, {'kind': 'chat', 'files': []})
            self.assertIn(context_manager.RECORD_PROTECTION_NOTE, chat)
            self.assertIn('不要换用其他工具', prompt)


class ProofTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.run = Path(self.temp.name) / 'run'
        self.run.mkdir()
        self.project = Path(self.temp.name) / 'proj'
        self.project.mkdir()
        self.row = {'schema': 1, 'source': protection.SOURCE, 'revision': protection.revision(),
                    'root': str(self.project.resolve()), 'scope': 'global', 'probe': 'ok',
                    'pid': 1, 'at': '2026-01-01T00:00:00Z', 'session_id': 'parent-session'}

    def tearDown(self):
        self.temp.cleanup()

    def write(self, **changes):
        registry.write_json(self.run / protection.PROOF_NAME, {**self.row, **changes})

    def test_parent_proof_is_accepted_and_bound_to_the_project(self):
        self.write()
        proof = protection.verify_proof(self.run, self.project, 'parent-session')
        self.assertEqual(proof['matched'], 'parent')

    def test_unknown_parent_cannot_be_authenticated_by_the_proof_itself(self):
        self.write()
        with self.assertRaisesRegex(ValueError, '缺少可信的父会话身份'):
            protection.verify_proof(self.run, self.project)

    def test_missing_or_stale_proof_never_passes(self):
        with self.assertRaisesRegex(ValueError, '没有返回'):
            protection.verify_proof(self.run, self.project, 'parent-session')
        self.write(probe='no')
        with self.assertRaisesRegex(ValueError, '全局工具层'):
            protection.verify_proof(self.run, self.project, 'parent-session')
        self.write(scope='agent')
        with self.assertRaisesRegex(ValueError, '全局工具层'):
            protection.verify_proof(self.run, self.project, 'parent-session')
        self.write(revision='older-guard')
        with self.assertRaisesRegex(ValueError, '内容与当前安装不一致'):
            protection.verify_proof(self.run, self.project, 'parent-session')

    def test_child_session_or_other_project_is_never_a_parent_proof(self):
        self.write(session_id='child-session')
        with self.assertRaisesRegex(ValueError, '子会话'):
            protection.verify_proof(self.run, self.project, 'parent-session')
        self.write(session_id='parent-session', root=str((self.project / 'other').resolve()))
        with self.assertRaisesRegex(ValueError, '工作目录与任务项目不一致'):
            protection.verify_proof(self.run, self.project, 'parent-session')
        self.write(session_id=None, root=str(self.project.resolve()))
        with self.assertRaisesRegex(ValueError, '缺少写入会话身份'):
            protection.verify_proof(self.run, self.project, 'parent-session')


if __name__ == '__main__':
    unittest.main(verbosity=2)
