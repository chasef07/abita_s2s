"""Insurance consumer contract. Acceptance is participation, never active benefits."""

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from abita_s2s.state import CallState, PatientAbsence

CoverageType = Literal["medical", "routine_vision"]


@dataclass(frozen=True, repr=False)
class AcceptedInsurance:
    office_key: str
    patient_revision: int
    patient_id: str | None
    absence: "PatientAbsence | None"
    plan: str
    coverage_type: CoverageType


@dataclass(repr=False)
class InsuranceState:
    accepted: AcceptedInsurance | None = None
    # Full/partial creation and uncertain updates must block scheduling until resolved.
    registration_patient_id: str | None = None
    registration_status: Literal["created", "partial"] | None = None
    write_pending: bool = False
    write_uncertain: bool = False


def accepted_insurance(state: "CallState", coverage_type: CoverageType | None = None) -> AcceptedInsurance | None:
    """Return only acceptance for the current office, identity and requested visit type.

    New registration requires the resolver's exact complete-search absence object.
    A subsequent resolution, patient switch, plan correction or visit-type check
    invalidates it. Consumers must use this function, not the stored field.
    """
    checked = state.insurance.accepted
    patient = state.patient
    if checked is None or checked.office_key != state.call.called_office_key:
        return None
    if checked.patient_revision != patient.revision:
        return None
    if coverage_type is not None and checked.coverage_type != coverage_type:
        return None
    if patient.active:
        return checked if checked.patient_id == patient.active.patientId else None
    return checked if checked.absence is not None and checked.absence is patient.absence else None


def insurance_ready(state: "CallState", coverage_type: CoverageType) -> bool:
    """Scheduling guard for this domain; does not replace appointment policy checks."""
    if state.insurance.write_pending or state.insurance.write_uncertain:
        return False
    active = state.patient.active
    if not active or active.preauthRequired or active.routingAmbiguous:
        return False
    if (state.insurance.registration_patient_id == active.patientId
            and state.insurance.registration_status == "partial"):
        return False
    return accepted_insurance(state, coverage_type) is not None
