"""Exercise migration boundaries with real composition and offline HTTP."""

import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

from insurance_fixtures import decision
from abita_s2s.insurance_contract import InsuranceDecision
from unittest.mock import AsyncMock
import httpx
from test_patient_resolution import CONFIG, call_state, receipt, search
from test_scheduling import NOW, inventory

from abita_s2s.tools.call_control import CallControl
from abita_s2s.config import load_config
from abita_s2s.handoff import AdmissionRejected
from abita_s2s.identity import PatientResolver
from abita_s2s.insurance import InsuranceRegistration
from abita_s2s.insurance_state import insurance_ready
from abita_s2s.integrations.patient_middleware import PatientMiddleware
from abita_s2s.scheduling import Scheduling
from abita_s2s.integrations.scheduling_http import SchedulingHTTP


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
                control = CallControl(call_state(), client, handoff=config.handoff)
                # Admission must retain startup's normalized settings even if env changes.
                with patch.dict(
                    "os.environ",
                    {"ACUITY_PRODUCT_HANDOFF_URL": "https://wrong.example/v1/handoffs"},
                ):
                    target = await control.admission.resolve()
        self.assertTrue(target.admitted)
        self.assertEqual(str(requests[0].url), "http://127.0.0.1:8000/v1/handoffs")
        self.assertEqual(requests[0].headers["Authorization"], "Bearer offline")

    async def resolved_owner(self, responses, **fields):
        state = call_state(None)
        responses = [
            receipt(**fields),
            *responses,
        ]
        requests = []

        def handler(request):
            requests.append(request)
            return httpx.Response(200, json=responses.pop(0))

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        self.addAsyncCleanup(client.aclose)
        resolver = PatientResolver(state, PatientMiddleware(client, CONFIG))
        scheduling = Scheduling(state, SchedulingHTTP(client, CONFIG), now=lambda: NOW)
        self.addAsyncCleanup(resolver.aclose)
        self.addAsyncCleanup(scheduling.aclose)
        self.assertEqual(
            (await resolver.resolve("Jane", "01/02/1980"))["outcome"], "verified"
        )
        self.assertIsNone(state.insurance.accepted)
        return scheduling, resolver, requests

    async def test_previous_patients_check_does_not_block_returning_patient(self):
        for plan in ("Self Pay", "Unknown corrected plan"):
            with self.subTest(plan=plan):
                owner, resolver, requests = await self.resolved_owner(
                    [
                        receipt(
                            "chart-john",
                            "John",
                            "03/04/1981",
                        ),
                        inventory(),
                        receipt(),
                    ]
                )
                insurance = InsuranceRegistration(
                    owner.state, resolver, AsyncMock(check=AsyncMock(return_value=None))
                )
                await insurance.check(plan, "medical")
                await resolver.resolve("John", "03/04/1981")
                self.assertEqual(owner.state.patient.active.patientId, "chart-john")
                self.assertEqual(
                    (await owner.availability("medical"))["outcome"], "found"
                )
                self.assertEqual(len(requests), 3)
                # Participation questions do not replace chart insurance for scheduling.
                await resolver.resolve("Jane", "01/02/1980")
                self.assertEqual(owner.state.patient.active.patientId, "chart-jane")
                self.assertTrue(insurance_ready(owner.state))

    async def test_absent_patients_check_does_not_block_returning_patient(self):
        state = call_state(None)
        responses = [
            search(),
            receipt(),
            inventory(),
        ]
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json=responses.pop(0))
            )
        ) as client:
            resolver = PatientResolver(state, PatientMiddleware(client, CONFIG))
            owner = Scheduling(state, SchedulingHTTP(client, CONFIG), now=lambda: NOW)
            self.addAsyncCleanup(resolver.aclose)
            self.addAsyncCleanup(owner.aclose)
            self.assertEqual(
                (await resolver.resolve("John", "03/04/1981"))["outcome"], "not_found"
            )
            insurance = InsuranceRegistration(
                state,
                resolver,
                AsyncMock(
                    check=AsyncMock(
                        return_value=InsuranceDecision.model_validate(
                            decision("Self Pay")
                        )
                    )
                ),
            )
            await insurance.check("Self Pay", "medical")
            self.assertIsNone(state.insurance.accepted.patient_id)
            self.assertEqual(
                (await resolver.resolve("Jane", "01/02/1980"))["outcome"], "verified"
            )
            self.assertEqual((await owner.availability("medical"))["outcome"], "found")
            self.assertEqual(responses, [])

    async def test_optional_practice_does_not_block_staff_tasks_or_direct_office(self):
        for practice in ("", "invalid"):
            with patch.dict(
                "os.environ",
                {
                    "OPENAI_API_KEY": "offline",
                    "ACUITY_PRODUCT_HANDOFF_URL": "https://product.example/v1/handoffs/",
                    "ABITA_EYE_GROUP_PRODUCT_SERVICE_SECRET": "offline",
                    "ABITA_EYE_GROUP_PRODUCT_PRACTICE_ID": practice,
                    "ACUITY_HANDOFF_URL": "https://legacy.example/admit",
                    "ACUITY_HANDOFF_SECRET": "legacy",
                },
                clear=True,
            ):
                config = load_config()
            self.assertEqual(config.staff_tasks_url, "https://product.example/v1/tasks")
            self.assertIsNone(
                config.handoff
            )  # Do not fall back across admission contracts.
            async with httpx.AsyncClient(
                transport=httpx.MockTransport(
                    lambda request: self.fail("No admission request expected")
                )
            ) as client:
                state = call_state()
                control = CallControl(state, client, handoff=config.handoff)
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
