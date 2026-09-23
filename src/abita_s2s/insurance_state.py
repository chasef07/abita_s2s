"""Insurance consumer contract. Acceptance is participation, never active benefits."""

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

from abita_s2s.insurance_contract import InsuranceDecision

if TYPE_CHECKING:
    from abita_s2s.eligibility_contract import EligibilityCheck
    from abita_s2s.state import CallState, PatientAbsence

CoverageType = Literal["medical", "routine_vision"]


@dataclass(frozen=True, repr=False)
class AcceptedInsurance:
    office_key: str
    patient_revision: int
    patient_id: str | None
    absence: "PatientAbsence | None"
    decision: InsuranceDecision = field(hash=False)
    requested_plan: str = ""


@dataclass(repr=False)
class InsuranceState:
    # Each intake retains its own submitted details, including corrected requests.
    eligibility_checks: list["EligibilityCheck"] = field(default_factory=list)
    current_eligibility: tuple[int, "EligibilityCheck"] | None = None
    accepted: AcceptedInsurance | None = None
    # Complete creation can schedule; partial creation must be resolved first.
    registrations: dict[str, Literal["created", "partial"]] = field(
        default_factory=dict
    )
    check_revision: int = 0
    write_pending: bool = False
    write_uncertain: bool = False


def accepted_insurance(
    state: "CallState", coverage_type: CoverageType | None = None
) -> AcceptedInsurance | None:
    """Return only acceptance for the current office, identity and requested visit type.

    New registration uses caller-confirmed identity without a chart lookup.
    A subsequent resolution, patient switch, plan correction or visit-type check
    invalidates it. Consumers must use this function, not the stored field.
    """
    checked = state.insurance.accepted
    patient = state.patient
    if checked is None or checked.office_key != state.call.called_office_key:
        return None
    if checked.patient_revision != patient.revision:
        return None
    if coverage_type is not None and checked.decision.coverageType != coverage_type:
        return None
    if patient.active:
        return checked if checked.patient_id == patient.active.patientId else None
    return (
        checked
        if checked.patient_id is None and checked.absence is patient.absence
        else None
    )


def registration_insurance(
    state: "CallState", coverage_type: CoverageType
) -> InsuranceDecision | None:
    """Current acceptance for a patient registered during this call."""
    active = state.patient.active
    if not active or active.patientId not in state.insurance.registrations:
        return None
    checked = accepted_insurance(state, coverage_type)
    decision = checked.decision if checked else None
    if decision and decision.officeId.replace("_", "-") == state.call.called_office_key:
        return decision
    return None


def insurance_ready(state: "CallState") -> bool:
    """Guard unfinished writes; middleware evaluates every completed chart."""
    if state.insurance.write_pending or state.insurance.write_uncertain:
        return False
    active = state.patient.active
    if not active:
        return False
    return state.insurance.registrations.get(active.patientId) != "partial"


def appointment_eligibility(state: "CallState", patient_id: str, visit: CoverageType):
    """Capture this patient's bound evidence before a booking request is sent."""
    checked = accepted_insurance(state, visit)
    if not checked or checked.patient_id != patient_id:
        return None
    for check in reversed(state.insurance.eligibility_checks):
        if (
            check.patient_id == patient_id
            and check.office == checked.office_key
            and check.request.coverageType == visit
            and check.canonical_plan == checked.decision.canonicalPlan
            and not check.invalidated
        ):
            return check
    return None
