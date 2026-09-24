"""Eligibility has time to finish its upstream request without extending writes."""

import asyncio
import unittest
from unittest.mock import patch

import httpx
from test_new_patient_eligibility import details, result
from test_patient_resolution import CONFIG

from abita_s2s.integrations.registration_middleware import RegistrationMiddleware


class EligibilityDeadlineTests(unittest.IsolatedAsyncioTestCase):
    async def test_default_eligibility_budget_exceeds_upstream_without_extending_writes(
        self,
    ):
        requests = []

        async def handler(request):
            requests.append(request)
            return httpx.Response(200, json=result())

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            middleware = RegistrationMiddleware(client, CONFIG)
            with patch("asyncio.timeout", wraps=asyncio.timeout) as timeout:
                checked = await middleware.eligibility("spring-hill", details())
                self.assertEqual(checked.status, "active")
                self.assertEqual(timeout.call_args.args, (30,))
                self.assertEqual(requests[-1].extensions["timeout"]["read"], 30)
                # An invalid write receipt still exercises the actual write transport.
                await middleware.create("spring-hill", {})
                self.assertEqual(timeout.call_args.args, (20,))
                self.assertEqual(requests[-1].extensions["timeout"]["read"], 20)

    async def test_response_after_write_deadline_retains_evidence(self):
        async def handler(request):
            await asyncio.sleep(0.03)
            return httpx.Response(
                200, json=result(providerResponse={"benefitsInformation": []})
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            middleware = RegistrationMiddleware(
                client, CONFIG, deadline=0.001, eligibility_deadline=1
            )
            checked = await middleware.eligibility("spring-hill", details())
            self.assertIsNotNone(checked)
            self.assertEqual(checked.providerResponse, {"benefitsInformation": []})

    async def test_eligibility_deadline_remains_bounded(self):
        async def handler(request):
            await asyncio.Event().wait()

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            middleware = RegistrationMiddleware(
                client, CONFIG, eligibility_deadline=0.01
            )
            self.assertIsNone(await middleware.eligibility("spring-hill", details()))
