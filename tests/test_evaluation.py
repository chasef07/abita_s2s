"""Exercise the real evaluator through a synthetic HTTP boundary."""

import json
import unittest
from unittest.mock import patch

import httpx
from livekit.agents import ChatContext
from livekit.agents.llm import AgentConfigUpdate, FunctionCall, FunctionCallOutput

from abita_s2s.observability.evaluation import evaluate_call, judge_state
from abita_s2s.observability.judges import JURORS
from abita_s2s.prompt import load_prompt


class JevTests(unittest.IsolatedAsyncioTestCase):
    async def run_evaluation(
        self,
        behavior=None,
        values=None,
        *,
        tool="reschedule_appointment",
        returned=True,
        extra_tools=(),
        action_output="success: The appointment was moved to Wednesday at 10:00.",
        action_error=False,
        knowledge_output="success: Office hours are Monday to Friday, 9 to 5.",
        jurors=("typesafe/jev-1.14",),
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
                    output=action_output,
                    is_error=action_error,
                ),
            ]
        )
        if not returned:
            history.items.pop()
        for extra_tool in extra_tools:
            history.items.extend(
                [
                    FunctionCall(call_id=extra_tool, name=extra_tool, arguments="{}"),
                    FunctionCallOutput(
                        call_id=extra_tool,
                        name=extra_tool,
                        output=knowledge_output
                        if extra_tool == "search_office_knowledge"
                        else action_output,
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
            if "intent" in payload["questions"]:
                return httpx.Response(200, json={"answers": {}})
            requests.append(payload)
            juror = payload["model"]
            attempt = sum(p["model"] == juror for p in requests)
            answers = {}
            for index, (name, question) in enumerate(payload["questions"].items()):
                default = 0.9 + index / 100
                value = (values or {}).get(name, default)
                if isinstance(value, dict):
                    value = value.get(juror, default)
                if question["type"] == "choice":
                    selected = "available" if value > 0.4 else "missing"
                    answers[name] = {
                        "type": "choice",
                        "choice": selected,
                        "probabilities": {
                            key: value
                            if key == "available"
                            else 1 - value
                            if key == "missing"
                            else 0
                            for key in question["criteria"]
                        },
                    }
                else:
                    answers[name] = {"type": "noul", "noul": value}
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

    async def test_configured_jury_uses_jev_perplexity_and_d1_ultra(self):
        requests, result = await self.run_evaluation(jurors=JURORS)
        expected = [
            "typesafe/jev-1.14",
            "perplexity/pplx-decider-v1.2",
            "liquid/d1-ultra",
        ]
        self.assertCountEqual([request["model"] for request in requests], expected)
        self.assertEqual(result["jurors"], expected)
        self.assertEqual(result["status"], "complete")
        self.assertEqual(
            set(result["results"]["transfer_policy_followed"]["votes"]), set(expected)
        )

    async def test_appointment_check_requires_a_returned_action_result(self):
        actions = {"book_appointment", "reschedule_appointment", "cancel_appointment"}
        for tool in (*actions, "list_available_appointments", "resolve_patient"):
            for returned in (False, True):
                with self.subTest(tool=tool, returned=returned):
                    requests, result = await self.run_evaluation(
                        tool=tool, returned=returned
                    )
                    asked = requests[0]["questions"]
                    self.assertEqual(
                        "appointment_datetime_correct" in asked,
                        returned and tool in actions,
                    )
                    if not (returned and tool in actions):
                        self.assertEqual(
                            result["results"]["appointment_datetime_correct"],
                            {
                                "status": "unknown"
                                if tool in actions
                                else "not_applicable",
                                "reason": "no_confirmed_appointment_action"
                                if tool in actions
                                else "no_appointment_action",
                            },
                        )
                    self.assertEqual(
                        result["results"]["knowledge_available"],
                        {
                            "status": "unknown",
                            "reason": "no_office_knowledge_result",
                            "choices": {"typesafe/jev-1.14": "available"},
                            "errors": {},
                        },
                    )

    async def test_appointment_dates_need_a_confirmed_action(self):
        for output, is_error, asked in (
            ("Timed out", True, False),
            ("blocked: The booking outcome could not be confirmed.", False, False),
            ("needs_input: That slot could not be booked.", False, False),
            ("success: Booked Wednesday at 10:00.", False, True),
            (
                "blocked: The new appointment is booked for Wednesday at 10:00, but cancellation of the old appointment requires staff reconciliation.",
                False,
                True,
            ),
        ):
            with self.subTest(output=output):
                requests, result = await self.run_evaluation(
                    action_output=output, action_error=is_error
                )
                self.assertEqual(
                    "appointment_datetime_correct" in requests[0]["questions"], asked
                )
                if not asked:
                    self.assertEqual(
                        result["results"]["appointment_datetime_correct"],
                        {
                            "status": "unknown",
                            "reason": "no_confirmed_appointment_action",
                        },
                    )

    async def test_knowledge_gap_requires_a_completed_search_not_an_outage(self):
        for output, graded in (
            ("success: Office hours are Monday to Friday, 9 to 5.", True),
            ("no_results: No information was found for this question.", True),
            ("blocked: Office information could not be checked.", False),
            ("needs_input: Provide a focused office question.", False),
        ):
            with self.subTest(output=output):
                requests, result = await self.run_evaluation(
                    extra_tools=("search_office_knowledge",),
                    knowledge_output=output,
                    values={"knowledge_available": 0.1},
                )
                self.assertIn("knowledge_available", requests[0]["questions"])
                if graded:
                    self.assertFalse(
                        result["results"]["knowledge_available"]["verdict"]
                    )
                else:
                    self.assertEqual(
                        result["results"]["knowledge_available"],
                        {
                            "status": "unknown",
                            "reason": "no_office_knowledge_result",
                            "choices": {"typesafe/jev-1.14": "missing"},
                            "errors": {},
                        },
                    )
                self.assertIn("office_question_answered", requests[0]["questions"])

    async def test_each_juror_gets_every_question_in_one_request_with_zero_retention(
        self,
    ):
        requests, result = await self.run_evaluation(
            tool="list_available_appointments",
            extra_tools=("book_appointment", "search_office_knowledge"),
        )
        sent = {}
        for request in requests:
            self.assertEqual(request["model"], "typesafe/jev-1.14")
            self.assertEqual(request["provider"], {"zdr": True})
            sent.update(request["questions"])
        self.assertEqual(
            set(sent),
            {
                "knowledge_available",
                "office_question_answered",
                "appointment_datetime_correct",
                "call_ended_correctly",
                "transfer_policy_followed",
                "emergency_policy_followed",
            },
        )
        self.assertEqual(len(requests), 1)
        self.assertEqual(result["evaluator"], "jury")
        self.assertEqual(result["evaluatorVersion"], "typesafe-scorecard-v8")
        self.assertEqual(result["jurors"], ["typesafe/jev-1.14"])
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["errors"], {})
        self.assertEqual(set(result["results"]), set(sent))
        for name, question in sent.items():
            if name == "knowledge_available":
                self.assertEqual(question["type"], "choice")
                self.assertEqual(
                    set(question["criteria"]),
                    {"available", "missing", "not_applicable", "unknown"},
                )
            else:
                self.assertEqual(question["type"], "noul")
                self.assertEqual(set(question["criteria"]), {"true", "false"})

    async def test_judges_receive_full_history_and_return_verdicts(self):
        requests, result = await self.run_evaluation(
            values={
                "appointment_datetime_correct": 0.1,
                "transfer_policy_followed": 0.4,
                "call_ended_correctly": 0.41,
            }
        )
        self.assertEqual(len(requests), 1)
        self.assertEqual(len(requests[0]["questions"]), 6)
        self.assertEqual(result["status"], "complete")
        jev = "typesafe/jev-1.14"
        self.assertEqual(
            result["results"]["appointment_datetime_correct"],
            {"verdict": False, "probability": 0.1, "votes": {jev: 0.1}, "errors": {}},
        )
        self.assertFalse(result["results"]["transfer_policy_followed"]["verdict"])
        self.assertTrue(result["results"]["call_ended_correctly"]["verdict"])
        state = requests[0]["state"]
        for request in requests:
            self.assertEqual(request["state"], state)
        self.assertEqual(set(state), {"agent_instructions", "transcript"})
        self.assertEqual(
            state["transcript"],
            [
                "caller: Move Tuesday at 9 to Wednesday at 10.",
                'tool reschedule_appointment({"time":"10:00"}) -> success: The appointment was moved to Wednesday at 10:00.',
                "agent: It is rescheduled.",
                "caller: This is frustrating. Please get a person.",
                "caller: Thanks.",
            ],
        )

    async def test_three_jurors_take_the_majority(self):
        jurors = ("juror-a", "juror-b", "juror-c")
        requests, result = await self.run_evaluation(
            values={
                "office_question_answered": {
                    "juror-a": 0.9,
                    "juror-b": 0.2,
                    "juror-c": 0.6,
                },
                "call_ended_correctly": {
                    "juror-a": 0.95,
                    "juror-b": 0.1,
                    "juror-c": 0.3,
                },
            },
            jurors=jurors,
        )
        self.assertEqual(result["jurors"], list(jurors))
        self.assertEqual(result["status"], "complete")
        need = result["results"]["office_question_answered"]
        self.assertTrue(need["verdict"])
        self.assertAlmostEqual(need["probability"], 1.7 / 3)
        self.assertEqual(
            need["votes"], {"juror-a": 0.9, "juror-b": 0.2, "juror-c": 0.6}
        )
        right = result["results"]["call_ended_correctly"]
        self.assertFalse(right["verdict"])
        self.assertAlmostEqual(right["probability"], 1.35 / 3)
        self.assertEqual(sorted(r["model"] for r in requests), list(jurors))

    async def test_two_of_three_answering_break_a_tie_on_mean_probability(self):
        async def behavior(juror, attempt, request, answers):
            if juror == "juror-c":
                return httpx.Response(400)

        with self.assertLogs("abita_s2s.observability.evaluation", "ERROR") as logs:
            _, result = await self.run_evaluation(
                behavior,
                values={
                    "office_question_answered": {"juror-a": 0.9, "juror-b": 0.3},
                    "call_ended_correctly": {"juror-a": 0.45, "juror-b": 0.1},
                },
                jurors=("juror-a", "juror-b", "juror-c"),
            )
        self.assertEqual(result["status"], "complete")
        need = result["results"]["office_question_answered"]
        self.assertTrue(need["verdict"])
        self.assertAlmostEqual(need["probability"], 0.6)
        self.assertEqual(need["errors"], {"juror-c": "HTTPStatusError"})
        right = result["results"]["call_ended_correctly"]
        self.assertFalse(right["verdict"])
        self.assertAlmostEqual(right["probability"], 0.275)
        self.assertNotIn("Thanks", str(logs.output))

    async def test_identical_valid_answers_remain_votes(self):
        names = (
            "knowledge_available",
            "office_question_answered",
            "appointment_datetime_correct",
            "call_ended_correctly",
            "transfer_policy_followed",
            "emergency_policy_followed",
        )
        for tool, extra_tools, count in (
            ("list_available_appointments", (), 5),
            ("reschedule_appointment", ("search_office_knowledge",), 6),
        ):
            for value in (0.0, 0.5, 1.0):
                with self.subTest(tool=tool, value=value):
                    requests, result = await self.run_evaluation(
                        values=dict.fromkeys(names, value),
                        tool=tool,
                        extra_tools=extra_tools,
                        jurors=JURORS,
                    )
                    self.assertEqual(result["status"], "complete")
                    self.assertEqual(result["errors"], {})
                    self.assertEqual(len(requests[0]["questions"]), count)
                    for name in requests[0]["questions"]:
                        answer = result["results"][name]
                        if name == "knowledge_available" and not extra_tools:
                            self.assertEqual(answer["status"], "unknown")
                            continue
                        self.assertEqual(answer["verdict"], value > 0.4)
                        self.assertAlmostEqual(answer["probability"], value)
                        self.assertEqual(answer["votes"], dict.fromkeys(JURORS, value))
                        self.assertEqual(answer["errors"], {})

    async def test_without_quorum_a_question_has_no_verdict(self):
        async def behavior(juror, attempt, request, answers):
            if juror != "juror-a":
                del answers["office_question_answered"]

        with self.assertLogs("abita_s2s.observability.evaluation", "ERROR"):
            _, result = await self.run_evaluation(
                behavior,
                values={"office_question_answered": {"juror-a": 0.8}},
                jurors=("juror-a", "juror-b", "juror-c"),
            )
        self.assertEqual(result["status"], "incomplete")
        self.assertNotIn("office_question_answered", result["results"])
        self.assertEqual(
            result["errors"],
            {
                "office_question_answered": {
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
                    del answers["emergency_policy_followed"]
                else:
                    answers["emergency_policy_followed"] = answer
                body = {"answers": answers}
                return httpx.Response(200, content=json.dumps(body).encode())

            with (
                self.subTest(answer=answer),
                self.assertLogs("abita_s2s.observability.evaluation", "ERROR"),
            ):
                _, result = await self.run_evaluation(behavior)
                self.assertEqual(len(result["results"]), 5)
                self.assertNotIn("emergency_policy_followed", result["results"])
                self.assertEqual(
                    result["errors"]["emergency_policy_followed"],
                    {
                        "cause": "no_quorum",
                        "votes": {},
                        "errors": {"typesafe/jev-1.14": "ValueError"},
                    },
                )

    async def test_retry_after_cannot_extend_deadline(self):
        async def behavior(juror, attempt, request, answers):
            return httpx.Response(429, headers={"Retry-After": "60"})

        with self.assertLogs("abita_s2s.observability.evaluation", "ERROR") as logs:
            requests, result = await self.run_evaluation(behavior)
        self.assertEqual(len(requests), 1)
        self.assertEqual(
            result["errors"]["emergency_policy_followed"]["errors"],
            {"typesafe/jev-1.14": "TimeoutError"},
        )
        self.assertIn("http_status=429", str(logs.output))
        self.assertEqual(len(result["errors"]), 6)
        self.assertEqual(set(result["results"]), set())

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
            self.assertEqual(set(result["results"]), set())
            cause = "ConnectError" if transport_failure else "HTTPStatusError"
            self.assertEqual(
                result["errors"]["emergency_policy_followed"]["errors"],
                {"typesafe/jev-1.14": cause},
            )
            self.assertIn("attempts=2", str(logs.output))
            self.assertNotIn("synthetic-secret", str(result) + str(logs.output))


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
