"""Verify the managed thinker configuration and what each live session is wired to."""

import unittest
from datetime import datetime
from unittest.mock import patch

from livekit.plugins.openai.realtime import GPTLiveSession

from abita_s2s.config import Config
from abita_s2s.model_config import CallModel, create_model
from abita_s2s.prompt import load_prompt
from abita_s2s.runtime.observability import ObservedGPTLiveSession


async def idle(session):
    async for _ in session._msg_ch:
        pass


class ModelConfigTests(unittest.TestCase):
    def test_each_call_gets_eastern_time_across_midnight_and_dst(self):
        cases = (
            ("2026-09-16T03:59:00+00:00", "Tuesday, 2026-09-15 23:59 EDT"),
            ("2026-09-16T04:00:00+00:00", "Wednesday, 2026-09-16 00:00 EDT"),
            ("2026-03-08T06:59:00+00:00", "Sunday, 2026-03-08 01:59 EST"),
            ("2026-03-08T07:00:00+00:00", "Sunday, 2026-03-08 03:00 EDT"),
            ("2026-11-01T05:30:00+00:00", "Sunday, 2026-11-01 01:30 EDT"),
            ("2026-11-01T06:30:00+00:00", "Sunday, 2026-11-01 01:30 EST"),
        )
        with (
            patch("abita_s2s.model_config.datetime") as clock,
            patch("abita_s2s.model_config.CallModel") as model,
        ):
            for instant, expected in cases:
                with self.subTest(instant=instant):
                    clock.now.side_effect = lambda tz: datetime.fromisoformat(
                        instant
                    ).astimezone(tz)
                    create_model(Config(openai_api_key="offline"))
                    instructions = model.call_args.kwargs["responses_options"][
                        "instructions"
                    ]
                    self.assertEqual(
                        instructions,
                        load_prompt("thinker")
                        + f"\n\nCall started: {expected} (America/New_York). "
                        "Use this clinic-local date to interpret relative dates.",
                    )
            self.assertEqual(clock.now.call_count, len(cases))

    def test_thinker_reasons_at_high_effort(self):
        with patch("abita_s2s.model_config.CallModel") as model:
            create_model(Config(openai_api_key="offline"))
        options = model.call_args.kwargs["responses_options"]
        self.assertEqual(options["model"], "gpt-6-luna")
        self.assertEqual(options["reasoning"], {"effort": "high"})

    def test_thinker_calls_one_tool_at_a_time(self):
        with patch("abita_s2s.model_config.CallModel") as model:
            create_model(Config(openai_api_key="offline"))
        options = model.call_args.kwargs["responses_options"]
        self.assertIs(options["parallel_tool_calls"], False)


class CallModelTests(unittest.IsolatedAsyncioTestCase):
    async def test_each_session_is_traced_and_feeds_the_backend_and_watchdog(self):
        with patch.object(GPTLiveSession, "_main_task", idle):
            model = CallModel(api_key="offline")
            live = model.session()
        self.addAsyncCleanup(model.aclose)
        self.addAsyncCleanup(live.aclose)
        self.assertIsInstance(live, ObservedGPTLiveSession)
        sent = patch.object(live, "send_event").start()
        self.addCleanup(patch.stopall)
        for event in (
            {"type": "session.input_transcript.delta", "delta": " Book it please."},
            {
                "type": "response.event",
                "delegation_id": "d1",
                "event": {"type": "response.created"},
            },
            {
                "type": "response.event",
                "delegation_id": "d1",
                "event": {
                    "type": "response.output_item.done",
                    "item": {
                        "type": "function_call",
                        "status": "completed",
                        "call_id": "c1",
                        "name": "book",
                        "arguments": "{}",
                    },
                },
            },
            {
                "type": "response.event",
                "delegation_id": "d1",
                "event": {"type": "response.completed"},
            },
        ):
            live.emit("openai_server_event_received", event)
            live._handle_event(event)
        self.assertTrue(model.backend.caller_spoke_since_request)
        self.assertEqual(model.backend.running, set())
        self.assertFalse(model.watchdog.hand_off_held())
        sent.assert_not_called()
