"""Call labels use the same shutdown deadline and retain only supported evidence."""

import asyncio
import json
import unittest
from unittest.mock import patch

import httpx
from livekit.agents import ChatContext
from livekit.agents.llm import AudioContent, ChatMessage

from abita_s2s.observability.call_labels import (
    INTENT_CRITERIA,
    MODEL,
    OUTCOME_CRITERIA,
    validate_choice,
)
from abita_s2s.observability.evaluation import evaluate_call


def choice(criteria, value):
    return {
        "type": "choice",
        "choice": value,
        "probabilities": {key: float(key == value) for key in criteria},
    }


class CallLabelTests(unittest.IsolatedAsyncioTestCase):
    async def run_labels(
        self, answers=None, *, closeout=None, failure=None, hang=None, seconds=60
    ):
        history = ChatContext()
        history.add_message(role="user", content="Please cancel my appointment.")
        history.add_message(role="assistant", content="It is cancelled.")
        requests = []

        async def respond(request):
            payload = json.loads(request.content)
            requests.append(payload)
            labels = "intent" in payload["questions"]
            if hang == ("labels" if labels else "jury"):
                await asyncio.Event().wait()
            if labels:
                if failure:
                    return httpx.Response(failure, json={"error": "private body"})
                return httpx.Response(
                    200, content=json.dumps({"answers": answers}).encode()
                )
            return httpx.Response(
                200,
                json={
                    "answers": {
                        name: (
                            {"type": "score", "score": 2, "probabilities": {"2": 1}}
                            if question["type"] == "score"
                            else {"type": "noul", "noul": 0.8}
                        )
                        for name, question in payload["questions"].items()
                    }
                },
            )

        if answers is None:
            answers = {
                "intent": choice(INTENT_CRITERIA, "cancel_appointment"),
                "outcome": choice(OUTCOME_CRITERIA, "resolved"),
            }
        client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
        with (
            patch(
                "abita_s2s.observability.evaluation.httpx.AsyncClient",
                return_value=client,
            ),
            patch("abita_s2s.observability.evaluation.EVALUATION_SECONDS", 0.03),
        ):
            result = await evaluate_call(
                {"chat_history": history.to_dict()},
                "synthetic-key",
                seconds,
                closeout=closeout or {"transferStatus": "idle"},
            )
        return requests, result

    async def test_two_choice_request_emits_exact_classification_contract(self):
        closeout = {
            "transferStatus": "idle",
            "domainOutcomes": [{"outcome": "cancelled"}],
        }
        requests, result = await self.run_labels(closeout=closeout)
        labels = [p for p in requests if "intent" in p["questions"]]
        self.assertEqual(len(labels), 1)
        request = labels[0]
        self.assertEqual(request["model"], MODEL)
        self.assertEqual(request["provider"], {"zdr": True})
        self.assertEqual(request["state"]["closeout"], closeout)
        self.assertIsNone(request["state"]["staffConnected"])
        self.assertEqual(set(request["questions"]), {"intent", "outcome"})
        self.assertEqual(len(request["questions"]["intent"]["criteria"]), 14)
        self.assertEqual(len(request["questions"]["outcome"]["criteria"]), 3)
        self.assertEqual(
            result["classification"],
            {
                "version": "call-labels-v1",
                "model": MODEL,
                "status": "complete",
                "intent": "cancel_appointment",
                "outcome": "resolved",
                "answers": {
                    "intent": choice(INTENT_CRITERIA, "cancel_appointment"),
                    "outcome": choice(OUTCOME_CRITERIA, "resolved"),
                },
            },
        )

    async def test_partial_choice_is_retained(self):
        for answers, retained, omitted in (
            ({"intent": choice(INTENT_CRITERIA, "other")}, "intent", "outcome"),
            ({"outcome": choice(OUTCOME_CRITERIA, "resolved")}, "outcome", "intent"),
        ):
            _, result = await self.run_labels(answers)
            labels = result["classification"]
            self.assertEqual(labels["status"], "incomplete")
            self.assertIn(retained, labels)
            self.assertNotIn(omitted, labels)
            self.assertEqual(labels["answers"], answers)

    async def test_short_meaningful_call_keeps_labels_without_failure_jury(self):
        requests, result = await self.run_labels(seconds=15)
        self.assertEqual(len(requests), 1)
        self.assertEqual(set(requests[0]["questions"]), {"intent", "outcome"})
        self.assertEqual(result["status"], "skipped")
        self.assertEqual(result["reason"], "call_too_short")
        self.assertEqual(result["classification"]["status"], "complete")
        self.assertEqual(result["classification"]["intent"], "cancel_appointment")

    async def test_unconfirmed_transfer_cannot_be_any_terminal_outcome(self):
        for transfer in ("pending", "accepted", "ambiguous", "unknown"):
            for outcome in OUTCOME_CRITERIA:
                with self.subTest(transfer=transfer, outcome=outcome):
                    answers = {
                        "intent": choice(INTENT_CRITERIA, "speak_to_staff"),
                        "outcome": choice(OUTCOME_CRITERIA, outcome),
                    }
                    _, result = await self.run_labels(
                        answers, closeout={"transferStatus": transfer}
                    )
                    labels = result["classification"]
                    self.assertEqual(labels["status"], "incomplete")
                    self.assertEqual(labels["intent"], "speak_to_staff")
                    self.assertEqual(
                        labels["reason"], "transfer_connection_not_evidenced"
                    )
                    self.assertNotIn("outcome", labels)

    async def test_known_failed_or_unsent_transfer_can_classify_remaining_call(self):
        for transfer in ("failed", "retryable"):
            _, result = await self.run_labels(closeout={"transferStatus": transfer})
            self.assertEqual(result["classification"]["status"], "complete")
            self.assertEqual(result["classification"]["outcome"], "resolved")

    async def test_model_cannot_invent_staff_connection(self):
        _, result = await self.run_labels(
            {
                "intent": choice(INTENT_CRITERIA, "other"),
                "outcome": choice(OUTCOME_CRITERIA, "transferred"),
            }
        )
        self.assertEqual(
            result["classification"]["reason"], "transfer_connection_not_evidenced"
        )
        self.assertNotIn("outcome", result["classification"])

    async def test_incomplete_call_evidence_cannot_be_resolved_or_abandoned(self):
        for closeout in (
            {"callStatus": "FAILED"},
            {"mutationDrainFailed": True},
            {"closeReason": "startup_or_report_failure"},
        ):
            for outcome in ("resolved", "abandoned"):
                _, result = await self.run_labels(
                    {
                        "intent": choice(INTENT_CRITERIA, "other"),
                        "outcome": choice(OUTCOME_CRITERIA, outcome),
                    },
                    closeout=closeout,
                )
                labels = result["classification"]
                self.assertEqual(labels["intent"], "other")
                self.assertEqual(labels["reason"], "call_evidence_incomplete")
                self.assertNotIn("outcome", labels)

    async def test_provider_failure_and_deadline_do_not_erase_jury(self):
        for kwargs in ({"failure": 400}, {"hang": "labels"}):
            _, result = await self.run_labels(**kwargs)
            self.assertEqual(result["status"], "complete")
            self.assertEqual(result["classification"]["status"], "incomplete")
            self.assertNotIn("private body", str(result))

    async def test_jury_deadline_does_not_erase_classification(self):
        with self.assertLogs("abita_s2s.observability.evaluation", "ERROR"):
            _, result = await self.run_labels(hang="jury")
        self.assertEqual(result["status"], "incomplete")
        self.assertEqual(result["classification"]["status"], "complete")

    async def test_evaluation_skips_emit_skipped_classification(self):
        history = ChatContext()
        history.add_message(role="user", content="Hello")
        report = {"chat_history": history.to_dict()}
        for native, key, seconds, budget, reason in (
            (report, "", 60, 30, "openrouter_key_not_configured"),
            (report, "key", 60, 0, "shutdown_budget_exhausted"),
            (
                {"chat_history": ChatContext().to_dict()},
                "key",
                60,
                30,
                "no_user_messages",
            ),
            ({}, "key", 60, 30, "native_report_unavailable"),
        ):
            with patch(
                "abita_s2s.observability.evaluation.httpx.AsyncClient"
            ) as client:
                result = await evaluate_call(native, key, seconds, budget=budget)
            client.assert_not_called()
            self.assertEqual(
                result["classification"],
                {
                    "version": "call-labels-v1",
                    "model": MODEL,
                    "status": "skipped",
                    "reason": reason,
                },
            )

    async def test_empty_or_whitespace_caller_evidence_skips_without_model_request(
        self,
    ):
        for content in ([], [""], [" \n\t "], [AudioContent(frame=[])]):
            history = ChatContext(items=[ChatMessage(role="user", content=content)])
            with patch(
                "abita_s2s.observability.evaluation.httpx.AsyncClient"
            ) as client:
                result = await evaluate_call(
                    {"chat_history": history.to_dict()}, "key", 60
                )
            client.assert_not_called()
            self.assertEqual(result["classification"]["status"], "skipped")
            self.assertEqual(result["classification"]["reason"], "no_user_messages")
            self.assertNotIn("outcome", result["classification"])

    def test_probability_validation_matches_product(self):
        valid = choice(OUTCOME_CRITERIA, "resolved")
        self.assertEqual(validate_choice(valid, OUTCOME_CRITERIA), "resolved")
        for invalid in (
            None,
            {**valid, "type": "noul"},
            {**valid, "choice": "unknown"},
            {**valid, "choice": []},
            {**valid, "probabilities": {"resolved": 1}},
            {**valid, "probabilities": {"resolved": 1, "transferred": 0, "other": 0}},
            *(
                {
                    **valid,
                    "probabilities": {
                        "resolved": value,
                        "transferred": 0,
                        "abandoned": 0,
                    },
                }
                for value in (
                    True,
                    None,
                    -1,
                    2,
                    10**1000,
                    float("nan"),
                    float("inf"),
                    0.9,
                )
            ),
        ):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                validate_choice(invalid, OUTCOME_CRITERIA)
