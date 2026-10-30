"""Scheduling HTTP contracts. Writes are sent once and uncertainty stays explicit."""

from datetime import date, datetime
from typing import Annotated, Literal

from pydantic import (
    AwareDatetime,
    BeforeValidator,
    Field,
    TypeAdapter,
    field_validator,
    model_validator,
)

from abita_s2s.insurance_contract import CoverageType, InsuranceDecision
from abita_s2s.integrations.middleware import Middleware, OfficeKey
from abita_s2s.integrations.transport import Request
from abita_s2s.records import Record, Text

SchedulingOutcome = Literal[
    "validation",
    "invalid_booking_token",
    "invalid_cancellation_token",
    "invalid_reschedule_token",
    "booking_token_required",
    "appointment_type_unresolved",
    "patient_context_mismatch",
    "slot_unavailable",
    "provider_conflict",
    "provider_rejected",
    "ownership_mismatch",
    "write_failed",
    "indeterminate_write",
]
Outcome = Annotated[
    SchedulingOutcome | None, BeforeValidator(lambda value: value or None)
]


class SlotSearch(Request):
    office: OfficeKey
    startDate: str
    patientId: str
    visitType: CoverageType
    insurancePlanId: str
    dob: str


class Booking(Request):
    patientId: str
    patientName: str
    dob: str
    bookingToken: str
    visitCategory: CoverageType
    appointmentReason: str
    visitReason: str
    referringDoctor: str
    referringDoctorPhone: str | None = None
    referringDoctorAddress: str | None = None
    rescheduleToken: str | None = None


class Cancellation(Request):
    patientId: str
    cancellationToken: str


class Slot(Record):
    provider: Text
    time: Text
    datetime: Text
    bookingToken: Text
    columnId: int | str | None = None
    profileId: int | str | None = None
    duration: int | None = Field(default=None, gt=0)
    sameStartBooked: int = Field(default=0, ge=0)

    @field_validator("datetime")
    @classmethod
    def validate_datetime(cls, value):
        if "T" not in value:
            raise ValueError("Expected a slot date and time")
        datetime.fromisoformat(value)
        date.fromisoformat(value.split("T", 1)[0])
        return value

    @property
    def date(self) -> str:
        return self.datetime.split("T", 1)[0]


class Inventory(Record):
    status: Literal["success", "error"]
    outcome: Literal[
        "invalid_input",
        "availability_found",
        "no_availability",
        "no_eligible_providers",
        "availability_search_incomplete",
        "insurance_not_cleared",
    ]
    slots: list[Slot]
    insuranceDecision: InsuranceDecision | None = None
    searchedFrom: str | None = None
    searchedThrough: str | None = None
    bookingTokenExpiresAt: AwareDatetime | None = None
    shouldRetrySameSearch: bool = False
    message: str = ""

    @field_validator("bookingTokenExpiresAt", mode="before")
    @classmethod
    def parse_expiry(cls, value):
        return datetime.fromisoformat(value) if isinstance(value, str) else value

    @model_validator(mode="after")
    def validate_inventory(self):
        if self.outcome == "invalid_input" and (
            self.status != "error"
            or self.shouldRetrySameSearch
            or not self.message.strip()
        ):
            raise ValueError("Invalid search failure")
        if self.outcome == "insurance_not_cleared" and (
            self.status != "success"
            or self.insuranceDecision is None
            or self.insuranceDecision.canSchedule
        ):
            raise ValueError("Uncleared insurance needs its decision")
        found = self.outcome == "availability_found"
        if found != bool(self.slots) or (
            found and (self.status != "success" or self.bookingTokenExpiresAt is None)
        ):
            raise ValueError("Inconsistent availability result")
        return self


class WriteReceipt(Record):
    status: Literal["booked", "cancelled", "error"]
    outcome: Outcome = None
    appointmentId: int | None = Field(default=None, gt=0)
    appointmentTypeId: int | None = Field(default=None, gt=0)
    rescheduleToken: str | None = None
    cancellationToken: str | None = None
    officeId: str | None = None
    office: str | None = None
    visitType: CoverageType | None = None
    profileId: str | None = None
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
    outcome: Outcome = None
    booking: WriteReceipt | None = None
    cancellation: WriteReceipt | None = None

    @model_validator(mode="after")
    def validate_effects(self):
        if self.status in ("completed", "partial"):
            if (
                not self.booking
                or self.booking.status != "booked"
                or not self.booking.appointmentId
            ):
                raise ValueError("Missing replacement receipt")
        if self.status == "completed":
            if (
                not self.cancellation
                or self.cancellation.status != "cancelled"
                or not self.cancellation.appointmentId
                or self.cancellation.appointmentId == self.booking.appointmentId
            ):
                raise ValueError("Missing original cancellation receipt")
        if self.status in ("failed", "uncertain") and (
            self.booking or self.cancellation
        ):
            raise ValueError("Unconfirmed reschedule cannot contain confirmed writes")
        return self


class SchedulingFailure(Record):
    reason: str
    uncertain: bool = False


INVENTORY = TypeAdapter(Inventory)
WRITE = TypeAdapter(WriteReceipt)
RESCHEDULE = TypeAdapter(RescheduleReceipt)


class SchedulingHTTP(Middleware):
    async def availability(self, request: SlotSearch) -> Inventory | SchedulingFailure:
        return await self._exchange(
            "/api/scheduler/slots", request, INVENTORY, write=False
        )

    async def book(self, request: Booking) -> WriteReceipt | SchedulingFailure:
        return await self._exchange("/api/appointment/book", request, WRITE, write=True)

    async def reschedule(
        self, request: Booking
    ) -> RescheduleReceipt | SchedulingFailure:
        return await self._exchange(
            "/api/appointment/reschedule", request, RESCHEDULE, write=True
        )

    async def cancel(self, request: Cancellation) -> WriteReceipt | SchedulingFailure:
        return await self._exchange(
            "/api/appointment/cancel", request, WRITE, write=True
        )

    async def _exchange[R: Inventory | WriteReceipt | RescheduleReceipt](
        self, path: str, request: Request, record: TypeAdapter[R], *, write: bool
    ) -> R | SchedulingFailure:
        reply = await self._post(path, request, record)
        if reply == "not_configured":
            return SchedulingFailure(reason="not_configured")
        if isinstance(reply, str):
            return SchedulingFailure(reason="transport_error", uncertain=write)
        if not reply.ok:
            if not write and reply.body is None:
                return SchedulingFailure(reason="invalid_response")
            if not write and reply.body and reply.body.status == "error":
                return reply.body
            return SchedulingFailure(reason="http_error", uncertain=write)
        if reply.body is None:
            return SchedulingFailure(reason="invalid_response", uncertain=write)
        return reply.body
