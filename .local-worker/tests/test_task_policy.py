"""Task-level authority is distinct from deliverables. Offline only."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import endpoint_access as access
import dsh_worker
import pi_worker
import panel_server as panel
import model_registry as registry
import execution_engines as engines
import workflow_settings
from test_stage_workflow import Base


class TaskContract(unittest.TestCase):
    def test_autonomous_prompt_allows_helpers_but_strict_prompt_never_promises_extra_writes(self):
        state={'kind':'edit','task_policy':access.policy_for_dispatch('edit')}
        self.assertIn('交付说明及辅助文件',access.scope_prompt(state,['main.py']))
        self.assertIn('不因文件未列在预期清单内而禁止操作',access.scope_prompt(state,['main.py']))
        strict={'kind':'edit','task_policy':access.policy_for_dispatch('edit','declared_files')}
        prompt=access.scratch_prompt(strict,'.modeldesk-task-tmp')
        self.assertNotIn('不需要逐个声明',prompt)
        self.assertIn('交付说明直接在最终回复中返回',prompt)
        self.assertEqual(strict['task_policy']['write_scope'],'declared_files')

    def test_durable_regression_tests_are_not_forced_into_excluded_scratch(self):
        state={'kind':'edit','task_policy':access.policy_for_dispatch('edit')}
        prompt=access.scratch_prompt(state,'.modeldesk-task-tmp')
        self.assertIn('长期维护的回归测试属于业务成果',prompt)
        self.assertIn('用户指定的交付路径优先',prompt)
        self.assertNotIn('测试脚本、运行产物与临时材料放这里',prompt)
        self.assertNotIn('长期维护的回归测试',access.scratch_prompt({'kind':'edit'},'.modeldesk-task-tmp'))
        self.assertEqual(access.scratch_prompt(state,None),'')

    def test_path_declarations_are_frozen_and_public_view_hides_locations(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(access.Path,'home',return_value=Path(folder)), patch.object(registry,'HERE',Path(folder)/'runtime'):
            value=access.policy_for_dispatch('edit')
            self.assertEqual(value['schema'],3)
            self.assertIn(str(Path(folder)/'.codex/auth.json'),value['deny_paths'])
            self.assertNotIn(str(Path(folder)/'.codex'),value['deny_paths'])
            self.assertIn(str(Path(folder)/'runtime/models.json'),value['deny_paths'])
        state={'kind':'edit','task_policy':value}
        self.assertEqual(access.policy_for_dispatch('edit',parent=state),value)
        self.assertEqual(access.policy_view(state),{'state':'valid','schema':3,'write_scope':'workspace'})
        old={'kind':'edit','task_policy':{'schema':1,'write_scope':'workspace'}}
        self.assertEqual(access.policy_for_dispatch('edit',parent=old),old['task_policy'])
        v2={**value,'schema':2}
        self.assertEqual(access.policy_for_dispatch('edit',parent={'kind':'edit','task_policy':v2}),v2)

    def test_custom_codex_home_does_not_deny_source_worktree_parent(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(access.os.environ,{'CODEX_HOME':folder}):
            value=access.policy_for_dispatch('edit')
            self.assertIn(str(Path(folder)/'auth.json'),value['deny_paths'])
            self.assertIn(str(Path(folder)/'sessions'),value['deny_paths'])
            self.assertNotIn(folder,value['deny_paths'])
            self.assertNotIn(str(Path(folder)/'worktrees'),value['deny_paths'])

    def test_bad_protected_path_declarations_refuse(self):
        value=access.policy_for_dispatch('edit')
        for bad in [{**value,'home':'relative'},{**value,'deny_paths':['relative']},
                    {**value,'deny_paths':None},{**value,'deny_paths':[value['home']]*129},
                    {**value,'extra':True}]:
            with self.subTest(bad=bad),self.assertRaises(ValueError):
                access.task_policy({'kind':'edit','task_policy':bad})

    def test_new_default_explicit_and_readonly_are_separate(self):
        self.assertEqual(access.policy_for_dispatch('edit')['write_scope'], 'workspace')
        self.assertEqual(access.policy_for_dispatch('edit','declared_files')['write_scope'], 'declared_files')
        self.assertEqual(access.policy_for_dispatch('chat')['write_scope'], 'read_only')
        self.assertEqual(access.policy_for_dispatch('edit',stage_limited=True)['write_scope'],'declared_files')
        for kind,scope in [('chat','workspace'),('edit','full'),('edit',False),('edit',{})]:
            with self.subTest(kind=kind,scope=scope), self.assertRaises(ValueError):
                access.policy_for_dispatch(kind,scope)
        with self.assertRaises(ValueError):
            access.policy_for_dispatch('edit','workspace',stage_limited=True)

    def test_legacy_parent_never_gets_new_authority(self):
        self.assertIsNone(access.policy_for_dispatch('edit',parent={'kind':'edit'}))
        old={'kind':'edit','task_policy':access.policy_for_dispatch('edit','declared_files')}
        self.assertEqual(access.policy_for_dispatch('edit',parent=old),old['task_policy'])
        with self.assertRaises(ValueError):access.policy_for_dispatch('edit','workspace',parent=old)
        with self.assertRaises(ValueError):access.policy_for_dispatch('edit','workspace',parent={})
        with self.assertRaises(ValueError):
            access.policy_for_dispatch('edit',parent={'kind':'edit','task_policy':access.policy_for_dispatch('edit')},stage_limited=True)

    def test_malformed_snapshots_never_degrade_to_legacy(self):
        for value in [None,{},[],True,{'schema':True,'write_scope':'workspace'},
                      {'schema':2,'write_scope':'workspace'},{'schema':1,'write_scope':'full'},
                      {'schema':1,'write_scope':'read_only'},{'schema':1,'write_scope':'workspace','extra':True}]:
            with self.subTest(value=value),self.assertRaises(ValueError):
                access.task_policy({'kind':'edit','task_policy':value})

    def test_common_prompt_preserves_legacy_and_declared_readonly(self):
        self.assertIsNone(access.scope_prompt({'kind':'edit'},['a.py']))
        state={'kind':'edit','task_policy':access.policy_for_dispatch('edit')}
        text=access.scope_prompt(state,['a.py'],['a.py'])
        self.assertIn('不是逐文件写入白名单',text)
        self.assertIn('即使同时出现在交付清单',text)
        with tempfile.TemporaryDirectory() as folder:
            run=Path(folder);(run/'prompt.txt').write_text('TASK',encoding='utf-8')
            prompt,_=dsh_worker.build_agent_prompt(run,{**state,'files':['a.py'],'readonly_files':['a.py']})
            self.assertIn('不是逐文件写入白名单',prompt)
            self.assertNotIn('只修改这些文件',prompt)


class DispatchContract(Base):
    def setUp(self):
        super().setUp()
        workflow_settings.save_settings({'enabled':True,'paid_allowed':True,'model_scope':'all',
                                         'enforce_paid_budget':False,'enforce_model_call_limit':False})
        self.enterContext(patch.object(engines,'availability',return_value={'ready':True}))
        self.enterContext(patch.object(panel,'connectivity_gate',return_value={
            'blocked':False,'checked':False,'message':'','verdict':'fixture','check':None}))
        self.enterContext(patch.object(panel.EXECUTOR,'submit'))

    def submit(self,**fields):
        return panel.submit_task({'engine':'dsh','kind':'edit','model_id':'home','project':str(self.project),
                                  'files':['a.py'],'prompt':'fixture task','source':'codex',
                                  'routing':{'acceptance':['fixture']},**fields})

    def test_new_dispatch_freezes_default_without_changing_access_settings(self):
        before=access.read()
        task=self.submit(readonly_files=['b.py'])
        self.assertEqual(task['task_policy'],access.policy_for_dispatch('edit'))
        self.assertEqual(task['files'],['a.py'])
        self.assertEqual(task['readonly_files'],['b.py'])
        self.assertFalse(task['files_readonly'])
        self.assertEqual(access.read(),before)

    def test_explicit_scope_and_controller_field_rejection(self):
        task=self.submit(write_scope='declared_files')
        self.assertEqual(task['task_policy']['write_scope'],'declared_files')
        with self.assertRaisesRegex(ValueError,'控制器生成'):
            self.submit(task_policy={'schema':1,'write_scope':'workspace'})

    def test_deliverable_names_can_be_omitted_but_explicit_scope_cannot(self):
        self.assertEqual(self.submit(files=[])['task_policy']['write_scope'],'workspace')
        with self.assertRaisesRegex(ValueError,'至少需要一个文件'):
            self.submit(files=[],write_scope='declared_files')

    def test_explicit_stage_scope_stays_restricted(self):
        goal,stage=self.goal_with_stage()
        task=self.submit(stage_id=stage['id'],goal_id=goal['id'])
        self.assertEqual(task['task_policy']['write_scope'],'declared_files')

    def test_pi_fingerprint_distinguishes_same_access_different_scope(self):
        model=registry.get_model('home')
        one={'kind':'edit','project':str(self.project),'files':['a.py'],
             'task_policy':access.policy_for_dispatch('edit')}
        other={**one,'task_policy':access.policy_for_dispatch('edit','declared_files')}
        self.assertNotEqual(pi_worker.fingerprint(model,one,self.project),pi_worker.fingerprint(model,other,self.project))
        self.assertNotEqual(dsh_worker.fingerprint(model,'edit',self.project,task=one),
                            dsh_worker.fingerprint(model,'edit',self.project,task=other))

    def test_frozen_acceptance_binding_includes_the_task_policy(self):
        import delegation_policy
        one=self.submit()
        old=delegation_policy.result_binding(one)
        changed={**one,'task_policy':access.policy_for_dispatch('edit','declared_files')}
        self.assertNotEqual(old,delegation_policy.result_binding(changed))

    def test_actual_git_delivery_includes_an_undeclared_helper(self):
        import task_workspace
        from test_task_dispatch import make_repo
        repo=make_repo(Path(self.temp.name)/'repo')
        task=self.submit(project=str(repo))
        run=registry.task_dir(task['id'])
        plan=panel.prepare_workspace_for_run(run,task)
        execution=Path(plan['path'])
        (execution/'a.py').write_text('print(2)\n',encoding='utf-8')
        (execution/'helper.py').write_text('assert True\n',encoding='utf-8')
        registry.patch_task(run,status='completed')
        commit=panel.commit_workspace_for_run(run,registry.read_task(run))
        saved=registry.read_task(run)
        self.assertTrue(commit['committed'])
        self.assertEqual(saved['changed_files'],['a.py','helper.py'])
        self.assertEqual(set(saved['file_hashes']),{'a.py','helper.py'})
        self.assertEqual(saved['artifact_manifest']['unverified_files'],[])
        self.assertEqual(saved['files'],['a.py'])
        self.assertFalse((repo/'helper.py').exists())

    def test_same_project_nonisolated_edits_keep_exclusive_occupancy(self):
        import execution_resources
        task=self.submit()
        rows=execution_resources.requests_for(task,registry.get_model(task['model_id']),workflow_settings.snapshot())
        self.assertEqual([r['mode'] for r in rows if r['resource'].startswith('project:')],['exclusive'])
