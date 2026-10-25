"""Offline picker acceptance: immutable settings, credential boundary and rollback."""
import copy,json,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import patch,Mock
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import model_registry as registry,model_picker as picker,native_models as native
import execution_endpoints as endpoints,workflow_settings,routing_priority,opencode_worker as oc

class PickerTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
        self.enterContext(patch.object(registry,'HERE',self.root));self.enterContext(patch.object(registry,'REGISTRY',self.root/'models.json'))
        self.enterContext(patch.object(native,'identity',lambda e:'fixture-'+e))
        self.workflow={**workflow_settings.DEFAULTS,'enabled':True,'paid_allowed':True,'readable':True}
        self.original={'id':'a','name':'API connection','model':'old','provider':'custom','default_agent':'dsh','api_base':'https://fixture.invalid/v1',
                       'secret':registry.protect('fixture-private-key'),'api_key_env':'','enabled':True,'thinking_level':'default','created_at':registry.now(),
                       'max_input_tokens':32768,'max_output_tokens':4096,'pricing_cny':{'cache_hit_input':0,'cache_miss_input':0,'output':0},
                       'max_task_complexity':'complex','capability_notes':'old-model-only','subagent':{}}
        other={**self.original,'id':'b','default_agent':'pi'}
        registry.write_json(registry.REGISTRY,{'models':[self.original,other],'default_model':'a'})
        endpoints.update('dsh',{'current_model':'a'},self.workflow)
        self.policy={'schema':1,'enabled':True,'slots':[{'label':'a','model_id':'a','unavailable_reason':''},
              {'label':'missing','model_id':None,'unavailable_reason':'no supplied model'},
              {'label':'b','model_id':'b','unavailable_reason':''}]}
        registry.write_json(self.root/routing_priority.CONFIG,self.policy)
        self.seed('dsh','api:a',[{'model':'new','name':'New model','reasoning':[picker.DEFAULT],'selectable':True,'unavailable_reason':''}])

    def seed(self,engine,source_id,models,ref=None):
        ref=self.original if ref is None and source_id.startswith('api:') else ref
        registry.write_json(self.root/picker.CACHE,{engine:{source_id:{'binding':picker.source_binding(ref) if ref else picker.native_identity(engine),'fetched_at':__import__('time').time(),'models':models}}})

    def selection(self,engine='dsh',pin=True,reasoning='default'):
        data=picker.catalog(engine,self.workflow);choice=next(m for m in data['models'] if m['_kind']!='saved')
        return {'choice_id':choice['id'],'reasoning':reasoning,'revision':data['revision'],'pin':pin}

    def test_catalog_public_no_secrets_no_mutation(self):
        before=registry.REGISTRY.read_bytes();public=picker.public(picker.catalog('dsh',self.workflow));dump=json.dumps(public)
        for forbidden in ('fixture-private-key',self.original['secret'],'_source_ref','api_key_env','api_base'):
            self.assertNotIn(forbidden,dump)
        self.assertEqual(before,registry.REGISTRY.read_bytes())
        self.assertEqual(len(public['models']),2)

    def test_immutable_clone_and_priority_only_own_engine(self):
        original=copy.deepcopy(registry.read_registry());result=picker.select('dsh',self.selection(),self.workflow)
        after=registry.read_registry();self.assertEqual(after['models'][:2],original['models'])
        new=after['models'][-1];self.assertEqual(new['secret'],self.original['secret']);self.assertEqual(new['model'],'new')
        self.assertIsNone(new['pricing_cny']);self.assertEqual(new['max_task_complexity'],'simple');self.assertEqual(new['subagent'],{})
        slots=routing_priority.snapshot()['slots'];self.assertEqual(slots[0]['model_id'],result['model_id']);self.assertEqual(slots[1:],self.policy['slots'][1:])
        self.assertFalse(result['model_called']);self.assertEqual(endpoints.snapshot()['endpoints']['dsh']['current_model'],new['id'])
        self.assertEqual(after['default_model'],'a')

    def test_pin_false_preserves_priority_bytes(self):
        before=(self.root/routing_priority.CONFIG).read_bytes();picker.select('dsh',self.selection(pin=False),self.workflow)
        self.assertEqual(before,(self.root/routing_priority.CONFIG).read_bytes())

    def test_stale_revision_unsupported_and_foreign_choice_rejected(self):
        for key,value in [('revision','stale'),('reasoning','max'),('choice_id','choice:forged')]:
            body=self.selection();body[key]=value;before=registry.REGISTRY.read_bytes()
            with self.assertRaises(ValueError):picker.select('dsh',body,self.workflow)
            self.assertEqual(before,registry.REGISTRY.read_bytes())

    def test_changed_credentials_invalidates_catalog(self):
        data=registry.read_registry();data['models'][0]['secret']=registry.protect('changed-key');registry.write_json(registry.REGISTRY,data)
        self.assertFalse(any(m['_kind']=='api' for m in picker.catalog('dsh',self.workflow)['models']))

    def test_rollback_all_three_files(self):
        body=self.selection();paths=[registry.REGISTRY,self.root/endpoints.CONFIG_NAME,self.root/routing_priority.CONFIG]
        before={p:p.read_bytes() for p in paths};write=registry.write_json
        def fail(path,value):
            if Path(path).name==endpoints.CONFIG_NAME:raise OSError('simulated disk failure')
            return write(path,value)
        with patch.object(registry,'write_json',side_effect=fail),self.assertRaises(OSError):picker.select('dsh',body,self.workflow)
        self.assertTrue(all(p.read_bytes()==v for p,v in before.items()))

    def test_native_details_and_reasoning_clone(self):
        rows=[{'model':'native-code','name':'Native','reasoning':[native.DEFAULT,{'value':'max','label':'Max'}],
               'control_id':'thought_level','selectable':True,'unavailable_reason':''}]
        native.write('workbuddy',rows,'fixture-workbuddy');self.seed('workbuddy','native:workbuddy',rows)
        result=picker.select('workbuddy',self.selection('workbuddy',False,'max'),self.workflow)
        saved=registry.get_model(result['model_id']);self.assertEqual(saved['native_reasoning']['value'],'max')
        self.assertEqual(saved['native_reasoning']['config_id'],'thought_level');self.assertEqual(saved['provider'],'workbuddy')

    def test_refresh_only_cache_and_envelope(self):
        before=registry.REGISTRY.read_bytes()
        with patch.object(picker,'api_catalog',return_value=[{'model':'refreshed','name':'Refreshed','reasoning':[picker.DEFAULT],'selectable':True}]):
            result=endpoints.update('dsh',{'model_picker':{'action':'refresh','source_id':'api:a'}},self.workflow)
        self.assertFalse(result['model_called']);self.assertEqual(before,registry.REGISTRY.read_bytes())
        self.assertTrue(any(m['model']=='refreshed' for m in result['catalog']['models']))
        with self.assertRaises(ValueError):endpoints.update('dsh',{'model_picker':{'action':'select'}},self.workflow)

    def test_api_queries_only_models_and_never_follows_redirect(self):
        response=Mock();response.read.return_value=b'{"data":[{"id":"fixture-new"}]}'
        response.__enter__=Mock(return_value=response);response.__exit__=Mock(return_value=False)
        opener=Mock();opener.open.return_value=response
        with patch.object(picker,'build_opener',return_value=opener) as build:
            result=picker.api_catalog({'source_ref':'a'})
        req=opener.open.call_args.args[0];self.assertEqual(req.full_url,'https://fixture.invalid/v1/models')
        self.assertEqual(req.get_header('Authorization'),'Bearer fixture-private-key');self.assertEqual(result[0]['model'],'fixture-new')
        self.assertTrue(any(isinstance(h,picker.NoRedirect) for h in build.call_args.args));self.assertIsNone(picker.NoRedirect().redirect_request(None,None,None,None,None,None))

    def test_key_only_edit_preserves_native_selection_model_change_invalidates(self):
        raw=registry.read_registry();row=raw['models'][0];row.update(native_model_id='opencode/old',native_reasoning={'value':'high','config_id':'effort'})
        registry.write_json(registry.REGISTRY,raw)
        body={k:row[k] for k in ('name','provider','model','api_base','default_agent')};body['api_key']='new-fixture-key'
        registry.upsert_model(body,'a');saved=registry.get_model('a')
        self.assertEqual(saved['native_reasoning']['value'],'high');self.assertEqual(saved['api_key'],'new-fixture-key')
        registry.upsert_model({**body,'model':'different'},'a');self.assertNotIn('native_model_id',registry.get_model('a'))

    def test_live_control_requires_confirmation_before_prompt(self):
        client=Mock();client.session_id='owned';client.config_options=[{'id':'effort','options':[{'value':'high','name':'High'}]}]
        control={'value':'high','config_id':'effort'}
        client.request.return_value={'configOptions':[{'id':'effort','currentValue':'high'}]};native.apply_control(client,control)
        client.request.assert_called_once_with('session/set_config_option',{'sessionId':'owned','configId':'effort','value':'high'})
        client.request.reset_mock()
        with self.assertRaises(ValueError):native.apply_control(client,{'value':'max','config_id':'effort'})
        client.request.assert_not_called()
        client.request.return_value={'configOptions':[{'id':'effort','currentValue':'low'}]}
        with self.assertRaises(ValueError):native.apply_control(client,control)

    def test_acp_exact_versions_and_model_mode_confirmation(self):
        for version,accepted in [('2.1.3',True),('2.1.4',True),('9.0.0',False)]:
            client=object.__new__(oc.ACP);client.root=self.root;client.model_id='opencode/fixture';client.native_updates=[]
            def request(method,params,startup=False):
                if method=='initialize':return {'protocolVersion':1,'agentInfo':{'version':version}}
                if method=='session/new':return {'sessionId':'owned'}
                return {'configOptions':[{'id':params['configId'],'currentValue':params['value']}]}
            client.request=request
            if accepted:self.assertEqual(client.connect('modeldesk')['model'],'opencode/fixture')
            else:
                with self.assertRaises(ValueError):client.connect('modeldesk')

    def priority_body(self):
        data=picker.catalog('dsh',self.workflow);choice=next(m for m in data['models'] if m['_kind']=='api')
        return {'enabled':True,'slots':[{'label':'Draft','model_id':'pending:fixture','unavailable_reason':''},*copy.deepcopy(self.policy['slots'][1:])],
                'expected':{'enabled':True,'slots':copy.deepcopy(self.policy['slots'])},'selections':[
                    {'id':'pending:fixture','endpoint':'dsh','choice_id':choice['id'],'reasoning':'default','revision':choice['choice_revision']}]}

    def test_priority_batch_registers_and_orders_without_endpoint_changes(self):
        body=self.priority_body();before=(self.root/endpoints.CONFIG_NAME).read_bytes();original=copy.deepcopy(registry.read_registry())
        result=picker.save_priority(body,self.workflow)
        self.assertEqual((self.root/endpoints.CONFIG_NAME).read_bytes(),before)
        after=registry.read_registry();self.assertEqual(after['models'][:2],original['models']);self.assertEqual(len(after['models']),3)
        self.assertEqual(result['slots'][0]['model_id'],after['models'][-1]['id']);self.assertEqual(result['slots'][1:],self.policy['slots'][1:])
        self.assertNotIn('secret',json.dumps(result));self.assertNotIn('fixture-private-key',json.dumps(result));self.assertFalse(result['model_called'])

    def test_priority_batch_stale_choice_or_order_does_not_register(self):
        for kind in ['choice','order']:
            with self.subTest(kind=kind):
                body=self.priority_body();before=registry.REGISTRY.read_bytes()
                if kind=='choice':body['selections'][0]['revision']='stale'
                else:body['expected']['enabled']=False
                with self.assertRaises(ValueError):picker.save_priority(body,self.workflow)
                self.assertEqual(before,registry.REGISTRY.read_bytes())

    def test_priority_batch_failure_rolls_back_both_files(self):
        body=self.priority_body();paths=[registry.REGISTRY,self.root/routing_priority.CONFIG];before={p:p.read_bytes() for p in paths};write=registry.write_json
        def fail(path,value):
            if Path(path).name==routing_priority.CONFIG:raise OSError('simulated failure')
            return write(path,value)
        with patch.object(registry,'write_json',side_effect=fail),self.assertRaises(OSError):picker.save_priority(body,self.workflow)
        self.assertTrue(all(p.read_bytes()==v for p,v in before.items()))

    def test_priority_choice_revision_survives_browsing_other_model(self):
        body=self.priority_body();cache=picker.read();cache['dsh']['api:a']['models'].append({'model':'unrelated','name':'Other','reasoning':[picker.DEFAULT],'selectable':True})
        registry.write_json(self.root/picker.CACHE,cache)
        self.assertTrue(picker.save_priority(body,self.workflow)['ok'])

    def test_priority_batch_rejects_unused_duplicate_and_malformed_drafts(self):
        for kind in ['unused','duplicate','malformed']:
            body=self.priority_body();before=registry.REGISTRY.read_bytes()
            if kind=='unused':body['selections'][0]['id']='pending:unused'
            elif kind=='duplicate':
                body['selections'].append({**body['selections'][0],'id':'pending:second'})
                body['slots'].append({'label':'Duplicate','model_id':'pending:second','unavailable_reason':''})
            else:body['slots'][0]['model_id']=[]
            with self.assertRaises(ValueError):picker.save_priority(body,self.workflow)
            self.assertEqual(before,registry.REGISTRY.read_bytes())

    def test_priority_native_variant_keeps_client_and_strength(self):
        rows=[{'model':'client-model','name':'Native choice','reasoning':[native.DEFAULT,{'value':'high','label':'High'}],
               'control_id':'thought_level','selectable':True,'unavailable_reason':''}]
        native.write('workbuddy',rows,'fixture-workbuddy');self.seed('workbuddy','native:workbuddy',rows)
        choice=next(m for m in picker.catalog('workbuddy',self.workflow)['models'] if m['_kind']=='native')
        body={'enabled':True,'slots':[{'label':'Native','model_id':'pending:wb','unavailable_reason':''}],
              'expected':{'enabled':True,'slots':self.policy['slots']},'selections':[{'id':'pending:wb','endpoint':'workbuddy','choice_id':choice['id'],'reasoning':'high','revision':choice['choice_revision']}]}
        result=picker.save_priority(body,self.workflow);saved=registry.get_model(result['slots'][0]['model_id'])
        self.assertEqual(saved['default_agent'],'workbuddy');self.assertEqual(saved['native_reasoning']['value'],'high')
        self.assertEqual(endpoints.snapshot()['endpoints']['workbuddy']['current_model'],None)

    def test_reselecting_disabled_variant_creates_new_row_keeps_old_disabled(self):
        first=picker.save_priority(self.priority_body(),self.workflow);old_id=first['slots'][0]['model_id']
        raw=registry.read_registry();next(m for m in raw['models'] if m['id']==old_id)['enabled']=False;registry.write_json(registry.REGISTRY,raw)
        body=self.priority_body();body['expected']={'enabled':first['enabled'],'slots':first['slots']}
        result=picker.save_priority(body,self.workflow)
        self.assertNotEqual(result['slots'][0]['model_id'],old_id);self.assertFalse(registry.get_model(old_id,require_enabled=False)['enabled'])

if __name__=='__main__':unittest.main()
