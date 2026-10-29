"""Exercise migration boundaries with real composition and offline HTTP."""

import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import httpx
from test_patient_resolution import call_state

from abita_s2s.call_control import CallControl
from abita_s2s.config import load_config
from abita_s2s.handoff import AdmissionRejected
from abita_s2s.integrations.product import Product
from abita_s2s.runtime.silence import BackendResponses


class MigrationRegressionTests(unittest.IsolatedAsyncioTestCase):
    async def test_configured_loopback_handoff_reaches_http(self):
        requests = []

        def handler(request):
            requests.append(request)
            return httpx.Response(
                201,
                json={
                    "id": "00000000-0000-4000-8000-000000000002",
                    "expiresAt": (datetime.now(UTC) + timedelta(minutes=2)).isoformat(),
                    "sipDestination": "sip:acuity-handoff@product.example",
                },
            )

        with patch.dict(
            "os.environ",
            {
                "OPENAI_API_KEY": "offline",
                "ACUITY_PRODUCT_HANDOFF_URL": " http://127.0.0.1:8000/v1/handoffs/ ",
                "ABITA_EYE_GROUP_PRODUCT_PRACTICE_ID": "00000000-0000-4000-8000-000000000001",
                "ABITA_EYE_GROUP_PRODUCT_SERVICE_SECRET": "offline",
            },
            clear=True,
        ):
            config = load_config()
            self.assertEqual(config.staff_tasks_url, "http://127.0.0.1:8000/v1/tasks")
            async with httpx.AsyncClient(
                transport=httpx.MockTransport(handler)
            ) as client:
                control = CallControl(
                    call_state(), Product(client, config), backend=BackendResponses()
                )
                with patch.dict(
                    "os.environ",
                    {"ACUITY_PRODUCT_HANDOFF_URL": "https://wrong.example/v1/handoffs"},
                ):
                    target = await control.admission.resolve()
        self.assertTrue(target.admitted)
        self.assertEqual(str(requests[0].url), "http://127.0.0.1:8000/v1/handoffs")
        self.assertEqual(requests[0].headers["Authorization"], "Bearer offline")

    async def test_optional_practice_does_not_block_staff_tasks_or_direct_office(self):
        for practice in ("", "invalid"):
            with patch.dict(
                "os.environ",
                {
                    "OPENAI_API_KEY": "offline",
                    "ACUITY_PRODUCT_HANDOFF_URL": "https://product.example/v1/handoffs/",
                    "ABITA_EYE_GROUP_PRODUCT_SERVICE_SECRET": "offline",
                    "ABITA_EYE_GROUP_PRODUCT_PRACTICE_ID": practice,
                },
                clear=True,
            ):
                config = load_config()
            self.assertEqual(config.staff_tasks_url, "https://product.example/v1/tasks")
            self.assertIsNone(config.handoff)
            async with httpx.AsyncClient(
                transport=httpx.MockTransport(
                    lambda request: self.fail("No admission request expected")
                )
            ) as client:
                state = call_state()
                control = CallControl(
                    state, Product(client, config), backend=BackendResponses()
                )
                with self.assertRaises(AdmissionRejected):
                    await control.admission.resolve()
                state.call = replace(state.call, called_office_key="crystal-river")
                target = await control.admission.resolve()
                self.assertEqual(target.destination, "tel:+13527941244")
                self.assertFalse(target.admitted)

    def test_product_handoff_url_validation(self):
        env = {
            "OPENAI_API_KEY": "offline",
            "ABITA_EYE_GROUP_PRODUCT_SERVICE_SECRET": "offline",
            "ABITA_EYE_GROUP_PRODUCT_PRACTICE_ID": "00000000-0000-4000-8000-000000000001",
        }
        for base in (
            "http://localhost:8000",
            "http://127.0.0.1:8000",
            "http://[::1]:8000",
            "https://product.example",
        ):
            with patch.dict(
                "os.environ",
                env | {"ACUITY_PRODUCT_HANDOFF_URL": base + "/v1/handoffs/"},
                clear=True,
            ):
                config = load_config()
            self.assertEqual(config.handoff.url, base + "/v1/handoffs")
        for url in (
            "http://external.example/v1/handoffs",
            "https://user:password@product.example/v1/handoffs",
            "https://product.example/v1/handoffs?x=1",
            "https://product.example/v1/handoffs#fragment",
        ):
            with (
                patch.dict(
                    "os.environ", env | {"ACUITY_PRODUCT_HANDOFF_URL": url}, clear=True
                ),
                self.assertRaises(ValueError),
            ):
                load_config()
