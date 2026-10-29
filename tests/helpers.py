"""Test helpers: a tiny in-process ASGI client and a fake Meta API. No network, no real key."""

from __future__ import annotations

import asyncio
import json
import threading

from app.config import load_settings
from app.meta_client import MetaUnavailable

TEST_TOKEN = "test-service-token-0123456789abcdefghijklmnop"
FAKE_KEY = "LLM|123|fake-secret-for-tests"


def settings(**overrides):
    env = {
        "SERVICE_TOKEN": TEST_TOKEN,
        "META_AI_ENABLED": "true",
        "MODEL_API_KEY": FAKE_KEY,
    }
    env.update(overrides)
    return load_settings(env)


class FakeMeta:
    """Stands in for https://api.meta.ai. Scripted responses; records every call."""

    def __init__(self, reply="Hello from fake Muse Spark!", status=200, body=None, headers=None):
        self.reply = reply
        self.status = status
        self.body = body
        self.headers = headers or {}
        self.calls: list[dict] = []
        self.raise_network = False
        self.gate: threading.Event | None = None

    def __call__(self, url, headers, body, timeout, max_bytes):
        self.calls.append(
            {"url": url, "headers": headers, "json": json.loads(body), "timeout": timeout}
        )
        if self.gate is not None:
            self.gate.wait(5)
        if self.raise_network:
            raise MetaUnavailable("network error")
        if self.body is not None:
            data = self.body
        else:
            data = json.dumps(
                {
                    "id": "chatcmpl-test",
                    "object": "chat.completion",
                    "model": "muse-spark-1.3",
                    "choices": [
                        {"index": 0, "message": {"role": "assistant", "content": self.reply}}
                    ],
                    "usage": {"prompt_tokens": 20, "completion_tokens": 9, "total_tokens": 29},
                }
            ).encode()
        return self.status, self.headers, data[: max_bytes + 1]


class Response:
    def __init__(self, status, headers, body):
        self.status = status
        self.headers = headers
        self.body = body

    def json(self):
        return json.loads(self.body)

    @property
    def text(self):
        return self.body.decode()


async def acall(app, method, path, headers=None, body=b"", client=("127.0.0.1", 5000)):
    raw_headers = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    if body:
        raw_headers.append((b"content-length", str(len(body)).encode()))
    path, _, query = path.partition("?")
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": query.encode(),
        "root_path": "",
        "headers": raw_headers,
        "client": client,
        "server": ("testserver", 80),
    }
    sent = False

    async def receive():
        nonlocal sent
        if not sent:
            sent = True
            return {"type": "http.request", "body": body, "more_body": False}
        return {"type": "http.disconnect"}

    status, out_headers, chunks = None, {}, []

    async def send(message):
        nonlocal status, out_headers
        if message["type"] == "http.response.start":
            status = message["status"]
            out_headers = {k.decode().lower(): v.decode() for k, v in message["headers"]}
        elif message["type"] == "http.response.body":
            chunks.append(message.get("body", b""))

    await app(scope, receive, send)
    return Response(status, out_headers, b"".join(chunks))


def call(app, method, path, **kwargs):
    return asyncio.run(acall(app, method, path, **kwargs))


def ping(app, prompt="Hello there", token=TEST_TOKEN, extra_headers=None):
    headers = {"content-type": "application/json"}
    if token is not None:
        headers["authorization"] = f"Bearer {token}"
    headers.update(extra_headers or {})
    return call(
        app, "POST", "/v1/ping-model", headers=headers, body=json.dumps({"prompt": prompt}).encode()
    )
