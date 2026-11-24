"""Verify the managed thinker configuration and what each live session is wired to."""

import os
import unittest
from datetime import datetime
from unittest.mock import patch

from livekit.agents import inference
from livekit.plugins.openai.realtime import GPTLiveSession

from abita_s2s.config import Config
from abita_s2s.model_config import CallModel, create_model
from abita_s2s.prompt import load_prompt
from abita_s2s.runtime.observability import ObservedGPTLiveSession


async def idle(session):
    async for _ in session._msg_ch:
        pass


class ModelConfigTests(unittest.TestCase):
    def test_each_call_gets_the_eastern_date_across_midnight_and_dst(self):
        cases = (
            ("2026-09-16T03:59:00+00:00", "Tuesday, 2026-09-15"),
            ("2026-09-16T04:00:00+00:00", "Wednesday, 2026-09-16"),
            ("2026-03-08T06:59:00+00:00", "Sunday, 2026-03-08"),
            ("2026-03-08T07:00:00+00:00", "Sunday, 2026-03-08"),
            ("2026-11-01T05:30:00+00:00", "Sunday, 2026-11-01"),
            ("2026-11-01T06:30:00+00:00", "Sunday, 2026-11-01"),
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
                        + f"\n\n## Call date\n\nCall date: {expected}. "
                        "Use this date to interpret relative dates.",
                    )
            self.assertEqual(clock.now.call_count, len(cases))

    def test_calls_at_one_office_on_one_day_share_identical_instructions(self):
        instructions = []
        with (
            patch("abita_s2s.model_config.datetime") as clock,
            patch("abita_s2s.model_config.CallModel") as model,
        ):
            for instant in ("2026-11-24T13:40:12+00:00", "2026-11-24T21:05:59+00:00"):
                clock.now.side_effect = lambda tz, instant=instant: (
                    datetime.fromisoformat(instant).astimezone(tz)
                )
                create_model(Config(openai_api_key="offline"), "Hours", "Plans")
                instructions.append(
                    model.call_args.kwargs["responses_options"]["instructions"]
                )
        self.assertEqual(instructions[0], instructions[1])

    def test_thinker_reasons_at_high_effort(self):
        with patch("abita_s2s.model_config.CallModel") as model:
            create_model(Config(openai_api_key="offline"), "", "")
        options = model.call_args.kwargs["responses_options"]
        self.assertEqual(options["model"], "gpt-6.1-terra")
        self.assertEqual(options["reasoning"], {"effort": "high"})

    def test_undelegated_turns_are_judged_by_livekit_default_detector(self):
        with patch.dict(
            os.environ, {"LIVEKIT_API_KEY": "offline", "LIVEKIT_API_SECRET": "offline"}
        ):
            model = create_model(Config(openai_api_key="offline"), "", "")
        detector = model._opts.delegation_recovery["detector"].model
        self.assertIsInstance(detector, inference.LLM)
        self.assertEqual(detector.model, "google/gemma-4-31b-it")

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
