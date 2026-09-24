"""Transient participation failures must not erase a proven eligibility correction."""

import unittest

import httpx
from insurance_fixtures import decision
from test_insurance_registration import registration
from test_new_patient_eligibility import details, result
from test_patient_resolution import CONFIG, call_state

from abita_s2s.identity import PatientResolver
from abita_s2s.insurance import InsuranceRegistration
from abita_s2s.integrations.patient_middleware import PatientMiddleware
from abita_s2s.integrations.registration_middleware import RegistrationMiddleware


class EligibilityPlanBindingTests(unittest.IsolatedAsyncioTestCase):
    async def test_canonical_retry_keeps_original_alias_correction(self):
        for eligibility_first in (False, True):
            with self.subTest(eligibility_first=eligibility_first):
                decisions = 0
                creations = []

                def handler(request):
                    nonlocal decisions
                    if request.url.path.endswith("/decision"):
                        decisions += 1
                        if decisions == 2:
                            return httpx.Response(503)
                        return httpx.Response(200, json=decision("Aetna Commercial"))
                    if request.url.path.endswith("/eligibility/check"):
                        return httpx.Response(
                            200,
                            json=result(
                                identity={
                                    "status": "matched_with_name_correction",
                                    "reviewRequired": False,
                                },
                                matchedPatient={
                                    "firstName": "Jane",
                                    "lastName": "Doe",
                                    "dateOfBirth": "19800102",
                                    "memberId": "test-member",
                                },
                            ),
                        )
                    creations.append(request)
                    return httpx.Response(500)

                async with httpx.AsyncClient(
                    transport=httpx.MockTransport(handler)
                ) as client:
                    state = call_state()
                    resolver = PatientResolver(state, PatientMiddleware(client, CONFIG))
                    owner = InsuranceRegistration(
                        state, resolver, RegistrationMiddleware(client, CONFIG)
                    )
                    intake = details(firstName="Ane", plan="Aetna Employer Plan")
                    if eligibility_first:
                        await owner.eligibility(intake)
                    await owner.check("Aetna Employer Plan", "medical")
                    if not eligibility_first:
                        await owner.eligibility(intake)
                    self.assertEqual(
                        (await owner.check("Aetna Commercial", "medical"))["outcome"],
                        "unavailable",
                    )
                    self.assertEqual(
                        (await owner.check("Aetna Commercial", "medical"))["outcome"],
                        "accepted",
                    )
                    blocked = await owner.add(
                        registration(
                            firstName="Ane",
                            subscriberName="Ane Doe",
                            insuranceMemberId="test-member",
                        )
                    )
                    self.assertEqual(blocked["outcome"], "needs_name_confirmation")
                    self.assertEqual(creations, [])
                    self.assertEqual(
                        state.insurance.eligibility_checks[0].request.plan,
                        "Aetna Employer Plan",
                    )
                    await owner.aclose()
                    await resolver.aclose()
