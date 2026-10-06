# syntax=docker/dockerfile:1
FROM ghcr.io/astral-sh/uv:0.12.23 AS uv
FROM python:3.13.16-slim-bookworm
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN --mount=from=uv,source=/uv,target=/bin/uv \
    --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-install-project
COPY README.md ./
COPY src ./src
RUN --mount=from=uv,source=/uv,target=/bin/uv \
    --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-editable \
    && test -f /app/.venv/lib/python3.13/site-packages/abita_s2s/release.json \
    && useradd --create-home --uid 10001 agent
USER 10001
CMD ["/app/.venv/bin/abita-s2s", "start"]
