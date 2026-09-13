import contextlib
import io
import json
import os
import tempfile
import unittest
from unittest import mock

from discord_username_checker import cli
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
