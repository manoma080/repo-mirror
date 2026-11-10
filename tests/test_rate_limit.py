import io
import unittest
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.error import URLError
from unittest.mock import patch
from urllib.error import HTTPError

from repo_mirror.github_api import API, _cooldowns


def error(code, headers=None, body=b''):
    return HTTPError('https://api.github.com/test', code, '', headers or {}, io.BytesIO(body))


class Response:
    def __enter__(self): return self
    def __exit__(self, *args): pass
    def read(self): return b'{}'


class RateLimitTests(unittest.TestCase):
    def setUp(self):
        _cooldowns.clear()
        self.addCleanup(_cooldowns.clear)

    def test_headerless_429_waits_one_minute(self):
        api = API()
        clock = [100.0]
        with (patch.object(api.opener, 'open', side_effect=[error(429), Response()]),
              patch('repo_mirror.github_api.time.monotonic', side_effect=lambda: clock[0]),
              patch('repo_mirror.github_api.time.sleep', side_effect=lambda n: clock.__setitem__(0, clock[0] + n)) as sleep):
            self.assertEqual(api.request('GET', 'https://api.github.com/test'), {})
        sleep.assert_called_once_with(60)

    def test_post_publishes_excessive_cooldown(self):
        first, second = API(), API()
        with (patch.object(first.opener, 'open', side_effect=error(429, {'Retry-After': '120'})),
              patch.object(second.opener, 'open') as opening, patch('repo_mirror.github_api.time.sleep') as sleep):
            with self.assertRaises(HTTPError): first.request('POST', 'https://api.github.com/test')
            with self.assertRaises(RuntimeError): second.request('GET', 'https://api.github.com/test')
            opening.assert_not_called()
            sleep.assert_not_called()

    def test_headerless_secondary_403(self):
        api = API()
        clock = [100.0]
        with (patch.object(api.opener, 'open', side_effect=[error(403, body=b'{"message":"You have exceeded a secondary rate limit"}'), Response()]),
              patch('repo_mirror.github_api.time.monotonic', side_effect=lambda: clock[0]),
              patch('repo_mirror.github_api.time.sleep', side_effect=lambda n: clock.__setitem__(0, clock[0]+n)) as sleep):
            self.assertEqual(api.request('GET', 'https://api.github.com/test'), {})
        sleep.assert_called_once_with(60)

    def test_cumulative_wait_is_bounded(self):
        api = API()
        clock = [100.0]
        with (patch.object(api.opener, 'open', side_effect=[error(429, {'Retry-After':'40'}), error(429, {'Retry-After':'40'}), Response()]) as opening,
              patch('repo_mirror.github_api.time.monotonic', side_effect=lambda: clock[0]),
              patch('repo_mirror.github_api.time.sleep', side_effect=lambda n: clock.__setitem__(0, clock[0]+n)) as sleep):
            with self.assertRaises(RuntimeError): api.request('GET', 'https://api.github.com/test')
        self.assertEqual(opening.call_count, 2)
        sleep.assert_called_once_with(40)

    def test_authentication_respects_existing_cooldown(self):
        from repo_mirror.github_api import AppTokenProvider
        import time
        _cooldowns['api.github.com'] = time.monotonic() + 120
        provider = AppTokenProvider('1', 'key', 'alice')
        with (patch('repo_mirror.github_api.subprocess.run') as run,
              patch('repo_mirror.github_api.build_opener') as opener):
            run.return_value.stdout = b'signature'
            with self.assertRaises(RuntimeError): provider._mint()
            opener.return_value.open.assert_not_called()

    def test_real_threads_block_until_cooldown_expires(self):
        first, second = API(), API()
        started = threading.Event()
        opened = threading.Event()
        def blocked_open(*args, **kwargs):
            opened.set()
            return Response()
        with (patch.object(first.opener, 'open', side_effect=error(429, {'Retry-After': '0.3'})),
              patch.object(second.opener, 'open', side_effect=blocked_open)):
            with self.assertRaises(HTTPError): first.request('POST', 'https://api.github.com/test')
            def worker():
                started.set()
                return second.request('GET', 'https://api.github.com/other')
            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(worker)
                self.assertTrue(started.wait(1))
                self.assertFalse(opened.wait(0.05))
                self.assertEqual(future.result(timeout=2), {})
                self.assertTrue(opened.is_set())

    def test_final_attempt_publishes_cooldown(self):
        api = API()
        clock = [100.0]
        errors = [error(500), error(500), error(500), error(429, {'Retry-After':'30'})]
        with (patch.object(api.opener, 'open', side_effect=errors),
              patch('repo_mirror.github_api.time.monotonic', side_effect=lambda: clock[0]),
              patch('repo_mirror.github_api.time.sleep', side_effect=lambda n: clock.__setitem__(0, clock[0]+n))):
            with self.assertRaises(HTTPError): api.request('GET', 'https://api.github.com/test')
            self.assertEqual(_cooldowns['api.github.com'], clock[0] + 30)

    def test_plain_403_is_not_retried(self):
        api = API()
        with patch.object(api.opener, 'open', side_effect=error(403)) as opening:
            with self.assertRaises(HTTPError): api.request('GET', 'https://api.github.com/test')
            self.assertEqual(opening.call_count, 1)

    def test_mutation_retry_and_post_no_retry(self):
        for method in ('PUT', 'PATCH', 'DELETE'):
            with self.subTest(method=method):
                api = API()
                with (patch.object(api.opener, 'open', side_effect=[URLError('temporary'), Response()]),
                      patch('repo_mirror.github_api.time.sleep')):
                    self.assertEqual(api.request(method, 'https://api.github.com/test'), {})
        api = API()
        with patch.object(api.opener, 'open', side_effect=error(503)) as opening:
            with self.assertRaises(HTTPError): api.request('POST', 'https://api.github.com/test')
            self.assertEqual(opening.call_count, 1)

    def test_authentication_publishes_rate_limit(self):
        from repo_mirror.github_api import AppTokenProvider
        provider = AppTokenProvider('1', 'key', 'alice')
        with (patch('repo_mirror.github_api.subprocess.run') as run,
              patch('repo_mirror.github_api.build_opener') as opener):
            run.return_value.stdout = b'signature'
            opener.return_value.open.side_effect = error(429, {'Retry-After': '120'})
            with self.assertRaises(HTTPError): provider._mint()
            self.assertGreater(_cooldowns['api.github.com'] - time.monotonic(), 100)

    def test_reset_later_than_retry_after_is_respected(self):
        api = API()
        with (patch.object(api.opener, 'open', side_effect=error(429, {'Retry-After':'2', 'X-RateLimit-Remaining':'0', 'X-RateLimit-Reset':'1120'})),
              patch('repo_mirror.github_api.time.time', return_value=1000),
              patch('repo_mirror.github_api.time.sleep') as sleep):
            with self.assertRaises(HTTPError): api.request('GET', 'https://api.github.com/test')
            sleep.assert_not_called()
            self.assertGreater(_cooldowns['api.github.com'] - time.monotonic(), 100)

    def test_success_with_exhausted_rate_header_publishes_cooldown(self):
        first, second = API(), API()
        response = Response()
        response.headers = {'X-RateLimit-Remaining':'0', 'X-RateLimit-Reset':'1120'}
        with (patch.object(first.opener, 'open', return_value=response),
              patch.object(second.opener, 'open') as opening,
              patch('repo_mirror.github_api.time.time', return_value=1000)):
            self.assertEqual(first.request('GET', 'https://api.github.com/test'), {})
            with self.assertRaises(RuntimeError): second.request('GET', 'https://api.github.com/other')
            opening.assert_not_called()
