import time
import unittest
from unittest.mock import patch
import steam_releases as releases

class ReleaseTests(unittest.TestCase):
    def test_explicit_release_and_dates(self):
        row = releases.release_fields(1, {'1': {'success': True, 'data': {'release_date': {'coming_soon': False, 'date': 'Nov 25, 2026'}}}}, 100)
        self.assertFalse(row['coming_soon'])
        self.assertEqual(row['release_ts'], 1795564800)

    def test_delayed_and_unknown_are_not_released(self):
        row = releases.release_fields(1, {'1': {'success': True, 'data': {'release_date': {'coming_soon': True, 'date': 'Coming soon'}}}}, 100)
        self.assertTrue(row['coming_soon'])
        self.assertIsNone(row['release_ts'])
        for doc in ({}, {'1': {'success': False}}, {'1': {'success': True, 'data': {'release_date': {'date': 'Nov 25, 2026'}}}}):
            with self.assertRaises(ValueError): releases.release_fields(1, doc, 100)

    def test_individual_failure_does_not_drop_success(self):
        class Transport:
            def __init__(self): self.calls = []
            def call(self, action, **kw):
                self.calls.append((action, kw))
                if action == 'release-candidates':
                    return {'records': [{'appid': 1}, {'appid': 2}]} if len(self.calls) == 1 else {'records': []}
                self.saved = kw
                return dict(released=1, upcoming=0, remaining_due=0)
        tr = Transport()
        with patch.object(releases, 'fetch_release', side_effect=[dict(appid=1, coming_soon=False), ValueError('invalid')]), patch.object(releases.time, 'sleep'):
            result = releases.refresh(tr, time.monotonic()+30)
        self.assertEqual(tr.saved['records'], [dict(appid=1, coming_soon=False)])
        self.assertEqual(tr.saved['failed_appids'], [2])
        self.assertEqual(result, dict(checked=1, released=1, upcoming=0, failed=1))

    def test_rate_limit_stops_requests(self):
        class Transport:
            def call(self, action, **kw):
                if action == 'release-candidates': return {'records': [{'appid': 1}, {'appid': 2}]}
                self.saved = kw
                return dict(released=0, upcoming=0, remaining_due=2)
        tr = Transport()
        error = releases.urllib.error.HTTPError('test', 429, 'limit', {}, None)
        with patch.object(releases, 'fetch_release', side_effect=error) as fetch:
            releases.refresh(tr, time.monotonic()+30)
        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(tr.saved['failed_appids'], [1])
        self.assertEqual(tr.saved['records'], [])

if __name__ == '__main__': unittest.main()
