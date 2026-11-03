"""Local facts only: no model/service starts, auth reads, desktops or real config."""
from contextlib import ExitStack
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import brain_gateway
import endpoint_readiness as readiness
import execution_endpoints as endpoints
import execution_engines as engines
import model_registry as registry
import workflow_settings
import workbuddy_worker as wb
import zcode_worker as zc


class ReadinessTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        self.root=Path(temp.name)
        self.enterContext(patch.object(registry,'HERE',self.root))
        self.enterContext(patch.object(registry,'REGISTRY',self.root/'models.json'))
        self.settings={**workflow_settings.DEFAULTS,'enabled':True,'readable':True,'model_scope':'all',
                       'paid_allowed':True,'enforce_paid_budget':False,'enforce_model_call_limit':False}
        self.model={'id':'local','name':'Local','provider':'local','enabled':True,'default_agent':'pi'}
        self.agents={key:{'ready':True,'readiness':readiness.runtime('runtime_ready')} for key in endpoints.IDS}
        self.controls={'readable':True,'error':'','endpoints':endpoints.defaults()}
        for value in self.controls['endpoints'].values():value['enabled']=True

    def row(self,key='pi',models=None,settings=None):
        rows=endpoints.public_rows(models if models is not None else [self.model],settings or self.settings,
                                  self.agents,controls=self.controls)['rows']
        return next(r for r in rows if r['id']==key)

    def test_read_only_explanations_create_no_files_and_do_not_modify_inputs(self):
        before=json.dumps([self.settings,self.model,self.controls,self.agents],sort_keys=True)
        result=self.row()
        self.assertTrue(result['diagnostic']['precheck_passed'])
        self.assertEqual(result['diagnostic']['code'],'runtime_ready')
        self.assertEqual(list(self.root.iterdir()),[])
        self.assertEqual(json.dumps([self.settings,self.model,self.controls,self.agents],sort_keys=True),before)
        self.assertIn('不证明',result['diagnostic']['note'])

    def test_disabled_and_runtime_problem_are_both_visible_without_changing_ready(self):
        self.controls['endpoints']['workbuddy']['enabled']=False
        self.agents['workbuddy']={'ready':False,'readiness':readiness.runtime('native_channel_unavailable')}
        native={**self.model,'id':'native','default_agent':'workbuddy','provider':'workbuddy'}
        row=self.row('workbuddy',[native])
        self.assertFalse(row['ready']);self.assertFalse(row['diagnostic']['precheck_passed'])
        self.assertEqual([i['code'] for i in row['diagnostic']['issues']],['endpoint_disabled','native_channel_unavailable'])

    def test_global_switch_off_is_not_reported_as_a_broken_installation(self):
        row=self.row(settings={**self.settings,'enabled':False})
        self.assertTrue(row['ready']);self.assertEqual(row['diagnostic']['code'],'workflow_disabled')
        self.assertFalse(row['diagnostic']['precheck_passed'])

    def test_stale_selection_never_silently_uses_another_model(self):
        self.controls['endpoints']['pi']['current_model']='removed'
        row=self.row()
        self.assertIsNone(row['model']);self.assertEqual(row['diagnostic']['code'],'model_missing')
        self.assertEqual(self.controls['endpoints']['pi']['current_model'],'removed')

    def test_disabled_model_and_empty_model_set_have_different_reasons(self):
        self.controls['endpoints']['pi']['current_model']='local'
        self.assertEqual(self.row(models=[{**self.model,'enabled':False}])['diagnostic']['code'],'model_disabled')
        self.controls['endpoints']['pi']['current_model']=None
        self.assertEqual(self.row(models=[])['diagnostic']['code'],'no_models')

    def test_scope_and_authorization_are_distinct_from_runtime_readiness(self):
        paid={**self.model,'provider':'deepseek'}
        row=self.row(models=[paid],settings={**self.settings,'model_scope':'local_only'})
        self.assertTrue(row['ready']);self.assertEqual(row['diagnostic']['code'],'scope_excluded')
        row=self.row(models=[paid],settings={**self.settings,'paid_allowed':False})
        self.assertEqual(row['diagnostic']['code'],'paid_not_allowed')
        self.assertEqual(self.row(settings={**self.settings,'model_scope':'invalid'})['diagnostic']['code'],'scope_invalid')

    def test_local_option_keeps_the_endpoint_eligible_without_paid_authorization(self):
        paid={**self.model,'provider':'deepseek','id':'paid'}
        self.assertTrue(self.row(models=[self.model,paid],settings={**self.settings,'paid_allowed':False})['diagnostic']['precheck_passed'])

    def test_native_hard_limits_explained_without_disabling_them(self):
        for engine in ('workbuddy','zcode'):
            native={**self.model,'id':'native','provider':engine,'default_agent':engine}
            settings={**self.settings,'enforce_paid_budget':True}
            row=self.row(engine,[native],settings)
            self.assertEqual(row['diagnostic']['code'],'native_limits_unsupported')
            self.assertTrue(settings['enforce_paid_budget']);self.assertTrue(row['ready'])

    def test_corrupt_controls_and_permission_settings_are_explained(self):
        self.controls['readable']=False
        self.assertEqual(self.row()['diagnostic']['code'],'endpoint_config_unreadable')
        self.controls['readable']=True
        with patch('endpoint_access.saved',side_effect=ValueError('invalid access')):
            row=self.row()
        self.assertEqual(row['diagnostic']['code'],'access_config_unreadable')
        self.assertTrue(row['ready']);self.assertFalse(row['diagnostic']['precheck_passed'])

    def test_legacy_missing_runtime_evidence_never_becomes_green(self):
        self.agents['pi']={}
        self.assertEqual(self.row()['diagnostic']['code'],'runtime_unknown')

    def test_invalid_runtime_diagnostic_shape_does_not_break_cards_or_claim_ready(self):
        for proof in ({'schema':1,'issues':None},{'schema':True,'issues':[]},{'schema':1,'issues':[{'code':[]}]},
                      {'schema':1,'issues':[{'code':'runtime_ready'}]}):
            self.agents['pi']={'ready':False,'readiness':proof}
            self.assertEqual(self.row()['diagnostic']['code'],'runtime_unknown')
            self.assertFalse(self.row()['diagnostic']['precheck_passed'])

    def test_candidate_tools_and_panel_share_the_same_explanation(self):
        registry.write_json(registry.REGISTRY,{'default_model':'local','models':[self.model]})
        self.agents['pi']={'ready':False,'readiness':readiness.runtime('configuration_missing')}
        with patch.object(brain_gateway,'_settings',return_value=self.settings),patch.object(endpoints,'snapshot',return_value=self.controls),patch.object(engines,'availability',side_effect=lambda k:self.agents[k]):
            response=brain_gateway._candidates(None,{})
        candidate=next(r for r in response['execution_endpoints']['rows'] if r['id']=='pi')
        self.assertEqual(candidate['diagnostic'],self.row()['diagnostic'])
        self.assertIn('MODEL_DESK_PI_HOME',candidate['diagnostic']['next_step'])
        self.assertNotIn('models',candidate);self.assertNotIn('runtime',candidate)
        self.assertEqual(response['dispatch_ready_model_ids'],[])
        self.assertFalse((self.root/'.model-desk').exists())

    def test_one_broken_adapter_does_not_break_other_cards_or_leak_exception(self):
        def available(engine):
            if engine=='workbuddy':raise RuntimeError('PRIVATE_NATIVE_ERROR')
            return self.agents[engine]
        with patch.object(engines,'availability',side_effect=available):
            result=engines.public_agents()
            selected,legacy=engines.public_selection({**self.settings,'execution_agent':'workbuddy'})
        self.assertEqual(selected,'workbuddy');self.assertFalse(legacy['ready'])
        self.assertTrue(result['pi']['ready']);self.assertFalse(result['workbuddy']['ready'])
        self.assertNotIn('PRIVATE_NATIVE_ERROR',json.dumps(result))


class NativeReadinessTests(unittest.TestCase):
    def setUp(self):
        self.stack=self.enterContext(ExitStack())
        self.info={'version':'3.16.3','fingerprint':'fixture','executable':'C:/fixture/app.exe'}
        self.config={'home':'C:/fixture','port':19765,'access_mode':'project','auto_connect':False}
        self.catalog={'models':[],'installation':'fixture','connection':{'home':'C:/fixture','port':19765}}

    def zcode(self,**values):
        defaults={'configuration':self.config,'installation':self.info,'bridge_connected':True,
                  'desktop_state':'running','catalog':self.catalog,'usage_cache':{'providers':{}}}
        for name,value in {**defaults,**values}.items():
            self.stack.enter_context(patch.object(zc,name,side_effect=value if isinstance(value,BaseException) else None,
                                                   return_value=None if isinstance(value,BaseException) else value))
        return zc.availability()

    def workbuddy(self,**values):
        import workbuddy_native_tools as tools
        self.stack.enter_context(patch.object(tools,'access_mode',return_value='project'))
        self.stack.enter_context(patch.object(tools,'revision',return_value='fixture'))
        defaults={'configuration':{'home':'C:/fixture','profile':'C:/profile'},'installation':self.info,
                  'catalog':{'models':[]}}
        for name,value in {**defaults,**values}.items():
            self.stack.enter_context(patch.object(wb,name,side_effect=value if isinstance(value,BaseException) else None,
                                                   return_value=None if isinstance(value,BaseException) else value))
        self.stack.enter_context(patch.object(wb.os,'listdir',return_value=['fixture']))
        self.stack.enter_context(patch.object(wb.bridge,'select_channel',side_effect=wb.bridge.HostUnavailable('PRIVATE_NATIVE_ERROR')))
        return wb.availability()

    def test_workbuddy_distinguishes_configuration_installation_and_channel(self):
        for values,code in (({'configuration':{'home':'','profile':''}},'configuration_missing'),
                ({'configuration':ValueError('PRIVATE_NATIVE_ERROR')},'configuration_unreadable'),
                ({'installation':readiness.DiagnosticError('version_unsupported','PRIVATE_NATIVE_ERROR')},'version_unsupported'),
                ({},'native_channel_unavailable')):
            with self.subTest(code=code),ExitStack() as scope:
                original=self.stack;self.stack=scope
                result=self.workbuddy(**values);self.stack=original
                self.assertFalse(result['ready']);self.assertEqual(result['readiness']['code'],code)
                self.assertNotIn('PRIVATE_NATIVE_ERROR',json.dumps(result))

    def test_zcode_corrupt_usage_is_not_a_failed_execution_channel(self):
        result=self.zcode(usage_cache=ValueError('PRIVATE_NATIVE_ERROR'))
        self.assertTrue(result['ready']);self.assertEqual(result['readiness']['code'],'runtime_ready')
        self.assertEqual(result['usage_status'],'unavailable');self.assertNotIn('PRIVATE_NATIVE_ERROR',json.dumps(result))

    def test_zcode_auto_connect_only_when_desktop_is_positively_absent(self):
        result=self.zcode(configuration={**self.config,'auto_connect':True},bridge_connected=False,desktop_state='absent')
        self.assertTrue(result['ready']);self.assertTrue(result['auto_connect_pending'])
        self.assertEqual(result['readiness']['code'],'auto_connect_pending')

    def test_zcode_unknown_desktop_cannot_advertise_auto_connection(self):
        result=self.zcode(configuration={**self.config,'auto_connect':True},bridge_connected=False,desktop_state='unknown')
        self.assertFalse(result['ready']);self.assertFalse(result['auto_connect_pending'])
        self.assertEqual(result['readiness']['code'],'connection_unknown')

    def test_zcode_failed_bridge_probe_is_not_proof_of_absent_desktop(self):
        result=self.zcode(configuration={**self.config,'auto_connect':True},bridge_connected=OSError('PRIVATE_NATIVE_ERROR'),desktop_state='absent')
        self.assertFalse(result['ready']);self.assertEqual(result['connection_state'],'unknown')
        self.assertNotIn('PRIVATE_NATIVE_ERROR',json.dumps(result))

    def test_zcode_running_without_bridge_and_stale_catalog_both_visible(self):
        result=self.zcode(bridge_connected=False,desktop_state='running',catalog={'models':[]})
        self.assertFalse(result['ready'])
        self.assertEqual([i['code'] for i in result['readiness']['issues']],['bridge_missing','catalog_stale'])

    def test_zcode_rejects_unverified_version_with_specific_reason(self):
        result=self.zcode(installation=readiness.DiagnosticError('version_unsupported','PRIVATE_NATIVE_ERROR'))
        self.assertFalse(result['ready']);self.assertEqual(result['readiness']['code'],'version_unsupported')
        self.assertNotIn('PRIVATE_NATIVE_ERROR',json.dumps(result))

    def test_zcode_closed_without_auto_and_catalog_valid(self):
        result=self.zcode(bridge_connected=False,desktop_state='absent')
        self.assertFalse(result['ready']);self.assertEqual(result['readiness']['code'],'desktop_closed')

    def test_zcode_malformed_catalog_binding_is_reported_without_crashing(self):
        result=self.zcode(catalog={'models':[],'installation':'fixture','connection':None})
        self.assertFalse(result['ready']);self.assertEqual(result['readiness']['code'],'catalog_stale')

    def test_pi_missing_home_and_dsh_missing_components_have_fixed_codes(self):
        import pi_worker as pi
        import dsh_worker as dsh
        import runtime_paths
        with patch.object(pi,'PI_HOME',runtime_paths.UNSET):
            self.assertEqual(pi.availability()['readiness']['code'],'configuration_missing')
        with tempfile.TemporaryDirectory() as directory:
            missing=Path(directory)/'missing'
            with patch.object(dsh,'EXECUTABLE',missing),patch.object(dsh,'NODE',missing),patch.object(dsh.runtime_paths,'acpx_runtime',return_value=missing):
                result=dsh.availability()
                self.assertFalse(result['ready'])
                self.assertEqual([i['code'] for i in result['readiness']['issues']],['executable_missing','node_missing','dependency_missing'])
