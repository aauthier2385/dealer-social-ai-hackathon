"""The one place this service talks to the Meta Model API.

Safety rules, mirroring The Dealer Social's moderation module:
* a hard time limit on every call and a cap on how much response we will read;
* at most N calls at once; extra calls are turned away instead of queued;
* redirects are never followed, so the API key cannot be forwarded elsewhere;
* prompts, replies and keys are never logged. Only event names, status codes and timings are.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field

import anyio.to_thread

from app.config import Settings

log = logging.getLogger("dsai.meta")

MAX_RESPONSE_BYTES = 256 * 1024
MAX_PROMPT_CHARS = 20_000
TIMEOUT_SECONDS = 30.0
ALLOWED_ROLES = {"system", "user", "assistant"}

# (url, headers, body, timeout_seconds, max_bytes) -> (status, headers, body)
Transport = Callable[[str, dict, bytes, float, int], "tuple[int, dict, bytes]"]


class MetaError(Exception):
    """Base error. `code` is safe to show to callers; details are never exposed."""

    http_status = 503
    code = "ai_unavailable"

    def __init__(self, message: str = "", retry_after: int | None = None):
        super().__init__(message or self.code)
        self.retry_after = retry_after


class MetaDisabled(MetaError):
    code = "ai_disabled"


class MetaUnavailable(MetaError):
    code = "ai_unavailable"


class MetaBusy(MetaError):
    code = "ai_busy"


class MetaBadResponse(MetaError):
    http_status = 502
    code = "ai_bad_response"


class MetaInputError(MetaError):
    http_status = 400
    code = "invalid_input"


@dataclass
class ChatResult:
    text: str
    model: str
    latency_ms: int
    usage: dict = field(default_factory=dict)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):  # noqa: D401 - never follow redirects
        return None


_opener = urllib.request.build_opener(_NoRedirect)


def urllib_transport(
    url: str, headers: dict, body: bytes, timeout: float, max_bytes: int
) -> tuple[int, dict, bytes]:
    """Default transport: standard-library HTTPS with no third-party HTTP client."""
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with _opener.open(request, timeout=timeout) as response:
            return response.status, dict(response.headers), response.read(max_bytes + 1)
    except urllib.error.HTTPError as exc:
        data = exc.read(max_bytes + 1) if exc.fp else b""
        return exc.code, dict(exc.headers or {}), data
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise MetaUnavailable("network error") from exc


def _retry_after(headers: dict) -> int:
    for name, value in headers.items():
        if name.lower() == "retry-after":
            try:
                return max(1, min(300, int(float(value))))
            except (TypeError, ValueError):
                break
    return 30


class MetaClient:
    def __init__(self, settings: Settings, transport: Transport | None = None):
        self._settings = settings
        self._transport = transport or urllib_transport
        self._active = 0

    @property
    def enabled(self) -> bool:
        return self._settings.meta_enabled

    @property
    def text_model(self) -> str:
        return self._settings.text_model

    def __repr__(self) -> str:
        return f"MetaClient(enabled={self.enabled}, model={self.text_model!r})"

    @staticmethod
    def _validate(messages: list[dict]) -> None:
        if not messages:
            raise MetaInputError("no messages")
        total = 0
        for message in messages:
            if message.get("role") not in ALLOWED_ROLES:
                raise MetaInputError("bad role")
            content = message.get("content")
            if not isinstance(content, str) or not content.strip():
                raise MetaInputError("empty content")
            total += len(content)
        if total > MAX_PROMPT_CHARS:
            raise MetaInputError("prompt too long")

    async def chat(self, messages: list[dict]) -> ChatResult:
        if not self.enabled:
            raise MetaDisabled()
        self._validate(messages)

        # No awaits between the check and the increment, so this is safe on one event loop.
        if self._active >= self._settings.max_concurrency:
            raise MetaBusy("too many calls at once", retry_after=5)
        self._active += 1
        started = time.monotonic()
        try:
            url = f"{self._settings.meta_base_url}/chat/completions"
            headers = {
                "Authorization": f"Bearer {self._settings.meta_api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": "dealer-social-ai/0.1",
            }
            body = json.dumps({"model": self._settings.text_model, "messages": messages}).encode()
            try:
                status, response_headers, data = await anyio.to_thread.run_sync(
                    self._transport, url, headers, body, TIMEOUT_SECONDS, MAX_RESPONSE_BYTES
                )
            except MetaUnavailable:
                waited = int((time.monotonic() - started) * 1000)
                log.warning("meta_chat network_error latency_ms=%s (timeout or no route)", waited)
                raise
        finally:
            self._active -= 1
        latency_ms = int((time.monotonic() - started) * 1000)
        log.info("meta_chat status=%s latency_ms=%s", status, latency_ms)

        if status == 429 or status >= 500:
            raise MetaUnavailable("upstream busy", retry_after=_retry_after(response_headers))
        if status in (401, 403):
            log.error("meta_auth_failed status=%s (check MODEL_API_KEY)", status)
            raise MetaUnavailable("upstream auth")
        if status != 200:
            raise MetaBadResponse(f"upstream status {status}")
        if len(data) > MAX_RESPONSE_BYTES:
            raise MetaBadResponse("response too large")
        try:
            payload = json.loads(data)
            text = payload["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise MetaBadResponse("unexpected response shape") from exc
        if not isinstance(text, str):
            raise MetaBadResponse("reply was not text")
        usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
        usage = {k: v for k, v in usage.items() if isinstance(v, int)}
        model = payload.get("model") if isinstance(payload.get("model"), str) else self.text_model
        return ChatResult(text=text, model=model, latency_ms=latency_ms, usage=usage)
