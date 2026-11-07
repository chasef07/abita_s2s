"""Per-call application facts, independent of customer policy and LiveKit."""

from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING

from abita_s2s.call_record import CallRecord
from abita_s2s.offices import OfficeProfile, get_product_office_key
from abita_s2s.records import Appointment, Candidate, Receipt

if TYPE_CHECKING:
    from abita_s2s.insurance_list import OfficePlans
    from abita_s2s.runtime.reporting import CallReporter


@dataclass(frozen=True, repr=False)
class CallContext:
    call_id: str
    session_started_at: datetime
    office: OfficeProfile
    caller_phone: str | None = None
    called_number: str | None = None
    room_name: str | None = None
    sip_participant_identity: str | None = None

    @property
    def office_phone(self) -> str:
        """The number the caller dialed as received, or the office's main trunk."""
        return self.called_number or self.office.trunk_numbers[0]

    @property
    def product_office_key(self) -> str:
        """Product's location for the dialed number, which may differ from the office key."""
        return get_product_office_key(self.office_phone)


@dataclass(repr=False)
class PatientState:
    """The caller's patient. Only these methods change it.

    activate with a different patient, deactivate, and update_insurance start a new
    revision. A same-patient reload and appointment changes keep it.
    tests/test_state_ownership.py names the modules that may call each method.
    """

    candidates: tuple[Candidate, ...] = ()
    active: Receipt | None = None
    revision: int = 0

    def activate(self, receipt: Receipt) -> None:
        """Make a verified chart active. A different patient starts a new revision."""
        if self.active is None or self.active.patientId != receipt.patientId:
            self.revision += 1
        self.active = receipt

    def deactivate(self) -> None:
        self.active = None
        self.revision += 1

    def update_insurance(self, carrier: str, carrier_id: str | None) -> None:
        """Record a completed insurance write; the old carrier's staff note no longer applies."""
        self.active = self._chart().model_copy(
            update={
                "insuranceCarrier": carrier,
                "insuranceCarrierId": carrier_id,
                "insuranceNote": None,
            }
        )
        self.revision += 1

    def update_appointments(self, appointments: list[Appointment]) -> None:
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

    def record_candidates(self, candidates: tuple[Candidate, ...]) -> None:
        """Keep the caller-phone lookup's well-formed candidates, or none."""
        self.candidates = candidates

    def _chart(self) -> Receipt:
        if self.active is None:
            raise RuntimeError("No active chart to change")
        return self.active


@dataclass(repr=False)
class CallState:
    call: CallContext
    plans: "OfficePlans | None" = None
    patient: PatientState = field(default_factory=PatientState)
    record: CallRecord = field(default_factory=CallRecord)
    reporter: "CallReporter | None" = None
