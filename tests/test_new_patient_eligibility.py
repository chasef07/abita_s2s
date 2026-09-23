"""Background intake evidence remains isolated from chart and coverage decisions."""

import asyncio
import json
import unittest
from types import SimpleNamespace

import httpx
from test_patient_resolution import CONFIG, call_state, receipt

from abita_s2s.eligibility_contract import EligibilityInput
from abita_s2s.identity import PatientResolver
from abita_s2s.insurance import InsuranceRegistration
from abita_s2s.integrations.patient_middleware import PatientMiddleware, Receipt
from abita_s2s.integrations.registration_middleware import RegistrationMiddleware
from abita_s2s.tools.insurance import InsuranceTools


def details(**changes):
    return EligibilityInput(
        **(
            dict(
                firstName="Jane",
                lastName="Doe",
                dob="01/02/1980",
                plan="Aetna",
                memberId="test-member",
            )
            | changes
        )
    )


def result(**changes):
    return (
        dict(
            status="active",
            officeId="spring_hill",
            checkedAt="2026-09-23T12:00:00Z",
            identity=dict(status="exact_name_dob", reviewRequired=False, reasons=None),
            checkId="check-example",
        )
        | changes
    )


class EligibilityTests(unittest.IsolatedAsyncioTestCase):
    def owner(self, handler, deadline=20):
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        self.addAsyncCleanup(client.aclose)
        state = call_state()
        resolver = PatientResolver(state, PatientMiddleware(client, CONFIG))
        self.addAsyncCleanup(resolver.aclose)
        owner = InsuranceRegistration(
            state, resolver, RegistrationMiddleware(client, CONFIG, deadline=deadline)
        )
        self.addAsyncCleanup(owner.aclose)
        return owner

    async def test_tool_returns_before_response_and_deduplicates(self):
        release = asyncio.Event()
        entered = asyncio.Event()
        requests = []

        async def handler(request):
            requests.append(request)
            entered.set()
            await release.wait()
            return httpx.Response(200, json=result())

        owner = self.owner(handler)
        tool = InsuranceTools(owner).check_new_patient_eligibility
        context = SimpleNamespace(userdata=owner.state)
        args = details().model_dump()
        args["insuranceMemberId"] = args.pop("memberId")
        try:
            answer = await asyncio.wait_for(tool(context, **args), 1)
            self.assertTrue(answer.startswith("started:"))
            await asyncio.wait_for(entered.wait(), 1)
            self.assertEqual(
                owner.state.insurance.eligibility_checks[0].status, "pending"
            )
            self.assertTrue(
                (await tool(context, **args)).startswith("already_started:")
            )
        finally:
            release.set()
        await owner.aclose()
        check = owner.state.insurance.eligibility_checks[0]
        self.assertEqual(check.result.status, "active")
        self.assertEqual(check.result.checkId, "check-example")
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0].url.path, "/api/eligibility/check")
        self.assertEqual(requests[0].headers["Authorization"], "test-auth")
        self.assertEqual(json.loads(requests[0].content)["memberId"], "test-member")
        self.assertIsNone(owner.state.patient.active)
        self.assertIsNone(owner.state.insurance.accepted)

    async def test_corrections_and_patient_switch_keep_separate_evidence(self):
        release = asyncio.Event()

        async def handler(request):
            body = json.loads(request.content)
            if body["memberId"] == "test-member":
                await release.wait()
            return httpx.Response(200, json=result(checkId=body["memberId"]))

        owner = self.owner(handler)
        try:
            owner.start_eligibility(details())
            owner.start_eligibility(details(memberId="corrected"))
            owner.state.patient.active = Receipt.model_validate(receipt())
            owner.state.insurance.registrations["chart-jane"] = "created"
            self.assertTrue(
                owner.start_eligibility(details(firstName="John")).startswith(
                    "started:"
                )
            )
            await asyncio.sleep(0)
        finally:
            release.set()
        await owner.aclose()
        checks = owner.state.insurance.eligibility_checks
        self.assertEqual(len(checks), 3)
        self.assertEqual(
            [c.result.checkId for c in checks],
            ["test-member", "corrected", "test-member"],
        )
        self.assertEqual(checks[2].request.firstName, "John")
        self.assertEqual(owner.state.patient.active.patientId, "chart-jane")

    async def test_review_unknown_and_malformed_responses(self):
        cases = [
            (
                httpx.Response(
                    200,
                    json=result(
                        status="review",
                        reviewReason="identity_uncertain",
                        identity=dict(
                            status="identity_conflict",
                            reviewRequired=True,
                            reasons=["last_name_mismatch"],
                        ),
                    ),
                ),
                "complete",
                "review",
            ),
            (
                httpx.Response(
                    200,
                    json=result(
                        status="unknown",
                        reviewReason="unrecognized_response",
                        identity=None,
                    ),
                ),
                "complete",
                "unknown",
            ),
            (httpx.Response(200, content=b"invalid"), "unavailable", None),
            (httpx.Response(200, json={"status": "active"}), "unavailable", None),
            (httpx.Response(200, json=result(identity=None)), "unavailable", None),
            (httpx.Response(200, json=result(officeId="wrong")), "unavailable", None),
            (httpx.Response(503), "unavailable", None),
        ]
        for response, status, coverage in cases:
            with self.subTest(status=status, body=response.content):
                owner = self.owner(lambda request: response)
                owner.start_eligibility(details())
                await owner.aclose()
                check = owner.state.insurance.eligibility_checks[0]
                self.assertEqual(check.status, status)
                self.assertEqual(
                    check.result.status if check.result else None, coverage
                )
                self.assertIsNone(owner.state.insurance.accepted)

    async def test_timeout_is_stored_without_retry(self):
        async def handler(request):
            await asyncio.Event().wait()

        owner = self.owner(handler, deadline=0.01)
        owner.start_eligibility(details())
        await asyncio.sleep(0.03)
        self.assertTrue(
            owner.start_eligibility(details()).startswith("already_started:")
        )
        self.assertEqual(
            owner.state.insurance.eligibility_checks[0].status, "unavailable"
        )

    async def test_exclusions_and_closed_admission_send_nothing(self):
        def handler(request):
            self.fail("No eligibility request should be sent")

        owner = self.owner(handler)
        self.assertTrue(
            owner.start_eligibility(details(plan="Self Pay")).startswith("skipped:")
        )
        self.assertTrue(
            owner.start_eligibility(details(dob="invalid")).startswith("needs_input:")
        )
        owner.state.patient.active = Receipt.model_validate(receipt())
        self.assertTrue(owner.start_eligibility(details()).startswith("skipped:"))
        owner.close_admission()
        self.assertTrue(owner.start_eligibility(details()).startswith("unavailable:"))
        self.assertEqual(owner.state.insurance.eligibility_checks, [])
