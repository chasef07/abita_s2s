"""Insurance consumer contract. Acceptance is participation, never active benefits."""

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

from abita_s2s.insurance_contract import InsuranceDecision

if TYPE_CHECKING:
    from abita_s2s.state import CallState, PatientAbsence

CoverageType = Literal["medical", "routine_vision"]


@dataclass(frozen=True, repr=False)
class AcceptedInsurance:
    office_key: str
    patient_revision: int
    patient_id: str | None
    absence: "PatientAbsence | None"
    decision: InsuranceDecision = field(hash=False)


@dataclass(repr=False)
class InsuranceState:
    accepted: AcceptedInsurance | None = None
    # Once the caller checks/corrects a chart's plan, old on-file coverage cannot
    # rescue a rejected or stale check, including after resolving that chart again.
    checked_patients: set[tuple[str, str]] = field(default_factory=set)
    # Full/partial creation and uncertain updates must block scheduling until resolved.
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


def scheduling_insurance(state: "CallState", coverage_type: CoverageType) -> InsuranceDecision | None:
    """The one current insurance decision eligible for a scheduling request."""
    if state.insurance.write_pending or state.insurance.write_uncertain:
        return None
    active = state.patient.active
    if not active:
        return None
    if state.insurance.registrations.get(active.patientId) == "partial":
        return None
    checked = state.insurance.accepted
    if checked is not None and checked.patient_id == active.patientId:
        current = accepted_insurance(state, coverage_type)
        decision = current.decision if current else None
    elif (
        active.patientId in state.insurance.registrations
        or (state.call.called_office_key, active.patientId)
        in state.insurance.checked_patients
    ):
        return None
    else:
        decision = active.insuranceDecision
    if (decision and decision.canSchedule and decision.coverageType == coverage_type
        and decision.officeId.replace("_", "-") == state.call.called_office_key):
        return decision
    return None


def insurance_ready(state: "CallState", coverage_type: CoverageType) -> bool:
    return scheduling_insurance(state, coverage_type) is not None
