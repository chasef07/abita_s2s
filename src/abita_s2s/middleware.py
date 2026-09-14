"""Read-only middleware transport; patient verification belongs to identity."""

import asyncio
import json
import logging
from dataclasses import dataclass
from time import monotonic
from typing import Annotated, Literal
from uuid import uuid4

import httpx
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator
from pydantic.alias_generators import to_camel

from abita_s2s.state import PatientCandidate

logger = logging.getLogger(__name__)
NonEmpty = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class _Record(BaseModel):
    model_config = ConfigDict(
        strict=True, frozen=True, alias_generator=to_camel, populate_by_name=True
    )

    def __repr_args__(self):
        # Patient records and authorization tokens must not appear in logs.
        return []


class PatientAppointment(_Record):
    id: Annotated[int, Field(gt=0)]
    date: NonEmpty
    time: NonEmpty
    provider: str | None = None
    type: str | None = None
    appointment_type_id: int | None = None
    facility: str | None = None
    office_id: str | None = None
    office: str | None = None
    cancellation_token: str | None = None
    reschedule_token: str | None = None


class HydratedPatient(_Record):
    """Loaded middleware evidence, not an identity-verification decision."""

    status: Literal["verified"]
    patient_id: NonEmpty
    name: NonEmpty
    dob: NonEmpty
    phone: str | None = None
    insurance_carrier: str | None = None
    insurance_carrier_id: str | None = None
    ins_plan_id: str | None = None
    resp_party_id: str | None = None
    routing: str | None = None
    allowed_providers: tuple[str, ...] = ()
    routing_ambiguous: bool = False
    preauth_required: bool = False
    appointments_status: Literal["found", "none", "error"]
    appointments: tuple[PatientAppointment, ...]
    appointments_message: str | None = None
    message: str | None = None

    @model_validator(mode="after")
    def consistent_appointments(self):
        if (self.appointments_status == "found") != bool(self.appointments):
            raise ValueError("Inconsistent appointment load result")
        return self


class _Candidate(_Record):
    model_config = ConfigDict(extra="forbid")
    status: Literal["candidate"]
    patient_id: NonEmpty
    first_name: NonEmpty
    last_name: NonEmpty
    dob: NonEmpty


@dataclass(frozen=True, repr=False)
class PatientMatches:
    matches: tuple[PatientCandidate, ...]
    status: Literal["multiple_matches"] = "multiple_matches"


@dataclass(frozen=True)
class PatientNotFound:
    status: Literal["not_found"] = "not_found"


@dataclass(frozen=True)
class MiddlewareFailure:
    reason: Literal[
        "network_error", "middleware_error", "request_rejected", "invalid_response"
    ]
    status: Literal["error"] = "error"


PatientResolveResult = (
    HydratedPatient | PatientMatches | PatientNotFound | MiddlewareFailure
)


def _parse_response(body: bytes) -> PatientResolveResult:
    # JSON-mode validation accepts JSON arrays as immutable tuples without coercing scalars.
    try:
        raw = json.loads(body)
        if not isinstance(raw, dict):
            return MiddlewareFailure("invalid_response")
        match raw.get("status"):
            case "verified":
                return HydratedPatient.model_validate_json(body)
            case "multiple_matches":
                matches = raw.get("matches")
                if not isinstance(matches, list) or not matches:
                    return MiddlewareFailure("invalid_response")
                candidates = tuple(_Candidate.model_validate(item) for item in matches)
                return PatientMatches(
                    tuple(
                        PatientCandidate(
                            patient_id=c.patient_id,
                            first_name=c.first_name,
                            last_name=c.last_name,
                            dob=c.dob,
                        )
                        for c in candidates
                    )
                )
            case "not_found":
                if (
                    raw.get("matches")
                    or raw.get("patientId")
                    or raw.get("appointments")
                ):
                    return MiddlewareFailure("invalid_response")
                return PatientNotFound()
            case "error":
                return MiddlewareFailure("middleware_error")
    except ValueError:
        pass
    return MiddlewareFailure("invalid_response")


class MiddlewareClient:
    """One bounded patient read; the caller owns the HTTP client's lifetime."""

    def __init__(
        self,
        http: httpx.AsyncClient,
        *,
        base_url: str,
        auth_token: str,
        timeout_seconds: float = 10,
    ):
        url = httpx.URL(base_url)
        if (
            url.scheme not in ("http", "https")
            or not url.host
            or url.userinfo
            or url.query
            or url.fragment
        ):
            raise ValueError(
                "Middleware URL must be an HTTP origin or base path without credentials"
            )
        if not auth_token.strip() or timeout_seconds <= 0:
            raise ValueError("Middleware token and positive timeout are required")
        self._http = http
        self._url = str(url).rstrip("/") + "/api/patient/resolve"
        self._auth_token = auth_token
        self._timeout = timeout_seconds

    async def resolve_patient(self, *, office: str, phone: str) -> PatientResolveResult:
        if not office.strip() or not any(c.isdigit() for c in phone):
            return MiddlewareFailure("request_rejected")
        for attempt in (1, 2):
            request_id = str(uuid4())
            started = monotonic()
            http_status = None
            retryable = False
            outcome = "cancelled"
            try:
                # A total deadline includes connection, body download, and slow streams.
                async with asyncio.timeout(self._timeout):
                    response = await self._http.post(
                        self._url,
                        json={"office": office, "phone": phone},
                        headers={
                            "Authorization": self._auth_token,
                            "X-Request-ID": request_id,
                        },
                        timeout=self._timeout,
                        follow_redirects=False,
                    )
                http_status = response.status_code
                if response.is_success:
                    result = _parse_response(response.content)
                    retryable = (
                        isinstance(result, MiddlewareFailure)
                        and result.reason == "middleware_error"
                    )
                else:
                    retryable = http_status in (408, 429) or http_status >= 500
                    result = MiddlewareFailure(
                        "middleware_error" if retryable else "request_rejected"
                    )
                outcome = (
                    result.reason
                    if isinstance(result, MiddlewareFailure)
                    else result.status
                )
            except (httpx.TransportError, TimeoutError):
                retryable = True
                outcome = "network_error"
                result = MiddlewareFailure("network_error")
            finally:
                logger.info(
                    "Patient resolution request",
                    extra={
                        "request_id": request_id,
                        "attempt": attempt,
                        "http_status": http_status,
                        "duration_ms": round((monotonic() - started) * 1000),
                        "outcome": outcome,
                    },
                )
            if not retryable or attempt == 2:
                return result
        raise AssertionError("Unreachable retry state")
