import contextlib
import io
import json
import os
import tempfile
import unittest
from unittest import mock

import logging
import time

from discord_username_checker import cli
from discord_username_checker.checker import STATUS_AVAILABLE, STATUS_ERROR, STATUS_TAKEN
from discord_username_checker.http import HttpResponse

URL = "https://discord.com/api/webhooks/123456789012345678/token-abc"


class CliTests(unittest.TestCase):
    def test_parse_headers(self):
        self.assertEqual(cli.parse_headers(["X-A: 1", "X-B:deux"]), {"X-A": "1", "X-B": "deux"})
        with self.assertRaises(ValueError):
            cli.parse_headers(["sans-deux-points"])

    def test_webhook_from_environment(self):
        with mock.patch.dict(os.environ, {cli.ENV_WEBHOOK: URL}):
            self.assertEqual(cli.resolve_webhook_url(None), URL)
            self.assertEqual(cli.resolve_webhook_url("autre"), "autre")
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(cli.resolve_webhook_url(""))

    def test_dry_run_pattern(self):
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer), mock.patch.dict(os.environ, {}, clear=True):
            code = cli.main(["--dry-run", "--pattern", "ab??", "--charset", "xy"])
        self.assertEqual(code, cli.EXIT_OK)
        output = buffer.getvalue()
        self.assertIn("abxx", output)
        self.assertIn("Total : 4 pseudo(s)", output)

    def test_invalid_webhook_url(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            code = cli.main(["--dry-run", "--webhook", "https://example.com/x"])
        self.assertEqual(code, cli.EXIT_ERROR)

    def test_explicit_usernames_end_to_end(self):
        responses = {
            "abcd": HttpResponse(200, {}, json.dumps({"taken": False})),
            "efgh": HttpResponse(200, {}, json.dumps({"taken": True})),
        }
        sent = []

        def fake_post_json(opener, url, payload, headers, timeout):
            if "webhooks" in url:
                sent.append(payload["content"])
                return HttpResponse(204)
            return responses[payload["username"]]

        with tempfile.TemporaryDirectory() as tmp, mock.patch(
            "discord_username_checker.http.post_json", fake_post_json
        ), mock.patch("discord_username_checker.checker.post_json", fake_post_json), mock.patch(
            "discord_username_checker.webhook.post_json", fake_post_json
        ), mock.patch.dict(os.environ, {}, clear=True):
            code = cli.main(
                ["abcd", "efgh", "A..B", "--output-dir", tmp, "--webhook", URL, "--webhook-batch", "1", "--rps", "1000"]
            )
            self.assertEqual(code, cli.EXIT_OK)
            with open(os.path.join(tmp, "disponibles.txt"), encoding="utf-8") as handle:
                self.assertEqual(handle.read().split(), ["abcd"])
            with open(os.path.join(tmp, "resultats.csv"), encoding="utf-8") as handle:
                self.assertEqual(len(handle.read().splitlines()), 3)

        self.assertEqual(len(sent), 2)  # le lot de pseudos + le récapitulatif
        self.assertIn("abcd", sent[0])
        self.assertIn("Vérification terminée", sent[1])

    def test_no_valid_usernames(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(cli.main(["A..B"]), cli.EXIT_ERROR)


if __name__ == "__main__":
    unittest.main()


class DueForCheckTests(unittest.TestCase):
    def test_selection(self):
        now = 1_000_000.0
        day = 86400.0
        latest = {
            "fresh": (STATUS_TAKEN, now - 60),
            "old": (STATUS_TAKEN, now - day - 1),
            "err_recent": (STATUS_ERROR, now - 60),
            "err_old": (STATUS_ERROR, now - 700),
            "avail_old": (STATUS_AVAILABLE, now - 2 * day),
        }
        names = ["never", "fresh", "old", "err_recent", "err_old", "avail_old"]
        due = cli.due_for_check(names, latest, now, day)
        self.assertEqual(due, ["never", "old", "err_old", "avail_old"])
        self.assertEqual(cli.due_for_check(names, latest, now, 0), names)


class ChangeOnlyNotifierTests(unittest.TestCase):
    def test_only_new_availability_is_forwarded(self):
        class Sink:
            def __init__(self):
                self.names = []

            def add(self, name):
                self.names.append(name)

        sink = Sink()
        latest = {"known": (STATUS_AVAILABLE, 1.0), "was_taken": (STATUS_TAKEN, 1.0)}
        notifier = cli.ChangeOnlyNotifier(sink, latest, logging.getLogger("test"))
        notifier.add("known")
        notifier.add("was_taken")
        notifier.add("brand_new")
        self.assertEqual(sink.names, ["was_taken", "brand_new"])
        cli.ChangeOnlyNotifier(None, {}, logging.getLogger("test")).add("x")  # sans webhook : pas d'erreur


class WatchModeTests(unittest.TestCase):
    def run_watch(self, tmp, responses, sent, calls, extra):
        def fake_post_json(opener, url, payload, headers, timeout):
            if "webhooks" in url:
                sent.append(payload["content"])
                return HttpResponse(204)
            calls.append(payload["username"])
            return responses[payload["username"]]

        with mock.patch("discord_username_checker.checker.post_json", fake_post_json), mock.patch(
            "discord_username_checker.webhook.post_json", fake_post_json
        ), mock.patch.dict(os.environ, {}, clear=True):
            return cli.main(
                ["--surveiller", "aaaa", "bbbb", "--cycles", "1", "--output-dir", tmp, "--webhook", URL, "--rps", "1000"]
                + extra
            )

    def test_watch_cycles_and_rechecks(self):
        responses = {
            "aaaa": HttpResponse(200, {}, json.dumps({"taken": False})),
            "bbbb": HttpResponse(200, {}, json.dumps({"taken": True})),
        }
        with tempfile.TemporaryDirectory() as tmp:
            sent, calls = [], []
            self.assertEqual(self.run_watch(tmp, responses, sent, calls, []), cli.EXIT_OK)
            self.assertEqual(sorted(calls), ["aaaa", "bbbb"])
            self.assertTrue(sent[0].startswith("👀"))
            self.assertTrue(any("aaaa" in m and "disponible" in m for m in sent[1:]))

            # Deuxième lancement : rien n'est dû avant 24 h, aucune requête.
            sent, calls = [], []
            self.assertEqual(self.run_watch(tmp, responses, sent, calls, ["--recheck-hours", "24"]), cli.EXIT_OK)
            self.assertEqual(calls, [])

            # Troisième lancement : re-vérification forcée, aaaa toujours libre n'est pas re-signalé.
            sent, calls = [], []
            self.assertEqual(self.run_watch(tmp, responses, sent, calls, ["--recheck-hours", "0"]), cli.EXIT_OK)
            self.assertEqual(sorted(calls), ["aaaa", "bbbb"])
            self.assertFalse(any("aaaa" in m and "disponible" in m for m in sent[1:]))

    def test_watch_needs_names(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(cli.main(["--surveiller", "--output-dir", tmp]), cli.EXIT_ERROR)

    def test_watch_fatal_returns_fatal_code(self):
        responses = {"aaaa": HttpResponse(400, {}, json.dumps({"captcha_key": ["captcha-required"]})),
                     "bbbb": HttpResponse(400, {}, json.dumps({"captcha_key": ["captcha-required"]}))}
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(self.run_watch(tmp, responses, [], [], []), cli.EXIT_FATAL)
