"""Authenticated middleware transport shared by the patient, registration and scheduling adapters."""

from typing import Annotated

import httpx
from pydantic import AfterValidator, TypeAdapter

from abita_s2s.config import Config
from abita_s2s.integrations.transport import Fault, Reply, Request, post
from abita_s2s.offices import get_office_profile

OfficeKey = Annotated[str, AfterValidator(lambda key: get_office_profile(key).key)]


class Middleware:
    """Authenticated middleware POSTs; subclasses own retries and outcome mapping."""

    DEADLINE: float = 20

    def __init__(
        self,
        client: httpx.AsyncClient,
        config: Config,
        *,
        deadline: float | None = None,
    ):
        self._client = client
        self._config = config
        self._deadline = self.DEADLINE if deadline is None else deadline

    async def _post[T](
        self, path: str, request: Request, reply: TypeAdapter[T]
    ) -> Reply[T] | Fault:
        url, token = self._config.middleware_url, self._config.middleware_token
        if not url or not token:
            return "not_configured"
        return await post(
            self._client,
            url.rstrip("/") + path,
            {"Authorization": token},
            request,
            reply,
            self._deadline,
            self._deadline,
        )
