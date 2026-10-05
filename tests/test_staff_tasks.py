"""Registered AgentSession tool and Product transport, entirely synthetic/offline."""

import asyncio
import json
import unittest
from contextlib import asynccontextmanager
from dataclasses import replace
from unittest.mock import AsyncMock, Mock, patch

import httpx
from livekit.agents import AgentSession, llm
from test_knowledge import call_state

from abita_s2s.agent import AbitaAgent
from abita_s2s.config import Config, load_config
from abita_s2s.identity import PatientResolver
from abita_s2s.integrations.patient_middleware import Failure, Receipt
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
        state.reporter = Mock()
        if state.call.caller_phone is None:
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
                    if owner._close_task is None or not owner._close_task.done():
                        await owner.aclose()
                    await resolver.aclose()

    async def invoke(self, session, agent, args=None, tool="save_staff_task"):
        await asyncio.wait_for(
            session.run(
                user_input=json.dumps(
                    {"tool": tool, "args": NEED if args is None else args}
                )
            ),
            5,
        )
        outputs = [x for x in agent.chat_ctx.items if x.type == "function_call_output"]
        return outputs[-1].output

    async def test_pharmacy_details_update_one_draft_and_deliver_once(self):
        async with self.setup_session() as (session, agent, state, owner, requests, _):
            state.patient.active = patient()
            first = await self.invoke(session, agent)
            self.assertTrue(first.startswith("saved:"))
            self.assertIn("Not yet sent", first)
            self.assertIn("Patient: Jane (verified).", first)
            draft_id = first.split("Draft ID: ")[1].splitlines()[0]
            updated = NEED | {
                "draft_id": draft_id,
                "summary": "Eye drop prescription request",
                "message": "Caller requests eye drops from Publix, 8245 Northwest 88th Avenue, Tamarac, Florida 33321.",
            }
            for _ in range(3):
                result = await self.invoke(session, agent, updated)
                self.assertIn(f"Draft ID: {draft_id}", result)
            self.assertEqual(requests, [])
            state.reporter.record.assert_not_called()
            await asyncio.gather(owner.aclose(), owner.aclose())
            await owner.aclose()
            self.assertEqual(len(requests), 1)
            self.assertEqual(requests[0]["message"], updated["message"])
            self.assertEqual(requests[0]["summary"], updated["summary"])
            self.assertEqual(requests[0]["patient"]["id"], "synthetic-1")
            self.assertEqual(requests[0]["callerPhone"], "+15555550101")
            self.assertNotIn(TASK_ID, result)
            state.reporter.record.assert_called_once()
            self.assertEqual(state.reporter.record.call_args.args[0], "staff_task")
            self.assertEqual(state.reporter.record.call_args.args[1]["taskId"], TASK_ID)
            self.assertTrue(state.reporter.record.call_args.kwargs["call_id"])

    async def test_distinct_needs_in_same_category_remain_separate(self):
        async with self.setup_session() as (session, agent, _, owner, requests, _):
            await self.invoke(session, agent)
            await self.invoke(
                session,
                agent,
                NEED
                | {
                    "summary": "Separate medication authorization",
                    "message": "Caller also requests authorization for another medication.",
                },
            )
            self.assertEqual(requests, [])
            await owner.aclose()
            self.assertEqual(len(requests), 2)
            self.assertEqual(len({r["idempotencyKey"] for r in requests}), 2)

    async def test_identical_new_drafts_are_delivered_once(self):
        async with self.setup_session() as (_, _, _, owner, requests, _):
            first = owner.save(**NEED)
            repeated = owner.save(**NEED)
            self.assertEqual(first["draftId"], repeated["draftId"])
            updated = NEED | {"message": NEED["message"] + " Pharmacy is Publix."}
            owner.save(**updated, draft_id=repeated["draftId"])
            await owner.aclose()
            self.assertEqual(len(requests), 1)
            self.assertEqual(requests[0]["message"], updated["message"])

    async def test_withdrawn_request_is_not_sent(self):
        async with self.setup_session() as (session, agent, _, owner, requests, _):
            result = await self.invoke(session, agent)
            draft_id = result.split("Draft ID: ")[1].splitlines()[0]
            result = await self.invoke(
                session, agent, NEED | {"draft_id": draft_id, "cancel": True}
            )
            self.assertTrue(result.startswith("cancelled:"))
            self.assertEqual(owner.save(**NEED, draft_id=draft_id)["outcome"], "failed")
            await owner.aclose()
            self.assertEqual(requests, [])

    async def test_patient_switch_cannot_reassign_existing_draft(self):
        async with self.setup_session() as (_, _, state, owner, requests, _):
            state.patient.active = patient()
            draft = owner.save(**NEED)
            state.patient.active = patient("Alex", "synthetic-2")
            result = owner.save(**NEED, draft_id=draft["draftId"])
            self.assertEqual(result["outcome"], "failed")
            self.assertIn("Patient context changed", result["answer"])
            owner.save(**NEED)
            await owner.aclose()
            self.assertEqual(
                [r["patient"]["id"] for r in requests], ["synthetic-1", "synthetic-2"]
            )

    async def test_draft_follows_its_patient_from_pending_to_verified(self):
        async with self.setup_session() as (
            session,
            agent,
            state,
            owner,
            requests,
            middleware,
        ):
            await self.invoke(
                session, agent, {"firstName": "Jane", "dob": None}, "resolve_patient"
            )
            draft = owner.save(**NEED)
            self.assertFalse(draft["patient"]["verified"])
            middleware.resolve.return_value = patient("Doe, Jane")
            await self.invoke(
                session,
                agent,
                {"firstName": "Jane", "dob": "01/01/1980"},
                "resolve_patient",
            )
            self.assertIsNotNone(state.patient.active)
            result = owner.save(**NEED, draft_id=draft["draftId"])
            self.assertEqual(result["outcome"], "saved")
            self.assertTrue(result["patient"]["verified"])
            await owner.aclose()
            self.assertEqual([r["patient"]["id"] for r in requests], ["synthetic-1"])

    async def test_unresolved_patient_from_real_resolution_not_old_patient(self):
        async with self.setup_session() as (
            session,
            agent,
            state,
            owner,
            requests,
            middleware,
        ):
            state.patient.active = patient()
            middleware.resolve.return_value = Failure(reason="offline")
            await self.invoke(
                session, agent, {"firstName": "Alex", "dob": None}, "resolve_patient"
            )
            await self.invoke(session, agent)
            await owner.aclose()
            self.assertEqual(requests[0]["patient"], {"name": "Alex"})

    async def test_invalid_update_preserves_draft_and_unknown_id_creates_nothing(self):
        async with self.setup_session() as (_, _, _, owner, requests, _):
            self.assertEqual(
                owner.save(**NEED, draft_id="missing")["outcome"], "failed"
            )
            self.assertEqual(
                owner.save(**NEED, draft_id="missing", cancel=True)["outcome"], "failed"
            )
            self.assertEqual(owner.save(**NEED, cancel=True)["outcome"], "failed")
            saved = owner.save(**NEED)
            for changes in (
                {"message": "x" * 2501},
                {"category": "unsupported"},
                {"urgency": "urgent"},
            ):
                self.assertEqual(
                    owner.save(**(NEED | changes), draft_id=saved["draftId"])[
                        "outcome"
                    ],
                    "failed",
                )
            owner.close_admission()
            self.assertEqual(owner.save(**NEED)["outcome"], "failed")
            self.assertEqual(
                owner.save(**NEED, draft_id=saved["draftId"], cancel=True)["outcome"],
                "failed",
            )
            await owner.aclose()
            self.assertEqual(len(requests), 1)
            self.assertEqual(requests[0]["message"], NEED["message"])

    async def test_missing_identity_allowed_missing_contact_rejected(self):
        async with self.setup_session() as (_, _, state, owner, requests, _):
            self.assertEqual(owner.save(**NEED)["outcome"], "saved")
            state.call = replace(state.call, caller_phone=None)
            self.assertEqual(owner.save(**NEED)["outcome"], "failed")
            await owner.aclose()
            self.assertNotIn("patient", requests[0])

    async def test_sweetwater_preserves_inbound_number_and_product_location(self):
        office = get_office_profile("sweetwater")
        for phone in office.trunk_numbers:
            with self.subTest(phone=phone):
                state = call_state(office)
                state.call = replace(state.call, called_number=phone)
                async with self.setup_session(state=state) as (
                    _,
                    _,
                    _,
                    owner,
                    requests,
                    _,
                ):
                    self.assertEqual(owner.save(**NEED)["outcome"], "saved")
                    await owner.aclose()
                    self.assertEqual(len(requests), 1)
                    self.assertEqual(
                        requests[0]["officeKey"],
                        "sweetwater-optical"
                        if phone == "+17864657479"
                        else "sweetwater",
                    )
                    self.assertEqual(requests[0]["officePhone"], "+17864657475")
                    if phone == "+17864657475":
                        self.assertNotIn("inboundOfficePhone", requests[0])
                    else:
                        self.assertEqual(requests[0]["inboundOfficePhone"], phone)

    async def test_sweetwater_rejects_another_offices_inbound_number(self):
        state = call_state(get_office_profile("sweetwater"))
        state.call = replace(state.call, called_number="+19542872010")
        async with self.setup_session(state=state) as (_, _, _, owner, requests, _):
            self.assertEqual(owner.save(**NEED)["outcome"], "failed")
            await owner.aclose()
            self.assertEqual(requests, [])

    async def test_office_disabled_and_no_configuration(self):
        state = call_state()
        state.call = replace(state.call, called_office_key="crystal-river")
        async with self.setup_session(state=state) as (_, agent, _, owner, requests, _):
            self.assertNotIn(
                "save_staff_task", [tool.info.name for tool in agent.tools]
            )
            self.assertNotIn(
                "discard_staff_task", [tool.info.name for tool in agent.tools]
            )
            self.assertEqual(owner.save(**NEED)["outcome"], "failed")
            self.assertEqual(requests, [])
        async with self.setup_session(config=Config("offline")) as (
            session,
            agent,
            _,
            _,
            requests,
            _,
        ):
            self.assertTrue((await self.invoke(session, agent)).startswith("failed:"))
            self.assertEqual(requests, [])

    async def test_all_supported_categories_deliver_at_closeout(self):
        async with self.setup_session() as (_, _, state, owner, requests, _):
            for category in ("insurance", "pre_op", "post_op"):
                self.assertEqual(
                    owner.save(**(NEED | {"category": category}))["outcome"], "saved"
                )
            await owner.aclose()
            self.assertEqual(
                [r["category"] for r in requests], ["insurance", "pre_op", "post_op"]
            )
            self.assertEqual(state.reporter.record.call_count, 3)

    async def test_timeout_retries_identical_final_payload(self):
        async def handler(request, payload):
            if len(requests) == 1:
                raise httpx.ReadTimeout("synthetic")
            return httpx.Response(200, json=receipt(payload, "duplicate"))

        async with self.setup_session(handler) as (_, _, state, owner, requests, _):
            owner.save(**NEED)
            await owner.aclose()
            self.assertEqual(len(requests), 2)
            self.assertEqual(requests[0], requests[1])
            self.assertEqual(
                state.reporter.record.call_args.args[1]["outcome"], "duplicate"
            )

    async def test_failed_or_ambiguous_delivery_is_visible_and_not_resent_on_cleanup(
        self,
    ):
        for statuses, expected in (
            ([403], "failed"),
            ([500, 403], "ambiguous"),
            ([500, 503], "ambiguous"),
        ):
            with self.subTest(statuses=statuses):
                remaining = list(statuses)

                async def handler(request, payload):
                    return httpx.Response(remaining.pop(0))

                async with self.setup_session(handler) as (
                    _,
                    _,
                    state,
                    owner,
                    requests,
                    _,
                ):
                    owner.save(**NEED)
                    for _ in range(2):
                        with self.assertRaisesRegex(RuntimeError, "not confirmed"):
                            await owner.aclose()
                    self.assertEqual(len(requests), len(statuses))
                    self.assertEqual(
                        state.reporter.record.call_args.args[1]["outcome"], expected
                    )
                    self.assertTrue(all(r == requests[0] for r in requests))

    async def test_invalid_receipts_are_ambiguous(self):
        for response in (
            {},
            {"status": "created", "taskId": TASK_ID},
            receipt(NEED) | {"taskId": "bad"},
            receipt(NEED) | {"category": "other"},
        ):

            async def handler(request, payload):
                return httpx.Response(201, json=response)

            async with self.setup_session(handler) as (_, _, state, owner, requests, _):
                owner.save(**NEED)
                with self.assertRaisesRegex(RuntimeError, "not confirmed"):
                    await owner.aclose()
                self.assertEqual(len(requests), 2)
                self.assertEqual(
                    state.reporter.record.call_args.args[1]["outcome"], "ambiguous"
                )

    async def test_cancelled_close_waiter_does_not_cancel_delivery(self):
        started, release = asyncio.Event(), asyncio.Event()

        async def delayed(request, payload):
            started.set()
            await release.wait()
            return httpx.Response(201, json=receipt(payload))

        async with self.setup_session(delayed) as (_, _, state, owner, requests, _):
            owner.save(**NEED)
            closing = asyncio.create_task(owner.aclose())
            await asyncio.wait_for(started.wait(), 2)
            closing.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await closing
            release.set()
            await owner.aclose()
            self.assertEqual(len(requests), 1)
            self.assertEqual(
                state.reporter.record.call_args.args[1]["outcome"], "created"
            )

    async def test_registered_schema_preserves_policy_and_draft_identity(self):
        from livekit.agents.llm.utils import build_strict_openai_schema

        async with self.setup_session() as (_, agent, _, _, _, _):
            schema = next(
                build_strict_openai_schema(t)["function"]
                for t in agent.tools
                if t.info.name == "save_staff_task"
            )
            self.assertEqual(
                set(schema["parameters"]["properties"]),
                {"category", "urgency", "summary", "message", "draft_id", "cancel"},
            )
            self.assertIn("caller-approved", schema["description"])
            self.assertEqual(
                set(schema["parameters"]["properties"]["category"]["enum"]),
                {
                    "appointments",
                    "documentation",
                    "medication",
                    "optical",
                    "referrals",
                    "other",
                    "insurance",
                    "pre_op",
                    "post_op",
                },
            )
            self.assertEqual(
                [t.info.name for t in agent.tools if "staff_task" in t.info.name],
                ["save_staff_task"],
            )
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
