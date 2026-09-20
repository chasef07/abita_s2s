"""Per-call application facts, independent of customer policy and LiveKit."""

from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Literal

from abita_s2s.insurance_state import InsuranceState

if TYPE_CHECKING:
    from abita_s2s.middleware import Candidate, Receipt
    from abita_s2s.reporting import CallReporter


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


@dataclass(frozen=True, repr=False)
class CandidateLookup:
    status: Literal["not_attempted", "found", "none", "failed"] = "not_attempted"
    candidates: tuple["Candidate | Receipt", ...] = ()
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
    active: "Receipt | None" = None
    revision: int = 0
    absence: "PatientAbsence | None" = None


@dataclass(repr=False)
class CallState:
    call: CallContext
    patient: PatientState = field(default_factory=PatientState)
    insurance: InsuranceState = field(default_factory=InsuranceState)
    reporter: "CallReporter | None" = None


@dataclass(frozen=True, repr=False)
class PatientAbsence:
    """Complete first-name/DOB search evidence, invalidated by the next operation."""

    first_name: str
    dob: str
    office_key: str
