"""Real AgentSession tool execution with offline model and Product transports."""

import asyncio
import json
import unittest
from unittest.mock import AsyncMock, patch

import httpx
from livekit.agents import AgentSession, llm
from test_knowledge import CONFIG, FOUND, call_state

from abita_s2s.agent import AbitaAgent
from abita_s2s.knowledge import OfficeKnowledge
from abita_s2s.offices import SPRING_HILL


class KnowledgeModel(llm.LLM):
    def __init__(self):
        super().__init__()
        self.requests = []

    def chat(self, *, chat_ctx, tools=None, conn_options, **kwargs):
        self.requests.append(chat_ctx.copy())
        return KnowledgeStream(
            self, chat_ctx=chat_ctx, tools=tools or [], conn_options=conn_options
        )


class KnowledgeStream(llm.LLMStream):
    async def _run(self):
        outputs = [
            item for item in self._chat_ctx.items if item.type == "function_call_output"
        ]
        if outputs:
            result = json.loads(outputs[-1].output)
            delta = llm.ChoiceDelta(role="assistant", content=result["answer"])
        else:
            delta = llm.ChoiceDelta(
                role="assistant",
                tool_calls=[
                    llm.FunctionToolCall(
                        name="search_office_knowledge",
                        arguments=json.dumps({"query": "When do you close?"}),
                        call_id="lookup-1",
                    )
                ],
            )
        self._event_ch.send_nowait(llm.ChatChunk(id="response-1", delta=delta))


class KnowledgeSessionTests(unittest.IsolatedAsyncioTestCase):
    async def test_tool_result_reaches_next_model_turn_and_followup(self):
        for body in (
            FOUND,
            {"outcome": "no_relevant_information", "passages": []},
            {"outcome": "temporary_failure", "passages": []},
        ):
            with self.subTest(outcome=body["outcome"]):
                requests = []

                def handler(request, requests=requests, body=body):
                    requests.append(request)
                    return httpx.Response(200, json=body)

                async with httpx.AsyncClient(
                    transport=httpx.MockTransport(handler)
                ) as client:
                    model = KnowledgeModel()
                    agent = AbitaAgent(SPRING_HILL, OfficeKnowledge(client, CONFIG))
                    async with AgentSession(
                        llm=model, userdata=call_state()
                    ) as session:
                        # Suppress only the greeting; exercise the real registered tool and executor.
                        with patch.object(AbitaAgent, "on_enter", new=AsyncMock()):
                            await session.start(agent=agent)
                        await asyncio.wait_for(
                            session.run(user_input="When do you close?"), 5
                        )
                        self.assertEqual(len(requests), 1)
                        self.assertEqual(
                            requests[0].headers["x-office-key"], "spring-hill"
                        )
                        consumed = [
                            item
                            for request in model.requests
                            for item in request.items
                            if item.type == "function_call_output"
                        ]
                        self.assertTrue(consumed)
                        result = json.loads(consumed[-1].output)
                        self.assertEqual(result["outcome"], body["outcome"])
                        self.assertNotIn("revision-1", consumed[-1].output)
                        self.assertNotIn("Status:", consumed[-1].output)
                        self.assertTrue(
                            any(
                                item.type == "message"
                                and item.role == "assistant"
                                and item.text_content == result["answer"]
                                for item in agent.chat_ctx.items
                            )
                        )
                        await asyncio.wait_for(
                            session.run(user_input="Repeat that, please."), 5
                        )
                        self.assertEqual(len(requests), 1)
                        self.assertTrue(
                            any(
                                item.type == "function_call_output"
                                for item in model.requests[-1].items
                            )
                        )
