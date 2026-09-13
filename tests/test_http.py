import unittest

from discord_username_checker.http import HttpResponse, extract_error_message, parse_retry_after


class ParseRetryAfterTests(unittest.TestCase):
    def test_header_wins(self):
        response = HttpResponse(429, {"retry-after": "7"}, '{"retry_after": 1.5}')
        self.assertEqual(parse_retry_after(response), 7.0)

    def test_json_seconds(self):
        response = HttpResponse(429, {}, '{"retry_after": 2.5}')
        self.assertEqual(parse_retry_after(response), 2.5)

    def test_long_json_wait_is_kept_in_seconds(self):
        response = HttpResponse(429, {}, '{"retry_after": 1500}')
        self.assertEqual(parse_retry_after(response), 1500.0)

    def test_default(self):
        response = HttpResponse(429, {}, "pas du json")
        self.assertEqual(parse_retry_after(response, default=3.0), 3.0)


class ExtractErrorMessageTests(unittest.TestCase):
    def test_nested_discord_error(self):
        data = {
            "code": 50035,
            "message": "Invalid Form Body",
            "errors": {"username": {"_errors": [{"code": "USERNAME_INVALID", "message": "Pseudo invalide"}]}},
        }
        self.assertEqual(extract_error_message(data), "USERNAME_INVALID: Pseudo invalide")

    def test_plain_message(self):
        self.assertEqual(extract_error_message({"message": "Oups"}), "Oups")
        self.assertEqual(extract_error_message("texte"), "")


if __name__ == "__main__":
    unittest.main()


import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from discord_username_checker.http import NetworkError, build_opener, post_json


class _FakeDiscordHandler(BaseHTTPRequestHandler):
    """Serveur local minimal imitant l'endpoint de vérification."""

    def log_message(self, *args):  # silence
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length) or b"{}")
        username = payload.get("username", "")
        if self.headers.get("Content-Type") != "application/json":
            self.send_response(415)
            self.end_headers()
            return
        if username == "bad!":
            body = json.dumps({"code": 50035, "message": "Invalid Form Body"}).encode()
            self.send_response(400)
        elif username == "slow":
            body = json.dumps({"retry_after": 0.01}).encode()
            self.send_response(429)
            self.send_header("Retry-After", "0")
        else:
            body = json.dumps({"taken": username != "free"}).encode()
            self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("X-Test-Header", "ok")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class PostJsonLocalServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(("127.0.0.1", 0), _FakeDiscordHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.url = f"http://127.0.0.1:{cls.server.server_port}/check"
        cls.opener = build_opener()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def _post(self, username):
        return post_json(self.opener, self.url, {"username": username}, {"User-Agent": "test"}, 5.0)

    def test_success(self):
        response = self._post("free")
        self.assertEqual(response.status, 200)
        self.assertEqual(response.json(), {"taken": False})
        self.assertEqual(response.header("X-Test-Header"), "ok")

    def test_http_error_returned_as_response(self):
        response = self._post("bad!")
        self.assertEqual(response.status, 400)
        self.assertEqual(response.json()["code"], 50035)

    def test_rate_limit_headers(self):
        response = self._post("slow")
        self.assertEqual(response.status, 429)
        self.assertEqual(parse_retry_after(response), 0.0)

    def test_connection_refused_is_network_error(self):
        closed = HTTPServer(("127.0.0.1", 0), _FakeDiscordHandler)
        port = closed.server_port
        closed.server_close()
        with self.assertRaises(NetworkError):
            post_json(self.opener, f"http://127.0.0.1:{port}/check", {}, {}, 2.0)
