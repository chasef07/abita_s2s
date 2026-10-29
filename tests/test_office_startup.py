import asyncio
from contextlib import nullcontext
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock, PropertyMock, patch

import httpx
from test_insurance_list import plan, plans_reply
from test_knowledge import KNOWLEDGE

from abita_s2s.agent import AbitaAgent
from abita_s2s.tools.registry import Owners
from abita_s2s.config import Config
from abita_s2s.insurance_list import UNAVAILABLE as INSURANCE_UNAVAILABLE
from abita_s2s.knowledge import UNAVAILABLE
from abita_s2s.integrations.patient_middleware import NotFound
from abita_s2s.offices import (
    OFFICES,
    SHARED_SCHEDULING_OFFICES,
    SPRING_HILL,
    e164,
    get_office_profile,
    get_office_profile_by_phone,
    get_product_office_key,
)
from abita_s2s.runtime.session_startup import (
    finish_voice_call,
    start_session,
    start_voice_call,
)


class OfficeRoutingTests(unittest.TestCase):
    def test_imported_entrypoint_loads_local_environment_without_overriding_exports(
        self,
    ):
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, ".env.local").write_text(
                "OPENAI_API_KEY=offline-file-key\nABITA_DOTENV_PROBE=from-file\n"
            )
            env = {
                **{k: v for k, v in os.environ.items() if k != "ABITA_DOTENV_PROBE"},
                "OPENAI_API_KEY": "offline-exported-key",
            }
            result = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    "import abita_s2s.main; import os; "
                    "assert os.environ['ABITA_DOTENV_PROBE'] == 'from-file'; "
                    "assert os.environ['OPENAI_API_KEY'] == 'offline-exported-key'",
                ],
                cwd=directory,
                env=env,
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_aliases_select_same_office(self):
        for phone in (*SPRING_HILL.trunk_numbers, "(727) 591-9997", "1-813-548-4830"):
            self.assertIs(get_office_profile_by_phone(phone), SPRING_HILL)

    def test_e164_keeps_a_given_country_code_and_reads_ten_digits_as_us(self):
        for given, expected in (
            ("+17275919997", "+17275919997"),
            ("(727) 591-9997", "+17275919997"),
            ("727.591.9997", "+17275919997"),
            ("1-813-548-4830", "+18135484830"),
            ("+1 (813) 548-4830", "+18135484830"),
            ("+4741234567", "+4741234567"),
            (" +44 20 7946 0958", "+442079460958"),
            ("591-9997", None),
            ("+0123456789", None),
            ("1234567890123456", None),
            ("", None),
            (None, None),
        ):
            with self.subTest(given=given):
                self.assertEqual(e164(given), expected)

    def test_all_production_routes(self):
        expected = {
            "spring-hill": ("+17275919997", "+18135484830"),
            "crystal-river": ("+13523202007",),
            "hollywood": ("+19542872010",),
            "sweetwater": (
                "+17864657475",
                "+17864654845",
                "+17866134310",
                "+17864657479",
                "+17864654836",
                "+17864654882",
            ),
            "north-miami-beach-optical": ("+13055095333",),
        }
        self.assertEqual({o.key for o in OFFICES}, set(expected))
        for key, numbers in expected.items():
            for number in numbers:
                self.assertIs(
                    get_office_profile_by_phone(number), get_office_profile(key)
                )
        for demo in ("+14843989071", "+18027878312", "+13207388132"):
            with self.assertRaises(ValueError):
                get_office_profile_by_phone(demo)

    def test_trunk_spelled_as_plus_ten_digits_routes_like_its_us_number(self):
        for office in OFFICES:
            for number in office.trunk_numbers:
                spelling = "+" + number.removeprefix("+1")
                with self.subTest(spelling=spelling):
                    self.assertIs(get_office_profile_by_phone(spelling), office)
                    self.assertEqual(
                        get_product_office_key(spelling),
                        get_product_office_key(number),
                    )
        self.assertEqual(get_product_office_key("+7864657479"), "sweetwater-optical")

    def test_offices_with_shared_scheduling_are_the_shared_scheduling_offices(self):
        self.assertEqual(
            {o.key for o in OFFICES if o.scheduling_offices},
            set(SHARED_SCHEDULING_OFFICES),
        )


class GreetingTests(unittest.IsolatedAsyncioTestCase):
    async def test_greeting_seeds_startup_history_and_is_spoken_once(self):
        agent = AbitaAgent(Owners(SPRING_HILL))
        seeded = [
            item
            for item in agent.chat_ctx.items
            if item.type == "message" and item.role == "developer"
        ]
        self.assertEqual(len(seeded), 1)
        self.assertIn(
            f'Practice name: "{SPRING_HILL.greeting_name}"', seeded[0].text_content
        )
        handle = asyncio.get_running_loop().create_future()
        handle.set_result(None)
        session = SimpleNamespace(generate_reply=Mock(return_value=handle))
        with patch.object(
            AbitaAgent, "session", new_callable=PropertyMock, return_value=session
        ):
            await agent.on_enter()
        session.generate_reply.assert_called_once_with(
            instructions=seeded[0].text_content
        )


class StartupTests(unittest.IsolatedAsyncioTestCase):
    async def test_session_and_greeting_start_while_phone_lookup_is_pending(self):
        entered, release, greeted = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def lookup(*args):
            entered.set()
            await release.wait()
            return NotFound(status="not_found")

        async def start_with_greeting(session, ctx, room_options, agent):
            def generate_reply(**kwargs):
                self.assertFalse(release.is_set())
                greeted.set()
                handle = asyncio.get_running_loop().create_future()
                handle.set_result(None)
                return handle

            with patch.object(
                AbitaAgent,
                "session",
                new_callable=PropertyMock,
                return_value=SimpleNamespace(generate_reply=generate_reply),
            ):
                await agent.on_enter()
            await start_session(session, ctx, room_options, agent)

        with (
            patch(
                "abita_s2s.runtime.session_startup.PatientMiddleware.resolve",
                side_effect=lookup,
            ),
            patch(
                "abita_s2s.runtime.session_startup.start_session",
                side_effect=start_with_greeting,
            ),
        ):
            task = asyncio.create_task(self.run_startup(False))
            try:
                await asyncio.wait_for(entered.wait(), 2)
                await asyncio.wait_for(greeted.wait(), 0.2)
            finally:
                release.set()
                await asyncio.wait_for(task, 2)

    async def run_startup(
        self,
        fake,
        trunk="+18135484830",
        env=None,
        config=None,
        model_error=None,
        simulation=None,
        product=None,
    ):
        participant = SimpleNamespace(
            identity="caller",
            attributes={
                "sip.trunkPhoneNumber": trunk,
                "sip.phoneNumber": "+15555550101",
                "sip.callID": "sip-test",
            },
        )
        ctx = SimpleNamespace(
            is_fake_job=lambda: fake,
            connect=AsyncMock(),
            wait_for_participant=AsyncMock(return_value=participant),
            room=SimpleNamespace(
                name="test-room",
                on=Mock(),
                off=Mock(),
                isconnected=lambda: True,
                remote_participants={"caller": participant},
            ),
            add_shutdown_callback=Mock(side_effect=self.addAsyncCleanup),
        )
        session = SimpleNamespace(
            start=AsyncMock(),
            on=Mock(),
            current_agent=SimpleNamespace(
                duplex_session=SimpleNamespace(session_id=None, on=Mock())
            ),
        )
        session_type = Mock(return_value=session)
        session_generic = MagicMock()
        session_generic.__getitem__.return_value = session_type
        with (
            patch(
                "abita_s2s.runtime.session_startup.httpx.AsyncClient",
                return_value=httpx.AsyncClient(transport=httpx.MockTransport(product)),
            )
            if product
            else nullcontext(),
            patch.dict("os.environ", env or {}, clear=True),
            patch(
                "abita_s2s.runtime.session_startup.load_config",
                return_value=config or Config("offline"),
            ),
            patch(
                "abita_s2s.runtime.session_startup.create_model",
                side_effect=model_error,
            ) as create_model,
            patch(
                "abita_s2s.runtime.session_startup.api.LiveKitAPI",
                return_value=SimpleNamespace(sip=Mock(), aclose=AsyncMock()),
            ) as sip_api,
            patch("abita_s2s.runtime.session_startup.AgentSession", session_generic),
            patch(
                "abita_s2s.runtime.session_startup.AbitaAgent", wraps=AbitaAgent
            ) as agent_factory,
        ):
            await start_voice_call(ctx, simulation=simulation)
            if fake or simulation is not None:
                sip_api.assert_not_called()
            else:
                sip_api.assert_called_once_with(failover=False)
        args = session.start.call_args.kwargs
        args["userdata"] = session_type.call_args.kwargs["userdata"]
        args["dependencies"] = agent_factory.call_args.args[0]
        args["office_knowledge"] = create_model.call_args.args[1]
        args["insurance_list"] = create_model.call_args.args[2]
        return ctx, args

    async def test_office_knowledge_and_insurance_list_load_concurrently(self):
        config = Config(
            "offline",
            product_secret="product-token",
            knowledge_url="https://product.test/v1/agent/knowledge/read",
            middleware_url="https://middleware.test",
            middleware_token="middleware-token",
        )
        requests, all_arrived = [], asyncio.Event()

        async def handler(request):
            if request.method == "POST" and request.url.host == "middleware.test":
                return httpx.Response(404, json={"status": "not_found"})
            requests.append(request)
            if len(requests) == 3:
                all_arrived.set()
            await asyncio.wait_for(all_arrived.wait(), 1)
            if request.url.host == "product.test":
                return httpx.Response(200, json=KNOWLEDGE["spring-hill"])
            coverage = request.url.params["coverage"]
            return httpx.Response(
                200,
                json=plans_reply("spring_hill", coverage, [plan(f"aetna-{coverage}")]),
            )

        _, args = await self.run_startup(False, config=config, product=handler)
        self.assertIn("Fax records requests to 555-0142.", args["office_knowledge"])
        self.assertIn("`aetna-medical`: accepted", args["insurance_list"])
        self.assertIn("`aetna-routine_vision`: accepted", args["insurance_list"])
        plans = args["userdata"].insurance.plans
        self.assertEqual(
            [plan.planId for plan in (*plans.medical, *plans.routine_vision)],
            ["aetna-medical", "aetna-routine_vision"],
        )
        self.assertEqual(
            {(r.url.host, r.url.params.get("office")) for r in requests},
            {("product.test", None), ("middleware.test", "spring-hill")},
        )

    async def test_failed_insurance_list_read_continues_with_the_notice(self):
        with self.assertLogs("abita_s2s.insurance_list", "ERROR") as logs:
            _, args = await self.run_startup(False)
        self.assertEqual(args["insurance_list"], INSURANCE_UNAVAILABLE)
        self.assertIsNone(args["userdata"].insurance.plans)
        self.assertIn("cause=medical:not_configured", logs.output[0])

    async def test_office_knowledge_reaches_the_model_and_a_failed_read_continues(
        self,
    ):
        config = Config(
            "offline",
            product_secret="product-token",
            knowledge_url="https://product.test/v1/agent/knowledge/read",
        )
        requests = []

        def read(status):
            def handler(request):
                requests.append(request)
                return httpx.Response(status, json=KNOWLEDGE["spring-hill"])

            return handler

        _, args = await self.run_startup(False, config=config, product=read(200))
        self.assertEqual(requests[-1].headers["x-office-key"], "spring-hill")
        self.assertIn("Medical records requests", args["office_knowledge"])
        self.assertIn("Fax records requests to 555-0142.", args["office_knowledge"])
        with self.assertLogs("abita_s2s.knowledge", "ERROR") as logs:
            _, args = await self.run_startup(False, config=config, product=read(503))
        self.assertEqual(args["office_knowledge"], UNAVAILABLE)
        self.assertIn("cause=http_503", logs.output[0])
        self.assertEqual(args["userdata"].call.called_office_key, "spring-hill")

    async def test_simulation_takes_synthetic_office_knowledge_from_userdata(self):
        sim = SimpleNamespace(
            userdata=lambda: {
                "office": "spring-hill",
                "office_knowledge": [
                    {"title": "Hours", "text": "Status: available\nDaily 7 AM to 9 PM."}
                ],
            },
            simulation_job_id="simulation-knowledge",
        )
        _, args = await self.run_startup(
            False,
            trunk="",
            simulation=sim,
            env={
                "SANDBOX_AMD_API_URL": "https://abita-middleware-sandbox-test.run.app",
                "SANDBOX_AMD_API_TOKEN": "sandbox-token",
            },
            config=Config(
                "offline",
                product_secret="product-token",
                knowledge_url="https://product.test/v1/agent/knowledge/read",
            ),
        )
        self.assertEqual(args["office_knowledge"], "### Hours\n\nDaily 7 AM to 9 PM.")

    async def test_simulation_takes_synthetic_insurance_plans_from_userdata(self):
        requests = []
        sim = SimpleNamespace(
            userdata=lambda: {
                "office": "spring-hill",
                "insurance_plans": {
                    "medical": [plan("aetna-hmo")],
                    "routine_vision": [],
                },
            },
            simulation_job_id="simulation-insurance",
        )
        _, args = await self.run_startup(
            False,
            trunk="",
            simulation=sim,
            env={
                "SANDBOX_AMD_API_URL": "https://abita-middleware-sandbox-test.run.app",
                "SANDBOX_AMD_API_TOKEN": "sandbox-token",
            },
            product=lambda request: requests.append(request) or httpx.Response(503),
        )
        self.assertEqual(
            args["insurance_list"],
            "### Medical visits\n\n#### Aetna\n\n- Aetna Hmo `aetna-hmo`: accepted\n\n"
            "### Routine vision exams\n\n"
            "This office does not accept insurance for routine vision exams.",
        )
        self.assertNotIn("/api/insurance/plans", [r.url.path for r in requests])
        self.assertEqual(
            args["userdata"].insurance.plans.medical[0].planId, "aetna-hmo"
        )

    async def test_named_deployment_sandboxes_call_control(self):
        for deployment, sandbox in ((None, False), ("staging", True)):
            with self.subTest(deployment=deployment):
                _, args = await self.run_startup(
                    False, config=Config("offline", deployment=deployment)
                )
                self.assertIs(args["dependencies"].call_control.sandbox, sandbox)

    async def test_simulation_uses_sandbox_and_non_sip_participant(self):
        sim = SimpleNamespace(
            userdata=lambda: {"office": "spring-hill"},
            simulation_job_id="simulation-intake",
        )
        ctx, args = await self.run_startup(
            False,
            trunk="",
            simulation=sim,
            env={
                "SANDBOX_AMD_API_URL": "https://abita-middleware-sandbox-test.run.app",
                "SANDBOX_AMD_API_TOKEN": "sandbox-token",
            },
            config=Config(
                "offline",
                middleware_url="https://production.test",
                middleware_token="production-token",
                product_secret="product-token",
                knowledge_url="https://product.test/v1/agent/knowledge/read",
                interaction_url="https://product.test/v1/ai/interactions",
                staff_tasks_url="https://product.test/v1/tasks",
            ),
            product=lambda request: httpx.Response(
                200,
                json={
                    "revisionId": "revision-1",
                    "entries": [{"id": "h", "title": "Hours", "text": "Weekdays."}],
                },
            ),
        )
        ctx.wait_for_participant.assert_awaited_once_with()
        self.assertEqual(args["room_options"].participant_identity, "caller")
        state = args["userdata"]
        self.assertEqual(state.call.call_id, "simulation-intake")
        self.assertIsNone(state.call.caller_phone)
        self.assertIsNone(state.reporter)
        config = args["dependencies"].insurance._middleware._config
        self.assertEqual(config.middleware_token, "sandbox-token")
        self.assertEqual(
            config.middleware_url, "https://abita-middleware-sandbox-test.run.app"
        )
        self.assertIsNone(config.staff_tasks_url)
        self.assertEqual(config.product_secret, "product-token")
        self.assertEqual(
            config.knowledge_url, "https://product.test/v1/agent/knowledge/read"
        )
        self.assertEqual(args["office_knowledge"], "### Hours\n\nWeekdays.")
        self.assertIsNone(config.interaction_url)
        self.assertIsNone(config.handoff)

    async def test_simulation_rejects_missing_or_non_sandbox_backend(self):
        sim = SimpleNamespace(userdata=lambda: {"office": "spring-hill"})
        for url in (
            "",
            "https://production.test",
            "https://abita-middleware-sandbox-test.run.app.evil.test",
        ):
            with (
                self.subTest(url=url),
                self.assertRaisesRegex(ValueError, "SANDBOX_AMD"),
            ):
                await self.run_startup(
                    False,
                    simulation=sim,
                    env={
                        "SANDBOX_AMD_API_URL": url,
                        "SANDBOX_AMD_API_TOKEN": "test",
                    },
                )

    async def test_sip_ignores_console_override_and_binds_caller(self):
        ctx, args = await self.run_startup(
            False, env={"ABITA_CONSOLE_OFFICE": "invalid"}
        )
        ctx.connect.assert_awaited_once()
        self.assertEqual(args["room_options"].participant_identity, "caller")
        self.assertTrue(args["room_options"].close_on_disconnect)
        self.assertTrue(args["room_options"].delete_room_on_close)
        self.assertEqual(args["agent"]._practice_name, SPRING_HILL.greeting_name)
        call = args["userdata"].call
        self.assertEqual(call.call_id, "sip-test")
        self.assertEqual(call.called_number, "+18135484830")
        self.assertEqual(call.called_office_key, "spring-hill")
        self.assertEqual(call.caller_phone, "+15555550101")
        self.assertIsNone(args["userdata"].patient.active)

    async def test_console_requires_explicit_office(self):
        with self.assertRaisesRegex(ValueError, "ABITA_CONSOLE_OFFICE"):
            await self.run_startup(True)
        ctx, args = await self.run_startup(
            True, env={"ABITA_CONSOLE_OFFICE": "spring-hill"}
        )
        _, other_args = await self.run_startup(
            True, env={"ABITA_CONSOLE_OFFICE": "spring-hill"}
        )
        self.assertNotEqual(
            args["userdata"].call.call_id, other_args["userdata"].call.call_id
        )
        self.assertIsNone(args["userdata"].call.caller_phone)
        ctx.connect.assert_not_awaited()
        ctx.wait_for_participant.assert_not_awaited()

    async def test_http_cleanup_is_registered_before_model_startup_can_fail(self):
        callbacks = []
        ctx = SimpleNamespace(
            is_fake_job=lambda: True,
            add_shutdown_callback=callbacks.append,
        )
        client = SimpleNamespace(aclose=AsyncMock())
        with (
            patch.dict("os.environ", {"ABITA_CONSOLE_OFFICE": "spring-hill"}),
            patch(
                "abita_s2s.runtime.session_startup.load_config",
                return_value=Config("offline"),
            ),
            patch(
                "abita_s2s.runtime.session_startup.httpx.AsyncClient",
                return_value=client,
            ),
            patch(
                "abita_s2s.runtime.session_startup.create_model",
                side_effect=RuntimeError("model startup failed"),
            ),
            self.assertRaisesRegex(RuntimeError, "model startup failed"),
        ):
            await start_voice_call(ctx)
        self.assertEqual(len(callbacks), 1)
        await callbacks[0]()
        client.aclose.assert_awaited_once()

    async def test_combined_shutdown_waits_for_other_writes_after_owner_failure(self):
        ctx, args = await self.run_startup(
            True, env={"ABITA_CONSOLE_OFFICE": "spring-hill"}
        )
        dependencies = args["dependencies"]
        finish, entered = asyncio.Event(), asyncio.Event()

        async def wait_for_write():
            entered.set()
            await finish.wait()

        client = dependencies.staff_tasks._product._client
        with (
            patch.object(
                dependencies.scheduling,
                "aclose",
                new=AsyncMock(side_effect=RuntimeError("write failed")),
            ),
            patch.object(dependencies.insurance, "aclose", new=wait_for_write),
        ):
            shutdown = asyncio.create_task(
                ctx.add_shutdown_callback.call_args_list[0].args[0]()
            )
            try:
                await asyncio.wait_for(entered.wait(), 2)
                self.assertFalse(client.is_closed)
                self.assertFalse(shutdown.done())
            finally:
                finish.set()
                with self.assertRaisesRegex(RuntimeError, "write failed"):
                    await shutdown
        self.assertTrue(client.is_closed)
        self._cleanups.pop()

    async def test_shutdown_closes_and_drains_call_control(self):
        ctx, args = await self.run_startup(
            True, env={"ABITA_CONSOLE_OFFICE": "spring-hill"}
        )
        control = args["dependencies"].call_control
        with (
            patch.object(control, "close_admission") as close_admission,
            patch.object(control, "aclose", new=AsyncMock()) as aclose,
        ):
            await ctx.add_shutdown_callback.call_args_list[0].args[0]()
        close_admission.assert_called_once_with()
        aclose.assert_awaited_once_with()

    async def test_product_closeout_waits_for_accepted_write_and_keeps_native_report(
        self,
    ):
        payloads = []

        async def handler(request):
            payloads.append(json.loads(request.content))
            return httpx.Response(
                201,
                json={
                    "status": "created",
                    "interactionId": "d3665980-68ce-4336-87af-e2ba40ad2e8e",
                },
            )

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        config = Config(
            "offline",
            product_secret="test",
            interaction_url="https://product.test/v1/ai/interactions",
        )
        with patch(
            "abita_s2s.runtime.session_startup.httpx.AsyncClient", return_value=client
        ):
            ctx, args = await self.run_startup(False, config=config)
        state = args["userdata"]
        ctx.primary_session = SimpleNamespace(userdata=state)
        report = {
            "chat_history": {"items": []},
            "events": [{"type": "close", "reason": "participant_disconnected"}],
            "usage": [],
        }
        ctx.make_session_report = Mock(
            return_value=SimpleNamespace(
                to_dict=lambda: report,
                chat_history=SimpleNamespace(items=[]),
            )
        )
        ctx.tagger = Mock()
        entered, release = asyncio.Event(), asyncio.Event()

        async def write():
            entered.set()
            await release.wait()
            state.reporter.appointment(
                {
                    "action": "BOOKED",
                    "externalPatientId": "original-patient",
                    "newAppointmentId": "42",
                    "bookingResult": {"status": "booked"},
                },
                "booked",
            )

        args["dependencies"].scheduling._write_task = asyncio.create_task(write())
        await entered.wait()
        shutdown = asyncio.create_task(finish_voice_call(ctx))
        await asyncio.sleep(0)
        self.assertFalse(client.is_closed)
        self.assertFalse(shutdown.done())
        release.set()
        await shutdown
        await ctx.add_shutdown_callback.call_args_list[0].args[0]()
        self.assertTrue(client.is_closed)
        self.assertEqual(
            [p["kind"] for p in payloads], ["START", "OUTCOME_CHECKPOINT", "CLOSEOUT"]
        )
        self.assertEqual(payloads[-1]["status"], "COMPLETED")
        self.assertEqual(payloads[-1]["transcript"], report)
        self.assertEqual(
            payloads[-1]["appointmentOutcome"]["externalPatientId"], "original-patient"
        )
        self.assertEqual(payloads[-1]["officePhone"], "+18135484830")

    async def test_model_start_failure_delivers_failed_closeout_before_shutdown(self):
        payloads = []

        async def handler(request):
            payloads.append(json.loads(request.content))
            return httpx.Response(
                201,
                json={
                    "status": "created",
                    "interactionId": "d3665980-68ce-4336-87af-e2ba40ad2e8e",
                },
            )

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        config = Config(
            "offline",
            product_secret="test",
            interaction_url="https://product.test/v1/ai/interactions",
        )
        with (
            patch(
                "abita_s2s.runtime.session_startup.httpx.AsyncClient",
                return_value=client,
            ),
            self.assertRaisesRegex(RuntimeError, "model failed"),
        ):
            await self.run_startup(
                False, config=config, model_error=RuntimeError("model failed")
            )
        self.assertEqual([p["kind"] for p in payloads], ["START", "CLOSEOUT"])
        self.assertEqual(payloads[-1]["status"], "FAILED")
        self.assertNotIn("transcript", payloads[-1])
