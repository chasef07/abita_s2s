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
            state,
            resolver,
            RegistrationMiddleware(
                client, CONFIG, deadline=deadline, eligibility_deadline=deadline
            ),
        )
        self.addAsyncCleanup(owner.aclose)
        return owner

    async def test_tool_waits_for_response_and_deduplicates(self):
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
        first = asyncio.create_task(tool(context, **args))
        second = asyncio.create_task(tool(context, **args))
        try:
            await asyncio.wait_for(entered.wait(), 1)
            self.assertFalse(first.done())
            self.assertFalse(second.done())
            self.assertEqual(
                owner.state.insurance.eligibility_checks[0].status, "pending"
            )
        finally:
            release.set()
        self.assertTrue((await first).startswith("eligibility: active"))
        self.assertEqual(await second, await tool(context, **args))
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

    async def test_corrected_name_reaches_tool_without_raw_benefits(self):
        response = result(
            identity=dict(status="matched_with_name_correction", reviewRequired=False),
            matchedPatient=dict(
                firstName="Jane",
                lastName="Doe",
                dateOfBirth="19800102",
                memberId="test-member",
            ),
            providerResponse={"privateMarker": "not-model-visible"},
        )
        owner = self.owner(lambda request: httpx.Response(200, json=response))
        answer = await owner.eligibility(details(firstName="Ane", lastName="Doe Jr."))
        self.assertIn("name_correction:", answer)
        self.assertIn("Jane", answer)
        self.assertNotIn("not-model-visible", answer)
        self.assertIsNone(owner.state.patient.active)
        self.assertIsNone(owner.state.insurance.accepted)

    async def test_late_correction_cannot_replace_newer_intake(self):
        release = asyncio.Event()
        entered = asyncio.Event()

        async def handler(request):
            body = json.loads(request.content)
            if body["firstName"] == "Ane":
                entered.set()
                await release.wait()
            return httpx.Response(
                200,
                json=result(
                    identity=dict(
                        status="matched_with_name_correction", reviewRequired=False
                    ),
                    matchedPatient=dict(
                        firstName="Jane",
                        lastName="Doe",
                        dateOfBirth="19800102",
                        memberId=body["memberId"],
                    ),
                ),
            )

        owner = self.owner(handler)
        pending = asyncio.create_task(owner.eligibility(details(firstName="Ane")))
        await entered.wait()
        await owner.eligibility(details(firstName="John", memberId="other"))
        release.set()
        self.assertTrue((await pending).startswith("stale:"))
        self.assertEqual(len(owner.state.insurance.eligibility_checks), 2)

    async def test_cached_check_can_be_reselected_after_another_intake(self):
        calls = []

        def handler(request):
            calls.append(request)
            return httpx.Response(200, json=result())

        owner = self.owner(handler)
        await owner.eligibility(details())
        await owner.eligibility(details(firstName="John", memberId="other"))
        answer = await owner.eligibility(details())
        self.assertTrue(answer.startswith("eligibility: active"))
        self.assertEqual(len(calls), 2)
        self.assertIs(
            owner.state.insurance.current_eligibility[1],
            owner.state.insurance.eligibility_checks[0],
        )

    async def test_existing_chart_allows_different_new_patient_intake(self):
        owner = self.owner(lambda request: httpx.Response(200, json=result()))
        owner.state.patient.active = Receipt.model_validate(receipt())
        self.assertTrue((await owner.eligibility(details())).startswith("skipped:"))
        answer = await owner.eligibility(details(firstName="John", memberId="other"))
        self.assertTrue(answer.startswith("eligibility: active"))
        self.assertIsNone(owner.state.patient.active)
        self.assertIsNone(owner.state.insurance.accepted)

    async def test_patient_revision_change_hides_late_name(self):
        entered, release = asyncio.Event(), asyncio.Event()

        async def handler(request):
            entered.set()
            await release.wait()
            return httpx.Response(200, json=result())

        owner = self.owner(handler)
        waiting = asyncio.create_task(owner.eligibility(details()))
        await entered.wait()
        owner.state.patient.revision += 1
        release.set()
        self.assertTrue((await waiting).startswith("stale:"))
        self.assertEqual(owner.state.insurance.eligibility_checks[0].status, "complete")

    async def test_cancelled_tool_keeps_one_request_and_retains_result(self):
        entered, release = asyncio.Event(), asyncio.Event()
        calls = []

        async def handler(request):
            calls.append(request)
            entered.set()
            await release.wait()
            return httpx.Response(200, json=result())

        owner = self.owner(handler)
        waiting = asyncio.create_task(owner.eligibility(details()))
        await entered.wait()
        waiting.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await waiting
        release.set()
        self.assertTrue(
            (await owner.eligibility(details())).startswith("eligibility: active")
        )
        self.assertEqual(len(calls), 1)

    async def test_correction_requires_bound_member_and_dob(self):
        for matched in [
            None,
            dict(
                firstName="Jane",
                lastName="Doe",
                dateOfBirth="19800102",
                memberId="other",
            ),
            dict(
                firstName="Jane",
                lastName="Doe",
                dateOfBirth="19800103",
                memberId="test-member",
            ),
        ]:
            owner = self.owner(
                lambda request: httpx.Response(
                    200,
                    json=result(
                        identity=dict(
                            status="matched_with_name_correction", reviewRequired=False
                        ),
                        matchedPatient=matched,
                    ),
                )
            )
            self.assertTrue(
                (await owner.eligibility(details(firstName="Ane"))).startswith(
                    "unavailable:"
                )
            )

    async def test_corrections_and_patient_switch_keep_separate_evidence(self):
        release = asyncio.Event()

        async def handler(request):
            body = json.loads(request.content)
            if body["memberId"] == "test-member":
                await release.wait()
            return httpx.Response(200, json=result(checkId=body["memberId"]))

        owner = self.owner(handler)
        try:
            owner._start_eligibility(details())
            owner._start_eligibility(details(memberId="corrected"))
            owner.state.patient.active = Receipt.model_validate(receipt())
            owner.state.insurance.registrations["chart-jane"] = "created"
            self.assertTrue(
                not isinstance(owner._start_eligibility(details(firstName="John")), str)
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
        self.assertIsNone(owner.state.patient.active)

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
                owner._start_eligibility(details())
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
        owner._start_eligibility(details())
        await asyncio.sleep(0.03)
        self.assertTrue(
            owner._start_eligibility(details())
            is owner.state.insurance.eligibility_checks[0]
        )
        self.assertEqual(
            owner.state.insurance.eligibility_checks[0].status, "unavailable"
        )

    async def test_exclusions_and_closed_admission_send_nothing(self):
        def handler(request):
            self.fail("No eligibility request should be sent")

        owner = self.owner(handler)
        self.assertTrue(
            owner._start_eligibility(details(plan="Self Pay")).startswith("skipped:")
        )
        self.assertTrue(
            owner._start_eligibility(details(dob="invalid")).startswith("needs_input:")
        )
        owner.state.patient.active = Receipt.model_validate(receipt())
        self.assertTrue(owner._start_eligibility(details()).startswith("skipped:"))
        owner.close_admission()
        self.assertTrue(owner._start_eligibility(details()).startswith("unavailable:"))
        self.assertEqual(owner.state.insurance.eligibility_checks, [])
