"""Offline explicit priority-save boundary, including competing panel drafts."""
import copy,json,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from concurrent.futures import ThreadPoolExecutor
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import model_registry as registry,routing_priority as priority

class PriorityEditorTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
        self.enterContext(patch.object(registry,'HERE',self.root));self.enterContext(patch.object(registry,'REGISTRY',self.root/'models.json'))
        registry.write_json(registry.REGISTRY,{'default_model':'a','models':[{'id':v,'name':v,'model':v,'provider':'local','enabled':True} for v in ['a','b','c']]})
        self.original={'enabled':True,'slots':[{'label':'first','model_id':'a','unavailable_reason':''},
            {'label':'free account not supplied','model_id':None,'unavailable_reason':'user kept this empty'},
            {'label':'third','model_id':'b','unavailable_reason':''}]}
        priority.save(self.original)

    def test_expected_snapshot_saves_exact_order_and_preserves_empty_slot(self):
        before=registry.REGISTRY.read_bytes();body=copy.deepcopy(self.original);body['slots'][0],body['slots'][2]=body['slots'][2],body['slots'][0]
        result=priority.save({**body,'expected':self.original})
        self.assertEqual(result['slots'],body['slots']);self.assertEqual(registry.REGISTRY.read_bytes(),before)
        self.assertEqual(json.loads((self.root/priority.CONFIG).read_text())['schema'],1)
        self.assertNotIn('expected',json.loads((self.root/priority.CONFIG).read_text()))

    def test_stale_draft_cannot_overwrite_new_policy(self):
        changed={**self.original,'enabled':False};priority.save(changed);before=(self.root/priority.CONFIG).read_bytes()
        with self.assertRaisesRegex(ValueError,'已经变化'):priority.save({**self.original,'expected':self.original})
        self.assertEqual((self.root/priority.CONFIG).read_bytes(),before)

    def test_missing_existing_model_preserved_only_with_matching_snapshot(self):
        current=copy.deepcopy(self.original);current['slots'][0]['model_id']='deleted-model'
        registry.write_json(self.root/priority.CONFIG,{'schema':1,**current})
        result=priority.save({**current,'enabled':False,'expected':current})
        self.assertEqual(result['slots'][0]['model_id'],'deleted-model')
        expected={**current,'enabled':False};body=copy.deepcopy(expected);body['slots'][2]['model_id']='invented-model'
        with self.assertRaises(ValueError):priority.save({**body,'expected':expected})

    def test_malformed_expected_duplicate_and_limit_rejected_without_write(self):
        before=(self.root/priority.CONFIG).read_bytes()
        for expected in [None,{},[],{'enabled':1,'slots':[]},{'enabled':True,'slots':'bad'}]:
            with self.assertRaises(ValueError):priority.save({**self.original,'expected':expected})
        with self.assertRaises(ValueError):priority.save({'enabled':True,'slots':[self.original['slots'][0]]*2,'expected':self.original})
        with self.assertRaises(ValueError):priority.save({'enabled':True,'slots':[self.original['slots'][1]]*21,'expected':self.original})
        self.assertEqual((self.root/priority.CONFIG).read_bytes(),before)

    def test_legacy_save_contract_and_disabled_order_remain_supported(self):
        self.assertFalse(priority.save({'enabled':False,'slots':[]})['enabled'])

    def test_invalid_policy_blocks_expected_save(self):
        p=self.root/priority.CONFIG;p.write_text('{broken')
        with self.assertRaisesRegex(ValueError,'已经变化'):priority.save({**self.original,'expected':self.original})
        self.assertEqual(p.read_text(),'{broken')

    def test_competing_drafts_have_one_winner(self):
        def attempt(change):
            try:priority.save({**change,'expected':self.original});return True
            except ValueError:return False
        a=copy.deepcopy(self.original);a['enabled']=False
        b=copy.deepcopy(self.original);b['slots'].reverse()
        with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(attempt,[a,b]))
        self.assertEqual(sorted(results),[False,True])

if __name__=='__main__':unittest.main()
