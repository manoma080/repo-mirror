import unittest
from unittest.mock import patch
from repo_mirror import one_time_draft_binding as binding


class SinglePatchTests(unittest.TestCase):
    def test_refuses_unapproved_write_methods(self):
        from repo_mirror import github_api
        captured = []
        with patch.dict('os.environ', {'DESTINATION_OWNER': 'manoma080'}), \
             patch.object(github_api, 'AppTokenProvider', return_value='token'), \
             patch.object(binding, 'reconcile', side_effect=lambda src, dst, secret: captured.append(dst)), \
             patch.object(github_api.API, 'request', return_value={} ) as request:
            binding.main()
            for method in ('POST', 'PUT', 'DELETE'):
                with self.subTest(method=method), self.assertRaises(ValueError):
                    captured[0].request(method, binding.RELEASE, {'body': 'x'})
            request.assert_not_called()

    def test_patch_is_restricted_and_not_retried(self):
        from repo_mirror import github_api
        captured = []
        with patch.dict('os.environ', {'DESTINATION_OWNER': 'manoma080'}), \
             patch.object(github_api, 'AppTokenProvider', return_value='token'), \
             patch.object(binding, 'reconcile', side_effect=lambda src, dst, secret: captured.append(dst)):
            binding.main()
        dst = captured[0]
        with patch.object(dst.opener, 'open', side_effect=TimeoutError) as opened:
            for url, data in ((binding.TARGET, {'body': 'x'}), (binding.RELEASE, {'body': 'x', 'draft': False})):
                with self.assertRaises(ValueError):
                    dst.request('PATCH', url, data)
            opened.assert_not_called()
            with self.assertRaises(TimeoutError):
                dst.request('PATCH', binding.RELEASE, {'body': 'x'})
            self.assertEqual(opened.call_count, 1)
            req = opened.call_args.args[0]
            self.assertEqual(req.full_url, binding.RELEASE)
            self.assertEqual(req.get_method(), 'PATCH')
            self.assertEqual(req.data, b'{"body": "x"}')
