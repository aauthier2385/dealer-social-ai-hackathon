"""Dealer Social AI service.

Run locally:   uvicorn app.main:create_app --factory --host 127.0.0.1 --port 8090 --env-file .env
"""

from __future__ import annotations

import logging
from pathlib import Path

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from app import __version__, audio, memory
from app.config import Settings, load_settings
from app.meta_client import MetaClient, MetaError, Transport
from app.ratelimit import RateLimiter
from app.security import (
    BadRequest,
    RequestTooLarge,
    client_key,
    read_bytes,
    read_json,
    token_ok,
)

log = logging.getLogger("dsai")

STATIC = Path(__file__).parent / "static"
MAX_PING_PROMPT = 500
MAX_JSON_BODY = 16 * 1024

PING_SYSTEM_PROMPT = (
    "You are the connection test for Dealer Social AI, an assistant for car dealership staff. "
    "Reply in one short, friendly sentence."
)

PLAYGROUND_CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; "
    "img-src 'self' data:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
)


class SecurityHeaders:
    """Adds protective headers to every response."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                present = {name.lower() for name, _ in headers}
                defaults = [
                    (b"x-content-type-options", b"nosniff"),
                    (b"referrer-policy", b"no-referrer"),
                    (b"x-frame-options", b"DENY"),
                    (b"strict-transport-security", b"max-age=31536000"),
                    (b"content-security-policy", b"default-src 'none'; frame-ancestors 'none'"),
                ]
                headers += [(k, v) for k, v in defaults if k not in present]
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, send_with_headers)


META_MESSAGES = {
    "ai_disabled": "Meta AI is turned off (META_AI_ENABLED=false).",
    "ai_busy": "The AI is busy with other requests. Try again in a few seconds.",
    "ai_unavailable": "The AI service is temporarily unavailable. Try again shortly.",
    "ai_bad_response": "The AI returned something unexpected. Try again.",
    "invalid_input": "That request could not be sent to the AI.",
}
AUDIO_TYPES = {"audio/wav", "audio/x-wav", "audio/wave", "audio/vnd.wave"}


def _meta_error(exc: MetaError) -> JSONResponse:
    return _error(exc.http_status, exc.code, META_MESSAGES[exc.code], exc.retry_after)


def _error(status: int, code: str, message: str, retry_after: int | None = None) -> JSONResponse:
    headers = {"Cache-Control": "no-store"}
    if retry_after:
        headers["Retry-After"] = str(retry_after)
    if status == 401:
        headers["WWW-Authenticate"] = "Bearer"
    return JSONResponse({"error": code, "message": message}, status_code=status, headers=headers)


def _configure_logging() -> None:
    """Show this service's event lines (never content) alongside uvicorn's own log."""
    logger = logging.getLogger("dsai")
    logger.setLevel(logging.INFO)
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(levelname)s:     %(name)s %(message)s"))
        logger.addHandler(handler)


def create_app(settings: Settings | None = None, transport: Transport | None = None) -> Starlette:
    _configure_logging()
    settings = settings or load_settings()
    meta = MetaClient(settings, transport)
    limiter = RateLimiter()
    playground_html = (STATIC / "playground.html").read_bytes()
    playground_js = (STATIC / "playground.js").read_bytes()
    playground_css = (STATIC / "playground.css").read_bytes()

    async def health(request: Request) -> Response:
        return JSONResponse(
            {
                "status": "ok",
                "service": "dealer-social-ai",
                "version": __version__,
                "meta_enabled": meta.enabled,
            },
            headers={"Cache-Control": "no-store"},
        )

    async def ping_model(request: Request) -> Response:
        if not token_ok(request, settings.service_token):
            return _error(401, "unauthorized", "Missing or wrong service token.")
        allowed, wait = limiter.allow_all(
            [
                (client_key(request), settings.per_client_per_minute, 60),
                ("global-minute", settings.global_per_minute, 60),
                ("global-day", settings.global_per_day, 86400),
            ]
        )
        if not allowed:
            return _error(429, "rate_limited", "Too many requests. Try again shortly.", wait)
        try:
            data = await read_json(request, MAX_JSON_BODY)
        except RequestTooLarge:
            return _error(413, "too_large", "Request body is too large.")
        except BadRequest as exc:
            return _error(400, "invalid_input", str(exc))
        prompt = data.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            return _error(400, "invalid_input", "Send a non-empty 'prompt' string.")
        if len(prompt) > MAX_PING_PROMPT:
            return _error(400, "invalid_input", f"Keep the prompt under {MAX_PING_PROMPT} chars.")
        try:
            result = await meta.chat(
                [
                    {"role": "system", "content": PING_SYSTEM_PROMPT},
                    {"role": "user", "content": prompt.strip()},
                ],
                reasoning_effort="minimal",
            )
        except MetaError as exc:
            return _meta_error(exc)
        return JSONResponse(
            {
                "reply": result.text,
                "model": result.model,
                "latency_ms": result.latency_ms,
                "usage": result.usage,
            },
            headers={"Cache-Control": "no-store"},
        )

    async def voice_note_memory(request: Request) -> Response:
        """Feature 1: upload a voice note (WAV), get relationship facts tied to their sources."""
        if not token_ok(request, settings.service_token):
            return _error(401, "unauthorized", "Missing or wrong service token.")
        content_type = request.headers.get("content-type", "").split(";")[0].strip().lower()
        if content_type not in AUDIO_TYPES:
            return _error(415, "unsupported_media", "Send the voice note as audio/wav.")
        allowed, wait = limiter.allow_all(
            [
                ("voice-" + client_key(request), settings.voice_per_client_per_minute, 60),
                ("voice-global-day", settings.voice_global_per_day, 86400),
                ("global-minute", settings.global_per_minute, 60),
                ("global-day", settings.global_per_day, 86400),
            ]
        )
        if not allowed:
            return _error(429, "rate_limited", "Too many voice notes. Try again shortly.", wait)
        try:
            wav = await read_bytes(request, audio.MAX_BYTES)
            info = audio.check_wav(wav)
        except RequestTooLarge:
            return _error(413, "too_large", "The recording is too long. Keep it under 5 minutes.")
        except audio.AudioError as exc:
            return _error(400, "invalid_audio", str(exc))
        try:
            result = await memory.remember(meta, wav, info.seconds)
        except memory.NoSpeech:
            return _error(422, "no_speech", "No speech was found in the recording.")
        except MetaError as exc:
            return _meta_error(exc)
        return JSONResponse(result, headers={"Cache-Control": "no-store"})

    def static(body: bytes, media_type: str):
        async def handler(request: Request) -> Response:
            return Response(
                body,
                media_type=media_type,
                headers={"Content-Security-Policy": PLAYGROUND_CSP, "Cache-Control": "no-cache"},
            )

        return handler

    routes = [
        Route("/health", health, methods=["GET"]),
        Route("/v1/ping-model", ping_model, methods=["POST"]),
        Route("/v1/memory/voice-note", voice_note_memory, methods=["POST"]),
    ]
    if settings.playground_enabled:
        routes += [
            Route("/playground", static(playground_html, "text/html; charset=utf-8")),
            Route("/playground.js", static(playground_js, "text/javascript; charset=utf-8")),
            Route("/playground.css", static(playground_css, "text/css; charset=utf-8")),
        ]

    log.info(
        "starting dealer-social-ai version=%s meta_enabled=%s", __version__, settings.meta_enabled
    )
    return Starlette(routes=routes, middleware=[Middleware(SecurityHeaders)])
