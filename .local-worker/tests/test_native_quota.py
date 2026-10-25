"""Offline quota tests; no provider, account, or real app requests."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import native_quota as q
import model_registry as registry
import execution_endpoints as endpoints


class QuotaTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.enterContext(patch.object(registry,'HERE',self.root))
        self.enterContext(patch.object(registry,'REGISTRY',self.root/'models.json'))

    def test_settings_default_read_has_no_side_effect(self):
        for key in q.IDS:self.assertFalse(q.settings(key)['enabled'])
        self.assertEqual(list(self.root.iterdir()),[])

    def test_invalid_settings_do_not_write(self):
        for body in ({'enabled':1},{'interval_seconds':True},{'interval_seconds':1},{'port':True},{'unknown':1}):
            with self.assertRaises(ValueError):q.save('workbuddy',body)
        self.assertEqual(list(self.root.iterdir()),[])

    def test_each_endpoint_settings_are_independent(self):
        q.save('workbuddy',{'enabled':True,'interval_seconds':300})
        self.assertTrue(q.settings('workbuddy')['enabled'])
        self.assertFalse(q.settings('zcode')['enabled'])
        self.assertFalse((self.root/'workflow.json').exists())

    def test_numbers_unknown_zero_and_time_units(self):
        self.assertIsNone(q.number(True));self.assertIsNone(q.number('0'))
        self.assertEqual(q.clean_limits([{'remaining':0,'total':0}])[0]['remaining'],0)
        self.assertIsNone(q.clean_limits([{'remaining':10}])[0]['total'])
        self.assertEqual(q.timestamp(1792937516333),1792937516.333)
        self.assertEqual(q.timestamp('2026-10-25T00:00:00Z'),1792886400)
        with self.assertRaises(ValueError):q.clean_limits([{'remaining':None,'total':True}])

    def test_display_reads_cache_without_fetching(self):
        with patch.object(q,'binding',return_value='bound'),patch.object(q,'fetch_workbuddy',side_effect=AssertionError('Network during display')):
            view=q.view('workbuddy');self.assertEqual(view['status'],'never')
        self.assertEqual(list(self.root.iterdir()),[])

    def test_failed_refresh_keeps_old_evidence_but_is_not_ready(self):
        with patch.object(q,'binding',return_value='bound'),patch.object(q,'fetch_workbuddy',return_value=q.clean_limits([{'remaining':3,'total':10}])):
            self.assertTrue(q.refresh('workbuddy')['ok'])
        with patch.object(q,'binding',return_value='bound'),patch.object(q,'fetch_workbuddy',side_effect=ValueError('secret should never be returned')),patch.object(q.time,'time',return_value=q.time.time()+20):
            result=q.refresh('workbuddy');self.assertFalse(result['ok'])
            self.assertEqual(result['quota']['status'],'error')
            self.assertEqual(result['quota']['limits'][0]['remaining'],3)
            self.assertNotIn('secret',json.dumps(result))

    def test_disabled_automatic_refresh_cannot_contact_provider(self):
        with patch.object(q,'binding',return_value='bound'),patch.object(q,'fetch_workbuddy',side_effect=AssertionError('disabled')):
            result=q.refresh('workbuddy',automatic=True)
            self.assertTrue(result['cooldown'])

    def test_multiple_clients_are_throttled(self):
        with patch.object(q,'binding',return_value='bound'),patch.object(q,'fetch_workbuddy',return_value=q.clean_limits([{'remaining':2}])) as fetch:
            q.refresh('workbuddy');self.assertTrue(q.refresh('workbuddy')['cooldown'])
            self.assertEqual(fetch.call_count,1)

    def test_source_binding_change_does_not_show_old_account(self):
        with patch.object(q,'binding',return_value='old'),patch.object(q,'fetch_workbuddy',return_value=q.clean_limits([{'remaining':3}])):q.refresh('workbuddy')
        with patch.object(q,'binding',return_value='new'):
            self.assertEqual(q.view('workbuddy')['limits'],[])

    def test_existing_endpoint_envelope_connects_without_changing_workflow(self):
        with patch.object(q,'save',return_value={'ok':True}) as save:
            self.assertTrue(endpoints.update('workbuddy',{'quota_sync':{'enabled':True}}, {})['ok'])
            save.assert_called_once_with('workbuddy',{'enabled':True})

    def test_fixed_opencode_account_url_and_percentage_direction(self):
        model={'id':'go','name':'Go','api_base':'https://opencode.ai/zen/go/v1','api_key_set':True,'default_agent':'opencode','model':'deepseek-v4.1-flash'}
        class Response:
            def __enter__(self):return self
            def __exit__(self,*a):pass
            def read(self,n):return json.dumps({'usage':{'rolling':{'percent':72,'resetsAt':'2026-10-25T00:00:00Z'}}}).encode()
        class Opener:
            def open(self,request,timeout):
                assert request.full_url=='https://opencode.ai/inference/go/v1/usage'
                return Response()
        with patch.object(q,'opencode_models',return_value=[model]),patch.object(registry,'get_model',return_value={'api_key':'offline-fixture'}),patch.object(q,'build_opener',return_value=Opener()):
            rows=q.fetch_opencode(q.settings('opencode'));self.assertEqual(rows[0]['remaining_percent'],28)

if __name__=='__main__':unittest.main()
