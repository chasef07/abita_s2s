import asyncio
import unittest
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock, patch

from abita_s2s.config import Config, MiddlewareConfig
from abita_s2s.middleware import PatientNotFound
from abita_s2s.runtime.session_startup import start_voice_call


class PrecallStartupTests(unittest.IsolatedAsyncioTestCase):
    def setup_call(self, stack, *, phone="+15555550101", start=None):
        callbacks = []
        participant = SimpleNamespace(
            identity="caller",
            attributes={
                "sip.trunkPhoneNumber": "+18135484830",
                "sip.callID": "call",
                "sip.phoneNumber": phone,
            },
        )
        ctx = SimpleNamespace(
            is_fake_job=lambda: False,
            connect=AsyncMock(),
            wait_for_participant=AsyncMock(return_value=participant),
            room=SimpleNamespace(name="room", on=Mock(), off=Mock()),
            add_shutdown_callback=callbacks.append,
        )
        http = SimpleNamespace(aclose=AsyncMock())
        client = SimpleNamespace(
            resolve_patient=AsyncMock(return_value=PatientNotFound())
        )
        session = SimpleNamespace(start=start or AsyncMock(), on=Mock(), off=Mock())
        session_type = Mock(return_value=session)
        generic = MagicMock()
        generic.__getitem__.return_value = session_type
        stack.enter_context(
            patch(
                "abita_s2s.runtime.session_startup.load_config",
                return_value=Config("offline"),
            )
        )
        stack.enter_context(
            patch(
                "abita_s2s.runtime.session_startup.load_middleware_config",
                return_value=MiddlewareConfig(
                    "https://sandbox.example", "test-token", "spring_hill"
                ),
            )
        )
        stack.enter_context(
            patch(
                "abita_s2s.runtime.session_startup.httpx.AsyncClient", return_value=http
            )
        )
        stack.enter_context(
            patch(
                "abita_s2s.runtime.session_startup.MiddlewareClient",
                return_value=client,
            )
        )
        stack.enter_context(patch("abita_s2s.runtime.session_startup.create_model"))
        stack.enter_context(
            patch("abita_s2s.runtime.session_startup.AgentSession", generic)
        )
        return ctx, callbacks, http, client, session, session_type

    async def test_slow_lookup_does_not_delay_voice_start_and_client_closes_on_shutdown(
        self,
    ):
        entered, release, voice_started = (
            asyncio.Event(),
            asyncio.Event(),
            asyncio.Event(),
        )

        async def read(**kwargs):
            entered.set()
            await release.wait()
            return PatientNotFound()

        async def start(**kwargs):
            voice_started.set()

        with ExitStack() as stack:
            ctx, callbacks, http, client, _, session_type = self.setup_call(
                stack, start=AsyncMock(side_effect=start)
            )
            client.resolve_patient.side_effect = read
            task = asyncio.create_task(start_voice_call(ctx))
            await asyncio.wait_for(entered.wait(), 1)
            await asyncio.wait_for(voice_started.wait(), 1)
            self.assertFalse(task.done())
            release.set()
            await task
            client.resolve_patient.assert_awaited_once_with(
                office="spring_hill", phone="+15555550101"
            )
            state = session_type.call_args.kwargs["userdata"]
            self.assertEqual(state.call.called_number, "+18135484830")
            self.assertEqual(state.patient.lookup.status, "none")
            self.assertIsNone(state.patient.active)
            http.aclose.assert_not_awaited()
            await callbacks[0]()
            http.aclose.assert_awaited_once()

    async def test_shutdown_cancels_lookup_without_recording_no_match(self):
        entered = asyncio.Event()

        async def read(**kwargs):
            entered.set()
            await asyncio.Event().wait()

        with ExitStack() as stack:
            ctx, callbacks, http, client, _, session_type = self.setup_call(stack)
            client.resolve_patient.side_effect = read
            task = asyncio.create_task(start_voice_call(ctx))
            await asyncio.wait_for(entered.wait(), 1)
            await callbacks[0]()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertEqual(
                session_type.call_args.kwargs["userdata"].patient.lookup.status,
                "not_attempted",
            )
            self.assertTrue(http.aclose.await_count >= 1)

    async def test_hangup_or_session_close_cancels_before_shutdown_callbacks(self):
        for trigger in ("participant", "session"):
            with self.subTest(trigger=trigger):
                entered = asyncio.Event()

                async def read(**kwargs):
                    entered.set()
                    await asyncio.Event().wait()

                with ExitStack() as stack:
                    ctx, callbacks, http, client, session, session_type = (
                        self.setup_call(stack)
                    )
                    client.resolve_patient.side_effect = read
                    task = asyncio.create_task(start_voice_call(ctx))
                    await asyncio.wait_for(entered.wait(), 1)
                    if trigger == "participant":
                        handler = ctx.room.on.call_args.args[1]
                        handler(SimpleNamespace(identity="observer"))
                        self.assertFalse(task.done())
                        handler(SimpleNamespace(identity="caller"))
                    else:
                        session.on.call_args.args[1](object())
                    with self.assertRaises(asyncio.CancelledError):
                        await asyncio.wait_for(task, 1)
                    self.assertEqual(
                        session_type.call_args.kwargs["userdata"].patient.lookup.status,
                        "not_attempted",
                    )
                    http.aclose.assert_awaited_once()
                    ctx.room.off.assert_called()
                    # No job shutdown callback was needed to cancel the request.

    async def test_voice_start_failure_releases_client(self):
        with ExitStack() as stack:
            ctx, _, http, _, _, _ = self.setup_call(
                stack, start=AsyncMock(side_effect=RuntimeError("start failed"))
            )
            with self.assertRaisesRegex(RuntimeError, "start failed"):
                await start_voice_call(ctx)
            http.aclose.assert_awaited_once()

    async def test_missing_caller_id_skips_middleware(self):
        with ExitStack() as stack:
            ctx, callbacks, _, client, _, session_type = self.setup_call(
                stack, phone=""
            )
            await start_voice_call(ctx)
            client.resolve_patient.assert_not_awaited()
            self.assertFalse(callbacks)
            self.assertEqual(
                session_type.call_args.kwargs["userdata"].patient.lookup.status,
                "not_attempted",
            )
