"""Exercise all three registered tools through LiveKit AgentSession, without APIs."""

import asyncio
import json
import unittest
from unittest.mock import AsyncMock, patch

import httpx
from livekit.agents import AgentSession, llm
from livekit.agents.llm.utils import build_strict_openai_schema
from test_insurance_registration import created, registration, updated
from test_patient_resolution import CONFIG, call_state, receipt, search

from abita_s2s.agent import AbitaAgent
from abita_s2s.identity import PatientResolver
from insurance_fixtures import check_response
from abita_s2s.insurance import InsuranceRegistration
from abita_s2s.insurance_state import insurance_ready
from abita_s2s.middleware import PatientMiddleware
from abita_s2s.offices import SPRING_HILL
from abita_s2s.registration_middleware import RegistrationMiddleware


class InsuranceModel(llm.LLM):
    def __init__(self):
        super().__init__()
        self.requests = []

    def chat(self, *, chat_ctx, tools=None, conn_options, **kwargs):
        self.requests.append(chat_ctx.copy())
        return InsuranceStream(
            self, chat_ctx=chat_ctx, tools=tools or [], conn_options=conn_options
        )


class InsuranceStream(llm.LLMStream):
    async def _run(self):
        items = self._chat_ctx.items
        last = max(
            i
            for i, item in enumerate(items)
            if item.type == "message" and item.role == "user"
        )
        outputs = [item for item in items[last:] if item.type == "function_call_output"]
        if outputs:
            delta = llm.ChoiceDelta(
                role="assistant", content=outputs[-1].output if outputs[-1].name in ("add_patient", "resolve_patient", "check_insurance") else json.loads(outputs[-1].output)["answer"]
            )
        else:
            name, args = json.loads(items[last].text_content)
            delta = llm.ChoiceDelta(
                role="assistant",
                tool_calls=[
                    llm.FunctionToolCall(
                        name=name, arguments=json.dumps(args), call_id=f"call-{last}"
                    )
                ],
            )
        self._event_ch.send_nowait(llm.ChatChunk(id="offline", delta=delta))


class InsuranceSessionTests(unittest.IsolatedAsyncioTestCase):
    async def test_registered_tools_create_update_duplicate_and_correction(self):
        bodies = [
            created(),
            receipt("new-chart", insuranceCarrier="Aetna"),
            updated(patientId="new-chart", newInsurance="VSP"),
        ]
        requests = []

        def handler(request):
            if request.url.path == "/api/insurance/decision":
                return check_response(request)
            requests.append((request.url.path, json.loads(request.content)))
            return httpx.Response(200, json=bodies.pop(0))

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            state = call_state()
            resolver = PatientResolver(state, PatientMiddleware(client, CONFIG))
            owner = InsuranceRegistration(
                state, resolver, RegistrationMiddleware(client, CONFIG)
            )
            self.addAsyncCleanup(resolver.aclose)
            self.addAsyncCleanup(owner.aclose)
            agent = AbitaAgent(SPRING_HILL, None, resolver, owner)
            model = InsuranceModel()
            async with AgentSession(llm=model, userdata=state) as session:
                with patch.object(AbitaAgent, "on_enter", new=AsyncMock()):
                    await session.start(agent=agent)
                cases = [
                    ("add_patient", registration().model_dump(), "needs_input"),
                    (
                        "check_insurance",
                        {"plan": "Aetna", "coverageType": "medical"},
                        "accepted",
                    ),
                    (
                        "add_patient",
                        registration(readBack=None).model_dump(),
                        "needs_input",
                    ),
                    ("add_patient", registration().model_dump(), "success"),
                    ("add_patient", registration().model_dump(), "success"),
                    (
                        "check_insurance",
                        {"plan": "VSP", "coverageType": "routine_vision"},
                        "accepted",
                    ),
                    (
                        "update_insurance",
                        {"insuranceMemberId": "member-private"},
                        "updated",
                    ),
                    (
                        "update_insurance",
                        {"insuranceMemberId": "member-private"},
                        "updated",
                    ),
                    (
                        "resolve_patient",
                        {"firstName": "John", "dob": None},
                        "needs_input",
                    ),
                    (
                        "update_insurance",
                        {"insuranceMemberId": "member-private"},
                        "needs_resolution",
                    ),
                ]
                for name, args, outcome in cases:
                    await asyncio.wait_for(
                        session.run(user_input=json.dumps([name, args])), 5
                    )
                    output = [
                        item
                        for item in model.requests[-1].items
                        if item.type == "function_call_output"
                    ][-1]
                    if name == "check_insurance":
                        self.assertIn("participates", output.output)
                    elif name in ("add_patient", "resolve_patient"):
                        self.assertTrue(output.output.startswith(outcome + ": "), output.output)
                    else:
                        self.assertEqual(json.loads(output.output)["outcome"], outcome)
                    self.assertNotIn("new-chart", output.output)
            self.assertEqual(
                [r[0] for r in requests],
                [
                    "/api/add-patient",
                    "/api/patient/resolve",
                    "/api/patient/update-insurance",
                ],
            )
            self.assertEqual(requests[-1][1]["patientId"], "new-chart")
            self.assertFalse(insurance_ready(state, "routine_vision"))

    async def test_partial_and_uncertain_creation_through_session(self):
        for backend, outcome in [
            (created("partial"), "partial"),
            ({"status": "error", "outcome": "indeterminate_write"}, "uncertain"),
        ]:
            bodies = [search(), backend]
            requests = []

            def handler(request, requests=requests, bodies=bodies):
                if request.url.path == "/api/insurance/decision":
                    return check_response(request)
                requests.append(request.url.path)
                return httpx.Response(200, json=bodies.pop(0))

            async with httpx.AsyncClient(
                transport=httpx.MockTransport(handler)
            ) as client:
                state = call_state()
                resolver = PatientResolver(state, PatientMiddleware(client, CONFIG))
                owner = InsuranceRegistration(
                    state, resolver, RegistrationMiddleware(client, CONFIG)
                )
                self.addAsyncCleanup(resolver.aclose)
                self.addAsyncCleanup(owner.aclose)
                model = InsuranceModel()
                async with AgentSession(llm=model, userdata=state) as session:
                    with patch.object(AbitaAgent, "on_enter", new=AsyncMock()):
                        await session.start(
                            agent=AbitaAgent(SPRING_HILL, None, resolver, owner)
                        )
                    for name, args in [
                        ("resolve_patient", {"firstName": "Jane", "dob": "01/02/1980"}),
                        (
                            "check_insurance",
                            {"plan": "Self Pay", "coverageType": "medical"},
                        ),
                        ("add_patient", registration().model_dump()),
                        ("add_patient", registration().model_dump()),
                    ]:
                        await asyncio.wait_for(
                            session.run(user_input=json.dumps([name, args])), 5
                        )
                    output = [
                        item
                        for item in model.requests[-1].items
                        if item.type == "function_call_output"
                    ][-1]
                    self.assertTrue(output.output.startswith("blocked: "), output.output)
                    if outcome == "partial":
                        self.assertIn("Created the patient chart", output.output)
                        self.assertIn("do not create another chart", output.output)
                    else:
                        self.assertIn("Do not repeat this write", output.output)
                    self.assertFalse(insurance_ready(state, "medical"))
                self.assertEqual(len(requests), 2)

    def test_schema_preserves_top_level_inputs_and_nullable_identity(self):
        agent = AbitaAgent(SPRING_HILL, None)
        for tool in [
            agent.check_insurance,
            agent.add_patient,
            agent.update_insurance,
            agent.resolve_patient,
        ]:
            schema = build_strict_openai_schema(tool)
            self.assertFalse(schema["function"]["parameters"]["additionalProperties"])
        schema = build_strict_openai_schema(agent.add_patient)["function"]["parameters"]
        self.assertEqual(set(schema["properties"]), set(registration().model_dump()))
        self.assertNotIn("registration", schema["properties"])
        self.assertNotIn("ssnLast4", schema["properties"])
        self.assertNotIn("newPatientConfirmed", schema["properties"])

    async def test_registered_creation_survives_session_interruption(self):
        entered, finish = asyncio.Event(), asyncio.Event()
        writes = []

        async def handler(request):
            if request.url.path == "/api/insurance/decision":
                return check_response(request)
            if request.url.path == "/api/patient/resolve":
                return httpx.Response(200, json=search())
            writes.append(request.url.path)
            entered.set()
            await finish.wait()
            return httpx.Response(200, json=created())

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            state = call_state()
            resolver = PatientResolver(state, PatientMiddleware(client, CONFIG))
            owner = InsuranceRegistration(
                state, resolver, RegistrationMiddleware(client, CONFIG)
            )
            self.addAsyncCleanup(resolver.aclose)
            self.addAsyncCleanup(owner.aclose)
            model = InsuranceModel()
            async with AgentSession(llm=model, userdata=state) as session:
                with patch.object(AbitaAgent, "on_enter", new=AsyncMock()):
                    await session.start(
                        agent=AbitaAgent(SPRING_HILL, None, resolver, owner)
                    )
                for name, args in [
                    ("resolve_patient", {"firstName": "Jane", "dob": "01/02/1980"}),
                    (
                        "check_insurance",
                        {"plan": "Self Pay", "coverageType": "medical"},
                    ),
                ]:
                    await asyncio.wait_for(
                        session.run(user_input=json.dumps([name, args])), 5
                    )
                run = session.run(
                    user_input=json.dumps(["add_patient", registration().model_dump()])
                )
                await asyncio.wait_for(entered.wait(), 5)
                interrupted = session.interrupt(force=True)
                self.assertTrue(state.insurance.write_pending)
                finish.set()
                await asyncio.wait_for(interrupted, 5)
                await asyncio.wait_for(run, 5)
                await owner.aclose()
                self.assertEqual(state.patient.active.patientId, "new-chart")
                self.assertFalse(state.insurance.write_uncertain)
                self.assertEqual(writes, ["/api/add-patient"])
