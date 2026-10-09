"""Exercise the real evaluator through a synthetic HTTP boundary."""

import json
import unittest
from unittest.mock import patch

import httpx
from livekit.agents import ChatContext
from livekit.agents.llm import AgentConfigUpdate, FunctionCall, FunctionCallOutput

from abita_s2s.observability.evaluation import evaluate_call, evaluate_judges


class JevTests(unittest.IsolatedAsyncioTestCase):
    async def run_evaluation(
        self,
        behavior=None,
        values=None,
        *,
        tool="reschedule_appointment",
        returned=True,
        extra_tool=None,
        jurors=("typesafe-ai/jev",),
    ):
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
                    name=tool,
                    arguments='{"time":"10:00"}',
                ),
                FunctionCallOutput(
                    call_id="move-1",
                    name=tool,
                    output="Timed out",
                    is_error=True,
                ),
            ]
        )
        if not returned:
            history.items.pop()
        if extra_tool:
            history.items.extend(
                [
                    FunctionCall(call_id="extra-1", name=extra_tool, arguments="{}"),
                    FunctionCallOutput(
                        call_id="extra-1",
                        name=extra_tool,
                        output="success: done",
                        is_error=False,
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
                str(request.url), "https://ai-gateway.vercel.sh/v1/evaluate"
            )
            self.assertEqual(
                request.headers["Authorization"], "Bearer synthetic-secret"
            )
            payload = json.loads(request.content)
            requests.append(payload)
            name, question = next(iter(payload["questions"].items()))
            juror = payload["model"]
            attempt = sum(
                name in p["questions"] and p["model"] == juror for p in requests
            )
            if behavior:
                response = await behavior(name, juror, attempt, request)
                if response is not None:
                    return response
            if question["type"] == "boolean":
                value = (values or {}).get(name, 0.9)
                if isinstance(value, dict):
                    value = value[juror]
                answer = {"type": "boolean", "probability": value}
            else:
                answer = {
                    "type": "score",
                    "score": 2.3,
                    "probabilities": {str(i): int(i == 2) for i in range(5)},
                }
            return httpx.Response(
                200, json={"answers": {name: answer}, "usage": {"inputTokens": 100}}
            )

        client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
        with (
            patch(
                "abita_s2s.observability.evaluation.httpx.AsyncClient",
                return_value=client,
            ),
            patch("abita_s2s.observability.evaluation.JURORS", jurors),
            patch("abita_s2s.observability.evaluation.EVALUATION_SECONDS", 0.1),
            patch("abita_s2s.observability.evaluation.RETRY_SECONDS", 0),
        ):
            result = await evaluate_call(
                {"chat_history": history.to_dict()}, "synthetic-secret", 60
            )
        return requests, result

    async def test_gated_judges_require_their_returned_tool_results(self):
        for tool in (
            "book_appointment",
            "reschedule_appointment",
            "cancel_appointment",
            "list_available_appointments",
            "resolve_patient",
            "confirm_appointment",
        ):
            for returned in (False, True):
                with self.subTest(tool=tool, returned=returned):
                    requests, result = await self.run_evaluation(
                        tool=tool, returned=returned
                    )
                    sent = {name for r in requests for name in r["questions"]}
                    datetime_applies = returned and tool in {
                        "book_appointment",
                        "reschedule_appointment",
                        "cancel_appointment",
                    }
                    time_applies = returned and tool == "list_available_appointments"
                    self.assertEqual(
                        "appointment_datetime_correct" in sent, datetime_applies
                    )
                    self.assertEqual("time_offered" in sent, time_applies)
                    self.assertEqual(len(requests), 7 + datetime_applies + time_applies)
                    self.assertEqual(result["status"], "complete")
                    if not datetime_applies:
                        self.assertEqual(
                            result["results"]["appointment_datetime_correct"],
                            {
                                "status": "not_applicable",
                                "reason": "no_appointment_action_result",
                            },
                        )
                    if not time_applies:
                        self.assertEqual(
                            result["results"]["time_offered"],
                            {
                                "status": "not_applicable",
                                "reason": "no_availability_result",
                            },
                        )

    async def test_v6_sends_one_boolean_question_per_request_with_zero_retention(self):
        requests, result = await self.run_evaluation(
            tool="list_available_appointments", extra_tool="book_appointment"
        )
        sent = {}
        for request in requests:
            self.assertEqual(request["model"], "typesafe-ai/jev")
            self.assertEqual(
                request["providerOptions"], {"gateway": {"zeroDataRetention": True}}
            )
            self.assertEqual(len(request["questions"]), 1)
            sent.update(request["questions"])
        self.assertEqual(
            set(sent),
            {
                "booking_requested",
                "time_offered",
                "need_understood",
                "right_help",
                "clear_and_responsive",
                "person_request_honored",
                "office_rules_grounded",
                "appointment_datetime_correct",
                "expressed_sentiment",
            },
        )
        self.assertEqual(len(requests), 9)
        self.assertEqual(result["evaluator"], "jury")
        self.assertEqual(result["evaluatorVersion"], "typesafe-scorecard-v6")
        self.assertEqual(result["jurors"], ["typesafe-ai/jev"])
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["errors"], {})
        self.assertEqual(set(result["results"]), set(sent))
        for name, question in sent.items():
            if name != "expressed_sentiment":
                self.assertEqual(question["type"], "boolean")
                self.assertEqual(set(question["criteria"]), {"true", "false"})

    async def test_judges_receive_full_history_and_return_verdicts(self):
        requests, result = await self.run_evaluation(
            values={
                "appointment_datetime_correct": 0.1,
                "clear_and_responsive": 0.4,
                "right_help": 0.41,
            }
        )
        self.assertEqual(len(requests), 8)
        self.assertEqual(result["status"], "complete")
        jev = "typesafe-ai/jev"
        self.assertEqual(
            result["results"]["appointment_datetime_correct"],
            {"verdict": False, "probability": 0.1, "votes": {jev: 0.1}, "errors": {}},
        )
        self.assertFalse(result["results"]["clear_and_responsive"]["verdict"])
        self.assertTrue(result["results"]["right_help"]["verdict"])
        for request in requests:
            self.assertEqual(
                request["state"]["conversation"], requests[0]["state"]["conversation"]
            )
        items = requests[0]["state"]["conversation"]
        self.assertTrue(items[3]["is_error"])
        self.assertEqual(items[2]["call_id"], items[3]["call_id"])
        self.assertIn("created_at", items[-1])
        self.assertEqual(
            result["results"]["expressed_sentiment"],
            {
                "score": 2.3,
                "probabilities": {str(i): int(i == 2) for i in range(5)},
                "model": jev,
            },
        )

    async def test_three_jurors_take_the_majority_and_sentiment_asks_the_first(self):
        jurors = ("juror-a", "juror-b", "juror-c")
        requests, result = await self.run_evaluation(
            values={
                "need_understood": {"juror-a": 0.9, "juror-b": 0.2, "juror-c": 0.6},
                "right_help": {"juror-a": 0.95, "juror-b": 0.1, "juror-c": 0.3},
            },
            jurors=jurors,
        )
        self.assertEqual(result["jurors"], list(jurors))
        self.assertEqual(result["status"], "complete")
        need = result["results"]["need_understood"]
        self.assertTrue(need["verdict"])
        self.assertAlmostEqual(need["probability"], 1.7 / 3)
        self.assertEqual(
            need["votes"], {"juror-a": 0.9, "juror-b": 0.2, "juror-c": 0.6}
        )
        right = result["results"]["right_help"]
        self.assertFalse(right["verdict"])
        self.assertAlmostEqual(right["probability"], 1.35 / 3)
        sentiment = [
            r["model"] for r in requests if "expressed_sentiment" in r["questions"]
        ]
        self.assertEqual(sentiment, ["juror-a"])
        self.assertEqual(result["results"]["expressed_sentiment"]["model"], "juror-a")
        self.assertEqual(len(requests), 7 * 3 + 1)

    async def test_two_of_three_answering_break_a_tie_on_mean_probability(self):
        async def behavior(name, juror, attempt, request):
            if juror == "juror-c":
                return httpx.Response(400)

        with self.assertLogs("abita_s2s.observability.evaluation", "ERROR") as logs:
            _, result = await self.run_evaluation(
                behavior,
                values={
                    "need_understood": {"juror-a": 0.9, "juror-b": 0.3},
                    "right_help": {"juror-a": 0.45, "juror-b": 0.1},
                },
                jurors=("juror-a", "juror-b", "juror-c"),
            )
        self.assertEqual(result["status"], "complete")
        need = result["results"]["need_understood"]
        self.assertTrue(need["verdict"])
        self.assertAlmostEqual(need["probability"], 0.6)
        self.assertEqual(need["errors"], {"juror-c": "HTTPStatusError"})
        right = result["results"]["right_help"]
        self.assertFalse(right["verdict"])
        self.assertAlmostEqual(right["probability"], 0.275)
        self.assertNotIn("Thanks", str(logs.output))

    async def test_without_quorum_a_question_has_no_verdict(self):
        async def behavior(name, juror, attempt, request):
            if name == "need_understood" and juror != "juror-a":
                return httpx.Response(400)

        with self.assertLogs("abita_s2s.observability.evaluation", "ERROR"):
            _, result = await self.run_evaluation(
                behavior,
                values={"need_understood": {"juror-a": 0.8}},
                jurors=("juror-a", "juror-b", "juror-c"),
            )
        self.assertEqual(result["status"], "incomplete")
        self.assertNotIn("need_understood", result["results"])
        self.assertEqual(
            result["errors"],
            {
                "need_understood": {
                    "cause": "no_quorum",
                    "votes": {"juror-a": 0.8},
                    "errors": {
                        "juror-b": "HTTPStatusError",
                        "juror-c": "HTTPStatusError",
                    },
                }
            },
        )

    async def test_backtest_can_ask_a_subset_of_questions(self):
        history = ChatContext()
        history.add_message(role="user", content="Synthetic caller asks for help.")
        requests = []

        def respond(request):
            payload = json.loads(request.content)
            requests.append(payload)
            name = next(iter(payload["questions"]))
            return httpx.Response(
                200,
                json={"answers": {name: {"type": "boolean", "probability": 0.7}}},
            )

        client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
        with patch(
            "abita_s2s.observability.evaluation.httpx.AsyncClient",
            return_value=client,
        ):
            result = await evaluate_judges(
                history,
                agent_purpose="Synthetic purpose.",
                api_key="synthetic-secret",
                questions=["right_help", "time_offered"],
                jurors=["juror-a", "juror-b"],
            )
        self.assertEqual(
            sorted((r["model"], *r["questions"]) for r in requests),
            [("juror-a", "right_help"), ("juror-b", "right_help")],
        )
        self.assertEqual(
            result["results"]["time_offered"],
            {"status": "not_applicable", "reason": "no_availability_result"},
        )
        self.assertTrue(result["results"]["right_help"]["verdict"])
        self.assertEqual(result["errors"], {})

    async def test_invalid_or_missing_answers_do_not_erase_other_results(self):
        for answer in [
            None,
            {},
            {"type": "noul", "noul": 0.9},
            {"type": "boolean", "probability": True},
            {"type": "boolean", "probability": 1.5},
            {"type": "boolean", "probability": float("nan")},
            {"type": "boolean"},
        ]:

            async def behavior(name, juror, attempt, request):
                if name == "office_rules_grounded":
                    body = {"answers": {} if answer is None else {name: answer}}
                    return httpx.Response(200, content=json.dumps(body).encode())

            with (
                self.subTest(answer=answer),
                self.assertLogs("abita_s2s.observability.evaluation", "ERROR"),
            ):
                _, result = await self.run_evaluation(behavior)
                self.assertEqual(len(result["results"]), 8)
                self.assertNotIn("office_rules_grounded", result["results"])
                self.assertEqual(
                    result["errors"]["office_rules_grounded"],
                    {
                        "cause": "no_quorum",
                        "votes": {},
                        "errors": {"typesafe-ai/jev": "ValueError"},
                    },
                )

    async def test_retry_after_cannot_extend_deadline(self):
        async def behavior(name, juror, attempt, request):
            if name == "office_rules_grounded":
                return httpx.Response(429, headers={"Retry-After": "60"})

        with self.assertLogs("abita_s2s.observability.evaluation", "ERROR") as logs:
            requests, result = await self.run_evaluation(behavior)
        self.assertEqual(len(requests), 8)
        self.assertEqual(
            result["errors"]["office_rules_grounded"]["errors"],
            {"typesafe-ai/jev": "TimeoutError"},
        )
        self.assertIn("http_status=429", str(logs.output))
        self.assertEqual(len(result["results"]), 8)

    async def test_exhausted_http_and_transport_retries_remain_visible(self):
        for transport_failure in (False, True):

            async def behavior(name, juror, attempt, request):
                if name == "office_rules_grounded":
                    if transport_failure:
                        raise httpx.ConnectError(
                            "synthetic-secret private details", request=request
                        )
                    return httpx.Response(503)

            with (
                self.subTest(transport=transport_failure),
                self.assertLogs("abita_s2s.observability.evaluation", "ERROR") as logs,
            ):
                requests, result = await self.run_evaluation(behavior)
            self.assertEqual(len(requests), 9)
            self.assertEqual(len(result["results"]), 8)
            cause = "ConnectError" if transport_failure else "HTTPStatusError"
            self.assertEqual(
                result["errors"]["office_rules_grounded"]["errors"],
                {"typesafe-ai/jev": cause},
            )
            self.assertIn("attempts=2", str(logs.output))
            self.assertNotIn("synthetic-secret", str(result) + str(logs.output))

    async def test_invalid_sentiment_preserves_all_other_judges(self):
        for answer in [
            {"type": "score", "score": 5, "probabilities": {"2": 1}},
            {"type": "score", "score": True, "probabilities": {"2": 1}},
            {"type": "score", "score": 2, "probabilities": {"2": -1}},
            {"type": "score", "score": 2},
        ]:

            async def behavior(name, juror, attempt, request):
                if name == "expressed_sentiment":
                    return httpx.Response(200, json={"answers": {name: answer}})

            with (
                self.subTest(answer=answer),
                self.assertLogs("abita_s2s.observability.evaluation", "ERROR"),
            ):
                _, result = await self.run_evaluation(behavior)
            self.assertEqual(len(result["results"]), 8)
            self.assertEqual(
                result["errors"]["expressed_sentiment"],
                {"cause": "ValueError", "model": "typesafe-ai/jev"},
            )
