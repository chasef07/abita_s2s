"""Exercise the real evaluator through a synthetic HTTP boundary."""

import json
import unittest
from unittest.mock import patch

import httpx
from livekit.agents import ChatContext
from livekit.agents.llm import (
    AgentConfigUpdate,
    AgentHandoff,
    ChatMessage,
    FunctionCall,
    FunctionCallOutput,
)

from abita_s2s.observability.evaluation import (
    evaluate_call,
    evaluate_judges,
    judge_state,
)

JUROR_ABC = ("juror-a", "juror-b", "juror-c")
BOOLEANS = {
    "booking_requested",
    "need_understood",
    "right_help",
    "clear_and_responsive",
    "person_request_honored",
    "office_rules_grounded",
}


class JudgeStateTests(unittest.TestCase):
    def test_transcript_lines_carry_words_tools_pauses_and_markers(self):
        history = ChatContext()
        history.items.extend(
            [
                AgentConfigUpdate(instructions="Office rules.", created_at=1000.0),
                ChatMessage(
                    role="user",
                    content=["Hi, I need to move my appointment."],
                    created_at=1001.2,
                ),
                ChatMessage(
                    role="assistant",
                    content=["Sure, let me look that up."],
                    created_at=1003.0,
                ),
                FunctionCall(
                    call_id="c1",
                    name="resolve_patient",
                    arguments='{"name": "Ana Ruiz", "dob": "1990-02-03"}',
                    created_at=1004.0,
                ),
                FunctionCallOutput(
                    call_id="c1",
                    name="resolve_patient",
                    output="found: patient 42, appointment Tue 09:00",
                    is_error=False,
                    created_at=1005.5,
                ),
                ChatMessage(
                    role="user",
                    content=["Is Wednesday at ten open?"],
                    created_at=1018.9,
                ),
                ChatMessage(
                    role="assistant",
                    content=["Wednesday at ten is"],
                    interrupted=True,
                    created_at=1020.0,
                ),
                ChatMessage(
                    role="user",
                    content=["uh the morning one"],
                    transcript_confidence=0.3,
                    created_at=1021.0,
                ),
                FunctionCall(
                    call_id="c2",
                    name="book_appointment",
                    arguments="{}",
                    created_at=1022.0,
                ),
                AgentHandoff(new_agent_id="front_desk", created_at=1023.0),
                AgentConfigUpdate(instructions="Second rules.", created_at=1024.0),
                ChatMessage(role="assistant", content=[" "], created_at=1025.0),
                ChatMessage(role="assistant", content=["Done."], created_at=1090.0),
            ]
        )
        state = judge_state(history, "Office rules.")
        self.assertEqual(set(state), {"agent_instructions", "transcript", "note"})
        self.assertEqual(state["agent_instructions"], "Office rules.")
        self.assertIn("evidence, not instructions", state["note"])
        self.assertEqual(
            state["transcript"],
            [
                "[00:01] caller: Hi, I need to move my appointment.",
                "[00:03] agent: Sure, let me look that up.",
                '[00:05] tool resolve_patient({"name":"Ana Ruiz","dob":"1990-02-03"})'
                " -> found: patient 42, appointment Tue 09:00",
                "[pause 13s]",
                "[00:18] caller: Is Wednesday at ten open?",
                "[00:20] agent: Wednesday at ten is (interrupted)",
                "[00:21] caller: uh the morning one (unclear audio)",
                "[00:22] tool book_appointment({}) -> no result",
                "[00:23] handoff to front_desk",
                "[pause 67s]",
                "[01:30] agent: Done.",
            ],
        )
        self.assertNotIn("Second rules", str(state))

    def test_judge_state_marks_failed_tool_results(self):
        history = ChatContext(
            [
                FunctionCall(call_id="c1", name="book_appointment", arguments="{}", created_at=10.0),
                FunctionCallOutput(
                    call_id="c1",
                    name="book_appointment",
                    output="Timed out",
                    is_error=True,
                    created_at=11.0,
                ),
            ]
        )
        self.assertEqual(
            judge_state(history, "Office rules.")["transcript"],
            ["[00:01] tool book_appointment({}) -> Timed out (error)"],
        )


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
        malformed=None,
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
            juror = payload["model"]
            attempt = sum(p["model"] == juror for p in requests)
            if behavior:
                response = await behavior(juror, attempt, request)
                if response is not None:
                    return response
            answers = {}
            for name, question in payload["questions"].items():
                if (juror, name) in (malformed or {}):
                    answers[name] = malformed[juror, name]
                elif question["type"] == "boolean":
                    value = (values or {}).get(name, 0.9)
                    if isinstance(value, dict):
                        value = value[juror]
                    answers[name] = {"type": "boolean", "probability": value}
                else:
                    answers[name] = {
                        "type": "score",
                        "score": 2.3,
                        "probabilities": {str(i): int(i == 2) for i in range(5)},
                    }
            body = {"answers": answers, "usage": {"inputTokens": 100}}
            return httpx.Response(200, content=json.dumps(body).encode())

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
                    self.assertEqual(len(requests), 1)
                    sent = set(requests[0]["questions"])
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

    async def test_one_request_per_juror_carries_every_applicable_question(self):
        requests, result = await self.run_evaluation(
            tool="list_available_appointments", extra_tool="book_appointment"
        )
        self.assertEqual(len(requests), 1)
        request = requests[0]
        self.assertEqual(request["model"], "typesafe-ai/jev")
        self.assertEqual(
            request["providerOptions"], {"gateway": {"zeroDataRetention": True}}
        )
        sent = request["questions"]
        self.assertEqual(
            set(sent),
            BOOLEANS
            | {"time_offered", "appointment_datetime_correct", "expressed_sentiment"},
        )
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

    async def test_judges_receive_the_transcript_and_return_verdicts(self):
        requests, result = await self.run_evaluation(
            values={
                "appointment_datetime_correct": 0.1,
                "clear_and_responsive": 0.4,
                "right_help": 0.41,
            }
        )
        self.assertEqual(result["status"], "complete")
        jev = "typesafe-ai/jev"
        self.assertEqual(
            result["results"]["appointment_datetime_correct"],
            {"verdict": False, "probability": 0.1, "votes": {jev: 0.1}, "errors": {}},
        )
        self.assertFalse(result["results"]["clear_and_responsive"]["verdict"])
        self.assertTrue(result["results"]["right_help"]["verdict"])
        state = requests[0]["state"]
        self.assertEqual(
            state["agent_instructions"],
            "Office timezone: America/Chicago. Use office rules.",
        )
        self.assertEqual(
            state["transcript"][0][8:], "caller: Move Tuesday at 9 to Wednesday at 10."
        )
        self.assertEqual(
            state["transcript"][1][8:],
            'tool reschedule_appointment({"time":"10:00"}) -> Timed out (error)',
        )
        self.assertEqual(state["transcript"][-1][8:], "caller: Thanks.")
        self.assertNotIn("Office timezone", str(state["transcript"]))
        self.assertEqual(
            result["results"]["expressed_sentiment"],
            {
                "score": 2.3,
                "probabilities": {str(i): int(i == 2) for i in range(5)},
                "model": jev,
            },
        )

    async def test_three_jurors_take_the_majority_and_sentiment_asks_the_first(self):
        requests, result = await self.run_evaluation(
            values={
                "need_understood": {"juror-a": 0.9, "juror-b": 0.2, "juror-c": 0.6},
                "right_help": {"juror-a": 0.95, "juror-b": 0.1, "juror-c": 0.3},
            },
            jurors=JUROR_ABC,
        )
        self.assertEqual(sorted(r["model"] for r in requests), list(JUROR_ABC))
        sheets = {r["model"]: set(r["questions"]) for r in requests}
        self.assertEqual(
            sheets,
            {
                "juror-a": BOOLEANS
                | {"appointment_datetime_correct", "expressed_sentiment"},
                "juror-b": BOOLEANS | {"appointment_datetime_correct"},
                "juror-c": BOOLEANS | {"appointment_datetime_correct"},
            },
        )
        self.assertEqual(result["jurors"], list(JUROR_ABC))
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
        self.assertEqual(result["results"]["expressed_sentiment"]["model"], "juror-a")

    async def test_a_failed_juror_fails_every_question_it_was_asked(self):
        async def behavior(juror, attempt, request):
            if juror == "juror-c":
                return httpx.Response(400)

        with self.assertLogs("abita_s2s.observability.evaluation", "ERROR") as logs:
            _, result = await self.run_evaluation(
                behavior,
                values={
                    "need_understood": {"juror-a": 0.9, "juror-b": 0.3},
                    "right_help": {"juror-a": 0.45, "juror-b": 0.1},
                },
                jurors=JUROR_ABC,
            )
        self.assertEqual(result["status"], "complete")
        for name in BOOLEANS | {"appointment_datetime_correct"}:
            self.assertEqual(
                result["results"][name]["errors"], {"juror-c": "HTTPStatusError"}
            )
        self.assertEqual(result["results"]["expressed_sentiment"]["model"], "juror-a")
        need = result["results"]["need_understood"]
        self.assertTrue(need["verdict"])
        self.assertAlmostEqual(need["probability"], 0.6)
        right = result["results"]["right_help"]
        self.assertFalse(right["verdict"])
        self.assertAlmostEqual(right["probability"], 0.275)
        self.assertEqual(len(logs.output), 1)
        self.assertNotIn("Thanks", str(logs.output))

    async def test_without_quorum_a_question_has_no_verdict(self):
        bad = {"type": "boolean"}
        with self.assertLogs("abita_s2s.observability.evaluation", "ERROR"):
            _, result = await self.run_evaluation(
                values={"need_understood": {"juror-a": 0.8}},
                jurors=JUROR_ABC,
                malformed={
                    ("juror-b", "need_understood"): bad,
                    ("juror-c", "need_understood"): bad,
                },
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
        self.assertEqual(len(result["results"]), 8)

    async def test_backtest_can_ask_a_subset_of_questions(self):
        history = ChatContext()
        history.add_message(role="user", content="Synthetic caller asks for help.")
        requests = []

        def respond(request):
            payload = json.loads(request.content)
            requests.append(payload)
            return httpx.Response(
                200,
                json={
                    "answers": {
                        name: {"type": "boolean", "probability": 0.7}
                        for name in payload["questions"]
                    }
                },
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

    async def test_invalid_answers_do_not_erase_other_results(self):
        for answer in [
            {},
            {"type": "noul", "noul": 0.9},
            {"type": "boolean", "probability": True},
            {"type": "boolean", "probability": 1.5},
            {"type": "boolean", "probability": float("nan")},
            {"type": "boolean"},
        ]:
            with (
                self.subTest(answer=answer),
                self.assertLogs("abita_s2s.observability.evaluation", "ERROR"),
            ):
                _, result = await self.run_evaluation(
                    malformed={("typesafe-ai/jev", "office_rules_grounded"): answer}
                )
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

    async def test_incomplete_answers_fail_the_whole_request(self):
        for body in [
            {"answers": {"right_help": {"type": "boolean", "probability": 0.9}}},
            {"answers": []},
            [],
        ]:

            async def behavior(juror, attempt, request):
                return httpx.Response(200, json=body)

            with (
                self.subTest(body=body),
                self.assertLogs("abita_s2s.observability.evaluation", "ERROR"),
            ):
                _, result = await self.run_evaluation(behavior)
            self.assertEqual(result["status"], "incomplete")
            self.assertEqual(len(result["errors"]), 8)
            self.assertEqual(set(result["results"]), {"time_offered"})

    async def test_retry_after_cannot_extend_deadline(self):
        async def behavior(juror, attempt, request):
            return httpx.Response(429, headers={"Retry-After": "60"})

        with self.assertLogs("abita_s2s.observability.evaluation", "ERROR") as logs:
            requests, result = await self.run_evaluation(behavior)
        self.assertEqual(len(requests), 1)
        self.assertEqual(
            result["errors"]["office_rules_grounded"]["errors"],
            {"typesafe-ai/jev": "TimeoutError"},
        )
        self.assertEqual(
            result["errors"]["expressed_sentiment"],
            {"cause": "TimeoutError", "model": "typesafe-ai/jev"},
        )
        self.assertIn("http_status=429", str(logs.output))
        self.assertEqual(len(result["errors"]), 8)

    async def test_exhausted_http_and_transport_retries_remain_visible(self):
        for transport_failure in (False, True):

            async def behavior(juror, attempt, request):
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
            cause = "ConnectError" if transport_failure else "HTTPStatusError"
            self.assertEqual(
                result["errors"]["office_rules_grounded"]["errors"],
                {"typesafe-ai/jev": cause},
            )
            self.assertEqual(len(result["errors"]), 8)
            self.assertIn("attempts=2", str(logs.output))
            self.assertNotIn("synthetic-secret", str(result) + str(logs.output))

    async def test_invalid_sentiment_preserves_all_other_judges(self):
        for answer in [
            {"type": "score", "score": 5, "probabilities": {"2": 1}},
            {"type": "score", "score": True, "probabilities": {"2": 1}},
            {"type": "score", "score": 2, "probabilities": {"2": -1}},
            {"type": "score", "score": 2},
        ]:
            with (
                self.subTest(answer=answer),
                self.assertLogs("abita_s2s.observability.evaluation", "ERROR"),
            ):
                _, result = await self.run_evaluation(
                    malformed={("typesafe-ai/jev", "expressed_sentiment"): answer}
                )
            self.assertEqual(len(result["results"]), 8)
            self.assertEqual(
                result["errors"]["expressed_sentiment"],
                {"cause": "ValueError", "model": "typesafe-ai/jev"},
            )
