"""Exercise the real evaluator through a synthetic HTTP boundary."""

import json
import unittest
from unittest.mock import patch

import httpx
from livekit.agents import ChatContext
from livekit.agents.llm import AgentConfigUpdate, FunctionCall, FunctionCallOutput

from abita_s2s.observability.evaluation import evaluate_call, judge_state
from abita_s2s.prompt import load_prompt


class JevTests(unittest.IsolatedAsyncioTestCase):
    async def run_evaluation(
        self,
        behavior=None,
        values=None,
        *,
        tool="reschedule_appointment",
        returned=True,
        extra_tool=None,
        jurors=("typesafe/jev-1.13",),
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
                str(request.url), "https://openrouter.ai/api/alpha/decisions"
            )
            self.assertEqual(
                request.headers["Authorization"], "Bearer synthetic-secret"
            )
            payload = json.loads(request.content)
            requests.append(payload)
            juror = payload["model"]
            attempt = sum(p["model"] == juror for p in requests)
            answers = {}
            for name, question in payload["questions"].items():
                default = 0.9 if question["type"] == "noul" else 2.3
                value = (values or {}).get(name, default)
                if isinstance(value, dict):
                    value = value.get(juror, default)
                if question["type"] == "noul":
                    answers[name] = {"type": "noul", "noul": value}
                else:
                    answers[name] = {
                        "type": "score",
                        "score": value,
                        "probabilities": {str(i): int(i == 2) for i in range(5)},
                    }
            if behavior:
                response = await behavior(juror, attempt, request, answers)
                if response is not None:
                    return response
            return httpx.Response(
                200, json={"answers": answers, "usage": {"inputTokens": 100}}
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
                    self.assertEqual(len(requests), 1)
                    self.assertEqual(len(sent), 7 + datetime_applies + time_applies)
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

    async def test_each_juror_gets_every_question_in_one_request_with_zero_retention(
        self,
    ):
        requests, result = await self.run_evaluation(
            tool="list_available_appointments", extra_tool="book_appointment"
        )
        sent = {}
        for request in requests:
            self.assertEqual(request["model"], "typesafe/jev-1.13")
            self.assertEqual(request["provider"], {"zdr": True})
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
        self.assertEqual(len(requests), 1)
        self.assertEqual(result["evaluator"], "jury")
        self.assertEqual(result["evaluatorVersion"], "typesafe-scorecard-v6")
        self.assertEqual(result["jurors"], ["typesafe/jev-1.13"])
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["errors"], {})
        self.assertEqual(set(result["results"]), set(sent))
        for name, question in sent.items():
            if name != "expressed_sentiment":
                self.assertEqual(question["type"], "noul")
                self.assertEqual(set(question["criteria"]), {"true", "false"})

    async def test_judges_receive_full_history_and_return_verdicts(self):
        requests, result = await self.run_evaluation(
            values={
                "appointment_datetime_correct": 0.1,
                "clear_and_responsive": 0.4,
                "right_help": 0.41,
            }
        )
        self.assertEqual(len(requests), 1)
        self.assertEqual(len(requests[0]["questions"]), 8)
        self.assertEqual(result["status"], "complete")
        jev = "typesafe/jev-1.13"
        self.assertEqual(
            result["results"]["appointment_datetime_correct"],
            {"verdict": False, "probability": 0.1, "votes": {jev: 0.1}, "errors": {}},
        )
        self.assertFalse(result["results"]["clear_and_responsive"]["verdict"])
        self.assertTrue(result["results"]["right_help"]["verdict"])
        state = requests[0]["state"]
        for request in requests:
            self.assertEqual(request["state"], state)
        self.assertEqual(set(state), {"agent_instructions", "transcript"})
        self.assertEqual(
            state["transcript"],
            [
                "caller: Move Tuesday at 9 to Wednesday at 10.",
                'tool reschedule_appointment({"time":"10:00"}) -> ERROR: Timed out',
                "agent: It is rescheduled.",
                "caller: This is frustrating. Please get a person.",
                "caller: Thanks.",
            ],
        )
        self.assertEqual(
            result["results"]["expressed_sentiment"],
            {"score": 2.3, "votes": {jev: 2.3}, "errors": {}},
        )

    async def test_three_jurors_take_the_majority_and_average_sentiment(self):
        jurors = ("juror-a", "juror-b", "juror-c")
        requests, result = await self.run_evaluation(
            values={
                "need_understood": {"juror-a": 0.9, "juror-b": 0.2, "juror-c": 0.6},
                "right_help": {"juror-a": 0.95, "juror-b": 0.1, "juror-c": 0.3},
                "expressed_sentiment": {"juror-a": 1.0, "juror-b": 2.0, "juror-c": 3.0},
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
        self.assertEqual(
            result["results"]["expressed_sentiment"],
            {
                "score": 2.0,
                "votes": {"juror-a": 1.0, "juror-b": 2.0, "juror-c": 3.0},
                "errors": {},
            },
        )
        self.assertEqual(sorted(r["model"] for r in requests), list(jurors))

    async def test_two_of_three_answering_break_a_tie_on_mean_probability(self):
        async def behavior(juror, attempt, request, answers):
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
        async def behavior(juror, attempt, request, answers):
            if juror != "juror-a":
                del answers["need_understood"]

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
                    "errors": {"juror-b": "ValueError", "juror-c": "ValueError"},
                }
            },
        )

    async def test_invalid_or_missing_answers_do_not_erase_other_results(self):
        for answer in [
            None,
            {},
            {"type": "boolean", "probability": 0.9},
            {"type": "noul", "noul": True},
            {"type": "noul", "noul": 1.5},
            {"type": "noul", "noul": float("nan")},
            {"type": "noul"},
        ]:

            async def behavior(juror, attempt, request, answers):
                if answer is None:
                    del answers["office_rules_grounded"]
                else:
                    answers["office_rules_grounded"] = answer
                body = {"answers": answers}
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
                        "errors": {"typesafe/jev-1.13": "ValueError"},
                    },
                )

    async def test_retry_after_cannot_extend_deadline(self):
        async def behavior(juror, attempt, request, answers):
            return httpx.Response(429, headers={"Retry-After": "60"})

        with self.assertLogs("abita_s2s.observability.evaluation", "ERROR") as logs:
            requests, result = await self.run_evaluation(behavior)
        self.assertEqual(len(requests), 1)
        self.assertEqual(
            result["errors"]["office_rules_grounded"]["errors"],
            {"typesafe/jev-1.13": "TimeoutError"},
        )
        self.assertIn("http_status=429", str(logs.output))
        self.assertEqual(len(result["errors"]), 8)
        self.assertEqual(set(result["results"]), {"time_offered"})

    async def test_exhausted_http_and_transport_retries_remain_visible(self):
        for transport_failure in (False, True):

            async def behavior(juror, attempt, request, answers):
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
            self.assertEqual(len(requests), 2)
            self.assertEqual(set(result["results"]), {"time_offered"})
            cause = "ConnectError" if transport_failure else "HTTPStatusError"
            self.assertEqual(
                result["errors"]["office_rules_grounded"]["errors"],
                {"typesafe/jev-1.13": cause},
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

            async def behavior(juror, attempt, request, answers):
                answers["expressed_sentiment"] = answer

            with (
                self.subTest(answer=answer),
                self.assertLogs("abita_s2s.observability.evaluation", "ERROR"),
            ):
                _, result = await self.run_evaluation(behavior)
            self.assertEqual(len(result["results"]), 8)
            self.assertEqual(
                result["errors"]["expressed_sentiment"],
                {
                    "cause": "no_quorum",
                    "votes": {},
                    "errors": {"typesafe/jev-1.13": "ValueError"},
                },
            )


class JudgeStateTests(unittest.TestCase):
    def test_call_reads_as_lines_with_the_agents_rules_once(self):
        history = ChatContext()
        history.items.append(
            AgentConfigUpdate(instructions="Speaker prompt.", created_at=1_791_475_200)
        )
        history.add_message(role="user", content="Is Dr. Lee in Friday?")
        history.items.append(
            FunctionCall(
                call_id="k-1", name="search_office_knowledge", arguments='{"q":"Lee"}'
            )
        )
        history.add_message(role="assistant", content="One moment.", interrupted=True)
        history.items.append(
            FunctionCallOutput(
                call_id="k-1",
                name="search_office_knowledge",
                output="Dr. Lee: Mon-Thu",
                is_error=False,
            )
        )
        history.add_message(role="assistant", content="  ")
        history.items.append(
            FunctionCall(call_id="t-1", name="transfer_call", arguments="{}")
        )
        state = judge_state(history)
        self.assertEqual(
            state["transcript"],
            [
                "caller: Is Dr. Lee in Friday?",
                "agent: One moment. (interrupted)",
                'tool search_office_knowledge({"q":"Lee"}) -> Dr. Lee: Mon-Thu',
                "tool transfer_call({}) -> no result",
            ],
        )
        instructions = state["agent_instructions"]
        self.assertTrue(instructions.startswith(load_prompt("thinker")))
        self.assertIn(
            "Call started: Thursday, 2026-10-08 12:00 EDT (America/New_York)",
            instructions,
        )
        self.assertTrue(instructions.endswith(load_prompt("speaker")))
