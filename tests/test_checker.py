import json
import threading
import unittest
from unittest import mock

from discord_username_checker.checker import (
    STATUS_AVAILABLE,
    STATUS_BLOCKED,
    STATUS_CANCELLED,
    STATUS_CAPTCHA,
    STATUS_ERROR,
    STATUS_INVALID,
    STATUS_TAKEN,
    STATUS_UNKNOWN,
    UsernameChecker,
)
from discord_username_checker.http import HttpResponse, NetworkError
from discord_username_checker.ratelimit import RateLimiter


def make_checker(responses, **kwargs):
    """Construit un checker dont ``_post`` renvoie les réponses données dans l'ordre."""
    options = dict(rate_limiter=RateLimiter(10000), base_backoff=0.0, max_backoff=0.0)
    options.update(kwargs)
    checker = UsernameChecker(**options)
    queue = list(responses)
    calls = []

    def fake_post(username):
        calls.append(username)
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    checker._post = fake_post  # type: ignore[assignment]
    checker.calls = calls  # type: ignore[attr-defined]
    return checker


def ok(taken):
    return HttpResponse(200, {}, json.dumps({"taken": taken}))


class CheckerTests(unittest.TestCase):
    def test_available_and_taken(self):
        checker = make_checker([ok(False), ok(True)])
        self.assertEqual(checker.check("abcd").status, STATUS_AVAILABLE)
        self.assertEqual(checker.check("efgh").status, STATUS_TAKEN)
        self.assertEqual(checker.calls, ["abcd", "efgh"])

    def test_sends_json_payload(self):
        checker = UsernameChecker(rate_limiter=RateLimiter(10000))
        captured = {}

        def fake_post_json(opener, url, payload, headers, timeout):
            captured.update(url=url, payload=payload, headers=headers)
            return ok(True)

        with mock.patch("discord_username_checker.checker.post_json", fake_post_json):
            checker.check("abcd")
        self.assertEqual(captured["payload"], {"username": "abcd"})
        self.assertIn("username-attempt-unauthed", captured["url"])
        self.assertIn("User-Agent", captured["headers"])

    def test_rate_limited_then_ok(self):
        limited = HttpResponse(429, {}, json.dumps({"retry_after": 0.05}))
        checker = make_checker([limited, ok(False)])
        result = checker.check("abcd")
        self.assertEqual(result.status, STATUS_AVAILABLE)
        self.assertEqual(len(checker.calls), 2)
        self.assertEqual(checker.rate_limit_hits, 1)

    def test_too_many_rate_limits(self):
        limited = HttpResponse(429, {"retry-after": "0"}, "")
        checker = make_checker([limited] * 3, max_rate_limit_retries=1)
        result = checker.check("abcd")
        self.assertEqual(result.status, STATUS_ERROR)
        self.assertEqual(len(checker.calls), 2)

    def test_invalid_username(self):
        body = json.dumps(
            {"code": 50035, "errors": {"username": {"_errors": [{"code": "USERNAME_INVALID", "message": "nope"}]}}}
        )
        checker = make_checker([HttpResponse(400, {}, body)])
        result = checker.check("he.re")
        self.assertEqual(result.status, STATUS_INVALID)
        self.assertIn("USERNAME_INVALID", result.detail)

    def test_captcha_is_fatal(self):
        checker = make_checker([HttpResponse(400, {}, json.dumps({"captcha_key": ["captcha-required"]}))])
        result = checker.check("abcd")
        self.assertEqual(result.status, STATUS_CAPTCHA)
        self.assertTrue(result.fatal)

    def test_blocked(self):
        checker = make_checker([HttpResponse(403, {}, "Cloudflare")])
        result = checker.check("abcd")
        self.assertEqual(result.status, STATUS_BLOCKED)
        self.assertTrue(result.fatal)

    def test_server_error_retried_then_ok(self):
        checker = make_checker([HttpResponse(502, {}, ""), ok(True)], max_retries=2)
        self.assertEqual(checker.check("abcd").status, STATUS_TAKEN)

    def test_network_error_exhausts_retries(self):
        checker = make_checker([NetworkError("timeout")] * 3, max_retries=2)
        result = checker.check("abcd")
        self.assertEqual(result.status, STATUS_ERROR)
        self.assertIn("timeout", result.detail)
        self.assertEqual(len(checker.calls), 3)

    def test_unexpected_body(self):
        checker = make_checker([HttpResponse(200, {}, json.dumps({"hello": "world"}))])
        result = checker.check("abcd")
        self.assertEqual(result.status, STATUS_UNKNOWN)

    def test_other_status(self):
        checker = make_checker([HttpResponse(418, {}, "teapot")])
        self.assertEqual(checker.check("abcd").status, STATUS_ERROR)

    def test_cancelled(self):
        checker = make_checker([ok(True)])
        stop = threading.Event()
        stop.set()
        self.assertEqual(checker.check("abcd", stop).status, STATUS_CANCELLED)
        self.assertEqual(checker.calls, [])


if __name__ == "__main__":
    unittest.main()


class RateLimitCallbackTests(unittest.TestCase):
    def test_callback_receives_delay_and_long_wait_is_honoured(self):
        limited = HttpResponse(429, {"retry-after": "3600"}, "")
        checker = make_checker([limited, ok(True)], max_rate_limit_retries=1)
        seen = []
        stop = threading.Event()

        def on_rate_limited(delay):
            seen.append(delay)
            stop.set()  # évite d'attendre réellement une heure : l'attente est interrompue

        checker.on_rate_limited = on_rate_limited
        result = checker.check("abcd", stop)
        self.assertEqual(result.status, STATUS_CANCELLED)
        self.assertEqual(len(seen), 1)
        self.assertGreaterEqual(seen[0], 3600)
        self.assertGreater(checker.rate_limiter.paused_for(), 3500)
