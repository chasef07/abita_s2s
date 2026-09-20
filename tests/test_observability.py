"""Post-call evaluation failures must remain visible without breaking closeout."""

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from livekit.agents import ChatContext
from livekit.agents.evals import EvaluationResult, JudgmentResult

from abita_s2s.observability import evaluate_call
from abita_s2s.runtime.session_startup import finish_voice_call


class ObservabilityTests(unittest.IsolatedAsyncioTestCase):
    def context(self):
        history = ChatContext()
        history.add_message(role="user", content="Please cancel my visit.")
        return SimpleNamespace(
            make_session_report=Mock(
                return_value=SimpleNamespace(chat_history=history)
            ),
            tagger=Mock(),
        )

    async def test_full_history_and_failed_verdict_are_complete(self):
        ctx = self.context()
        group = Mock(judges=[SimpleNamespace(name="accuracy")], llm=AsyncMock())
        group.evaluate = AsyncMock(
            return_value=EvaluationResult(
                judgments={
                    "accuracy": JudgmentResult(
                        verdict="fail", reasoning="Unsupported claim"
                    ),
                }
            )
        )
        with patch("abita_s2s.observability.JudgeGroup", return_value=group):
            await evaluate_call(ctx)
        group.evaluate.assert_awaited_once_with(
            ctx.make_session_report.return_value.chat_history
        )
        ctx.tagger.add.assert_called_once_with("abita.evaluation:complete")

    async def test_missing_or_empty_verdicts_never_pass(self):
        for judgments in (
            {},
            {"accuracy": JudgmentResult(verdict="pass", reasoning="Grounded")},
        ):
            ctx = self.context()
            group = Mock(
                judges=[
                    SimpleNamespace(name="accuracy"),
                    SimpleNamespace(name="tool_use"),
                ],
                llm=AsyncMock(),
            )
            group.evaluate = AsyncMock(
                return_value=EvaluationResult(judgments=judgments)
            )
            with patch("abita_s2s.observability.JudgeGroup", return_value=group):
                await evaluate_call(ctx)
            ctx.tagger.add.assert_called_once_with("abita.evaluation:incomplete")

    async def test_timeout_is_incomplete_and_closes_model(self):
        ctx = self.context()
        group = Mock(llm=AsyncMock())

        async def hang(*_):
            await asyncio.Event().wait()

        group.evaluate = hang
        with (
            patch("abita_s2s.observability.JudgeGroup", return_value=group),
            patch("abita_s2s.observability.EVALUATION_SECONDS", 0.01),
        ):
            await evaluate_call(ctx)
        ctx.tagger.add.assert_called_once_with("abita.evaluation:incomplete")
        group.llm.__aexit__.assert_awaited_once()

    async def test_no_caller_or_missing_report(self):
        ctx = self.context()
        ctx.make_session_report.return_value.chat_history = ChatContext()
        with patch("abita_s2s.observability.JudgeGroup") as group:
            await evaluate_call(ctx)
            group.assert_not_called()
        ctx.tagger.add.assert_called_once_with("abita.evaluation:skipped")
        ctx.tagger.reset_mock()
        ctx.make_session_report.side_effect = RuntimeError("unavailable")
        await evaluate_call(ctx)
        ctx.tagger.add.assert_called_once_with("abita.evaluation:incomplete")

    async def test_closeout_finishes_before_evaluation_even_on_failure(self):
        for error in (None, RuntimeError("delivery failed")):
            events = []

            async def finish(*_):
                events.append("closeout")
                if error:
                    raise error

            async def evaluate(*_):
                events.append("evaluation")

            ctx = SimpleNamespace(
                primary_session=SimpleNamespace(
                    userdata=SimpleNamespace(reporter=SimpleNamespace(finish=finish)),
                )
            )
            with patch(
                "abita_s2s.runtime.session_startup.evaluate_call", side_effect=evaluate
            ):
                if error:
                    with self.assertRaises(RuntimeError):
                        await finish_voice_call(ctx)
                else:
                    await finish_voice_call(ctx)
            self.assertEqual(events, ["closeout", "evaluation"])
