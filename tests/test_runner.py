import json
import threading
import unittest

from discord_username_checker.checker import STATUS_CAPTCHA, UsernameChecker
from discord_username_checker.http import HttpResponse
from discord_username_checker.ratelimit import RateLimiter
from discord_username_checker.runner import Runner, format_duration


class ScriptedChecker(UsernameChecker):
    """Renvoie une réponse déterminée par le pseudo demandé."""

    def __init__(self, script):
        super().__init__(rate_limiter=RateLimiter(10000), base_backoff=0.0, max_backoff=0.0, max_retries=0)
        self.script = script
        self.seen = []
        self._seen_lock = threading.Lock()

    def _post(self, username):
        with self._seen_lock:
            self.seen.append(username)
        return self.script(username)


class CollectingNotifier:
    def __init__(self):
        self.names = []

    def add(self, username):
        self.names.append(username)


class RunnerTests(unittest.TestCase):
    def test_counts_and_notifications(self):
        def script(name):
            if name.startswith("a"):
                return HttpResponse(200, {}, json.dumps({"taken": False}))
            if name == "zzzz":
                return HttpResponse(500)
            return HttpResponse(200, {}, json.dumps({"taken": True}))

        names = ["aaaa", "bbbb", "abcd", "zzzz", "cccc"]
        checker = ScriptedChecker(script)
        notifier = CollectingNotifier()
        runner = Runner(checker, iter(names), len(names), workers=3, notifier=notifier, already_checked={"cccc"})
        stats = runner.run()
        self.assertEqual(stats.checked, 4)
        self.assertEqual(stats.available, 2)
        self.assertEqual(stats.taken, 1)
        self.assertEqual(stats.errors, 1)
        self.assertEqual(stats.skipped, 1)
        self.assertEqual(sorted(notifier.names), ["aaaa", "abcd"])
        self.assertNotIn("cccc", checker.seen)

    def test_fatal_result_stops_everything(self):
        def script(name):
            return HttpResponse(400, {}, json.dumps({"captcha_key": ["captcha-required"]}))

        names = [f"n{i:03d}" for i in range(200)]
        checker = ScriptedChecker(script)
        runner = Runner(checker, iter(names), len(names), workers=2)
        stats = runner.run()
        self.assertIsNotNone(stats.fatal)
        self.assertEqual(stats.fatal.status, STATUS_CAPTCHA)
        self.assertTrue(runner.stop_event.is_set())
        self.assertLess(len(checker.seen), 200)

    def test_abort_after_consecutive_problems(self):
        def script(name):
            return HttpResponse(200, {}, "{}")  # corps inattendu → unknown

        names = [f"n{i:03d}" for i in range(500)]
        checker = ScriptedChecker(script)
        runner = Runner(checker, iter(names), len(names), workers=2, abort_after_problems=5)
        stats = runner.run()
        self.assertTrue(stats.aborted_reason)
        self.assertLess(stats.checked, 500)
        self.assertGreaterEqual(stats.unknown, 5)

    def test_progress_line(self):
        checker = ScriptedChecker(lambda name: HttpResponse(200, {}, json.dumps({"taken": True})))
        runner = Runner(checker, iter(["abcd"]), 10, workers=1)
        runner.run()
        line = runner._progress()
        self.assertIn("1/10", line)
        self.assertIn("req/s", line)

    def test_format_duration(self):
        self.assertEqual(format_duration(5), "5s")
        self.assertEqual(format_duration(65), "1m05s")
        self.assertEqual(format_duration(3661), "1h01m")


if __name__ == "__main__":
    unittest.main()


class PausedProgressTests(unittest.TestCase):
    def test_progress_shows_pause_instead_of_eta(self):
        checker = ScriptedChecker(lambda name: HttpResponse(200, {}, json.dumps({"taken": True})))
        runner = Runner(checker, iter([]), 10, workers=1)
        checker.rate_limiter.pause(120)
        line = runner._progress()
        self.assertIn("en pause (429)", line)
        self.assertNotIn("ETA", line)
