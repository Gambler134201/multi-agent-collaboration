import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import model_registry as registry
import model_router as router
import execution_endpoints as ep
import execution_engines as engines
import workflow_settings
import routing_priority as priority
import opencode_worker as oc
import commandcode_worker as cc


class OrderedRoutingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.enterContext(patch.object(registry, 'HERE', self.root))
        self.enterContext(patch.object(registry, 'REGISTRY', self.root / 'models.json'))
        self.settings = {**workflow_settings.DEFAULTS, 'enabled': True, 'readable': True,
            'paid_allowed': True, 'enforce_paid_budget': False, 'enforce_model_call_limit': False}
        self.models = []
        for mid, engine, model in [('free', 'opencode', oc.MODEL), ('wb', 'workbuddy', 'deepseek-v4.1-flash'),
                                  ('go', 'opencode', oc.GO_MODEL), ('cmd', 'commandcode', cc.MODELS[0])]:
            self.models.append({'id': mid, 'name': mid, 'provider': 'workbuddy' if mid=='wb' else 'custom',
                'model': model, 'default_agent': engine, 'enabled': True, 'api_key_set': mid != 'wb',
                'api_base': 'https://opencode.ai/zen/go/v1' if mid=='go' else 'https://example.invalid',
                'max_input_tokens': 32768, 'max_output_tokens': 4096, 'max_task_complexity': 'simple',
                'preferred_tasks': ['信息整理'], 'secret': '' if mid == 'wb' else registry.protect('offline-fixture')})
        registry.write_json(registry.REGISTRY, {'models': self.models, 'default_model': 'free'})
        for engine in ('opencode', 'workbuddy', 'commandcode'):
            ep.update(engine, {'enabled': True}, self.settings)
        self.policy = {'enabled': True, 'slots': [
            {'label': 'OpenCode免费', 'model_id':'free', 'unavailable_reason':''},
            {'label': 'CommandCode免费', 'model_id':None, 'unavailable_reason':'不存在，按用户要求跳过'},
            {'label': 'WorkBuddy积分', 'model_id':'wb', 'unavailable_reason':''},
            {'label': 'OpenCode Go', 'model_id':'go', 'unavailable_reason':''},
            {'label': 'CommandCode普通', 'model_id':'cmd', 'unavailable_reason':''}]}
        priority.save(self.policy)
        self.enterContext(patch.object(engines, 'availability', return_value={'ready':True}))
        self.enterContext(patch('workbuddy_worker.validate_task'))
        self.body = {'project':str(self.root), 'kind':'chat', 'prompt':'read fixture', 'route_mode':'auto',
                     'routing':{'category':'extract','complexity':'simple','expected_calls':2}}

    def choose(self, failed=(), **changes):
        body = {**self.body, 'routing':{**self.body['routing'], 'failed_model_ids':list(failed)}, **changes}
        return router.choose(body, self.models, self.settings, [], 10)

    def test_exact_order_hole_and_unknown_native_cost(self):
        self.assertEqual(self.choose()['model_id'], 'free')
        self.assertEqual(self.choose(['free'])['model_id'], 'wb')
        self.assertEqual(self.choose(['free','wb'])['model_id'], 'go')
        result=self.choose(['free','wb','go'])
        self.assertEqual(result['model_id'], 'cmd')
        self.assertIsNone(result['estimated_cost_cny'])
        self.assertEqual(result['cost']['prediction']['kind'], 'unknown')

    def test_same_endpoint_two_models_keep_user_order(self):
        ep.update('opencode', {'current_model':'free'}, self.settings)
        self.assertEqual(self.choose(['free','wb'])['model_id'], 'go')
        self.assertEqual(ep.snapshot()['endpoints']['opencode']['current_model'], 'free')

    def test_manual_choice_does_not_change_order(self):
        self.assertEqual(self.choose(route_mode='manual',model_id='cmd')['model_id'], 'cmd')
        self.assertEqual(priority.snapshot()['slots'], self.policy['slots'])

    def test_disabled_endpoint_skips_both_its_models(self):
        ep.update('opencode', {'enabled':False}, self.settings)
        self.assertEqual(self.choose()['model_id'], 'wb')
        ep.update('workbuddy', {'enabled':False}, self.settings)
        self.assertEqual(self.choose()['model_id'], 'cmd')

    def test_scope_and_paid_authorization_remain_gates(self):
        self.settings['paid_allowed']=False
        with self.assertRaises(ValueError):self.choose(['wb'])
        self.settings['paid_allowed']=True
        self.settings['model_scope']='local_only'
        with self.assertRaises(ValueError):self.choose()

    def test_unknown_native_counts_refuse_hard_caps(self):
        self.settings['enforce_model_call_limit']=True
        # WorkBuddy is patched for route isolation; exclude it for this check.
        with self.assertRaises(ValueError):self.choose(['wb'])

    def test_corrupt_policy_blocks_auto_not_silent_cost_sort(self):
        (self.root / priority.CONFIG).write_text('{bad',encoding='utf-8')
        with self.assertRaisesRegex(ValueError,'无法核实'):self.choose()

    def test_invalid_priority_never_replaces_good_config(self):
        before=(self.root/priority.CONFIG).read_bytes()
        for body in ({'enabled':True,'slots':[self.policy['slots'][0]]*2},
                     {'enabled':True,'slots':[{'label':'missing','model_id':None,'unavailable_reason':''}]}):
            with self.assertRaises(ValueError):priority.save(body)
            self.assertEqual((self.root/priority.CONFIG).read_bytes(),before)

    def test_go_identity_is_separate_from_free_and_wrong_endpoint_denied(self):
        go=self.models[2]
        self.assertEqual(oc.native_model(go),'opencode-go/deepseek-v4.1-flash')
        self.assertEqual(oc.native_model(self.models[0]),'opencode/deepseek-v4.1-flash-free')
        with self.assertRaises(ValueError):oc.native_model({**go,'api_base':'https://example.invalid'})
        cfg=oc.native_config(self.root,[],'chat','agent',oc.native_model(go))
        self.assertEqual(cfg['model'],'opencode-go/deepseek-v4.1-flash')
        self.assertIn({'action':'provider.use','resource':'opencode-go','effect':'allow'},cfg['policies'])

    def test_commandcode_does_not_invent_free_model_or_grant_terminal(self):
        cc.validate_task(self.body,self.settings,self.models[3])
        with self.assertRaises(ValueError):cc.validate_task(self.body,self.settings,{**self.models[3],'model':'deepseek/deepseek-v4.1-flash:free'})
        with self.assertRaises(ValueError):cc.validate_task({**self.body,'test_command':'test'},self.settings,self.models[3])


if __name__=='__main__':unittest.main()
