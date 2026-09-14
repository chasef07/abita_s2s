"""One attempt per chart/insurance write; ambiguous responses are never retried."""

import asyncio
from typing import Literal

import httpx
from pydantic import Field

from abita_s2s.config import Config
from abita_s2s.middleware import Record, Text
from abita_s2s.offices import get_office_profile


class CreationReceipt(Record):
    status: Literal["created", "partial"]
    patientId: Text
    name: Text
    dob: Text
    routing: str | None = None
    allowedProviders: list[str] = Field(default_factory=list)
    preauthRequired: bool = False


class UpdatedReceipt(Record):
    status: Literal["updated"]
    patientId: Text
    newInsurance: Text
    routing: str | None = None
    allowedProviders: list[str] = Field(default_factory=list)
    routingAmbiguous: bool = False
    preauthRequired: bool = False


class WriteFailure(Record):
    status: Literal["failed", "uncertain"]
    reason: str


class RegistrationMiddleware:
    def __init__(
        self, client: httpx.AsyncClient, config: Config, *, deadline: float = 20
    ):
        self._client = client
        self._config = config
        self._deadline = deadline

    async def create(
        self, office: str, payload: dict
    ) -> CreationReceipt | WriteFailure:
        return await self._write("/api/add-patient", office, payload, CreationReceipt)

    async def update(self, office: str, payload: dict) -> UpdatedReceipt | WriteFailure:
        return await self._write(
            "/api/patient/update-insurance", office, payload, UpdatedReceipt
        )

    async def _write(self, path, office, payload, receipt):
        if not self._config.middleware_url or not self._config.middleware_token:
            return WriteFailure(status="failed", reason="not_configured")
        try:
            async with asyncio.timeout(self._deadline):
                response = await self._client.post(
                    self._config.middleware_url.rstrip("/") + path,
                    headers={"Authorization": self._config.middleware_token},
                    json={
                        **payload,
                        "office": get_office_profile(office).trunk_numbers[0],
                    },
                    timeout=self._deadline,
                    follow_redirects=False,
                )
            if not response.is_success:
                return WriteFailure(status="uncertain", reason="http_failure")
            body = response.json()
            if isinstance(body, dict) and body.get("status") == "error":
                # Only these outcomes prove that chart creation made no chart.
                # An update can already have ended the old plan before failing.
                safe = receipt is CreationReceipt and body.get("outcome") in (
                    "validation_failed",
                    "rejected",
                    "reconciled_failure",
                    "unavailable",
                    "failed",
                )
                return WriteFailure(
                    status="failed" if safe else "uncertain", reason="backend_failure"
                )
            return receipt.model_validate(body)
        except (httpx.HTTPError, TimeoutError, ValueError):
            return WriteFailure(status="uncertain", reason="unverified_write")
