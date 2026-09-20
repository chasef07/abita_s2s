"""Behavioral identity tests using the real HTTP contract with offline records."""

import asyncio
import json
import unittest
from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
from insurance_fixtures import decision
from livekit.agents.llm.utils import build_strict_openai_schema

from abita_s2s.agent import AbitaAgent
from abita_s2s.config import Config
from abita_s2s.identity import (
    PatientResolver,
)
from abita_s2s.middleware import PatientMiddleware
from abita_s2s.name_matcher import phone_name_matches
from abita_s2s.offices import SPRING_HILL
from abita_s2s.state import CallContext, CallState

CONFIG = Config(
    "offline", middleware_url="https://middleware.test", middleware_token="test-auth"
)


def call_state(phone="+15555550101", office=SPRING_HILL):
    return CallState(
        CallContext("call", datetime.now(UTC), "abita", office.key, caller_phone=phone)
    )


def candidate(patient_id="chart-jane", name="Jane", dob="01/02/1980"):
    return {
        "status": "candidate",
        "patientId": patient_id,
        "firstName": name,
        "lastName": "Doe",
        "dob": dob,
    }


def receipt(patient_id="chart-jane", name="Jane", dob="01/02/1980", **extra):
    return {
        "status": "verified",
        "patientId": patient_id,
        "name": f"Doe, {name}",
        "dob": dob,
        "phone": "+15555550999",
        "insuranceCarrier": "Test Insurance",
        "insuranceDecision": decision(),
        "insPlanId": "private-plan",
        "respPartyId": "private-party",
        "routing": "optical_only",
        "preauthRequired": True,
        "appointmentsStatus": "none",
        "appointments": [],
        **extra,
    }


def search(*matches, complete=True):
    if not complete:
        return {"status": "unresolved", "reason": "incomplete_identity"}
    if not matches:
        return {"status": "not_found"}
    if len(matches) > 1:
        return {"status": "multiple_matches", "matches": list(matches)}
    m = matches[0]
    return receipt(m["patientId"], m["firstName"], m["dob"])


class PatientResolutionTests(unittest.IsolatedAsyncioTestCase):
    def resolver(self, responses, state=None):
        requests = []

        async def handler(request):
            requests.append(json.loads(request.content))
            body = responses.pop(0)
            if callable(body):
                return await body(request)
            return httpx.Response(200, json=body)

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        self.addAsyncCleanup(client.aclose)
        resolver = PatientResolver(
            state or call_state(), PatientMiddleware(client, CONFIG)
        )
        self.addAsyncCleanup(resolver.aclose)
        return resolver, requests

    async def preload(self, resolver):
        resolver.start_phone_lookup()
        await resolver._precall
        self.assertIsNone(resolver.state.patient.active)

    async def test_phone_fast_path_preserves_private_receipt_and_caller_separation(
        self,
    ):
        r, calls = self.resolver([receipt()])
        await self.preload(r)
        result = await r.resolve("Jane", None)
        self.assertEqual(result["outcome"], "verified")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["phone"], r.state.call.caller_phone)
        self.assertNotEqual(r.state.patient.active.phone, r.state.call.caller_phone)
        self.assertNotIn("insPlanId", r.state.patient.active.model_dump())
        self.assertNotIn("respPartyId", r.state.patient.active.model_dump())
        self.assertEqual(
            r.state.patient.active.insuranceDecision.canonicalPlan, "Aetna"
        )
        for private in (
            "chart-jane",
            "private-plan",
            "private-party",
            "01/02/1980",
            "+1555555",
        ):
            self.assertNotIn(private, json.dumps(result))
        self.assertEqual((await r.resolve("Jane", None))["outcome"], "verified")
        self.assertEqual(len(calls), 1)

    async def test_resolution_waits_for_shared_phone_lookup(self):
        started, release = asyncio.Event(), asyncio.Event()

        async def delayed(request):
            started.set()
            await release.wait()
            return httpx.Response(200, json=receipt())

        r, calls = self.resolver([delayed])
        r.start_phone_lookup()
        await started.wait()
        first = asyncio.create_task(r.resolve("Jane", None))
        duplicate = asyncio.create_task(r.resolve("Jane", None))
        try:
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            self.assertFalse(r._precall.done())
            self.assertIsNone(r.state.patient.active)
            first.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await first
        finally:
            release.set()
        self.assertEqual((await duplicate)["outcome"], "verified")
        self.assertEqual(r.state.patient.active.patientId, "chart-jane")
        self.assertEqual(len(calls), 1)

    async def test_no_caller_id_requires_dob_then_hydrates(self):
        r, calls = self.resolver([receipt()], call_state(None))
        r.start_phone_lookup()
        self.assertIsNone(r._precall)
        self.assertEqual(
            (await r.resolve(None, None))["answer"],
            "needs_input: What is the patient's first name?",
        )
        self.assertEqual(
            (await r.resolve("Jane", None))["answer"],
            "needs_input: What is the patient's date of birth?",
        )
        self.assertEqual(calls, [])
        self.assertEqual((await r.resolve(None, "01/02/1980"))["outcome"], "verified")
        self.assertEqual(
            calls[0],
            {"firstName": "Jane", "dob": "01/02/1980", "office": "+17275919997"},
        )
        self.assertEqual(len(calls), 1)

    async def test_shared_phone_and_caller_acting_for_other_patient(self):
        r, calls = self.resolver(
            [
                {
                    "status": "multiple_matches",
                    "matches": [candidate(), candidate("child", "John")],
                },
                receipt("child", "John"),
            ]
        )
        await self.preload(r)
        self.assertEqual((await r.resolve("John", None))["outcome"], "verified")
        self.assertEqual(r.state.patient.active.patientId, "child")
        self.assertEqual(calls[1]["patientId"], "child")

    async def test_patient_not_on_callers_phone_uses_name_dob(self):
        r, calls = self.resolver([receipt(), receipt("child", "John")])
        await self.preload(r)
        self.assertEqual(
            (await r.resolve("John", None))["answer"],
            "needs_input: What is the patient's date of birth?",
        )
        self.assertEqual((await r.resolve(None, "01/02/1980"))["outcome"], "verified")
        self.assertNotIn("phone", calls[1])
        self.assertEqual(r.state.patient.active.patientId, "child")

    async def test_same_name_requires_dob_and_same_dob_remains_ambiguous(self):
        for second_dob in ("02/03/1982", "01/02/1980"):
            r, calls = self.resolver(
                [
                    {
                        "status": "multiple_matches",
                        "matches": [candidate(), candidate("other", dob=second_dob)],
                    },
                    receipt(),
                ]
            )
            await self.preload(r)
            self.assertEqual(
                (await r.resolve("Jane", None))["answer"],
                "needs_input: What is the patient's date of birth?",
            )
            self.assertIsNone(r.state.patient.active)
            result = await r.resolve(None, "01/02/1980")
            self.assertEqual(
                result["outcome"],
                "multiple_matches" if second_dob == "01/02/1980" else "verified",
            )
            self.assertEqual(len(calls), 1 if second_dob == "01/02/1980" else 2)

    async def test_same_name_switch_with_supplied_conflicting_dob(self):
        r, _ = self.resolver(
            [
                {
                    "status": "multiple_matches",
                    "matches": [candidate(), candidate("other", dob="02/03/1982")],
                },
                receipt(),
                receipt("other", dob="02/03/1982"),
            ]
        )
        await self.preload(r)
        await r.resolve("Jane", "01/02/1980")
        # No DOB cannot silently choose the already-active member of this family.
        self.assertEqual((await r.resolve("Jane", None))["outcome"], "multiple_matches")
        self.assertEqual((await r.resolve("Jane", "02/03/1982"))["outcome"], "switched")
        self.assertEqual(r.state.patient.active.patientId, "other")

    async def test_changed_name_clears_active_immediately_and_does_not_inherit_dob(
        self,
    ):
        r, calls = self.resolver([receipt(), receipt("john", "John")])
        await self.preload(r)
        await r.resolve("Jane", "01/02/1980")
        self.assertEqual(
            (await r.resolve("John", None))["answer"],
            "needs_input: What is the patient's date of birth?",
        )
        self.assertIsNone(r.state.patient.active)
        self.assertEqual(len(calls), 1)
        self.assertEqual((await r.resolve(None, "01/02/1980"))["outcome"], "switched")

    async def test_conflicting_dob_never_activates_phone_candidate_and_correction_can_retry(
        self,
    ):
        r, _ = self.resolver([receipt(), search(), receipt()])
        await self.preload(r)
        self.assertEqual(
            (await r.resolve("Jane", "02/03/1982"))["outcome"], "not_found"
        )
        self.assertIsNone(r.state.patient.active)
        self.assertIsNotNone(r.state.patient.absence)
        self.assertEqual((await r.resolve(None, "01/02/1980"))["outcome"], "verified")
        self.assertIsNone(r.state.patient.absence)

    async def test_name_spelling_correction(self):
        r, _ = self.resolver([receipt()])
        await self.preload(r)
        self.assertEqual(
            (await r.resolve("Jame", None))["answer"],
            "needs_input: What is the patient's date of birth?",
        )
        self.assertEqual((await r.resolve("J-A-N-E", None))["outcome"], "verified")

    async def test_complete_absence_is_distinct_from_failed_partial_or_unexpected_search(
        self,
    ):
        for body, outcome in [
            (search(), "not_found"),
            (search(complete=False), "lookup_failed"),
            (search(candidate(), complete=False), "lookup_failed"),
            ({"status": "error"}, "lookup_failed"),
            (
                {
                    "status": "candidates",
                    "source": "first_name",
                    "complete": True,
                    "matches": [candidate()],
                },
                "lookup_failed",
            ),
        ]:
            with self.subTest(body=body):
                r, _ = self.resolver([body, body])
                result = await r.resolve("Jane", "01/02/1980")
                self.assertEqual(result["outcome"], outcome)
                expected = "no_results: " if outcome == "not_found" else "blocked: "
                self.assertTrue(result["answer"].startswith(expected), result["answer"])
                self.assertEqual(
                    r.state.patient.absence is not None, outcome == "not_found"
                )
                self.assertIsNone(r.state.patient.active)
                await r.resolve("John", None)
                self.assertIsNone(r.state.patient.absence)

    async def test_complete_search_multiple_charts_never_hydrates(self):
        r, calls = self.resolver([search(candidate(), candidate("other"))])
        self.assertEqual(
            (await r.resolve("Jane", "01/02/1980"))["outcome"], "multiple_matches"
        )
        self.assertEqual(len(calls), 1)
        self.assertIsNone(r.state.patient.active)

    async def test_invalid_hydrated_receipts_never_activate_or_establish_absence(self):
        for override in (
            {"name": "Doe, John"},
            {"dob": "02/03/1982"},
            {"patientId": ""},
            {"name": None},
            {"dob": None},
            {"appointments": [{}]},
            {"appointmentsStatus": "unknown"},
        ):
            with self.subTest(override=override):
                r, _ = self.resolver(
                    [{**receipt(), **override}, {**receipt(), **override}]
                )
                self.assertEqual(
                    (await r.resolve("Jane", "01/02/1980"))["outcome"], "lookup_failed"
                )
                self.assertIsNone(r.state.patient.active)
                self.assertIsNone(r.state.patient.absence)

    async def test_invalid_dob_is_rejected_before_read(self):
        for dob in ("02/30/1980", "13/01/1980", "01/01/2999", "1980-01-01", ""):
            r, calls = self.resolver([])
            self.assertEqual(
                (await r.resolve("Jane", dob))["answer"],
                "needs_input: Ask for a corrected date of birth in MM/DD/YYYY.",
            )
            self.assertEqual(calls, [])

    async def test_appointment_load_error_is_retained_and_reloaded(self):
        r, calls = self.resolver([receipt(appointmentsStatus="error"), receipt()])
        self.assertEqual((await r.resolve("Jane", "01/02/1980"))["outcome"], "verified")
        self.assertEqual(r.state.patient.active.appointmentsStatus, "error")
        await r.resolve("Jane", None)
        self.assertEqual(r.state.patient.active.appointmentsStatus, "none")
        self.assertEqual(len(calls), 2)

    async def test_duplicates_share_one_pending_read(self):
        started, release = asyncio.Event(), asyncio.Event()

        async def delayed(request):
            started.set()
            await release.wait()
            return httpx.Response(200, json=search(candidate()))

        r, calls = self.resolver([delayed])
        first = asyncio.create_task(r.resolve("Jane", "01/02/1980"))
        await started.wait()
        duplicate = asyncio.create_task(r.resolve("Jane", "01/02/1980"))
        await asyncio.sleep(0)
        release.set()
        results = await asyncio.gather(first, duplicate)
        self.assertEqual([v["outcome"] for v in results], ["verified", "verified"])
        self.assertEqual(len(calls), 1)

    async def test_superseded_read_cannot_commit_even_if_transport_ignores_cancellation(
        self,
    ):
        started, release = asyncio.Event(), asyncio.Event()

        async def delayed(request):
            started.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                await release.wait()
            return httpx.Response(200, json=receipt())

        r, _ = self.resolver(
            [
                delayed,
                receipt("john", "John"),
            ]
        )
        first = asyncio.create_task(r.resolve("Jane", "01/02/1980"))
        await started.wait()
        self.assertEqual((await r.resolve("John", "01/02/1980"))["outcome"], "verified")
        release.set()
        self.assertEqual((await first)["outcome"], "superseded")
        self.assertEqual(r.state.patient.active.patientId, "john")

    async def test_cancelled_duplicate_waiter_leaves_shared_lookup_running(self):
        for cancelled_index in (0, 1):
            with self.subTest(cancelled_index=cancelled_index):
                started, release = asyncio.Event(), asyncio.Event()

                async def delayed(request):
                    started.set()
                    await release.wait()
                    return httpx.Response(200, json=search(candidate()))

                r, calls = self.resolver([delayed])
                first = asyncio.create_task(r.resolve("Jane", "01/02/1980"))
                await started.wait()
                second = asyncio.create_task(r.resolve("Jane", "01/02/1980"))
                await asyncio.sleep(0)
                tasks = (first, second)
                tasks[cancelled_index].cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await tasks[cancelled_index]
                release.set()
                result = await tasks[1 - cancelled_index]
                self.assertEqual(result["outcome"], "verified")
                self.assertEqual(r.state.patient.active.patientId, "chart-jane")
                self.assertEqual(len(calls), 1)

    async def test_lookup_finishes_after_its_only_waiter_leaves(self):
        started, release = asyncio.Event(), asyncio.Event()

        async def delayed(request):
            started.set()
            await release.wait()
            return httpx.Response(200, json=search(candidate()))

        r, calls = self.resolver([delayed])
        task = asyncio.create_task(r.resolve("Jane", "01/02/1980"))
        await started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertIsNone(r.state.patient.active)
        release.set()
        self.assertEqual((await r._task)["outcome"], "verified")
        self.assertIsNone(r._token)
        self.assertEqual((await r.resolve("Jane", "01/02/1980"))["outcome"], "verified")
        self.assertEqual(len(calls), 1)

    async def test_shutdown_fences_read_even_if_transport_swallows_cancellation(
        self,
    ):
        started, cancelled, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def delayed(request):
            started.set()
            while not release.is_set():
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    cancelled.set()
            return httpx.Response(200, json=receipt())

        r, _ = self.resolver(
            [
                delayed,
            ]
        )
        task = asyncio.create_task(r.resolve("Jane", "01/02/1980"))
        await started.wait()
        closing = asyncio.create_task(r.aclose())
        await cancelled.wait()
        self.assertIsNone(r._token)
        release.set()
        await closing
        self.assertEqual((await task)["outcome"], "superseded")
        self.assertIsNone(r.state.patient.active)

    async def test_appointment_references_remain_private(self):
        appt = {
            "id": 123,
            "date": "10/01/2026",
            "time": "9:00 AM",
            "provider": "Doctor Example",
            "officeId": "office-ref",
            "office": "Spring Hill",
            "cancellationToken": "cancel-private",
            "rescheduleToken": "reschedule-private",
        }
        r, _ = self.resolver([receipt(appointmentsStatus="found", appointments=[appt])])
        await self.preload(r)
        result = await r.resolve("Jane", None)
        self.assertEqual(r.state.patient.active.appointmentsStatus, "found")
        self.assertEqual(r.state.patient.active.appointments[0].officeId, "office-ref")
        self.assertEqual(
            r.state.patient.active.appointments[0].cancellationToken,
            "cancel-private",
        )
        self.assertNotIn("cancel-private", json.dumps(result))
        self.assertNotIn("123", json.dumps(result))

    async def test_phone_absence_and_failure_never_establish_registration_absence(self):
        for body, status in [
            ({"status": "not_found"}, "none"),
            ({"status": "error"}, "failed"),
            (search(candidate(), complete=False), "failed"),
            (
                {"status": "multiple_matches", "matches": [candidate(), candidate()]},
                "failed",
            ),
            ({**receipt(), "name": None}, "failed"),
        ]:
            r, _ = self.resolver([body, body])
            await self.preload(r)
            self.assertEqual(r.state.patient.lookup.status, status)
            self.assertIsNone(r.state.patient.absence)
            self.assertEqual(
                (await r.resolve("Jane", None))["answer"],
                "needs_input: What is the patient's date of birth?",
            )

    async def test_correction_while_phone_lookup_is_pending_uses_latest_identity(self):
        started, release = asyncio.Event(), asyncio.Event()

        async def delayed(request):
            started.set()
            await release.wait()
            return httpx.Response(
                200,
                json={
                    "status": "multiple_matches",
                    "matches": [candidate(), candidate("john", "John")],
                },
            )

        r, calls = self.resolver([delayed, receipt("john", "John")])
        r.start_phone_lookup()
        await started.wait()
        first = asyncio.create_task(r.resolve("Jane", None))
        await asyncio.sleep(0)
        latest = asyncio.create_task(r.resolve("John", None))
        await asyncio.sleep(0)
        release.set()
        self.assertEqual((await first)["outcome"], "superseded")
        self.assertEqual((await latest)["outcome"], "verified")
        self.assertEqual(r.state.patient.active.patientId, "john")
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[1]["patientId"], "john")

    async def test_shutdown_fences_pending_phone_lookup(self):
        started, cancelled, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def delayed(request):
            started.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                cancelled.set()
                await release.wait()
            return httpx.Response(200, json=receipt())

        r, _ = self.resolver([delayed])
        r.start_phone_lookup()
        await started.wait()
        resolving = asyncio.create_task(r.resolve("Jane", None))
        await asyncio.sleep(0)
        closing = asyncio.create_task(r.aclose())
        await cancelled.wait()
        release.set()
        await closing
        self.assertEqual((await resolving)["outcome"], "superseded")
        self.assertIsNone(r.state.patient.active)
        self.assertEqual(r.state.patient.lookup.status, "not_attempted")

    async def test_close_fences_result_and_rejects_new_work(self):
        r, calls = self.resolver([])
        await r.aclose()
        self.assertEqual((await r.resolve("Jane", None))["outcome"], "superseded")
        r.start_phone_lookup()
        self.assertEqual(calls, [])

    async def test_tool_schema_and_cross_call_guard(self):
        r, calls = self.resolver([])
        agent = AbitaAgent(SPRING_HILL, None, r)
        schema = build_strict_openai_schema(agent.resolve_patient)["function"]
        self.assertEqual(set(schema["parameters"]["properties"]), {"firstName", "dob"})
        self.assertFalse(schema["parameters"]["additionalProperties"])
        for value in schema["parameters"]["properties"].values():
            self.assertEqual(value["type"], ["string", "null"])
        result = await agent.resolve_patient(
            SimpleNamespace(userdata=call_state()), "Jane", None
        )
        self.assertEqual(
            result, "blocked: Patient lookup is unavailable. Ask office staff for help."
        )
        self.assertEqual(calls, [])

    def test_phone_fuzzy_thresholds_match_agent_examples(self):
        for left, right in [
            ("Amy", "Emmy"),
            ("Jonatham", "Jonathan"),
            ("J-A-N-E", "Jane"),
        ]:
            self.assertTrue(phone_name_matches(left, right))
        for left, right in [
            ("Amy", "Emma"),
            ("Ann", "Joanne"),
            ("Alex", "Alexander"),
            ("Jame", "Jane"),
        ]:
            self.assertFalse(phone_name_matches(left, right))
