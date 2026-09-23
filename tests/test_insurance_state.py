import unittest

from test_patient_resolution import call_state, receipt

from abita_s2s.insurance_contract import InsuranceDecision
from insurance_fixtures import decision

from abita_s2s.insurance_state import (
    AcceptedInsurance,
    accepted_insurance,
    insurance_ready,
    registration_insurance,
)
from abita_s2s.integrations.patient_middleware import Receipt
from abita_s2s.state import PatientAbsence


class InsuranceStateTests(unittest.TestCase):
    def test_absence_cannot_survive_a_new_resolution(self):
        state = call_state(None)
        absence = PatientAbsence("Jane", "01/02/1980", "spring-hill")
        state.patient.absence = absence
        checked = AcceptedInsurance(
            "spring-hill",
            0,
            None,
            absence,
            InsuranceDecision.model_validate(decision("Self Pay")),
        )
        state.insurance.accepted = checked
        self.assertIs(accepted_insurance(state, "medical"), checked)
        self.assertIsNone(accepted_insurance(state, "routine_vision"))
        state.patient.absence = PatientAbsence("Jane", "01/02/1980", "spring-hill")
        self.assertIsNone(accepted_insurance(state))

    def test_patient_revision_and_partial_write_guard(self):
        state = call_state(None)
        state.patient.active = Receipt.model_validate(receipt())
        state.insurance.accepted = AcceptedInsurance(
            "spring-hill",
            0,
            "chart-jane",
            None,
            InsuranceDecision.model_validate(decision("Self Pay")),
        )
        state.insurance.registrations["chart-jane"] = "created"
        self.assertTrue(insurance_ready(state))
        from dataclasses import replace

        original = state.insurance.accepted
        state.insurance.accepted = replace(
            original,
            decision=InsuranceDecision.model_validate(decision(office="hollywood")),
        )
        self.assertIsNone(registration_insurance(state, "medical"))
        self.assertTrue(insurance_ready(state))
        state.insurance.accepted = original
        state.insurance.registrations["chart-jane"] = "partial"
        self.assertFalse(insurance_ready(state))
        state.patient.revision += 1
        self.assertIsNone(accepted_insurance(state))

    def test_completed_chart_policy_is_backend_owned_after_context_changes(self):
        state = call_state(None)
        state.patient.active = Receipt.model_validate(receipt())
        state.insurance.registrations["chart-jane"] = "created"
        # A completed chart remains usable after acceptance was invalidated or
        # the visit changed; middleware verifies chart coverage for the visit.
        self.assertTrue(insurance_ready(state))
        self.assertIsNone(registration_insurance(state, "medical"))
        self.assertIsNone(registration_insurance(state, "routine_vision"))
