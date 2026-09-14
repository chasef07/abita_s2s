"""Registered tools execute in a real AgentSession; all external effects are offline."""

import asyncio
import json
import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import httpx
from livekit import api, rtc
from livekit.agents import AgentSession, llm
from test_patient_resolution import call_state

from abita_s2s.agent import AbitaAgent
from abita_s2s.call_control import CallControl
from abita_s2s.offices import SPRING_HILL


class Model(llm.LLM):
    def __init__(self):
        super().__init__()
        self.outputs = []

    def chat(self, *, chat_ctx, tools=None, conn_options, **kwargs):
        return Stream(
            self, chat_ctx=chat_ctx, tools=tools or [], conn_options=conn_options
        )


class Stream(llm.LLMStream):
    async def _run(self):
        items = self._chat_ctx.items
        user = max(
            i for i, x in enumerate(items) if x.type == "message" and x.role == "user"
        )
        outputs = [x for x in items[user:] if x.type == "function_call_output"]
        if outputs:
            self._llm.outputs.append(outputs[-1].output)
            delta = llm.ChoiceDelta(role="assistant", content="Goodbye.")
        else:
            delta = llm.ChoiceDelta(
                role="assistant",
                tool_calls=[
                    llm.FunctionToolCall(
                        name="transfer_call"
                        if items[user].text_content == "duplicate"
                        else items[user].text_content,
                        arguments="{}",
                        call_id=f"tool-{user}-{index}",
                    )
                    for index in range(
                        2 if items[user].text_content == "duplicate" else 1
                    )
                ],
            )
        self._event_ch.send_nowait(llm.ChatChunk(id="offline", delta=delta))


class CallControlTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.state = call_state(None)
        self.state.call = replace(
            self.state.call,
            called_office_key="crystal-river",
            room_name="room",
            sip_participant_identity="caller",
            called_number="+13523202007",
        )
        self.room = SimpleNamespace(
            name="room",
            isconnected=lambda: True,
            remote_participants={
                "caller": SimpleNamespace(
                    kind=rtc.ParticipantKind.PARTICIPANT_KIND_SIP
                ),
                "other": SimpleNamespace(kind=rtc.ParticipantKind.PARTICIPANT_KIND_SIP),
            },
        )
        self.events = []

        async def transfer(request, **kwargs):
            self.events.append("refer")
            return api.TransferSIPParticipantResponse(
                status=api.STS_TRANSFER_SUCCESSFUL
            )

        self.sip = SimpleNamespace(
            transfer_sip_participant=AsyncMock(side_effect=transfer)
        )
        self.client = httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: httpx.Response(500))
        )
        self.addAsyncCleanup(self.client.aclose)
        self.control = CallControl(self.state, self.client, self.room, self.sip)
        self.model = Model()
        self.session = AgentSession(llm=self.model, userdata=self.state)
        self.addAsyncCleanup(self.session.aclose)
        with patch.object(AbitaAgent, "on_enter", new=AsyncMock()):
            await self.session.start(
                agent=AbitaAgent(SPRING_HILL, None, call_control=self.control)
            )

    async def run_tool(self, name):
        async def playout():
            self.events.append("announcement_done")

        speech = SimpleNamespace(
            wait_for_playout=playout, interrupted=False, exception=lambda: None
        )
        # Replace speech only; tool selection, RunContext and execution are LiveKit's.
        original = self.session.generate_reply

        def generate(**kwargs):
            return speech if "instructions" in kwargs else original(**kwargs)

        with patch.object(self.session, "generate_reply", side_effect=generate):
            await asyncio.wait_for(self.session.run(user_input=name), 4)
        return json.loads(self.model.outputs[-1])

    async def test_acceptance_announcement_exact_participant_duplicate_and_end_guard(
        self,
    ):
        self.assertEqual((await self.run_tool("transfer_call"))["outcome"], "accepted")
        self.assertEqual(self.events, ["announcement_done", "refer"])
        req = self.sip.transfer_sip_participant.call_args.args[0]
        self.assertEqual(
            (req.room_name, req.participant_identity, req.transfer_to),
            ("room", "caller", "tel:+13527941244"),
        )
        self.state.patient.revision += 1
        self.assertEqual((await self.run_tool("transfer_call"))["outcome"], "accepted")
        self.assertEqual((await self.run_tool("end_call"))["outcome"], "blocked")
        self.sip.transfer_sip_participant.assert_awaited_once()

    async def test_transport_uncertainty_suppresses_retry_and_hangup(self):
        self.sip.transfer_sip_participant.side_effect = TimeoutError()
        self.assertEqual((await self.run_tool("transfer_call"))["outcome"], "ambiguous")
        await self.run_tool("transfer_call")
        self.assertEqual((await self.run_tool("end_call"))["outcome"], "blocked")
        self.sip.transfer_sip_participant.assert_awaited_once()

    async def test_structured_failure_and_ongoing_are_not_success(self):
        self.sip.transfer_sip_participant.side_effect = None
        self.sip.transfer_sip_participant.return_value = (
            api.TransferSIPParticipantResponse(status=api.STS_TRANSFER_FAILED)
        )
        self.assertEqual((await self.run_tool("transfer_call"))["outcome"], "failed")
        self.assertEqual((await self.run_tool("transfer_call"))["outcome"], "blocked")
        self.control.attempts = 0
        self.sip.transfer_sip_participant.return_value = (
            api.TransferSIPParticipantResponse(status=api.STS_TRANSFER_ONGOING)
        )
        self.assertEqual((await self.run_tool("transfer_call"))["outcome"], "ambiguous")

    async def test_missing_caller_and_console_never_select_other_participant(self):
        del self.room.remote_participants["caller"]
        self.assertEqual(
            (await self.run_tool("transfer_call"))["outcome"], "unavailable"
        )
        self.assertEqual((await self.run_tool("end_call"))["outcome"], "unavailable")
        self.control.room = None
        self.assertEqual(
            (await self.run_tool("transfer_call"))["outcome"], "unavailable"
        )
        self.sip.transfer_sip_participant.assert_not_awaited()
        self.assertEqual(self.events, [])

    async def test_disconnect_during_announcement_prevents_refer(self):
        async def playout():
            self.room.remote_participants.clear()

        speech = SimpleNamespace(
            wait_for_playout=playout, interrupted=False, exception=lambda: None
        )
        original = self.session.generate_reply

        def generate(**kwargs):
            return speech if "instructions" in kwargs else original(**kwargs)

        with patch.object(self.session, "generate_reply", side_effect=generate):
            await asyncio.wait_for(self.session.run(user_input="transfer_call"), 4)
        self.assertEqual(json.loads(self.model.outputs[-1])["outcome"], "failed")
        self.sip.transfer_sip_participant.assert_not_awaited()

    async def test_goodbye_finishes_before_builtin_shutdown(self):
        job = SimpleNamespace(shutdown=Mock(), add_shutdown_callback=Mock())
        closed = asyncio.Event()
        self.session.on("close", lambda event: closed.set())
        with patch(
            "livekit.agents.beta.tools.end_call.get_job_context", return_value=job
        ):
            await asyncio.wait_for(self.session.run(user_input="end_call"), 4)
            await asyncio.wait_for(closed.wait(), 4)
        self.assertEqual(self.model.outputs[-1], "Say a brief goodbye to the caller.")
        self.assertIn(
            "Goodbye.",
            [x.text_content for x in self.session.history.items if x.type == "message"],
        )
        job.shutdown.assert_called_once()
        job.add_shutdown_callback.assert_not_called()

    async def test_cancel_during_refer_is_ambiguous(self):
        started = asyncio.Event()

        async def refer(*args, **kwargs):
            started.set()
            raise asyncio.CancelledError()

        self.sip.transfer_sip_participant.side_effect = refer
        # Real executor sees a canceled tool; the owner's state still fences retries.
        try:
            await self.run_tool("transfer_call")
        except (TimeoutError, IndexError, asyncio.CancelledError):
            pass
        self.assertTrue(started.is_set())
        self.assertEqual(self.control.status, "ambiguous")

    async def test_product_partial_write_conflict_and_stable_identity(self):
        self.state.call = replace(self.state.call, called_office_key="spring-hill")
        requests = []

        def handler(request):
            requests.append(json.loads(request.content))
            return httpx.Response(409)

        self.control.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        self.addAsyncCleanup(self.control.client.aclose)
        with patch.dict(
            "os.environ",
            {
                "ACUITY_PRODUCT_HANDOFF_URL": "https://product.example/v1/handoffs",
                "ABITA_EYE_GROUP_PRODUCT_PRACTICE_ID": "00000000-0000-4000-8000-000000000001",
                "ABITA_EYE_GROUP_PRODUCT_SERVICE_SECRET": "offline",
            },
        ):
            self.assertEqual(
                (await self.run_tool("transfer_call"))["outcome"], "ambiguous"
            )
            self.state.patient.revision += 1
            await self.run_tool("transfer_call")
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0]["officeKey"], "spring-hill")
        self.sip.transfer_sip_participant.assert_not_awaited()

    async def test_product_success_and_bounded_explicit_retry(self):
        self.state.call = replace(self.state.call, called_office_key="spring-hill")
        requests = []

        def handler(request):
            requests.append(json.loads(request.content))
            if len(requests) == 1:
                return httpx.Response(429)
            return httpx.Response(
                201,
                json={
                    "id": "00000000-0000-4000-8000-000000000002",
                    "expiresAt": (datetime.now(UTC) + timedelta(minutes=2)).isoformat(),
                    "sipDestination": "sip:acuity-handoff@product.example",
                },
            )

        self.control.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        self.addAsyncCleanup(self.control.client.aclose)
        with patch.dict(
            "os.environ",
            {
                "ACUITY_PRODUCT_HANDOFF_URL": "https://product.example/v1/handoffs",
                "ABITA_EYE_GROUP_PRODUCT_PRACTICE_ID": "00000000-0000-4000-8000-000000000001",
                "ABITA_EYE_GROUP_PRODUCT_SERVICE_SECRET": "offline",
            },
        ):
            first = await self.run_tool("transfer_call")
            self.assertEqual(first["outcome"], "failed")
            self.assertIn("once more", first["answer"])
            self.state.patient.revision += 1
            self.assertEqual(
                (await self.run_tool("transfer_call"))["outcome"], "accepted"
            )
        self.assertEqual(requests[0], requests[1])
        self.assertEqual(
            self.sip.transfer_sip_participant.call_args.args[0].transfer_to,
            "sip:acuity-handoff@product.example",
        )

    async def test_partial_admission_and_invalid_destination_block_refer(self):
        self.state.call = replace(self.state.call, called_office_key="spring-hill")
        for response in (
            httpx.Response(503),
            httpx.Response(
                201,
                json={
                    "expiresAt": (datetime.now(UTC) + timedelta(minutes=2)).isoformat(),
                    "id": "bad",
                    "sipDestination": "tel:+15555555555",
                },
            ),
        ):
            self.control.status = "idle"
            self.control.attempts = 0
            self.control.client = httpx.AsyncClient(
                transport=httpx.MockTransport(lambda request, reply=response: reply)
            )
            self.addAsyncCleanup(self.control.client.aclose)
            with patch.dict(
                "os.environ",
                {
                    "ACUITY_PRODUCT_HANDOFF_URL": "https://product.example/v1/handoffs",
                    "ABITA_EYE_GROUP_PRODUCT_PRACTICE_ID": "00000000-0000-4000-8000-000000000001",
                    "ABITA_EYE_GROUP_PRODUCT_SERVICE_SECRET": "offline",
                },
            ):
                self.assertEqual(
                    (await self.run_tool("transfer_call"))["outcome"], "ambiguous"
                )
            self.sip.transfer_sip_participant.assert_not_awaited()

    async def test_sandbox_blocks_before_speech(self):
        with patch.dict("os.environ", {"LIVEKIT_AGENT_DEPLOYMENT": "preview"}):
            self.assertEqual(
                (await self.run_tool("transfer_call"))["outcome"], "blocked"
            )
        self.assertEqual(self.events, [])

    async def test_pending_duplicate_does_not_send_second_refer(self):
        self.control.status = "pending"
        self.assertEqual((await self.run_tool("transfer_call"))["outcome"], "pending")
        self.assertEqual((await self.run_tool("end_call"))["outcome"], "blocked")
        self.sip.transfer_sip_participant.assert_not_awaited()

    async def test_session_close_cancels_pending_patient_read(self):
        from test_patient_resolution import CONFIG, candidate, search

        from abita_s2s.identity import PatientResolver
        from abita_s2s.middleware import PatientMiddleware

        started = asyncio.Event()
        cancelled = asyncio.Event()

        async def handler(request):
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                return httpx.Response(200, json=search(candidate()))

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            resolver = PatientResolver(self.state, PatientMiddleware(client, CONFIG))
            self.addAsyncCleanup(resolver.aclose)
            self.session.current_agent._resolver = resolver
            task = asyncio.create_task(resolver.resolve("Jane", "01/02/1980"))
            await asyncio.wait_for(started.wait(), 2)
            await self.session.aclose()
            await asyncio.wait_for(cancelled.wait(), 2)
            await asyncio.gather(task, return_exceptions=True)
            self.assertIsNone(self.state.patient.active)
            self.assertEqual(
                (await resolver.resolve("Jane", None))["outcome"], "superseded"
            )

    async def test_announcement_failure_never_sends_refer_and_retry_is_bounded(self):
        speech = SimpleNamespace(
            wait_for_playout=AsyncMock(),
            interrupted=False,
            exception=lambda: RuntimeError("speech failed"),
        )
        original = self.session.generate_reply

        def generate(**kwargs):
            return speech if "instructions" in kwargs else original(**kwargs)

        with patch.object(self.session, "generate_reply", side_effect=generate):
            for _ in range(3):
                await asyncio.wait_for(self.session.run(user_input="transfer_call"), 4)
        self.assertEqual(json.loads(self.model.outputs[-1])["outcome"], "blocked")
        self.sip.transfer_sip_participant.assert_not_awaited()
        self.assertEqual(speech.wait_for_playout.await_count, 2)

    async def test_direct_admission_uses_original_trunk(self):
        self.state.call = replace(
            self.state.call,
            called_office_key="spring-hill",
            called_number="+18135484830",
        )
        requests = []
        target = "sip:office~ah1~" + "a" * 43 + "@handoff.example"

        def handler(request):
            requests.append(request)
            return httpx.Response(
                200,
                json={
                    "type": "DIRECT",
                    "handoffId": "handoff-test",
                    "expiresAt": (datetime.now(UTC) + timedelta(minutes=2)).isoformat(),
                    "sipUri": target,
                },
            )

        self.control.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        self.addAsyncCleanup(self.control.client.aclose)
        with patch.dict(
            "os.environ",
            {
                "ACUITY_HANDOFF_URL": "https://handoff.example/admit",
                "ACUITY_HANDOFF_SECRET": "offline",
            },
            clear=True,
        ):
            self.assertEqual(
                (await self.run_tool("transfer_call"))["outcome"], "accepted"
            )
        self.assertEqual(
            json.loads(requests[0].content)["routePhoneNumber"], "+18135484830"
        )
        self.assertTrue(requests[0].headers["Idempotency-Key"])
        self.assertEqual(
            self.sip.transfer_sip_participant.call_args.args[0].transfer_to, target
        )

    async def test_concurrent_registered_duplicates_send_one_refer(self):
        await self.run_tool("duplicate")
        self.sip.transfer_sip_participant.assert_awaited_once()
        self.assertEqual(self.events, ["announcement_done", "refer"])
        self.assertEqual(self.control.status, "accepted")
