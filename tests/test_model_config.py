"""Verify the clock context supplied to the managed thinker at call startup."""

import unittest
from datetime import datetime
from unittest.mock import patch

from abita_s2s.config import Config
from abita_s2s.model_config import create_model
from abita_s2s.prompt import load_prompt


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
            patch("abita_s2s.model_config.GPTLiveModel") as model,
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
