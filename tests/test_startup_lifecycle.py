import asyncio
from dataclasses import replace
import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import httpx
from livekit.agents import room_io
import test_office_startup as existing
from test_staff_tasks import CONFIG, NEED, receipt

from abita_s2s.config import load_config
from abita_s2s.runtime import session_startup as startup


class Room:
    def __init__(self):
        self.handlers = {}
        self.connected = True
        self.remote_participants = {"caller": SimpleNamespace(identity="caller")}

    def on(self, name, fn):
        self.handlers[name] = fn

    def off(self, name, fn):
        self.handlers.pop(name)

    def isconnected(self):
        return self.connected

    def disconnect(self):
        self.connected = False
        self.handlers["disconnected"]()


class LifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_missing_sip_is_bounded_and_listener_removed(self):
        room = Room()
        ctx = SimpleNamespace(
            room=room, wait_for_participant=AsyncMock(side_effect=asyncio.Event().wait)
        )

        # accept the SDK keyword; await an absent participant forever
        async def absent(**_):
            await asyncio.Event().wait()

        ctx.wait_for_participant = absent
        with (
            patch.object(startup, "SIP_WAIT_SECONDS", 0.01),
            self.assertRaises(TimeoutError),
        ):
            await startup.wait_for_sip(ctx)
        self.assertEqual(room.handlers, {})

    async def test_disconnect_cancels_sip_wait(self):
        room, entered, cancelled = Room(), asyncio.Event(), asyncio.Event()

        async def absent(**_):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        task = asyncio.create_task(
            startup.wait_for_sip(
                SimpleNamespace(room=room, wait_for_participant=absent)
            )
        )
        await entered.wait()
        room.disconnect()
        with self.assertRaisesRegex(RuntimeError, "disconnected"):
            await task
        self.assertTrue(cancelled.is_set())
        self.assertEqual(room.handlers, {})

    async def test_disconnect_during_session_start_cancels_start(self):
        for participant_only in (False, True):
            room, entered, cancelled = Room(), asyncio.Event(), asyncio.Event()

            async def start(**_):
                entered.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    cancelled.set()

            ctx = SimpleNamespace(room=room, is_fake_job=lambda: False)
            task = asyncio.create_task(
                startup.start_session(
                    SimpleNamespace(start=start),
                    ctx,
                    room_io.RoomOptions(participant_identity="caller"),
                    Mock(),
                )
            )
            await entered.wait()
            if participant_only:
                room.handlers["participant_disconnected"](
                    SimpleNamespace(identity="caller")
                )
            else:
                room.disconnect()
            with self.assertRaisesRegex(RuntimeError, "disconnected"):
                await task
            self.assertTrue(cancelled.is_set())
            self.assertEqual(room.handlers, {})

    async def test_caller_already_gone_before_session_start_is_rejected(self):
        room = Room()
        room.remote_participants.clear()
        ctx = SimpleNamespace(room=room, is_fake_job=lambda: False)
        session = SimpleNamespace(start=AsyncMock())
        with self.assertRaisesRegex(RuntimeError, "disconnected"):
            await startup.start_session(
                session, ctx, room_io.RoomOptions(participant_identity="caller"), Mock()
            )
        session.start.assert_not_awaited()

    async def test_session_start_failure_closes_application_transports(self):
        callbacks = []
        client = httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _: httpx.Response(500))
        )
        ctx = SimpleNamespace(
            is_fake_job=lambda: True,
            room=Mock(),
            add_shutdown_callback=callbacks.append,
        )
        session = SimpleNamespace(
            start=AsyncMock(side_effect=RuntimeError("failed")), on=Mock()
        )
        generic = MagicMock()
        generic.__getitem__.return_value = Mock(return_value=session)
        with (
            patch.dict(
                "os.environ",
                {"ABITA_CONSOLE_OFFICE": "spring-hill", "OPENAI_API_KEY": "offline"},
                clear=True,
            ),
            patch.object(startup, "AgentSession", generic),
            patch.object(startup, "create_model"),
            patch.object(startup.httpx, "AsyncClient", return_value=client),
            self.assertRaisesRegex(RuntimeError, "failed"),
        ):
            await startup.start_voice_call(ctx)
        self.assertTrue(client.is_closed)
        # SDK may invoke application callback again concurrently after failure.
        await asyncio.gather(*(callback() for callback in callbacks))

    async def test_concurrent_sdk_cleanup_cancels_read_and_drains_accepted_http_write(
        self,
    ):
        entered, finish, read_cancelled = (
            asyncio.Event(),
            asyncio.Event(),
            asyncio.Event(),
        )
        order = []

        async def response(request):
            entered.set()
            await finish.wait()
            self.assertTrue(read_cancelled.is_set())
            order.append("write")
            return httpx.Response(201, json=receipt(json.loads(request.content)))

        client = httpx.AsyncClient(transport=httpx.MockTransport(response))
        with patch.object(startup.httpx, "AsyncClient", return_value=client):
            ctx, args = await existing.StartupTests.run_startup(
                self, True, env={"ABITA_CONSOLE_OFFICE": "spring-hill"}
            )
        agent = args["agent"]
        owner, resolver = agent._staff_tasks, agent._resolver
        owner._url, owner._secret = CONFIG.staff_tasks_url, CONFIG.product_secret
        owner.state.call = replace(owner.state.call, caller_phone="+15555550101")

        async def private_read():
            try:
                await asyncio.Event().wait()
            finally:
                read_cancelled.set()

        resolver._precall = asyncio.create_task(private_read())
        caller = asyncio.create_task(owner.submit(**NEED))
        await entered.wait()

        async def sdk_session_close():
            caller.cancel()
            await asyncio.gather(caller, return_exceptions=True)

        app_close = ctx.add_shutdown_callback.call_args.args[0]
        closing = asyncio.gather(app_close(), sdk_session_close(), app_close())
        await asyncio.wait_for(read_cancelled.wait(), 1)
        self.assertTrue(owner._closed)
        self.assertFalse(client.is_closed)
        self.assertEqual((await owner.submit(**NEED))["outcome"], "failed")
        finish.set()
        await closing
        self.assertEqual(order, ["write"])
        self.assertTrue(client.is_closed)
        self.assertEqual(
            next(iter(owner._deliveries.values())).result()["outcome"], "created"
        )

    async def test_cleanup_budget_expires_visibly_and_closes_transport(self):
        ctx, args = await existing.StartupTests.run_startup(
            self, True, env={"ABITA_CONSOLE_OFFICE": "spring-hill"}
        )
        owner = args["agent"]._insurance
        pending = asyncio.create_task(asyncio.Event().wait())
        owner._task = pending
        try:
            with (
                patch.object(startup, "CLEANUP_SECONDS", 0.01),
                self.assertRaises(TimeoutError),
            ):
                await ctx.add_shutdown_callback.call_args.args[0]()
            self.assertTrue(owner._middleware._client.is_closed)
            self.assertTrue(owner._closed)
            self._cleanups.pop()  # The expected timeout was already asserted.
        finally:
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
        self.assertGreater(startup.CLEANUP_SECONDS, 40)
        self.assertGreater(startup.SHUTDOWN_PROCESS_SECONDS, startup.CLEANUP_SECONDS)


class StagingConfigTests(unittest.TestCase):
    def test_staging_never_inherits_production_backend_keys(self):
        env = {
            "OPENAI_API_KEY": "offline",
            "LIVEKIT_AGENT_DEPLOYMENT": "staging",
            "AMD_API_URL": "https://production.example",
            "AMD_API_TOKEN": "production",
        }
        with (
            patch.dict("os.environ", env, clear=True),
            self.assertRaisesRegex(ValueError, "staging"),
        ):
            load_config()

    def test_staging_requires_every_key_and_distinct_credentials(self):
        env = {
            "OPENAI_API_KEY": "offline",
            "LIVEKIT_AGENT_DEPLOYMENT": "staging",
            "STAGING_AMD_API_URL": "https://sandbox.example",
            "STAGING_AMD_API_TOKEN": "sandbox",
            "STAGING_ACUITY_PRODUCT_KNOWLEDGE_URL": "https://sandbox.example/knowledge",
            "STAGING_ACUITY_PRODUCT_HANDOFF_URL": "https://sandbox.example/v1/handoffs",
            "STAGING_ABITA_EYE_GROUP_PRODUCT_SERVICE_SECRET": "sandbox-product",
        }
        with patch.dict("os.environ", env, clear=True):
            config = load_config()
            self.assertEqual(config.middleware_token, "sandbox")
            self.assertEqual(config.staff_tasks_url, "https://sandbox.example/v1/tasks")
        for key in env:
            if key.startswith("STAGING_"):
                missing = {k: v for k, v in env.items() if k != key}
                with (
                    patch.dict("os.environ", missing, clear=True),
                    self.assertRaises(ValueError),
                ):
                    load_config()
                with (
                    patch.dict(
                        "os.environ",
                        {**env, key.removeprefix("STAGING_"): env[key]},
                        clear=True,
                    ),
                    self.assertRaises(ValueError),
                ):
                    load_config()

    def test_unknown_deployment_fails(self):
        with (
            patch.dict(
                "os.environ",
                {"OPENAI_API_KEY": "offline", "LIVEKIT_AGENT_DEPLOYMENT": "preview"},
                clear=True,
            ),
            self.assertRaises(ValueError),
        ):
            load_config()
