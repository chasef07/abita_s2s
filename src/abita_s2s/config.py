"""Read runtime credentials without exposing them in logs or repr."""

import os
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Config:
    openai_api_key: str = field(repr=False)
    voice: str = "gleam"


def load_config() -> Config:
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        raise ValueError("Missing required environment variable: OPENAI_API_KEY")
    # LiveKit CLI validates its URL, API key, and secret in room modes.
    voice = os.environ.get("GPT_LIVE_VOICE", "gleam").strip()
    if not voice:
        raise ValueError("GPT_LIVE_VOICE must not be empty")
    return Config(openai_api_key=api_key, voice=voice)


@dataclass(frozen=True)
class MiddlewareConfig:
    base_url: str
    auth_token: str = field(repr=False)
    office_override: str | None = None


def load_middleware_config(*, console: bool = False) -> MiddlewareConfig:
    """Default to sandbox; production access must be selected explicitly."""
    from urllib.parse import urlsplit

    mode = os.environ.get("ABITA_MIDDLEWARE_ENV", "sandbox").strip()
    if mode not in ("sandbox", "production"):
        raise ValueError("ABITA_MIDDLEWARE_ENV must be sandbox or production")
    if mode == "production" and (
        console or os.environ.get("LIVEKIT_AGENT_DEPLOYMENT", "").strip()
    ):
        raise ValueError("Console and named deployments require sandbox middleware")

    prefix = "SANDBOX_AMD" if mode == "sandbox" else "AMD"
    url = os.environ.get(f"{prefix}_API_URL", "").strip().rstrip("/")
    token = os.environ.get(f"{prefix}_API_TOKEN", "").strip()
    if not url or not token:
        raise ValueError(f"{prefix}_API_URL and {prefix}_API_TOKEN are required")

    def origin(value: str) -> tuple[str, str, int]:
        try:
            parsed = urlsplit(value)
            if (
                parsed.scheme != "https"
                or not parsed.hostname
                or parsed.username is not None
                or parsed.password is not None
                or parsed.query
                or parsed.fragment
            ):
                raise ValueError
            return parsed.scheme, parsed.hostname, parsed.port or 443
        except ValueError:
            raise ValueError(
                "Middleware URL must be HTTPS without credentials, query, or fragment"
            ) from None

    selected_origin = origin(url)
    if mode == "sandbox":
        production_url = os.environ.get("AMD_API_URL", "").strip()
        production_token = os.environ.get("AMD_API_TOKEN", "").strip()
        if production_url and selected_origin == origin(production_url):
            raise ValueError(
                "Sandbox and production middleware must use separate origins"
            )
        if production_token and token == production_token:
            raise ValueError(
                "Sandbox and production middleware must use separate tokens"
            )
    return MiddlewareConfig(url, token, "spring_hill" if mode == "sandbox" else None)
