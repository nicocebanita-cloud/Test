import json
import unittest

from discord_username_checker.http import HttpResponse, NetworkError
from discord_username_checker.webhook import (
    MAX_CONTENT_LENGTH,
    AvailableNotifier,
    DiscordWebhook,
    chunk_available_names,
    format_available_messages,
    mask_webhook_url,
    validate_webhook_url,
)

URL = "https://discord.com/api/webhooks/123456789012345678/abcDEF_ghi-JKL"


class UrlTests(unittest.TestCase):
    def test_valid_urls(self):
        self.assertTrue(validate_webhook_url(URL))
        self.assertTrue(validate_webhook_url("https://discordapp.com/api/webhooks/1/tok"))
        self.assertTrue(validate_webhook_url("https://ptb.discord.com/api/v10/webhooks/1/tok"))

    def test_invalid_urls(self):
        self.assertFalse(validate_webhook_url(""))
        self.assertFalse(validate_webhook_url("http://discord.com/api/webhooks/1/tok"))
        self.assertFalse(validate_webhook_url("https://example.com/api/webhooks/1/tok"))
        self.assertFalse(validate_webhook_url("https://discord.com/api/webhooks/1"))

    def test_mask(self):
        self.assertEqual(mask_webhook_url(URL), "https://discord.com/api/webhooks/123456789012345678/…")
        self.assertNotIn("abcDEF", mask_webhook_url(URL))


class FormattingTests(unittest.TestCase):
    def test_single_message(self):
        messages = format_available_messages(["abcd", "efgh"])
        self.assertEqual(len(messages), 1)
        self.assertIn("2 pseudo(s)", messages[0])
        self.assertIn("abcd\nefgh", messages[0])

    def test_empty(self):
        self.assertEqual(format_available_messages([]), [])

    def test_chunking_respects_limit(self):
        names = [f"{i:04d}" for i in range(1500)]
        chunks = chunk_available_names(names)
        self.assertGreater(len(chunks), 1)
        self.assertEqual(sum(len(c) for c in chunks), 1500)
        for message in format_available_messages(names):
            self.assertLessEqual(len(message), MAX_CONTENT_LENGTH)


class FakeWebhook(DiscordWebhook):
    def __init__(self, responses):
        super().__init__(URL, max_retries=2)
        self.responses = list(responses)
        self.payloads = []

    def _post(self, payload):
        self.payloads.append(payload)
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class WebhookSendTests(unittest.TestCase):
    def test_success_payload(self):
        hook = FakeWebhook([HttpResponse(204)])
        self.assertTrue(hook.send("salut"))
        payload = hook.payloads[0]
        self.assertEqual(payload["content"], "salut")
        self.assertEqual(payload["allowed_mentions"], {"parse": []})
        self.assertEqual(payload["username"], "Discord Username Checker")

    def test_rate_limited_then_ok(self):
        hook = FakeWebhook([HttpResponse(429, {}, json.dumps({"retry_after": 0.01})), HttpResponse(204)])
        self.assertTrue(hook.send("x"))
        self.assertEqual(len(hook.payloads), 2)

    def test_server_error_then_ok(self):
        hook = FakeWebhook([HttpResponse(500), HttpResponse(200)])
        hook.max_retries = 1
        self.assertTrue(hook.send("x"))

    def test_client_error_not_retried(self):
        hook = FakeWebhook([HttpResponse(404, {}, "Unknown Webhook")])
        self.assertFalse(hook.send("x"))
        self.assertEqual(len(hook.payloads), 1)

    def test_network_error_exhausted(self):
        hook = FakeWebhook([NetworkError("boom")] * 3)
        hook.max_retries = 0
        self.assertFalse(hook.send("x"))

    def test_content_truncated(self):
        hook = FakeWebhook([HttpResponse(204)])
        hook.send("a" * 3000)
        self.assertLessEqual(len(hook.payloads[0]["content"]), MAX_CONTENT_LENGTH)

    def test_invalid_url_rejected(self):
        with self.assertRaises(ValueError):
            DiscordWebhook("https://example.com/hook")


class NotifierTests(unittest.TestCase):
    def test_batching(self):
        hook = FakeWebhook([HttpResponse(204)] * 5)
        notifier = AvailableNotifier(hook, batch_size=2, flush_interval=999)
        notifier.add("abcd")
        self.assertEqual(hook.payloads, [])
        notifier.add("efgh")
        self.assertEqual(len(hook.payloads), 1)
        self.assertIn("abcd\nefgh", hook.payloads[0]["content"])
        notifier.add("ijkl")
        notifier.stop()  # vide ce qui reste
        self.assertEqual(len(hook.payloads), 2)
        self.assertEqual(notifier.sent, 3)
        self.assertEqual(notifier.failed, 0)

    def test_failed_send_is_counted(self):
        hook = FakeWebhook([HttpResponse(404, {}, "gone")])
        notifier = AvailableNotifier(hook, batch_size=1)
        notifier.add("abcd")
        self.assertEqual(notifier.failed, 1)
        self.assertEqual(notifier.sent, 0)
        self.assertEqual(notifier.pending_count(), 0)


if __name__ == "__main__":
    unittest.main()


class WebhookHeadersTests(unittest.TestCase):
    def test_user_agent_is_sent(self):
        captured = {}

        def fake_post_json(opener, url, payload, headers, timeout):
            captured.update(headers=headers)
            return HttpResponse(204)

        from unittest import mock

        hook = DiscordWebhook(URL)
        with mock.patch("discord_username_checker.webhook.post_json", fake_post_json):
            self.assertTrue(hook.send("x"))
        self.assertIn("DiscordBot", captured["headers"]["User-Agent"])

    def test_cloudflare_block_is_reported(self):
        body = '{"title":"Error 1010: Access denied","status":403,"detail":"cloudflare"}'
        hook = FakeWebhook([HttpResponse(403, {}, body)])
        with self.assertLogs(hook.logger, level="ERROR") as logs:
            self.assertFalse(hook.send("x"))
        self.assertTrue(any("Cloudflare" in line for line in logs.output))
