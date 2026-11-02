"""Per-call application facts, independent of customer policy and LiveKit."""

from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING

from abita_s2s.insurance_state import InsuranceState

if TYPE_CHECKING:
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
    called_office_key: str
    caller_phone: str | None = None
    called_number: str | None = None
    room_name: str | None = None
    sip_participant_identity: str | None = None


@dataclass(repr=False)
class PatientState:
    """The caller's patient. Only these methods change it.

    activate with a different patient, deactivate, and update_insurance start a new
    revision. A same-patient reload and appointment changes keep it.
    tests/test_state_ownership.py names the modules that may call each method.
    """

    candidates: tuple["Candidate | Receipt", ...] = ()
    active: "Receipt | None" = None
    revision: int = 0

    def activate(self, receipt: "Receipt") -> None:
        """Make a verified chart active. A different patient starts a new revision."""
        if self.active is None or self.active.patientId != receipt.patientId:
            self.revision += 1
        self.active = receipt

    def deactivate(self) -> None:
        self.active = None
        self.revision += 1

    def update_insurance(self, carrier: str) -> None:
        """Record a completed insurance write on the active chart."""
        self.active = self._chart().model_copy(update={"insuranceCarrier": carrier})
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

    def record_candidates(self, candidates: tuple["Candidate | Receipt", ...]) -> None:
        """Keep the caller-phone lookup's well-formed candidates, or none."""
        self.candidates = candidates

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
