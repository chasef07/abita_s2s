import asyncio
from datetime import datetime
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from zoneinfo import ZoneInfo
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock, PropertyMock, patch

import httpx

from abita_s2s.agent import AbitaAgent
from abita_s2s.config import Config
from abita_s2s.middleware import NotFound
from abita_s2s.offices import (
    OFFICES,
    SPRING_HILL,
    get_office_profile,
    get_office_profile_by_phone,
)
from abita_s2s.runtime.session_startup import finish_voice_call, start_session, start_voice_call


class OfficeRoutingTests(unittest.TestCase):
    def test_imported_entrypoint_loads_local_environment_without_overriding_exports(self):
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, ".env.local").write_text("OPENAI_API_KEY=offline-file-key\n")
            env = {k: v for k, v in os.environ.items() if k != "OPENAI_API_KEY"}
            for exported, expected in ((None, "offline-file-key"), ("offline-exported-key", "offline-exported-key")):
                if exported:
                    env["OPENAI_API_KEY"] = exported
                result = subprocess.run(
                    [sys.executable, "-c", "import abita_s2s.main; import os; assert os.environ['OPENAI_API_KEY'] == " + repr(expected)],
                    cwd=directory, env=env, capture_output=True, text=True,
                )
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_aliases_select_same_office(self):
        for phone in (*SPRING_HILL.trunk_numbers, "(727) 591-9997", "1-813-548-4830"):
            self.assertIs(get_office_profile_by_phone(phone), SPRING_HILL)

    def test_all_production_routes(self):
        expected = {
            "spring-hill": ("+17275919997", "+18135484830"),
            "crystal-river": ("+13523202007",),
            "hollywood": ("+19542872010",),
            "sweetwater": ("+17864657475", "+17864654845", "+17866134310",
                           "+17864657479", "+17864654836", "+17864654882"),
            "north-miami-beach-optical": ("+13055095333",),
        }
        self.assertEqual({o.key for o in OFFICES}, set(expected))
        for key, numbers in expected.items():
            for number in numbers:
                self.assertIs(get_office_profile_by_phone(number), get_office_profile(key))
        for demo in ("+14843989071", "+18027878312", "+13207388132"):
            with self.assertRaises(ValueError):
                get_office_profile_by_phone(demo)

    def test_crystal_river_identity(self):
        office = get_office_profile("crystal-river")
        self.assertIn("Eye Radiance", office.greeting_name)
        self.assertIn("Current office: Eye Radiance", AbitaAgent(office, Mock()).instructions)

    def test_unknown_trunks_fail(self):
        for phone in ("", "+15555555555"):
            with self.assertRaises(ValueError):
                get_office_profile_by_phone(phone)


class StartupTests(unittest.IsolatedAsyncioTestCase):
    async def test_session_and_greeting_start_while_phone_lookup_is_pending(self):
        entered, release, greeted = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def lookup(*args):
            entered.set()
            await release.wait()
            return NotFound(status="not_found")

        async def start_with_greeting(session, ctx, room_options, agent):
            def generate_reply(**kwargs):
                self.assertFalse(release.is_set())
                greeted.set()
                handle = asyncio.get_running_loop().create_future()
                handle.set_result(None)
                return handle

            with patch.object(AbitaAgent, "session", new_callable=PropertyMock,
                              return_value=SimpleNamespace(generate_reply=generate_reply)):
                await agent.on_enter()
            await start_session(session, ctx, room_options, agent)

        with (
            patch("abita_s2s.runtime.session_startup.PatientMiddleware.resolve", side_effect=lookup),
            patch("abita_s2s.runtime.session_startup.start_session", side_effect=start_with_greeting),
        ):
            task = asyncio.create_task(self.run_startup(False))
            try:
                await asyncio.wait_for(entered.wait(), 2)
                await asyncio.wait_for(greeted.wait(), 0.2)
            finally:
                release.set()
                await asyncio.wait_for(task, 2)

    async def run_startup(self, fake, trunk="+18135484830", env=None, config=None, model_error=None, simulation=None):
        participant = SimpleNamespace(identity="caller", attributes={"sip.trunkPhoneNumber": trunk, "sip.phoneNumber": "+15555550101", "sip.callID": "sip-test"})
        ctx = SimpleNamespace(
            is_fake_job=lambda: fake,
            connect=AsyncMock(),
            wait_for_participant=AsyncMock(return_value=participant),
            room=SimpleNamespace(name="test-room", on=Mock(), off=Mock(), isconnected=lambda: True, remote_participants={"caller": participant}),
            add_shutdown_callback=Mock(side_effect=self.addAsyncCleanup),
        )
        session = SimpleNamespace(start=AsyncMock(), on=Mock())
        session_type = Mock(return_value=session)
        session_generic = MagicMock()
        session_generic.__getitem__.return_value = session_type
        with (
            patch.dict("os.environ", env or {}, clear=True),
            patch("abita_s2s.runtime.session_startup.load_config", return_value=config or Config("offline")),
            patch("abita_s2s.runtime.session_startup.create_model", side_effect=model_error),
            patch("abita_s2s.runtime.session_startup.api.LiveKitAPI", return_value=SimpleNamespace(sip=Mock(), aclose=AsyncMock())) as sip_api,
            patch("abita_s2s.runtime.session_startup.AgentSession", session_generic),
        ):
            await start_voice_call(ctx, simulation=simulation)
            if fake or simulation is not None:
                sip_api.assert_not_called()
            else:
                sip_api.assert_called_once_with(failover=False)
        args = session.start.call_args.kwargs
        args["userdata"] = session_type.call_args.kwargs["userdata"]
        return ctx, args

    async def test_simulation_uses_sandbox_and_non_sip_participant(self):
        sim = SimpleNamespace(
            userdata=lambda: {"office": "spring-hill"},
            simulation_job_id="simulation-intake",
        )
        ctx, args = await self.run_startup(
            False, trunk="", simulation=sim,
            env={
                "SANDBOX_AMD_API_URL": "https://abita-middleware-sandbox-test.run.app",
                "SANDBOX_AMD_API_TOKEN": "sandbox-token",
            },
            config=Config(
                "offline", middleware_url="https://production.test",
                middleware_token="production-token", product_secret="product-token",
                knowledge_url="https://product.test/v1/agent/knowledge/search",
                interaction_url="https://product.test/v1/ai/interactions",
                staff_tasks_url="https://product.test/v1/tasks",
            ),
        )
        ctx.wait_for_participant.assert_awaited_once_with()
        self.assertEqual(args["room_options"].participant_identity, "caller")
        state = args["userdata"]
        self.assertEqual(state.call.call_id, "simulation-intake")
        self.assertIsNone(state.call.sip_call_id)
        self.assertIsNone(state.call.caller_phone)
        self.assertIsNone(state.reporter)
        config = args["agent"]._insurance._middleware._config
        self.assertEqual(config.middleware_token, "sandbox-token")
        self.assertEqual(config.middleware_url, "https://abita-middleware-sandbox-test.run.app")
        self.assertIsNone(config.staff_tasks_url)
        self.assertEqual(config.product_secret, "product-token")
        self.assertEqual(config.knowledge_url, "https://product.test/v1/agent/knowledge/search")
        self.assertEqual(args["agent"]._knowledge._url, config.knowledge_url)
        self.assertEqual(args["agent"]._knowledge._secret, "product-token")
        self.assertIsNone(config.interaction_url)
        self.assertIsNone(config.handoff)

    async def test_simulation_rejects_missing_or_non_sandbox_backend(self):
        sim = SimpleNamespace(userdata=lambda: {"office": "spring-hill"})
        for url in ("", "https://production.test", "https://abita-middleware-sandbox-test.run.app.evil.test"):
            with self.subTest(url=url), self.assertRaisesRegex(ValueError, "SANDBOX_AMD"):
                await self.run_startup(False, simulation=sim, env={
                    "SANDBOX_AMD_API_URL": url, "SANDBOX_AMD_API_TOKEN": "test",
                })

    async def test_sip_ignores_console_override_and_binds_caller(self):
        ctx, args = await self.run_startup(False, env={"ABITA_CONSOLE_OFFICE": "invalid"})
        ctx.connect.assert_awaited_once()
        self.assertEqual(args["room_options"].participant_identity, "caller")
        self.assertTrue(args["room_options"].close_on_disconnect)
        self.assertTrue(args["room_options"].delete_room_on_close)
        self.assertEqual(args["agent"]._practice_name, SPRING_HILL.greeting_name)
        call = args["userdata"].call
        self.assertEqual(call.call_id, "sip-test")
        self.assertEqual(call.called_number, "+18135484830")
        self.assertEqual(call.called_office_key, "spring-hill")
        self.assertEqual(call.caller_phone, "+15555550101")
        self.assertIsNone(args["userdata"].patient.active)

    async def test_console_requires_explicit_office(self):
        with self.assertRaisesRegex(ValueError, "ABITA_CONSOLE_OFFICE"):
            await self.run_startup(True)
        ctx, args = await self.run_startup(True, env={"ABITA_CONSOLE_OFFICE": "spring-hill"})
        _, other_args = await self.run_startup(True, env={"ABITA_CONSOLE_OFFICE": "spring-hill"})
        self.assertNotEqual(args["userdata"].call.call_id, other_args["userdata"].call.call_id)
        self.assertIsNone(args["userdata"].call.caller_phone)
        self.assertIsNone(args["userdata"].call.sip_call_id)
        ctx.connect.assert_not_awaited()
        ctx.wait_for_participant.assert_not_awaited()

    async def test_sip_never_falls_back_for_unknown_trunk(self):
        with self.assertRaises(ValueError):
            await self.run_startup(False, trunk="", env={"ABITA_CONSOLE_OFFICE": "spring-hill"})

    async def test_http_cleanup_is_registered_before_model_startup_can_fail(self):
        callbacks = []
        ctx = SimpleNamespace(
            is_fake_job=lambda: True,
            add_shutdown_callback=callbacks.append,
        )
        client = SimpleNamespace(aclose=AsyncMock())
        with (
            patch.dict("os.environ", {"ABITA_CONSOLE_OFFICE": "spring-hill"}),
            patch("abita_s2s.runtime.session_startup.load_config", return_value=Config("offline")),
            patch("abita_s2s.runtime.session_startup.httpx.AsyncClient", return_value=client),
            patch("abita_s2s.runtime.session_startup.create_model", side_effect=RuntimeError("model startup failed")),
            self.assertRaisesRegex(RuntimeError, "model startup failed"),
        ):
            await start_voice_call(ctx)
        self.assertEqual(len(callbacks), 1)
        await callbacks[0]()
        client.aclose.assert_awaited_once()

    async def test_http_cleanup_drains_registration_write_before_closing_client(self):
        ctx, args = await self.run_startup(True, env={"ABITA_CONSOLE_OFFICE": "spring-hill"})
        owner = args["agent"]._insurance
        finish = asyncio.Event()
        owner._task = asyncio.create_task(finish.wait())
        close_client = ctx.add_shutdown_callback.call_args_list[0].args[0]
        shutdown = asyncio.create_task(close_client())
        await asyncio.sleep(0)
        self.assertFalse(owner._middleware._client.is_closed)
        finish.set()
        await shutdown
        self.assertTrue(owner._middleware._client.is_closed)

    async def test_http_cleanup_drains_scheduling_before_closing_client(self):
        ctx, args = await self.run_startup(True, env={"ABITA_CONSOLE_OFFICE": "spring-hill"})
        owner = args["agent"]._scheduling
        finish, entered = asyncio.Event(), asyncio.Event()
        owner._write_task = asyncio.create_task(finish.wait())
        original = owner.aclose
        async def close():
            entered.set()
            await original()
        close_client = ctx.add_shutdown_callback.call_args_list[0].args[0]
        with patch.object(owner, "aclose", new=close):
            shutdown = asyncio.create_task(close_client())
            try:
                await asyncio.wait_for(entered.wait(), 2)
                self.assertTrue(owner._closed)
                self.assertFalse(owner.http.client.is_closed)
            finally:
                finish.set()
                await shutdown
        self.assertTrue(owner.http.client.is_closed)

    async def test_shutdown_drains_staff_delivery_before_closing_transport(self):
        ctx, args = await self.run_startup(True, env={"ABITA_CONSOLE_OFFICE": "spring-hill"})
        owner = args["agent"]._staff_tasks
        order = []
        with (
            patch.object(owner, "aclose", new=AsyncMock(side_effect=lambda: order.append("staff"))),
            patch.object(owner._client, "aclose", new=AsyncMock(side_effect=lambda: order.append("http"))),
        ):
            await ctx.add_shutdown_callback.call_args_list[0].args[0]()
        self.assertEqual(order, ["staff", "http"])

    async def test_combined_shutdown_waits_for_other_writes_after_owner_failure(self):
        ctx, args = await self.run_startup(True, env={"ABITA_CONSOLE_OFFICE": "spring-hill"})
        agent = args["agent"]
        finish, entered = asyncio.Event(), asyncio.Event()
        async def wait_for_write():
            entered.set()
            await finish.wait()
        client = agent._staff_tasks._client
        with (
            patch.object(agent._scheduling, "aclose", new=AsyncMock(side_effect=RuntimeError("write failed"))),
            patch.object(agent._insurance, "aclose", new=wait_for_write),
        ):
            shutdown = asyncio.create_task(ctx.add_shutdown_callback.call_args_list[0].args[0]())
            try:
                await asyncio.wait_for(entered.wait(), 2)
                self.assertFalse(client.is_closed)
                self.assertFalse(shutdown.done())
            finally:
                finish.set()
                with self.assertRaisesRegex(RuntimeError, "write failed"):
                    await shutdown
        self.assertTrue(client.is_closed)
        self._cleanups.pop()  # Already awaited and asserted this cleanup failure.

    async def test_greeting_uses_office_profile(self):
        agent = AbitaAgent(SPRING_HILL, Mock())
        handle = AsyncMock()
        class Speech:
            def __await__(self):
                return handle().__await__()
            def exception(self):
                return None
        session = Mock()
        session.generate_reply.return_value = Speech()
        with (
            patch.object(AbitaAgent, "session", new_callable=unittest.mock.PropertyMock, return_value=session),
            patch("abita_s2s.agent.datetime") as clock,
        ):
            clock.now.return_value = datetime(2026, 9, 20, 14, 30, tzinfo=ZoneInfo("America/New_York"))
            await agent.on_enter()
        clock.now.assert_called_once_with(ZoneInfo("America/New_York"))
        instructions = session.generate_reply.call_args.kwargs["instructions"]
        self.assertIn(f'Practice name: "{SPRING_HILL.greeting_name}"', instructions)
        self.assertIn("Office-local time: 14:30 EDT (America/New_York)", instructions)

    async def test_product_closeout_waits_for_accepted_write_and_keeps_native_report(self):
        payloads = []
        async def handler(request):
            payloads.append(json.loads(request.content))
            return httpx.Response(201, json={"status": "created", "interactionId": "d3665980-68ce-4336-87af-e2ba40ad2e8e"})
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        config = Config("offline", product_secret="test", interaction_url="https://product.test/v1/ai/interactions")
        with patch("abita_s2s.runtime.session_startup.httpx.AsyncClient", return_value=client):
            ctx, args = await self.run_startup(False, config=config)
        state = args["userdata"]
        ctx.primary_session = SimpleNamespace(userdata=state)
        report = {"chat_history": {"items": []}, "events": [{"type": "close", "reason": "participant_disconnected"}], "usage": []}
        ctx.make_session_report = Mock(return_value=SimpleNamespace(
            to_dict=lambda: report, chat_history=SimpleNamespace(items=[]),
        ))
        ctx.tagger = Mock()
        entered, release = asyncio.Event(), asyncio.Event()
        async def write():
            entered.set()
            await release.wait()
            state.reporter.appointment({"action": "BOOKED", "externalPatientId": "original-patient", "newAppointmentId": "42", "bookingResult": {"status": "booked"}})
        args["agent"]._scheduling._write_task = asyncio.create_task(write())
        await entered.wait()
        shutdown = asyncio.create_task(finish_voice_call(ctx))
        await asyncio.sleep(0)
        self.assertFalse(client.is_closed)
        self.assertFalse(shutdown.done())
        release.set()
        await shutdown
        await ctx.add_shutdown_callback.call_args_list[0].args[0]()
        self.assertTrue(client.is_closed)
        self.assertEqual([p["kind"] for p in payloads], ["START", "OUTCOME_CHECKPOINT", "CLOSEOUT"])
        self.assertEqual(payloads[-1]["status"], "COMPLETED")
        self.assertEqual(payloads[-1]["transcript"], report)
        self.assertEqual(payloads[-1]["appointmentOutcome"]["externalPatientId"], "original-patient")
        self.assertEqual(payloads[-1]["officePhone"], "+17275919997")

    async def test_model_start_failure_delivers_failed_closeout_before_shutdown(self):
        payloads = []
        async def handler(request):
            payloads.append(json.loads(request.content))
            return httpx.Response(201, json={"status": "created", "interactionId": "d3665980-68ce-4336-87af-e2ba40ad2e8e"})
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        config = Config("offline", product_secret="test", interaction_url="https://product.test/v1/ai/interactions")
        with patch("abita_s2s.runtime.session_startup.httpx.AsyncClient", return_value=client), self.assertRaisesRegex(RuntimeError, "model failed"):
            await self.run_startup(False, config=config, model_error=RuntimeError("model failed"))
        self.assertEqual([p["kind"] for p in payloads], ["START", "CLOSEOUT"])
        self.assertEqual(payloads[-1]["status"], "FAILED")
        self.assertNotIn("transcript", payloads[-1])
