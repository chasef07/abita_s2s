"""One call's owners for a test, with stand-ins for the owners it does not exercise."""

from unittest.mock import Mock

from abita_s2s.call_record import (
    INSURANCE_WRITES,
    Registered,
    WriteKey,
    WriteSettled,
)
from abita_s2s.offices import SPRING_HILL
from abita_s2s.records import Receipt
from abita_s2s.tools.registry import Owners


def call_owners(office=SPRING_HILL, **given) -> Owners:
    scheduling = Mock()
    scheduling.appointments_text.return_value = ""
    stand_ins = {
        "resolver": Mock(),
        "scheduling": scheduling,
        "insurance": Mock(),
        "staff_tasks": Mock(),
        "call_control": Mock(),
    }
    return Owners(office, **(stand_ins | given))


def insurance_ready(state) -> bool:
    """Whether the active chart may schedule as far as insurance is concerned."""
    active = state.chart()
    return active is not None and state.record.insurance_ready(active.patientId)


def write_pending(state) -> bool:
    return state.record.in_flight(INSURANCE_WRITES)


def write_uncertain(state) -> bool:
    return bool(state.record.blockers(INSURANCE_WRITES))


def registered(state, patient_id, status) -> None:
    """Record that this call registered the chart, as a settled create would."""
    state.record.settled(
        WriteSettled(
            WriteKey(None, "register", patient_id),
            "written",
            {},
            registered=Registered(patient_id, status),
        )
    )


def insurance_key(patient_id="chart-jane") -> WriteKey:
    return WriteKey(patient_id, "update_insurance", "plan")


def insurance_write_open(state, patient_id="chart-jane") -> None:
    """An insurance write sent and not yet settled."""
    state.record.sent(insurance_key(patient_id))


def insurance_write_uncertain(state, patient_id="chart-jane") -> None:
    """An insurance write whose outcome could not be confirmed."""
    state.record.settled(WriteSettled(insurance_key(patient_id), "uncertain", {}))


def select(state, chart) -> Receipt:
    """Put the call on this chart as a verified load would."""
    receipt = chart if isinstance(chart, Receipt) else Receipt.model_validate(chart)
    position = state.record.loaded(receipt)
    state.patient.select(receipt.patientId, position)
    return receipt


def leave(state) -> None:
    """Take the call off its chart, as a new patient's details would."""
    state.patient.deselect()


def revoke(state) -> None:
    """Revoke the loaded appointments, as a rejected write would."""
    state.record.revoke(state.chart().patientId)


def coverage_written(state, carrier, carrier_id) -> None:
    """Record a confirmed insurance update on the current chart."""
    key = insurance_key(state.chart().patientId)
    state.record.settled(
        WriteSettled(key, "written", {}, coverage=(carrier, carrier_id))
    )
