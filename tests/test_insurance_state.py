import unittest

from test_patient_resolution import call_state

from abita_s2s.insurance_contract import InsuranceDecision
from insurance_fixtures import decision

from abita_s2s.insurance_state import (
    AcceptedInsurance,
    accepted_insurance,
)
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
