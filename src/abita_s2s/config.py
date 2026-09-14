"""Read runtime credentials without exposing them in logs or repr."""

import os
from dataclasses import dataclass, field
from urllib.parse import urlsplit


@dataclass(frozen=True)
class Config:
    openai_api_key: str = field(repr=False)
    voice: str = "gleam"
    knowledge_url: str | None = None
    product_secret: str | None = field(default=None, repr=False)
    middleware_url: str | None = None
    middleware_token: str | None = field(default=None, repr=False)
    staff_tasks_url: str | None = None


def load_config() -> Config:
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        raise ValueError("Missing required environment variable: OPENAI_API_KEY")
    # LiveKit CLI validates its URL, API key, and secret in room modes.
    voice = os.environ.get("GPT_LIVE_VOICE", "gleam").strip()
    if not voice:
        raise ValueError("GPT_LIVE_VOICE must not be empty")
    knowledge_url = os.environ.get("ACUITY_PRODUCT_KNOWLEDGE_URL", "").strip() or None
    product_secret = (
        os.environ.get("ABITA_EYE_GROUP_PRODUCT_SERVICE_SECRET", "").strip() or None
    )
    handoff_url = os.environ.get("ACUITY_PRODUCT_HANDOFF_URL", "").strip().rstrip("/")
    staff_tasks_url = None
    if handoff_url:
        if not handoff_url.endswith("/v1/handoffs"):
            raise ValueError("ACUITY_PRODUCT_HANDOFF_URL must end in /v1/handoffs")
        staff_tasks_url = handoff_url.removesuffix("/v1/handoffs") + "/v1/tasks"
        target = urlsplit(staff_tasks_url)
        if (
            not target.hostname
            or target.username
            or target.password
            or target.query
            or target.fragment
            or not (
                target.scheme == "https"
                or (
                    target.scheme == "http"
                    and target.hostname in ("localhost", "127.0.0.1", "::1")
                )
            )
        ):
            raise ValueError(
                "ACUITY_PRODUCT_HANDOFF_URL must use HTTPS (HTTP only on loopback)"
            )
    if bool(knowledge_url or staff_tasks_url) != bool(product_secret):
        raise ValueError(
            "Set ABITA_EYE_GROUP_PRODUCT_SERVICE_SECRET with ACUITY_PRODUCT_KNOWLEDGE_URL or ACUITY_PRODUCT_HANDOFF_URL"
        )
    if knowledge_url:
        url = urlsplit(knowledge_url)
        if (
            not url.hostname
            or url.username is not None
            or url.password is not None
            or not (
                url.scheme == "https"
                or (
                    url.scheme == "http"
                    and url.hostname in ("localhost", "127.0.0.1", "::1")
                )
            )
        ):
            raise ValueError(
                "ACUITY_PRODUCT_KNOWLEDGE_URL must use HTTPS (HTTP only on loopback)"
            )
    middleware_url = os.environ.get("AMD_API_URL", "").strip() or None
    middleware_token = os.environ.get("AMD_API_TOKEN", "").strip() or None
    if bool(middleware_url) != bool(middleware_token):
        raise ValueError("Set both AMD_API_URL and AMD_API_TOKEN")
    if middleware_url:
        url = urlsplit(middleware_url)
        if (
            not url.hostname
            or url.username
            or url.password
            or url.query
            or url.fragment
            or not (
                url.scheme == "https"
                or (
                    url.scheme == "http"
                    and url.hostname in ("localhost", "127.0.0.1", "::1")
                )
            )
        ):
            raise ValueError("AMD_API_URL must use HTTPS (HTTP only on loopback)")
    return Config(
        api_key,
        voice,
        knowledge_url,
        product_secret,
        middleware_url,
        middleware_token,
        staff_tasks_url,
    )
