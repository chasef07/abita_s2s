import asyncio
import json
import unittest
from unittest.mock import patch

import httpx
from test_patient_resolution import CONFIG, receipt

from abita_s2s.config import Config, load_config
from abita_s2s.middleware import Failure, PatientMiddleware, Receipt
from abita_s2s.offices import OFFICES


class PatientMiddlewareTests(unittest.IsolatedAsyncioTestCase):
    async def test_trusted_routing_and_auth_for_all_offices(self):
        requests = []

        def handler(request):
            requests.append(request)
            return httpx.Response(200, json=receipt())

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            middleware = PatientMiddleware(client, CONFIG)
            for office in OFFICES:
                self.assertIsInstance(
                    await middleware.resolve(office.key, {"phone": "+15555550101"}),
                    Receipt,
                )
                request = requests[-1]
                self.assertEqual(
                    str(request.url), "https://middleware.test/api/patient/resolve"
                )
                self.assertEqual(request.headers["authorization"], "test-auth")
                self.assertEqual(
                    json.loads(request.content)["office"], office.trunk_numbers[0]
                )
            with self.assertRaises(ValueError):
                await middleware.resolve("unknown", {})
            self.assertEqual(len(requests), len(OFFICES))

    async def test_only_eligible_failures_retry_once(self):
        for status, body, count in [
            (503, {}, 2),
            (408, {}, 2),
            (429, {}, 2),
            (401, {}, 1),
            (400, {}, 1),
            (302, {}, 1),
            (200, {"status": "error"}, 2),
            (200, {"status": "verified"}, 1),
            (200, [], 1),
        ]:
            requests = []

            def handler(request, requests=requests, status=status, body=body):
                requests.append(request)
                return httpx.Response(
                    status, json=body, headers={"location": "https://other.test"}
                )

            async with httpx.AsyncClient(
                transport=httpx.MockTransport(handler)
            ) as client:
                result = await PatientMiddleware(client, CONFIG).resolve(
                    "spring-hill", {"phone": "+15555550101"}
                )
                self.assertIsInstance(result, Failure)
                self.assertEqual(len(requests), count)

    async def test_network_retry_can_recover(self):
        count = 0

        def handler(request):
            nonlocal count
            count += 1
            if count == 1:
                raise httpx.ConnectError("offline")
            return httpx.Response(200, json=receipt())

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            self.assertIsInstance(
                await PatientMiddleware(client, CONFIG).resolve("spring-hill", {}),
                Receipt,
            )
        self.assertEqual(count, 2)

    async def test_total_deadline_cancels_transport(self):
        cancelled = asyncio.Event()

        async def handler(request):
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            result = await PatientMiddleware(client, CONFIG, deadline=0.01).resolve(
                "spring-hill", {}
            )
        self.assertEqual(result.reason, "timeout")
        self.assertTrue(cancelled.is_set())

    async def test_unconfigured_does_not_make_requests(self):
        def handler(request):
            self.fail("Unconfigured client made a request")

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            result = await PatientMiddleware(client, Config("offline")).resolve(
                "spring-hill", {}
            )
        self.assertEqual(result.reason, "not_configured")

    def test_configuration_requires_pair_and_safe_transport(self):
        for env in (
            {"AMD_API_URL": "https://middleware.test"},
            {"AMD_API_TOKEN": "secret"},
            {"AMD_API_URL": "http://remote.test", "AMD_API_TOKEN": "secret"},
            {
                "AMD_API_URL": "https://user:secret@remote.test",
                "AMD_API_TOKEN": "secret",
            },
        ):
            with (
                patch.dict(
                    "os.environ", {"OPENAI_API_KEY": "offline", **env}, clear=True
                ),
                self.assertRaises(ValueError),
            ):
                load_config()
        with patch.dict(
            "os.environ",
            {
                "OPENAI_API_KEY": "offline",
                "AMD_API_URL": "http://localhost:8080",
                "AMD_API_TOKEN": "secret",
            },
            clear=True,
        ):
            config = load_config()
            self.assertEqual(config.middleware_url, "http://localhost:8080")
            self.assertNotIn("secret", repr(config))
