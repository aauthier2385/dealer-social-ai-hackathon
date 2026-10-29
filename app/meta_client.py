"""The one place this service talks to the Meta Model API.

Safety rules, mirroring The Dealer Social's moderation module:
* a hard time limit on every call and a cap on how much response we will read;
* at most N calls at once; extra calls are turned away instead of queued;
* redirects are never followed, so the API key cannot be forwarded elsewhere;
* prompts, replies and keys are never logged. Only event names, status codes and timings are.
"""

from __future__ import annotations

import base64
import json
import logging
import secrets
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
TIMEOUT_SECONDS = 45.0
TRANSCRIBE_TIMEOUT_SECONDS = 120.0
SEGMENT_TIMEOUT_SECONDS = 60.0
SEGMENT_MAX_RESPONSE_BYTES = 4 * 1024 * 1024  # masks for a crowded photo
IMAGE_TIMEOUT_SECONDS = 150.0
IMAGE_MAX_RESPONSE_BYTES = 16 * 1024 * 1024  # one generated image, base64
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

    @property
    def voice_model(self) -> str:
        return self._settings.voice_model

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

    async def _send(
        self,
        event: str,
        path: str,
        body: bytes,
        content_type: str,
        timeout: float,
        max_bytes: int = MAX_RESPONSE_BYTES,
    ) -> dict:
        """POST to the Meta Model API and return parsed JSON. Logs only event, status, timing."""
        if not self.enabled:
            raise MetaDisabled()
        # No awaits between the check and the increment, so this is safe on one event loop.
        if self._active >= self._settings.max_concurrency:
            raise MetaBusy("too many calls at once", retry_after=5)
        self._active += 1
        started = time.monotonic()
        try:
            url = f"{self._settings.meta_base_url}{path}"
            headers = {
                "Authorization": f"Bearer {self._settings.meta_api_key}",
                "Content-Type": content_type,
                "Accept": "application/json",
                "User-Agent": "dealer-social-ai/0.2",
            }
            try:
                status, response_headers, data = await anyio.to_thread.run_sync(
                    self._transport, url, headers, body, timeout, max_bytes
                )
            except MetaUnavailable:
                waited = int((time.monotonic() - started) * 1000)
                log.warning("%s network_error latency_ms=%s (timeout or no route)", event, waited)
                raise
        finally:
            self._active -= 1
        latency_ms = int((time.monotonic() - started) * 1000)
        log.info("%s status=%s latency_ms=%s", event, status, latency_ms)

        if status == 429 or status >= 500:
            raise MetaUnavailable("upstream busy", retry_after=_retry_after(response_headers))
        if status in (401, 403):
            log.error("meta_auth_failed status=%s (check MODEL_API_KEY)", status)
            raise MetaUnavailable("upstream auth")
        if status == 413:
            raise MetaInputError("upstream says the upload is too large")
        if status != 200:
            raise MetaBadResponse(f"upstream status {status}")
        if len(data) > max_bytes:
            raise MetaBadResponse("response too large")
        try:
            payload = json.loads(data)
        except ValueError as exc:
            raise MetaBadResponse("response was not JSON") from exc
        if not isinstance(payload, dict):
            raise MetaBadResponse("unexpected response shape")
        payload["_latency_ms"] = latency_ms
        return payload

    async def chat(
        self,
        messages: list[dict],
        *,
        schema: dict | None = None,
        schema_name: str = "result",
        reasoning_effort: str | None = None,
    ) -> ChatResult:
        """Muse Spark chat completion, optionally constrained to a JSON schema."""
        self._validate(messages)
        request: dict = {"model": self._settings.text_model, "messages": messages}
        if schema is not None:
            request["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": schema_name, "schema": schema},
            }
        effort = reasoning_effort or self._settings.reasoning_effort
        if effort:
            request["reasoning_effort"] = effort
        payload = await self._send(
            "meta_chat",
            "/chat/completions",
            json.dumps(request).encode(),
            "application/json",
            TIMEOUT_SECONDS,
        )
        try:
            text = payload["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise MetaBadResponse("unexpected response shape") from exc
        if not isinstance(text, str):
            raise MetaBadResponse("reply was not text")
        model = payload.get("model") if isinstance(payload.get("model"), str) else self.text_model
        return ChatResult(
            text=text, model=model, latency_ms=payload["_latency_ms"], usage=_usage(payload)
        )

    async def chat_json(self, messages: list[dict], schema: dict, schema_name: str, **kw):
        """Chat constrained to `schema`; returns (parsed_dict, ChatResult)."""
        result = await self.chat(messages, schema=schema, schema_name=schema_name, **kw)
        try:
            data = json.loads(result.text)
        except ValueError as exc:
            raise MetaBadResponse("structured output was not JSON") from exc
        if not isinstance(data, dict):
            raise MetaBadResponse("structured output was not an object")
        return data, result

    async def transcribe(self, wav: bytes, mode: str = "DIARIZATION") -> dict:
        """Muse Voice Transcribe, one-shot upload of a mono 16-bit WAV.

        Returns {"transcript", "turns": [...], "audioDurationMs", "_latency_ms"}.
        """
        request = {"mode": mode, "model": self._settings.voice_model, "audioEncoding": "WAV"}
        boundary = "dsai-" + secrets.token_hex(16)
        body = b"".join(
            [
                f"--{boundary}\r\n".encode(),
                b'Content-Disposition: form-data; name="request"\r\n',
                b"Content-Type: application/json\r\n\r\n",
                json.dumps(request).encode(),
                f"\r\n--{boundary}\r\n".encode(),
                b'Content-Disposition: form-data; name="audio"; filename="voice-note.wav"\r\n',
                b"Content-Type: audio/wav\r\n\r\n",
                wav,
                f"\r\n--{boundary}--\r\n".encode(),
            ]
        )
        payload = await self._send(
            "meta_transcribe",
            "/asr/transcribe",
            body,
            f"multipart/form-data; boundary={boundary}",
            TRANSCRIBE_TIMEOUT_SECONDS,
        )
        transcript = payload.get("transcript")
        turns = payload.get("turns")
        if not isinstance(transcript, str) and not isinstance(turns, list):
            raise MetaBadResponse("transcription had no text")
        return payload

    @property
    def sam_model(self) -> str:
        return self._settings.sam_model

    async def segment(self, image: bytes, mime: str, concept: str) -> ChatResult:
        """SAM 3.1 image segmentation for one short noun phrase ("face", "license plate").

        Returns the raw segmentation record text (parsed by `meta_sam_parser`), the model and
        timing. The image is sent inline as a data URL and is not stored (store=false).
        """
        phrase = " ".join(concept.split())
        if not phrase or len(phrase) > 40:
            raise MetaInputError("concept must be a short noun phrase")
        request = {
            "model": self._settings.sam_model,
            "input": [
                {
                    "type": "message",
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": phrase},
                        {
                            "type": "input_image",
                            "image_url": f"data:{mime};base64,{base64.b64encode(image).decode()}",
                        },
                    ],
                }
            ],
            "metadata": {"mask_encoding": "one_bit"},
            "store": False,
        }
        payload = await self._send(
            "meta_segment",
            "/responses",
            json.dumps(request).encode(),
            "application/json",
            SEGMENT_TIMEOUT_SECONDS,
            max_bytes=SEGMENT_MAX_RESPONSE_BYTES,
        )
        status = payload.get("status")
        if status not in (None, "completed"):
            raise MetaBadResponse(f"segmentation did not complete: {status}")
        text = payload.get("output_text")
        if not isinstance(text, str):
            parts = []
            for item in payload.get("output") or []:
                if not isinstance(item, dict):
                    continue
                for part in item.get("content") or []:
                    if not isinstance(part, dict):
                        continue
                    if part.get("type") == "refusal":
                        raise MetaBadResponse("segmentation was refused")
                    if part.get("type") == "output_text" and isinstance(part.get("text"), str):
                        parts.append(part["text"])
            if not parts and not isinstance(payload.get("output"), list):
                raise MetaBadResponse("segmentation had no output")
            text = "".join(parts)
        model = payload.get("model") if isinstance(payload.get("model"), str) else self.sam_model
        return ChatResult(
            text=text, model=model, latency_ms=payload["_latency_ms"], usage=_usage(payload)
        )

    @property
    def image_model(self) -> str:
        return self._settings.image_model

    async def generate_image(self, prompt: str) -> tuple[bytes, ChatResult]:
        """Muse Image: one image from a text prompt. Returns (image bytes, result metadata).

        store=false, so no conversation state is kept on Meta's side.
        """
        prompt = prompt.strip()
        if not prompt or len(prompt) > MAX_PROMPT_CHARS:
            raise MetaInputError("bad image prompt")
        request = {"model": self._settings.image_model, "input": prompt, "store": False}
        payload = await self._send(
            "meta_image",
            "/responses",
            json.dumps(request).encode(),
            "application/json",
            IMAGE_TIMEOUT_SECONDS,
            max_bytes=IMAGE_MAX_RESPONSE_BYTES,
        )
        status = payload.get("status")
        if status not in (None, "completed"):
            raise MetaBadResponse(f"image generation did not complete: {status}")
        encoded = None
        for item in payload.get("output") or []:
            if not isinstance(item, dict):
                continue
            if item.get("type") == "image_generation_call" and isinstance(item.get("result"), str):
                encoded = item["result"]
                break
            for part in item.get("content") or []:
                if isinstance(part, dict) and part.get("type") == "refusal":
                    raise MetaBadResponse("image generation was refused")
        if not encoded:
            raise MetaBadResponse("no image in the response")
        try:
            image = base64.b64decode(encoded, validate=True)
        except ValueError as exc:
            raise MetaBadResponse("image was not valid base64") from exc
        model = payload.get("model") if isinstance(payload.get("model"), str) else self.image_model
        return image, ChatResult(
            text="", model=model, latency_ms=payload["_latency_ms"], usage=_usage(payload)
        )


def _usage(payload: dict) -> dict:
    """Keep integer token counts, including reasoning tokens (they are billed as output)."""
    raw = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
    usage = {k: v for k, v in raw.items() if isinstance(v, int)}
    for key in ("completion_tokens_details", "output_tokens_details"):  # chat / Responses API
        details = raw.get(key)
        if isinstance(details, dict) and isinstance(details.get("reasoning_tokens"), int):
            usage["reasoning_tokens"] = details["reasoning_tokens"]
    return usage
