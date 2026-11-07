"""Per-call application facts, independent of customer policy and LiveKit."""

from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING

from abita_s2s.call_record import CallRecord
from abita_s2s.offices import OfficeProfile, get_product_office_key
from abita_s2s.records import Candidate, Receipt

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
    """Which chart the call is on. The chart itself is computed from the record.

    since is the record position where the call came to this patient: offers made
    before it belong to someone else. tests/test_state_ownership.py names the modules
    that may call each method.
    """

    candidates: tuple[Candidate, ...] = ()
    patient_id: str | None = None
    since: int | None = None

    def select(self, patient_id: str, position: int) -> None:
        """Put the call on this chart. Staying on the same patient keeps since."""
        if self.patient_id != patient_id:
            self.since = position
        self.patient_id = patient_id

    def deselect(self) -> None:
        self.patient_id = None
        self.since = None

    def record_candidates(self, candidates: tuple[Candidate, ...]) -> None:
        """Keep the caller-phone lookup's well-formed candidates, or none."""
        self.candidates = candidates


@dataclass(repr=False)
class CallState:
    call: CallContext
    plans: "OfficePlans | None" = None
    patient: PatientState = field(default_factory=PatientState)
    record: CallRecord = field(default_factory=CallRecord)
    reporter: "CallReporter | None" = None

    def chart(self) -> Receipt | None:
        """The chart the call is on, computed from the record."""
        patient_id = self.patient.patient_id
        return self.record.chart(patient_id) if patient_id else None
