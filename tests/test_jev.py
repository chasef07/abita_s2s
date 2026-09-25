"""Exercise the real evaluator through a synthetic HTTP boundary."""

import json
import unittest
from unittest.mock import patch

import httpx
from livekit.agents import ChatContext
from livekit.agents.llm import AgentConfigUpdate, FunctionCall, FunctionCallOutput

from abita_s2s.observability.jev import evaluate_call


class JevTests(unittest.IsolatedAsyncioTestCase):
    async def run_evaluation(self, behavior=None, values=None):
        history = ChatContext()
        history.items.append(
            AgentConfigUpdate(
                instructions="Office timezone: America/Chicago. Use office rules."
            )
        )
        history.add_message(
            role="user", content="Move Tuesday at 9 to Wednesday at 10."
        )
        history.items.extend(
            [
                FunctionCall(
                    call_id="move-1",
                    name="reschedule_appt",
                    arguments='{"time":"10:00"}',
                ),
                FunctionCallOutput(
                    call_id="move-1",
                    name="reschedule_appt",
                    output="Timed out",
                    is_error=True,
                ),
            ]
        )
        history.add_message(role="assistant", content="It is rescheduled.")
        history.add_message(
            role="user", content="This is frustrating. Please get a person."
        )
        history.add_message(role="user", content="Thanks.")
        requests = []

        async def respond(request):
            self.assertEqual(
                str(request.url), "https://ai-gateway.vercel.sh/typesafe/v1/systemone"
            )
            payload = json.loads(request.content)
            requests.append(payload)
            name, question = next(iter(payload["questions"].items()))
            attempt = sum(name in p["questions"] for p in requests)
            if behavior:
                response = await behavior(name, attempt, request)
                if response is not None:
                    return response
            if question["type"] == "noul":
                answer = {"type": "noul", "noul": (values or {}).get(name, 0.9)}
            else:
                answer = {
                    "type": "score",
                    "score": 2,
                    "probabilities": {str(i): int(i == 2) for i in range(5)},
                }
            return httpx.Response(
                200, json={"answers": {name: answer}, "usage": {"inputTokens": 100}}
            )

        client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
        with (
            patch.dict("os.environ", {"AI_GATEWAY_API_KEY": "synthetic-secret"}),
            patch("abita_s2s.observability.jev.httpx.AsyncClient", return_value=client),
            patch("abita_s2s.observability.jev.EVALUATION_SECONDS", 0.1),
            patch("abita_s2s.observability.jev.RETRY_SECONDS", 0),
        ):
            result = await evaluate_call({"chat_history": history.to_dict()})
        return requests, result

    async def test_seven_judges_receive_full_history_and_return_decisions(self):
        requests, result = await self.run_evaluation(
            values={
                "appointment_datetime_correct": 0.1,
                "conversation_responsive": 0.05,
            }
        )
        self.assertEqual(len(requests), 7)
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["evaluatorVersion"], "typesafe-scorecard-v2")
        self.assertEqual(
            set(result["results"]),
            {
                "request_understood",
                "appointment_datetime_correct",
                "office_rules_grounded",
                "results_reported_truthfully",
                "resolved_or_handed_off",
                "conversation_responsive",
                "expressed_sentiment",
            },
        )
        answer = result["results"]["appointment_datetime_correct"]["answers"][
            "appointment_datetime_correct"
        ]
        self.assertEqual(answer, {"type": "noul", "noul": 0.1})
        self.assertEqual(
            result["results"]["conversation_responsive"]["answers"][
                "conversation_responsive"
            ],
            {"type": "noul", "noul": 0.05},
        )
        for request in requests:
            self.assertEqual(
                request["state"]["conversation"], requests[0]["state"]["conversation"]
            )
        items = requests[0]["state"]["conversation"]
        self.assertTrue(items[3]["is_error"])
        self.assertEqual(items[2]["call_id"], items[3]["call_id"])
        self.assertIn("created_at", items[-1])
        sentiment = result["results"]["expressed_sentiment"]
        self.assertEqual(sentiment["answers"]["expressed_sentiment"]["score"], 2)
        self.assertEqual(sentiment["usage"]["inputTokens"], 100)

    async def test_invalid_or_missing_answers_do_not_erase_other_results(self):
        for answer in [
            None,
            {},
            {"type": "boolean", "probability": 0.9},
            {"type": "noul", "noul": True},
            {"type": "noul", "noul": 1.5},
            {"type": "noul", "noul": float("nan")},
        ]:

            async def behavior(name, attempt, request):
                if name == "request_understood":
                    body = {"answers": {} if answer is None else {name: answer}}
                    return httpx.Response(200, content=json.dumps(body).encode())

            with (
                self.subTest(answer=answer),
                self.assertLogs("abita_s2s.observability.jev", "ERROR"),
            ):
                _, result = await self.run_evaluation(behavior)
                self.assertEqual(len(result["results"]), 6)
                self.assertNotIn("request_understood", result["results"])
                self.assertEqual(
                    result["errors"]["request_understood"]["cause"], "ValueError"
                )

    async def test_retry_after_cannot_extend_deadline(self):
        async def behavior(name, attempt, request):
            if name == "request_understood":
                return httpx.Response(429, headers={"Retry-After": "60"})

        with self.assertLogs("abita_s2s.observability.jev", "ERROR"):
            requests, result = await self.run_evaluation(behavior)
        self.assertEqual(len(requests), 7)
        self.assertEqual(result["errors"]["request_understood"]["httpStatus"], 429)
        self.assertEqual(len(result["results"]), 6)

    async def test_exhausted_http_and_transport_retries_remain_visible(self):
        for transport_failure in (False, True):

            async def behavior(name, attempt, request):
                if name == "request_understood":
                    if transport_failure:
                        raise httpx.ConnectError(
                            "synthetic-secret private details", request=request
                        )
                    return httpx.Response(503)

            with (
                self.subTest(transport=transport_failure),
                self.assertLogs("abita_s2s.observability.jev", "ERROR") as logs,
            ):
                requests, result = await self.run_evaluation(behavior)
            self.assertEqual(len(requests), 8)
            self.assertEqual(len(result["results"]), 6)
            error = result["errors"]["request_understood"]
            self.assertEqual(error["attempts"], 2)
            self.assertEqual(
                error["cause"],
                "ConnectError" if transport_failure else "HTTPStatusError",
            )
            self.assertNotIn("synthetic-secret", str(result) + str(logs.output))

    async def test_invalid_sentiment_preserves_all_six_checks(self):
        for answer in [
            {"type": "score", "score": 5, "probabilities": {"2": 1}},
            {"type": "score", "score": True, "probabilities": {"2": 1}},
            {"type": "score", "score": 2, "probabilities": {"2": -1}},
            {"type": "score", "score": 2},
        ]:

            async def behavior(name, attempt, request):
                if name == "expressed_sentiment":
                    return httpx.Response(200, json={"answers": {name: answer}})

            with (
                self.subTest(answer=answer),
                self.assertLogs("abita_s2s.observability.jev", "ERROR"),
            ):
                _, result = await self.run_evaluation(behavior)
            self.assertEqual(len(result["results"]), 6)
            self.assertEqual(
                result["errors"]["expressed_sentiment"]["cause"], "ValueError"
            )
