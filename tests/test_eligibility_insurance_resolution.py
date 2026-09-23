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
            dict(status="unmapped", plans=["Aetna Unknown Product"]),
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

    async def test_changed_plan_requires_new_eligibility(self):
        owner = self.setup_owner(
            dict(
                status="resolved",
                plans=["Aetna Better Health"],
                decision=decision("Aetna Better Health"),
            )
        )
        await owner.eligibility(details())
        self.assertEqual(
            (await owner.check("VSP", "routine_vision"))["outcome"], "needs_eligibility"
        )
        self.assertEqual(
            (await owner.add(registration(insuranceMemberId="test-member")))["outcome"],
            "needs_insurance",
        )
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

    async def test_unavailable_preserves_existing_policy(self):
        owner = self.setup_owner(dict(status="unavailable", plans=[]))
        await owner.check("Aetna", "medical")
        await owner.eligibility(details())
        self.assertEqual(
            (await owner.add(registration(insuranceMemberId="test-member")))["outcome"],
            "created",
        )
        self.assertEqual(self.writes[0]["insurance"], "Aetna")

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
