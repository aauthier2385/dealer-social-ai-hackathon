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

from app import __version__, audio, handoff, memory, photo, planner, risk, truckiversary
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
MAX_PLAN_BODY = 64 * 1024
MAX_RISK_BODY = 512 * 1024  # a store's customer list
FAVICON_SVG = (
    b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">'
    b'<rect width="32" height="32" rx="7" fill="#1d4ed8"/>'
    b'<path d="M8 21c4 4 12 4 16 0M11 12h.01M21 12h.01" stroke="#fff" stroke-width="3" '
    b'stroke-linecap="round" fill="none"/></svg>'
)

PING_SYSTEM_PROMPT = (
    "You are the connection test for Dealer Social AI, an assistant for car dealership staff. "
    "Reply in one short, friendly sentence."
)

PLAYGROUND_CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; "
    "img-src 'self' data: blob:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
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
PHOTO_TYPES = {"image/jpeg", "image/png", "image/webp"}


def _meta_error(exc: MetaError) -> JSONResponse:
    # The reason is our own wording (e.g. "no image in the response"), never customer content.
    logging.getLogger("dsai").warning("request_failed code=%s reason=%s", exc.code, exc)
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

    async def favicon(request: Request) -> Response:
        return Response(
            FAVICON_SVG, media_type="image/svg+xml", headers={"Cache-Control": "max-age=86400"}
        )

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

    async def plan_touchpoints(request: Request) -> Response:
        """Feature 2: verified facts -> follow-up plan on the dealership's cadence."""
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
            data = await read_json(request, MAX_PLAN_BODY)
            plan = planner.parse_input(data)
        except RequestTooLarge:
            return _error(413, "too_large", "Request body is too large.")
        except (BadRequest, planner.PlanInputError) as exc:
            return _error(400, "invalid_input", str(exc))
        try:
            result = await planner.plan_touchpoints(meta, plan)
        except MetaError as exc:
            return _meta_error(exc)
        return JSONResponse(result, headers={"Cache-Control": "no-store"})

    async def handoff_brief(request: Request) -> Response:
        """Feature 3: a customer's verified facts -> handoff brief and warm intro."""
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
            data = await read_json(request, MAX_PLAN_BODY)
            customer, since, from_sp = handoff.parse_input(data)
        except RequestTooLarge:
            return _error(413, "too_large", "Request body is too large.")
        except (BadRequest, planner.PlanInputError) as exc:
            return _error(400, "invalid_input", str(exc))
        try:
            result = await handoff.brief(meta, customer, since, from_sp)
        except MetaError as exc:
            return _meta_error(exc)
        return JSONResponse(result, headers={"Cache-Control": "no-store"})

    async def privacy_blur(request: Request) -> Response:
        """Feature 5: a store photo -> the same photo with bystanders' faces and plates blurred."""
        if not token_ok(request, settings.service_token):
            return _error(401, "unauthorized", "Missing or wrong service token.")
        content_type = request.headers.get("content-type", "").split(";")[0].strip().lower()
        if content_type not in PHOTO_TYPES:
            return _error(415, "unsupported_media", "Send a JPEG, PNG or WebP photo.")
        allowed, wait = limiter.allow_all(
            [
                ("photo-" + client_key(request), settings.photo_per_client_per_minute, 60),
                ("photo-global-day", settings.photo_global_per_day, 86400),
                ("global-minute", settings.global_per_minute, 60),
                ("global-day", settings.global_per_day, 86400),
            ]
        )
        if not allowed:
            return _error(429, "rate_limited", "Too many photos. Try again shortly.", wait)
        q = request.query_params
        try:
            data = await read_bytes(request, photo.MAX_UPLOAD_BYTES)
            result = await photo.privacy_blur(
                meta,
                data,
                keep=q.get("keep", "")[:400],
                blur_points=q.get("blur", "")[:400],
                blur_all_faces=q.get("blur_all_faces", "") in ("1", "true"),
            )
        except RequestTooLarge:
            return _error(413, "too_large", "That photo is too large. Keep it under 12 MB.")
        except photo.PhotoError as exc:
            return _error(400, "invalid_photo", str(exc))
        except MetaError as exc:
            return _meta_error(exc)
        return JSONResponse(result, headers={"Cache-Control": "no-store"})

    async def truckiversary_card(request: Request) -> Response:
        """Feature 6: a delivery photo -> an anniversary post (card + caption)."""
        if not token_ok(request, settings.service_token):
            return _error(401, "unauthorized", "Missing or wrong service token.")
        allowed, wait = limiter.allow_all(
            [
                ("photo-" + client_key(request), settings.photo_per_client_per_minute, 60),
                ("photo-global-day", settings.photo_global_per_day, 86400),
                ("global-minute", settings.global_per_minute, 60),
                ("global-day", settings.global_per_day, 86400),
            ]
        )
        if not allowed:
            return _error(429, "rate_limited", "Too many requests. Try again shortly.", wait)
        try:
            data = await read_json(request, truckiversary.MAX_PHOTO_CHARS + 64 * 1024)
            card = truckiversary.parse_input(data)
            result = await truckiversary.make_card(meta, card)
        except RequestTooLarge:
            return _error(413, "too_large", "That photo is too large. Keep it under 12 MB.")
        except (BadRequest, planner.PlanInputError) as exc:
            return _error(400, "invalid_input", str(exc))
        except photo.PhotoError as exc:
            return _error(400, "invalid_photo", str(exc))
        except MetaError as exc:
            return _meta_error(exc)
        return JSONResponse(result, headers={"Cache-Control": "no-store"})

    async def risk_brief(request: Request) -> Response:
        """Feature 6: a store's customer list -> who needs attention this week, and why."""
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
            data = await read_json(request, MAX_RISK_BODY)
            store, as_of, top, use_ai, customers = risk.parse_input(data)
        except RequestTooLarge:
            return _error(413, "too_large", "Request body is too large.")
        except (BadRequest, planner.PlanInputError) as exc:
            return _error(400, "invalid_input", str(exc))
        try:
            result = await risk.brief(meta, store, as_of, top, use_ai, customers)
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
        Route("/v1/plan/touchpoints", plan_touchpoints, methods=["POST"]),
        Route("/v1/handoff/brief", handoff_brief, methods=["POST"]),
        Route("/v1/risk/brief", risk_brief, methods=["POST"]),
        Route("/v1/photo/privacy-blur", privacy_blur, methods=["POST"]),
        Route("/v1/truckiversary", truckiversary_card, methods=["POST"]),
        Route("/favicon.ico", favicon, methods=["GET"]),
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
