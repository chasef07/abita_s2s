"""The one HTTP path: typed request out, parsed reply or transport fault back."""

import asyncio
from collections.abc import Awaitable
from typing import Generic, Literal, NamedTuple, TypeVar

import httpx
from pydantic import ConfigDict, TypeAdapter

from abita_s2s.records import Record

Body = TypeVar("Body", covariant=True)
Fault = Literal["not_configured", "timeout", "network"]


class Request(Record):
    """A request body; unknown fields fail at construction, never on the wire."""

    model_config = ConfigDict(extra="forbid")


class Reply(NamedTuple, Generic[Body]):
    """An HTTP answer; `body` is None when it does not parse as the expected reply."""

    code: int
    body: Body | None

    @property
    def ok(self) -> bool:
        return 200 <= self.code < 300

    @property
    def retryable(self) -> bool:
        return self.code in (408, 429) or self.code >= 500


async def post[T](
    client: httpx.AsyncClient,
    url: str,
    headers: dict[str, str],
    request: Request,
    reply: TypeAdapter[T],
    timeout: float,
    cap: float | None,
) -> Reply[T] | Fault:
    """`timeout` bounds each connect, write and read; `cap` bounds the whole exchange."""
    return await _exchange(
        client.post(
            url,
            headers=headers,
            json=request.model_dump(exclude_none=True),
            timeout=timeout,
            follow_redirects=False,
        ),
        reply,
        cap,
    )


async def get[T](
    client: httpx.AsyncClient,
    url: str,
    headers: dict[str, str],
    params: dict[str, str],
    reply: TypeAdapter[T],
    timeout: float,
    cap: float | None,
) -> Reply[T] | Fault:
    """`timeout` bounds each connect, write and read; `cap` bounds the whole exchange."""
    return await _exchange(
        client.get(
            url,
            headers=headers,
            params=params,
            timeout=timeout,
            follow_redirects=False,
        ),
        reply,
        cap,
    )


async def _exchange[T](
    sending: Awaitable[httpx.Response], reply: TypeAdapter[T], cap: float | None
) -> Reply[T] | Fault:
    try:
        async with asyncio.timeout(cap):
            response = await sending
    except (TimeoutError, httpx.TimeoutException):
        return "timeout"
    except httpx.HTTPError:
        return "network"
    try:
        body = reply.validate_python(response.json())
    except ValueError:
        body = None
    return Reply(response.status_code, body)
