"""Insurance consumer contract: the office plan list loaded for this call and write guards."""

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from abita_s2s.insurance_list import OfficePlans
    from abita_s2s.state import CallState


@dataclass(repr=False)
class InsuranceState:
    plans: "OfficePlans | None" = None
    registrations: dict[str, Literal["created", "partial"]] = field(
        default_factory=dict
    )
    write_pending: bool = False
    write_uncertain: bool = False


def insurance_ready(state: "CallState") -> bool:
    """Guard unfinished writes; middleware evaluates every completed chart."""
    if state.insurance.write_pending or state.insurance.write_uncertain:
        return False
    active = state.patient.active
    if not active:
        return False
    return state.insurance.registrations.get(active.patientId) != "partial"
