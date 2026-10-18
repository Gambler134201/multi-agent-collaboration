"""Trusted launch-context and authority tests (no service, socket or model)."""
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import brain_context as ctx


class ContextTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.project = Path(self.tmp.name).resolve()

    def tearDown(self):
        self.tmp.cleanup()

    def build(self, **kwargs):
        values = dict(project=str(self.project), host='test-host', owner='main-a',
                      role='brain', operator_session='session-a',
                      session_binding='user_bound')
        values.update(kwargs)
        return ctx.build_context(**values)

    def test_explicit_project_and_declared_unknown_identities(self):
        context = self.build(model='main-model', host_version='1.2.3')
        public = context.public()
        self.assertEqual(public['project'], str(self.project))
        self.assertEqual(public['owner'], {'id': 'main-a', 'source': 'launcher', 'immutable': True,
                                           'binding_key': context.binding_key})
        self.assertEqual(public['model']['state'], 'declared')
        self.assertEqual(public['host']['version']['state'], 'declared')
        self.assertEqual(public['session']['binding'], 'user_bound')
        self.assertTrue(public['mutations_allowed'])
        # No native-session evidence at all: unknown, never pretended verified.
        self.assertEqual(public['session']['native']['state'], 'unknown')

    def test_no_cwd_or_relative_inference(self):
        for project in ('.', 'relative/project', None, ''):
            with self.subTest(project=project):
                with self.assertRaises(ctx.ContextError):
                    self.build(project=project)

    def test_unknown_session_binding_refuses_mutations(self):
        context = self.build(operator_session=None, session_binding='unknown')
        self.assertEqual(context.session_binding, 'unknown')
        self.assertFalse(context.mutations_allowed)
        with self.assertRaises(ctx.ContextError):
            ctx.require_operation(context, 'dispatch')
        # Reads stay available so the host can explain what is missing.
        self.assertIs(ctx.require_operation(context, 'describe'), context)

    def test_executor_role_cannot_mutate_or_issue_authority(self):
        context = self.build(role='executor')
        with self.assertRaises(ctx.ContextError):
            ctx.require_operation(context, 'dispatch')
        with self.assertRaises(ctx.ContextError):
            ctx.issue_authority(context)
        self.assertIs(ctx.require_operation(context, 'status'), context)

    def test_verified_native_session_is_the_only_host_verified_binding(self):
        verified = ctx.build_context(
            project=str(self.project), host='test-host', owner='main-a',
            native_session='native-1', native_session_verified=True)
        self.assertEqual(verified.session_binding, 'host_verified')
        self.assertEqual(verified.public()['session']['native']['state'], 'verified')
        declared = ctx.build_context(
            project=str(self.project), host='test-host', owner='main-a',
            native_session='native-1')
        self.assertEqual(declared.session_binding, 'unknown')
        self.assertEqual(declared.public()['session']['native']['state'], 'declared')

    def test_declaration_cannot_claim_verified_identity(self):
        declaration = self.build(model='m', operator_session='session-a').declaration()
        self.assertEqual(set(declaration) - {'schema', 'project', 'host', 'owner', 'role',
                                             'host_version', 'model', 'native_session',
                                             'operator_session', 'session_binding', 'launcher'}, set())
        loaded = ctx.from_declaration(declaration)
        self.assertEqual(loaded.owner, 'main-a')
        self.assertEqual(loaded.session_binding, 'user_bound')
        forged = {**declaration, 'model': {'value': 'm', 'state': 'verified', 'source': 'host'}}
        with self.assertRaisesRegex(ctx.ContextError, '不能把声明值当作已核实'):
            ctx.from_declaration(forged)
        forged_host = {**declaration, 'session_binding': 'host_verified'}
        with self.assertRaisesRegex(ctx.ContextError, 'host_verified'):
            ctx.from_declaration(forged_host)
        with self.assertRaises(ctx.ContextError):
            ctx.from_declaration({**declaration, 'owner_override': 'evil'})

    def test_declaration_user_bound_requires_session_and_rejects_unknown_mix(self):
        declaration = self.build().declaration()
        with self.assertRaises(ctx.ContextError):
            ctx.from_declaration({**declaration, 'operator_session': None})
        with self.assertRaises(ctx.ContextError):
            ctx.from_declaration({**declaration, 'session_binding': 'unknown'})

    def test_authority_is_signed_and_bound_to_owner_and_project(self):
        context = self.build()
        authority = ctx.issue_authority(context)
        self.assertTrue(ctx.verify_authority(authority))
        self.assertTrue(ctx.authority_matches(authority, 'main-a', str(self.project)))
        self.assertFalse(ctx.authority_matches(authority, 'main-b', str(self.project)))
        with tempfile.TemporaryDirectory() as other:
            self.assertFalse(ctx.authority_matches(authority, 'main-a', other))
        self.assertFalse(ctx.authority_matches('main-a', 'main-a', str(self.project)))
        with self.assertRaises(ctx.ContextError):
            ctx.issue_authority(self.build(operator_session=None, session_binding='unknown'))

    def test_public_and_declaration_never_contain_a_capability(self):
        context = self.build()
        blob = str(context.public()) + str(context.declaration()) + repr(context)
        self.assertNotIn('X-Brain-Capability', blob)
        self.assertNotIn('capability', blob)


if __name__ == '__main__':
    unittest.main()
