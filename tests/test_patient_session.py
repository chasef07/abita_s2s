"""The real registered LiveKit tool/executor with deterministic model and HTTP."""

import asyncio
import json
import unittest
from unittest.mock import AsyncMock, patch

import httpx
from livekit.agents import AgentSession, llm
from test_patient_resolution import CONFIG, call_state, receipt

from abita_s2s.agent import AbitaAgent
from abita_s2s.identity import PatientResolver
from abita_s2s.middleware import PatientMiddleware
from abita_s2s.offices import SPRING_HILL


class PatientModel(llm.LLM):
    def __init__(self):
        super().__init__()
        self.requests = []

    def chat(self, *, chat_ctx, tools=None, conn_options, **kwargs):
        self.requests.append(chat_ctx.copy())
        return PatientStream(
            self, chat_ctx=chat_ctx, tools=tools or [], conn_options=conn_options
        )


class PatientStream(llm.LLMStream):
    async def _run(self):
        items = self._chat_ctx.items
        last_user = max(
            i
            for i, item in enumerate(items)
            if item.type == "message" and item.role == "user"
        )
        outputs = [
            item for item in items[last_user:] if item.type == "function_call_output"
        ]
        if outputs:
            delta = llm.ChoiceDelta(role="assistant", content=outputs[-1].output)
        else:
            args = {
                "Jane": {"firstName": "Jane", "dob": None},
                "01/02/1980": {"firstName": None, "dob": "01/02/1980"},
                "Actually John": {"firstName": "John", "dob": None},
            }
            delta = llm.ChoiceDelta(
                role="assistant",
                tool_calls=[
                    llm.FunctionToolCall(
                        name="resolve_patient",
                        arguments=json.dumps(args[items[last_user].text_content]),
                        call_id=f"patient-{last_user}",
                    )
                ],
            )
        self._event_ch.send_nowait(llm.ChatChunk(id="offline-response", delta=delta))


class PatientSessionTests(unittest.IsolatedAsyncioTestCase):
    async def test_first_name_dob_and_correction_through_session(self):
        requests = []
        bodies = [receipt()]

        def handler(request):
            requests.append(json.loads(request.content))
            return httpx.Response(200, json=bodies.pop(0))

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            state = call_state(None)
            resolver = PatientResolver(state, PatientMiddleware(client, CONFIG))
            self.addAsyncCleanup(resolver.aclose)
            model = PatientModel()
            agent = AbitaAgent(SPRING_HILL, None, resolver)
            async with AgentSession(llm=model, userdata=state) as session:
                with patch.object(AbitaAgent, "on_enter", new=AsyncMock()):
                    await session.start(agent=agent)
                for user, outcome, active in [
                    ("Jane", "needs_input", False),
                    ("01/02/1980", "success", True),
                    ("Actually John", "needs_input", False),
                ]:
                    await asyncio.wait_for(session.run(user_input=user), 5)
                    outputs = [
                        item
                        for item in model.requests[-1].items
                        if item.type == "function_call_output"
                    ]
                    self.assertTrue(
                        outputs[-1].output.startswith(outcome + ": "),
                        outputs[-1].output,
                    )
                    self.assertEqual(state.patient.active is not None, active)
                    for output in outputs:
                        for private in (
                            "chart-jane",
                            "private-plan",
                            "private-party",
                            "+15555550999",
                        ):
                            self.assertNotIn(private, output.output)
                self.assertEqual(len(requests), 1)
                self.assertNotIn("patientId", requests[0])
