"""Offline tests for the panel-side DSH host account snapshot cache.

Only temporary in-memory snapshots are used; no DSH, Platform, account or model
service is contacted.
"""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import host_balance


BASE = datetime(2026, 10, 18, 15, 30, tzinfo=timezone.utc)


def iso(value):
    return value.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')


def snapshot(status, fetched_at, wallets=None, error='', ttl=300):
    return {
        'schema': 1,
        'host': 'dsh',
        'status': status,
        'source': 'dsh-account-controller',
        'source_api': 'account.getBalance',
        'fetched_at': iso(fetched_at),
        'expires_at': iso(fetched_at + timedelta(seconds=ttl)),
        'wallets': wallets or [],
        'error': error,
    }


class HostBalanceTests(unittest.TestCase):
    def setUp(self):
        host_balance._reset_for_tests()

    def tearDown(self):
        host_balance._reset_for_tests()

    def test_unknown_state_is_explicit_and_has_no_quota_fallback(self):
        state = host_balance.state(now=BASE)
        self.assertEqual(state['status'], 'unavailable')
        self.assertEqual(state['wallets'], [])
        self.assertFalse(state['fresh'])
        self.assertEqual(state['source_label'], 'DSH 官方登录账户')
        self.assertEqual(state['account_source'], 'native-dsh-login')
        self.assertEqual(state['refresh'], 'snapshot-only')
        self.assertIn('不会回落到', state['message'])
        self.assertNotIn('codex', state)

    def test_ready_snapshot_exposes_only_public_wallet_state(self):
        state = host_balance.record(snapshot('ready', BASE, [
            {'kind': 'normal', 'currency': 'CNY', 'balance': '12.34'},
            {'kind': 'bonus', 'currency': 'USD', 'balance': '1.00'},
        ]), now=BASE)
        self.assertEqual(state['status'], 'ready')
        self.assertTrue(state['fresh'])
        self.assertEqual(len(state['wallets']), 2)
        self.assertEqual(state['source_api'], 'account.getBalance')
        self.assertIn('不会向 DSH 或 Platform', state['refresh_note'])
        blob = repr(state)
        self.assertNotIn('token', blob.lower())
        self.assertNotIn('profile', blob.lower())
        self.assertNotIn('avatar', blob.lower())

    def test_signed_out_clears_the_previous_account_amount(self):
        host_balance.record(snapshot('ready', BASE, [
            {'kind': 'normal', 'currency': 'CNY', 'balance': '12.34'},
        ]), now=BASE)
        state = host_balance.record(snapshot('signed-out', BASE + timedelta(seconds=1)),
                                    now=BASE + timedelta(seconds=1))
        self.assertEqual(state['status'], 'signed-out')
        self.assertEqual(state['wallets'], [])
        self.assertIn('未登录', state['message'])
        with self.assertRaises(host_balance.SnapshotError):
            host_balance.record(snapshot('ready', BASE, [
                {'kind': 'normal', 'currency': 'CNY', 'balance': '12.34'},
            ]), now=BASE + timedelta(seconds=2))

    def test_old_ready_snapshot_is_marked_stale_then_expired_without_amounts(self):
        stale_at = BASE - timedelta(seconds=200)
        state = host_balance.record(snapshot('ready', stale_at, [
            {'kind': 'normal', 'currency': 'CNY', 'balance': '5.00'},
        ]), now=BASE)
        self.assertEqual(state['status'], 'ready')
        self.assertTrue(state['stale'])
        self.assertTrue(state['fresh'] is False)
        self.assertEqual(len(state['wallets']), 1)

        host_balance._reset_for_tests()
        expired_at = BASE - timedelta(seconds=1000)
        state = host_balance.record(snapshot('ready', expired_at, [
            {'kind': 'normal', 'currency': 'CNY', 'balance': '5.00'},
        ]), now=BASE)
        self.assertEqual(state['status'], 'expired')
        self.assertTrue(state['expired'])
        self.assertEqual(state['wallets'], [])
        self.assertIn('未显示旧金额', state['message'])

    def test_failed_or_disabled_adapter_never_exposes_old_amounts(self):
        host_balance.record(snapshot('ready', BASE, [
            {'kind': 'normal', 'currency': 'CNY', 'balance': '12.34'},
        ]), now=BASE)
        failed = host_balance.record(snapshot('error', BASE + timedelta(seconds=1),
                                              error='官方余额接口失败'),
                                     now=BASE + timedelta(seconds=1))
        self.assertEqual(failed['status'], 'error')
        self.assertEqual(failed['wallets'], [])
        self.assertIn('未显示旧余额', failed['message'])
        cleared = host_balance.clear('适配器已停用', now=BASE + timedelta(seconds=2))
        self.assertEqual(cleared['status'], 'unavailable')
        self.assertEqual(cleared['wallets'], [])
        self.assertIn('适配器已停用', cleared['message'])

    def test_snapshot_rejects_credentials_profile_unknown_fields_and_old_amounts(self):
        bad = snapshot('ready', BASE)
        bad['token'] = 'secret'
        with self.assertRaises(host_balance.SnapshotError):
            host_balance.record(bad, now=BASE)
        bad = snapshot('signed-out', BASE, [
            {'kind': 'normal', 'currency': 'CNY', 'balance': '1.00'},
        ])
        with self.assertRaises(host_balance.SnapshotError):
            host_balance.record(bad, now=BASE)
        bad = snapshot('ready', BASE, [
            {'kind': 'normal', 'currency': 'CNY', 'balance': '1.00', 'profile': 'x'},
        ])
        with self.assertRaises(host_balance.SnapshotError):
            host_balance.record(bad, now=BASE)
        bad = snapshot('ready', BASE, [
            {'kind': 'normal', 'currency': 'CNY', 'balance': '1.00'},
        ])
        bad['source'] = 'api-key-account'
        with self.assertRaises(host_balance.SnapshotError):
            host_balance.record(bad, now=BASE)
        future = snapshot('ready', BASE + timedelta(minutes=10), [])
        with self.assertRaises(host_balance.SnapshotError):
            host_balance.record(future, now=BASE)

    def test_error_diagnostics_and_nonfinite_amounts_are_not_forwarded(self):
        state = host_balance.record(snapshot('error', BASE,
            error='Authorization: fixture-sensitive-value'), now=BASE)
        self.assertNotIn('fixture-sensitive-value', repr(state))
        with self.assertRaises(host_balance.SnapshotError):
            host_balance.record(snapshot('ready', BASE, [
                {'kind': 'normal', 'currency': 'CNY', 'balance': '1e9999'},
            ]), now=BASE)


if __name__ == '__main__':
    unittest.main()
