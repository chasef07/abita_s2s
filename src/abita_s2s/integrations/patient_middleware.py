"""Authenticated, bounded patient reads and validation of the middleware contract."""

import asyncio
from typing import Annotated, Literal

import httpx
from pydantic import Field, TypeAdapter, ValidationError

from abita_s2s.insurance_contract import CoverageType, InsuranceDecision
from abita_s2s.integrations.middleware import Middleware
from abita_s2s.offices import office_phone
from abita_s2s.records import Record, Text


class Appointment(Record):
    id: int
    date: Text
    time: Text
    provider: str = ""
    type: str = ""
    facility: str = ""
    visitType: CoverageType | None = None
    officeId: str | None = None
    office: str | None = None
    confirmed: bool = False
    appointmentTypeId: int | None = None
    cancellationToken: str | None = None
    rescheduleToken: str | None = None


class Candidate(Record):
    status: Literal["candidate"]
    patientId: Text
    firstName: Text
    lastName: Text
    dob: Text


class Receipt(Record):
    insuranceDecision: InsuranceDecision | None = None
    status: Literal["verified"]
    patientId: Text
    name: Text
    dob: Text
    phone: str | None = None
    insuranceCarrier: str | None = None
    appointmentsStatus: Literal["found", "none", "error"]
    appointments: list[Appointment]


class Multiple(Record):
    status: Literal["multiple_matches"]
    matches: list[Annotated[Candidate | Receipt, Field(discriminator="status")]] = (
        Field(min_length=1)
    )


class NotFound(Record):
    status: Literal["not_found"]


class Unresolved(Record):
    status: Literal["unresolved"]
    reason: Text


class Failure(Record):
    status: Literal["error"] = "error"
    reason: str = "middleware_error"


Result = Receipt | Multiple | NotFound | Unresolved | Failure
RESULT = TypeAdapter(Annotated[Result, Field(discriminator="status")])


class PatientMiddleware(Middleware):
    DEADLINE = 10

    async def resolve(self, office_key: str, identity: dict[str, str]) -> Result:
        office = office_phone(office_key)
        if not self._configured:
            return Failure(reason="not_configured")
        try:
            async with asyncio.timeout(self._deadline):
                for attempt in range(2):
                    try:
                        response = await self._send(
                            "/api/patient/resolve", {**identity, "office": office}
                        )
                    except httpx.TransportError:
                        if attempt == 0:
                            continue
                        return Failure(reason="network_error")
                    if (
                        response.status_code in (408, 429)
                        or response.status_code >= 500
                    ):
                        if attempt == 0:
                            continue
                        return Failure()
                    if not response.is_success:
                        return Failure(reason="request_rejected")
                    try:
                        result = RESULT.validate_python(response.json())
                    except (ValueError, ValidationError):
                        return Failure(reason="invalid_response")
                    if isinstance(result, Failure) and attempt == 0:
                        continue
                    return result
        except TimeoutError:
            return Failure(reason="timeout")
        return Failure()
