import unittest
from unittest.mock import patch
import urllib.error
import collect

class CheckpointRetries(unittest.TestCase):
    def attempt(self, retry_seconds=60, deadline=None):
        clock=[0.0]; attempts=[]
        def request(*args,**kw):
            attempts.append(kw['timeout']);clock[0]+=min(25,kw['timeout'])
            if len(attempts)<=4:raise urllib.error.HTTPError('https://example.test',503,'Busy',{},None)
            response=unittest.mock.MagicMock();response.__enter__.return_value=response
            response.read.return_value=b'{"records": []}';response.headers={'Content-Type':'application/json'}
            return response
        opener=unittest.mock.Mock();opener.open.side_effect=request
        with patch.object(collect.time,'monotonic',side_effect=lambda:clock[0]),patch.object(collect.time,'sleep',side_effect=lambda s:clock.__setitem__(0,clock[0]+s)),patch.object(collect.random,'uniform',return_value=0),patch.object(collect.urllib.request,'build_opener',return_value=opener):
            result=collect.Transport('https://example.test','test',retry_seconds=retry_seconds,deadline=deadline).call('release-candidates',limit=50)
        return result,clock[0],attempts
    def test_parallel_retry_survives_more_than_one_minute(self):
        result,elapsed,attempts=self.attempt(900)
        self.assertEqual(result,{'records':[]});self.assertGreater(elapsed,60);self.assertEqual(len(attempts),5)
    def test_existing_default_stays_bounded(self):
        with self.assertRaises(collect.CheckpointUnavailable):self.attempt()
    def test_job_deadline_caps_extended_retry(self):
        with self.assertRaises(collect.CheckpointUnavailable):self.attempt(900,45)

if __name__=='__main__':unittest.main()
