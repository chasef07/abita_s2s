"""Registration must use the payer-resolved plan and reject stale selection retries."""

import asyncio
import json
import unittest

import httpx
from insurance_fixtures import decision
from test_insurance_registration import created, registration
import test_new_patient_eligibility as fixtures
from test_new_patient_eligibility import details, result


class ResolvedInsuranceTests(unittest.IsolatedAsyncioTestCase):
    owner = fixtures.EligibilityTests.owner

    # Reuse the real owner/HTTP fixture without inheriting its test cases.
    def setup_owner(self, resolution, *, gate=None):
        self.writes = []

        async def handler(request):
            if request.url.path.endswith("/decision"):
                return httpx.Response(200, json=decision())
            if request.url.path.endswith("/eligibility/check"):
                if gate:
                    await gate.wait()
                return httpx.Response(200, json=result(insuranceResolution=resolution))
            body = json.loads(request.content)
            self.writes.append(body)
            return httpx.Response(
                200, json=created(insuranceDecision=decision(body["insurance"]))
            )

        return self.owner(handler)

    async def test_specific_plan_reaches_readback_write_and_batch(self):
        owner = self.setup_owner(
            dict(
                status="resolved",
                plans=["Aetna Better Health"],
                decision=decision("Aetna Better Health"),
            )
        )
        await owner.check("Aetna", "medical")
        await owner.eligibility(details())
        batch = owner.state.insurance.current_eligibility[1]
        await owner.check("Aetna", "medical")
        readback = await owner.add(
            registration(insuranceMemberId="test-member", readBack=None)
        )
        self.assertIn("Aetna Better Health", readback["answer"])
        self.assertEqual(
            (await owner.add(registration(insuranceMemberId="test-member")))["outcome"],
            "created",
        )
        self.assertEqual(self.writes[0]["insurance"], "Aetna Better Health")
        self.assertEqual(batch.patient_id, "new-chart")
        self.assertEqual(batch.canonical_plan, "Aetna Better Health")

    async def test_unresolved_and_rejected_plans_cannot_restore_generic_acceptance(
        self,
    ):
        for resolution in [
            dict(
                status="resolved",
                plans=["CarePlus"],
                decision=decision(
                    "",
                    participation="unknown",
                    outcome="needs_staff_task",
                    canSchedule=False,
                ),
            ),
            dict(
                status="conflicting", plans=["Aetna Commercial", "Aetna Better Health"]
            ),
            dict(
                status="resolved",
                plans=["Rejected"],
                decision=decision(
                    "Rejected",
                    participation="not_accepted",
                    outcome="not_accepted",
                    canSchedule=False,
                ),
            ),
            dict(
                status="resolved",
                plans=["Other office"],
                decision=decision(office="hollywood"),
            ),
        ]:
            with self.subTest(resolution=resolution):
                owner = self.setup_owner(resolution)
                await owner.check("Aetna", "medical")
                await owner.eligibility(details())
                await owner.check("Aetna", "medical")
                self.assertNotEqual(
                    (await owner.add(registration(insuranceMemberId="test-member")))[
                        "outcome"
                    ],
                    "created",
                )
                self.assertEqual(self.writes, [])

    async def test_registration_identity_and_member_must_match(self):
        for change in [
            dict(firstName="Other"),
            dict(lastName="Other"),
            dict(dob="02/03/1990"),
            dict(insuranceMemberId="other"),
        ]:
            with self.subTest(change=change):
                owner = self.setup_owner(
                    dict(
                        status="resolved",
                        plans=["Aetna Better Health"],
                        decision=decision("Aetna Better Health"),
                    )
                )
                await owner.eligibility(details())
                response = await owner.add(
                    registration(**(dict(insuranceMemberId="test-member") | change))
                )
                self.assertEqual(response["outcome"], "needs_eligibility")
                self.assertEqual(self.writes, [])

    async def test_pending_check_blocks_generic_write(self):
        gate = asyncio.Event()
        owner = self.setup_owner(
            dict(
                status="resolved",
                plans=["Aetna Better Health"],
                decision=decision("Aetna Better Health"),
            ),
            gate=gate,
        )
        await owner.check("Aetna", "medical")
        pending = asyncio.create_task(owner.eligibility(details()))
        await asyncio.sleep(0)
        try:
            self.assertEqual(
                (await owner.add(registration(insuranceMemberId="test-member")))[
                    "outcome"
                ],
                "eligibility_pending",
            )
            self.assertEqual(self.writes, [])
        finally:
            gate.set()
            await pending

    async def test_pending_result_respects_latest_coverage(self):
        for coverage in ("medical", "routine_vision"):
            with self.subTest(coverage=coverage):
                entered, release = asyncio.Event(), asyncio.Event()
                writes = []

                async def handler(request):
                    body = json.loads(request.content)
                    if request.url.path.endswith("/decision"):
                        rejected = body["coverageType"] == "routine_vision"
                        return httpx.Response(
                            200,
                            json=decision(
                                coverage=body["coverageType"],
                                participation="not_accepted"
                                if rejected
                                else "accepted",
                                outcome="not_accepted" if rejected else "accepted",
                                canSchedule=not rejected,
                            ),
                        )
                    if request.url.path.endswith("/eligibility/check"):
                        entered.set()
                        await release.wait()
                        return httpx.Response(
                            200,
                            json=result(
                                insuranceResolution=dict(
                                    status="resolved",
                                    plans=["Aetna Better Health"],
                                    decision=decision("Aetna Better Health"),
                                )
                            ),
                        )
                    writes.append(body)
                    return httpx.Response(
                        200, json=created(insuranceDecision=decision(body["insurance"]))
                    )

                owner = self.owner(handler)
                await owner.check("Aetna", "medical")
                pending = asyncio.create_task(owner.eligibility(details()))
                await entered.wait()
                batch = owner.state.insurance.current_eligibility[1]
                try:
                    await owner.check("Aetna", coverage)
                finally:
                    release.set()
                answer = await pending
                registered = await owner.add(
                    registration(insuranceMemberId="test-member")
                )
                if coverage == "routine_vision":
                    self.assertTrue(answer.startswith("stale:"))
                    self.assertEqual(registered["outcome"], "needs_insurance")
                    self.assertEqual(writes, [])
                    self.assertIsNone(batch.patient_id)
                else:
                    self.assertEqual(registered["outcome"], "created")
                    self.assertEqual(writes[0]["insurance"], "Aetna Better Health")
                    self.assertEqual(batch.patient_id, "new-chart")
                    self.assertEqual(len(owner.state.insurance.eligibility_checks), 1)

    async def test_completed_result_cannot_apply_after_coverage_changes(self):
        release = asyncio.Event()
        owner = self.setup_owner(
            dict(
                status="resolved",
                plans=["Aetna Better Health"],
                decision=decision("Aetna Better Health"),
            ),
            gate=release,
        )
        await owner.check("Aetna", "medical")
        check = owner._start_eligibility(details())
        changed = []
        # Queue the coverage change before the eligibility waiter resumes.
        check.task.add_done_callback(
            lambda _: changed.append(
                asyncio.create_task(owner.check("Aetna", "routine_vision"))
            )
        )
        pending = asyncio.create_task(owner.eligibility(details()))
        await asyncio.sleep(0)
        release.set()
        answer = await pending
        self.assertEqual((await changed[0])["outcome"], "needs_eligibility")
        self.assertTrue(answer.startswith("stale:"))
        self.assertEqual(
            (await owner.add(registration(insuranceMemberId="test-member")))["outcome"],
            "needs_insurance",
        )
        self.assertEqual(self.writes, [])

    async def test_late_generic_participation_cannot_overwrite_resolution(self):
        entered, release = asyncio.Event(), asyncio.Event()

        async def handler(request):
            if request.url.path.endswith("/decision"):
                entered.set()
                await release.wait()
                return httpx.Response(200, json=decision())
            return httpx.Response(
                200,
                json=result(
                    insuranceResolution=dict(
                        status="resolved",
                        plans=["Aetna Better Health"],
                        decision=decision("Aetna Better Health"),
                    )
                ),
            )

        owner = self.owner(handler)
        pending = asyncio.create_task(owner.check("Aetna", "medical"))
        await entered.wait()
        try:
            await owner.eligibility(details())
        finally:
            release.set()
        self.assertEqual((await pending)["outcome"], "stale")
        self.assertEqual(
            owner.state.insurance.accepted.decision.canonicalPlan, "Aetna Better Health"
        )

    async def test_new_member_unavailable_does_not_reuse_previous_resolved_plan(self):
        owner = self.setup_owner(
            dict(
                status="resolved",
                plans=["Aetna Better Health"],
                decision=decision("Aetna Better Health"),
            )
        )
        await owner.eligibility(details())

        # A failed second check must not leave the first member's accepted plan.
        async def unavailable(*args):
            return None

        owner._middleware.eligibility = unavailable
        await owner.eligibility(details(memberId="other"))
        self.assertIsNone(owner.state.insurance.accepted)
        self.assertEqual(
            (await owner.add(registration(insuranceMemberId="other")))["outcome"],
            "needs_insurance",
        )
        self.assertEqual(self.writes, [])

    async def test_unmapped_plan_falls_back_and_keeps_patient_evidence(self):
        for check_first in (True, False):
            with self.subTest(check_first=check_first):
                owner = self.setup_owner(
                    dict(status="unmapped", plans=["Aetna Unknown Product"])
                )
                if check_first:
                    await owner.check("Aetna", "medical")
                answer = await owner.eligibility(details())
                self.assertIn("no mapping", answer)
                batch = owner.state.insurance.current_eligibility[1]
                await owner.check("Aetna", "medical")
                self.assertEqual(
                    (await owner.add(registration(insuranceMemberId="wrong")))[
                        "outcome"
                    ],
                    "needs_eligibility",
                )
                self.assertEqual(
                    (await owner.add(registration(insuranceMemberId="test-member")))[
                        "outcome"
                    ],
                    "created",
                )
                self.assertEqual(self.writes[0]["insurance"], "Aetna")
                self.assertEqual(batch.patient_id, "new-chart")
                self.assertEqual(batch.result.insuranceResolution.status, "unmapped")
