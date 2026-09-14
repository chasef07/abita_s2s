import asyncio
import json
import unittest

import httpx

from abita_s2s.middleware import (
    HydratedPatient,
    MiddlewareClient,
    MiddlewareFailure,
    PatientMatches,
    PatientNotFound,
)


def patient(**overrides):
    return {
        "status": "verified",
        "patientId": "123",
        "name": "TEST,PATIENT",
        "dob": "01/01/1980",
        "insuranceCarrier": "Test plan",
        "insPlanId": "456",
        "appointmentsStatus": "found",
        "appointments": [
            {
                "id": 12,
                "date": "Friday",
                "time": "10:00 AM",
                "officeId": "hollywood",
                "cancellationToken": "private-token",
                "rescheduleToken": "other-private-token",
            }
        ],
        **overrides,
    }


class MiddlewareTests(unittest.IsolatedAsyncioTestCase):
    async def resolve(self, handler, **options):
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            return await MiddlewareClient(
                http,
                base_url="https://middleware.test",
                auth_token="test-token",
                **options,
            ).resolve_patient(office="hollywood", phone="+15555550123")

    async def test_exact_request_and_hydrated_evidence(self):
        def handler(request):
            self.assertEqual(request.url.path, "/api/patient/resolve")
            self.assertEqual(request.method, "POST")
            self.assertEqual(request.headers["Authorization"], "test-token")
            self.assertTrue(request.headers["X-Request-ID"])
            self.assertEqual(
                json.loads(request.content),
                {"office": "hollywood", "phone": "+15555550123"},
            )
            return httpx.Response(200, json=patient())

        result = await self.resolve(handler)
        self.assertIsInstance(result, HydratedPatient)
        self.assertIsNone(result.phone)
        self.assertEqual(result.ins_plan_id, "456")
        self.assertEqual(result.appointments[0].office_id, "hollywood")
        self.assertEqual(result.appointments[0].cancellation_token, "private-token")
        self.assertNotIn("TEST", repr(result))
        self.assertNotIn("private-token", str(result.appointments[0]))

    async def test_lightweight_matches_remain_distinct(self):
        result = await self.resolve(
            lambda r: httpx.Response(
                200,
                json={
                    "status": "multiple_matches",
                    "appointments": [],
                    "matches": [
                        {
                            "status": "candidate",
                            "patientId": "123",
                            "firstName": "Test",
                            "lastName": "Patient",
                            "dob": "01/01/1980",
                        }
                    ],
                },
            )
        )
        self.assertIsInstance(result, PatientMatches)
        self.assertEqual(result.matches[0].patient_id, "123")
        self.assertFalse(hasattr(result.matches[0], "appointments"))

    async def test_none_and_appointment_failure_are_not_lookup_failure(self):
        result = await self.resolve(
            lambda r: httpx.Response(
                200, json={"status": "not_found", "appointments": []}
            )
        )
        self.assertIsInstance(result, PatientNotFound)
        result = await self.resolve(
            lambda r: httpx.Response(
                200,
                json=patient(
                    appointmentsStatus="error",
                    appointments=[],
                    appointmentsMessage="Unavailable",
                ),
            )
        )
        self.assertIsInstance(result, HydratedPatient)
        self.assertEqual(result.appointments_status, "error")

    async def test_invalid_responses_are_not_retried(self):
        missing_status = patient()
        del missing_status["appointmentsStatus"]
        for body in [
            missing_status,
            patient(appointmentsStatus="none"),
            patient(patientId=123),
            patient(appointments=None),
            {"status": "multiple_matches", "matches": []},
            {"status": "not_found", "matches": [1]},
            {"status": "no_match"},
            [],
            "not json",
        ]:
            with self.subTest(body=body):
                count = 0

                def handler(request):
                    nonlocal count
                    count += 1
                    return httpx.Response(
                        200, content=body if isinstance(body, str) else json.dumps(body)
                    )

                result = await self.resolve(handler)
                self.assertEqual(result, MiddlewareFailure("invalid_response"))
                self.assertEqual(count, 1)

    async def test_only_transient_http_errors_retry_once(self):
        for status in (302, 400, 401, 403, 408, 429, 500, 503):
            with self.subTest(status=status):
                count = 0

                def handler(request):
                    nonlocal count
                    count += 1
                    return httpx.Response(
                        status, headers={"Location": "https://elsewhere.test"}
                    )

                result = await self.resolve(handler)
                self.assertIsInstance(result, MiddlewareFailure)
                self.assertEqual(count, 2 if status in (408, 429, 500, 503) else 1)

    async def test_network_retry_can_recover(self):
        count = 0

        def handler(request):
            nonlocal count
            count += 1
            if count == 1:
                raise httpx.ConnectError("secret body", request=request)
            return httpx.Response(200, json={"status": "not_found"})

        self.assertIsInstance(await self.resolve(handler), PatientNotFound)
        self.assertEqual(count, 2)

    async def test_total_deadline_and_cancellation(self):
        count = 0

        async def slow(request):
            nonlocal count
            count += 1
            await asyncio.sleep(10)

        self.assertEqual(
            await self.resolve(slow, timeout_seconds=0.01),
            MiddlewareFailure("network_error"),
        )
        self.assertEqual(count, 2)
        count = 0

        async def cancel(request):
            nonlocal count
            count += 1
            raise asyncio.CancelledError()

        with self.assertRaises(asyncio.CancelledError):
            await self.resolve(cancel)
        self.assertEqual(count, 1)

    async def test_safe_diagnostics_and_application_errors(self):
        count = 0

        def handler(request):
            nonlocal count
            count += 1
            return httpx.Response(
                200, json={"status": "error", "message": "private PHI"}
            )

        with self.assertLogs("abita_s2s.middleware", level="INFO") as logs:
            result = await self.resolve(handler)
        self.assertEqual(result, MiddlewareFailure("middleware_error"))
        self.assertEqual(count, 2)
        for record in logs.records:
            logged = str(record.__dict__)
            for private in ("private PHI", "test-token", "+15555550123"):
                self.assertNotIn(private, logged)
            self.assertEqual(record.outcome, "middleware_error")


if __name__ == "__main__":
    unittest.main()
