"""Per-call application facts, independent of customer policy and LiveKit."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from abita_s2s.middleware import HydratedPatient


@dataclass(frozen=True, repr=False)
class CallContext:
    call_id: str
    session_started_at: datetime
    customer_key: str
    called_office_key: str
    caller_phone: str | None = None
    called_number: str | None = None
    room_name: str | None = None
    sip_participant_identity: str | None = None
    sip_call_id: str | None = None


@dataclass(frozen=True, repr=False)
class PatientCandidate:
    """Private lookup evidence; not permission to act on a patient chart."""

    patient_id: str
    first_name: str
    last_name: str
    dob: str


@dataclass(frozen=True, repr=False)
class VerifiedPatient:
    """Identity already verified by the identity workflow, not by this record."""

    patient_id: str
    name: str
    dob: str
    phone: str | None = None


@dataclass(frozen=True, repr=False)
class CandidateLookup:
    status: Literal["not_attempted", "found", "none", "failed"] = "not_attempted"
    candidates: tuple[PatientCandidate | "HydratedPatient", ...] = ()
    failure_reason: str | None = None

    def __post_init__(self) -> None:
        if self.status not in ("not_attempted", "found", "none", "failed"):
            raise ValueError("Unsupported candidate lookup status")
        object.__setattr__(self, "candidates", tuple(self.candidates))
        if (self.status == "found") != bool(self.candidates):
            raise ValueError("Only a found lookup may contain candidates")
        if (self.status == "failed") != bool(self.failure_reason):
            raise ValueError("Only a failed lookup must have a failure reason")


@dataclass(repr=False)
class PatientState:
    lookup: CandidateLookup = field(default_factory=CandidateLookup)
    active: VerifiedPatient | None = None
    revision: int = 0
    _lookup_token: object | None = field(default=None, init=False)


@dataclass(repr=False)
class CallState:
    call: CallContext
    patient: PatientState = field(default_factory=PatientState)
