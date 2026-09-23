"""Complete eligibility evidence reaches Product or produces a visible failure."""

import json
import unittest
from dataclasses import replace

import httpx
from test_patient_resolution import CONFIG, call_state
from test_reporting import ACK

from abita_s2s.eligibility_contract import (
    EligibilityCheck,
    EligibilityInput,
    EligibilityResult,
)
from abita_s2s.runtime.reporting import CallReporter, ReportingError


class EligibilityCloseoutSizeTests(unittest.IsolatedAsyncioTestCase):
    async def test_multiple_large_checks_are_not_truncated(self):
        await self.deliver(201, "synthetic-evidence-" * 300000)

    async def test_oversized_rejection_is_visible_without_truncated_retry(self):
        await self.deliver(413, "synthetic-complete-response")

    async def deliver(self, closeout_status, evidence):
        state = call_state()
        for name in ("Jane", "John"):
            state.insurance.eligibility_checks.append(
                EligibilityCheck(
                    office="spring-hill",
                    request=EligibilityInput(
                        firstName=name,
                        lastName="Example",
                        dob="01/02/1980",
                        plan="Example Health",
                        memberId="synthetic-member",
                    ),
                    status="complete",
                    result=EligibilityResult(
                        status="review",
                        officeId="spring_hill",
                        checkedAt="2026-09-23T12:00:00Z",
                        providerResponse={"x12": evidence},
                    ),
                )
            )
        sent = []

        async def receive(request):
            body = json.loads(request.content)
            sent.append(body)
            if body["kind"] == "CLOSEOUT":
                if closeout_status == 201:
                    self.assertGreater(len(request.content), 8 * 1024 * 1024)
                    self.assertLess(len(request.content), 32 * 1024 * 1024)
                return httpx.Response(closeout_status, json=ACK)
            return httpx.Response(201, json=ACK)

        async def drain():
            pass

        async with httpx.AsyncClient(transport=httpx.MockTransport(receive)) as client:
            reporter = CallReporter(
                state.call,
                client,
                replace(
                    CONFIG,
                    interaction_url="https://product.test/v1/ai/interactions",
                    product_secret="synthetic",
                ),
                drain,
                insurance=state.insurance,
            )
            if closeout_status == 201:
                await reporter.finish()
            else:
                with (
                    self.assertRaises(ReportingError),
                    self.assertLogs("abita_s2s.runtime.reporting", "ERROR"),
                ):
                    await reporter.finish()
        self.assertEqual([p["kind"] for p in sent], ["START", "CLOSEOUT"])
        results = sent[-1]["closeoutPayload"]["eligibilityChecks"]
        self.assertEqual(len(results), 2)
        self.assertTrue(
            all(c["result"]["providerResponse"]["x12"] == evidence for c in results)
        )
