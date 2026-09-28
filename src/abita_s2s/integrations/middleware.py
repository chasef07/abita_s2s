"""Authenticated middleware transport shared by the patient, registration and scheduling adapters."""

import httpx

from abita_s2s.config import Config


class Middleware:
    """Authenticated middleware POSTs; subclasses own deadlines, retries, and errors."""

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

    @property
    def _configured(self) -> bool:
        return bool(self._config.middleware_url and self._config.middleware_token)

    def _send(self, path: str, body: dict, deadline: float | None = None):
        deadline = self._deadline if deadline is None else deadline
        return self._client.post(
            self._config.middleware_url.rstrip("/") + path,
            headers={"Authorization": self._config.middleware_token},
            json=body,
            timeout=deadline,
            follow_redirects=False,
        )
