"""Own lookup freshness and patient changes; matching policy is not migrated yet."""

from abita_s2s.state import CandidateLookup, PatientState, VerifiedPatient


def begin_lookup(state: PatientState) -> object:
    """Supersede older reads without changing the patient context.

    Start pre-call lookup before interactive resolution, never after it.
    """
    token = object()
    state._lookup_token = token
    return token


def apply_candidate_lookup(
    state: PatientState, token: object, result: CandidateLookup
) -> bool:
    """Store private evidence without activating or replacing a patient."""
    if state._lookup_token is None or token is not state._lookup_token:
        return False
    state._lookup_token = None
    state.lookup = result
    return True


def activate_verified_patient(
    state: PatientState, token: object, patient: VerifiedPatient
) -> bool:
    """Commit a current read after the caller/record verification workflow passes.

    This does not verify identity and must not accept model-supplied chart IDs.
    Write receipts (including chart creation) need their own commit semantics.
    """
    if state._lookup_token is None or token is not state._lookup_token:
        return False
    if not patient.patient_id.strip():
        raise ValueError("Verified patient requires a chart ID")
    state._lookup_token = None
    if state.active is None or state.active.patient_id != patient.patient_id:
        state.revision += 1
    state.active = patient
    return True


def clear_active_patient(state: PatientState) -> None:
    """Begin a patient switch immediately; late reads cannot restore the old chart."""
    state._lookup_token = None
    state.active = None
    state.revision += 1
