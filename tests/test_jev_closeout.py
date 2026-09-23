"""Post-call evaluation errors must remain visible in Product's closeout evidence."""

import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from livekit.agents import ChatContext
from livekit.agents.llm import AgentConfigUpdate

from abita_s2s.jev import evaluate_call


class JevCloseoutTests(unittest.IsolatedAsyncioTestCase):
    def report(self):
        history = ChatContext()
        history.items.append(AgentConfigUpdate(instructions="Manage appointments."))
        history.add_message(role="user", content="Please cancel my visit.")
        return {"chat_history": history.to_dict()}

    async def test_results_and_metadata_are_returned_for_persistence(self):
        report = self.report()
        results = {
            "outcome": {"answers": {"request_fulfilled": {"probability": 0.1}}},
            "reaction": {
                "answers": {
                    "expressed_sentiment": {
                        "type": "score",
                        "score": 2,
                        "probabilities": {"2": 1},
                    }
                }
            },
        }
        with (
            patch.dict("os.environ", {"AI_GATEWAY_API_KEY": "offline"}),
            patch(
                "abita_s2s.jev.evaluate_with_jev",
                new_callable=AsyncMock,
                return_value=results,
            ) as judge,
        ):
            result = await evaluate_call(report)
        self.assertEqual(judge.await_args.args[0].to_dict(), report["chat_history"])
        self.assertEqual(
            judge.await_args.kwargs["agent_purpose"], "Manage appointments."
        )
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["results"], results)
        self.assertEqual(result["evaluatorVersion"], "typesafe-trace-v4")
        self.assertIn("evaluatedAt", result)

    async def test_timeout_or_error_is_incomplete_not_a_call_failure(self):
        async def hang(*_, **__):
            await asyncio.Event().wait()

        for failure in (hang, ValueError("private API body")):
            report = self.report()
            with (
                patch.dict("os.environ", {"AI_GATEWAY_API_KEY": "offline"}),
                patch(
                    "abita_s2s.jev.evaluate_with_jev",
                    new_callable=AsyncMock,
                    side_effect=failure,
                ),
                patch("abita_s2s.jev.EVALUATION_SECONDS", 0.01),
                self.assertLogs("abita_s2s.jev", "ERROR") as logs,
            ):
                result = await evaluate_call(report)
            self.assertEqual(result["status"], "incomplete")
            self.assertNotIn("private API body", str(result) + str(logs.output))

    async def test_missing_key_or_instructions_never_calls_provider(self):
        for key, instructions, expected in [
            ("", True, "gateway_key_not_configured"),
            ("offline", False, "agent_instructions_missing"),
        ]:
            report = self.report()
            if not instructions:
                report["chat_history"]["items"].pop(0)
            with (
                patch.dict("os.environ", {"AI_GATEWAY_API_KEY": key}),
                patch("abita_s2s.jev.evaluate_with_jev") as judge,
            ):
                result = await evaluate_call(report)
            judge.assert_not_called()
            self.assertEqual(result["reason"], expected)

    async def test_no_caller_or_missing_report(self):
        report = self.report()
        report["chat_history"] = ChatContext().to_dict()
        result = await evaluate_call(report)
        self.assertEqual(result["status"], "skipped")
        with self.assertLogs("abita_s2s.jev", "ERROR"):
            result = await evaluate_call({})
        self.assertEqual(result["status"], "incomplete")
