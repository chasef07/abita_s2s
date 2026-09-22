"""Verify the evidence boundary without sending call data to an external model."""

import json
import unittest
from unittest.mock import patch

import httpx
from livekit.agents import ChatContext
from livekit.agents.llm import AgentConfigUpdate, FunctionCall, FunctionCallOutput

from abita_s2s.jev import QUESTIONS, evaluate_with_jev


class JevTests(unittest.IsolatedAsyncioTestCase):
    async def run_evaluation(
        self, feedback=None, missing_answer=False, invalid_answer=None
    ):
        history = ChatContext()
        history.items.append(
            AgentConfigUpdate(instructions="Confirm before canceling.")
        )
        history.add_message(role="user", content="Yes, cancel my appointment.")
        history.items.extend(
            [
                FunctionCall(
                    call_id="cancel-1", name="cancel_appt", arguments='{"id":"1"}'
                ),
                FunctionCallOutput(
                    call_id="cancel-1",
                    name="cancel_appt",
                    output="Timed out",
                    is_error=True,
                ),
            ]
        )
        history.add_message(
            role="assistant", content="It is canceled.", interrupted=True
        )
        requests = []

        def respond(request):
            payload = json.loads(request.content)
            requests.append(payload)
            answers = {
                name: {"type": question["type"], "probability": 0.2}
                if question["type"] == "boolean"
                else {
                    "type": "score",
                    "score": 1.1,
                    "probabilities": {"0": 0.2, "1": 0.5, "2": 0.3},
                }
                for name, question in payload["questions"].items()
            }
            if invalid_answer is not None:
                name, answer = invalid_answer
                if name in answers:
                    answers[name] = answer
            if missing_answer:
                answers.pop(next(iter(answers)))
            return httpx.Response(
                200,
                json={
                    "answers": answers,
                    "model": "typesafe-ai/jev",
                    "usage": {"inputTokens": 100},
                },
            )

        client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
        with (
            patch.dict("os.environ", {"AI_GATEWAY_API_KEY": "synthetic-test-key"}),
            patch("abita_s2s.jev.httpx.AsyncClient", return_value=client),
        ):
            result = await evaluate_with_jev(
                history, agent_purpose="Manage appointments.", feedback=feedback
            )
        return requests, result

    async def test_full_outcome_history_and_isolated_clarity(self):
        requests, result = await self.run_evaluation()
        self.assertEqual(len(requests), 2)
        items = requests[0]["state"]["conversation"]
        self.assertEqual(
            [item["type"] for item in items],
            [
                "agent_config_update",
                "message",
                "function_call",
                "function_call_output",
                "message",
            ],
        )
        self.assertEqual(items[0]["instructions"], "Confirm before canceling.")
        self.assertEqual(items[2]["call_id"], items[3]["call_id"])
        self.assertTrue(items[3]["is_error"])
        self.assertTrue(items[4]["interrupted"])
        self.assertIn("created_at", items[4])
        clarity = requests[1]["state"]
        self.assertEqual(len(clarity["user_messages"]), 1)
        self.assertNotIn("conversation", clarity)
        self.assertEqual(result["reaction"]["status"], "unavailable")
        self.assertEqual(result["outcome"]["usage"]["inputTokens"], 100)

    async def test_reaction_uses_only_supplied_feedback_and_final_message(self):
        feedback = {"comment": "My appointment is still scheduled."}
        requests, result = await self.run_evaluation(feedback)
        self.assertEqual(len(requests), 3)
        state = requests[2]["state"]
        self.assertEqual(state["feedback"], feedback)
        self.assertNotIn("conversation", state)
        self.assertEqual(set(result["reaction"]["answers"]), set(QUESTIONS["reaction"]))

    async def test_missing_answer_is_an_error(self):
        with self.assertRaisesRegex(ValueError, "Incomplete Jev answers"):
            await self.run_evaluation(missing_answer=True)

    async def test_malformed_answer_values_are_errors(self):
        for name, answer in [
            ("request_fulfilled", None),
            ("request_fulfilled", {}),
            ("request_fulfilled", {"type": "boolean", "probability": True}),
            ("request_fulfilled", {"type": "boolean", "probability": 1.5}),
            ("request_specificity", {"type": "score", "score": 1.0}),
            (
                "request_specificity",
                {"type": "score", "score": "1", "probabilities": {"0": 1}},
            ),
            (
                "request_specificity",
                {"type": "score", "score": 1, "probabilities": {"0": -1}},
            ),
        ]:
            with self.subTest(name=name, answer=answer), self.assertRaises(ValueError):
                await self.run_evaluation(invalid_answer=(name, answer))
