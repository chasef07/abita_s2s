"""Exercise the real evaluator through a synthetic HTTP boundary."""

import json
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
from livekit.agents import ChatContext
from livekit.agents.llm import AgentConfigUpdate, FunctionCall, FunctionCallOutput

from abita_s2s.knowledge import UNAVAILABLE
from abita_s2s.observability import knowledge_gap
from abita_s2s.observability.decisions import parse, read_answers
from abita_s2s.observability.evaluation import (
    EVALUATOR_VERSION,
    PUBLISHED_VERSIONS,
    evaluate_call,
)
from abita_s2s.observability.judge_state import judge_state
from abita_s2s.observability.judges import (
    DEFINITIONS_HASH,
    GATES,
    JURORS,
    QUESTIONS,
    knowledge_available,
)
from abita_s2s.prompt import load_prompt

OFFICE_KNOWLEDGE = "Hours\nMonday to Friday, 9 to 5."
SOURCE = Path(__file__).parents[1] / "src"


def scratch_version(edit):
    with tempfile.TemporaryDirectory() as scratch:
        package = Path(scratch) / "abita_s2s"
        shutil.copytree(SOURCE / "abita_s2s", package)
        edit(package)
        return subprocess.run(
            [
                sys.executable,
                "-c",
                "from abita_s2s.observability.evaluation import EVALUATOR_VERSION;"
                "print(EVALUATOR_VERSION)",
            ],
            cwd=scratch,
            env={"PYTHONPATH": scratch},
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()


def confirmed(*outcomes):
    return {
        "transferStatus": "idle",
        "domainOutcomes": [{"outcome": outcome} for outcome in outcomes],
    }


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
        office_knowledge=OFFICE_KNOWLEDGE,
        jurors=("juror-a",),
        closeout=None,
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
                        output=action_output,
                        is_error=False,
                    ),
                ]
            )
        history.add_message(role="assistant", content="It is rescheduled.")
        history.add_message(
            role="user", content="This is frustrating. Please get a person."
        )
        history.add_message(role="user", content="Thanks.")
        history.items.extend(
            [
                FunctionCall(call_id="end-1", name="end_call", arguments="{}"),
                FunctionCallOutput(
                    call_id="end-1",
                    name="end_call",
                    output="Say a brief goodbye to the caller.",
                    is_error=False,
                ),
            ]
        )
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
                default = 2 if question["type"] == "score" else 0.9 + index / 100
                value = (values or {}).get(name, default)
                if isinstance(value, dict):
                    value = value.get(juror, default)
                if question["type"] == "choice":
                    pick = (values or {}).get(name, "none")
                    if isinstance(pick, dict):
                        pick = pick.get(juror, "none")
                    answers[name] = {
                        "type": "choice",
                        "choice": pick,
                        "probabilities": {
                            label: float(label == pick)
                            for label in question["criteria"]
                        },
                    }
                    continue
                answers[name] = (
                    {"type": "score", "score": value, "probabilities": {str(value): 1}}
                    if question["type"] == "score"
                    else {"type": "noul", "noul": value}
                )
            if behavior:
                response = await behavior(juror, attempt, request, answers)
                if response is not None:
                    return response
            return httpx.Response(
                200,
                json={
                    "answers": answers,
                    "model": f"{juror}-20261014",
                    "usage": {"inputTokens": 100},
                },
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
                {"chat_history": history.to_dict()},
                "synthetic-secret",
                60,
                office_knowledge=office_knowledge,
                closeout=confirmed("rescheduled") if closeout is None else closeout,
            )
        return requests, result

    async def test_sentiment_is_a_zero_to_four_mean_without_a_failure_verdict(self):
        jurors = ("juror-a", "juror-b", "juror-c")
        requests, result = await self.run_evaluation(
            values={"expressed_sentiment": dict(zip(jurors, (0, 2, 4)))},
            jurors=jurors,
        )
        question = requests[0]["questions"]["expressed_sentiment"]
        self.assertEqual(question["type"], "score")
        self.assertEqual(len(question["criteria"]), 5)
        self.assertEqual(
            result["results"]["expressed_sentiment"],
            {
                "score": 2,
                "votes": dict(zip(jurors, (0, 2, 4))),
                "errors": {},
            },
        )
        self.assertEqual(result["status"], "complete")

    async def test_invalid_sentiment_is_visible_without_erasing_failure_judges(self):
        for answer in (
            None,
            {},
            {"type": "noul", "noul": 0.8},
            *(
                {"type": "score", "score": value, "probabilities": {"2": 1}}
                for value in (-1, 5, True, "2")
            ),
            *(
                {"type": "score", "score": 2, "probabilities": probabilities}
                for probabilities in (
                    None,
                    {},
                    {"2": True},
                    {"2": -1},
                    {"2": 0.9},
                    {"2": 1, "5": 0},
                )
            ),
            {"type": "score", "score": 4, "probabilities": {"0": 1}},
        ):

            async def behavior(juror, attempt, request, answers):
                if juror != "juror-a":
                    answers["expressed_sentiment"] = answer
                return httpx.Response(
                    200, content=json.dumps({"answers": answers}).encode()
                )

            with (
                self.subTest(answer=answer),
                self.assertLogs("abita_s2s.observability.evaluation", "ERROR"),
            ):
                _, result = await self.run_evaluation(
                    behavior, jurors=("juror-a", "juror-b", "juror-c")
                )
            self.assertEqual(result["status"], "incomplete")
            self.assertNotIn("expressed_sentiment", result["results"])
            self.assertEqual(
                result["errors"]["expressed_sentiment"],
                {
                    "cause": "no_quorum",
                    "votes": {"juror-a": 2},
                    "errors": {"juror-b": "ValueError", "juror-c": "ValueError"},
                },
            )
            self.assertTrue(result["results"]["transfer_policy_followed"]["verdict"])

    async def test_sentiment_uses_valid_quorum_when_one_juror_fails(self):
        async def behavior(juror, attempt, request, answers):
            if juror == "juror-c":
                answers.pop("expressed_sentiment", None)

        with self.assertLogs("abita_s2s.observability.evaluation", "ERROR"):
            _, result = await self.run_evaluation(
                behavior,
                values={"expressed_sentiment": {"juror-a": 0, "juror-b": 3}},
                jurors=("juror-a", "juror-b", "juror-c"),
            )
        self.assertEqual(
            result["results"]["expressed_sentiment"],
            {
                "score": 1.5,
                "votes": {"juror-a": 0, "juror-b": 3},
                "errors": {"juror-c": "ValueError"},
            },
        )
        self.assertEqual(result["status"], "complete")

    async def test_each_configured_juror_gets_one_request_and_a_vote(self):
        requests, result = await self.run_evaluation(jurors=JURORS)
        expected = list(JURORS)
        self.assertCountEqual([request["model"] for request in requests], expected)
        self.assertEqual(result["jurors"], expected)
        self.assertEqual(result["status"], "complete")
        self.assertEqual(
            set(result["results"]["transfer_policy_followed"]["votes"]), set(expected)
        )

    async def test_appointment_action_without_a_confirmed_write_is_unknown(self):
        actions = {"book_appointment", "reschedule_appointment", "cancel_appointment"}
        for tool in (*actions, "list_available_appointments", "resolve_patient"):
            for returned in (False, True):
                with self.subTest(tool=tool, returned=returned):
                    requests, result = await self.run_evaluation(
                        tool=tool, returned=returned, closeout=confirmed()
                    )
                    self.assertNotIn(
                        "appointment_datetime_correct", requests[0]["questions"]
                    )
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

    async def test_recorded_write_outcome_decides_the_date_check_not_answer_wording(
        self,
    ):
        for outcome, asked in (
            ("booked", True),
            ("cancelled", True),
            ("rescheduled", True),
            ("partial_reschedule", True),
            ("rejected", False),
            ("failed", False),
            ("uncertain", False),
            ("needs_input", False),
            ("staff_task_created", False),
        ):
            for output in (
                "success: Booked Wednesday at 10:00.",
                "Your visit is set for Wednesday at 10:00.",
            ):
                with self.subTest(outcome=outcome, output=output):
                    requests, result = await self.run_evaluation(
                        action_output=output, closeout=confirmed(outcome)
                    )
                    self.assertEqual(
                        "appointment_datetime_correct" in requests[0]["questions"],
                        asked,
                    )
                    if not asked:
                        self.assertEqual(
                            result["results"]["appointment_datetime_correct"],
                            {
                                "status": "unknown",
                                "reason": "no_confirmed_appointment_action",
                            },
                        )

    async def test_call_office_knowledge_is_sent_to_the_yes_no_jury(self):
        for knowledge in (OFFICE_KNOWLEDGE, UNAVAILABLE):
            with self.subTest(knowledge=knowledge):
                requests, result = await self.run_evaluation(
                    office_knowledge=knowledge,
                    values={"knowledge_available": 0.1},
                )
                question = requests[0]["questions"]["knowledge_available"]
                self.assertEqual(question["type"], "noul")
                self.assertEqual(set(question["criteria"]), {"true", "false"})
                self.assertFalse(result["results"]["knowledge_available"]["verdict"])
                self.assertIn("office_question_answered", requests[0]["questions"])
                self.assertEqual(requests[0]["state"]["office_knowledge"], knowledge)

    async def test_a_knowledge_gap_names_the_caller_line_most_jurors_picked(self):
        jurors = ("juror-a", "juror-b", "juror-c")
        requests, result = await self.run_evaluation(
            values={
                "knowledge_available": 0.1,
                "knowledge_gap_line": {"juror-a": "3", "juror-b": "3", "juror-c": "0"},
            },
            jurors=jurors,
        )
        question = requests[0]["questions"]["knowledge_gap_line"]
        self.assertEqual(question["type"], "choice")
        self.assertEqual(
            question["criteria"],
            {
                "0": "caller: Move Tuesday at 9 to Wednesday at 10.",
                "3": "caller: This is frustrating. Please get a person.",
                "4": "caller: Thanks.",
                "none": "No caller office question went unanswered for missing office knowledge.",
            },
        )
        self.assertEqual(result["status"], "complete")
        self.assertEqual(
            result["results"]["knowledge_available"]["line"],
            "This is frustrating. Please get a person.",
        )

    async def test_a_knowledge_gap_without_a_majority_line_names_none(self):
        jurors = ("juror-a", "juror-b", "juror-c")
        for picks in (
            {"juror-a": "3", "juror-b": "4", "juror-c": "0"},
            {"juror-a": "none", "juror-b": "none", "juror-c": "3"},
        ):
            with self.subTest(picks=picks):
                _, result = await self.run_evaluation(
                    values={"knowledge_available": 0.1, "knowledge_gap_line": picks},
                    jurors=jurors,
                )
                self.assertEqual(result["status"], "complete")
                self.assertEqual(result["errors"], {})
                self.assertNotIn("line", result["results"]["knowledge_available"])

    async def test_an_invalid_line_pick_is_logged_and_not_counted(self):
        with self.assertLogs("abita_s2s.observability.knowledge_gap", "ERROR") as logs:
            _, result = await self.run_evaluation(
                values={
                    "knowledge_available": 0.1,
                    "knowledge_gap_line": {
                        "juror-a": "3",
                        "juror-b": "7",
                        "juror-c": "7",
                    },
                },
                jurors=("juror-a", "juror-b", "juror-c"),
            )
        self.assertEqual(
            [record.getMessage() for record in logs.records],
            [
                f"Call evaluation answer invalid judge=knowledge_gap_line juror={juror}"
                for juror in ("juror-b", "juror-c")
            ],
        )
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["errors"], {})
        self.assertNotIn("line", result["results"]["knowledge_available"])

    async def test_answered_knowledge_names_no_line(self):
        _, result = await self.run_evaluation(
            values={"knowledge_available": 0.9, "knowledge_gap_line": "3"}
        )
        self.assertTrue(result["results"]["knowledge_available"]["verdict"])
        self.assertNotIn("line", result["results"]["knowledge_available"])

    async def test_each_juror_gets_every_question_in_one_request_with_zero_retention(
        self,
    ):
        requests, result = await self.run_evaluation(
            tool="list_available_appointments",
            extra_tools=("book_appointment",),
        )
        sent = {}
        for request in requests:
            self.assertEqual(request["model"], "juror-a")
            self.assertEqual(request["provider"], {"zdr": True})
            sent.update(request["questions"])
        self.assertEqual(set(sent), {*QUESTIONS, "knowledge_gap_line"})
        self.assertEqual(len(requests), 1)
        self.assertEqual(result["evaluator"], "jury")
        self.assertEqual(result["evaluatorVersion"], EVALUATOR_VERSION)
        self.assertEqual(result["jurors"], ["juror-a"])
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["errors"], {})
        self.assertEqual(set(result["results"]), set(QUESTIONS))
        for name, question in sent.items():
            if name in ("expressed_sentiment", "knowledge_gap_line"):
                continue
            self.assertEqual(question["type"], "noul")
            self.assertEqual(set(question["criteria"]), {"true", "false"})

    async def test_the_model_version_that_answered_is_kept_per_juror(self):
        async def unnamed(juror, attempt, request, answers):
            if juror == "juror-b":
                return httpx.Response(200, json={"answers": answers})

        _, result = await self.run_evaluation(unnamed, jurors=("juror-a", "juror-b"))
        self.assertEqual(
            result["servedModels"], {"juror-a": "juror-a-20261014", "juror-b": None}
        )

    async def test_judges_receive_full_history_and_return_verdicts(self):
        requests, result = await self.run_evaluation(
            values={
                "appointment_datetime_correct": 0.1,
                "transfer_policy_followed": 0.4,
                "call_ended_correctly": 0.41,
            }
        )
        self.assertEqual(len(requests), 1)
        self.assertEqual(len(requests[0]["questions"]), 8)
        self.assertEqual(result["status"], "complete")
        jev = "juror-a"
        self.assertEqual(
            result["results"]["appointment_datetime_correct"],
            {"verdict": False, "probability": 0.1, "votes": {jev: 0.1}, "errors": {}},
        )
        self.assertFalse(result["results"]["transfer_policy_followed"]["verdict"])
        self.assertTrue(result["results"]["call_ended_correctly"]["verdict"])
        state = requests[0]["state"]
        for request in requests:
            self.assertEqual(request["state"], state)
        self.assertEqual(
            set(state),
            {"transcript", "call_started", "office_knowledge", "transfer_policy"},
        )
        self.assertEqual(
            state["transcript"],
            [
                "caller: Move Tuesday at 9 to Wednesday at 10.",
                'tool reschedule_appointment({"time":"10:00"}) -> success: The appointment was moved to Wednesday at 10:00.',
                "agent: It is rescheduled.",
                "caller: This is frustrating. Please get a person.",
                "caller: Thanks.",
                "tool end_call({}) -> Say a brief goodbye to the caller.",
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
        for tool, extra_tools, facts, count in (
            ("list_available_appointments", (), (), 5),
            ("reschedule_appointment", (), ("rescheduled",), 6),
        ):
            for value in (0.0, 0.5, 1.0):
                with self.subTest(tool=tool, value=value):
                    requests, result = await self.run_evaluation(
                        values=dict.fromkeys(names, value),
                        tool=tool,
                        extra_tools=extra_tools,
                        jurors=JURORS,
                        closeout=confirmed(*facts),
                    )
                    self.assertEqual(result["status"], "complete")
                    self.assertEqual(result["errors"], {})
                    self.assertEqual(len(requests[0]["questions"]), count + 2)
                    for name in requests[0]["questions"]:
                        if name in ("expressed_sentiment", "knowledge_gap_line"):
                            continue
                        answer = result["results"][name]
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
                self.assertEqual(len(result["results"]), 6)
                self.assertNotIn("emergency_policy_followed", result["results"])
                self.assertEqual(
                    result["errors"]["emergency_policy_followed"],
                    {
                        "cause": "no_quorum",
                        "votes": {},
                        "errors": {"juror-a": "ValueError"},
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
            {"juror-a": "TimeoutError"},
        )
        self.assertIn("http_status=429", str(logs.output))
        self.assertEqual(len(result["errors"]), 7)
        self.assertEqual(result["results"], {})

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
            self.assertEqual(result["results"], {})
            cause = "ConnectError" if transport_failure else "HTTPStatusError"
            self.assertEqual(
                result["errors"]["emergency_policy_followed"]["errors"],
                {"juror-a": cause},
            )
            self.assertIn("attempts=2", str(logs.output))
            self.assertNotIn("synthetic-secret", str(result) + str(logs.output))


class JudgeStateTests(unittest.TestCase):
    def test_call_reads_as_lines_beside_the_policies_judges_read(self):
        history = ChatContext()
        history.items.append(
            AgentConfigUpdate(instructions="Speaker prompt.", created_at=1_791_475_200)
        )
        history.add_message(role="user", content="Is Dr. Lee in Friday?")
        history.items.append(
            FunctionCall(call_id="k-1", name="resolve_patient", arguments='{"q":"Lee"}')
        )
        history.add_message(role="assistant", content="One moment.", interrupted=True)
        history.items.append(
            FunctionCallOutput(
                call_id="k-1",
                name="resolve_patient",
                output="Dr. Lee: Mon-Thu",
                is_error=False,
            )
        )
        history.add_message(role="assistant", content="  ")
        history.items.append(
            FunctionCall(call_id="t-1", name="transfer_call", arguments="{}")
        )
        state = judge_state(history, OFFICE_KNOWLEDGE)
        self.assertEqual(
            state["transcript"],
            [
                "caller: Is Dr. Lee in Friday?",
                "agent: One moment. (interrupted)",
                'tool resolve_patient({"q":"Lee"}) -> Dr. Lee: Mon-Thu',
                "tool transfer_call({}) -> no result",
            ],
        )
        self.assertEqual(
            state["call_started"], "Thursday, 2026-10-08 12:00 EDT (America/New_York)"
        )
        self.assertEqual(state["office_knowledge"], OFFICE_KNOWLEDGE)
        self.assertEqual(state["transfer_policy"], load_prompt("transfer_policy"))

    def test_every_state_field_is_named_by_a_question_and_every_named_field_is_sent(
        self,
    ):
        history = ChatContext()
        history.add_message(role="user", content="Hi.")
        named = set()
        for question in (*QUESTIONS.values(), knowledge_gap.line_question([])):
            text = json.dumps(question)
            named.update(re.findall(r"`([a-z_]+)`", text))
        self.assertEqual(named, set(judge_state(history, OFFICE_KNOWLEDGE)))

    def test_only_an_agent_hangup_is_graded_for_how_the_call_ended(self):
        def ended(*outputs):
            history = ChatContext()
            history.add_message(role="user", content="Please get me a person.")
            for index, output in enumerate(outputs):
                history.items.extend(
                    [
                        FunctionCall(
                            call_id=f"end-{index}", name="end_call", arguments="{}"
                        ),
                        FunctionCallOutput(
                            call_id=f"end-{index}",
                            name="end_call",
                            output=output,
                            is_error=False,
                        ),
                    ]
                )
            return GATES["call_ended_correctly"](history, {})

        skipped = {"status": "not_applicable", "reason": "no_agent_hangup"}
        blocked = "blocked: The caller spoke again after this request."
        self.assertEqual(ended(), skipped)
        self.assertEqual(ended(blocked), skipped)
        self.assertIsNone(ended("Say a brief goodbye to the caller."))
        self.assertIsNone(ended(blocked, "Say a brief goodbye to the caller."))

    def test_office_answers_share_the_knowledge_gap_office_question_scope(self):
        self.assertIn(
            knowledge_available.OFFICE_QUESTION,
            QUESTIONS["office_question_answered"]["instructions"],
        )

    def test_a_knowledge_gap_and_its_caller_line_share_one_office_question_scope(
        self,
    ):
        scope = knowledge_available.OFFICE_QUESTION
        for excluded in (
            "insurance acceptance",
            "open appointment times",
            "medical advice",
        ):
            self.assertIn(excluded, scope.split("Not office questions:")[1])
        self.assertIn(scope, QUESTIONS["knowledge_available"]["instructions"])
        self.assertIn(scope, knowledge_gap.line_question([])["instructions"])


class DecisionsAnswerTests(unittest.TestCase):
    sentiment = QUESTIONS["expressed_sentiment"]

    def test_score_within_rounding_of_its_weighted_distribution_is_kept(self):
        for score, probabilities in (
            (1.99, {"0": 0, "1": 0, "2": 1}),
            (2.01, {"1": 0.33, "2": 0.34, "3": 0.34}),
            (0.6, {"0": 0.4, "1": 0.6}),
        ):
            answer = {"type": "score", "score": score, "probabilities": probabilities}
            with self.subTest(answer=answer):
                self.assertEqual(parse(self.sentiment, answer), score)

    def test_score_that_contradicts_its_distribution_is_rejected(self):
        for score, probabilities in (
            (4, {"0": 1}),
            (0, {"0": 0.5, "4": 0.5}),
            (1, {"0": 0.5, "4": 0.5}),
        ):
            answer = {"type": "score", "score": score, "probabilities": probabilities}
            with self.subTest(answer=answer), self.assertRaises(ValueError):
                parse(self.sentiment, answer)

    def test_response_over_one_megabyte_is_rejected(self):
        body = b'{"answers": {}}'
        at_cap = body + b" " * (1024 * 1024 - len(body))
        self.assertEqual(read_answers(at_cap), ({}, None))
        with self.assertRaises(ValueError):
            read_answers(at_cap + b" ")

    def test_non_finite_constants_are_rejected(self):
        for constant in (b"NaN", b"Infinity", b"-Infinity"):
            with self.subTest(constant=constant), self.assertRaises(ValueError):
                read_answers(b'{"answers": {"x": ' + constant + b"}}")


class EvaluatorVersionTests(unittest.TestCase):
    def test_current_judge_definitions_have_a_published_version(self):
        self.assertIn(
            DEFINITIONS_HASH,
            PUBLISHED_VERSIONS,
            "Judge definitions changed. Product reads evaluatorVersion by exact "
            "value: map this hash to a new version name in PUBLISHED_VERSIONS "
            "after Product supports it, or to the current name if meaning is "
            "unchanged.",
        )
        self.assertEqual(EVALUATOR_VERSION, PUBLISHED_VERSIONS[DEFINITIONS_HASH])

    def test_editing_one_judge_question_changes_the_version(self):
        def reword(package):
            judge = package / "observability" / "judges" / "call_ended_correctly.py"
            text = judge.read_text()
            judge.write_text(
                text.replace('"instructions": "', '"instructions": "Reworded. ')
            )
            self.assertNotEqual(judge.read_text(), text)

        version = scratch_version(reword)
        self.assertTrue(version)
        self.assertNotEqual(version, EVALUATOR_VERSION)

    def test_line_endings_do_not_change_the_version(self):
        def crlf(package):
            for source in (
                *(package / "observability" / "judges").glob("*.py"),
                package / "observability" / "judge_state.py",
                package / "prompts" / "transfer_policy.md",
            ):
                source.write_bytes(source.read_bytes().replace(b"\n", b"\r\n"))

        self.assertEqual(scratch_version(crlf), EVALUATOR_VERSION)

    def test_editing_what_judges_are_shown_changes_the_version(self):
        for source in (
            Path("observability") / "judge_state.py",
            Path("prompts") / "transfer_policy.md",
        ):
            with self.subTest(source=source):

                def append(package, source=source):
                    path = package / source
                    path.write_text(path.read_text() + "\n")

                version = scratch_version(append)
                self.assertTrue(version)
                self.assertNotEqual(version, EVALUATOR_VERSION)
