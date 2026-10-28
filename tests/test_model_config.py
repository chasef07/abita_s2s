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
                    create_model(Config(openai_api_key="offline"), "Hours\nWeekdays.")
                    instructions = model.call_args.kwargs["responses_options"][
                        "instructions"
                    ]
                    self.assertEqual(
                        instructions,
                        load_prompt("thinker").format(
                            transfer_policy=load_prompt("transfer_policy"),
                            office_knowledge="Hours\nWeekdays.",
                        )
                        + f"\n\n## Call start\n\nCall started: {expected} (America/New_York). "
                        "Use this clinic-local date to interpret relative dates.",
                    )
            self.assertEqual(clock.now.call_count, len(cases))

    def test_thinker_reasons_at_high_effort(self):
        with patch("abita_s2s.model_config.CallModel") as model:
            create_model(Config(openai_api_key="offline"), "")
        options = model.call_args.kwargs["responses_options"]
        self.assertEqual(options["model"], "gpt-6-luna")
        self.assertEqual(options["reasoning"], {"effort": "high"})

    def test_thinker_calls_one_tool_at_a_time(self):
        with patch("abita_s2s.model_config.CallModel") as model:
            create_model(Config(openai_api_key="offline"), "")
        options = model.call_args.kwargs["responses_options"]
        self.assertIs(options["parallel_tool_calls"], False)


class CallModelTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        with patch.object(GPTLiveSession, "_main_task", idle):
            self.model = CallModel(api_key="offline")
            self.live = self.model.session()
        self.addAsyncCleanup(self.model.aclose)
        self.addAsyncCleanup(self.live.aclose)
        self.sent = patch.object(self.live, "send_event").start()
        self.addCleanup(patch.stopall)

    def receive(self, *events):
        for event in events:
            self.live.emit("openai_server_event_received", event)
            self.live._handle_event(event)

    async def test_each_session_is_traced(self):
        self.assertIsInstance(self.live, ObservedGPTLiveSession)

    async def test_an_undelegated_caller_turn_is_handed_off_once(self):
        self.receive(
            {"type": "session.input_transcript.delta", "delta": " Book it please."}
        )
        self.assertTrue(self.model.watchdog.hand_off_held())
        item, create = (call.args[0] for call in self.sent.call_args_list)
        self.assertIn(
            "The caller said: Book it please.\n",
            item["item"]["content"][0]["text"],
        )
        self.assertEqual(create["type"], "response.create")

    async def test_a_tool_call_awaiting_its_output_holds_the_hand_off(self):
        self.receive(
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
        )
        self.assertTrue(self.model.backend.caller_spoke_since_request)
        self.assertEqual(self.model.backend.running, set())
        self.assertFalse(self.model.watchdog.hand_off_held())
        self.sent.assert_not_called()
