"""Request checks shared by every protected route."""

from __future__ import annotations

import hmac
import json
import re

from starlette.requests import Request

_USER_REF = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


class RequestTooLarge(Exception):
    pass


class BadRequest(Exception):
    pass


def token_ok(request: Request, expected: str) -> bool:
    """Constant-time check of `Authorization: Bearer <SERVICE_TOKEN>`."""
    header = request.headers.get("authorization", "")
    scheme, _, supplied = header.partition(" ")
    if scheme.lower() != "bearer" or not supplied:
        return False
    return hmac.compare_digest(supplied.strip().encode(), expected.encode())


def client_key(request: Request) -> str:
    """Who to rate-limit. The demo app sends an opaque user reference; otherwise use the IP."""
    ref = request.headers.get("x-user-ref", "")
    if _USER_REF.fullmatch(ref):
        return f"user:{ref}"
    host = request.client.host if request.client else "unknown"
    return f"ip:{host}"


async def read_json(request: Request, limit: int) -> dict:
    content_type = request.headers.get("content-type", "").split(";")[0].strip().lower()
    if content_type != "application/json":
        raise BadRequest("Send JSON with Content-Type: application/json")
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > limit:
        raise RequestTooLarge()
    body = bytearray()
    async for chunk in request.stream():
        body += chunk
        if len(body) > limit:
            raise RequestTooLarge()
    try:
        data = json.loads(body)
    except ValueError as exc:
        raise BadRequest("Body is not valid JSON") from exc
    if not isinstance(data, dict):
        raise BadRequest("Body must be a JSON object")
    return data
