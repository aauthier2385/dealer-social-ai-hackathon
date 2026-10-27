"""Exercises the real standard-library transport against a local HTTP server (no internet)."""

import http.server
import json
import threading
import time
import unittest

from app.meta_client import MetaUnavailable, urllib_transport


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_POST(self):  # noqa: N802 - http.server naming
        length = int(self.headers.get("Content-Length", 0))
        self.rfile.read(length)
        if self.path == "/ok":
            body = json.dumps({"auth": self.headers.get("Authorization")}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "http://evil.example/steal")
            self.send_header("Content-Length", "0")
            self.end_headers()
        elif self.path == "/big":
            body = b"a" * 5000
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/slow":
            time.sleep(2)
            self.send_response(200)
            self.end_headers()

    def log_message(self, *args):
        pass


class TransportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def test_sends_headers_and_reads_response(self):
        status, _, body = urllib_transport(
            self.base + "/ok", {"Authorization": "Bearer k"}, b"{}", 5, 1000
        )
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["auth"], "Bearer k")

    def test_redirects_are_never_followed(self):
        status, headers, _ = urllib_transport(self.base + "/redirect", {}, b"{}", 5, 1000)
        self.assertEqual(status, 302)  # surfaced as an error, the key never goes to evil.example

    def test_reads_at_most_one_byte_past_the_cap(self):
        _, _, body = urllib_transport(self.base + "/big", {}, b"{}", 5, 100)
        self.assertEqual(len(body), 101)

    def test_timeout_becomes_unavailable(self):
        with self.assertRaises(MetaUnavailable):
            urllib_transport(self.base + "/slow", {}, b"{}", 0.3, 1000)

    def test_connection_refused_becomes_unavailable(self):
        with self.assertRaises(MetaUnavailable):
            urllib_transport("http://127.0.0.1:1/x", {}, b"{}", 2, 1000)


if __name__ == "__main__":
    unittest.main()
