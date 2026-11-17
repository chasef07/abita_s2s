"""Verify the managed thinker configuration and what each live session is wired to."""

import unittest
from datetime import datetime
from unittest.mock import patch

from livekit.plugins.openai import LLM
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
                    create_model(
                        Config(openai_api_key="offline"),
                        "Hours\nWeekdays.",
                        "### Medical visits",
                    )
                    instructions = model.call_args.kwargs["responses_options"][
                        "instructions"
                    ]
                    self.assertEqual(
                        instructions,
                        load_prompt("thinker").format(
                            transfer_policy=load_prompt("transfer_policy"),
                            office_knowledge="Hours\nWeekdays.",
                            insurance_list="### Medical visits",
                        )
                        + f"\n\n## Call start\n\nCall started: {expected} (America/New_York). "
                        "Use this clinic-local date to interpret relative dates.",
                    )
            self.assertEqual(clock.now.call_count, len(cases))

    def test_thinker_reasons_at_high_effort(self):
        with patch("abita_s2s.model_config.CallModel") as model:
            create_model(Config(openai_api_key="offline"), "", "")
        options = model.call_args.kwargs["responses_options"]
        self.assertEqual(options["model"], "gpt-6-luna")
        self.assertEqual(options["reasoning"], {"effort": "high"})

    def test_undelegated_turns_are_judged_by_openai_on_our_key(self):
        model = create_model(Config(openai_api_key="offline"), "", "")
        detector = model._opts.delegation_recovery["detector"].model
        self.assertIsInstance(detector, LLM)
        self.assertEqual(detector.model, "gpt-5.6-luna")
        self.assertEqual(detector._opts.reasoning_effort, "none")
        self.assertEqual(detector._client.api_key, "offline")

    def test_thinker_calls_one_tool_at_a_time(self):
        with patch("abita_s2s.model_config.CallModel") as model:
            create_model(Config(openai_api_key="offline"), "", "")
        options = model.call_args.kwargs["responses_options"]
        self.assertIs(options["parallel_tool_calls"], False)


class CallModelTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        with patch.object(GPTLiveSession, "_main_task", idle):
            self.model = CallModel(api_key="offline")
            self.live = self.model.session()
        self.addAsyncCleanup(self.model.aclose)
        self.addAsyncCleanup(self.live.aclose)

    async def test_each_session_is_traced(self):
        self.assertIsInstance(self.live, ObservedGPTLiveSession)
