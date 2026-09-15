"""Offline Product lifecycle contracts and LiveKit's session-end boundary."""

import asyncio
import json
import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import httpx
from livekit.agents import AgentSession, llm
from livekit.agents.voice.events import CloseEvent, CloseReason
from livekit.agents.voice.report import SessionReport
from test_patient_resolution import CONFIG, call_state

from abita_s2s.config import load_config
from abita_s2s.reporting import CallReporter, ReportingError
from abita_s2s.staff_tasks import StaffTasks
from abita_s2s.identity import PatientResolver
from abita_s2s.middleware import PatientMiddleware
from test_patient_resolution import candidate, receipt, search
from abita_s2s.runtime.session_startup import finish_voice_call

ACK = {"status": "created", "interactionId": "d3665980-68ce-4336-87af-e2ba40ad2e8e"}
REPORT = {
    "job_id": "job-test",
    "room": "room-test",
    "chat_history": {"items": []},
    "events": [{"type": "close", "reason": "participant_disconnected", "error": None}],
    "usage": [{"model": "gpt-live", "input_audio_tokens": 24}],
}
OUTCOME = {
    "action": "BOOKED",
    "externalPatientId": "original-patient",
    "newAppointmentId": "42",
    "bookingResult": {"status": "booked", "appointmentId": 42},
}


class ReportingTests(unittest.IsolatedAsyncioTestCase):
    def reporter(self, handler=None, drain=None):
        self.requests = []

        async def receive(request):
            self.requests.append(json.loads(request.content))
            self.assertEqual(request.headers["Authorization"], "Bearer offline-secret")
            if handler:
                return await handler(request)
            return httpx.Response(201, json=ACK)

        client = httpx.AsyncClient(transport=httpx.MockTransport(receive))
        self.addAsyncCleanup(client.aclose)
        return CallReporter(
            call_state().call,
            client,
            replace(
                CONFIG,
                interaction_url="https://product.test/v1/ai/interactions",
                product_secret="offline-secret",
            ),
            drain or AsyncMock(),
        )

    async def test_native_report_and_ordered_private_outcomes_survive_cancellation(
        self,
    ):
        released = asyncio.Event()

        async def drain():
            await released.wait()
            reporter.record(
                "staff_task", {"outcome": "created", "taskId": "private-task"}
            )
            reporter.appointment(OUTCOME)

        reporter = self.reporter(drain=drain)
        reporter.started = True
        waiter = asyncio.create_task(reporter.finish(lambda: REPORT))
        await asyncio.sleep(0)
        waiter.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await waiter
        released.set()
        await reporter.finish()
        await reporter.finish()
        self.assertEqual(
            [p["kind"] for p in self.requests],
            ["START", "OUTCOME_CHECKPOINT", "CLOSEOUT"],
        )
        closeout = self.requests[-1]
        self.assertEqual(closeout["status"], "COMPLETED")
        self.assertEqual(closeout["transcript"], REPORT)
        self.assertEqual(
            closeout["appointmentOutcome"]["externalPatientId"], "original-patient"
        )
        self.assertEqual(
            closeout["closeoutPayload"]["domainOutcomes"][0]["evidence"]["taskId"],
            "private-task",
        )
        self.assertNotIn("closeoutPayload", self.requests[0])
        self.assertNotIn("transcript", self.requests[1])

    async def test_actual_sdk_report_serializes_without_a_custom_transcript(self):
        history = llm.ChatContext()
        history.add_message(role="user", content="Thank you")
        native = SessionReport(
            job_id="job",
            room_id="room-id",
            room="room",
            options=AgentSession().options,
            events=[CloseEvent(reason=CloseReason.PARTICIPANT_DISCONNECTED)],
            chat_history=history,
        )
        reporter = self.reporter()
        reporter.started = True
        await reporter.finish(native.to_dict)
        self.assertEqual(self.requests[-1]["transcript"], native.to_dict())
        self.assertEqual(self.requests[-1]["status"], "COMPLETED")

    async def test_payload_is_snapshotted_before_async_delivery(self):
        reporter = self.reporter()
        outcome = {**OUTCOME, "bookingResult": {"status": "booked"}}
        reporter.appointment(outcome)
        outcome["bookingResult"]["status"] = "changed"
        await reporter.finish()
        self.assertEqual(
            self.requests[1]["appointmentOutcome"]["bookingResult"]["status"], "booked"
        )

    async def test_failed_start_does_not_invent_a_transcript(self):
        reporter = self.reporter()
        await reporter.finish()
        self.assertEqual(self.requests[-1]["status"], "FAILED")
        self.assertNotIn("transcript", self.requests[-1])

    async def test_report_without_native_close_does_not_claim_completion(self):
        reporter = self.reporter()
        reporter.started = True
        await reporter.finish(lambda: {**REPORT, "events": []})
        self.assertEqual(self.requests[-1]["status"], "FAILED")

    async def test_native_hook_uses_primary_session_and_report(self):
        reporter = self.reporter()
        reporter.started = True
        state = call_state()
        state.reporter = reporter
        ctx = SimpleNamespace(
            primary_session=SimpleNamespace(userdata=state),
            make_session_report=Mock(
                return_value=SimpleNamespace(to_dict=lambda: REPORT)
            ),
        )
        await finish_voice_call(ctx)
        ctx.make_session_report.assert_called_once()
        self.assertEqual(self.requests[-1]["transcript"], REPORT)

    async def test_drain_error_still_reports_failure(self):
        reporter = self.reporter(
            drain=AsyncMock(side_effect=RuntimeError("private details"))
        )
        reporter.started = True
        with self.assertLogs("abita_s2s.reporting", "ERROR") as logs:
            await reporter.finish(lambda: REPORT)
        self.assertNotIn("private details", str(logs.output))
        self.assertEqual(self.requests[-1]["status"], "FAILED")
        self.assertTrue(self.requests[-1]["closeoutPayload"]["mutationDrainFailed"])

    async def test_staff_delivery_exception_or_cancellation_fails_closeout(self):
        for error in (RuntimeError("delivery failed"), asyncio.CancelledError()):
            with self.subTest(error=type(error).__name__):
                owner = StaffTasks(call_state(), Mock(), Mock(), CONFIG)
                delivery = asyncio.create_task(AsyncMock(side_effect=error)())
                owner._deliveries["accepted"] = delivery
                await asyncio.gather(delivery, return_exceptions=True)
                reporter = self.reporter(drain=owner.aclose)
                reporter.started = True
                await reporter.finish(lambda: REPORT)
                self.assertEqual(self.requests[-1]["status"], "FAILED")
                self.assertTrue(self.requests[-1]["closeoutPayload"]["mutationDrainFailed"])

    async def test_appointment_domain_outcomes_use_product_names(self):
        reporter = self.reporter()
        reporter.appointment(OUTCOME)
        await reporter.finish(lambda: REPORT)
        self.assertEqual(self.requests[-1]["closeoutPayload"]["domainOutcomes"][0]["outcome"], "booked")

    async def test_verified_and_switched_patients_reach_product_classification(self):
        reporter = self.reporter()
        state = call_state(None)
        state.reporter = reporter
        responses = [
            search(candidate()), receipt(),
            search(candidate("chart-john", "John", "03/04/1981")),
            receipt("chart-john", "John", "03/04/1981"),
        ]
        async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json=responses.pop(0))
        )) as client:
            resolver = PatientResolver(state, PatientMiddleware(client, CONFIG))
            self.addAsyncCleanup(resolver.aclose)
            await resolver.resolve("Jane", "01/02/1980")
            await resolver.resolve("John", "03/04/1981")
        await reporter.finish(lambda: REPORT)
        facts = self.requests[-1]["closeoutPayload"]["domainOutcomes"]
        self.assertEqual([f["outcome"] for f in facts], ["patient_verified", "patient_switched"])
        self.assertTrue(all(f["status"] == "success" for f in facts))
        self.assertEqual(facts[-1]["evidence"]["externalPatientId"], "chart-john")

    async def test_error_close_is_failed_and_accepted_transfer_is_escalated(self):
        for reason, transfer, expected in [
            ("error", "accepted", "FAILED"),
            ("participant_disconnected", "accepted", "ESCALATED"),
            ("participant_disconnected", "ambiguous", "COMPLETED"),
        ]:
            reporter = self.reporter()
            reporter.started = True
            reporter.transfer_status = transfer
            await reporter.finish(
                lambda reason=reason: {
                    **REPORT,
                    "events": [{"type": "close", "reason": reason}],
                }
            )
            self.assertEqual(self.requests[-1]["status"], expected)
            self.assertEqual(
                self.requests[-1]["closeoutPayload"]["transferStatus"], transfer
            )

    async def test_transient_delivery_retries_identical_envelope(self):
        async def handler(request):
            if len(self.requests) == 1:
                raise httpx.ReadTimeout("secret URL", request=request)
            return httpx.Response(200, json=ACK)

        reporter = self.reporter(handler)
        await reporter.finish()
        self.assertEqual(self.requests[0], self.requests[1])
        self.assertEqual(len(self.requests), 3)

    async def test_rejected_closeout_is_visible_and_not_retried(self):
        async def handler(request):
            return httpx.Response(401, text="private data")

        reporter = self.reporter(handler)
        with (
            self.assertLogs("abita_s2s.reporting", "ERROR") as logs,
            self.assertRaises(ReportingError),
        ):
            await reporter.finish()
        self.assertNotIn("private data", str(logs.output))
        self.assertEqual(len(self.requests), 2)

    async def test_invalid_acknowledgement_is_not_success(self):
        async def handler(request):
            return httpx.Response(200, json={"status": "created", "interactionId": 123})

        reporter = self.reporter(handler)
        with (
            self.assertLogs("abita_s2s.reporting", "ERROR"),
            self.assertRaises(ReportingError),
        ):
            await reporter.finish()
        self.assertEqual(len(self.requests), 4)


class ReportingConfigTests(unittest.TestCase):
    def test_reporting_requires_valid_endpoint_and_credential(self):
        for url in (
            "http://product.test/v1/ai/interactions",
            "https://user:password@product.test/v1/ai/interactions",
            "https://product.test/other",
            "https://product.test/v1/ai/interactions?q=1",
        ):
            with (
                patch.dict(
                    "os.environ",
                    {
                        "OPENAI_API_KEY": "offline",
                        "ACUITY_PRODUCT_INTERACTION_URL": url,
                    },
                    clear=True,
                ),
                self.assertRaises(ValueError),
            ):
                load_config()
        with (
            patch.dict(
                "os.environ",
                {
                    "OPENAI_API_KEY": "offline",
                    "ACUITY_PRODUCT_INTERACTION_URL": "https://product.test/v1/ai/interactions",
                },
                clear=True,
            ),
            self.assertRaisesRegex(ValueError, "SECRET"),
        ):
            load_config()

    def test_named_deployments_do_not_ingest_simulations(self):
        with patch.dict(
            "os.environ",
            {
                "OPENAI_API_KEY": "offline",
                "ACUITY_PRODUCT_INTERACTION_URL": "https://production.test/v1/ai/interactions",
                "LIVEKIT_AGENT_DEPLOYMENT": "staging",
            },
            clear=True,
        ):
            self.assertIsNone(load_config().interaction_url)
