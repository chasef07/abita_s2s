"""Registered AgentSession tool and Product transport, entirely synthetic/offline."""

import asyncio
import json
import unittest
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, patch

import httpx
from livekit.agents import AgentSession, llm
from test_knowledge import call_state

from abita_s2s.agent import AbitaAgent
from abita_s2s.config import Config, load_config
from abita_s2s.identity import PatientResolver
from abita_s2s.middleware import Failure, Receipt
from abita_s2s.offices import get_office_profile
from abita_s2s.staff_tasks import StaffTasks

CONFIG = Config(
    "offline",
    product_secret="test-secret",
    staff_tasks_url="https://product.example/v1/tasks",
)
NEED = {
    "category": "medication",
    "urgency": "normal",
    "summary": "Refill requested",
    "message": "Caller approves sending refill request. Medication name and pharmacy are missing.",
}
TASK_ID = "11111111-1111-4111-8111-111111111111"


def receipt(payload, status="created"):
    return {
        "status": status,
        "taskId": TASK_ID,
        "category": payload["category"],
        "urgency": payload["urgency"],
    }


def patient(name="Jane", id="synthetic-1"):
    return Receipt(
        status="verified",
        patientId=id,
        name=name,
        dob="01/01/1980",
        phone="+15555550999",
        appointmentsStatus="none",
        appointments=[],
    )


class Model(llm.LLM):
    def chat(self, *, chat_ctx, tools=None, conn_options, **kwargs):
        return Stream(
            self, chat_ctx=chat_ctx, tools=tools or [], conn_options=conn_options
        )


class Stream(llm.LLMStream):
    async def _run(self):
        items = self._chat_ctx.items
        start = max(
            i for i, x in enumerate(items) if x.type == "message" and x.role == "user"
        )
        outputs = [x for x in items[start:] if x.type == "function_call_output"]
        if outputs:
            delta = llm.ChoiceDelta(role="assistant", content=outputs[-1].output)
        else:
            command = json.loads(items[start].text_content)
            delta = llm.ChoiceDelta(
                role="assistant",
                tool_calls=[
                    llm.FunctionToolCall(
                        name=command["tool"],
                        arguments=json.dumps(command["args"]),
                        call_id=f"call-{start}",
                    )
                ],
            )
        self._event_ch.send_nowait(llm.ChatChunk(id="offline", delta=delta))


class StaffTaskTests(unittest.IsolatedAsyncioTestCase):
    @asynccontextmanager
    async def setup_session(self, handler=None, state=None, config=CONFIG):
        state = state or call_state()
        # call_state from knowledge tests has no caller contact.
        if state.call.caller_phone is None:
            from dataclasses import replace

            state.call = replace(state.call, caller_phone="+15555550101")
        requests = []

        async def transport(request):
            payload = json.loads(request.content)
            requests.append(payload)
            self.assertEqual(request.url.path, "/v1/tasks")
            self.assertEqual(request.headers["authorization"], "Bearer test-secret")
            if handler:
                return await handler(request, payload)
            return httpx.Response(201, json=receipt(payload))

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(transport)
        ) as client:
            middleware = AsyncMock()
            resolver = PatientResolver(state, middleware)
            owner = StaffTasks(state, resolver, client, config)
            agent = AbitaAgent(
                get_office_profile(state.call.called_office_key),
                AsyncMock(),
                resolver,
                staff_tasks=owner,
            )
            async with AgentSession(llm=Model(), userdata=state) as session:
                with patch.object(AbitaAgent, "on_enter", new=AsyncMock()):
                    await session.start(agent=agent)
                try:
                    yield session, agent, state, owner, requests, middleware
                finally:
                    await owner.aclose()
                    await resolver.aclose()

    async def invoke(self, session, agent, args=None, tool="create_staff_task"):
        await asyncio.wait_for(
            session.run(
                user_input=json.dumps(
                    {"tool": tool, "args": NEED if args is None else args}
                )
            ),
            5,
        )
        outputs = [x for x in agent.chat_ctx.items if x.type == "function_call_output"]
        return json.loads(outputs[-1].output)

    async def test_distinct_needs_duplicates_and_changed_details(self):
        async with self.setup_session() as (session, agent, state, _owner, requests, _):
            state.patient.active = patient()
            first = await self.invoke(session, agent)
            self.assertEqual(first["outcome"], "created")
            self.assertEqual(
                (await self.invoke(session, agent))["outcome"], "duplicate"
            )
            for changes in (
                {"message": "Also requests different medication."},
                {"summary": "Corrected refill title"},
                {"urgency": "high_priority"},
            ):
                self.assertEqual(
                    (await self.invoke(session, agent, NEED | changes))["outcome"],
                    "created",
                )
            state.patient.active = patient("Alex", "synthetic-2")
            self.assertEqual((await self.invoke(session, agent))["outcome"], "created")
            self.assertEqual(len(requests), 5)
            self.assertEqual(len({r["idempotencyKey"] for r in requests}), 5)
            self.assertEqual(requests[0]["patient"]["id"], "synthetic-1")
            self.assertEqual(requests[0]["callerPhone"], "+15555550101")
            self.assertNotEqual(requests[0]["callerPhone"], patient().phone)

    async def test_unresolved_patient_from_real_resolution_not_old_patient(self):
        async with self.setup_session() as (
            session,
            agent,
            state,
            _owner,
            requests,
            middleware,
        ):
            state.patient.active = patient()
            middleware.resolve.return_value = Failure(reason="offline")
            await self.invoke(
                session, agent, {"firstName": "Alex", "dob": None}, "resolve_patient"
            )
            self.assertEqual((await self.invoke(session, agent))["outcome"], "created")
            self.assertEqual(requests[-1]["patient"], {"name": "Alex"})

    async def test_missing_identity_is_allowed_missing_contact_is_not(self):
        from dataclasses import replace

        async with self.setup_session() as (session, agent, state, _owner, requests, _):
            self.assertEqual((await self.invoke(session, agent))["outcome"], "created")
            self.assertNotIn("patient", requests[0])
            state.call = replace(state.call, caller_phone=None)
            self.assertEqual((await self.invoke(session, agent))["outcome"], "failed")
            self.assertEqual(len(requests), 1)

    async def test_rejection_and_contract_categories(self):
        async def forbidden(request, payload):
            return httpx.Response(403)

        async with self.setup_session(forbidden) as (
            session,
            agent,
            _state,
            _owner,
            requests,
            _,
        ):
            self.assertEqual((await self.invoke(session, agent))["outcome"], "failed")
            self.assertEqual(len(requests), 1)
            for category in ("insurance", "pre_op", "post_op"):
                self.assertEqual(
                    (await self.invoke(session, agent, NEED | {"category": category}))[
                        "outcome"
                    ],
                    "failed",
                )
            self.assertEqual(len(requests), 1)
            self.assertEqual(
                (await self.invoke(session, agent, NEED | {"message": "x" * 2501}))[
                    "outcome"
                ],
                "failed",
            )
            self.assertEqual(len(requests), 1)

    async def test_office_disabled_and_no_configuration(self):
        from dataclasses import replace

        state = call_state()
        state.call = replace(state.call, called_office_key="crystal-river")
        async with self.setup_session(state=state) as (_, agent, _, owner, requests, _):
            self.assertNotIn(
                "create_staff_task", [tool.info.name for tool in agent.tools]
            )
            self.assertEqual((await owner.submit(**NEED))["outcome"], "failed")
            self.assertEqual(requests, [])
        async with self.setup_session(config=Config("offline")) as (
            session,
            agent,
            _,
            _,
            requests,
            _,
        ):
            self.assertEqual((await self.invoke(session, agent))["outcome"], "failed")
            self.assertEqual(requests, [])

    async def test_timeout_recovers_duplicate_with_identical_payload(self):
        count = 0

        async def timeout_then_duplicate(request, payload):
            nonlocal count
            count += 1
            if count == 1:
                raise httpx.ReadTimeout("synthetic timeout")
            return httpx.Response(200, json=receipt(payload, "duplicate"))

        async with self.setup_session(timeout_then_duplicate) as (
            session,
            agent,
            _,
            _,
            requests,
            _,
        ):
            self.assertEqual(
                (await self.invoke(session, agent))["outcome"], "duplicate"
            )
            self.assertEqual(requests[0], requests[1])

    async def test_invalid_receipts_timeouts_and_partial_delivery_are_ambiguous(self):
        for response in (
            None,
            {},
            {"status": "created", "taskId": TASK_ID},
            {
                "status": "created",
                "taskId": "bad",
                "category": "medication",
                "urgency": "normal",
            },
            {
                "status": "created",
                "taskId": TASK_ID,
                "category": "other",
                "urgency": "normal",
            },
        ):

            async def invalid(request, payload, response=response):
                if response is None:
                    raise httpx.ReadTimeout("synthetic")
                return httpx.Response(201, json=response)

            async with self.setup_session(invalid) as (
                session,
                agent,
                _,
                _,
                requests,
                _,
            ):
                result = await self.invoke(session, agent)
                self.assertEqual(result["outcome"], "ambiguous")
                self.assertEqual(requests[0], requests[1])
                self.assertNotIn("taskId", result)

    async def test_patient_switch_during_registered_write_keeps_old_receipt(self):
        started, release = asyncio.Event(), asyncio.Event()

        async def delayed(request, payload):
            started.set()
            await release.wait()
            return httpx.Response(201, json=receipt(payload))

        async with self.setup_session(delayed) as (
            session,
            agent,
            state,
            _owner,
            _requests,
            _,
        ):
            state.patient.active = patient()
            pending = asyncio.create_task(self.invoke(session, agent))
            await asyncio.wait_for(started.wait(), 2)
            state.patient.active = patient("Alex", "synthetic-2")
            release.set()
            result = await pending
            self.assertTrue(result["patientChanged"])
            self.assertEqual(result["patient"], {"name": "Jane", "verified": True})
            self.assertEqual(result["outcome"], "created")

    async def test_cancellation_and_concurrent_duplicates_retain_mutation(self):
        started, release = asyncio.Event(), asyncio.Event()

        async def delayed(request, payload):
            started.set()
            await release.wait()
            return httpx.Response(201, json=receipt(payload))

        async with self.setup_session(delayed) as (_, _, _, owner, requests, _):
            first = asyncio.create_task(owner.submit(**NEED))
            await started.wait()
            second = asyncio.create_task(owner.submit(**NEED))
            first.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await first
            release.set()
            self.assertEqual((await second)["outcome"], "duplicate")
            self.assertEqual((await owner.submit(**NEED))["outcome"], "duplicate")
            self.assertEqual(len(requests), 1)

    async def test_uncertainty_survives_later_rejection_and_can_recover(self):
        responses = [500, 503, 403, 200]

        async def handler(request, payload):
            status = responses.pop(0)
            return httpx.Response(status, json=receipt(payload, "duplicate"))

        async with self.setup_session(handler) as (session, agent, _, _, requests, _):
            self.assertEqual(
                (await self.invoke(session, agent))["outcome"], "ambiguous"
            )
            self.assertEqual(
                (await self.invoke(session, agent))["outcome"], "ambiguous"
            )
            self.assertEqual(
                (await self.invoke(session, agent))["outcome"], "duplicate"
            )
            self.assertTrue(all(p == requests[0] for p in requests))

    async def test_definite_rejection_can_be_retried(self):
        statuses = [403, 201]

        async def handler(request, payload):
            return httpx.Response(statuses.pop(0), json=receipt(payload))

        async with self.setup_session(handler) as (session, agent, _, _, requests, _):
            self.assertEqual((await self.invoke(session, agent))["outcome"], "failed")
            self.assertEqual((await self.invoke(session, agent))["outcome"], "created")
            self.assertEqual(requests[0], requests[1])

    async def test_registered_schema_preserves_policy_without_consent_input(self):
        from livekit.agents.llm.utils import build_strict_openai_schema

        async with self.setup_session() as (_, agent, _, _, _, _):
            schema = next(
                build_strict_openai_schema(t)["function"]
                for t in agent.tools
                if t.info.name == "create_staff_task"
            )
            self.assertEqual(
                set(schema["parameters"]["properties"]),
                {"category", "urgency", "summary", "message"},
            )
            self.assertIn("caller-approved", schema["description"])
            self.assertNotIn("characters", json.dumps(schema))

    def test_product_configuration(self):
        env = {
            "OPENAI_API_KEY": "offline",
            "ACUITY_PRODUCT_HANDOFF_URL": "https://product.example/v1/handoffs",
            "ABITA_EYE_GROUP_PRODUCT_SERVICE_SECRET": "test",
        }
        with patch.dict("os.environ", env, clear=True):
            self.assertEqual(
                load_config().staff_tasks_url, "https://product.example/v1/tasks"
            )
        for url in (
            "http://product.example/v1/handoffs",
            "https://user:secret@product.example/v1/handoffs",
            "https://product.example/wrong",
        ):
            with (
                patch.dict(
                    "os.environ", env | {"ACUITY_PRODUCT_HANDOFF_URL": url}, clear=True
                ),
                self.assertRaises(ValueError),
            ):
                load_config()
