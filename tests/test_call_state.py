import unittest
from dataclasses import FrozenInstanceError
from datetime import datetime, timezone

from abita_s2s.identity import (
    activate_verified_patient,
    apply_candidate_lookup,
    begin_lookup,
    clear_active_patient,
)
from abita_s2s.state import (
    CallContext, CallState, CandidateLookup, PatientCandidate, PatientState, VerifiedPatient,
)


class CallStateTests(unittest.TestCase):
    def test_calls_do_not_share_patient_state(self):
        context = CallContext("call", datetime.now(timezone.utc), "customer", "office")
        first, second = CallState(context), CallState(context)
        patient = VerifiedPatient("chart", "Test Person", "1990-01-01")
        activate_verified_patient(first.patient, begin_lookup(first.patient), patient)
        self.assertIsNone(second.patient.active)
        self.assertNotIn("Test Person", repr(first))
        self.assertNotIn("1990-01-01", repr(patient))
        with self.assertRaises(FrozenInstanceError):
            context.called_office_key = "different"

    def test_candidates_never_activate_patient(self):
        state = PatientState()
        candidate = PatientCandidate("chart", "Test", "Person", "1990-01-01")
        result = CandidateLookup("found", (candidate,))
        self.assertTrue(apply_candidate_lookup(state, begin_lookup(state), result))
        self.assertIsNone(state.active)
        patient = VerifiedPatient("verified", "Other Person", "1980-01-01")
        activate_verified_patient(state, begin_lookup(state), patient)
        self.assertTrue(apply_candidate_lookup(state, begin_lookup(state), result))
        self.assertIs(state.active, patient)

    def test_stale_cross_call_and_replayed_reads_are_rejected(self):
        state = PatientState()
        old = begin_lookup(state)
        latest = begin_lookup(state)
        patient = VerifiedPatient("chart", "Test Person", "1990-01-01")
        self.assertFalse(activate_verified_patient(state, old, patient))
        self.assertFalse(activate_verified_patient(PatientState(), latest, patient))
        self.assertTrue(activate_verified_patient(state, latest, patient))
        self.assertFalse(activate_verified_patient(state, latest, patient))
        self.assertFalse(activate_verified_patient(state, None, patient))

    def test_switch_clears_patient_and_fences_old_results(self):
        state = PatientState()
        patient = VerifiedPatient("chart", "Test Person", "1990-01-01")
        activate_verified_patient(state, begin_lookup(state), patient)
        revision = state.revision
        token = begin_lookup(state)
        self.assertEqual(state.revision, revision)
        clear_active_patient(state)
        self.assertIsNone(state.active)
        self.assertGreater(state.revision, revision)
        self.assertFalse(activate_verified_patient(state, token, patient))
        self.assertFalse(apply_candidate_lookup(state, token, CandidateLookup("none")))

    def test_failure_is_distinct_from_no_match(self):
        state = PatientState()
        apply_candidate_lookup(state, begin_lookup(state), CandidateLookup("failed", failure_reason="network_error"))
        self.assertEqual(state.lookup.status, "failed")
        apply_candidate_lookup(state, begin_lookup(state), CandidateLookup("none"))
        self.assertEqual(state.lookup.status, "none")
        self.assertIsNone(state.lookup.failure_reason)
        with self.assertRaises(ValueError):
            CandidateLookup("unexpected")
        with self.assertRaises(ValueError):
            CandidateLookup("failed")
        with self.assertRaises(ValueError):
            CandidateLookup("found")

    def test_completed_candidate_read_cannot_be_replayed(self):
        state = PatientState()
        token = begin_lookup(state)
        self.assertTrue(apply_candidate_lookup(state, token, CandidateLookup("none")))
        self.assertFalse(apply_candidate_lookup(state, token, CandidateLookup("failed", failure_reason="network_error")))
        self.assertEqual(state.lookup.status, "none")
