"""Per-call application facts, independent of customer policy and LiveKit."""

from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING

from abita_s2s.call_record import CallRecord
from abita_s2s.offices import OfficeProfile, get_product_office_key
from abita_s2s.records import Receipt

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


@dataclass(frozen=True, repr=False)
class Claimed:
    """What the caller said, matched to no chart yet. left is the chart the call came
    from, which makes the next chart a switch."""

    first_name: str | None
    dob: str | None
    left: str | None = None

    @property
    def claim(self) -> "Claimed":
        return self


@dataclass(frozen=True, repr=False)
class Selected:
    """The chart the call is on.

    since is where in the record the call came to this patient, so offers made before it
    belong to someone else. said_name is the name that verified the chart. claim is what
    the caller said since, not yet settled against it.
    """

    patient_id: str
    since: int
    said_name: str | None = None
    claim: Claimed | None = None

    @property
    def left(self) -> str:
        """The chart a new claim would leave."""
        return self.patient_id


@dataclass(repr=False)
class CallState:
    call: CallContext
    plans: "OfficePlans | None" = None
    identity: Claimed | Selected | None = None
    record: CallRecord = field(default_factory=CallRecord)
    reporter: "CallReporter | None" = None

    def chart(self) -> Receipt | None:
        """The chart the call is on, computed from the record."""
        if isinstance(self.identity, Selected):
            return self.record.chart(self.identity.patient_id)
        return None
