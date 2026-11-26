import time
import unittest
from unittest.mock import patch
import steam_releases as releases

class Transport:
    def __init__(self, batches): self.batches=iter(batches);self.calls=[]
    def call(self, action, **kw):
        self.calls.append((action,kw))
        if action=='release-candidates': return {'records':next(self.batches,[])}
        if action=='ratings':return {'ratings_saved':len(kw['records'])}
        return dict(released=sum(not r['coming_soon'] for r in kw['records']),upcoming=sum(r['coming_soon'] for r in kw['records']),remaining_due=0)

class ReleaseTests(unittest.TestCase):
    def test_explicit_release_and_dates(self):
        row = releases.release_fields(1, {'1': {'success': True, 'data': {'release_date': {'coming_soon': False, 'date': 'Nov 25, 2026'}}}}, 100)
        self.assertFalse(row['coming_soon']);self.assertEqual(row['release_ts'],1795564800)
        for doc in ({}, {'1': {'success': False}}, {'1': {'success': True, 'data': {'release_date': {'date':'Nov 25, 2026'}}}}):
            with self.assertRaises(ValueError):releases.release_fields(1,doc,100)

    def test_batch_upcoming_and_confirm_launch(self):
        tr=Transport([[{'appid':1},{'appid':2},{'appid':3}]])
        items={1:{'release':{'is_coming_soon':True}},2:{'release':{'steam_release_date':100}},3:{}}
        with patch.object(releases,'store_items',return_value=items),patch.object(releases,'fetch_release',return_value=dict(appid=2,coming_soon=False)) as confirm,patch.object(releases.time,'sleep'):
            result=releases.refresh(tr,time.monotonic()+100)
        confirm.assert_called_once();self.assertEqual(confirm.call_args.args[0],2)
        self.assertEqual(result,dict(checked=2,released=1,upcoming=1,failed=1))
        self.assertEqual([x for x in tr.calls if x[0]=='releases'][0][1]['failed_appids'],[3])

    def test_continues_past_one_batch(self):
        tr=Transport([[{'appid':1}],[{'appid':2}],[]])
        def items(ids,*a):return {i:{'release':{'is_coming_soon':True}} for i in ids}
        with patch.object(releases,'store_items',side_effect=items),patch.object(releases.time,'sleep'):
            result=releases.refresh(tr,time.monotonic()+100)
        self.assertEqual(result['checked'],2)
        self.assertEqual(sum(c[0]=='release-candidates' for c in tr.calls),3)

    def test_fifty_per_claim_repeats_until_empty(self):
        tr=Transport([[{'appid':i} for i in range(1,51)], [{'appid':51}], []])
        def items(ids,*a):return {i:{'release':{'is_coming_soon':True}} for i in ids}
        with patch.object(releases,'store_items',side_effect=items),patch.object(releases.time,'sleep'):
            result=releases.refresh(tr,time.monotonic()+100,batch_size=50)
        self.assertEqual(result['checked'],51)
        claims=[kw for action,kw in tr.calls if action=='release-candidates']
        self.assertEqual([c['limit'] for c in claims],[50,50,50])

    def test_rejects_oversized_claim(self):
        tr=Transport([[{'appid':i} for i in range(1,52)]])
        with self.assertRaises(ValueError):
            releases.refresh(tr,time.monotonic()+100,batch_size=50)

    def test_reviews_zero_is_missing_score(self):
        row=releases.review_fields(1,{'reviews':{'summary_filtered':{'review_count':611,'percent_positive':96}}},100)
        self.assertEqual(row['rating'],4.8);self.assertEqual(row['rating_source'],'steam_store_summary')
        row=releases.review_fields(1,{'reviews':{'summary_filtered':{'review_count':0,'percent_positive':0}}},100)
        self.assertIsNone(row['rating']);self.assertEqual(row['rating_count'],0)
        self.assertIsNone(releases.review_fields(1,{},100))

    def test_ratings_survive_release_failure(self):
        tr=Transport([[{'appid':1}]])
        item={'release':{},'reviews':{'summary_filtered':{'review_count':611,'percent_positive':96}}}
        with patch.object(releases,'store_items',return_value={1:item}),patch.object(releases,'fetch_release',side_effect=ValueError('unavailable')),patch.object(releases.time,'sleep'):
            releases.refresh(tr,time.monotonic()+100)
        self.assertEqual([c for c in tr.calls if c[0]=='ratings'][0][1]['records'][0]['rating_count'],611)

if __name__=='__main__':unittest.main()
