"""Per-call application facts, independent of customer policy and LiveKit."""

from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Literal

from abita_s2s.insurance_state import InsuranceState

if TYPE_CHECKING:
    from abita_s2s.insurance_contract import InsuranceDecision
    from abita_s2s.integrations.patient_middleware import (
        Appointment,
        Candidate,
        Receipt,
    )
    from abita_s2s.runtime.reporting import CallReporter


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
    """The caller's patient. Only these methods change it.

    activate with a different patient, deactivate, and update_insurance start a new
    revision. A same-patient reload and appointment changes keep it.
    tests/test_state_ownership.py names the modules that may call each method.
    """

    lookup: CandidateLookup = field(default_factory=CandidateLookup)
    active: "Receipt | None" = None
    revision: int = 0
    absence: "PatientAbsence | None" = None

    def activate(self, receipt: "Receipt") -> None:
        """Make a verified chart active. A different patient starts a new revision."""
        if self.active is None or self.active.patientId != receipt.patientId:
            self.revision += 1
        self.active = receipt
        self.absence = None

    def deactivate(self) -> None:
        self.active = None
        self.absence = None
        self.revision += 1

    def update_insurance(self, carrier: str, decision: "InsuranceDecision") -> None:
        """Record a completed insurance write on the active chart."""
        self.active = self._chart().model_copy(
            update={"insuranceCarrier": carrier, "insuranceDecision": decision}
        )
        self.revision += 1

    def update_appointments(self, appointments: "list[Appointment]") -> None:
        """Apply confirmed appointment writes. Stale appointments stay stale until a reload."""
        active = self._chart()
        if appointments == active.appointments:
            return
        status = (
            active.appointmentsStatus
            if active.appointmentsStatus == "error"
            else ("found" if appointments else "none")
        )
        self.active = active.model_copy(
            update={"appointments": appointments, "appointmentsStatus": status}
        )

    def mark_appointments_stale(self) -> None:
        """Revoke the loaded appointments' authority until identity reloads the chart."""
        self.active = self._chart().model_copy(update={"appointmentsStatus": "error"})

    def record_absence(self, absence: "PatientAbsence | None") -> None:
        """Keep a complete search's no-match evidence, or drop it as the next search starts."""
        self.absence = absence

    def record_lookup(self, lookup: CandidateLookup) -> None:
        self.lookup = lookup

    def _chart(self) -> "Receipt":
        if self.active is None:
            raise RuntimeError("No active chart to change")
        return self.active


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
