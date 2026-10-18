"""Ownership/link guards for brain tasks; legacy behaviour stays compatible."""
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import brain_context
import brain_gateway
import delegation_policy as policy
import model_registry as registry
import panel_server as panel


class BrainOwnershipTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.saved = registry.HERE, registry.REGISTRY, registry.RUNS
        registry.HERE = Path(self.tmp.name)
        registry.REGISTRY = registry.HERE / 'models.json'
        registry.RUNS = registry.HERE / 'runs'
        registry.write_json(registry.HERE / 'workflow.json', {'enabled': True})
        self.project = registry.HERE / 'project'
        self.project.mkdir()
        self.other = registry.HERE / 'other'
        self.other.mkdir()
        registry.write_json(registry.REGISTRY, {'default_model': 'mock', 'models': [{
            'id': 'mock', 'name': 'Mock', 'provider': 'local', 'model': 'mock',
            'enabled': True, 'max_input_tokens': 32768, 'max_output_tokens': 4096,
            'preferred_tasks': [], 'max_task_complexity': 'simple',
        }]})
        brain_gateway._CAPABILITIES.clear()
        self.submitted = []
        self.patches = [
            mock.patch.object(panel.dsh_worker, 'availability',
                              return_value={'ready': True, 'message': ''}),
            mock.patch.object(panel.EXECUTOR, 'submit',
                              side_effect=lambda fn, task_id: self.submitted.append(task_id)),
        ]
        for patch in self.patches:
            patch.start()

    def tearDown(self):
        for patch in reversed(self.patches):
            patch.stop()
        brain_gateway._CAPABILITIES.clear()
        registry.HERE, registry.REGISTRY, registry.RUNS = self.saved
        self.tmp.cleanup()

    def context(self, owner='main-a', project=None):
        return brain_context.build_context(
            project=str(project or self.project), host='test-host', owner=owner,
            operator_session='s-' + owner, session_binding='user_bound')

    def dispatch(self, owner='main-a', project=None, request_id='own-1'):
        context = self.context(owner, project)
        return panel.submit_task({
            'prompt': 'Do the work', 'kind': 'edit', 'files': ['a.py'],
            'route_mode': 'manual', 'model_id': 'mock',
            'routing': {'acceptance': ['criterion one']},
            'project': str(project or self.project),
        }, trusted=context, authority=brain_context.issue_authority(context),
            request_id=request_id)

    def complete(self, task_id):
        run = registry.task_dir(task_id, str(self.project))
        registry.patch_task(run, status='completed', ended_at=registry.now())
        policy.record_executor(run, {'completed': True, 'changed_files': ['a.py'],
                                     'test': {'exit_code': 0}})
        return run

    def return_task(self, task_id, owner='main-a'):
        context = self.context(owner)
        return policy.request_rework(
            registry.task_dir(task_id, str(self.project)), 'main', ['c1'], 'wrong result',
            'a.py only', authority=brain_context.issue_authority(context), request_id='return-1')

    def evidence(self):
        return [{'id': 'c1', 'status': 'pass',
                 'evidence': [{'source': 'reviewer_test', 'detail': 'reran', 'exit_code': 0}]}]

    def test_plain_panel_review_continuation_and_rework_are_refused(self):
        task_id = self.dispatch()['id']
        self.complete(task_id)
        with self.assertRaisesRegex(ValueError, '仅填写 actor'):
            panel.update_review(task_id, {'action': 'accept', 'actor': 'main',
                                          'criteria': self.evidence()})
        self.return_task(task_id)
        with self.assertRaisesRegex(ValueError, '主脑任务'):
            panel.submit_task({'continue_task': task_id, 'prompt': 'continue',
                               'kind': 'edit', 'project': str(self.project),
                               'files': ['a.py'], 'route_mode': 'manual', 'model_id': 'mock'})
        with self.assertRaisesRegex(ValueError, '主脑任务'):
            panel.submit_task({'rework_of': task_id, 'prompt': 'rework',
                               'kind': 'edit', 'project': str(self.project),
                               'files': ['a.py'], 'route_mode': 'manual', 'model_id': 'mock'})

    def test_trusted_connection_cannot_adopt_a_legacy_task(self):
        legacy = registry.prepare_task_dir('legacy-parent', str(self.project))
        registry.patch_task(legacy, id='legacy-parent', status='completed', engine='dsh',
                            model_id='mock', model='mock', model_name='Mock', kind='edit',
                            project=str(self.project), files=['a.py'])
        context = self.context()
        with self.assertRaisesRegex(ValueError, '不能收养'):
            panel.submit_task({'continue_task': 'legacy-parent', 'prompt': 'continue',
                               'kind': 'edit', 'project': str(self.project),
                               'files': ['a.py'], 'route_mode': 'manual', 'model_id': 'mock'},
                              trusted=context, authority=brain_context.issue_authority(context),
                              request_id='adopt-1')

    def test_trusted_rework_of_another_owner_is_refused_by_the_panel(self):
        task_id = self.dispatch(owner='main-a')['id']
        self.complete(task_id)
        self.return_task(task_id, 'main-a')
        other = self.context('main-b')
        with self.assertRaisesRegex(ValueError, '另一个拥有者或项目'):
            panel.submit_task({'rework_of': task_id, 'prompt': 'rework', 'kind': 'edit',
                               'project': str(self.project), 'files': ['a.py'],
                               'route_mode': 'manual', 'model_id': 'mock'},
                              trusted=other, authority=brain_context.issue_authority(other),
                              request_id='other-rework')

    def test_authority_must_match_the_task_owner_and_project(self):
        task_id = self.dispatch(owner='main-a')['id']
        self.complete(task_id)
        run = registry.task_dir(task_id, str(self.project))
        with self.assertRaisesRegex(ValueError, '仅填写 actor'):
            policy.accept(run, 'main', self.evidence())
        wrong_owner = brain_context.issue_authority(self.context('main-b'))
        with self.assertRaisesRegex(ValueError, '属于主脑 main-a'):
            policy.accept(run, 'main', self.evidence(), authority=wrong_owner)
        wrong_project = brain_context.issue_authority(self.context('main-a', self.other))
        with self.assertRaisesRegex(ValueError, '属于主脑 main-a'):
            policy.accept(run, 'main', self.evidence(), authority=wrong_project)
        right = brain_context.issue_authority(self.context('main-a'))
        record = policy.accept(run, 'main', self.evidence(), authority=right, request_id='accept-1')
        self.assertEqual(record['state'], policy.ACCEPTED)

    def test_executor_report_is_allowed_but_never_accepts(self):
        task_id = self.dispatch()['id']
        run = self.complete(task_id)
        record = policy.load(run)
        self.assertEqual(record['state'], policy.EXECUTOR_COMPLETED)
        self.assertIsNone(record.get('final'))
        with self.assertRaisesRegex(ValueError, '仅填写 actor'):
            policy.accept(run, 'main', self.evidence())

    def test_rework_claim_and_release_need_authority_and_one_slot(self):
        task_id = self.dispatch()['id']
        run = self.complete(task_id)
        self.return_task(task_id)
        with self.assertRaisesRegex(ValueError, '属于主脑'):
            policy.mark_rework_dispatched(run, 'child-zero')
        authority = brain_context.issue_authority(self.context())
        policy.mark_rework_dispatched(run, 'child-one', authority=authority, request_id='rework-1')
        with self.assertRaises(policy.ReworkClaimRefused):
            policy.mark_rework_dispatched(run, 'child-two', authority=authority)
        with self.assertRaisesRegex(ValueError, '属于主脑'):
            policy.release_rework_dispatch(run, 'child-one', reason='no')
        with self.assertRaisesRegex(ValueError, '属于主脑'):
            policy.request_rework(run, 'main', ['c1'], 'again', 'a.py only')
        released = policy.release_rework_dispatch(run, 'child-one', reason='not started',
                                                  authority=authority)
        self.assertIsNone(released['rework']['dispatched_task_id'])
        self.assertEqual(released['rework_count'], 1)

    def test_legacy_non_brain_tasks_keep_the_old_review_behaviour(self):
        legacy = registry.prepare_task_dir('legacy', str(self.project))
        registry.patch_task(legacy, id='legacy', status='completed', engine='dsh',
                            model_id='mock', model='mock', model_name='Mock', kind='edit',
                            project=str(self.project), files=['a.py'],
                            route={'requirements': {'acceptance': 'old criterion'}},
                            delegation={'schema': 1})
        policy.record_executor(legacy, {'completed': True, 'changed_files': ['a.py']})
        record = policy.accept(legacy, 'main',
                               [{'id': 'c1', 'status': 'pass',
                                 'evidence': [{'source': 'reviewer_file', 'detail': 'checked'}]}])
        self.assertEqual(record['state'], policy.ACCEPTED)
        self.assertIsNone(record.get('brain_owner'))


    def test_same_owner_wrong_session_cannot_approve_or_continue(self):
        from dataclasses import replace
        task_id = self.dispatch()['id']
        run = self.complete(task_id)
        other = replace(self.context(), operator_session='other-chat')
        authority = brain_context.issue_authority(other)
        with self.assertRaises(ValueError):
            policy.accept(run, 'main', self.evidence(), authority=authority)
        with self.assertRaises(ValueError):
            panel.submit_task({'continue_task':task_id, 'prompt':'continue', 'kind':'edit',
                'project':str(self.project), 'files':['a.py'], 'model_id':'mock'},
                trusted=other, request_id='wrong-chat')

    def test_brain_actor_string_does_not_expand_legacy_main_authority(self):
        with self.assertRaises(ValueError):
            policy._require_main('brain:pretend-main')


if __name__ == '__main__':
    unittest.main()
