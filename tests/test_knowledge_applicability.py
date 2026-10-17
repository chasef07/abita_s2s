"""Knowledge applicability is judged from caller requests, not missing lookups."""

import json
import unittest
from unittest.mock import patch

import httpx
from livekit.agents import ChatContext
from livekit.agents.llm import FunctionCall, FunctionCallOutput

from abita_s2s.observability.evaluation import evaluate_call
from abita_s2s.observability.judges import JURORS


class KnowledgeApplicabilityTests(unittest.IsolatedAsyncioTestCase):
    async def evaluate(
        self,
        choices,
        *,
        question="Please cancel my appointment.",
        output=None,
        error=False,
    ):
        history = ChatContext()
        history.add_message(role="user", content=question)
        if output is not None:
            history.items.extend(
                [
                    FunctionCall(
                        call_id="knowledge",
                        name="search_office_knowledge",
                        arguments='{"query":"hours"}',
                    ),
                    FunctionCallOutput(
                        call_id="knowledge",
                        name="search_office_knowledge",
                        output=output,
                        is_error=error,
                    ),
                ]
            )
        requests = []

        async def respond(request):
            payload = json.loads(request.content)
            requests.append(payload)
            if "intent" in payload["questions"]:
                return httpx.Response(200, json={"answers": {}})
            self.assertIn("knowledge_available", payload["questions"])
            definition = payload["questions"]["knowledge_available"]
            self.assertEqual(definition["type"], "choice")
            selected = choices[JURORS.index(payload["model"])]
            answers = {
                name: {"type": "noul", "noul": 0.9} for name in payload["questions"]
            }
            answers["knowledge_available"] = {
                "type": "choice",
                "choice": selected,
                "probabilities": {
                    key: float(key == selected) for key in definition["criteria"]
                },
            }
            return httpx.Response(200, json={"answers": answers})

        client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
        with patch(
            "abita_s2s.observability.evaluation.httpx.AsyncClient", return_value=client
        ):
            result = await evaluate_call(
                {"chat_history": history.to_dict()}, "synthetic", 60
            )
        self.assertEqual(len(requests), 4)
        return result

    async def test_no_office_question_is_not_applicable_without_a_lookup(self):
        result = await self.evaluate(("not_applicable", "not_applicable", "unknown"))
        self.assertEqual(
            result["results"]["knowledge_available"]["status"], "not_applicable"
        )
        self.assertEqual(
            result["results"]["knowledge_available"]["reason"], "no_office_question"
        )
        self.assertEqual(result["status"], "complete")

    async def test_office_question_without_a_usable_lookup_stays_unknown(self):
        for output, error in (
            (None, False),
            ("blocked: Outage", False),
            ("success: hours", True),
        ):
            with self.subTest(output=output):
                result = await self.evaluate(
                    ("unknown",) * 3,
                    question="What are your hours?",
                    output=output,
                    error=error,
                )
                self.assertEqual(
                    result["results"]["knowledge_available"]["status"], "unknown"
                )
                self.assertNotIn("verdict", result["results"]["knowledge_available"])

    async def test_completed_search_can_pass_or_fail(self):
        for state, output, verdict in (
            ("available", "success: Open Monday", True),
            ("missing", "no_results: Nothing found", False),
        ):
            with self.subTest(state=state):
                result = await self.evaluate(
                    (state,) * 3, question="What are your hours?", output=output
                )
                self.assertEqual(
                    result["results"]["knowledge_available"]["verdict"], verdict
                )

    async def test_model_cannot_grade_missing_or_failed_search_as_available(self):
        for output, error in (
            (None, False),
            ("blocked: Outage", False),
            ("success: hours", True),
        ):
            result = await self.evaluate(
                ("available",) * 3,
                question="What are your hours?",
                output=output,
                error=error,
            )
            self.assertEqual(
                result["results"]["knowledge_available"]["status"], "unknown"
            )
            self.assertNotIn("verdict", result["results"]["knowledge_available"])

    async def test_disagreement_cannot_hide_an_applicable_check(self):
        result = await self.evaluate(
            ("not_applicable", "unknown", "available"), question="What are your hours?"
        )
        self.assertNotIn("verdict", result["results"]["knowledge_available"])
        self.assertEqual(result["results"]["knowledge_available"]["status"], "unknown")

    async def test_outage_does_not_make_the_agent_answer_check_not_applicable(self):
        result = await self.evaluate(
            ("unknown",) * 3, question="What are your hours?", output="blocked: Outage"
        )
        self.assertIn("verdict", result["results"]["office_question_answered"])

    async def test_not_applicable_requires_a_quorum_of_valid_answers(self):
        result = await self.evaluate(("not_applicable", None, None))
        self.assertNotIn("knowledge_available", result["results"])
        self.assertEqual(result["errors"]["knowledge_available"]["cause"], "no_quorum")
        self.assertEqual(result["status"], "incomplete")

    async def test_incidental_lookup_does_not_make_a_check_applicable(self):
        result = await self.evaluate(
            ("not_applicable",) * 3, output="success: Open Monday"
        )
        self.assertEqual(
            result["results"]["knowledge_available"]["status"], "not_applicable"
        )
