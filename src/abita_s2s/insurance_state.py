"""Insurance consumer contract. Acceptance is participation, never active benefits.

InsuranceRegistration grants acceptance; every owner clears or rebinds it only
through clear_acceptance and rebind_acceptance.
"""

from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Literal

from abita_s2s.insurance_contract import (
    CoverageType,
    InsuranceDecision,
    InsuranceOption,
)
from abita_s2s.offices import same_office

if TYPE_CHECKING:
    from abita_s2s.state import CallState, PatientAbsence


@dataclass(frozen=True, repr=False)
class AcceptedInsurance:
    office_key: str
    patient_revision: int
    patient_id: str | None
    absence: "PatientAbsence | None"
    decision: InsuranceDecision = field(hash=False)


@dataclass(frozen=True, repr=False)
class OfferedPlans:
    office_key: str
    patient_revision: int
    coverage_type: CoverageType
    options: tuple[InsuranceOption, ...]
    question: str


@dataclass(repr=False)
class InsuranceState:
    accepted: AcceptedInsurance | None = None
    offered: OfferedPlans | None = None
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


def offered_plans(
    state: "CallState", coverage_type: CoverageType
) -> OfferedPlans | None:
    """Return the plans just offered only for the same office, patient and visit type."""
    offered = state.insurance.offered
    if (
        offered is None
        or offered.office_key != state.call.called_office_key
        or offered.patient_revision != state.patient.revision
        or offered.coverage_type != coverage_type
    ):
        return None
    return offered


def clear_acceptance(state: "CallState") -> None:
    """Drop acceptance and fence any participation check still in flight."""
    state.insurance.accepted = None
    state.insurance.check_revision += 1


def rebind_acceptance(
    state: "CallState",
    checked: AcceptedInsurance,
    decision: InsuranceDecision | None,
    **changes,
) -> None:
    """Carry acceptance onto the patient revision a completed write produced."""
    if state.insurance.accepted is not checked:
        return
    if decision is None:
        clear_acceptance(state)
        return
    state.insurance.accepted = replace(
        checked, patient_revision=state.patient.revision, decision=decision, **changes
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
    if decision and same_office(decision.officeId, state.call.called_office_key):
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
