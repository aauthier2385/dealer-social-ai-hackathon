import asyncio
import io
import json
import logging
import threading
import unittest

from app.main import create_app
from tests.helpers import FAKE_KEY, TEST_TOKEN, FakeMeta, acall, call, ping, settings


class HealthAndPageTests(unittest.TestCase):
    def test_health_needs_no_token_and_reveals_no_secrets(self):
        app = create_app(settings(), FakeMeta())
        r = call(app, "GET", "/health")
        self.assertEqual(r.status, 200)
        self.assertEqual(r.json()["status"], "ok")
        self.assertTrue(r.json()["meta_enabled"])
        self.assertNotIn(FAKE_KEY, r.text)
        self.assertNotIn(TEST_TOKEN, r.text)

    def test_security_headers_on_every_response(self):
        app = create_app(settings(), FakeMeta())
        r = call(app, "GET", "/health")
        self.assertEqual(r.headers["x-content-type-options"], "nosniff")
        self.assertEqual(r.headers["x-frame-options"], "DENY")
        self.assertIn("frame-ancestors 'none'", r.headers["content-security-policy"])

    def test_playground_is_served_with_strict_csp_and_no_secrets(self):
        app = create_app(settings(), FakeMeta())
        r = call(app, "GET", "/playground")
        self.assertEqual(r.status, 200)
        self.assertIn("AI Playground", r.text)
        self.assertIn("script-src 'self'", r.headers["content-security-policy"])
        self.assertNotIn("unsafe-inline", r.headers["content-security-policy"])
        self.assertNotIn(TEST_TOKEN, r.text)
        for asset in ("/playground.js", "/playground.css"):
            self.assertEqual(call(app, "GET", asset).status, 200)

    def test_playground_can_be_turned_off(self):
        app = create_app(settings(PLAYGROUND_ENABLED="false"), FakeMeta())
        self.assertEqual(call(app, "GET", "/playground").status, 404)


class AuthTests(unittest.TestCase):
    def test_missing_and_wrong_tokens_are_rejected_before_meta_is_called(self):
        meta = FakeMeta()
        app = create_app(settings(), meta)
        for token in [None, "wrong-token", TEST_TOKEN + "x", ""]:
            r = ping(app, token=token)
            self.assertEqual(r.status, 401, token)
            self.assertEqual(r.headers["www-authenticate"], "Bearer")
        self.assertEqual(meta.calls, [])


class PingTests(unittest.TestCase):
    def test_returns_muse_spark_reply(self):
        meta = FakeMeta(reply="Hey team, happy selling!")
        app = create_app(settings(), meta)
        r = ping(app, "Say hi to the sales team")
        self.assertEqual(r.status, 200)
        body = r.json()
        self.assertEqual(body["reply"], "Hey team, happy selling!")
        self.assertEqual(body["model"], "muse-spark-1.3")
        self.assertEqual(body["usage"]["total_tokens"], 29)
        self.assertEqual(r.headers["cache-control"], "no-store")

        sent = meta.calls[0]
        self.assertEqual(sent["url"], "https://api.meta.ai/v1/chat/completions")
        self.assertEqual(sent["headers"]["Authorization"], f"Bearer {FAKE_KEY}")
        self.assertEqual(sent["json"]["model"], "muse-spark-1.3")
        self.assertEqual(sent["json"]["messages"][0]["role"], "system")
        self.assertEqual(sent["json"]["messages"][1]["content"], "Say hi to the sales team")
        self.assertLessEqual(sent["timeout"], 45)
        self.assertEqual(sent["json"]["reasoning_effort"], "minimal")  # cheap connection test

    def test_rejects_bad_input_without_calling_meta(self):
        meta = FakeMeta()
        app = create_app(settings(), meta)
        auth = {"authorization": f"Bearer {TEST_TOKEN}"}
        cases = [
            ({"content-type": "application/json"}, json.dumps({"prompt": ""}).encode(), 400),
            ({"content-type": "application/json"}, json.dumps({"prompt": "x" * 501}).encode(), 400),
            ({"content-type": "application/json"}, json.dumps({"prompt": 5}).encode(), 400),
            ({"content-type": "application/json"}, b"not json", 400),
            ({"content-type": "application/json"}, b"[1,2]", 400),
            ({"content-type": "text/plain"}, b'{"prompt":"hi"}', 400),
            ({"content-type": "application/json"}, b'{"prompt":"' + b"a" * 20000 + b'"}', 413),
        ]
        for headers, body, expected in cases:
            r = call(app, "POST", "/v1/ping-model", headers={**auth, **headers}, body=body)
            self.assertEqual(r.status, expected, body[:40])
        self.assertEqual(meta.calls, [])

    def test_disabled_meta_returns_clear_message_and_never_calls_out(self):
        meta = FakeMeta()
        app = create_app(settings(META_AI_ENABLED="false", MODEL_API_KEY=""), meta)
        r = ping(app)
        self.assertEqual(r.status, 503)
        self.assertEqual(r.json()["error"], "ai_disabled")
        self.assertEqual(meta.calls, [])


class UpstreamFailureTests(unittest.TestCase):
    def test_meta_rate_limit_becomes_503_with_retry_after(self):
        app = create_app(
            settings(), FakeMeta(status=429, body=b"{}", headers={"Retry-After": "12"})
        )
        r = ping(app)
        self.assertEqual(r.status, 503)
        self.assertEqual(r.json()["error"], "ai_unavailable")
        self.assertEqual(r.headers["retry-after"], "12")

    def test_meta_server_error_and_network_error(self):
        self.assertEqual(
            ping(create_app(settings(), FakeMeta(status=500, body=b"oops"))).status, 503
        )
        meta = FakeMeta()
        meta.raise_network = True
        self.assertEqual(ping(create_app(settings(), meta)).status, 503)

    def test_bad_key_is_hidden_from_callers(self):
        r = ping(create_app(settings(), FakeMeta(status=401, body=b'{"error":"bad key"}')))
        self.assertEqual(r.status, 503)
        self.assertNotIn("key", r.json()["message"].lower())
        self.assertNotIn(FAKE_KEY, r.text)

    def test_malformed_and_oversized_responses(self):
        for body in [b"not json", b'{"choices":[]}', b'{"choices":[{"message":{"content":7}}]}']:
            r = ping(create_app(settings(), FakeMeta(body=body)))
            self.assertEqual(r.status, 502, body)
        huge = b'{"choices":[{"message":{"content":"' + b"a" * 300_000 + b'"}}]}'
        self.assertEqual(ping(create_app(settings(), FakeMeta(body=huge))).status, 502)


class LimitTests(unittest.TestCase):
    def test_per_client_rate_limit(self):
        app = create_app(settings(RATE_LIMIT_PER_CLIENT_PER_MINUTE="2"), FakeMeta())
        ref = {"x-user-ref": "salesperson-7"}
        self.assertEqual(ping(app, extra_headers=ref).status, 200)
        self.assertEqual(ping(app, extra_headers=ref).status, 200)
        r = ping(app, extra_headers=ref)
        self.assertEqual(r.status, 429)
        self.assertGreater(int(r.headers["retry-after"]), 0)
        # A different salesperson still has room.
        self.assertEqual(ping(app, extra_headers={"x-user-ref": "salesperson-8"}).status, 200)

    def test_daily_cap_protects_credits(self):
        app = create_app(settings(RATE_LIMIT_GLOBAL_PER_DAY="1"), FakeMeta())
        self.assertEqual(ping(app).status, 200)
        self.assertEqual(ping(app, extra_headers={"x-user-ref": "someone-else"}).status, 429)

    def test_concurrency_cap_turns_away_extra_calls(self):
        meta = FakeMeta()
        meta.gate = threading.Event()
        app = create_app(settings(MAX_CONCURRENT_META_CALLS="1"), meta)
        headers = {"content-type": "application/json", "authorization": f"Bearer {TEST_TOKEN}"}
        body = json.dumps({"prompt": "hi"}).encode()

        async def scenario():
            first = asyncio.create_task(
                acall(app, "POST", "/v1/ping-model", headers=headers, body=body)
            )
            while not meta.calls:  # wait until the first call is inside Meta
                await asyncio.sleep(0.01)
            second = await acall(app, "POST", "/v1/ping-model", headers=headers, body=body)
            meta.gate.set()
            return await first, second

        first, second = asyncio.run(scenario())
        self.assertEqual(first.status, 200)
        self.assertEqual(second.status, 503)
        self.assertEqual(second.json()["error"], "ai_busy")


class PrivacyTests(unittest.TestCase):
    def test_prompts_replies_and_keys_are_never_logged(self):
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        root = logging.getLogger()
        root.addHandler(handler)
        old_level = root.level
        root.setLevel(logging.DEBUG)
        try:
            app = create_app(settings(), FakeMeta(reply="SECRET-REPLY-TEXT"))
            ping(app, "Mike's daughter starts driving in spring SECRET-PROMPT-TEXT")
            ping(create_app(settings(), FakeMeta(status=401, body=b"{}")), "SECRET-PROMPT-TEXT")
        finally:
            root.removeHandler(handler)
            root.setLevel(old_level)
        logs = stream.getvalue()
        self.assertIn("meta_chat status=200", logs)  # we do log that a call happened
        for secret in ["SECRET-PROMPT-TEXT", "SECRET-REPLY-TEXT", "daughter", FAKE_KEY, TEST_TOKEN]:
            self.assertNotIn(secret, logs)


if __name__ == "__main__":
    unittest.main()
