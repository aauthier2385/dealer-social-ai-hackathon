"""Settings come only from the environment (.env locally, Render settings in production).

Nothing secret is ever hardcoded, and the Settings object never prints its secrets.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass

DEFAULT_BASE_URL = "https://api.meta.ai/v1"
DEFAULT_TEXT_MODEL = "muse-spark-1.3"
DEFAULT_VOICE_MODEL = "muse-voice-transcribe-1.0"
REASONING_EFFORTS = ("minimal", "low", "medium", "high")
MIN_TOKEN_LENGTH = 32


class ConfigError(ValueError):
    """Raised at startup when a setting is missing or unsafe. The service refuses to start."""


@dataclass(frozen=True, repr=False)
class Settings:
    service_token: str
    meta_enabled: bool
    meta_api_key: str
    meta_base_url: str
    text_model: str
    voice_model: str
    reasoning_effort: str
    max_concurrency: int
    per_client_per_minute: int
    global_per_minute: int
    global_per_day: int
    voice_per_client_per_minute: int
    voice_global_per_day: int
    playground_enabled: bool

    def __repr__(self) -> str:  # never format key material
        return (
            f"Settings(meta_enabled={self.meta_enabled}, text_model={self.text_model!r}, "
            f"playground_enabled={self.playground_enabled}, secrets=<redacted>)"
        )


def _bool(env: Mapping[str, str], name: str, default: bool) -> bool:
    raw = env.get(name, "").strip().lower()
    if raw == "":
        return default
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    raise ConfigError(f"{name} must be true or false")


def _int(env: Mapping[str, str], name: str, default: int, low: int, high: int) -> int:
    raw = env.get(name, "").strip()
    if raw == "":
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a whole number") from exc
    if not low <= value <= high:
        raise ConfigError(f"{name} must be between {low} and {high}")
    return value


def _model(env: Mapping[str, str], name: str, default: str) -> str:
    model = env.get(name, default).strip()
    if not model:
        raise ConfigError(f"{name} must not be empty")
    if "contributor" in model.lower():
        # Contributor variants let Meta train on submitted content. Customer data never goes there.
        raise ConfigError("Contributor model variants are not allowed for customer data")
    return model


def load_settings(env: Mapping[str, str] | None = None) -> Settings:
    env = os.environ if env is None else env

    token = env.get("SERVICE_TOKEN", "").strip()
    if len(token) < MIN_TOKEN_LENGTH or any(c.isspace() for c in token):
        raise ConfigError(
            "SERVICE_TOKEN must be at least 32 characters with no spaces. Generate one with: "
            'py -c "import secrets; print(secrets.token_urlsafe(32))"'
        )

    enabled = _bool(env, "META_AI_ENABLED", False)
    key = env.get("MODEL_API_KEY", "").strip()
    if enabled and (not key or any(c.isspace() for c in key)):
        raise ConfigError("Set a valid MODEL_API_KEY when META_AI_ENABLED=true")

    base_url = env.get("META_BASE_URL", DEFAULT_BASE_URL).strip().rstrip("/")
    if not base_url.startswith("https://"):
        raise ConfigError("META_BASE_URL must use https://")

    model = _model(env, "META_TEXT_MODEL", DEFAULT_TEXT_MODEL)
    voice_model = _model(env, "META_VOICE_MODEL", DEFAULT_VOICE_MODEL)

    effort = env.get("META_REASONING_EFFORT", "low").strip().lower()
    if effort not in REASONING_EFFORTS:
        raise ConfigError(f"META_REASONING_EFFORT must be one of {', '.join(REASONING_EFFORTS)}")

    return Settings(
        service_token=token,
        meta_enabled=enabled,
        meta_api_key=key,
        meta_base_url=base_url,
        text_model=model,
        voice_model=voice_model,
        reasoning_effort=effort,
        max_concurrency=_int(env, "MAX_CONCURRENT_META_CALLS", 4, 1, 32),
        per_client_per_minute=_int(env, "RATE_LIMIT_PER_CLIENT_PER_MINUTE", 20, 1, 1000),
        global_per_minute=_int(env, "RATE_LIMIT_GLOBAL_PER_MINUTE", 60, 1, 10000),
        global_per_day=_int(env, "RATE_LIMIT_GLOBAL_PER_DAY", 1000, 1, 1000000),
        voice_per_client_per_minute=_int(env, "VOICE_LIMIT_PER_CLIENT_PER_MINUTE", 6, 1, 100),
        voice_global_per_day=_int(env, "VOICE_LIMIT_GLOBAL_PER_DAY", 300, 1, 100000),
        playground_enabled=_bool(env, "PLAYGROUND_ENABLED", True),
    )
