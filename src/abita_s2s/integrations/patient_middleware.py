"""Authenticated, bounded patient reads and validation of the middleware contract."""

import asyncio
from typing import Annotated, Literal

from pydantic import Field, TypeAdapter

from abita_s2s.insurance_contract import CoverageType, InsuranceDecision
from abita_s2s.integrations.middleware import Middleware, OfficeKey
from abita_s2s.integrations.transport import Request
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
    insuranceNote: str | None = None
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


class ResolveRequest(Request):
    phone: str | None = None
    firstName: str | None = None
    dob: str | None = None
    patientId: str | None = None
    office: OfficeKey


class PatientMiddleware(Middleware):
    DEADLINE = 10

    async def resolve(self, request: ResolveRequest) -> Result:
        try:
            async with asyncio.timeout(self._deadline):
                for attempt in range(2):
                    reply = await self._post("/api/patient/resolve", request, RESULT)
                    if reply == "not_configured":
                        return Failure(reason="not_configured")
                    if isinstance(reply, str):
                        if attempt == 0:
                            continue
                        return Failure(reason="network_error")
                    if reply.retryable:
                        if attempt == 0:
                            continue
                        return Failure()
                    if not reply.ok:
                        return Failure(reason="request_rejected")
                    if reply.body is None:
                        return Failure(reason="invalid_response")
                    if isinstance(reply.body, Failure) and attempt == 0:
                        continue
                    return reply.body
        except TimeoutError:
            return Failure(reason="timeout")
        return Failure()
