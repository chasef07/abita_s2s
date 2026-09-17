"""Scheduling HTTP contracts. Writes are sent once and uncertainty stays explicit."""

import asyncio
from typing import Literal

import httpx
from pydantic import Field, ValidationError, model_validator

from abita_s2s.config import Config
from abita_s2s.middleware import Record, Text


class Slot(Record):
    provider: Text
    time: Text
    datetime: Text
    bookingToken: str | None = None
    columnId: int | str | None = None
    profileId: int | str | None = None
    duration: int | None = None

    @property
    def date(self) -> str:
        return self.datetime.split("T", 1)[0]

    @property
    def key(self):
        return (
            self.columnId,
            self.profileId,
            self.provider,
            self.datetime,
            self.duration,
        )


class Inventory(Record):
    status: Literal["success", "error"]
    outcome: Literal[
        "availability_found",
        "no_availability",
        "no_eligible_providers",
        "availability_search_incomplete",
    ]
    slots: list[Slot]
    searchedFrom: str | None = None
    searchedThrough: str | None = None
    bookingTokenExpiresAt: str | None = None
    shouldRetrySameSearch: bool = False


class WriteReceipt(Record):
    status: str
    outcome: str | None = None
    appointmentId: int | None = Field(default=None, gt=0)
    appointmentTypeId: int | None = Field(default=None, gt=0)
    rescheduleToken: str | None = None
    cancellationToken: str | None = None
    officeId: str | None = None
    office: str | None = None
    visitType: Literal["medical", "routine_vision"] | None = None
    providerName: str | None = None
    locationName: str | None = None
    appointmentTypeName: str | None = None
    missing: list[
        Literal[
            "patientStatus",
            "dob",
            "routing",
            "routeToSpringHill",
            "appointmentLane",
            "office",
        ]
    ] = Field(default_factory=list)


class RescheduleReceipt(Record):
    status: Literal["completed", "partial", "failed", "uncertain"]
    outcome: str | None = None
    booking: WriteReceipt | None = None
    cancellation: WriteReceipt | None = None

    @model_validator(mode="after")
    def validate_effects(self):
        if self.status in ("completed", "partial"):
            if not self.booking or self.booking.status not in ("booked", "partial") or not self.booking.appointmentId:
                raise ValueError("Missing replacement receipt")
        if self.status == "completed":
            if (not self.cancellation or self.cancellation.status != "cancelled"
                    or not self.cancellation.appointmentId
                    or self.cancellation.appointmentId == self.booking.appointmentId):
                raise ValueError("Missing original cancellation receipt")
        if self.status == "failed" and (self.booking or self.cancellation):
            raise ValueError("Failed reschedule cannot contain confirmed writes")
        return self


class SchedulingFailure(Record):
    reason: str
    uncertain: bool = False


class SchedulingHTTP:
    def __init__(self, client: httpx.AsyncClient, config: Config, *, deadline=20):
        self.client = client
        self.url = config.middleware_url
        self.token = config.middleware_token
        self.deadline = deadline

    async def availability(self, body: dict) -> Inventory | SchedulingFailure:
        return await self._post("/api/scheduler/slots", body, Inventory, write=False)

    async def book(self, body: dict) -> WriteReceipt | SchedulingFailure:
        return await self._post("/api/appointment/book", body, WriteReceipt, write=True)

    async def reschedule(self, body: dict) -> RescheduleReceipt | SchedulingFailure:
        return await self._post(
            "/api/appointment/reschedule", body, RescheduleReceipt, write=True
        )

    async def cancel(self, body: dict) -> WriteReceipt | SchedulingFailure:
        return await self._post(
            "/api/appointment/cancel", body, WriteReceipt, write=True
        )

    async def _post(self, path, body, record, *, write):
        if not self.url or not self.token:
            return SchedulingFailure(reason="not_configured")
        try:
            async with asyncio.timeout(self.deadline):
                response = await self.client.post(
                    self.url.rstrip("/") + path,
                    headers={"Authorization": self.token},
                    json=body,
                    timeout=self.deadline,
                    follow_redirects=False,
                )
            if not response.is_success:
                return SchedulingFailure(reason="http_error", uncertain=write)
            return record.model_validate(response.json())
        except (httpx.TransportError, TimeoutError):
            return SchedulingFailure(reason="transport_error", uncertain=write)
        except (ValueError, ValidationError):
            return SchedulingFailure(reason="invalid_response", uncertain=write)
