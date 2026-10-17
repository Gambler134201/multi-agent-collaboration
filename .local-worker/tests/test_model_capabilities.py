"""Offline tests for per-model subagent capability and the user allow/deny policy.

No inference, no paid request and no real subagent is started here.
"""
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import model_capabilities as caps


def model_row(**overrides):
    row = {'id': 'm1', 'name': 'worker', 'provider': 'custom', 'model': 'some-model',
           'agent_preset': 'standard', 'enabled': True}
    row.update(overrides)
    return row


def evidence(state='supported', version='0.2.0-rc.2', model='some-model', preset='standard',
             executor='dsh', tested_at=None, **extra):
    record = {
        'state': state,
        'measured': True,
        'tested_at': tested_at or datetime.now(timezone.utc).isoformat(),
        'executor_version': version,
        'identity': caps.evidence_identity(executor=executor, model=model, preset=preset, version=version),
        'test_id': 'offline-fixture',
        'source': 'unit-test',
    }
    record.update(extra)
    return record


def identity(version='0.2.0-rc.2', model='some-model', preset='standard'):
    return caps.evidence_identity(executor='dsh', model=model, preset=preset, version=version)


class DefaultStateTests(unittest.TestCase):
    def test_unknown_model_is_unverified_and_denied(self):
        decision = caps.evaluate(model_row(), identity=identity())
        self.assertEqual(decision['state'], caps.STATE_UNVERIFIED)
        self.assertEqual(decision['decision'], caps.VERDICT_DENY)
        self.assertFalse(decision['local'])
        self.assertTrue(any('尚无' in reason for reason in decision['reasons']))

    def test_missing_subagent_block_never_treated_as_supported(self):
        decision = caps.evaluate({'id': 'bare', 'model': 'x'}, identity=identity(model='x'))
        self.assertEqual(decision['state'], caps.STATE_UNVERIFIED)
        self.assertEqual(decision['decision'], caps.VERDICT_DENY)

    def test_boolean_style_block_is_not_accepted_as_support(self):
        row = model_row(subagent={'supported': True, 'policy': 'allow'})
        decision = caps.evaluate(row, identity=identity())
        self.assertEqual(decision['state'], caps.STATE_UNVERIFIED)
        self.assertEqual(decision['decision'], caps.VERDICT_DENY)

    def test_local_model_stays_denied_even_with_support_evidence(self):
        row = model_row(id='home', provider='local', model='qwen-local',
                        subagent={'policy': 'allow', 'evidence': [evidence(model='qwen-local', preset='native')]})
        decision = caps.evaluate(row, identity=identity(model='qwen-local', preset='native'))
        self.assertEqual(decision['decision'], caps.VERDICT_DENY)
        self.assertEqual(decision['policy'], caps.POLICY_DENY)
        self.assertEqual(decision['state'], caps.STATE_SUPPORTED)
        self.assertTrue(decision['local'])
        self.assertTrue(any('本地' in reason for reason in decision['reasons']))


class PolicyTests(unittest.TestCase):
    def test_user_deny_beats_supported_evidence(self):
        row = model_row(subagent={'policy': 'deny', 'evidence': [evidence()]})
        decision = caps.evaluate(row, identity=identity())
        self.assertEqual(decision['state'], caps.STATE_SUPPORTED)
        self.assertEqual(decision['decision'], caps.VERDICT_DENY)

    def test_matching_support_plus_allow_is_the_only_grant_path(self):
        row = model_row(subagent={'policy': 'allow', 'evidence': [evidence()]})
        decision = caps.evaluate(row, identity=identity())
        self.assertEqual(decision['decision'], caps.VERDICT_ALLOW)
        self.assertEqual(decision['state'], caps.STATE_SUPPORTED)
        self.assertFalse(decision['stale'])
        self.assertFalse(decision['expired'])

    def test_allow_without_evidence_is_denied(self):
        row = model_row(subagent={'policy': 'allow', 'evidence': []})
        decision = caps.evaluate(row, identity=identity())
        self.assertEqual(decision['decision'], caps.VERDICT_DENY)
        self.assertEqual(decision['state'], caps.STATE_UNVERIFIED)

    def test_allow_with_unsupported_evidence_is_denied(self):
        row = model_row(subagent={'policy': 'allow', 'evidence': [evidence(state='unsupported')]})
        decision = caps.evaluate(row, identity=identity())
        self.assertEqual(decision['state'], caps.STATE_UNSUPPORTED)
        self.assertEqual(decision['decision'], caps.VERDICT_DENY)

    def test_unset_policy_never_grants(self):
        row = model_row(subagent={'policy': 'unset', 'evidence': [evidence()]})
        self.assertEqual(caps.evaluate(row, identity=identity())['decision'], caps.VERDICT_DENY)


class EvidenceFreshnessTests(unittest.TestCase):
    def test_executor_version_change_invalidates_evidence(self):
        row = model_row(subagent={'policy': 'allow', 'evidence': [evidence(version='0.2.0-rc.1')]})
        decision = caps.evaluate(row, identity=identity(version='0.2.0-rc.2'))
        self.assertEqual(decision['decision'], caps.VERDICT_DENY)
        self.assertEqual(decision['state'], caps.STATE_UNVERIFIED)
        self.assertTrue(decision['stale'])
        self.assertTrue(any('证据身份' in reason or '证据身份已变化' in reason for reason in decision['reasons']))

    def test_model_and_preset_change_invalidate_evidence(self):
        row = model_row(subagent={'policy': 'allow', 'evidence': [evidence(model='other-model', preset='native')]})
        changed_model = caps.evaluate(row, identity=identity(model='some-model'))
        self.assertEqual(changed_model['decision'], caps.VERDICT_DENY)
        self.assertTrue(changed_model['stale'])
        changed_preset = caps.evaluate(
            model_row(agent_preset='native', subagent={'policy': 'allow', 'evidence': [evidence()]}),
            identity=identity(preset='native'))
        self.assertEqual(changed_preset['decision'], caps.VERDICT_DENY)

    def test_expired_evidence_is_downgraded(self):
        old = (datetime.now(timezone.utc) - timedelta(days=caps.EVIDENCE_MAX_AGE_DAYS + 1)).isoformat()
        row = model_row(subagent={'policy': 'allow', 'evidence': [evidence(tested_at=old)]})
        decision = caps.evaluate(row, identity=identity())
        self.assertEqual(decision['decision'], caps.VERDICT_DENY)
        self.assertTrue(decision['expired'])
        self.assertEqual(decision['state'], caps.STATE_UNVERIFIED)

    def test_unreadable_test_time_is_treated_as_expired(self):
        row = model_row(subagent={'policy': 'allow', 'evidence': [evidence(tested_at='not-a-time')]})
        decision = caps.evaluate(row, identity=identity())
        self.assertEqual(decision['decision'], caps.VERDICT_DENY)
        self.assertTrue(decision['expired'])

    def test_identity_requires_model_executor_preset_and_version(self):
        name = caps.evidence_identity(executor='dsh', model='m', preset='native', version='v1', model_version='r2')
        self.assertEqual(set(name), {'executor', 'model', 'agent_preset', 'version', 'model_version',
                                     'endpoint', 'preset_revision'})
        self.assertEqual(len(caps.identity_key(name)), 7)
        self.assertEqual(caps.identity_differences(name, {**name, 'version': 'v2'}), ['version'])

    def test_identity_binds_endpoint_and_preset_content(self):
        # D2: a grant must not survive a changed upstream endpoint or a changed
        # preset declaration, even when the release version is identical.
        name = caps.evidence_identity(executor='dsh', model='m', preset='standard', version='v1',
                                      endpoint='https://api.example', preset_revision='abc123')
        self.assertEqual(caps.identity_differences(name, {**name, 'endpoint': 'https://other.example'}),
                         ['endpoint'])
        self.assertEqual(caps.identity_differences(name, {**name, 'preset_revision': 'def456'}),
                         ['preset_revision'])
        self.assertEqual(caps.identity_key(name),
                         caps.identity_key({**name, 'endpoint': 'https://api.example', 'preset_revision': 'abc123'}))

    def test_unresolved_preset_placeholder_is_not_a_wildcard(self):
        # Without a registered resolver the preset content identity is an explicit
        # "unresolved" placeholder, never an empty wildcard that matches anything.
        saved = caps._PRESET_REVISION_RESOLVER
        caps.register_preset_resolver(None)
        try:
            self.assertEqual(caps.resolved_preset_revision('standard'), caps.UNRESOLVED_PRESET)
            self.assertTrue(caps.UNRESOLVED_PRESET)
            self.assertNotEqual(caps.UNRESOLVED_PRESET, '')
        finally:
            caps.register_preset_resolver(saved)


class NonDshExecutorTests(unittest.TestCase):
    def test_other_executors_are_unimplemented_and_denied(self):
        for engine in ('aider', 'direct', 'mystery'):
            decision = caps.evaluate(model_row(subagent={'policy': 'allow', 'evidence': [evidence()]}), executor=engine)
            self.assertEqual(decision['decision'], caps.VERDICT_DENY, engine)
            self.assertEqual(decision['state'], caps.STATE_UNVERIFIED, engine)
            self.assertTrue(any('执行器' in reason for reason in decision['reasons']), engine)


class NormalizeTests(unittest.TestCase):
    def test_normalize_rejects_bad_policy(self):
        with self.assertRaises(ValueError):
            caps.normalize_block({'policy': 'maybe'})

    def test_normalize_drops_unmeasured_supported_claims(self):
        block = caps.normalize_block({'policy': 'allow', 'evidence': [{'state': 'supported', 'measured': False}]})
        self.assertEqual(block['policy'], 'allow')
        self.assertEqual(block['evidence'], [])

    def test_normalize_requires_measured_identity_and_time(self):
        identity = caps.evidence_identity(executor='dsh', model='some-model', preset='standard',
                                          version='0.2.0-rc.2')
        bad_rows = [
            {'state': 'supported'},
            {'state': 'supported', 'measured': True},
            {'state': 'supported', 'measured': True, 'tested_at': caps._now().isoformat()},
            {'state': 'supported', 'measured': True, 'identity': {**identity, 'model': ''},
             'tested_at': caps._now().isoformat()},
            {'state': 'supported', 'measured': True, 'identity': identity, 'tested_at': 'not-a-time'},
        ]
        for bad in bad_rows:
            block = caps.normalize_block({'policy': 'allow', 'evidence': [bad]})
            self.assertEqual(block['evidence'], [], bad)
        good = caps.normalize_block({'policy': 'allow', 'evidence': [evidence()]})
        self.assertEqual(len(good['evidence']), 1)
        self.assertTrue(good['evidence'][0]['measured'])

    def test_explicit_empty_evidence_clears_previous(self):
        previous = caps.normalize_block({'policy': 'allow', 'evidence': [evidence()]})
        cleared = caps.normalize_block({'policy': 'allow', 'evidence': []}, previous)
        self.assertEqual(cleared['evidence'], [])
        kept = caps.normalize_block({}, previous)
        self.assertEqual(len(kept['evidence']), 1)

    def test_normalize_keeps_previous_evidence_and_policy(self):
        previous = caps.normalize_block({'policy': 'allow', 'evidence': [evidence()]})
        again = caps.normalize_block({}, previous)
        self.assertEqual(again['policy'], 'allow')
        self.assertEqual(len(again['evidence']), 1)
        self.assertNotIn('evidence', caps.public_block({'subagent': again}))

    def test_public_view_strips_private_keys_recursively(self):
        raw = {'api_key': 'sk-private', 'nested': [{'Authorization': 'Bearer x', 'token': 't', 'safe': 1}],
               'secret': 's', 'ok': 'keep'}
        cleaned = caps.public_view(raw)
        self.assertEqual(cleaned, {'nested': [{'safe': 1}], 'ok': 'keep'})
        self.assertNotIn('sk-private', json.dumps(cleaned))

    def test_public_block_never_carries_raw_evidence(self):
        row = model_row(subagent={'policy': 'allow', 'evidence': [evidence()]})
        block = caps.public_block(row)
        self.assertNotIn('evidence', block)
        self.assertEqual(block['policy'], 'allow')
        self.assertIn(block['decision'], (caps.VERDICT_ALLOW, caps.VERDICT_DENY))


class CapabilitiesViewTests(unittest.TestCase):
    def test_view_lists_allowed_models_and_denies_the_rest(self):
        rows = [
            model_row(id='ok', subagent={'policy': 'allow', 'evidence': [evidence()]}),
            model_row(id='unknown'),
            model_row(id='home', provider='local', model='qwen', subagent={'policy': 'allow', 'evidence': [evidence(model='qwen')]}),
        ]
        view = caps.capabilities_view(rows, identity=identity())
        self.assertEqual(view['allowed_model_ids'], ['ok'])
        self.assertEqual(view['default_verdict'], caps.VERDICT_DENY)
        by_id = {row['model_id']: row for row in view['models']}
        self.assertEqual(by_id['unknown']['decision'], caps.VERDICT_DENY)
        self.assertEqual(by_id['home']['decision'], caps.VERDICT_DENY)
        self.assertEqual(len(view['models']), 3)
        self.assertNotIn('sk-', json.dumps(view, ensure_ascii=False))


class EvidenceStrictnessTests(unittest.TestCase):
    """Negative cases for the loopholes an independent review found.

    The gate must stay closed when a ``supported`` claim is not backed by a real
    measurement, when the measurement time is missing or in the future, and when
    the record does not name the model it was measured on.
    """

    def test_supported_record_without_measured_flag_is_not_evidence(self):
        record = evidence()
        record.pop('measured')
        row = model_row(subagent={'policy': 'allow', 'evidence': [record]})
        decision = caps.evaluate(row, identity=identity())
        self.assertEqual(decision['decision'], caps.VERDICT_DENY)
        self.assertEqual(decision['state'], caps.STATE_UNVERIFIED)

    def test_supported_record_without_tested_at_expires(self):
        record = evidence()
        record['tested_at'] = None
        row = model_row(subagent={'policy': 'allow', 'evidence': [record]})
        decision = caps.evaluate(row, identity=identity())
        self.assertEqual(decision['decision'], caps.VERDICT_DENY)
        self.assertTrue(decision['expired'])
        self.assertTrue(any('实测时间' in reason for reason in decision['reasons']))

    def test_future_measurement_time_is_rejected(self):
        future = (datetime.now(timezone.utc) + timedelta(days=3)).isoformat()
        row = model_row(subagent={'policy': 'allow', 'evidence': [evidence(tested_at=future)]})
        decision = caps.evaluate(row, identity=identity())
        self.assertEqual(decision['decision'], caps.VERDICT_DENY)
        self.assertTrue(decision['expired'])
        self.assertTrue(any('晚于当前时间' in reason for reason in decision['reasons']))

    def test_model_less_record_never_matches_any_model(self):
        record = evidence()
        record['identity'] = {**record['identity'], 'model': ''}
        row = model_row(model='some-model', subagent={'policy': 'allow', 'evidence': [record]})
        decision = caps.evaluate(row, identity=identity(model='some-model'))
        self.assertEqual(decision['decision'], caps.VERDICT_DENY)
        self.assertTrue(decision['stale'])

    def test_unverified_row_is_never_upgraded_by_a_boolean(self):
        row = model_row(subagent={'policy': 'allow', 'supported': True})
        self.assertEqual(caps.evaluate(row, identity=identity())['decision'], caps.VERDICT_DENY)


class VersionResolutionTests(unittest.TestCase):
    """The default identity must use the registered installed version."""

    def setUp(self):
        self.saved = caps._INSTALLED_VERSION_RESOLVER

    def tearDown(self):
        caps.register_version_resolver(self.saved)

    def test_default_identity_uses_registered_installed_version(self):
        caps.register_version_resolver(lambda: '9.9.9')
        decision = caps.evaluate(model_row())
        self.assertEqual(decision['identity']['version'], '9.9.9')
        self.assertEqual(caps.evaluate(model_row())['decision'], caps.VERDICT_DENY)

    def test_explicit_identity_and_row_version_win(self):
        caps.register_version_resolver(lambda: '9.9.9')
        self.assertEqual(caps.evaluate(model_row(executor_version='2.0.0'))['identity']['version'], '2.0.0')
        explicit = caps.evidence_identity(executor='dsh', model='some-model', preset='standard', version='1.1.1')
        self.assertEqual(caps.evaluate(model_row(), identity=explicit)['identity']['version'], '1.1.1')

    def test_grant_bound_to_registered_version_allows(self):
        caps.register_version_resolver(lambda: '9.9.9')
        row = model_row(subagent={'policy': 'allow', 'evidence': [evidence(version='9.9.9')]})
        self.assertEqual(caps.evaluate(row)['decision'], caps.VERDICT_ALLOW)


class DigestTests(unittest.TestCase):
    """The session-identity digest must notice replaced evidence content."""

    def test_digest_changes_when_evidence_content_changes(self):
        first = caps.normalize_block({'policy': 'allow',
                                      'evidence': [evidence(tested_at='2026-01-01T00:00:00+00:00')]})
        second = caps.normalize_block({'policy': 'allow',
                                       'evidence': [evidence(tested_at='2026-02-02T00:00:00+00:00')]})
        self.assertEqual(len(first['evidence']), len(second['evidence']))
        self.assertNotEqual(caps.capability_digest(first), caps.capability_digest(second))

    def test_digest_ignores_unrelated_updated_at(self):
        block = caps.normalize_block({'policy': 'allow', 'evidence': [evidence()]})
        self.assertEqual(caps.capability_digest(block),
                         caps.capability_digest({**block, 'updated_at': 'later'}))

    def test_decision_exposes_the_digest_it_decided_on(self):
        row = model_row(subagent={'policy': 'allow', 'evidence': [evidence()]})
        decision = caps.evaluate(row, identity=identity())
        self.assertEqual(decision['digest'], caps.capability_digest(row['subagent']))


class ModeEligibilityTests(unittest.TestCase):
    """Capability is measured per mode; a read-only probe is not an edit grant."""

    def test_read_only_probe_allows_chat_and_denies_edit(self):
        row = model_row(subagent={'policy': 'allow', 'evidence': [evidence(read_only_probe=True)]})
        chat = caps.evaluate(row, identity=identity(), mode='chat')
        self.assertEqual(chat['decision'], caps.VERDICT_ALLOW)
        self.assertEqual(chat['proven_modes'], ['chat'])
        edit = caps.evaluate(row, identity=identity(), mode='edit')
        self.assertEqual(edit['decision'], caps.VERDICT_DENY)
        self.assertEqual(edit['proven_modes'], ['chat'])
        self.assertTrue(any('edit' in reason for reason in edit['reasons']))

    def test_read_only_probe_cannot_be_relabelled_as_edit_by_a_client(self):
        forged = {**evidence(read_only_probe=True), 'modes': ['edit']}
        row = model_row(subagent={'policy': 'allow', 'evidence': [forged]})
        decision = caps.evaluate(row, identity=identity(), mode='edit')
        self.assertEqual(decision['decision'], caps.VERDICT_DENY)
        self.assertEqual(decision['proven_modes'], ['chat'])

    def test_normalize_forces_read_only_modes_and_keeps_measured_edit(self):
        forced = caps.normalize_block({'policy': 'allow',
                                       'evidence': [{**evidence(read_only_probe=True), 'modes': ['edit']}]})
        self.assertEqual(forced['evidence'][0]['modes'], ['chat'])
        self.assertEqual(forced['evidence'][0]['modes_source'], 'read_only_probe')
        real_edit = caps.normalize_block({'policy': 'allow', 'evidence': [evidence(modes=['chat', 'edit'])]})
        self.assertEqual(real_edit['evidence'][0]['modes'], ['chat', 'edit'])
        self.assertEqual(real_edit['evidence'][0]['modes_source'], 'measured')

    def test_evidence_without_modes_is_unproven_but_legacy_no_mode_still_allows(self):
        row = model_row(subagent={'policy': 'allow', 'evidence': [evidence()]})
        self.assertEqual(caps.evaluate(row, identity=identity())['decision'], caps.VERDICT_ALLOW)
        self.assertEqual(caps.evaluate(row, identity=identity(), mode='chat')['decision'], caps.VERDICT_DENY)

    def test_unknown_mode_is_denied(self):
        row = model_row(subagent={'policy': 'allow', 'evidence': [evidence(read_only_probe=True)]})
        decision = caps.evaluate(row, identity=identity(), mode='deploy')
        self.assertEqual(decision['decision'], caps.VERDICT_DENY)
        self.assertTrue(any('未知任务模式' in reason for reason in decision['reasons']))

    def test_public_block_exposes_the_measured_modes(self):
        row = model_row(subagent={'policy': 'allow', 'evidence': [evidence(read_only_probe=True)]})
        block = caps.public_block(row, identity=identity())
        self.assertEqual(block['proven_modes'], ['chat'])
        self.assertNotIn('edit', block['proven_modes'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
