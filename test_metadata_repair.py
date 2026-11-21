import unittest
from unittest.mock import Mock, patch
from apkpure.admin_mobile import WebsiteUnavailable, SyncError
from play_history import post_checkpoint
from repair_catalog_metadata import normalize, repair_one, progress_line, post_until
from play_history import CheckpointPending


class RepairTests(unittest.TestCase):
    def test_worker_survives_a_checkpoint_retry_window(self):
        http = Mock(deadline=1000)
        payload = {'action': 'submit', 'claim_id': 'same'}
        with patch('repair_catalog_metadata.time.monotonic', return_value=0), patch('repair_catalog_metadata.post_checkpoint', side_effect=[CheckpointPending('busy'), {'status': 'submitted'}]) as post:
            self.assertEqual(post_until(http, 'test', payload, 900), {'status': 'submitted'})
        self.assertEqual(post.call_count, 2)
        for call in post.call_args_list:
            self.assertIs(call.args[2], payload)

    def test_expired_upload_does_not_resend(self):
        http = Mock(deadline=1000)
        with patch('repair_catalog_metadata.time.monotonic', return_value=900), patch('repair_catalog_metadata.post_checkpoint') as post:
            with self.assertRaises(CheckpointPending):
                post_until(http, 'test', {}, 850)
        post.assert_not_called()

    def test_host_busy_retries_the_same_payload(self):
        http = Mock()
        payload = {'action': 'submit', 'claim_id': 'same-claim'}
        http.website.side_effect = [WebsiteUnavailable('busy', 'website', 503), {'status': 'submitted'}]
        with patch('play_history.time.monotonic', return_value=0), patch('play_history.time.sleep'):
            self.assertEqual(post_checkpoint(http, 'test-token', payload, 100)['status'], 'submitted')
        self.assertEqual(http.website.call_count, 2)
        for call in http.website.call_args_list:
            self.assertIs(call.args[1], payload)

    def test_auth_failure_is_not_hidden_as_warning(self):
        http = Mock()
        http.website.side_effect = SyncError('unauthorized', 'website', 401)
        with patch('play_history.time.monotonic', return_value=0):
            with self.assertRaises(SyncError):
                post_checkpoint(http, 'test-token', {'action': 'status'}, 100)
        self.assertEqual(http.website.call_count, 1)

    def test_normalization_does_not_invent_absent_fields(self):
        self.assertEqual(normalize({'title': 'Game', 'icon_url': None, 'asset': {'size': 0}}, 'com.example.game'),
                         {'package_name': 'com.example.game', 'title': 'Game'})
        self.assertEqual(normalize({'title': ''}, 'com.example.game'), {})

    def test_identity_mismatch_rejected(self):
        with self.assertRaises(ValueError):
            normalize({'package_name': 'com.other.game'}, 'com.example.game')

    def test_any_missing_field_triggers_fallback(self):
        item = {'package': 'com.example.game', 'missing': ['icon', 'size']}
        def play(p): return {'metadata': {'title': 'Game', 'icon_url': 'https://example.com/icon'}}
        def apk(h, p): return {'title': 'Older name', 'asset': {'size': 123}}
        r = repair_one(item, None, play, apk)
        self.assertEqual(r['outcome'], 'ok')
        self.assertEqual(r['detail']['title'], 'Game')
        self.assertEqual(r['detail']['asset']['size'], 123)

    def test_complete_play_result_skips_second_request(self):
        def apk(*args): self.fail('Unneeded request')
        r = repair_one({'package': 'com.example.game', 'missing': ['developer']}, None,
                       lambda p: {'metadata': {'developer': 'Publisher'}}, apk)
        self.assertEqual(r['detail']['developer'], 'Publisher')

    def test_transient_failure_preserves_successful_fields_for_retry(self):
        def apk(*args): raise TimeoutError('temporary')
        r = repair_one({'package': 'com.example.game', 'missing': ['icon', 'size']}, None,
                       lambda p: {'metadata': {'icon_url': 'https://example.com/icon'}}, apk)
        self.assertEqual(r['outcome'], 'retry')
        self.assertIn('icon_url', r['detail'])

    def test_unavailable_is_not_zero_or_fake_success(self):
        r = repair_one({'package': 'com.example.game', 'missing': ['icon']}, None,
                       lambda p: None, lambda h, p: None)
        self.assertEqual(r['outcome'], 'unavailable')
        self.assertIsNone(r['detail'])

    def test_progress_distinguishes_checked_from_repaired(self):
        p=dict(processed=10,total=20,fixed=3,fields_filled=8,complete=1,partial=2,
               unchanged=2,unavailable=4,failed=1,pending=9,leased=1)
        s=progress_line(p)
        self.assertIn('Checked 10/20', s)
        self.assertIn('repaired 3', s)
        self.assertIn('unavailable 4', s)


if __name__ == '__main__':
    unittest.main()
