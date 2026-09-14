"""Load private patient evidence without selecting a patient or changing prompts."""

from abita_s2s.identity import apply_candidate_lookup, begin_lookup
from abita_s2s.middleware import (
    HydratedPatient,
    MiddlewareClient,
    MiddlewareFailure,
    PatientMatches,
    PatientNotFound,
)
from abita_s2s.state import CallState, CandidateLookup


async def precall_lookup(
    state: CallState, middleware: MiddlewareClient, *, office: str
) -> None:
    phone = state.call.caller_phone
    if not phone:
        return
    token = begin_lookup(state.patient)
    result = await middleware.resolve_patient(office=office, phone=phone)
    if isinstance(result, HydratedPatient):
        lookup = CandidateLookup("found", (result,))
    elif isinstance(result, PatientMatches):
        lookup = CandidateLookup("found", result.matches)
    elif isinstance(result, MiddlewareFailure):
        lookup = CandidateLookup("failed", failure_reason=result.reason)
    elif isinstance(result, PatientNotFound):
        lookup = CandidateLookup("none")
    else:
        raise TypeError("Unsupported patient-resolution result")
    apply_candidate_lookup(state.patient, token, lookup)
