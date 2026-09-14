import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock, patch

from abita_s2s.agent import AbitaAgent
from abita_s2s.config import Config
from abita_s2s.offices import (
    OFFICES,
    SPRING_HILL,
    get_office_profile,
    get_office_profile_by_phone,
)
from abita_s2s.runtime.session_startup import start_voice_call


class OfficeRoutingTests(unittest.TestCase):
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
        self.assertIn("Eye Radiance", office.greeting)
        self.assertIn("Current office: Eye Radiance", AbitaAgent(office, Mock()).instructions)

    def test_unknown_trunks_fail(self):
        for phone in ("", "+15555555555"):
            with self.assertRaises(ValueError):
                get_office_profile_by_phone(phone)


class StartupTests(unittest.IsolatedAsyncioTestCase):
    async def run_startup(self, fake, trunk="+18135484830", env=None):
        participant = SimpleNamespace(identity="caller", attributes={"sip.trunkPhoneNumber": trunk, "sip.phoneNumber": "+15555550101", "sip.callID": "sip-test"})
        ctx = SimpleNamespace(
            is_fake_job=lambda: fake,
            connect=AsyncMock(),
            wait_for_participant=AsyncMock(return_value=participant),
            room=SimpleNamespace(name="test-room"),
            add_shutdown_callback=Mock(side_effect=self.addAsyncCleanup),
        )
        session = SimpleNamespace(start=AsyncMock())
        session_type = Mock(return_value=session)
        session_generic = MagicMock()
        session_generic.__getitem__.return_value = session_type
        with (
            patch.dict("os.environ", env or {}, clear=True),
            patch("abita_s2s.runtime.session_startup.load_config", return_value=Config("offline")),
            patch("abita_s2s.runtime.session_startup.create_model"),
            patch("abita_s2s.runtime.session_startup.AgentSession", session_generic),
        ):
            await start_voice_call(ctx)
        args = session.start.call_args.kwargs
        args["userdata"] = session_type.call_args.kwargs["userdata"]
        return ctx, args

    async def test_sip_ignores_console_override_and_binds_caller(self):
        ctx, args = await self.run_startup(False, env={"ABITA_CONSOLE_OFFICE": "invalid"})
        ctx.connect.assert_awaited_once()
        self.assertEqual(args["room_options"].participant_identity, "caller")
        self.assertEqual(args["agent"]._greeting, SPRING_HILL.greeting)
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
        finish = asyncio.Event()
        owner._write_task = asyncio.create_task(finish.wait())
        close_client = ctx.add_shutdown_callback.call_args_list[0].args[0]
        shutdown = asyncio.create_task(close_client())
        await asyncio.sleep(0)
        self.assertTrue(owner._closed)
        self.assertFalse(owner.http.client.is_closed)
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
        with patch.object(AbitaAgent, "session", new_callable=unittest.mock.PropertyMock, return_value=session):
            await agent.on_enter()
        self.assertIn(SPRING_HILL.greeting, session.generate_reply.call_args.kwargs["instructions"])
